"""Management reports from invoices (plan 14 §14.9): what each doctor, service and procedure brought in, what each
operation left after its team, supplies and stay, and who owes the center since when. Read-only.

Revenue here is invoiced revenue of the period: invoice lines dated in it, less the credit-note lines dated in it
(so a line invoiced in March and taken back in April lowers April, exactly like the books)."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from apps.ledger.money import ZERO

from .models import Invoice, InvoiceKind, InvoiceLine, InvoiceShare, LineKind

BUCKETS = [(30, "لحد 30 يوم"), (60, "31-60"), (90, "61-90"), (None, "أكتر من 90")]


def _lines(org, start: date, end: date):
    return InvoiceLine.objects.filter(organization=org, invoice__date__gte=start, invoice__date__lte=end)


def _signed(rows, key_fields, amount="net") -> dict:
    """{key: invoiced less credited} for grouped invoice lines."""
    out = {}
    for row in rows.values(*key_fields, "invoice__kind").annotate(s=Sum(amount), n=Sum("quantity")):
        key = tuple(row[f] for f in key_fields)
        sign = -1 if row["invoice__kind"] == InvoiceKind.CREDIT else 1
        total, count = out.get(key, (ZERO, 0))
        out[key] = (total + sign * (row["s"] or ZERO), count + sign * (row["n"] or 0))
    return out


@dataclass
class DoctorRow:
    doctor: object
    revenue: Decimal = ZERO  # the lines the doctor did (visits, services, operations as surgeon)
    share: Decimal = ZERO  # what the doctor earned from them


def by_doctor(org, start: date, end: date) -> list[DoctorRow]:
    from apps.doctors.models import Doctor

    doctors = {d.pk: d for d in Doctor.objects.filter(organization=org)}
    rows = {}
    for (doctor_id,), (total, _n) in _signed(
        _lines(org, start, end).filter(doctor__isnull=False), ["doctor_id"]
    ).items():
        rows.setdefault(doctor_id, DoctorRow(doctors[doctor_id])).revenue = total
    shares = (
        InvoiceShare.objects.filter(
            organization=org, doctor__isnull=False, line__invoice__date__gte=start, line__invoice__date__lte=end
        )
        .values("doctor_id", "line__invoice__kind")
        .annotate(s=Sum("amount"))
    )
    for row in shares:
        sign = -1 if row["line__invoice__kind"] == InvoiceKind.CREDIT else 1
        rows.setdefault(row["doctor_id"], DoctorRow(doctors[row["doctor_id"]])).share += sign * (row["s"] or ZERO)
    return sorted((r for r in rows.values() if r.revenue or r.share), key=lambda r: -r.revenue)


@dataclass
class ItemRow:
    kind: str
    name: str
    count: int
    revenue: Decimal


def by_item(org, start: date, end: date) -> list[ItemRow]:
    """Revenue by what was sold: each visit type, service, procedure, the rooms and the extra charges."""
    labels = dict(LineKind.choices)
    out = []
    for (kind, name), (total, count) in _signed(_lines(org, start, end), ["kind", "description"]).items():
        if total or count:
            # a stay's description names its room; group all rooms under one row
            out.append(ItemRow(kind, name, count, total))
    merged = {}
    for row in out:
        key = (row.kind, "الإقامة" if row.kind == LineKind.ROOM else row.name)
        have = merged.get(key)
        if have is None:
            merged[key] = ItemRow(row.kind, key[1], row.count, row.revenue)
        else:
            have.count, have.revenue = have.count + row.count, have.revenue + row.revenue
    rows = sorted(merged.values(), key=lambda r: (list(labels).index(r.kind), -r.revenue))
    for row in rows:
        row.kind_label = str(labels[row.kind])
    return rows


@dataclass
class OperationRow:
    operation: object
    invoice: Invoice
    price: Decimal
    team: Decimal
    supplies: Decimal
    extra: Decimal  # what the patient was charged apart for the operation (supplies with a sale price...)

    @property
    def left(self) -> Decimal:
        return self.price + self.extra - self.team - self.supplies


def operations_profit(org, start: date, end: date) -> list[OperationRow]:
    """ربحية العمليات: for every operation invoiced in the period, its price (after discount), what its team was paid
    and what its supplies cost the store. What is left pays for the room, the staff and the rest."""
    from apps.inventory.models import MoveKind, StockMove

    lines = list(
        _lines(org, start, end)
        .filter(kind=LineKind.OPERATION, is_live=True, invoice__kind=InvoiceKind.INVOICE)
        .select_related("invoice__fiscal_year", "operation__patient", "operation__surgeon")
        .order_by("invoice__date", "id")
    )
    ids = [line.operation_id for line in lines]
    team = dict(InvoiceShare.objects.filter(line__in=lines).values_list("line_id").annotate(s=Sum("amount")))
    used = {}
    moves = StockMove.objects.filter(doc__operation_id__in=ids, kind__in=(MoveKind.ISSUE, MoveKind.ISSUE_RETURN))
    for row in moves.values("doc__operation_id", "kind").annotate(s=Sum("value")):
        sign = 1 if row["kind"] == MoveKind.ISSUE else -1
        used[row["doc__operation_id"]] = used.get(row["doc__operation_id"], ZERO) + sign * (row["s"] or ZERO)
    extra = dict(
        InvoiceLine.objects.filter(
            organization=org, kind=LineKind.CHARGE, is_live=True, charge__operation_id__in=ids,
            invoice__kind=InvoiceKind.INVOICE,
        )
        .values_list("charge__operation_id")
        .annotate(s=Sum("net"))
    )  # fmt: skip
    return [
        OperationRow(
            operation=line.operation, invoice=line.invoice, price=line.net, team=team.get(line.pk) or ZERO,
            supplies=used.get(line.operation_id, ZERO), extra=extra.get(line.operation_id) or ZERO,
        )
        for line in lines
    ]  # fmt: skip


@dataclass
class AgingRow:
    patient: object
    buckets: list = field(default_factory=lambda: [ZERO] * len(BUCKETS))

    @property
    def total(self) -> Decimal:
        return sum(self.buckets, ZERO)


def _bucket(days: int) -> int:
    for i, (limit, _label) in enumerate(BUCKETS):
        if limit is None or days <= limit:
            return i
    return len(BUCKETS) - 1


def receivables_aging(org, as_of: date) -> list[AgingRow]:
    """أعمار ديون المرضى: for every patient who owes, what is unpaid by the age of its invoice (her money pays her
    oldest invoices first, see services.account_state)."""
    from apps.patients.models import Patient

    from . import services

    owing = {
        pk: amount
        for pk, amount in services.party_balances(org, "patients_receivable", "patient").items()
        if amount > 0
    }
    rows = []
    for patient in Patient.objects.filter(organization=org, pk__in=owing).order_by("full_name"):
        states, _left = services.account_state(patient)
        row = AgingRow(patient)
        for state in states:
            if state.remaining > 0 and state.invoice.date <= as_of:
                row.buckets[_bucket((as_of - state.invoice.date).days)] += state.remaining
        if row.total > 0:
            rows.append(row)
    return sorted(rows, key=lambda r: -r.total)
