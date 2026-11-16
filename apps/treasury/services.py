"""Treasury logic: where money lands, vouchers, cashier shifts and cash counts. Every movement of money posts through
`ledger.services.post()`; a treasury's balance is its account's posted balance, never a stored number."""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.billing.models import PaymentMethod
from apps.ledger import chart
from apps.ledger import services as ledger
from apps.ledger.money import ZERO, money
from apps.organizations.models import Role

from .models import (
    CashCount,
    CountKind,
    Shift,
    ShiftStatus,
    Treasury,
    TreasuryKind,
    VarianceStatus,
    Voucher,
    VoucherKind,
    VoucherSequence,
    VoucherStatus,
)

# (system key, name, kind, ledger account key, payment method it receives)
DEFAULTS = [
    ("reception_drawer", "درج الاستقبال", TreasuryKind.DRAWER, "cash_reception", PaymentMethod.CASH),
    ("main_safe", "الخزينة الرئيسية", TreasuryKind.SAFE, "cash_main", ""),
    ("bank_main", "البنك: الحساب الجاري", TreasuryKind.BANK, "bank_main", ""),
    ("card_clearing", "مقبوضات الفيزا", TreasuryKind.ELECTRONIC, "card_clearing", PaymentMethod.CARD),
    ("instapay", "إنستاباي", TreasuryKind.ELECTRONIC, "instapay_clearing", PaymentMethod.INSTAPAY),
    ("wallet", "المحافظ الإلكترونية", TreasuryKind.ELECTRONIC, "wallet_clearing", PaymentMethod.WALLET),
]
# Egyptian notes and coins, largest first (keys of CashCount.denominations)
DENOMINATIONS = ["200", "100", "50", "20", "10", "5", "1", "0.5", "0.25"]


class TreasuryError(Exception):
    """The movement was refused. The message is written for the person at the screen."""


# --- the clinic's treasuries -----------------------------------------------------------------------------------


@transaction.atomic
def ensure(org) -> None:
    """The starting treasuries (a drawer, the safe, a bank account and the electronic collection accounts), each on
    its account of the chart. Safe to call any time: existing ones are never touched."""
    chart.ensure(org)
    have = set(Treasury.objects.filter(organization=org).values_list("system_key", flat=True))
    taken = set(
        Treasury.objects.filter(organization=org).exclude(receives_method="").values_list("receives_method", flat=True)
    )
    for key, name, kind, account_key, method in DEFAULTS:
        if key in have:
            continue
        Treasury.objects.create(
            organization=org,
            name=name,
            kind=kind,
            account=chart.account(org, account_key),
            receives_method="" if method in taken else method,
            system_key=key,
        )


def treasury(org, key: str) -> Treasury:
    found = Treasury.objects.filter(organization=org, system_key=key).select_related("account").first()
    if found is None:
        ensure(org)
        found = Treasury.objects.select_related("account").get(organization=org, system_key=key)
    return found


def for_method(org, method: str) -> Treasury:
    """Where a payment made this way lands (cash → the reception drawer, card → card receipts...)."""
    qs = Treasury.objects.filter(organization=org, receives_method=method, is_active=True).select_related("account")
    found = qs.first()
    if found is None:
        ensure(org)
        found = qs.first()
    if found is None:
        raise TreasuryError(_("مفيش خزينة متحددة لطريقة الدفع دي. حددها من شاشة الخزن."))
    return found


def balance(treasury_obj, as_of=None) -> Decimal:
    """What the books say: the posted balance of the treasury's account."""
    return ledger.balance_of(treasury_obj.account, as_of=as_of)


def pending_difference(treasury_obj) -> Decimal:
    """Counted less expected over the counts whose difference still waits for approval (negative = short)."""
    counts = CashCount.objects.filter(treasury=treasury_obj, variance_status=VarianceStatus.PENDING)
    return sum((c.counted_total - c.expected_total for c in counts), ZERO)


def cash_on_hand(treasury_obj) -> Decimal:
    """The money that is really there: the books' balance, less a shortage (or plus an overage) that was already
    counted and only waits to be approved. Once it is approved the two numbers are the same again."""
    return balance(treasury_obj) + pending_difference(treasury_obj)


def all_treasuries(org) -> list[Treasury]:
    ensure(org)
    rows = list(Treasury.objects.filter(organization=org).select_related("account").order_by("id"))
    for row in rows:
        row.balance = balance(row)
    return rows


# --- vouchers --------------------------------------------------------------------------------------------------


def _next_number(org, fiscal_year, kind) -> int:
    VoucherSequence.objects.get_or_create(organization=org, fiscal_year=fiscal_year, kind=kind)
    sequence = VoucherSequence.objects.select_for_update().get(fiscal_year=fiscal_year, kind=kind)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number", "updated_at"])
    return sequence.last_number


def _must_cover(treasury_obj, amount) -> None:
    """Cash cannot go below zero: there is no such thing as minus banknotes."""
    if treasury_obj.holds_cash and amount > cash_on_hand(treasury_obj):
        raise TreasuryError(
            _("%(t)s مفيهاش رصيد كفاية (الموجود %(b)s).") % {"t": treasury_obj.name, "b": cash_on_hand(treasury_obj)}
        )


@transaction.atomic
def create_voucher(
    org, *, kind, treasury, amount, date, description, by, counter_account=None, to_treasury=None,
    cost_center=None, party_name="", allow_negative=False, party=None, template=None,
) -> Voucher:  # fmt: skip
    """A receipt (money into `treasury`), a payment (money out of it) or a transfer (to `to_treasury`). Numbered per
    kind per fiscal year and posted in the same transaction."""
    from .posting import post_voucher

    amount = money(amount)
    if amount <= 0:
        raise TreasuryError(_("المبلغ لازم يكون أكبر من صفر."))
    if kind not in VoucherKind.values:
        raise TreasuryError(_("نوع السند مش معروف."))
    if treasury.organization_id != org.pk or not treasury.is_active:
        raise TreasuryError(_("الخزينة دي مش متاحة."))
    if not (description or "").strip():
        raise TreasuryError(_("اكتب البيان."))
    if kind == VoucherKind.TRANSFER:
        if to_treasury is None or to_treasury.pk == treasury.pk:
            raise TreasuryError(_("اختار الخزينة اللي الفلوس رايحة لها."))
        if to_treasury.organization_id != org.pk or not to_treasury.is_active:
            raise TreasuryError(_("الخزينة دي مش متاحة."))
        counter_account = cost_center = None
    else:
        to_treasury = None
        if counter_account is None or counter_account.organization_id != org.pk:
            raise TreasuryError(_("اختار الحساب المقابل."))
        if counter_account.is_group or not counter_account.is_active:
            raise TreasuryError(_("الحساب المقابل لازم يكون حساب فرعي مفعّل."))
        if Treasury.objects.filter(account=counter_account).exists():
            raise TreasuryError(_("ده حساب خزينة. لنقل فلوس بين خزنتين استخدم «تحويل»."))
        if counter_account.system_key in chart.CONTROL_KEYS and party is None:
            # a control account is the sum of its people's accounts: a voucher on it must say whose
            raise TreasuryError(
                _("%(a)s بيتحرك من مستنداته (تحصيل مريض، صرف مستحقات دكتور، سداد مورد...)، مش بسند حر.")
                % {"a": counter_account}
            )
    try:
        party_type, party_id = ledger.party_key(party)
    except ledger.LedgerError as e:
        raise TreasuryError(str(e)) from e
    if kind in (VoucherKind.PAYMENT, VoucherKind.TRANSFER) and not allow_negative:
        _must_cover(treasury, amount)

    fiscal_year = ledger.fiscal_year_for(org, date)
    voucher = Voucher.objects.create(
        organization=org,
        kind=kind,
        fiscal_year=fiscal_year,
        number=_next_number(org, fiscal_year, kind),
        date=date,
        treasury=treasury,
        counter_account=counter_account,
        to_treasury=to_treasury,
        amount=amount,
        cost_center=cost_center,
        party_name=(party_name or "").strip()[:150],
        party_type=party_type if kind != VoucherKind.TRANSFER else "",
        party_id=party_id if kind != VoucherKind.TRANSFER else None,
        template=template,
        description=description.strip()[:300],
        created_by=by,
    )
    try:
        post_voucher(voucher, by=by)
    except ledger.LedgerError as e:
        raise TreasuryError(str(e)) from e
    return voucher


@transaction.atomic
def cancel_voucher(voucher, *, by, reason="") -> Voucher:
    """A wrong voucher: its entry is reversed today and the voucher is marked cancelled. Both stay on record."""
    from .posting import reverse_voucher

    voucher = (
        Voucher.objects.select_for_update(of=("self",))
        .select_related("treasury__account", "to_treasury__account")
        .get(pk=voucher.pk)
    )
    if voucher.is_cancelled:
        return voucher
    if voucher.kind == VoucherKind.RECEIPT:
        _must_cover(voucher.treasury, voucher.amount)  # the money goes back out of the treasury
    elif voucher.kind == VoucherKind.TRANSFER:
        _must_cover(voucher.to_treasury, voucher.amount)
    try:
        reverse_voucher(voucher, by=by)
    except ledger.LedgerError as e:
        raise TreasuryError(str(e)) from e
    voucher.status = VoucherStatus.CANCELLED
    voucher.cancelled_by, voucher.cancelled_at = by, timezone.now()
    voucher.cancel_reason = (reason or "").strip()[:200]
    voucher._history_user = by
    voucher.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancel_reason", "updated_at"])
    return voucher


# --- shifts ----------------------------------------------------------------------------------------------------


def current_shift(treasury_obj) -> Shift | None:
    return Shift.objects.filter(treasury=treasury_obj, status=ShiftStatus.OPEN).first()


@transaction.atomic
def ensure_shift(treasury_obj, *, by) -> Shift:
    """The open shift of a cash drawer, opened now if there is none: taking money never waits for someone to "open
    the shift" first."""
    Treasury.objects.select_for_update().get(pk=treasury_obj.pk)
    shift = current_shift(treasury_obj)
    if shift is None:
        shift = Shift.objects.create(
            organization=treasury_obj.organization,
            treasury=treasury_obj,
            opened_by=by,
            opening_balance=cash_on_hand(treasury_obj),
        )
    return shift


def count_total(denominations: dict) -> Decimal:
    total = ZERO
    for face, pieces in (denominations or {}).items():
        if face not in DENOMINATIONS:
            raise TreasuryError(_("فئة مش معروفة: %(f)s") % {"f": face})
        try:
            pieces = int(pieces or 0)
        except (TypeError, ValueError) as e:
            raise TreasuryError(_("اكتب عدد الورق أرقام.")) from e
        if pieces < 0:
            raise TreasuryError(_("العدد مينفعش يكون بالسالب."))
        total += Decimal(face) * pieces
    return money(total)


def _clean_denominations(denominations: dict) -> dict:
    return {face: int(denominations[face]) for face in DENOMINATIONS if int(denominations.get(face) or 0)}


@transaction.atomic
def close_shift(shift, *, denominations, by, keep=0, notes="") -> CashCount:
    """Close a cashier's shift: count the drawer, hand the cash over to the main safe (leaving `keep` as the float for
    the next shift), and record the difference between what was counted and what the books expect. A difference
    stays on the drawer's account until someone else approves it (`approve_variance`)."""
    shift = Shift.objects.select_for_update(of=("self",)).select_related("treasury__account").get(pk=shift.pk)
    if shift.status != ShiftStatus.OPEN:
        raise TreasuryError(_("الوردية دي مقفولة."))
    drawer, org = shift.treasury, shift.organization
    counted = count_total(denominations)
    keep = money(keep or 0)
    if keep < 0 or keep > counted:
        raise TreasuryError(_("المبلغ اللي هيفضل في الدرج لازم يكون بين صفر واللي اتعد."))
    expected = cash_on_hand(drawer)  # a difference still waiting from an earlier count is not expected again
    count = CashCount.objects.create(
        organization=org,
        treasury=drawer,
        shift=shift,
        kind=CountKind.SHIFT_CLOSE,
        denominations=_clean_denominations(denominations),
        counted_total=counted,
        expected_total=expected,
        counted_by=by,
        notes=(notes or "").strip()[:300],
        variance_status=VarianceStatus.NONE if counted == expected else VarianceStatus.PENDING,
    )
    handover = counted - keep
    safe = treasury(org, "main_safe")
    if handover > 0 and safe.pk != drawer.pk:
        create_voucher(
            org, kind=VoucherKind.TRANSFER, treasury=drawer, to_treasury=safe, amount=handover, date=ledger.today(),
            description=_("توريد وردية %(d)s") % {"d": drawer.name}, by=by,
            allow_negative=True,  # with cash over, more leaves the drawer than the books knew of, until approval
        )  # fmt: skip
    shift.status = ShiftStatus.CLOSED
    shift.closed_by, shift.closed_at, shift.handed_over = by, timezone.now(), handover
    shift._history_user = by
    shift.save(update_fields=["status", "closed_by", "closed_at", "handed_over", "updated_at"])
    return count


def can_approve(count, membership) -> bool:
    """Separation of duties: the person who counted does not approve their own difference. The owner always can
    (it is their money)."""
    if membership is None:
        return False
    return membership.role == Role.OWNER or membership.user_id != count.counted_by_id


@transaction.atomic
def approve_variance(count, *, by, membership) -> CashCount:
    """Accept a shift's cash difference: it leaves the drawer's account and goes to "عجز وزيادة الخزينة"."""
    from .posting import post_variance

    count = CashCount.objects.select_for_update(of=("self",)).select_related("treasury__account").get(pk=count.pk)
    if count.variance_status != VarianceStatus.PENDING:
        raise TreasuryError(_("مفيش فرق مستني اعتماد في الجرد ده."))
    if not can_approve(count, membership):
        raise TreasuryError(_("اللي عدّ الخزينة مينفعش يعتمد الفرق بنفسه. لازم حد تاني."))
    try:
        post_variance(count, by=by)
    except ledger.LedgerError as e:
        raise TreasuryError(str(e)) from e
    count.variance_status = VarianceStatus.APPROVED
    count.approved_by, count.approved_at = by, timezone.now()
    count._history_user = by
    count.save(update_fields=["variance_status", "approved_by", "approved_at", "updated_at"])
    return count


@transaction.atomic
def surprise_count(treasury_obj, *, denominations, by, witness_name="", notes="") -> CashCount:
    """A surprise count (جرد مفاجئ): what is in the treasury right now against the books, who counted and who
    witnessed. It records; it moves nothing. A difference found here is settled at the shift close."""
    counted = count_total(denominations)
    return CashCount.objects.create(
        organization=treasury_obj.organization,
        treasury=treasury_obj,
        kind=CountKind.SURPRISE,
        denominations=_clean_denominations(denominations),
        counted_total=counted,
        expected_total=cash_on_hand(treasury_obj),
        counted_by=by,
        witness_name=(witness_name or "").strip()[:120],
        notes=(notes or "").strip()[:300],
    )


# --- expenses that come back every month ------------------------------------------------------------------------


def expense_templates(org, today) -> list:
    """The clinic's recurring expenses with this month's state: the voucher that paid it (`paid_voucher`), or
    whether its day has passed (`overdue`)."""
    from .models import ExpenseTemplate, VoucherStatus

    rows = list(
        ExpenseTemplate.objects.filter(organization=org)
        .select_related("treasury", "counter_account", "cost_center")
        .order_by("-is_active", "day_of_month", "id")
    )
    month_start = today.replace(day=1)
    paid = {}
    made = Voucher.objects.filter(
        organization=org, template__in=rows, status=VoucherStatus.POSTED, date__gte=month_start, date__lte=today
    ).order_by("date", "id")
    for voucher in made:
        paid[voucher.template_id] = voucher
    for row in rows:
        row.paid_voucher = paid.get(row.pk)
        row.overdue = row.is_active and row.paid_voucher is None and today.day > row.day_of_month
    return rows


@transaction.atomic
def record_expense(template, *, by, amount=None, date=None) -> Voucher:
    """This month's payment voucher for a recurring expense (the amount can differ from the usual one)."""
    from .models import ExpenseTemplate, VoucherStatus

    template = ExpenseTemplate.objects.select_for_update(of=("self",)).get(pk=template.pk)
    if not template.is_active:
        raise TreasuryError(_("المصروف ده متوقف."))
    date = date or ledger.today()
    already = Voucher.objects.filter(
        template=template, status=VoucherStatus.POSTED, date__gte=date.replace(day=1), date__lte=date
    )
    if already.exists():
        raise TreasuryError(_("%(n)s اتسجل الشهر ده قبل كده.") % {"n": template.name})
    try:
        amount = money(Decimal(str(amount))) if amount not in (None, "") else template.amount
    except (ArithmeticError, ValueError, TypeError) as e:
        raise TreasuryError(_("اكتب المبلغ بالأرقام.")) from e
    return create_voucher(
        template.organization, kind=VoucherKind.PAYMENT, treasury=template.treasury, amount=amount, date=date,
        description=f"{template.name} {date:%m/%Y}", by=by, counter_account=template.counter_account,
        cost_center=template.cost_center, party_name=template.party_name, template=template,
    )  # fmt: skip
