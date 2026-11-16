"""How an invoice becomes journal entries. One document, up to three entries (each asked for by name, so asking twice
posts once):

    the invoice ("")       Dr patients receivable (the patient)       her part
                           Dr payers receivable (the payer)           the payer's part
                           Dr discounts allowed (per cost center)     the discount
                               Cr revenue (per account + cost center)     the full price
    the shares ("shares")  Dr doctor fees expense (per cost center)
                               Cr doctor fees payable (each doctor)
                               Cr outside team fees payable
    the deposit ("deposit") Dr patient deposits (the patient)
                               Cr patients receivable (the patient)

A credit note posts the same three shapes with the sides swapped (the deposit is never un-applied: what the patient
is owed after a credit note sits on her account and goes back through a refund).
"""

from collections import defaultdict

from django.utils.translation import gettext as _

from apps.ledger import services as ledger
from apps.ledger.money import ZERO
from apps.ledger.services import Line


def _side(debit_side: bool, account, amount, **kw) -> Line:
    return ledger.debit(account, amount, **kw) if debit_side else ledger.credit(account, amount, **kw)


def invoice_lines(invoice) -> list:
    """The entry of an invoice (or, mirrored, of a credit note)."""
    dr = not invoice.is_credit  # the side the receivables are on
    revenue, discounts = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    patient_part = payer_part = ZERO
    for line in invoice.lines.all():
        revenue[(line.revenue_account_id, line.cost_center_id)] += line.gross
        discounts[line.cost_center_id] += line.discount
        patient_part += line.patient_amount
        payer_part += line.payer_amount
    out = []
    if patient_part > 0:
        out.append(_side(dr, "patients_receivable", patient_part, party=invoice.patient))
    if payer_part > 0:
        out.append(_side(dr, "payers_receivable", payer_part, party=invoice.payer))
    for center_id, amount in sorted(discounts.items()):
        if amount > 0:
            out.append(_side(dr, "discounts_allowed", amount, cost_center=_center(invoice, center_id)))
    for (account_id, center_id), amount in sorted(revenue.items()):
        if amount > 0:
            out.append(_side(not dr, _account(invoice, account_id), amount, cost_center=_center(invoice, center_id)))
    return out


def share_lines(invoice) -> list:
    dr = not invoice.is_credit
    expense, doctors, outside = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO), ZERO
    for line in invoice.lines.all():
        for share in line.shares.all():
            expense[line.cost_center_id] += share.amount
            if share.doctor_id:
                doctors[share.doctor_id] += share.amount
            else:
                outside += share.amount
    out = [
        _side(dr, "doctor_fees_expense", amount, cost_center=_center(invoice, center_id))
        for center_id, amount in sorted(expense.items())
    ]
    out += [
        _side(not dr, "doctor_payables", amount, party=("doctor", doctor_id))
        for doctor_id, amount in sorted(doctors.items())
    ]
    if outside > 0:
        out.append(_side(not dr, "external_fees_payable", outside))
    return out


def _account(invoice, pk):
    from apps.ledger.models import Account

    return Account.objects.get(organization=invoice.organization_id, pk=pk)


def _center(invoice, pk):
    from apps.ledger.models import CostCenter

    return CostCenter.objects.get(organization=invoice.organization_id, pk=pk)


def post_invoice(invoice, *, by=None):
    """Post the invoice and, when it has any, its shares. Returns the invoice's entry (None for an invoice of zero)."""
    org = invoice.organization
    lines = invoice_lines(invoice)
    entry = None
    if lines:
        memo = f"{invoice.get_kind_display()} {invoice.label}: {invoice.patient.full_name}"
        entry = ledger.post(org, date=invoice.date, memo=memo, lines=lines, source=invoice, by=by)
    shares = share_lines(invoice)
    if shares:
        memo = _("أتعاب %(i)s") % {"i": invoice.label}
        ledger.post(org, date=invoice.date, memo=memo, lines=shares, source=invoice, event="shares", by=by)
    return entry


def post_deposit_applied(invoice, amount, *, by=None):
    """Part of the patient's deposit pays this invoice: it leaves the deposits account for her receivable."""
    patient = invoice.patient
    memo = _("مقدم مخصوم من %(i)s: %(p)s") % {"i": invoice.label, "p": patient.full_name}
    return ledger.post(
        invoice.organization, date=invoice.date, memo=memo, source=invoice, event="deposit", by=by,
        lines=[
            ledger.debit("patient_deposits", amount, party=patient),
            ledger.credit("patients_receivable", amount, party=patient),
        ],
    )  # fmt: skip
