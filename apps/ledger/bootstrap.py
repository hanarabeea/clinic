"""Bring one clinic's books in line with what already happened in it (plan 14 §14.5 "Existing data").

Run by `manage.py ledger_bootstrap` (run.bat and the deploy steps call it after `migrate`). Everything here is
idempotent: the chart and the treasuries are only completed, and a payment that already has its entry is skipped.
"""

from django.db import transaction


def bootstrap(org) -> int:
    """Returns how many payments were posted now."""
    from apps.billing.models import Payment
    from apps.billing.posting import post_payment
    from apps.treasury import services as treasury

    from . import chart, services
    from .models import JournalEntry

    with transaction.atomic():
        chart.ensure(org)
        treasury.ensure(org)
    done = set(
        JournalEntry.objects.filter(organization=org, source_type="billing.payment", source_event="").values_list(
            "source_id", flat=True
        )
    )
    posted = 0
    # oldest first, so the entry numbers follow the order the money came in
    pending = Payment.objects.filter(organization=org).exclude(pk__in=done).order_by("received_at", "id")
    for payment in pending.select_related("patient", "treasury__account", "received_by"):
        with transaction.atomic():
            if payment.treasury_id is None:
                payment.treasury = treasury.for_method(org, payment.method)
                payment.save(update_fields=["treasury", "updated_at"])
            try:
                post_payment(payment)
            except services.PeriodClosed:
                continue  # its month was closed without it: left for the books check to report
            posted += 1
    return posted


def invoice_visits(org) -> int:
    """Visits that happened before invoicing existed get their invoice, dated the day of the visit (today when that
    month is closed). Idempotent: a visit already invoiced is left alone. Returns how many documents were made."""
    from apps.invoicing import services as invoicing
    from apps.scheduling.models import Appointment

    made = 0
    visits = Appointment.objects.filter(organization=org, status__in=invoicing.IN_THE_CENTER).order_by("date", "id")
    for appt in visits.iterator():
        for day in (appt.date, None):
            try:
                with transaction.atomic():
                    made += len(invoicing.sync_appointment(appt, date=day))
                break
            except invoicing.InvoiceError:
                continue  # the visit's month is closed: dated today instead
    return made
