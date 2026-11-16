"""How purchasing documents become journal entries.

    purchase invoice   Dr each line's account (its net, plus its VAT when the VAT is not recoverable)
                       Dr input VAT (the recoverable VAT)
                           Cr suppliers payable (the supplier)            the invoice total
    purchase return    the mirror, for what went back. Stock leaves at the store's average today, which may not be
                       what was paid for it: the difference goes to stock over and short.
    supplier payment   Dr suppliers payable (the supplier)                what comes off their account
                           Cr the treasury (cash / transfer) or cheques payable (cheque)      what is paid
                           Cr withholding tax payable (the supplier)      what is withheld for the tax authority
    opening balance    Dr opening balances      Cr suppliers payable (the supplier)

Cancelling a document reverses its entry (event "cancel").
"""

from collections import defaultdict

from django.utils.translation import gettext as _

from apps.ledger import services as ledger
from apps.ledger.money import ZERO
from apps.ledger.services import credit, debit

from .models import PayMethod


def _cost_lines(rows, side):
    """rows: (account, cost center, amount). One ledger line per account + cost center."""
    totals, keep = defaultdict(lambda: ZERO), {}
    for account, center, amount in rows:
        key = (account.pk, center.pk if center else None)
        totals[key] += amount
        keep[key] = (account, center)
    out = []
    for key in sorted(totals, key=lambda k: (k[0], k[1] or 0)):
        if totals[key] > 0:
            account, center = keep[key]
            out.append(side(account, totals[key], **({"cost_center": center} if center else {})))
    return out


def invoice_lines(invoice) -> list:
    lines = list(invoice.lines.select_related("account", "cost_center"))
    out = _cost_lines([(line.account, line.cost_center, line.cost) for line in lines], debit)
    recoverable = sum((line.vat_amount for line in lines if line.vat_recoverable), ZERO)
    if recoverable > 0:
        out.append(debit("vat_input", recoverable))
    out.append(credit("suppliers_payable", invoice.total, party=invoice.supplier))
    return out


def post_invoice(invoice, *, by=None):
    memo = f"{invoice.label}: {invoice.supplier.name}"
    if invoice.supplier_ref:
        memo += f" ({invoice.supplier_ref})"
    return ledger.post(
        invoice.organization, date=invoice.date, memo=memo, lines=invoice_lines(invoice), source=invoice, by=by
    )


def return_lines(ret) -> list:
    lines = list(ret.lines.select_related("line__account", "line__cost_center"))
    out = [debit("suppliers_payable", ret.total, party=ret.invoice.supplier)]
    plain = [r for r in lines if r.stock_value is None]
    out += _cost_lines([(r.line.account, r.line.cost_center, r.cost) for r in plain], credit)
    left_store = sum((r.stock_value for r in lines if r.stock_value is not None), ZERO)
    credited = sum((r.cost for r in lines if r.stock_value is not None), ZERO)
    if left_store > 0:
        out.append(credit("inventory", left_store))
    if credited > left_store:  # the supplier gives back more than the goods are carried at
        out.append(credit("stock_over_short", credited - left_store, cost_center="admin"))
    elif credited < left_store:
        out.append(debit("stock_over_short", left_store - credited, cost_center="admin"))
    recoverable = sum((r.vat_amount for r in lines if r.line.vat_recoverable), ZERO)
    if recoverable > 0:
        out.append(credit("vat_input", recoverable))
    return out


def post_return(ret, *, by=None):
    memo = f"{ret.label}: {ret.invoice.supplier.name} ({ret.invoice.label})"
    return ledger.post(ret.organization, date=ret.date, memo=memo, lines=return_lines(ret), source=ret, by=by)


def payment_lines(payment) -> list:
    supplier = payment.supplier
    out = [debit("suppliers_payable", payment.amount, party=supplier)]
    if payment.net_paid > 0:
        if payment.method == PayMethod.CHEQUE:
            out.append(credit("cheques_payable", payment.net_paid))
        else:
            out.append(credit(payment.treasury.account, payment.net_paid))
    if payment.wht_amount > 0:
        out.append(credit("wht_payable", payment.wht_amount, party=supplier))
    return out


def post_payment(payment, *, by=None):
    memo = f"{payment.label}: {payment.supplier.name} ({payment.get_method_display()})"
    return ledger.post(
        payment.organization, date=payment.date, memo=memo, lines=payment_lines(payment), source=payment, by=by
    )


def reverse_document(doc, *, by=None, memo=""):
    entry = ledger.entry_for(doc)
    if entry is None:
        raise ledger.LedgerError(_("المستند ده ملوش قيد."))
    return ledger.reverse(entry, by=by, memo=memo or _("إلغاء %(d)s") % {"d": doc}, source=doc, event="cancel")


def post_opening(supplier, amount, date, *, by=None):
    """What the clinic already owed a supplier when it started using the program (negative: the supplier owes it)."""
    memo = _("رصيد افتتاحي: %(s)s") % {"s": supplier.name}
    if amount > 0:
        lines = [debit("opening_balances", amount), credit("suppliers_payable", amount, party=supplier)]
    else:
        lines = [debit("suppliers_payable", -amount, party=supplier), credit("opening_balances", -amount)]
    return ledger.post(
        supplier.organization, date=date, memo=memo, lines=lines, source=supplier, event="opening", by=by
    )
