"""Invoicing logic. Views stay thin; every invoice, credit note and share is made here and posted in the same
transaction (apps/invoicing/posting.py), so an invoice without its entry cannot exist.

Two kinds of invoice:

- **A visit's invoice** is made by the program. `sync_appointment` compares what the visit should bill right now (the
  visit fee and its active services, once the patient is in the center and has paid, or once the visit is finished)
  with the lines already invoiced, takes back the lines that changed by a credit note and bills what is missing. It
  can be called any number of times; reception keeps its fast "pay at the desk" flow and never sees a form.
- **An account invoice** is issued by a person for what waits on a patient's account: operations that are done,
  stays that ended, extra charges. Her deposit is applied to it.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext as _

from apps.accounts.permissions import has_perm
from apps.ledger import chart
from apps.ledger import services as ledger
from apps.ledger.money import ZERO, allocate, money, percent_of
from apps.scheduling.models import Appointment, AppointmentStatus, BookingSource
from apps.theatre.models import Admission, AdmissionStatus, Operation, OperationStatus

from . import posting
from .models import (
    Charge,
    ChargeKind,
    Invoice,
    InvoiceKind,
    InvoiceLine,
    InvoiceSequence,
    InvoiceSettings,
    InvoiceShare,
    LineKind,
    PatientCoverage,
    Payer,
)

log = logging.getLogger(__name__)

# the patient is in the center (or was): from here on a visit can be invoiced
IN_THE_CENTER = (AppointmentStatus.ARRIVED, AppointmentStatus.IN_CONSULTATION, AppointmentStatus.COMPLETED)
OPERATION_BILLABLE = (OperationStatus.RECOVERY, OperationStatus.DONE)
OPERATION_OFF = (OperationStatus.CANCELLED, OperationStatus.POSTPONED)
CHARGE_REVENUE = {
    ChargeKind.SERVICE: "revenue_services",
    ChargeKind.SUPPLY: "revenue_services",
    ChargeKind.FEE: "revenue_services",
    ChargeKind.OTHER: "revenue_other",
}


class InvoiceError(Exception):
    """Refused. The message is written for the person at the screen."""


# --- what a line will be -----------------------------------------------------------------------------------------


@dataclass
class Share:
    amount: Decimal
    doctor: object = None
    payee_name: str = ""
    role: str = ""
    percent: Decimal | None = None


@dataclass
class Spec:
    """One line about to be invoiced."""

    kind: str
    description: str
    quantity: int
    unit_price: Decimal
    gross: Decimal
    revenue_key: str
    center_key: str
    discount: Decimal = ZERO
    doctor: object = None
    shares: list = field(default_factory=list)
    source: tuple = ("", None)  # ("appointment" | "service_order" | "operation" | "admission" | "charge", object)

    @property
    def source_key(self) -> tuple:
        name, obj = self.source
        return name, getattr(obj, "pk", None)

    @property
    def net(self) -> Decimal:
        return self.gross - self.discount


def _share_of(amount, percent, doctor) -> list:
    part = percent_of(amount, percent) if percent else ZERO
    return [Share(amount=part, doctor=doctor, percent=Decimal(str(percent)))] if part > 0 and doctor else []


def appointment_specs(appt) -> list[Spec]:
    """What this visit should have on invoices right now. Empty until the patient is in the center and either paid
    something or the visit is finished (money taken before that sits on her account as a credit)."""
    if appt.status not in IN_THE_CENTER:
        return []
    if appt.status != AppointmentStatus.COMPLETED:
        paid = sum((p.signed_amount for p in appt.payments.all()), ZERO)
        if paid <= 0:
            return []
    emergency = appt.source == BookingSource.EMERGENCY
    center = "emergency" if emergency else "outpatient"
    specs = []
    price = money(appt.price)
    if price > 0:
        discount = min(money(appt.discount), price)
        specs.append(
            Spec(
                kind=LineKind.VISIT,
                description=appt.visit_type.name_ar,
                quantity=1,
                unit_price=price,
                gross=price,
                discount=discount,
                revenue_key="revenue_emergency" if emergency else "revenue_visits",
                center_key=center,
                doctor=appt.doctor,
                shares=_share_of(price - discount, appt.doctor.visit_share_percent, appt.doctor),
                source=("appointment", appt),
            )
        )
    for order in appt.service_orders.select_related("service", "doctor"):
        total = money(order.total)
        if not order.is_active or total <= 0:
            continue
        specs.append(
            Spec(
                kind=LineKind.SERVICE,
                description=order.name,
                quantity=order.quantity,
                unit_price=money(order.unit_price),
                gross=total,
                revenue_key=order.service.revenue_account_key or "revenue_services",
                center_key=center,
                doctor=order.doctor,
                shares=_share_of(total, order.doctor_share_percent, order.doctor),
                source=("service_order", order),
            )
        )
    return specs


def operation_spec(op) -> Spec:
    """The operation's package price. What the team is paid for it comes out of the package: each member with a fee
    is a share (a doctor of the center on their own account, an outside name on the outside-team account)."""
    shares = [
        Share(amount=money(m.fee), doctor=m.doctor if m.doctor_id else None, payee_name=m.name,
              role=m.get_role_display())
        for m in op.team.all()
        if m.fee and m.fee > 0
    ]  # fmt: skip
    price = money(op.price)
    return Spec(
        kind=LineKind.OPERATION, description=op.procedure_name, quantity=1, unit_price=price, gross=price,
        revenue_key="revenue_operations", center_key="theatre", doctor=op.surgeon, shares=shares,
        source=("operation", op),
    )  # fmt: skip


def admission_spec(adm) -> Spec:
    nights = adm.nights()
    rate = money(adm.nightly_rate)
    description = _("إقامة: %(r)s") % {"r": adm.room.name}
    return Spec(
        kind=LineKind.ROOM, description=description, quantity=nights, unit_price=rate, gross=money(rate * nights),
        revenue_key="revenue_rooms", center_key="inpatient", doctor=adm.doctor, source=("admission", adm),
    )  # fmt: skip


def charge_spec(charge) -> Spec:
    total = money(charge.total)
    center = "theatre" if charge.operation_id else "inpatient" if charge.admission_id else "outpatient"
    return Spec(
        kind=LineKind.CHARGE, description=charge.description, quantity=charge.quantity,
        unit_price=money(charge.unit_price), gross=total, revenue_key=CHARGE_REVENUE[charge.kind],
        center_key=center, doctor=charge.doctor,
        shares=_share_of(total, charge.doctor_share_percent, charge.doctor), source=("charge", charge),
    )  # fmt: skip


# --- issuing -----------------------------------------------------------------------------------------------------


def _next_number(org, fiscal_year, kind) -> int:
    seq, _created = InvoiceSequence.objects.get_or_create(organization=org, fiscal_year=fiscal_year, kind=kind)
    seq = InvoiceSequence.objects.select_for_update().get(pk=seq.pk)
    seq.last_number += 1
    seq.save(update_fields=["last_number", "updated_at"])
    return seq.last_number


def coverage_of(patient) -> PatientCoverage | None:
    found = PatientCoverage.objects.filter(patient=patient, is_active=True).select_related("payer").first()
    return found if found and found.payer.is_active and found.percent > 0 else None


def _issue(org, patient, specs, *, by, appointment=None, payer=None, percent=None, reason="", date=None):
    """Write the invoice, its lines and shares, and post it. `specs` already carry their discounts."""
    specs = [s for s in specs if s.gross > 0 or s.shares]
    if not specs:
        return None
    if payer is None and percent is None:
        coverage = coverage_of(patient)
        if coverage is not None:
            payer, percent = coverage.payer, coverage.percent
    percent = Decimal(str(percent or 0)) if payer is not None else Decimal(0)
    if not 0 <= percent <= 100:
        raise InvoiceError(_("نسبة تحمل جهة التعاقد لازم تكون بين 0 و100."))
    if payer is not None and (payer.organization_id != org.pk or not payer.is_active):
        raise InvoiceError(_("جهة التعاقد دي مش متاحة."))
    gross = sum((s.gross for s in specs), ZERO)
    discount = sum((s.discount for s in specs), ZERO)
    total = gross - discount
    payer_share = percent_of(total, percent) if payer is not None else ZERO
    if payer_share == 0:
        payer, percent = None, Decimal(0)
    payer_parts = allocate(payer_share, [s.net for s in specs]) if payer_share else [ZERO] * len(specs)

    date = date or ledger.today()
    fiscal_year = ledger.fiscal_year_for(org, date)
    invoice = Invoice.objects.create(
        organization=org, kind=InvoiceKind.INVOICE, fiscal_year=fiscal_year,
        number=_next_number(org, fiscal_year, InvoiceKind.INVOICE), date=date, patient=patient,
        appointment=appointment, payer=payer, coverage_percent=percent, gross=gross, discount=discount, total=total,
        payer_share=payer_share, patient_share=total - payer_share, reason=(reason or "").strip()[:200], issued_by=by,
    )  # fmt: skip
    for i, (spec, payer_part) in enumerate(zip(specs, payer_parts, strict=True), start=1):
        name, obj = spec.source
        line = InvoiceLine.objects.create(
            organization=org, invoice=invoice, order=i, kind=spec.kind, description=spec.description[:200],
            quantity=spec.quantity, unit_price=spec.unit_price, gross=spec.gross, discount=spec.discount,
            net=spec.net, payer_amount=payer_part, patient_amount=spec.net - payer_part,
            revenue_account=chart.account(org, spec.revenue_key), cost_center=chart.cost_center(org, spec.center_key),
            doctor=spec.doctor, **({name: obj} if name else {}),
        )  # fmt: skip
        InvoiceShare.objects.bulk_create(
            InvoiceShare(
                organization=org, line=line, doctor=share.doctor, payee_name=(share.payee_name or "")[:150],
                role=(share.role or "")[:60], percent=share.percent, amount=share.amount,
            )
            for share in spec.shares
            if share.amount > 0
        )  # fmt: skip
    try:
        posting.post_invoice(invoice, by=by)
    except ledger.LedgerError as e:
        raise InvoiceError(str(e)) from e
    return invoice


@transaction.atomic
def _credit(invoice, lines, *, reason, by, date=None):
    """A credit note for whole lines of one invoice. The lines stop being live, so what they billed can be billed
    again (corrected) or not at all."""
    org = invoice.organization
    lines = [line for line in lines if line.is_live and line.reverses_id is None]
    if not lines:
        return None
    date = date or ledger.today()
    fiscal_year = ledger.fiscal_year_for(org, date)
    amounts = {
        key: sum((getattr(line, key) for line in lines), ZERO)
        for key in ("gross", "discount", "net", "payer_amount", "patient_amount")
    }
    note = Invoice.objects.create(
        organization=org, kind=InvoiceKind.CREDIT, fiscal_year=fiscal_year,
        number=_next_number(org, fiscal_year, InvoiceKind.CREDIT), date=date, patient=invoice.patient,
        appointment=invoice.appointment, payer=invoice.payer if amounts["payer_amount"] else None,
        coverage_percent=invoice.coverage_percent, credit_of=invoice, gross=amounts["gross"],
        discount=amounts["discount"], total=amounts["net"], payer_share=amounts["payer_amount"],
        patient_share=amounts["patient_amount"], reason=(reason or "").strip()[:200], issued_by=by,
    )  # fmt: skip
    for i, line in enumerate(lines, start=1):
        mirror = InvoiceLine.objects.create(
            organization=org, invoice=note, order=i, kind=line.kind, description=line.description,
            quantity=line.quantity, unit_price=line.unit_price, gross=line.gross, discount=line.discount,
            net=line.net, payer_amount=line.payer_amount, patient_amount=line.patient_amount,
            revenue_account=line.revenue_account, cost_center=line.cost_center, doctor=line.doctor,
            appointment=line.appointment, service_order=line.service_order, operation=line.operation,
            admission=line.admission, charge=line.charge, reverses=line, is_live=False,
        )  # fmt: skip
        InvoiceShare.objects.bulk_create(
            InvoiceShare(
                organization=org, line=mirror, doctor=s.doctor, payee_name=s.payee_name, role=s.role,
                percent=s.percent, amount=s.amount,
            )
            for s in line.shares.all()
        )  # fmt: skip
    InvoiceLine.objects.filter(pk__in=[line.pk for line in lines]).update(is_live=False)
    try:
        posting.post_invoice(note, by=by)
    except ledger.LedgerError as e:
        raise InvoiceError(str(e)) from e
    return note


# --- visits: the program keeps their invoices right ------------------------------------------------------------------


@transaction.atomic
def sync_appointment(appt, *, by=None, date=None) -> list[Invoice]:
    """Make this visit's invoices say what the visit is worth now. Returns the documents it had to make (none when
    everything was already right)."""
    appt = (
        Appointment.objects.select_for_update(of=("self",))
        .select_related("patient", "doctor", "visit_type", "organization")
        .get(pk=appt.pk)
    )
    target = {spec.source_key: spec for spec in appointment_specs(appt)}
    live = list(
        InvoiceLine.objects.filter(invoice__appointment=appt, invoice__kind=InvoiceKind.INVOICE, is_live=True)
        .select_related("invoice", "revenue_account", "cost_center")
        .prefetch_related("shares")
    )
    stale, kept = [], set()
    for line in live:
        spec = target.get(line.source_key)
        if spec is None or (line.gross, line.discount) != (spec.gross, spec.discount):
            stale.append(line)
        else:
            kept.add(line.source_key)
    made = []
    by_invoice = {}
    for line in stale:
        by_invoice.setdefault(line.invoice_id, (line.invoice, []))[1].append(line)
    for invoice, lines in by_invoice.values():
        made.append(_credit(invoice, lines, reason=_("تعديل على الزيارة"), by=by, date=date))
    fresh = [spec for key, spec in target.items() if key not in kept]
    if fresh:
        made.append(_issue(appt.organization, appt.patient, fresh, by=by, appointment=appt, date=date))
    return [m for m in made if m is not None]


def sync_quietly(appt, *, by=None) -> None:
    """`sync_appointment` for the places where the visit itself must go on whatever happens to its invoice (finishing
    a visit, taking a payment). A refusal is logged and the visit stays un-invoiced until the next sync picks it up
    (the hourly job, the next change, `verify_books`). Under tests the error is raised, so it never hides a bug."""
    try:
        with transaction.atomic():
            sync_appointment(appt, by=by)
    except Exception:
        if getattr(settings, "INVOICING_STRICT", False):
            raise
        log.exception("invoice sync failed for appointment %s", appt.pk)


def sync_recent(days=3) -> int:
    """The safety net behind `sync_quietly` (scheduler, hourly): every visit of the last days is put right."""
    from datetime import timedelta

    since = ledger.today() - timedelta(days=days)
    done = 0
    for appt in Appointment.objects.filter(date__gte=since, status__in=IN_THE_CENTER).iterator():
        try:
            with transaction.atomic():
                done += len(sync_appointment(appt))
        except Exception:
            log.exception("invoice sync failed for appointment %s", appt.pk)
    return done


def check_discount(org, membership, *, gross, discount) -> None:
    """Who may give this discount: "invoice.discount" up to the clinic's limit, "invoice.discount_any" above it."""
    if discount <= 0:
        return
    if membership is None or not has_perm(membership, "invoice.discount"):
        raise InvoiceError(_("مش مسموح لك تعمل خصم."))
    limit = InvoiceSettings.for_org(org).discount_limit_percent
    if gross > 0 and discount * 100 > limit * gross and not has_perm(membership, "invoice.discount_any"):
        raise InvoiceError(
            _("الخصم ده أكبر من الحد المسموح (%(l)s%%). لازم حد معاه صلاحية الخصم المفتوح.") % {"l": f"{limit:g}"}
        )


@transaction.atomic
def set_visit_discount(appt, *, amount, reason, by, membership) -> Appointment:
    """A discount on the visit fee. If the visit was already invoiced, its invoice is corrected (credit note + a new
    invoice) by the sync."""
    appt = Appointment.objects.select_for_update(of=("self",)).get(pk=appt.pk)
    if appt.status in (AppointmentStatus.CANCELLED, AppointmentStatus.RESCHEDULED, AppointmentStatus.NO_SHOW):
        raise InvoiceError(_("الحجز ده مش نشط."))
    try:
        amount = money(Decimal(str(amount or 0)))
    except (ArithmeticError, ValueError, TypeError) as e:
        raise InvoiceError(_("اكتب مبلغ الخصم بالأرقام.")) from e
    if amount < 0 or amount > appt.price:
        raise InvoiceError(_("الخصم لازم يكون بين صفر وسعر الكشف."))
    reason = (reason or "").strip()
    if amount > 0 and not reason:
        raise InvoiceError(_("اكتب سبب الخصم."))
    if amount > appt.discount:
        check_discount(appt.organization, membership, gross=appt.price, discount=amount)
    appt.discount, appt.discount_reason = amount, reason[:200] if amount else ""
    appt.save(update_fields=["discount", "discount_reason", "updated_at"])
    sync_appointment(appt, by=by)
    return appt


# --- the patient's account: operations, stays, extra charges ----------------------------------------------------------


@dataclass
class OpenItem:
    key: str  # "operation:12": the checkbox value
    spec: Spec
    ready: bool
    waiting: str = ""  # why it cannot be invoiced yet
    obj: object = None

    @property
    def amount(self) -> Decimal:
        return self.spec.gross


def open_items(patient) -> list[OpenItem]:
    """What waits on the patient's account for an invoice."""
    items = []
    operations = (
        Operation.objects.filter(patient=patient)
        .exclude(status__in=OPERATION_OFF)
        .exclude(invoice_lines__is_live=True)
        .select_related("surgeon", "procedure")
        .prefetch_related("team__doctor")
    )
    for op in operations:
        spec = operation_spec(op)
        if spec.gross <= 0 and not spec.shares:
            continue
        ready = op.status in OPERATION_BILLABLE
        items.append(OpenItem(f"operation:{op.pk}", spec, ready, "" if ready else _("لسه العملية ما خلصتش"), op))
    admissions = (
        Admission.objects.filter(patient=patient)
        .exclude(status=AdmissionStatus.CANCELLED)
        .exclude(invoice_lines__is_live=True)
        .select_related("room", "doctor")
    )
    for adm in admissions:
        spec = admission_spec(adm)
        if spec.gross <= 0:
            continue
        ready = adm.status == AdmissionStatus.DISCHARGED
        items.append(OpenItem(f"admission:{adm.pk}", spec, ready, "" if ready else _("لسه مقيمة"), adm))
    charges = (
        Charge.objects.filter(patient=patient, is_cancelled=False)
        .exclude(invoice_lines__is_live=True)
        .select_related("doctor")
    )
    for charge in charges:
        items.append(OpenItem(f"charge:{charge.pk}", charge_spec(charge), True, "", charge))
    return items


@transaction.atomic
def issue_account_invoice(
    patient, *, keys, by, membership=None, discount=0, reason="", payer=None, percent=None
) -> Invoice:
    """Invoice the chosen items of the patient's account (`keys` as `open_items` names them). The discount is spread
    over the lines in proportion to their price; the patient's deposit is applied to her part."""
    from apps.patients.models import Patient

    patient = Patient.objects.select_for_update(of=("self",)).get(pk=patient.pk)  # one invoice at a time per account
    org = patient.organization
    wanted = set(keys or [])
    chosen = [item for item in open_items(patient) if item.key in wanted]
    if not chosen or len(chosen) != len(wanted):
        raise InvoiceError(_("اختار البنود اللي هتتفوتر. (فيه بند اتفوتر أو اتلغى، حدّث الصفحة.)"))
    not_ready = [item for item in chosen if not item.ready]
    if not_ready:
        raise InvoiceError(_("مينفعش يتفوتر دلوقتي: %(l)s.") % {"l": "، ".join(i.spec.description for i in not_ready)})
    specs = [item.spec for item in chosen]
    gross = sum((s.gross for s in specs), ZERO)
    try:
        discount = money(Decimal(str(discount or 0)))
    except (ArithmeticError, ValueError, TypeError) as e:
        raise InvoiceError(_("اكتب مبلغ الخصم بالأرقام.")) from e
    if discount < 0 or discount > gross:
        raise InvoiceError(_("الخصم لازم يكون بين صفر وإجمالي الفاتورة."))
    if discount > 0:
        if not (reason or "").strip():
            raise InvoiceError(_("اكتب سبب الخصم."))
        check_discount(org, membership, gross=gross, discount=discount)
        for spec, part in zip(specs, allocate(discount, [s.gross for s in specs]), strict=True):
            spec.discount = part
    invoice = _issue(org, patient, specs, by=by, payer=payer, percent=percent, reason=reason if discount else "")
    if invoice is None:
        raise InvoiceError(_("مفيش حاجة تتفوتر."))
    available = deposit_balance(patient)
    applied = min(available, invoice.patient_share)
    if applied > 0:
        try:
            posting.post_deposit_applied(invoice, applied, by=by)
        except ledger.LedgerError as e:
            raise InvoiceError(str(e)) from e
        invoice.deposit_applied = applied
        invoice.save(update_fields=["deposit_applied", "updated_at"])
    return invoice


@transaction.atomic
def credit_invoice(invoice, *, line_ids=None, reason, by) -> Invoice:
    """إشعار دائن: take back an account invoice, or some of its lines. What they billed goes back to the account."""
    invoice = (
        Invoice.objects.select_for_update(of=("self",)).select_related("organization", "patient").get(pk=invoice.pk)
    )
    if invoice.is_credit:
        raise InvoiceError(_("ده إشعار دائن، مش فاتورة."))
    if invoice.appointment_id:
        raise InvoiceError(_("فاتورة الزيارة بتتصحح من الزيارة نفسها: خصم، إلغاء خدمة، أو رجوع خطوة."))
    if not (reason or "").strip():
        raise InvoiceError(_("اكتب سبب الإشعار."))
    lines = (
        invoice.lines.filter(is_live=True).select_related("revenue_account", "cost_center").prefetch_related("shares")
    )
    if line_ids:
        lines = lines.filter(pk__in=line_ids)
    note = _credit(invoice, list(lines), reason=reason, by=by)
    if note is None:
        raise InvoiceError(_("مفيش بنود تترجع في الفاتورة دي."))
    return note


@transaction.atomic
def add_charge(
    patient, *, description, unit_price, by, quantity=1, kind=ChargeKind.SERVICE, doctor=None, share_percent=0,
    operation=None, admission=None,
) -> Charge:  # fmt: skip
    description = (description or "").strip()
    if not description:
        raise InvoiceError(_("اكتب اسم البند."))
    try:
        unit_price = money(Decimal(str(unit_price)))
        quantity = int(quantity or 1)
        share_percent = Decimal(str(share_percent or 0))
    except (ArithmeticError, ValueError, TypeError) as e:
        raise InvoiceError(_("اكتب السعر والعدد بالأرقام.")) from e
    if unit_price <= 0 or not 1 <= quantity <= 999:
        raise InvoiceError(_("السعر لازم يكون أكبر من صفر، والعدد من 1 لـ999."))
    if not 0 <= share_percent <= 100 or (share_percent and doctor is None):
        raise InvoiceError(_("نسبة الدكتور لازم تكون بين 0 و100، ومع دكتور."))
    if kind not in ChargeKind.values:
        raise InvoiceError(_("نوع البند مش معروف."))
    for obj in (doctor, operation, admission):
        if obj is not None and obj.organization_id != patient.organization_id:
            raise InvoiceError(_("فيه حاجة في البند مش بتاعة العيادة دي."))
    for obj in (operation, admission):
        if obj is not None and obj.patient_id != patient.pk:
            raise InvoiceError(_("العملية أو الإقامة دي مش بتاعة المريضة دي."))
    charge = Charge(
        organization=patient.organization, patient=patient, kind=kind, description=description[:150],
        quantity=quantity, unit_price=unit_price, doctor=doctor, doctor_share_percent=share_percent,
        operation=operation, admission=admission, created_by=by,
    )  # fmt: skip
    charge._history_user = by
    charge.save()
    return charge


@transaction.atomic
def cancel_charge(charge, *, by) -> Charge:
    charge = Charge.objects.select_for_update(of=("self",)).get(pk=charge.pk)
    if charge.invoice_lines.filter(is_live=True).exists():
        raise InvoiceError(_("البند ده اتفوتر. اعمل إشعار دائن للفاتورة الأول."))
    charge.is_cancelled = True
    charge._history_user = by
    charge.save(update_fields=["is_cancelled", "updated_at"])
    return charge


# --- balances ----------------------------------------------------------------------------------------------------


def patient_balance(patient) -> Decimal:
    """What the patient owes the center by the books (negative: the center owes her)."""
    return ledger.balance_of(chart.account(patient.organization, "patients_receivable"), party=patient)


def deposit_balance(patient) -> Decimal:
    """The deposit the center holds for her."""
    return ledger.balance_of(chart.account(patient.organization, "patient_deposits"), party=patient)


def doctor_balance(doctor) -> Decimal:
    """What the center owes the doctor (shares earned less what was paid out)."""
    return ledger.balance_of(chart.account(doctor.organization, "doctor_payables"), party=doctor)


def payer_balance(payer) -> Decimal:
    return ledger.balance_of(chart.account(payer.organization, "payers_receivable"), party=payer)


def party_balances(org, key: str, kind: str) -> dict[int, Decimal]:
    """Every party's balance in one control account, in one query: {party id: balance signed the account's way}."""
    from apps.ledger.models import EntryStatus, JournalLine

    account = chart.account(org, key)
    rows = (
        JournalLine.objects.filter(account=account, entry__status=EntryStatus.POSTED, party_type=kind)
        .values("party_id")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )
    sign = 1 if account.debit_normal else -1
    return {r["party_id"]: sign * ((r["d"] or ZERO) - (r["c"] or ZERO)) for r in rows}


@dataclass
class InvoiceState:
    invoice: Invoice
    credited: Decimal  # the patient's part taken back by credit notes
    paid: Decimal

    @property
    def due(self) -> Decimal:
        return self.invoice.patient_share - self.credited

    @property
    def remaining(self) -> Decimal:
        return self.due - self.paid


def account_state(patient) -> tuple[list[InvoiceState], Decimal]:
    """Which of the patient's invoices her money has paid. A visit's payments pay that visit's invoices first, an
    applied deposit pays its own invoice, and everything else pays the oldest unpaid invoice first. Returns the
    invoices (oldest first) and the money left over that no invoice has taken yet."""
    from apps.billing.models import Payment, PaymentKind

    invoices = list(
        Invoice.objects.filter(patient=patient, kind=InvoiceKind.INVOICE)
        .select_related("fiscal_year", "appointment")
        .order_by("date", "id")
    )
    credited = dict(
        Invoice.objects.filter(patient=patient, kind=InvoiceKind.CREDIT)
        .values_list("credit_of_id")
        .annotate(s=Sum("patient_share"))
    )
    states = [InvoiceState(inv, credited.get(inv.pk) or ZERO, ZERO) for inv in invoices]
    pool = ZERO
    by_visit = {}
    for p in Payment.objects.filter(patient=patient, kind=PaymentKind.PAYMENT):
        if p.appointment_id:
            by_visit[p.appointment_id] = by_visit.get(p.appointment_id, ZERO) + p.signed_amount
        else:
            pool += p.signed_amount
    for state in states:
        take = min(state.invoice.deposit_applied, state.remaining)
        state.paid += max(take, ZERO)
    for state in states:
        visit = state.invoice.appointment_id
        if visit and by_visit.get(visit, ZERO) > 0:
            take = min(by_visit[visit], max(state.remaining, ZERO))
            state.paid += take
            by_visit[visit] -= take
    pool += sum(by_visit.values(), ZERO)
    for state in states:
        if pool <= 0:
            break
        take = min(pool, max(state.remaining, ZERO))
        state.paid += take
        pool -= take
    return states, pool


# --- doctors and payers ------------------------------------------------------------------------------------------


def pay_doctor(doctor, *, amount, treasury_obj, by, date=None, note=""):
    """Pay a doctor what the center owes them: a payment voucher against their account in "أتعاب أطباء مستحقة"."""
    from apps.treasury import services as treasury
    from apps.treasury.models import VoucherKind

    amount = money(Decimal(str(amount)))
    with transaction.atomic():
        owed = doctor_balance(doctor)
        if amount > owed:
            raise InvoiceError(_("المبلغ أكبر من مستحقات الدكتور (%(o)s).") % {"o": owed})
        try:
            return treasury.create_voucher(
                doctor.organization, kind=VoucherKind.PAYMENT, treasury=treasury_obj, amount=amount,
                date=date or ledger.today(), by=by, party=doctor, party_name=str(doctor),
                counter_account=chart.account(doctor.organization, "doctor_payables"),
                description=(note or "").strip() or _("صرف مستحقات %(d)s") % {"d": doctor},
            )  # fmt: skip
        except treasury.TreasuryError as e:
            raise InvoiceError(str(e)) from e


def receive_from_payer(payer, *, amount, treasury_obj, by, date=None, note=""):
    """Money from a contracted company or insurer: a receipt voucher against its account."""
    from apps.treasury import services as treasury
    from apps.treasury.models import VoucherKind

    amount = money(Decimal(str(amount)))
    with transaction.atomic():
        owed = payer_balance(payer)
        if amount > owed:
            raise InvoiceError(_("المبلغ أكبر من المستحق على الجهة (%(o)s).") % {"o": owed})
        try:
            return treasury.create_voucher(
                payer.organization, kind=VoucherKind.RECEIPT, treasury=treasury_obj, amount=amount,
                date=date or ledger.today(), by=by, party=payer, party_name=payer.name,
                counter_account=chart.account(payer.organization, "payers_receivable"),
                description=(note or "").strip() or _("تحصيل من %(p)s") % {"p": payer.name},
            )  # fmt: skip
        except treasury.TreasuryError as e:
            raise InvoiceError(str(e)) from e


@transaction.atomic
def set_coverage(patient, *, payer, percent, member_no="", by=None) -> PatientCoverage | None:
    """Put the patient under a payer (`payer=None` takes her off it). Invoices already issued stay as they are."""
    current = PatientCoverage.objects.filter(patient=patient).first()
    if payer is None:
        if current is not None and current.is_active:
            current.is_active = False
            current._history_user = by
            current.save(update_fields=["is_active", "updated_at"])
        return None
    if payer.organization_id != patient.organization_id or not payer.is_active:
        raise InvoiceError(_("جهة التعاقد دي مش متاحة."))
    try:
        percent = Decimal(str(percent))
    except (ArithmeticError, ValueError, TypeError) as e:
        raise InvoiceError(_("اكتب النسبة بالأرقام.")) from e
    if not 0 < percent <= 100:
        raise InvoiceError(_("نسبة التحمل لازم تكون أكبر من صفر ولحد 100."))
    coverage = current or PatientCoverage(organization=patient.organization, patient=patient)
    coverage.payer, coverage.percent, coverage.is_active = payer, percent, True
    coverage.member_no = (member_no or "").strip()[:60]
    coverage._history_user = by
    coverage.save()
    return coverage


def save_payer(org, *, name, by, payer=None, kind="company", default_percent=100, phone="", contact_name="",
               notes="", is_active=True) -> Payer:  # fmt: skip
    name = (name or "").strip()
    if not name:
        raise InvoiceError(_("اكتب اسم الجهة."))
    clash = Payer.objects.filter(organization=org, name__iexact=name)
    if payer is not None:
        clash = clash.exclude(pk=payer.pk)
    if clash.exists():
        raise InvoiceError(_("فيه جهة بنفس الاسم."))
    payer = payer or Payer(organization=org)
    payer.name, payer.kind, payer.default_percent = name[:150], kind, default_percent
    payer.phone, payer.contact_name, payer.notes, payer.is_active = phone, contact_name, notes, is_active
    payer._history_user = by
    payer.full_clean(exclude=["organization"])
    payer.save()
    return payer
