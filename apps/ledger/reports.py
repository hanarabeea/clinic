"""Reports of the accounting core: trial balance, account statement, journal. Read-only, aggregate queries, and
every number comes from posted lines only."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from .models import Account, EntryStatus, JournalEntry, JournalLine
from .money import ZERO


def _sums(org, **filters) -> dict[int, tuple[Decimal, Decimal]]:
    """{account id: (debit, credit)} over posted lines matching `filters` (one grouped query)."""
    rows = (
        JournalLine.objects.filter(organization=org, entry__status=EntryStatus.POSTED, **filters)
        .values("account_id")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )
    return {r["account_id"]: (r["d"] or ZERO, r["c"] or ZERO) for r in rows}


def _sides(net: Decimal) -> tuple[Decimal, Decimal]:
    """A net amount (debit positive) as (debit column, credit column)."""
    return (net, ZERO) if net >= 0 else (ZERO, -net)


@dataclass
class TrialRow:
    account: Account
    depth: int
    opening_debit: Decimal = ZERO
    opening_credit: Decimal = ZERO
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    closing_debit: Decimal = ZERO
    closing_credit: Decimal = ZERO

    @property
    def is_zero(self) -> bool:
        return not any(
            (self.opening_debit, self.opening_credit, self.debit, self.credit, self.closing_debit, self.closing_credit)
        )

    def add(self, other: "TrialRow") -> None:
        for name in ("opening_debit", "opening_credit", "debit", "credit", "closing_debit", "closing_credit"):
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class TrialBalance:
    start: date
    end: date
    rows: list = field(default_factory=list)
    totals: TrialRow = None

    @property
    def balanced(self) -> bool:
        t = self.totals
        return t.opening_debit == t.opening_credit and t.debit == t.credit and t.closing_debit == t.closing_credit


def account_depths(accounts) -> dict[int, int]:
    """{account id: depth in the tree} (1 = a class such as الأصول). `accounts` is the clinic's whole chart."""
    parents = {a.pk: a.parent_id for a in accounts}
    depths: dict[int, int] = {}

    def depth(pk):
        if pk not in depths:
            parent = parents.get(pk)
            depths[pk] = 1 if parent is None else depth(parent) + 1
        return depths[pk]

    for a in accounts:
        depth(a.pk)
    return depths


def trial_balance(org, start: date, end: date, *, max_depth=None, hide_zero=True) -> TrialBalance:
    """ميزان المراجعة: for every account its opening balance (before `start`), the period's movement and its closing
    balance. A group row is the sum of its accounts. The totals are sums of the leaf accounts, so the three pairs of
    columns agree exactly when the books balance."""
    accounts = list(Account.objects.filter(organization=org).order_by("code"))
    depths = account_depths(accounts)
    before = _sums(org, entry__date__lt=start)
    during = _sums(org, entry__date__gte=start, entry__date__lte=end)

    by_pk = {a.pk: TrialRow(account=a, depth=depths[a.pk]) for a in accounts}
    totals = TrialRow(account=None, depth=0)
    for a in accounts:
        if a.is_group:
            continue
        row = by_pk[a.pk]
        opening_d, opening_c = before.get(a.pk, (ZERO, ZERO))
        row.debit, row.credit = during.get(a.pk, (ZERO, ZERO))
        opening = opening_d - opening_c
        row.opening_debit, row.opening_credit = _sides(opening)
        row.closing_debit, row.closing_credit = _sides(opening + row.debit - row.credit)
        totals.add(row)
        parent = a.parent_id
        while parent is not None:  # every level above it carries the same amounts
            by_pk[parent].add(row)
            parent = by_pk[parent].account.parent_id

    rows = [by_pk[a.pk] for a in accounts]
    if max_depth:
        rows = [r for r in rows if r.depth <= max_depth]
    if hide_zero:
        rows = [r for r in rows if not r.is_zero]
    return TrialBalance(start=start, end=end, rows=rows, totals=totals)


@dataclass
class StatementLine:
    line: JournalLine
    balance: Decimal  # running, signed the account's normal way


@dataclass
class Statement:
    account: Account
    start: date
    end: date
    opening: Decimal
    lines: list
    debit: Decimal
    credit: Decimal
    closing: Decimal


def account_statement(org, account, start: date, end: date, *, party=None) -> Statement:
    """كشف حساب: the opening balance, every posted line of the period in order, and the balance after each one.
    Balances are signed the account's normal way (a debit balance of an asset is positive)."""
    from .services import party_key

    base = JournalLine.objects.filter(organization=org, account=account, entry__status=EntryStatus.POSTED)
    if party is not None:
        kind, pk = party_key(party)
        base = base.filter(party_type=kind, party_id=pk)
    sign = 1 if account.debit_normal else -1
    before = base.filter(entry__date__lt=start).aggregate(d=Sum("debit"), c=Sum("credit"))
    opening = sign * ((before["d"] or ZERO) - (before["c"] or ZERO))
    lines, running, debit, credit = [], opening, ZERO, ZERO
    period = base.filter(entry__date__gte=start, entry__date__lte=end).select_related("entry", "cost_center")
    for line in period.order_by("entry__date", "entry__number", "line_no", "id"):
        running += sign * (line.debit - line.credit)
        debit += line.debit
        credit += line.credit
        lines.append(StatementLine(line=line, balance=running))
    return Statement(
        account=account, start=start, end=end, opening=opening, lines=lines, debit=debit, credit=credit, closing=running
    )


def journal(org, start: date, end: date, *, account=None, q=""):
    """دفتر اليومية: the posted entries of the period, oldest first, with their lines."""
    entries = JournalEntry.objects.filter(organization=org, status=EntryStatus.POSTED, date__gte=start, date__lte=end)
    if account is not None:
        entries = entries.filter(lines__account=account).distinct()
    q = (q or "").strip()
    if q:
        entries = entries.filter(number=int(q)) if q.isdigit() else entries.filter(memo__icontains=q)
    return entries.select_related("created_by", "reverses").prefetch_related("lines__account", "lines__cost_center")
