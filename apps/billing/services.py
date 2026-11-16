from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext as _

from apps.core.timeutils import CAIRO
from apps.ledger import services as ledger
from apps.ledger.money import money, percent_of
from apps.scheduling.models import Appointment
from apps.treasury import services as treasury
from apps.treasury.models import TreasuryKind

from .models import Payment, PaymentKind, PaymentMethod


class PaymentError(Exception):
    pass


@dataclass
class Balance:
    due: Decimal  # the visit (price less discount) plus the services added to it, less what a payer covers
    paid: Decimal
    services: Decimal = Decimal(0)  # the part of `due` that is services (center mode, apps/center)
    covered: Decimal = Decimal(0)  # what the patient's company / insurer pays of this visit (apps/invoicing)

    @property
    def remaining(self):
        return max(self.due - self.paid, Decimal(0))

    @property
    def status(self):
        if self.due <= 0:
            return "free"
        if self.paid >= self.due:
            return "paid"
        return "partial" if self.paid > 0 else "unpaid"


COVERAGE_UNKNOWN = object()


def balance(appt, payments=None, orders=None, coverage=COVERAGE_UNKNOWN) -> Balance:
    """`payments` / `orders` can be prefetched lists to avoid a query per row. Cancelled service orders don't count.
    `coverage` is the patient's payer coverage percent (0 = none); left out, it is looked up (one query)."""
    if payments is None:
        payments = list(appt.payments.all())
    if orders is None:
        orders = list(appt.service_orders.all())
    paid = sum((p.signed_amount for p in payments), Decimal(0))
    services = sum((o.total for o in orders if o.is_active), Decimal(0))
    total = appt.price - appt.discount + services
    if coverage is COVERAGE_UNKNOWN:
        coverage = coverage_percent(appt.patient_id)
    covered = percent_of(total, coverage) if coverage else Decimal(0)
    return Balance(due=total - covered, paid=paid, services=services, covered=covered)


def coverage_percent(patient_id) -> Decimal:
    """The share of this patient's invoices her company / insurer pays (0 when she has none)."""
    return coverage_map([patient_id]).get(patient_id, Decimal(0))


def coverage_map(patient_ids) -> dict:
    """{patient id: percent} for the patients that are covered, in one query (lists pass it to `balance`)."""
    from apps.invoicing.models import PatientCoverage

    rows = PatientCoverage.objects.filter(
        patient_id__in=list(patient_ids), is_active=True, payer__is_active=True, percent__gt=0
    ).values_list("patient_id", "percent")
    return dict(rows)


@transaction.atomic
def record_payment(appt, *, amount, method=PaymentMethod.CASH, by, note="", is_refund=False):
    """Take (or give back) money for a visit. The payment lands in the treasury of its method and posts its journal
    entry in the same transaction: a payment without its entry cannot exist."""
    from .posting import post_payment

    appt = Appointment.objects.select_for_update().get(pk=appt.pk)
    amount = money(Decimal(str(amount)))
    if amount <= 0:
        raise PaymentError(_("المبلغ لازم يكون أكبر من صفر."))
    current = balance(appt)
    if is_refund and amount > current.paid:
        raise PaymentError(_("الاسترداد أكبر من المدفوع."))
    try:
        drawer = treasury.for_method(appt.organization, method)
    except treasury.TreasuryError as e:
        raise PaymentError(str(e)) from e
    if is_refund and drawer.holds_cash and amount > treasury.cash_on_hand(drawer):
        raise PaymentError(_("مفيش كاش كفاية في %(t)s للاسترداد ده.") % {"t": drawer.name})
    payment = Payment.objects.create(
        organization=appt.organization,
        patient_id=appt.patient_id,
        appointment=appt,
        amount=amount,
        method=method,
        received_by=by,
        note=note,
        is_refund=is_refund,
        treasury=drawer,
    )
    if drawer.kind == TreasuryKind.DRAWER:
        treasury.ensure_shift(drawer, by=by)  # the cashier's shift starts with the first money of the day
    try:
        post_payment(payment, by=by)
    except ledger.LedgerError as e:
        raise PaymentError(str(e)) from e
    from apps.invoicing import services as invoicing

    invoicing.sync_quietly(appt, by=by)  # a paid visit of a patient who is here is invoiced behind the scenes
    return payment


@transaction.atomic
def record_account_payment(
    patient, *, amount, by, method=PaymentMethod.CASH, note="", is_refund=False, kind=PaymentKind.PAYMENT
):
    """Money on the patient's account, not on one visit: paying an operation / stay invoice, a deposit (مقدم) before
    an operation, or giving either back. Same treasury, shift and posting rules as a visit payment."""
    from apps.invoicing import services as invoicing
    from apps.patients.models import Patient

    from .posting import post_payment

    patient = Patient.objects.select_for_update(of=("self",)).get(pk=patient.pk)
    org = patient.organization
    try:
        amount = money(Decimal(str(amount)))
    except (ArithmeticError, ValueError, TypeError) as e:
        raise PaymentError(_("اكتب المبلغ بالأرقام.")) from e
    if amount <= 0:
        raise PaymentError(_("المبلغ لازم يكون أكبر من صفر."))
    if kind not in PaymentKind.values:
        raise PaymentError(_("نوع الدفعة مش معروف."))
    owed = invoicing.patient_balance(patient)
    if kind == PaymentKind.DEPOSIT:
        if is_refund and amount > invoicing.deposit_balance(patient):
            raise PaymentError(_("المبلغ أكبر من المقدم اللي عندنا للمريضة."))
    elif is_refund:
        if amount > -owed:
            raise PaymentError(_("المبلغ أكبر من اللي ليها عندنا."))
    elif amount > owed:
        raise PaymentError(_("المبلغ أكبر من المستحق عليها. لو ده مقدم لعملية، سجّله «مقدم»."))
    try:
        drawer = treasury.for_method(org, method)
    except treasury.TreasuryError as e:
        raise PaymentError(str(e)) from e
    if is_refund and drawer.holds_cash and amount > treasury.cash_on_hand(drawer):
        raise PaymentError(_("مفيش كاش كفاية في %(t)s للاسترداد ده.") % {"t": drawer.name})
    payment = Payment.objects.create(
        organization=org, patient=patient, kind=kind, amount=amount, method=method, received_by=by,
        note=(note or "")[:200], is_refund=is_refund, treasury=drawer,
    )  # fmt: skip
    if drawer.kind == TreasuryKind.DRAWER:
        treasury.ensure_shift(drawer, by=by)
    try:
        post_payment(payment, by=by)
    except ledger.LedgerError as e:
        raise PaymentError(str(e)) from e
    return payment


@dataclass
class Cashbox:
    day: object
    payments: list
    by_method: list  # [(label, total)]
    total: Decimal
    drawer: object = None  # the cash drawer reception takes money into
    shift: object = None  # its open shift, if any
    in_drawer: Decimal = Decimal(0)  # the cash that is in the drawer right now (treasury.cash_on_hand)


def cashbox(org, day) -> Cashbox:
    start = datetime.combine(day, time.min, tzinfo=CAIRO)
    qs = (
        Payment.objects.for_org(org)
        .filter(received_at__gte=start, received_at__lt=start + timedelta(days=1))
        .select_related("patient", "appointment__visit_type", "received_by")
    )
    payments = list(qs)
    by_method = []
    for value, label in PaymentMethod.choices:
        income = qs.filter(method=value, is_refund=False).aggregate(s=Sum("amount"))["s"] or Decimal(0)
        refunds = qs.filter(method=value, is_refund=True).aggregate(s=Sum("amount"))["s"] or Decimal(0)
        if income or refunds:
            by_method.append((label, income - refunds))
    total = sum((p.signed_amount for p in payments), Decimal(0))
    drawer = treasury.for_method(org, PaymentMethod.CASH)
    return Cashbox(
        day=day, payments=payments, by_method=by_method, total=total, drawer=drawer,
        shift=treasury.current_shift(drawer), in_drawer=treasury.cash_on_hand(drawer),
    )  # fmt: skip
