"""A patient's WhatsApp answer to one of our messages (plan 15 §1.1).

"1" / "2" (or the same said in words) answering, within `NotificationSettings.reply_window_hours`, the last
booking confirmation, reminder or waiting-list offer sent to that number:
- to a confirmation / reminder: "1" confirms the booking (and a short "اتأكد" goes back), "2" cancels it (the
  usual cancellation message goes back, and the freed place goes to the waiting list);
- to a waiting-list offer: "1" books it, "2" passes it to the next patient.
Anything else, or a cancellation, waits in the reception inbox (`handled_at` empty). Nothing here sends directly:
the acknowledgements are outbox rows like every other message.
"""

import logging
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.arabic import normalize_arabic

from .models import (
    Event,
    InboundMessage,
    MessageChannel,
    MessageStatus,
    NotificationSettings,
    ReplyAction,
    ScheduledMessage,
)

logger = logging.getLogger(__name__)

YES, NO = "yes", "no"
# whole answers only ("1", "١", "تمام"...): a sentence that happens to contain "1" is a message for reception
_YES = {"1", "تاكيد", "اكد", "اكيد", "موافق", "موافقه", "تمام", "ايوه", "اه", "نعم", "ok", "yes"}
_NO = {"2", "الغاء", "الغي", "لا", "لأ", "مش جاي", "مش هقدر", "مش هاجي", "no"}
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
REPLY_EVENTS = (Event.BOOKING_CONFIRMED, Event.REMINDER, Event.WAITLIST_OFFER)


def intent(body: str) -> str | None:
    text = normalize_arabic((body or "").translate(_DIGITS).lower())
    text = re.sub(r"[^\w\s]", " ", text)  # punctuation and emoji
    text = re.sub(r"\s+", " ", text).strip()
    if text in _YES:
        return YES
    if text in _NO:
        return NO
    return None


def answered(msg: InboundMessage, ns, now) -> ScheduledMessage | None:
    """The message this reply answers: the last confirmation / reminder / offer sent to this patient on WhatsApp
    inside the reply window."""
    if msg.patient_id is None:
        return None
    return (
        ScheduledMessage.objects.filter(
            organization_id=msg.organization_id,
            patient_id=msg.patient_id,
            channel=MessageChannel.WHATSAPP,
            status=MessageStatus.SENT,
            event__in=REPLY_EVENTS,
            sent_at__gte=now - timedelta(hours=ns.reply_window_hours),
            sent_at__lte=now,
        )
        .select_related("appointment", "appointment__doctor")
        .order_by("-sent_at", "-id")
        .first()
    )


def handle(msg: InboundMessage, now=None) -> InboundMessage:
    """Act on a fresh inbound message when it is a 1 / 2 answer. Errors are logged, never raised: the webhook must
    always store the message, and reception still sees it."""
    try:
        with transaction.atomic():
            _handle(msg, now or timezone.now())
    except Exception:
        logger.exception("reply handling failed for inbound %s", msg.pk)
    return msg


def _handle(msg, now):
    ns = NotificationSettings.for_org(msg.organization)
    choice = intent(msg.body)
    if not ns.replies_enabled or choice is None:
        return
    source = answered(msg, ns, now)
    if source is None:
        return
    if source.event == Event.WAITLIST_OFFER:
        _answer_offer(msg, source, choice == YES, now)
    elif source.appointment is not None:
        _answer_booking(msg, source.appointment, choice, ns, now)


def _done(msg, action, note="", appointment=None, handled=True, now=None):
    msg.action, msg.action_note = action, note[:300]
    if appointment is not None:
        msg.appointment = appointment
    if handled:
        msg.handled_at = now or timezone.now()  # nothing left for reception to do
    msg.save(update_fields=["action", "action_note", "appointment", "handled_at", "updated_at"])


def _answer_booking(msg, appt, choice, ns, now):
    from apps.scheduling import services as booking
    from apps.scheduling.models import AppointmentStatus

    appt.refresh_from_db()
    if appt.status not in (AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED) or appt.start_at <= now:
        _done(msg, ReplyAction.TOO_LATE, _("الحجز بقى: %(s)s") % {"s": appt.get_status_display()}, appt,
              handled=False, now=now)  # fmt: skip
        return
    if choice == YES:
        if appt.status == AppointmentStatus.BOOKED:
            booking.transition(appt, AppointmentStatus.CONFIRMED, by=None, notes=_("المريض أكد برد على الواتساب"))
            transaction.on_commit(lambda: _acknowledge(appt, msg, ns))
        _done(msg, ReplyAction.CONFIRMED, "", appt, now=now)
    else:
        booking.cancel(appt, by=None, reason=_("المريض لغى برد على الواتساب"))
        # reception is told: the place may need a call to someone (the waiting list was offered it already)
        _done(msg, ReplyAction.CANCELLED, _("الحجز اتلغى"), appt, handled=False, now=now)


def _acknowledge(appt, msg, ns):
    from . import services

    tpl = services._first_template(appt.organization, Event.REPLY_CONFIRMED, appt.doctor.booking_mode)
    if tpl is not None and tpl.is_enabled:
        services.enqueue(appt=appt, template=tpl, event=Event.REPLY_CONFIRMED, send_at=timezone.now(), ns=ns,
                         key_extra=f"in{msg.pk}")  # fmt: skip


def _answer_offer(msg, source, accept, now):
    from apps.scheduling import waitlist
    from apps.scheduling.models import WaitlistEntry

    entry = WaitlistEntry.objects.filter(organization_id=msg.organization_id, pk=source.context.get("entry")).first()
    if entry is None:
        return
    if source.context.get("offer") != entry.offers_made:  # an answer to an older offer
        _done(msg, ReplyAction.TOO_LATE, _("رد على عرض قديم"), handled=False, now=now)
        return
    answer = waitlist.respond(entry, accept=accept, now=now)
    if answer.booked is not None:
        # a second booking still on: reception decides which one she keeps
        _done(msg, ReplyAction.WAITLIST_BOOKED, answer.note, answer.booked, handled=answer.other is None, now=now)
    elif answer.too_late:
        _done(msg, ReplyAction.TOO_LATE, answer.note, handled=False, now=now)
    else:
        _done(msg, ReplyAction.WAITLIST_DECLINED, answer.note, now=now)


# --- the reception inbox ------------------------------------------------------------------------------------------


def unhandled(org):
    return InboundMessage.objects.for_org(org).filter(handled_at__isnull=True)


def mark_handled(msg, *, by) -> InboundMessage:
    if msg.handled_at is None:
        msg.handled_at, msg.handled_by = timezone.now(), by
        msg.save(update_fields=["handled_at", "handled_by", "updated_at"])
    return msg
