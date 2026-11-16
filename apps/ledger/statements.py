"""Financial statements (plan 14 §14.9): the income statement and the balance sheet, straight from posted lines.

Signs: every amount is shown the natural way of its section (revenue and liabilities positive when in credit,
expenses and assets positive when in debit). A group row is the sum of its accounts.

The entry that closes a fiscal year (its source is the fiscal year itself) empties revenue and expense accounts
into retained earnings. The income statement leaves those entries out, so a closed year still shows what it earned;
the balance sheet keeps them in, so the year's result is in equity where it now belongs.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum

from .models import Account, AccountType, EntryStatus, JournalLine
from .money import ZERO
from .reports import account_depths

# every entry a fiscal year posts itself: its closing entry and, if it was reopened, the reversal of that
CLOSING = {"entry__source_type": "ledger.fiscalyear"}


@dataclass
class Row:
    account: Account
    depth: int
    amount: Decimal = ZERO
    compare: Decimal = ZERO

    @property
    def is_zero(self) -> bool:
        return not self.amount and not self.compare


@dataclass
class Section:
    key: str
    title: str
    rows: list = field(default_factory=list)
    total: Decimal = ZERO
    compare: Decimal = ZERO


def _nets(org, *, closing_entries: bool, **filters) -> dict[int, Decimal]:
    """{account id: debit - credit} over posted lines matching `filters`."""
    lines = JournalLine.objects.filter(organization=org, entry__status=EntryStatus.POSTED, **filters)
    if not closing_entries:
        lines = lines.exclude(**CLOSING)
    rows = lines.values("account_id").annotate(d=Sum("debit"), c=Sum("credit"))
    return {r["account_id"]: (r["d"] or ZERO) - (r["c"] or ZERO) for r in rows}


def _section(key, title, accounts, depths, kind, nets, compare_nets, sign, max_depth) -> Section:
    """One class of accounts as rows that roll up. `sign` turns debit-minus-credit into the section's natural sign."""
    mine = [a for a in accounts if a.type == kind]
    by_pk = {a.pk: Row(a, depths[a.pk]) for a in mine}
    parent_of = {a.pk: a.parent_id for a in mine}
    section = Section(key, title)
    for a in mine:
        if a.is_group:
            continue
        amount, compare = sign * nets.get(a.pk, ZERO), sign * compare_nets.get(a.pk, ZERO)
        section.total += amount
        section.compare += compare
        pk = a.pk
        while pk in by_pk:  # the account and every group above it
            by_pk[pk].amount += amount
            by_pk[pk].compare += compare
            pk = parent_of.get(pk)
    rows = [by_pk[a.pk] for a in mine if not by_pk[a.pk].is_zero]
    # A group is worth a row only when it adds up two lines or more: a chain of groups each repeating its only
    # child's number is noise. (The section's own total already stands for the top of the tree.)
    children = {}
    for row in rows:
        children[parent_of[row.account.pk]] = children.get(parent_of[row.account.pk], 0) + 1
    rows = [r for r in rows if not r.account.is_group or (children.get(r.account.pk, 0) >= 2 and r.depth > 1)]
    kept = {r.account.pk for r in rows}
    for row in rows:  # indent by the groups that are still shown above the row
        level, pk = 1, parent_of[row.account.pk]
        while pk in parent_of:
            level += pk in kept
            pk = parent_of[pk]
        row.depth = level
    if max_depth:
        rows = [r for r in rows if r.depth <= max_depth]
    section.rows = rows
    return section


@dataclass
class IncomeStatement:
    start: date
    end: date
    revenue: Section
    expenses: Section
    compare_start: date | None = None
    compare_end: date | None = None

    @property
    def net(self) -> Decimal:
        return self.revenue.total - self.expenses.total

    @property
    def compare_net(self) -> Decimal:
        return self.revenue.compare - self.expenses.compare


def compare_range(start: date, end: date, mode: str) -> tuple[date, date] | tuple[None, None]:
    """The period to compare with: "previous" = the same number of days just before, "year" = the same dates a year
    earlier."""
    if mode == "previous":
        days = (end - start).days + 1
        return start - timedelta(days=days), start - timedelta(days=1)
    if mode == "year":

        def back(d):
            try:
                return d.replace(year=d.year - 1)
            except ValueError:  # 29 February
                return d.replace(year=d.year - 1, day=28)

        return back(start), back(end)
    return None, None


def income_statement(org, start: date, end: date, *, cost_center=None, compare="", max_depth=None) -> IncomeStatement:
    """قائمة الدخل: what was earned and what was spent between two dates, optionally for one cost center, with a
    comparison column."""
    accounts = list(Account.objects.filter(organization=org).order_by("code"))
    depths = account_depths(accounts)
    extra = {"cost_center": cost_center} if cost_center is not None else {}
    nets = _nets(org, closing_entries=False, entry__date__gte=start, entry__date__lte=end, **extra)
    c_start, c_end = compare_range(start, end, compare)
    c_nets = (
        _nets(org, closing_entries=False, entry__date__gte=c_start, entry__date__lte=c_end, **extra) if c_start else {}
    )
    revenue = _section("revenue", "الإيرادات", accounts, depths, AccountType.REVENUE, nets, c_nets, -1, max_depth)
    expenses = _section("expenses", "المصروفات", accounts, depths, AccountType.EXPENSE, nets, c_nets, 1, max_depth)
    return IncomeStatement(start, end, revenue, expenses, c_start, c_end)


def by_cost_center(org, start: date, end: date) -> list[dict]:
    """Revenue, expenses and the result of each cost center for the period (one query)."""
    from .models import CostCenter

    rows = (
        JournalLine.objects.filter(
            organization=org, entry__status=EntryStatus.POSTED, entry__date__gte=start, entry__date__lte=end,
            account__type__in=(AccountType.REVENUE, AccountType.EXPENSE),
        )
        .exclude(**CLOSING)
        .values("cost_center_id", "account__type")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )  # fmt: skip
    totals = {}
    for r in rows:
        cell = totals.setdefault(r["cost_center_id"], {"revenue": ZERO, "expenses": ZERO})
        net = (r["d"] or ZERO) - (r["c"] or ZERO)
        if r["account__type"] == AccountType.REVENUE:
            cell["revenue"] -= net
        else:
            cell["expenses"] += net
    out = []
    for center in CostCenter.objects.filter(organization=org).order_by("code"):
        cell = totals.get(center.pk)
        if cell:
            out.append({"center": center, **cell, "net": cell["revenue"] - cell["expenses"]})
    return out


@dataclass
class BalanceSheet:
    as_of: date
    assets: Section
    liabilities: Section
    equity: Section
    result: Decimal  # revenue less expenses not yet closed into retained earnings
    compare_as_of: date | None = None
    compare_result: Decimal = ZERO

    @property
    def liabilities_and_equity(self) -> Decimal:
        return self.liabilities.total + self.equity.total + self.result

    @property
    def balanced(self) -> bool:
        return self.assets.total == self.liabilities_and_equity


def balance_sheet(org, as_of: date, *, compare_as_of=None, max_depth=None) -> BalanceSheet:
    """المركز المالي: what the center owns and owes on a date. The year's result so far is shown as its own line in
    equity until the year is closed."""
    accounts = list(Account.objects.filter(organization=org).order_by("code"))
    depths = account_depths(accounts)
    nets = _nets(org, closing_entries=True, entry__date__lte=as_of)
    c_nets = _nets(org, closing_entries=True, entry__date__lte=compare_as_of) if compare_as_of else {}

    def result(source):
        return -sum((source.get(a.pk, ZERO) for a in accounts if a.is_result and not a.is_group), ZERO)

    args = (accounts, depths)
    return BalanceSheet(
        as_of=as_of,
        assets=_section("assets", "الأصول", *args, AccountType.ASSET, nets, c_nets, 1, max_depth),
        liabilities=_section("liabilities", "الالتزامات", *args, AccountType.LIABILITY, nets, c_nets, -1, max_depth),
        equity=_section("equity", "حقوق الملكية", *args, AccountType.EQUITY, nets, c_nets, -1, max_depth),
        result=result(nets),
        compare_as_of=compare_as_of,
        compare_result=result(c_nets) if compare_as_of else ZERO,
    )


def cash_movement(org, start: date, end: date) -> list[dict]:
    """حركة النقدية: for every treasury what it held at the start, what came in, what went out and what it holds at
    the end of the period."""
    from apps.treasury.models import Treasury

    places = list(Treasury.objects.filter(organization=org).select_related("account").order_by("id"))
    ids = [t.account_id for t in places]
    before = _nets(org, closing_entries=True, entry__date__lt=start, account_id__in=ids)
    rows = (
        JournalLine.objects.filter(
            organization=org, entry__status=EntryStatus.POSTED, entry__date__gte=start, entry__date__lte=end,
            account_id__in=ids,
        )
        .values("account_id")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )  # fmt: skip
    during = {r["account_id"]: (r["d"] or ZERO, r["c"] or ZERO) for r in rows}
    out = []
    for t in places:
        opening = before.get(t.account_id, ZERO)
        came, went = during.get(t.account_id, (ZERO, ZERO))
        if opening or came or went or t.is_active:
            out.append(
                {"treasury": t, "opening": opening, "came": came, "went": went, "closing": opening + came - went}
            )
    return out
