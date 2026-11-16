"""Messages about an operation, through the same outbox as every other message (nothing here sends).

- booked: a confirmation now, and a reminder the day before with the fasting instructions;
- time or room changed: the reminders still waiting are withdrawn, a "changed" message goes out and a new reminder
  is planned;
- cancelled or postponed: whatever is still waiting is withdrawn (the clinic phones the patient itself).

Every row's key starts with `op:<pk>:` and carries the planned time, so asking twice writes once.
"""

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.timeutils import CAIRO
from apps.notifications import services as outbox
from apps.notifications.models import Event, MessageStatus, ScheduledMessage
from apps.notifications.templating import fmt_date, fmt_time

REMIND_BEFORE = timedelta(hours=24)


def _context(op) -> dict:
    start = timezone.localtime(op.planned_start, CAIRO)
    return {
        "operation_name": op.procedure_name,
        "operation_date": fmt_date(start.date()),
        "operation_time": fmt_time(op.planned_start),
        "fasting_hours": op.fasting_hours,
        "doctor_name": str(op.surgeon),
        "room_name": op.room.name,
    }


def _stamp(op) -> str:
    return f"{op.planned_start:%Y%m%d%H%M}:{op.room_id}"


def _enqueue(op, event, send_at, by=None):
    return outbox.enqueue_for_patient(
        org=op.organization, patient=op.patient, event=event, send_at=send_at, context=_context(op),
        key=f"op:{op.pk}:{event}:{_stamp(op)}", by=by,
    )  # fmt: skip


def _remind(op, by=None) -> None:
    send_at = op.planned_start - REMIND_BEFORE
    if send_at > timezone.now():
        _enqueue(op, Event.OPERATION_REMINDER, send_at, by)


def withdraw(op, reason: str) -> int:
    """Cancel this operation's messages that are still waiting."""
    return ScheduledMessage.objects.filter(
        organization=op.organization, dedupe_key__startswith=f"op:{op.pk}:", status=MessageStatus.PENDING
    ).update(status=MessageStatus.CANCELLED, status_reason=reason[:200])


def on_booked(op, by=None) -> None:
    if op.is_emergency:
        return  # she is already here
    _enqueue(op, Event.OPERATION_BOOKED, timezone.now(), by)
    _remind(op, by)


def on_changed(op, by=None) -> None:
    withdraw(op, _("ميعاد العملية اتغير"))
    _enqueue(op, Event.OPERATION_CHANGED, timezone.now(), by)
    _remind(op, by)


def on_cancelled(op) -> None:
    withdraw(op, _("العملية اتلغت أو اتأجلت"))
