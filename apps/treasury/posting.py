"""How treasury documents become journal entries. One function per document; each names the document as the entry's
source, so asking twice posts once.

    receipt voucher     Dr treasury            Cr counter account
    payment voucher     Dr counter account     Cr treasury
    transfer            Dr receiving treasury  Cr sending treasury
    cash short (عجز)    Dr cash over/short     Cr treasury
    cash over (زيادة)   Dr treasury            Cr cash over/short
"""

from django.utils.translation import gettext as _

from apps.ledger import services as ledger
from apps.ledger.services import credit, debit

from .models import VoucherKind


def voucher_lines(voucher) -> list:
    amount, drawer = voucher.amount, voucher.treasury.account
    if voucher.kind == VoucherKind.TRANSFER:
        return [debit(voucher.to_treasury.account, amount), credit(drawer, amount)]
    other = {"cost_center": voucher.cost_center} if voucher.cost_center_id else {}
    if voucher.party_type and voucher.party_id:
        other["party"] = (voucher.party_type, voucher.party_id)
    if voucher.kind == VoucherKind.RECEIPT:
        return [debit(drawer, amount), credit(voucher.counter_account, amount, **other)]
    return [debit(voucher.counter_account, amount, **other), credit(drawer, amount)]


def post_voucher(voucher, *, by=None):
    memo = f"{voucher.get_kind_display()} {voucher.number}: {voucher.description}"
    return ledger.post(
        voucher.organization, date=voucher.date, memo=memo, lines=voucher_lines(voucher), source=voucher, by=by
    )


def reverse_voucher(voucher, *, by=None):
    entry = ledger.entry_for(voucher)
    if entry is None:
        raise ledger.LedgerError(_("السند ده ملوش قيد."))
    memo = _("إلغاء %(k)s رقم %(n)s") % {"k": voucher.get_kind_display(), "n": voucher.number}
    return ledger.reverse(entry, by=by, memo=memo, source=voucher, event="cancel")


def post_variance(count, *, by=None):
    """An approved difference of a shift-close count leaves the drawer's account."""
    difference = count.difference
    if difference == 0:
        raise ledger.LedgerError(_("مفيش فرق في الجرد ده."))
    drawer = count.treasury.account
    if difference < 0:
        lines = [debit("cash_over_short", -difference, cost_center="admin"), credit(drawer, -difference)]
        memo = _("عجز خزينة: %(t)s") % {"t": count.treasury.name}
    else:
        lines = [debit(drawer, difference), credit("cash_over_short", difference, cost_center="admin")]
        memo = _("زيادة خزينة: %(t)s") % {"t": count.treasury.name}
    return ledger.post(count.organization, date=ledger.today(), memo=memo, lines=lines, source=count, by=by)
