"""Closing a fiscal year (plan 14 §14.9).

A year is closed from a checklist, never blindly: its first eleven months are closed, the books verify clean, and no
cash or stock difference is waiting for approval. Closing posts one entry on the year's last day that empties every
revenue and expense account (per cost center) into retained earnings, then locks the last month and the year.
Reopening reverses that entry. The income statement leaves closing entries out (ledger/statements.py), so a closed
year still shows what it earned.
"""

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext as _

from . import services, verify
from .models import EntryStatus, FiscalPeriod, FiscalYear, JournalLine, PeriodStatus
from .money import ZERO
from .services import LedgerError, Line


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


def checklist(year) -> list[Check]:
    """What must be true before the year can be closed."""
    org = year.organization
    periods = list(year.periods.order_by("number"))
    open_before_last = [p for p in periods[:-1] if p.is_open]
    earlier = FiscalYear.objects.filter(organization=org, start_date__lt=year.start_date, status=PeriodStatus.OPEN)
    problems = verify.verify(org)
    out = [
        Check(_("السنين اللي قبلها مقفولة"), not earlier.exists(), "، ".join(y.name for y in earlier)),
        Check(
            _("كل الشهور ما عدا الأخير مقفولة"),
            not open_before_last,
            "، ".join(f"{p.start_date:%m/%Y}" for p in open_before_last),
        ),
        Check(_("فحص الدفاتر سليم"), not problems, "؛ ".join(p.message for p in problems[:3])),
    ]
    out += _pending_differences(org)
    return out


def _pending_differences(org) -> list[Check]:
    from apps.inventory.models import CountStatus, StockCount
    from apps.treasury.models import CashCount, VarianceStatus

    cash = CashCount.objects.filter(organization=org, variance_status=VarianceStatus.PENDING).count()
    counts = StockCount.objects.filter(organization=org, status__in=(CountStatus.OPEN, CountStatus.SUBMITTED)).count()
    return [
        Check(_("مفيش فروق جرد خزينة مستنية اعتماد"), cash == 0, str(cash) if cash else ""),
        Check(_("مفيش جرد مخزن مفتوح أو مستني اعتماد"), counts == 0, str(counts) if counts else ""),
    ]


def result_of(year):
    """Revenue less expenses of the year, before any closing entry."""
    from .statements import income_statement

    return income_statement(year.organization, year.start_date, year.end_date).net


@transaction.atomic
def close_year(year, *, by) -> FiscalYear:
    year = FiscalYear.objects.select_for_update().select_related("organization").get(pk=year.pk)
    if year.status == PeriodStatus.CLOSED:
        return year
    failed = [c for c in checklist(year) if not c.ok]
    if failed:
        raise LedgerError(_("السنة مش جاهزة للقفل: %(l)s.") % {"l": "، ".join(c.label for c in failed)})
    org = year.organization
    rows = (
        JournalLine.objects.filter(
            organization=org, entry__status=EntryStatus.POSTED, entry__date__gte=year.start_date,
            entry__date__lte=year.end_date, account__type__in=("revenue", "expense"),
        )
        .values("account_id", "cost_center_id")
        .annotate(d=Sum("debit"), c=Sum("credit"))
        .order_by("account_id", "cost_center_id")
    )  # fmt: skip
    from .models import Account, CostCenter

    accounts = {a.pk: a for a in Account.objects.filter(organization=org)}
    centers = {c.pk: c for c in CostCenter.objects.filter(organization=org)}
    lines, result = [], ZERO  # result = credits less debits = the profit to carry to equity
    for row in rows:
        net = (row["d"] or ZERO) - (row["c"] or ZERO)
        if net == 0:
            continue
        account, center = accounts[row["account_id"]], centers.get(row["cost_center_id"])
        # the mirror of what the account holds, on the same cost center
        lines.append(Line(account=account, debit=-net if net < 0 else ZERO, credit=net if net > 0 else ZERO,
                          cost_center=center))  # fmt: skip
        result -= net
    if result > 0:
        lines.append(services.credit("retained_earnings", result))
    elif result < 0:
        lines.append(services.debit("retained_earnings", -result))
    if lines:
        services.post(
            org, date=year.end_date, memo=_("قيد إقفال السنة المالية %(y)s") % {"y": year.name}, lines=lines,
            source=year, event=f"close:{_times_closed(year) + 1}", by=by,
        )  # fmt: skip
    last = year.periods.order_by("-number").first()
    services.close_period(last, by=by, verified=True)  # the checklist above has just verified the books
    year.status = PeriodStatus.CLOSED
    year.save(update_fields=["status", "updated_at"])
    return year


@transaction.atomic
def reopen_year(year, *, by) -> FiscalYear:
    """Undo a year close: the closing entry is reversed (dated the year's last day) and the last month reopens."""
    year = FiscalYear.objects.select_for_update().get(pk=year.pk)
    if year.status != PeriodStatus.CLOSED:
        return year
    later = FiscalYear.objects.filter(
        organization=year.organization, start_date__gt=year.start_date, status=PeriodStatus.CLOSED
    )
    if later.exists():
        raise LedgerError(_("فيه سنة بعدها مقفولة. افتحها الأول."))
    year.status = PeriodStatus.OPEN
    year.save(update_fields=["status", "updated_at"])
    last = FiscalPeriod.objects.get(fiscal_year=year, number=year.periods.count())
    services.reopen_period(last, by=by)
    entry = closing_entry(year)
    if entry is not None:
        services.reverse(
            entry, by=by, date=year.end_date, memo=_("فتح السنة المالية %(y)s") % {"y": year.name}, source=year,
            event=f"reopen:{_times_closed(year)}",
        )  # fmt: skip
    return year


def _closings(year):
    from .models import JournalEntry

    kind, pk = services.source_key(year)
    return JournalEntry.objects.filter(
        organization=year.organization_id, source_type=kind, source_id=pk, source_event__startswith="close:"
    )


def _times_closed(year) -> int:
    return _closings(year).count()


def closing_entry(year):
    """The entry that closed the year, if it is closed now (a reopened year's old closing entry was reversed)."""
    return _closings(year).filter(reversal__isnull=True).order_by("-id").first()
