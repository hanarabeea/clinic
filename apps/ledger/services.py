"""The only way into the journal.

`post()` creates one balanced, numbered, immutable entry. Every money-moving document of the system (a payment, a
voucher, an invoice, a supplier bill, a cheque event, a stock move, a shift close) has a small `posting.py` that turns
it into the lines and calls `post()` with itself as the `source`: one document posts once per event, however many
times it is asked.

A posted entry is never edited: `reverse()` posts its mirror image.
"""

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.timeutils import CAIRO

from . import chart
from .models import (
    Account,
    CostCenter,
    EntrySequence,
    EntryStatus,
    FiscalPeriod,
    FiscalYear,
    JournalEntry,
    JournalLine,
    LedgerSettings,
    PartyType,
    PeriodStatus,
)
from .money import ZERO, money

PARTY_TYPES = {
    "patient": PartyType.PATIENT,
    "supplier": PartyType.SUPPLIER,
    "doctor": PartyType.DOCTOR,
    "user": PartyType.EMPLOYEE,
    "payer": PartyType.PAYER,
}


class LedgerError(Exception):
    """The entry was refused. The message is written for the person at the screen."""


class PeriodClosed(LedgerError):
    pass


def today() -> date:
    return timezone.localtime(timezone.now(), CAIRO).date()


# --- fiscal years and periods ----------------------------------------------------------------------------------


def _year_bounds(day: date, start_month: int) -> tuple[date, date]:
    start_year = day.year if day.month >= start_month else day.year - 1
    start = date(start_year, start_month, 1)
    end_month = 12 if start_month == 1 else start_month - 1
    end_year = start_year if start_month == 1 else start_year + 1
    return start, date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])


@transaction.atomic
def fiscal_year_for(org, day: date) -> FiscalYear:
    """The fiscal year `day` falls in, created with its twelve monthly periods the first time it is needed."""
    year = FiscalYear.objects.filter(organization=org, start_date__lte=day, end_date__gte=day).first()
    if year is not None:
        return year
    start, end = _year_bounds(day, LedgerSettings.for_org(org).fiscal_year_start_month)
    name = str(start.year) if start.year == end.year else f"{start.year}/{end.year}"
    year, created = FiscalYear.objects.get_or_create(
        organization=org, start_date=start, defaults={"end_date": end, "name": name}
    )
    if created:
        EntrySequence.objects.create(organization=org, fiscal_year=year)
        month, y = start.month, start.year
        periods = []
        for number in range(1, 13):
            last = calendar.monthrange(y, month)[1]
            periods.append(
                FiscalPeriod(
                    organization=org,
                    fiscal_year=year,
                    number=number,
                    start_date=date(y, month, 1),
                    end_date=date(y, month, last),
                )
            )
            month, y = (1, y + 1) if month == 12 else (month + 1, y)
        FiscalPeriod.objects.bulk_create(periods)
    return year


def period_for(org, day: date) -> FiscalPeriod:
    year = fiscal_year_for(org, day)
    return FiscalPeriod.objects.get(fiscal_year=year, start_date__lte=day, end_date__gte=day)


@transaction.atomic
def close_period(period, *, by, verified=False) -> FiscalPeriod:
    """Lock a month: nothing can be posted into it any more. Months close in order, and only while the books verify
    clean (`verified=True` when the caller has just run the verification itself)."""
    period = FiscalPeriod.objects.select_for_update().get(pk=period.pk)
    if not period.is_open:
        return period
    if not verified:
        from . import verify

        problems = verify.verify(period.organization)
        if problems:
            raise LedgerError(
                _("فحص الدفاتر لقى مشاكل، والشهر مش هيتقفل قبل ما تتحل: %(m)s") % {"m": problems[0].message}
            )
    earlier_open = FiscalPeriod.objects.filter(
        organization=period.organization, start_date__lt=period.start_date, status=PeriodStatus.OPEN
    )
    if earlier_open.exists():
        raise LedgerError(_("فيه شهر قبله لسه مفتوح. اقفل الشهور بالترتيب."))
    period.status = PeriodStatus.CLOSED
    period.closed_at, period.closed_by = timezone.now(), by
    period._history_user = by
    period.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    return period


@transaction.atomic
def reopen_period(period, *, by) -> FiscalPeriod:
    """Unlock the last closed month (months reopen in reverse order; a closed year stays closed)."""
    period = FiscalPeriod.objects.select_for_update(of=("self",)).select_related("fiscal_year").get(pk=period.pk)
    if period.is_open:
        return period
    if period.fiscal_year.status == PeriodStatus.CLOSED:
        raise LedgerError(_("السنة المالية مقفولة."))
    later_closed = FiscalPeriod.objects.filter(
        organization=period.organization, start_date__gt=period.start_date, status=PeriodStatus.CLOSED
    )
    if later_closed.exists():
        raise LedgerError(_("فيه شهر بعده مقفول. افتح الشهور من الآخر للأول."))
    period.status = PeriodStatus.OPEN
    period.closed_at, period.closed_by = None, None
    period._history_user = by
    period.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    return period


# --- posting ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One side of an entry to be posted. `account` is an Account or the system key of one ("cash_main"); give
    exactly one of `debit` / `credit`. `party` is the patient, supplier, doctor or employee whose account this is."""

    account: object
    debit: object = 0
    credit: object = 0
    cost_center: object = None  # CostCenter or its system key
    party: object = None
    memo: str = ""


def debit(account, amount, **kw) -> Line:
    return Line(account=account, debit=amount, **kw)


def credit(account, amount, **kw) -> Line:
    return Line(account=account, credit=amount, **kw)


def source_key(source) -> tuple[str, int]:
    """How a document is named on its entry: ("billing.payment", 42)."""
    return source._meta.label_lower, source.pk


def party_key(party) -> tuple[str, int | None]:
    """`party` is a patient / supplier / doctor / user / payer, or an already stored (type, id) pair."""
    if party is None:
        return "", None
    if isinstance(party, tuple):
        kind, pk = party
        if kind not in PartyType.values or not pk:
            raise LedgerError(_("نوع الطرف ده مش معروف في الحسابات."))
        return kind, pk
    kind = PARTY_TYPES.get(party._meta.model_name)
    if kind is None:
        raise LedgerError(_("نوع الطرف ده مش معروف في الحسابات."))
    return kind, party.pk


def entry_for(source, event="") -> JournalEntry | None:
    """The entry a document already posted for `event`, if any."""
    kind, pk = source_key(source)
    return JournalEntry.objects.filter(
        organization=source.organization_id, source_type=kind, source_id=pk, source_event=event
    ).first()


def _resolve(org, lines):
    """Lines → (account, debit, credit, cost center, party type, party id, memo), every rule checked."""
    if len(lines) < 2:
        raise LedgerError(_("القيد لازم يكون فيه سطرين على الأقل."))
    accounts, centers, rows = {}, {}, []
    total_debit = total_credit = ZERO
    for line in lines:
        account = line.account
        if not isinstance(account, Account):
            if account not in accounts:
                accounts[account] = chart.account(org, account)
            account = accounts[account]
        if account.organization_id != org.pk:
            raise LedgerError(_("فيه حساب مش بتاع العيادة دي."))
        if account.is_group:
            raise LedgerError(_("%(a)s حساب رئيسي، الترحيل بيكون على الحسابات الفرعية بس.") % {"a": account})
        if not account.is_active:
            raise LedgerError(_("%(a)s حساب موقوف.") % {"a": account})
        d, c = money(line.debit or 0), money(line.credit or 0)
        if d < 0 or c < 0:
            raise LedgerError(_("المبلغ مينفعش يكون بالسالب."))
        if (d > 0) == (c > 0):
            raise LedgerError(_("كل سطر لازم يكون مدين أو دائن، مش الاتنين ومش صفر."))
        center = line.cost_center
        if center is not None and not isinstance(center, CostCenter):
            if center not in centers:
                centers[center] = chart.cost_center(org, center)
            center = centers[center]
        if center is not None and center.organization_id != org.pk:
            raise LedgerError(_("فيه مركز تكلفة مش بتاع العيادة دي."))
        if center is None and account.is_result:
            raise LedgerError(_("%(a)s حساب إيراد أو مصروف، لازم يتحدد له مركز تكلفة.") % {"a": account})
        party = line.party
        if party is not None and not isinstance(party, tuple) and getattr(party, "organization_id", org.pk) != org.pk:
            raise LedgerError(_("فيه طرف مش بتاع العيادة دي."))
        party_type, party_id = party_key(party)
        rows.append((account, d, c, center, party_type, party_id, (line.memo or "")[:200]))
        total_debit += d
        total_credit += c
    if total_debit != total_credit:
        raise LedgerError(_("القيد مش متزن: المدين %(d)s والدائن %(c)s.") % {"d": total_debit, "c": total_credit})
    return rows, total_debit


def _check_now() -> None:
    """Run the database's deferred balance check for this transaction right away, instead of at commit: a bad entry
    fails here, in the function that built it."""
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ledger_line_balance, ledger_entry_balance IMMEDIATE")
        cursor.execute("SET CONSTRAINTS ledger_line_balance, ledger_entry_balance DEFERRED")


@transaction.atomic
def post(org, *, date, memo, lines, source=None, event="", by=None, reverses=None) -> JournalEntry:
    """Post one entry. With a `source` document it is idempotent: asking again returns the entry that document already
    posted for that `event`. Raises LedgerError (nothing is saved) when the entry breaks a rule."""
    source_type, source_id = ("", None)
    if source is not None:
        if getattr(source, "organization_id", org.pk) != org.pk:
            raise LedgerError(_("المستند ده مش بتاع العيادة دي."))
        source_type, source_id = source_key(source)
        already = JournalEntry.objects.filter(
            organization=org, source_type=source_type, source_id=source_id, source_event=event
        ).first()
        if already is not None:
            return already
    rows, total = _resolve(org, list(lines))
    period = period_for(org, date)
    if not period.is_open:
        raise PeriodClosed(_("شهر %(m)s مقفول في الحسابات، مينفعش يتسجل فيه حاجة.") % {"m": f"{date:%m/%Y}"})

    entry = JournalEntry.objects.create(
        organization=org,
        fiscal_year_id=period.fiscal_year_id,
        period=period,
        date=date,
        memo=(memo or "")[:300],
        total=total,
        source_type=source_type,
        source_id=source_id,
        source_event=event,
        reverses=reverses,
        created_by=by,
    )
    JournalLine.objects.bulk_create(
        JournalLine(
            organization=org,
            entry=entry,
            line_no=i,
            account=account,
            debit=d,
            credit=c,
            cost_center=center,
            party_type=party_type,
            party_id=party_id,
            memo=line_memo,
        )
        for i, (account, d, c, center, party_type, party_id, line_memo) in enumerate(rows, start=1)
    )
    # the number is taken last and in this same transaction: a refused entry never burns one
    sequence = EntrySequence.objects.select_for_update().get(fiscal_year_id=period.fiscal_year_id)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number", "updated_at"])
    entry.number = sequence.last_number
    entry.status = EntryStatus.POSTED
    entry.posted_at = timezone.now()
    entry.save(update_fields=["number", "status", "posted_at", "updated_at"])
    _check_now()
    return entry


@transaction.atomic
def reverse(entry, *, by, memo="", date=None, source=None, event="") -> JournalEntry:
    """Cancel a posted entry by posting its mirror image (same accounts, sides swapped). The original stays as it
    was; the two together net to zero. An entry is reversed once."""
    entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)
    if not entry.is_posted:
        raise LedgerError(_("القيد ده مش مرحّل."))
    if JournalEntry.objects.filter(reverses=entry).exists():
        raise LedgerError(_("القيد ده اتعكس قبل كده."))
    if entry.reverses_id:
        raise LedgerError(_("ده قيد عكسي، مينفعش يتعكس. سجّل القيد الصح من جديد."))
    lines = [
        Line(
            account=line.account,
            debit=line.credit,
            credit=line.debit,
            cost_center=line.cost_center,
            party=(line.party_type, line.party_id) if line.party_type else None,  # whose account it was
            memo=line.memo,
        )
        for line in entry.lines.select_related("account", "cost_center").order_by("line_no")
    ]
    return post(
        entry.organization,
        date=date or today(),
        memo=memo or _("عكس القيد رقم %(n)s") % {"n": entry.number},
        lines=lines,
        source=source,
        event=event,
        by=by,
        reverses=entry,
    )


# --- balances --------------------------------------------------------------------------------------------------


def balance_of(account, *, as_of=None, party=None) -> Decimal:
    """Posted balance of one account, signed the account's normal way (a debit balance of an asset is positive)."""
    from django.db.models import Sum

    lines = JournalLine.objects.filter(account=account, entry__status=EntryStatus.POSTED)
    if as_of is not None:
        lines = lines.filter(entry__date__lte=as_of)
    if party is not None:
        kind, pk = party_key(party)
        lines = lines.filter(party_type=kind, party_id=pk)
    sums = lines.aggregate(d=Sum("debit"), c=Sum("credit"))
    net = (sums["d"] or ZERO) - (sums["c"] or ZERO)
    return net if account.debit_normal else -net
