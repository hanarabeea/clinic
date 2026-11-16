"""Books verification (فحص سلامة الدفاتر, plan 14 §14.9): the proof, on demand and every night, that the books are
sound and that every number on a screen is the number in the ledger.

`verify(org)` runs every check and returns the problems it found (an empty list = sound). Each check is read-only and
independent; one that breaks does not stop the others. Checks:

 1. every posted entry balances and has two lines or more; the whole ledger balances;
 2. every money document has its entry, and every entry that names a document names one that exists;
 3. each control account equals its subledger: no line on it without a party, and where the documents can say the
    figure themselves (suppliers, cheques, stock, deposits) the account equals it;
 4. numbering has no gaps or duplicates, and nothing was posted into a month after it was closed;
 5. no cash treasury below zero; no stock quantity below zero; lots add up to their item's balance; each balance
    equals the replay of its moves;
 6. recent visits carry exactly the invoice lines they should.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, F, Max, Min, Sum
from django.utils import timezone

from . import chart
from .models import BooksCheck, EntryStatus, JournalEntry, JournalLine, PeriodStatus
from .money import ZERO

log = logging.getLogger(__name__)
SHOW = 5  # how many offenders a message names


@dataclass
class Problem:
    code: str
    message: str


def _names(values) -> str:
    values = [str(v) for v in values]
    more = f" (+{len(values) - SHOW})" if len(values) > SHOW else ""
    return "، ".join(values[:SHOW]) + more


def _balance(org, key) -> Decimal:
    from .services import balance_of

    return balance_of(chart.account(org, key))


# --- 1. the journal ----------------------------------------------------------------------------------------------


def check_entries(org):
    posted = JournalEntry.objects.filter(organization=org, status=EntryStatus.POSTED)
    bad = (
        posted.annotate(d=Sum("lines__debit"), c=Sum("lines__credit"), n=Count("lines"))
        .exclude(d=F("c"), n__gte=2)
        .values_list("number", flat=True)[:50]
    )
    if bad:
        yield Problem("entry.unbalanced", f"قيود مش متزنة أو بسطر واحد: {_names(bad)}")
    off = posted.annotate(d=Sum("lines__debit")).exclude(total=F("d")).values_list("number", flat=True)[:50]
    if off:
        yield Problem("entry.total", f"قيود إجماليها غير مجموع سطورها: {_names(off)}")
    sums = JournalLine.objects.filter(organization=org, entry__status=EntryStatus.POSTED).aggregate(
        d=Sum("debit"), c=Sum("credit")
    )
    if (sums["d"] or ZERO) != (sums["c"] or ZERO):
        yield Problem("ledger.unbalanced", f"الدفاتر مش متزنة: مدين {sums['d']} ودائن {sums['c']}")


# --- 2. documents and their entries ------------------------------------------------------------------------------


def _documents(org):
    """(label of the kind, queryset of documents that must have posted an entry, source type)."""
    from apps.billing.models import Payment
    from apps.invoicing.models import Invoice
    from apps.purchasing.models import DocStatus, PurchaseInvoice, PurchaseReturn, SupplierPayment
    from apps.treasury.models import Voucher

    yield "مدفوعات المرضى", Payment.objects.filter(organization=org), "billing.payment"
    yield "السندات", Voucher.objects.filter(organization=org), "treasury.voucher"
    yield "الفواتير", Invoice.objects.filter(organization=org, gross__gt=0), "invoicing.invoice"
    yield "فواتير المشتريات", PurchaseInvoice.objects.filter(organization=org), "purchasing.purchaseinvoice"
    yield "مرتجعات المشتريات", PurchaseReturn.objects.filter(organization=org), "purchasing.purchasereturn"
    yield "مدفوعات الموردين", SupplierPayment.objects.filter(organization=org), "purchasing.supplierpayment"
    # a cancelled document must also have posted its reversal
    yield "سندات ملغية من غير عكس", Voucher.objects.filter(organization=org, status="cancelled"), (
        "treasury.voucher", "cancel",
    )  # fmt: skip
    yield (
        "فواتير مشتريات ملغية من غير عكس",
        PurchaseInvoice.objects.filter(organization=org, status=DocStatus.CANCELLED),
        ("purchasing.purchaseinvoice", "cancel"),
    )
    yield (
        "مدفوعات موردين ملغية من غير عكس",
        SupplierPayment.objects.filter(organization=org, status=DocStatus.CANCELLED),
        ("purchasing.supplierpayment", "cancel"),
    )


def check_documents(org):
    entries = JournalEntry.objects.filter(organization=org, status=EntryStatus.POSTED)
    for label, docs, source in _documents(org):
        source_type, event = source if isinstance(source, tuple) else (source, "")
        have = set(entries.filter(source_type=source_type, source_event=event).values_list("source_id", flat=True))
        missing = [pk for pk in docs.values_list("pk", flat=True) if pk not in have]
        if missing:
            yield Problem("document.no_entry", f"{label} من غير قيد: {_names(missing)}")
    # an entry that names a document that is not there
    from django.apps import apps

    by_type = {}
    for source_type, source_id, number in entries.exclude(source_type="").values_list(
        "source_type", "source_id", "number"
    ):
        by_type.setdefault(source_type, []).append((source_id, number))
    for source_type, rows in by_type.items():
        try:
            model = apps.get_model(source_type)
        except (LookupError, ValueError):
            yield Problem(
                "entry.unknown_source", f"قيود مصدرها مش معروف ({source_type}): {_names(n for _i, n in rows)}"
            )
            continue
        there = set(model._base_manager.filter(pk__in=[i for i, _n in rows]).values_list("pk", flat=True))
        gone = [number for source_id, number in rows if source_id not in there]
        if gone:
            yield Problem("entry.no_document", f"قيود مستندها مش موجود ({source_type}): {_names(gone)}")


# --- 3. control accounts and their subledgers --------------------------------------------------------------------

PARTY_ACCOUNTS = {
    "patients_receivable": "patient",
    "payers_receivable": "payer",
    "suppliers_payable": "supplier",
    "doctor_payables": "doctor",
    "patient_deposits": "patient",
}


def check_subledgers(org):
    from apps.billing.models import Payment
    from apps.inventory import services as stock
    from apps.purchasing.models import DocStatus, PurchaseInvoice, PurchaseReturn, SupplierPayment
    from apps.treasury import cheques
    from apps.treasury.models import ChequeDirection

    posted = JournalLine.objects.filter(organization=org, entry__status=EntryStatus.POSTED)
    for key, kind in PARTY_ACCOUNTS.items():
        account = chart.account(org, key)
        loose = posted.filter(account=account).exclude(party_type=kind, party_id__isnull=False)
        loose = loose.aggregate(d=Sum("debit"), c=Sum("credit"), n=Count("id"))
        if loose["n"]:
            yield Problem(
                "control.no_party",
                f"{account.name_ar}: {loose['n']} سطر من غير طرف (مدين {loose['d']}، دائن {loose['c']})",
            )

    def expect(key, expected, what):
        actual = _balance(org, key)
        if actual != expected:
            account = chart.account(org, key)
            yield Problem("control.mismatch", f"{account.name_ar}: الدفاتر {actual} و{what} {expected}")

    # suppliers: invoices less returns less payments, plus opening balances and cheques endorsed to them
    live = PurchaseInvoice.objects.filter(organization=org, status=DocStatus.POSTED)
    billed = live.aggregate(s=Sum("total"))["s"] or ZERO
    returned = PurchaseReturn.objects.filter(organization=org).aggregate(s=Sum("total"))["s"] or ZERO
    paid = (
        SupplierPayment.objects.filter(organization=org, status=DocStatus.POSTED).aggregate(s=Sum("amount"))["s"]
        or ZERO
    )
    other = (
        posted.filter(account=chart.account(org, "suppliers_payable"))
        .exclude(entry__source_type__startswith="purchasing.purchase")
        .exclude(entry__source_type="purchasing.supplierpayment")
        .aggregate(d=Sum("debit"), c=Sum("credit"))
    )
    extra = (other["c"] or ZERO) - (other["d"] or ZERO)  # openings, endorsed cheques
    yield from expect("suppliers_payable", billed - returned - paid + extra, "مستندات الموردين")
    yield from expect("cheques_payable", cheques.open_total(org, ChequeDirection.ISSUED), "الشيكات الصادرة المفتوحة")
    yield from expect(
        "cheques_receivable", cheques.open_total(org, ChequeDirection.RECEIVED), "الشيكات الواردة المفتوحة"
    )
    yield from expect("inventory", stock.stock_value(org), "قيمة أرصدة المخزن")
    deposits = Payment.objects.filter(organization=org, kind="deposit")
    taken = sum((p.signed_amount for p in deposits), ZERO)
    from apps.invoicing.models import Invoice

    applied = Invoice.objects.filter(organization=org).aggregate(s=Sum("deposit_applied"))["s"] or ZERO
    yield from expect("patient_deposits", taken - applied, "مقدمات المرضى")


# --- 4. numbering and closed months ------------------------------------------------------------------------------


def _gaps(queryset, group_fields, label):
    rows = queryset.values(*group_fields).annotate(n=Count("id"), lo=Min("number"), hi=Max("number"))
    for row in rows:
        if row["lo"] != 1 or row["hi"] != row["n"]:
            yield Problem(
                "numbering.gap", f"ترقيم {label} فيه فجوة أو تكرار (من {row['lo']} لـ{row['hi']}، العدد {row['n']})"
            )


def check_numbering(org):
    from apps.inventory.models import StockDoc
    from apps.invoicing.models import Invoice
    from apps.purchasing.models import PurchaseInvoice, PurchaseReturn, SupplierPayment
    from apps.treasury.models import Voucher

    posted = JournalEntry.objects.filter(organization=org, status=EntryStatus.POSTED)
    yield from _gaps(posted, ["fiscal_year_id"], "القيود")
    yield from _gaps(Invoice.objects.filter(organization=org), ["fiscal_year_id", "kind"], "الفواتير")
    yield from _gaps(Voucher.objects.filter(organization=org), ["fiscal_year_id", "kind"], "السندات")
    yield from _gaps(PurchaseInvoice.objects.filter(organization=org), ["fiscal_year_id"], "فواتير المشتريات")
    yield from _gaps(PurchaseReturn.objects.filter(organization=org), ["fiscal_year_id"], "مرتجعات المشتريات")
    yield from _gaps(SupplierPayment.objects.filter(organization=org), ["fiscal_year_id"], "مدفوعات الموردين")
    yield from _gaps(StockDoc.objects.filter(organization=org), ["fiscal_year_id", "kind"], "مستندات المخزن")
    late = posted.filter(
        period__status=PeriodStatus.CLOSED, period__closed_at__isnull=False, posted_at__gt=F("period__closed_at")
    ).values_list("number", flat=True)[:50]
    if late:
        yield Problem("period.posted_after_close", f"قيود اترحّلت في شهر بعد قفله: {_names(late)}")
    outside = posted.exclude(date__gte=F("period__start_date"), date__lte=F("period__end_date")).values_list(
        "number", flat=True
    )[:50]
    if outside:
        yield Problem("period.wrong_month", f"قيود تاريخها بره شهرها: {_names(outside)}")


# --- 5. treasuries and stock -------------------------------------------------------------------------------------


def check_treasuries(org):
    from apps.treasury import services as treasury
    from apps.treasury.models import Treasury

    for t in Treasury.objects.filter(organization=org).select_related("account"):
        if t.holds_cash and treasury.balance(t) < 0:
            yield Problem("treasury.negative", f"{t.name}: رصيدها بالسالب ({treasury.balance(t)})")


def check_stock(org):
    from apps.inventory.models import StockBalance, StockBatch, StockMove

    negative = StockBalance.objects.filter(organization=org).filter(quantity__lt=0).select_related("item")
    if negative:
        yield Problem("stock.negative", f"أصناف رصيدها بالسالب: {_names(b.item.name for b in negative)}")
    lots = {
        (r["item_id"], r["warehouse_id"]): r["q"]
        for r in StockBatch.objects.filter(organization=org)
        .values("item_id", "warehouse_id")
        .annotate(q=Sum("quantity"))
    }
    moves = {}
    for m in (
        StockMove.objects.filter(organization=org)
        .order_by("id")
        .values("kind", "item_id", "warehouse_id", "quantity", "value")
    ):
        from apps.inventory.models import MOVES_IN

        sign = 1 if m["kind"] in MOVES_IN else -1
        qty, value = moves.get((m["item_id"], m["warehouse_id"]), (Decimal(0), ZERO))
        moves[(m["item_id"], m["warehouse_id"])] = (qty + sign * m["quantity"], value + sign * m["value"])
    off_lots, off_moves = [], []
    for b in StockBalance.objects.filter(organization=org).select_related("item", "warehouse"):
        key = (b.item_id, b.warehouse_id)
        if b.item.track_expiry and (lots.get(key) or Decimal(0)) != b.quantity:
            off_lots.append(f"{b.item.name} ({b.warehouse.name})")
        if moves.get(key, (Decimal(0), ZERO)) != (b.quantity, b.value):
            off_moves.append(f"{b.item.name} ({b.warehouse.name})")
    if off_lots:
        yield Problem("stock.lots", f"تشغيلات مش مساوية للرصيد: {_names(off_lots)}")
    if off_moves:
        yield Problem("stock.moves", f"رصيد مش مساوي لحركته: {_names(off_moves)}")


# --- 6. visits and their invoices --------------------------------------------------------------------------------


def check_visits(org, days=30):
    from apps.invoicing import services as invoicing
    from apps.invoicing.models import InvoiceKind, InvoiceLine
    from apps.scheduling.models import Appointment

    since = timezone.localdate() - timedelta(days=days)
    wrong = []
    visits = (
        Appointment.objects.filter(organization=org, date__gte=since, status__in=invoicing.IN_THE_CENTER)
        .select_related("patient", "doctor", "visit_type")
        .prefetch_related("payments", "service_orders__service", "service_orders__doctor")
    )
    live = {}
    lines = InvoiceLine.objects.filter(
        organization=org, invoice__appointment__in=visits, invoice__kind=InvoiceKind.INVOICE, is_live=True
    )
    for line in lines.select_related("invoice"):
        live.setdefault(line.invoice.appointment_id, {})[line.source_key] = (line.gross, line.discount)
    for appt in visits:
        target = {s.source_key: (s.gross, s.discount) for s in invoicing.appointment_specs(appt)}
        if target != live.get(appt.pk, {}):
            wrong.append(f"{appt.patient.full_name} {appt.date:%d/%m}")
    if wrong:
        yield Problem("visit.invoice", f"زيارات فاتورتها مش مطابقة للزيارة: {_names(wrong)}")


CHECKS = [
    check_entries,
    check_documents,
    check_subledgers,
    check_numbering,
    check_treasuries,
    check_stock,
    check_visits,
]


def verify(org) -> list[Problem]:
    problems = []
    for check in CHECKS:
        try:
            problems.extend(check(org))
        except Exception as e:  # a check that cannot run is itself a finding, never a silent pass
            log.exception("books check %s failed", check.__name__)
            problems.append(Problem("check.error", f"فحص {check.__name__} ما اشتغلش: {type(e).__name__}"))
    return problems


def run(org, *, by=None) -> BooksCheck:
    """Verify and keep the result (the screen shows the last one; a failed one blocks closing a month)."""
    started = timezone.now()
    problems = verify(org)
    return BooksCheck.objects.create(
        organization=org, ran_at=started, ok=not problems, problems=[asdict(p) for p in problems], ran_by=by,
        seconds=round((timezone.now() - started).total_seconds(), 2),
    )  # fmt: skip


def last(org) -> BooksCheck | None:
    return BooksCheck.objects.filter(organization=org).order_by("-ran_at", "-id").first()


def run_all(alert=True) -> int:
    """Every clinic, every night (scheduler job). Returns how many clinics have problems; owners are told."""
    from apps.organizations.models import Organization

    failing = 0
    for org in Organization.objects.all():
        result = run(org)
        if not result.ok:
            failing += 1
            if alert:
                _alert(org, result)
    return failing


def _alert(org, result) -> None:
    """Tell the owners, at most once a day per clinic (the same channel and memory as the health alerts)."""
    from django.conf import settings
    from django.core.mail import send_mail

    from apps.audit import services as audit
    from apps.audit.models import AuditEvent
    from apps.organizations.models import Membership, Role

    key = "books.verify"
    since = timezone.now() - timedelta(hours=20)
    if AuditEvent.objects.filter(organization=org, action="ops.alert", summary=key, created_at__gte=since).exists():
        return
    owners = Membership.objects.filter(organization=org, role=Role.OWNER, is_active=True).select_related("user")
    emails = [m.user.email for m in owners if m.user.email]
    lines = "\n".join(f"- {p['message']}" for p in result.problems[:20])
    if emails:
        try:
            send_mail(
                f"[DigiClinic] فحص الدفاتر لقى {len(result.problems)} مشكلة",
                f"فحص سلامة الدفاتر لقى الآتي:\n\n{lines}\n\nافتح الدفاتر، فحص الدفاتر، للتفاصيل.",
                getattr(settings, "DEFAULT_FROM_EMAIL", None), emails, fail_silently=True,
            )  # fmt: skip
        except Exception:
            log.exception("books alert e-mail failed")
    audit.log("ops.alert", org=org, summary=key)
