"""Post-dated cheques (plan 14 §14.7). Every step that moves money posts its entry here and is logged on the cheque.

    received, in hand      Dr cheques receivable        Cr the account it settles (a payer's, or another account)
    collected              Dr the bank                  Cr cheques receivable
    bounced                Dr the account it settled    Cr cheques receivable
    endorsed to a supplier Dr suppliers payable (them)  Cr cheques receivable
    issued (not a supplier payment)   Dr the expense / account    Cr cheques payable
    issued for a supplier payment     posted by the payment itself (purchasing/posting.py)
    cleared                Dr cheques payable           Cr the bank
    cancelled / returned   the issuing entry is reversed (for a supplier payment: the payment is cancelled)

Depositing a received cheque at the bank moves no money: it only says where the cheque is.
A step is undone only by `undo`, which reverses that step's entry and puts the cheque back where it was.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext as _

from apps.ledger import chart
from apps.ledger import services as ledger
from apps.ledger.money import ZERO, money
from apps.ledger.services import credit, debit

from .models import CHEQUE_OPEN, Cheque, ChequeDirection, ChequeEvent, ChequeStatus, Treasury, TreasuryKind
from .services import TreasuryError

S = ChequeStatus
ALLOWED = {
    S.ISSUED: {S.CLEARED, S.RETURNED, S.CANCELLED},
    S.IN_HAND: {S.DEPOSITED, S.COLLECTED, S.BOUNCED, S.ENDORSED},
    S.DEPOSITED: {S.COLLECTED, S.BOUNCED},
}
UNDOABLE = (S.CLEARED, S.DEPOSITED, S.COLLECTED, S.BOUNCED, S.ENDORSED)


def _clean(org, *, number, due_date, amount, bank=None):
    number = " ".join((number or "").split())
    if not number:
        raise TreasuryError(_("اكتب رقم الشيك."))
    if due_date is None:
        raise TreasuryError(_("اكتب تاريخ استحقاق الشيك."))
    try:
        amount = money(Decimal(str(amount)))
    except (ArithmeticError, ValueError, TypeError) as e:
        raise TreasuryError(_("اكتب مبلغ الشيك بالأرقام.")) from e
    if amount <= 0:
        raise TreasuryError(_("المبلغ لازم يكون أكبر من صفر."))
    if bank is not None and (bank.organization_id != org.pk or bank.kind != TreasuryKind.BANK or not bank.is_active):
        raise TreasuryError(_("اختار حساب بنكي شغال."))
    return number[:40], amount


def _counter(cheque) -> dict:
    """The other side of a cheque's first entry, as keyword arguments for a ledger line."""
    kw = {}
    if cheque.party_type and cheque.party_id:
        kw["party"] = (cheque.party_type, cheque.party_id)
    if cheque.cost_center_id:
        kw["cost_center"] = cheque.cost_center
    return kw


def _log(cheque, old, new, *, by, entry=None, notes="", is_undo=False) -> ChequeEvent:
    return ChequeEvent.objects.create(
        organization=cheque.organization, cheque=cheque, from_status=old, to_status=new, entry=entry, by=by,
        notes=(notes or "")[:200], is_undo=is_undo,
    )  # fmt: skip


def _post(cheque, lines, memo, *, by, date=None):
    step = cheque.events.count()  # each step of a cheque posts once, and a redone step is a new one
    try:
        return ledger.post(
            cheque.organization, date=date or ledger.today(), memo=memo, lines=lines, source=cheque,
            event=f"step:{step}", by=by,
        )  # fmt: skip
    except ledger.LedgerError as e:
        raise TreasuryError(str(e)) from e


def _label(cheque) -> str:
    return _("شيك %(n)s") % {"n": cheque.number} + (f" ({cheque.party_name})" if cheque.party_name else "")


# --- received ---------------------------------------------------------------------------------------------------


@transaction.atomic
def receive_cheque(
    org, *, number, due_date, amount, by, bank_name="", payer=None, counter_account=None, cost_center=None,
    party_name="", notes="", date=None,
) -> Cheque:  # fmt: skip
    """A cheque the clinic was given. From a contracted company (`payer`) it settles what the company owes; from
    anyone else it credits `counter_account`."""
    number, amount = _clean(org, number=number, due_date=due_date, amount=amount)
    if payer is not None:
        if payer.organization_id != org.pk:
            raise TreasuryError(_("جهة التعاقد دي مش في العيادة دي."))
        counter_account = chart.account(org, "payers_receivable")
        owed = ledger.balance_of(counter_account, party=payer)
        if amount > owed:
            raise TreasuryError(_("الشيك أكبر من المستحق على الجهة (%(o)s).") % {"o": owed})
        party = ledger.party_key(payer)
        party_name = party_name or payer.name
        cost_center = None
    else:
        party = ("", None)
        if counter_account is None or counter_account.organization_id != org.pk:
            raise TreasuryError(_("اختار الحساب اللي الشيك بيسدده."))
        if counter_account.is_group or not counter_account.is_active:
            raise TreasuryError(_("الحساب لازم يكون حساب فرعي مفعّل."))
        if (
            counter_account.system_key in chart.CONTROL_KEYS
            or Treasury.objects.filter(account=counter_account).exists()
        ):
            raise TreasuryError(_("%(a)s مينفعش يتحرك بشيك حر.") % {"a": counter_account})
    cheque = Cheque.objects.create(
        organization=org, direction=ChequeDirection.RECEIVED, number=number, bank_name=(bank_name or "")[:120],
        due_date=due_date, amount=amount, party_type=party[0], party_id=party[1],
        party_name=(party_name or "")[:150], counter_account=counter_account, cost_center=cost_center,
        status=S.IN_HAND, notes=(notes or "")[:300], created_by=by,
    )  # fmt: skip
    entry = _post(
        cheque,
        [debit("cheques_receivable", amount), credit(counter_account, amount, **_counter(cheque))],
        _("استلام %(c)s") % {"c": _label(cheque)}, by=by, date=date,
    )  # fmt: skip
    _log(cheque, "", S.IN_HAND, by=by, entry=entry)
    return cheque


# --- issued (not through a supplier payment) -------------------------------------------------------------------


@transaction.atomic
def issue_cheque(
    org, *, bank, number, due_date, amount, by, counter_account=None, cost_center=None, party_name="", notes="",
    date=None, party=None, post=True,
) -> Cheque:  # fmt: skip
    """A cheque the clinic wrote. `post=False` is for a supplier payment, whose own entry credits "أوراق الدفع"."""
    number, amount = _clean(org, number=number, due_date=due_date, amount=amount, bank=bank)
    if bank is None:
        raise TreasuryError(_("اختار الحساب البنكي المسحوب عليه الشيك."))
    if Cheque.objects.filter(organization=org, direction=ChequeDirection.ISSUED, bank=bank, number=number).exclude(
        status__in=(S.CANCELLED, S.RETURNED)
    ).exists():  # fmt: skip
        raise TreasuryError(_("فيه شيك بنفس الرقم مسحوب على نفس الحساب."))
    party_type, party_id = ledger.party_key(party)
    if post:
        if counter_account is None or counter_account.organization_id != org.pk:
            raise TreasuryError(_("اختار الحساب اللي الشيك بيتصرف عليه."))
        if counter_account.is_group or not counter_account.is_active:
            raise TreasuryError(_("الحساب لازم يكون حساب فرعي مفعّل."))
        if (
            counter_account.system_key in chart.CONTROL_KEYS
            or Treasury.objects.filter(account=counter_account).exists()
        ):
            raise TreasuryError(_("%(a)s مينفعش يتحرك بشيك حر.") % {"a": counter_account})
    cheque = Cheque.objects.create(
        organization=org, direction=ChequeDirection.ISSUED, number=number, bank=bank, bank_name=bank.name,
        due_date=due_date, amount=amount, party_type=party_type, party_id=party_id,
        party_name=(party_name or "")[:150], counter_account=counter_account if post else None,
        cost_center=cost_center if post else None, status=S.ISSUED, notes=(notes or "")[:300], created_by=by,
    )  # fmt: skip
    entry = None
    if post:
        entry = _post(
            cheque,
            [debit(counter_account, amount, **_counter(cheque)), credit("cheques_payable", amount)],
            _("إصدار %(c)s") % {"c": _label(cheque)}, by=by, date=date,
        )  # fmt: skip
    _log(cheque, "", S.ISSUED, by=by, entry=entry)
    return cheque


# --- steps ------------------------------------------------------------------------------------------------------


def _locked(cheque) -> Cheque:
    return (
        Cheque.objects.select_for_update(of=("self",))
        .select_related("bank__account", "counter_account")
        .get(pk=cheque.pk)
    )


@transaction.atomic
def move(cheque, to_status, *, by, bank=None, supplier=None, notes="", date=None) -> Cheque:
    """Take a cheque one step on. `bank`: where a received cheque is deposited / collected. `supplier`: who a
    received cheque is endorsed to."""
    cheque = _locked(cheque)
    old = cheque.status
    if to_status not in ALLOWED.get(old, set()):
        raise TreasuryError(
            _("الشيك «%(a)s»، مينفعش يبقى «%(b)s».")
            % {"a": cheque.get_status_display(), "b": dict(S.choices).get(to_status, to_status)}
        )
    org, amount, entry = cheque.organization, cheque.amount, None
    if to_status in (S.CANCELLED, S.RETURNED):
        entry = _take_back(cheque, by=by, reason=notes, date=date)
    elif to_status == S.CLEARED:
        entry = _post(
            cheque, [debit("cheques_payable", amount), credit(cheque.bank.account, amount)],
            _("صرف %(c)s من البنك") % {"c": _label(cheque)}, by=by, date=date,
        )  # fmt: skip
    elif to_status in (S.DEPOSITED, S.COLLECTED):
        bank = bank or cheque.bank
        if bank is None or bank.organization_id != org.pk or bank.kind != TreasuryKind.BANK or not bank.is_active:
            raise TreasuryError(_("اختار الحساب البنكي اللي الشيك اتودع فيه."))
        cheque.bank = bank
        if to_status == S.COLLECTED:
            entry = _post(
                cheque, [debit(bank.account, amount), credit("cheques_receivable", amount)],
                _("تحصيل %(c)s") % {"c": _label(cheque)}, by=by, date=date,
            )  # fmt: skip
    elif to_status == S.BOUNCED:
        entry = _post(
            cheque,
            [debit(cheque.counter_account, amount, **_counter(cheque)), credit("cheques_receivable", amount)],
            _("ارتداد %(c)s") % {"c": _label(cheque)}, by=by, date=date,
        )  # fmt: skip
    elif to_status == S.ENDORSED:
        if supplier is None or supplier.organization_id != org.pk:
            raise TreasuryError(_("اختار المورد اللي الشيك هيتظهّر له."))
        cheque.endorsed_to_id = supplier.pk
        entry = _post(
            cheque,
            [debit("suppliers_payable", amount, party=supplier), credit("cheques_receivable", amount)],
            _("تظهير %(c)s إلى %(s)s") % {"c": _label(cheque), "s": supplier.name}, by=by, date=date,
        )  # fmt: skip
    cheque.status = to_status
    cheque._history_user = by
    cheque.save(update_fields=["status", "bank", "endorsed_to_id", "updated_at"])
    _log(cheque, old, to_status, by=by, entry=entry, notes=notes)
    return cheque


def _take_back(cheque, *, by, reason, date):
    """An issued cheque that will never be paid: whatever it settled is owed again."""
    payment = getattr(cheque, "supplier_payment", None)
    if payment is not None:
        from apps.purchasing import services as purchasing

        purchasing.cancel_payment(payment, by=by, reason=reason or _("الشيك اتلغى"), from_cheque=True)
        return None  # the payment's reversal is the entry
    first = cheque.events.filter(to_status=S.ISSUED, entry__isnull=False).order_by("id").first()
    if first is None:
        return None
    try:
        return ledger.reverse(
            first.entry, by=by, date=date, memo=_("إلغاء %(c)s") % {"c": _label(cheque)}, source=cheque,
            event=f"step:{cheque.events.count()}",
        )  # fmt: skip
    except ledger.LedgerError as e:
        raise TreasuryError(str(e)) from e


@transaction.atomic
def undo(cheque, *, by, notes="") -> Cheque:
    """Undo the cheque's last step (a clearing or a collection recorded by mistake...). Its entry is reversed."""
    cheque = _locked(cheque)
    if cheque.status not in UNDOABLE:
        raise TreasuryError(_("الخطوة دي مينفعش يترجع فيها."))
    last = cheque.events.filter(undone=False, is_undo=False).order_by("-id").first()
    if last is None or last.to_status != cheque.status:
        raise TreasuryError(_("مفيش خطوة ترجع فيها."))
    entry = None
    if last.entry_id:
        try:
            entry = ledger.reverse(
                last.entry, by=by, memo=_("رجوع عن خطوة في %(c)s") % {"c": _label(cheque)}, source=cheque,
                event=f"step:{cheque.events.count()}",
            )  # fmt: skip
        except ledger.LedgerError as e:
            raise TreasuryError(str(e)) from e
    last.undone = True
    last.save(update_fields=["undone", "updated_at"])
    old, cheque.status = cheque.status, last.from_status
    if old == S.ENDORSED:
        cheque.endorsed_to_id = None
    cheque._history_user = by
    cheque.save(update_fields=["status", "endorsed_to_id", "updated_at"])
    _log(cheque, old, cheque.status, by=by, entry=entry, notes=notes, is_undo=True)
    return cheque


# --- what is coming ---------------------------------------------------------------------------------------------


def open_total(org, direction) -> Decimal:
    """What the books should hold in أوراق الدفع / أوراق القبض: the cheques still on their way."""
    rows = Cheque.objects.filter(organization=org, direction=direction, status__in=CHEQUE_OPEN[direction])
    return rows.aggregate(s=Sum("amount"))["s"] or ZERO


def due_soon(org, days: int, today=None) -> dict:
    """Cheques falling due within `days` (and the ones already past due), and whether each bank account can pay
    the cheques drawn on it."""
    from .services import balance

    today = today or ledger.today()
    until = today + timedelta(days=days)
    issued = list(
        Cheque.objects.filter(organization=org, direction=ChequeDirection.ISSUED, status=S.ISSUED, due_date__lte=until)
        .select_related("bank__account")
        .order_by("due_date", "id")
    )
    received = list(
        Cheque.objects.filter(
            organization=org, direction=ChequeDirection.RECEIVED, status__in=(S.IN_HAND, S.DEPOSITED),
            due_date__lte=until,
        ).order_by("due_date", "id")
    )  # fmt: skip
    by_bank = {}
    for c in issued:
        row = by_bank.setdefault(c.bank_id, {"bank": c.bank, "due": ZERO})
        row["due"] += c.amount
    short = []
    for row in by_bank.values():
        row["balance"] = balance(row["bank"])
        if row["due"] > row["balance"]:
            short.append(row)
    return {"issued": issued, "received": received, "short": short, "until": until, "today": today}
