"""The waiting list (plan 15 §1.3): patients who want an earlier / fuller day than the free ones.

When a booking is cancelled or moved, `on_slot_freed` offers the freed time to the first matching patient on the
list, by WhatsApp, **one person at a time**: they have `NotificationSettings.waitlist_hold_minutes` to answer "1"
(`respond(..., accept=True)` books it through `services.book`, which still refuses a slot that got taken) or "2".
A refusal or silence (`run`, every scheduler tick) passes the same slot to the next one. Each patient is offered a
given slot once (`WaitlistEntry.passed`). Patients without WhatsApp consent are never messaged: reception sees them
on the list and calls.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.timeutils import CAIRO
from apps.doctors.models import BookingMode

from .models import ACTIVE_STATUSES, Appointment, BookingSource, TimePreference, WaitlistEntry, WaitlistStatus

AFTERNOON_FROM = time(14, 0)  # "الصبح" = before 2 PM, Cairo time
MIN_LEAD = timedelta(hours=1)  # a slot freed less than an hour ahead is not worth offering


class WaitlistError(Exception):
    pass


@dataclass(frozen=True)
class Slot:
    """A freed place: an exact time (slots mode) or a period of a day (queue mode)."""

    doctor_id: int
    day: date
    start_at: datetime  # slots: the time; queue: the period's estimated time for the next number
    period_key: str = ""

    @property
    def key(self) -> str:
        if self.period_key:
            return f"{self.doctor_id}:{self.day.isoformat()}:{self.period_key}"
        return f"{self.doctor_id}:{self.start_at.astimezone(UTC).isoformat()}"  # one spelling whatever the zone


def today() -> date:
    return timezone.localtime(timezone.now(), CAIRO).date()


# --- the list --------------------------------------------------------------------------------------------------


@transaction.atomic
def add(*, patient, doctor, visit_type, earliest, latest, time_pref=TimePreference.ANY, notes="", by=None):
    if visit_type.doctor_id != doctor.pk:
        raise WaitlistError(_("نوع الزيارة ده مش للدكتور ده."))
    if earliest is None or latest is None:
        raise WaitlistError(_("اختار من يوم لحد يوم."))
    if latest < earliest:
        raise WaitlistError(_("آخر يوم قبل أول يوم."))
    if latest < today():
        raise WaitlistError(_("الأيام دي فاتت."))
    if WaitlistEntry.objects.filter(
        patient=patient, doctor=doctor, status__in=(WaitlistStatus.WAITING, WaitlistStatus.OFFERED)
    ).exists():
        raise WaitlistError(_("المريض ده في قايمة الانتظار للدكتور ده فعلًا."))
    return WaitlistEntry.objects.create(
        organization=patient.organization, patient=patient, doctor=doctor, visit_type=visit_type,
        earliest=max(earliest, today()), latest=latest, time_pref=time_pref, notes=notes.strip()[:300],
        created_by=by,
    )  # fmt: skip


@transaction.atomic
def remove(entry, *, by=None):
    entry = WaitlistEntry.objects.select_for_update().get(pk=entry.pk)
    if not entry.is_open:
        raise WaitlistError(_("الطلب ده مش في القايمة."))
    entry.status = WaitlistStatus.CANCELLED
    entry.offer_expires_at = None
    entry.save(update_fields=["status", "offer_expires_at", "updated_at"])
    if entry.offered_day:
        _withdraw_offer_messages(entry)
    return entry


def open_entries(org, doctor=None):
    qs = (
        WaitlistEntry.objects.for_org(org)
        .filter(status__in=(WaitlistStatus.WAITING, WaitlistStatus.OFFERED))
        .select_related("patient", "doctor", "visit_type")
    )
    return qs.filter(doctor=doctor) if doctor is not None else qs


def after_booking(appt) -> None:
    """Any booking that falls inside the days a patient was waiting for takes her off the list (`services.book`)."""
    WaitlistEntry.objects.filter(
        patient_id=appt.patient_id, doctor_id=appt.doctor_id, status=WaitlistStatus.WAITING,
        earliest__lte=appt.date, latest__gte=appt.date,
    ).update(status=WaitlistStatus.BOOKED, appointment=appt, updated_at=timezone.now())  # fmt: skip


# --- offering a freed place ------------------------------------------------------------------------------------


def slot_of(appt) -> Slot | None:
    """The place a cancelled / moved booking leaves, or None when it is not worth offering (past, too soon, an
    emergency or walk-in, a queue period without a limit)."""
    if appt.source in (BookingSource.EMERGENCY, BookingSource.WALK_IN):
        return None
    if appt.start_at - timezone.now() < MIN_LEAD:
        return None
    if appt.doctor.booking_mode == BookingMode.QUEUE:
        if not appt.period_key:
            return None
        return Slot(appt.doctor_id, appt.date, appt.start_at, appt.period_key)
    return Slot(appt.doctor_id, appt.date, appt.start_at)


def _matches(entry, slot: Slot) -> bool:
    if not entry.earliest <= slot.day <= entry.latest:
        return False
    local = timezone.localtime(slot.start_at, CAIRO).time()
    if entry.time_pref == TimePreference.MORNING:
        return local < AFTERNOON_FROM
    if entry.time_pref == TimePreference.EVENING:
        return local >= AFTERNOON_FROM
    return True


def _still_free(entry, slot: Slot) -> Slot | None:
    """The slot as it can be booked now for this entry's visit type (None when it is gone)."""
    from . import services

    doctor = entry.doctor
    if slot.period_key:
        for p in services.queue_availability(doctor, slot.day):
            if p.period_id == slot.period_key and (p.remaining is None or p.remaining > 0):
                return Slot(slot.doctor_id, slot.day, p.estimated_time, slot.period_key)
        return None
    return slot if slot.start_at in services.free_slots(doctor, entry.visit_type, slot.day) else None


def _reachable(patient) -> bool:
    return bool(patient.messaging_consent and (patient.whatsapp or patient.phone))


def offer_next(org, slot: Slot, *, now=None) -> WaitlistEntry | None:
    """Offer `slot` to the first matching patient who has not had it yet. Returns the entry offered, or None."""
    from apps.notifications.models import NotificationSettings

    ns = NotificationSettings.for_org(org)
    if not ns.waitlist_enabled:
        return None
    now = now or timezone.now()
    deciding = WaitlistEntry.objects.filter(
        organization=org, status=WaitlistStatus.OFFERED, doctor_id=slot.doctor_id, offered_day=slot.day
    )
    if slot.period_key:
        deciding = deciding.filter(offered_period_key=slot.period_key)
    else:
        deciding = deciding.filter(offered_start=slot.start_at)
    if deciding.exists():
        return None  # someone is already deciding about this place
    candidates = (
        WaitlistEntry.objects.filter(organization=org, doctor_id=slot.doctor_id, status=WaitlistStatus.WAITING)
        .select_related("patient", "doctor", "visit_type")
        .order_by("created_at", "id")
    )
    for entry in candidates:
        if slot.key in entry.passed or not _matches(entry, slot) or not _reachable(entry.patient):
            continue
        free = _still_free(entry, slot)
        if free is None:
            continue
        return _offer(entry, free, ns, now)
    return None


@transaction.atomic
def _offer(entry, slot: Slot, ns, now) -> WaitlistEntry:
    from apps.notifications import services as notifications
    from apps.notifications.models import Event, MessageStatus
    from apps.notifications.templating import fmt_date, fmt_time

    entry = WaitlistEntry.objects.select_for_update().get(pk=entry.pk)
    entry.offers_made += 1
    entry.passed = [*entry.passed, slot.key]
    ctx = {
        "date": fmt_date(slot.day), "time": fmt_time(slot.start_at), "doctor_name": str(entry.doctor),
        "visit_type": entry.visit_type.name_ar, "hold_minutes": ns.waitlist_hold_minutes,
        "time_label": _("الساعة %(t)s") % {"t": fmt_time(slot.start_at)} if not slot.period_key
        else _("حوالي الساعة %(t)s") % {"t": fmt_time(slot.start_at)},
        "entry": entry.pk, "offer": entry.offers_made,
    }  # fmt: skip
    rows = notifications.enqueue_for_patient(
        org=entry.organization, patient=entry.patient, event=Event.WAITLIST_OFFER, send_at=now, context=ctx,
        key=f"wl:{entry.pk}:{entry.offers_made}",
    )  # fmt: skip
    sendable = [r for r in rows if r.status == MessageStatus.PENDING]
    if not sendable:  # no template, consent withdrawn, quiet hours with "skip": nobody was asked
        entry.save(update_fields=["offers_made", "passed", "updated_at"])
        return entry
    starts = min(r.send_at for r in sendable)  # deferred by quiet hours: the clock starts when it is sent
    entry.status = WaitlistStatus.OFFERED
    entry.offered_day = slot.day
    entry.offered_start = slot.start_at  # queue mode: the estimated time of the next number
    entry.offered_period_key = slot.period_key
    entry.offer_expires_at = max(starts, now) + timedelta(minutes=ns.waitlist_hold_minutes)
    entry.save()
    return entry


def on_slot_freed(appt) -> WaitlistEntry | None:
    """Called after a booking is cancelled or moved (on commit)."""
    slot = slot_of(appt)
    return offer_next(appt.organization, slot) if slot else None


def offered_slot(entry) -> Slot | None:
    if entry.status != WaitlistStatus.OFFERED or entry.offered_day is None:
        return None
    return Slot(entry.doctor_id, entry.offered_day, entry.offered_start, entry.offered_period_key)


def _clear_offer(entry, status=WaitlistStatus.WAITING):
    entry.status = status
    entry.offered_day = entry.offered_start = entry.offer_expires_at = None
    entry.offered_period_key = ""


def _withdraw_offer_messages(entry):
    from apps.notifications.models import MessageStatus, ScheduledMessage

    ScheduledMessage.objects.filter(
        organization=entry.organization, dedupe_key__startswith=f"wl:{entry.pk}:", status=MessageStatus.PENDING
    ).update(status=MessageStatus.CANCELLED, status_reason=_("العرض خلص"), updated_at=timezone.now())


# --- the answer ------------------------------------------------------------------------------------------------


@dataclass
class Answer:
    booked: Appointment | None = None
    note: str = ""
    too_late: bool = False
    other: Appointment | None = None  # another booking of hers with this doctor that is still on


@transaction.atomic
def respond(entry, *, accept: bool, now=None) -> Answer:
    """The patient's "1" / "2" to an offer. "1" books (the normal confirmation goes out); "2" passes the slot on."""
    from . import services

    now = now or timezone.now()
    entry = WaitlistEntry.objects.select_for_update().get(pk=entry.pk)
    slot = offered_slot(entry)
    if slot is None:
        return Answer(note=_("مفيش عرض مفتوح"), too_late=True)
    if entry.offer_expires_at and entry.offer_expires_at < now:
        _clear_offer(entry)
        entry.save()
        transaction.on_commit(lambda: offer_next(entry.organization, slot))
        return Answer(note=_("الرد جه بعد المهلة"), too_late=True)
    if not accept:
        _clear_offer(entry)
        entry.save()
        transaction.on_commit(lambda: offer_next(entry.organization, slot))
        return Answer(note=_("مش مناسب"))
    try:
        with transaction.atomic():
            appt = services.book(
                patient=entry.patient, doctor=entry.doctor, visit_type=entry.visit_type, by=None,
                start_at=None if slot.period_key else slot.start_at, day=slot.day,
                period_key=slot.period_key or None, source=BookingSource.WHATSAPP,
            )  # fmt: skip
    except services.BookingError:
        _clear_offer(entry)
        entry.save()
        return Answer(note=_("الميعاد اتحجز قبل الرد"), too_late=True)
    _clear_offer(entry, WaitlistStatus.BOOKED)
    entry.appointment = appt
    entry.save()
    note = _("اتحجزله")
    other = (
        Appointment.objects.filter(patient=entry.patient, doctor=entry.doctor, status__in=ACTIVE_STATUSES)
        .exclude(pk=appt.pk)
        .order_by("start_at")
        .first()
    )
    if other is not None:  # she was booked later and wanted earlier: reception decides about the old booking
        from apps.notifications.templating import fmt_date

        note += "، " + _("وعنده حجز تاني يوم %(d)s لسه قايم") % {"d": fmt_date(other.date)}
    return Answer(booked=appt, note=note, other=other)


# --- the scheduler job -----------------------------------------------------------------------------------------


def run(now=None) -> int:
    """Every tick: an offer nobody answered passes to the next one; entries whose last day passed close."""
    now = now or timezone.now()
    passed_on = 0
    for entry in WaitlistEntry.objects.filter(status=WaitlistStatus.OFFERED, offer_expires_at__lt=now):
        with transaction.atomic():
            locked = WaitlistEntry.objects.select_for_update().get(pk=entry.pk)
            slot = offered_slot(locked)
            if locked.status != WaitlistStatus.OFFERED or slot is None:
                continue
            _clear_offer(locked)
            locked.save()
        _withdraw_offer_messages(locked)
        if offer_next(locked.organization, slot, now=now):
            passed_on += 1
    day = timezone.localtime(now, CAIRO).date()
    WaitlistEntry.objects.filter(status=WaitlistStatus.WAITING, latest__lt=day).update(
        status=WaitlistStatus.EXPIRED, updated_at=now
    )
    return passed_on
