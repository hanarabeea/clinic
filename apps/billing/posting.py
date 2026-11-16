"""How a patient payment becomes a journal entry.

    payment   Dr the treasury it landed in     Cr patients receivable (that patient)
    refund    Dr patients receivable           Cr the treasury
    deposit   Dr the treasury                  Cr patient deposits (that patient)      and the reverse for its refund

The credit goes to the patient's account, not to revenue: revenue is recognised by the invoice (apps/invoicing), which
debits the same patient account. Until a visit is invoiced, what the patient paid sits as a credit on their account.
A deposit (مقدم) is a liability of its own until an invoice takes it.
"""

from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.timeutils import CAIRO
from apps.ledger import services as ledger
from apps.ledger.services import credit, debit

from .models import PaymentKind


def payment_lines(payment) -> list:
    patient = payment.patient
    drawer = payment.treasury.account
    account = "patient_deposits" if payment.kind == PaymentKind.DEPOSIT else "patients_receivable"
    if payment.is_refund:
        return [debit(account, payment.amount, party=patient), credit(drawer, payment.amount)]
    return [debit(drawer, payment.amount), credit(account, payment.amount, party=patient)]


def post_payment(payment, *, by=None):
    """Idempotent: a payment posts once."""
    patient = payment.patient
    if payment.kind == PaymentKind.DEPOSIT:
        verb = _("رد مقدم لـ") if payment.is_refund else _("مقدم من ")
    else:
        verb = _("استرداد لـ") if payment.is_refund else _("تحصيل من ")
    memo = f"{verb}{patient.full_name} ({payment.get_method_display()})"
    day = timezone.localtime(payment.received_at, CAIRO).date()
    return ledger.post(
        payment.organization, date=day, memo=memo, lines=payment_lines(payment), source=payment,
        by=by or payment.received_by,
    )  # fmt: skip
