"""Supplier and tax reports: who is owed what and since when, the VAT paid on purchases, and what was withheld
from suppliers (the lines of the quarterly withholding return, نموذج 41). Read-only."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from apps.ledger.money import ZERO

from .models import DocStatus, PaymentAllocation, PurchaseInvoice, PurchaseLine, PurchaseReturnLine, SupplierPayment

BUCKETS = [(0, "لسه ما استحقش"), (30, "1-30 يوم"), (60, "31-60"), (90, "61-90"), (None, "أكتر من 90")]


@dataclass
class OpenInvoice:
    invoice: PurchaseInvoice
    open: Decimal
    days_late: int


@dataclass
class AgingRow:
    supplier: object
    buckets: list = field(default_factory=lambda: [ZERO] * len(BUCKETS))
    invoices: list = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum(self.buckets, ZERO)


def open_invoices(org, as_of: date, supplier=None) -> list[OpenInvoice]:
    """Posted invoices dated up to `as_of` with something still to pay, oldest due date first (three queries)."""
    invoices = PurchaseInvoice.objects.filter(organization=org, status=DocStatus.POSTED, date__lte=as_of)
    if supplier is not None:
        invoices = invoices.filter(supplier=supplier)
    invoices = list(invoices.select_related("supplier", "fiscal_year").order_by("due_date", "id"))
    ids = [i.pk for i in invoices]
    paid = dict(
        PaymentAllocation.objects.filter(invoice_id__in=ids, payment__status=DocStatus.POSTED, payment__date__lte=as_of)
        .values_list("invoice_id")
        .annotate(s=Sum("amount"))
    )
    returned = dict(
        PurchaseInvoice.objects.filter(pk__in=ids, returns__date__lte=as_of)
        .values_list("pk")
        .annotate(s=Sum("returns__total"))
    )
    out = []
    for inv in invoices:
        left = inv.total - (paid.get(inv.pk) or ZERO) - (returned.get(inv.pk) or ZERO)
        if left > 0:
            out.append(OpenInvoice(inv, left, (as_of - inv.due_date).days))
    return out


def _bucket(days_late: int) -> int:
    if days_late <= 0:
        return 0
    for i, (limit, _label) in enumerate(BUCKETS[1:], start=1):
        if limit is None or days_late <= limit:
            return i
    return len(BUCKETS) - 1


def aging(org, as_of: date) -> list[AgingRow]:
    """أعمار ديون الموردين: what is unpaid per supplier, by how late it is."""
    rows = {}
    for item in open_invoices(org, as_of):
        row = rows.setdefault(item.invoice.supplier_id, AgingRow(item.invoice.supplier))
        row.buckets[_bucket(item.days_late)] += item.open
        row.invoices.append(item)
    return sorted(rows.values(), key=lambda r: -r.total)


@dataclass
class VatRow:
    name: str
    rate: Decimal
    net: Decimal = ZERO
    recoverable: Decimal = ZERO
    in_cost: Decimal = ZERO


def vat_paid(org, start: date, end: date) -> list[VatRow]:
    """VAT on purchases of the period by tax code, net of returns: how much can be recovered and how much became
    part of the cost."""
    rows = {}

    def add(code, rate, net, vat, recoverable, sign):
        if code is None:
            return
        row = rows.setdefault(code.pk, VatRow(code.name, rate))
        row.net += sign * net
        if recoverable:
            row.recoverable += sign * vat
        else:
            row.in_cost += sign * vat

    lines = PurchaseLine.objects.filter(
        organization=org, invoice__status=DocStatus.POSTED, invoice__date__gte=start, invoice__date__lte=end
    ).select_related("tax_code")
    for line in lines:
        add(line.tax_code, line.vat_rate, line.net, line.vat_amount, line.vat_recoverable, 1)
    back = PurchaseReturnLine.objects.filter(organization=org, ret__date__gte=start, ret__date__lte=end).select_related(
        "line__tax_code"
    )
    for r in back:
        add(r.line.tax_code, r.line.vat_rate, r.net, r.vat_amount, r.line.vat_recoverable, -1)
    return sorted(rows.values(), key=lambda r: (-r.rate, r.name))


def withheld(org, start: date, end: date):
    """Every payment of the period that withheld tax, oldest first: the lines of the withholding return."""
    return (
        SupplierPayment.objects.filter(
            organization=org, status=DocStatus.POSTED, wht_amount__gt=0, date__gte=start, date__lte=end
        )
        .select_related("supplier", "wht_code", "fiscal_year")
        .order_by("date", "id")
    )


def quarter_of(day: date) -> tuple[date, date]:
    first = date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
    last_month = first.month + 2
    nxt = date(day.year + (last_month == 12), 1 if last_month == 12 else last_month + 1, 1)
    return first, date.fromordinal(nxt.toordinal() - 1)
