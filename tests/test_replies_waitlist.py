"""Phase 22 (plan 15 §1.1-1.3, §1.6): WhatsApp replies 1 / 2, the reception inbox, the waiting list, no-show risk."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from apps.core.timeutils import CAIRO, local_dt
from apps.doctors.models import Doctor, VisitType, WorkingPeriod
from apps.notifications import dispatcher as dispatch
from apps.notifications import replies, services
from apps.notifications.management.commands.seed_notifications import seed_org_notifications
from apps.notifications.models import (
    Event,
    InboundMessage,
    MessageChannel,
    MessageStatus,
    NotificationSettings,
    ReplyAction,
    ScheduledMessage,
)
from apps.patients.models import Gender, Patient
from apps.scheduling import services as booking
from apps.scheduling import waitlist
from apps.scheduling.models import AppointmentStatus, TimePreference, WaitlistEntry, WaitlistStatus
from tests.test_notifications import FakeGateway

NOW = datetime(2026, 1, 2, 10, 0, tzinfo=CAIRO)  # Friday morning; the slots below are Saturday
SATURDAY = date(2026, 1, 3)


def at(hour, minute=0, day=SATURDAY):
    return local_dt(day, datetime.min.time().replace(hour=hour, minute=minute))


@pytest.fixture(autouse=True)
def clock():
    with freeze_time(NOW) as frozen:
        yield frozen


@pytest.fixture
def gateway(settings):
    import httpx

    gw = FakeGateway()
    settings.WA_GATEWAY_TRANSPORT = httpx.MockTransport(gw.handler)
    return gw


def _patient(org, n, consent=True, **kw):
    return Patient.objects.create(
        organization=org, full_name=f"مريضة {n}", phone=f"+20100123400{n}", gender=Gender.FEMALE, file_number=n,
        messaging_consent=consent, **kw,
    )  # fmt: skip


@pytest.fixture
def clinic(org_a, make_member, gateway):
    seed_org_notifications(org_a)
    doctor = Doctor.objects.create(organization=org_a, name_ar="سارة علي", title_ar="د.", slot_minutes=20)
    for weekday in range(7):
        WorkingPeriod.objects.create(
            organization=org_a, doctor=doctor, weekday=weekday, start_time="09:00", end_time="21:00"
        )
    vt = VisitType.objects.create(
        organization=org_a, doctor=doctor, name_ar="كشف", duration_minutes=20, price=Decimal("300")
    )
    return {
        "org": org_a, "doctor": doctor, "vt": vt, "staff": make_member(org_a, "reception"),
        "p1": _patient(org_a, 1), "p2": _patient(org_a, 2), "p3": _patient(org_a, 3),
    }  # fmt: skip


def _book(clinic, patient, start, capture):
    with capture(execute=True):
        return booking.book(patient=patient, doctor=clinic["doctor"], visit_type=clinic["vt"], by=clinic["staff"],
                            start_at=start)  # fmt: skip


def _sent(rows, when=None):
    """The outbox rows as if the dispatcher had sent them on WhatsApp."""
    ids = [r.pk for r in rows if r.channel == MessageChannel.WHATSAPP]
    ScheduledMessage.objects.filter(pk__in=ids).update(status=MessageStatus.SENT, sent_at=when or timezone.now())


def _reply(clinic, patient, body, msg_id=None):
    return services.record_inbound(
        clinic["org"].pk, provider_message_id=msg_id or f"in-{patient.pk}-{InboundMessage.objects.count()}",
        from_number=patient.phone.lstrip("+"), body=body,
    )  # fmt: skip


def _rows(event, **kw):
    return list(ScheduledMessage.objects.filter(event=event, **kw))


# --- reading the answer ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected",
    [("1", "yes"), (" ١ ", "yes"), ("تأكيد", "yes"), ("تمام!", "yes"), ("2", "no"), ("٢", "no"), ("إلغاء", "no"),
     ("لا", "no"), ("ممكن أأجل للأسبوع الجاي؟", None), ("1 بس هتأخر شوية", None), ("", None)],
)  # fmt: skip
def test_intent_reads_whole_answers_only(body, expected):
    assert replies.intent(body) == expected


# --- replies to a booking ----------------------------------------------------------------------------------------


def test_one_confirms_and_sends_a_short_ok(clinic, django_capture_on_commit_callbacks):
    appt = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    _sent(_rows(Event.BOOKING_CONFIRMED))
    with django_capture_on_commit_callbacks(execute=True):
        msg = _reply(clinic, clinic["p1"], "1")
    appt.refresh_from_db()
    msg.refresh_from_db()
    assert appt.status == AppointmentStatus.CONFIRMED
    assert msg.action == ReplyAction.CONFIRMED and msg.handled_at is not None and msg.appointment == appt
    assert [r.status for r in _rows(Event.REPLY_CONFIRMED, appointment=appt)] == [MessageStatus.PENDING]
    # confirming does not withdraw the reminders already planned
    assert any(r.status == MessageStatus.PENDING for r in _rows(Event.REMINDER, appointment=appt))


def test_two_cancels_and_waits_for_reception(clinic, django_capture_on_commit_callbacks):
    appt = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    _sent(_rows(Event.BOOKING_CONFIRMED))
    with django_capture_on_commit_callbacks(execute=True):
        msg = _reply(clinic, clinic["p1"], "2")
    appt.refresh_from_db()
    msg.refresh_from_db()
    assert appt.status == AppointmentStatus.CANCELLED
    assert msg.action == ReplyAction.CANCELLED and msg.handled_at is None
    assert _rows(Event.CANCELLED, appointment=appt)
    assert msg in replies.unhandled(clinic["org"])


def test_the_same_message_delivered_twice_is_acted_on_once(clinic, django_capture_on_commit_callbacks):
    appt = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    _sent(_rows(Event.BOOKING_CONFIRMED))
    with django_capture_on_commit_callbacks(execute=True):
        _reply(clinic, clinic["p1"], "1", msg_id="same")
        _reply(clinic, clinic["p1"], "1", msg_id="same")
    assert InboundMessage.objects.count() == 1
    assert len(_rows(Event.REPLY_CONFIRMED, appointment=appt)) == 1


def test_a_reply_after_the_window_or_without_a_message_is_left_to_reception(
    clinic, clock, django_capture_on_commit_callbacks
):
    msg = _reply(clinic, clinic["p2"], "1")  # we never wrote to her
    assert msg.action == "" and msg.handled_at is None
    appt = _book(clinic, clinic["p1"], at(12, day=SATURDAY + timedelta(days=5)), django_capture_on_commit_callbacks)
    _sent(_rows(Event.BOOKING_CONFIRMED))
    clock.tick(timedelta(hours=49))  # the reply window is 48 hours
    msg = _reply(clinic, clinic["p1"], "1")
    appt.refresh_from_db()
    assert appt.status == AppointmentStatus.BOOKED and msg.action == ""


def test_free_text_is_never_acted_on(clinic, django_capture_on_commit_callbacks):
    appt = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    _sent(_rows(Event.BOOKING_CONFIRMED))
    msg = _reply(clinic, clinic["p1"], "هو أنا ممكن آجي 1 بدل 12؟")
    appt.refresh_from_db()
    assert appt.status == AppointmentStatus.BOOKED and msg.action == "" and msg.handled_at is None


def test_replies_switched_off(clinic, django_capture_on_commit_callbacks):
    NotificationSettings.objects.filter(organization=clinic["org"]).update(replies_enabled=False)
    appt = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    _sent(_rows(Event.BOOKING_CONFIRMED))
    _reply(clinic, clinic["p1"], "2")
    appt.refresh_from_db()
    assert appt.status == AppointmentStatus.BOOKED


def test_the_reply_line_is_added_on_whatsapp_only(clinic, gateway, settings, django_capture_on_commit_callbacks):
    settings.NOTIFY_SLEEP_BETWEEN_WA = False
    p = clinic["p1"]
    p.email = "p1@example.com"
    p.save()
    NotificationSettings.objects.filter(organization=clinic["org"]).update(default_channel="both")
    _book(clinic, p, at(12), django_capture_on_commit_callbacks)
    dispatch.Dispatcher(sleep=lambda s: None).run_once()
    wa = ScheduledMessage.objects.get(event=Event.BOOKING_CONFIRMED, channel=MessageChannel.WHATSAPP)
    email = ScheduledMessage.objects.get(event=Event.BOOKING_CONFIRMED, channel=MessageChannel.EMAIL)
    assert wa.rendered_text.endswith("للتأكيد ابعت 1، وللإلغاء ابعت 2")
    assert "ابعت 1" not in email.rendered_text


# --- the waiting list --------------------------------------------------------------------------------------------


def _wait(clinic, patient, **kw):
    kw.setdefault("earliest", SATURDAY)
    kw.setdefault("latest", SATURDAY + timedelta(days=3))
    return waitlist.add(patient=patient, doctor=clinic["doctor"], visit_type=clinic["vt"], by=clinic["staff"], **kw)


def _cancel(appt, capture):
    with capture(execute=True):
        booking.cancel(appt, by=None)


def test_a_cancelled_slot_is_offered_to_the_first_waiting_and_a_one_books_it(
    clinic, django_capture_on_commit_callbacks
):
    taken = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    first, second = _wait(clinic, clinic["p2"]), _wait(clinic, clinic["p3"])
    _cancel(taken, django_capture_on_commit_callbacks)
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == WaitlistStatus.OFFERED and second.status == WaitlistStatus.WAITING
    assert first.offered_start == at(12) and first.offer_expires_at == timezone.now() + timedelta(minutes=60)
    offer = _rows(Event.WAITLIST_OFFER, patient=clinic["p2"])
    assert len(offer) == 1 and offer[0].status == MessageStatus.PENDING
    _sent(offer)
    with django_capture_on_commit_callbacks(execute=True):
        msg = _reply(clinic, clinic["p2"], "1")
    first.refresh_from_db()
    msg.refresh_from_db()
    assert first.status == WaitlistStatus.BOOKED and first.appointment.start_at == at(12)
    assert first.appointment.source == "whatsapp"
    assert msg.action == ReplyAction.WAITLIST_BOOKED and msg.handled_at is not None
    assert _rows(Event.BOOKING_CONFIRMED, appointment=first.appointment)  # the usual confirmation goes out


def test_a_two_passes_the_slot_to_the_next(clinic, django_capture_on_commit_callbacks):
    taken = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    first, second = _wait(clinic, clinic["p2"]), _wait(clinic, clinic["p3"])
    _cancel(taken, django_capture_on_commit_callbacks)
    _sent(_rows(Event.WAITLIST_OFFER, patient=clinic["p2"]))
    with django_capture_on_commit_callbacks(execute=True):
        msg = _reply(clinic, clinic["p2"], "2")
    first.refresh_from_db()
    second.refresh_from_db()
    assert msg.action == ReplyAction.WAITLIST_DECLINED
    assert first.status == WaitlistStatus.WAITING and second.status == WaitlistStatus.OFFERED
    # she is never offered that same slot again
    assert first.passed == [waitlist.Slot(clinic["doctor"].pk, SATURDAY, at(12)).key]


def test_silence_passes_the_slot_on_when_the_hold_ends(clinic, clock, django_capture_on_commit_callbacks):
    taken = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    first, second = _wait(clinic, clinic["p2"]), _wait(clinic, clinic["p3"])
    _cancel(taken, django_capture_on_commit_callbacks)
    clock.tick(timedelta(minutes=61))
    assert waitlist.run() == 1
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == WaitlistStatus.WAITING and second.status == WaitlistStatus.OFFERED
    late = _reply(clinic, clinic["p2"], "1")  # her offer message was never marked sent: nothing to answer
    assert late.action == ""


def test_offer_follows_dates_time_of_day_and_consent(clinic, django_capture_on_commit_callbacks):
    taken = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    _wait(clinic, clinic["p2"], earliest=SATURDAY + timedelta(days=1))  # wants later days
    _wait(clinic, clinic["p3"], time_pref=TimePreference.EVENING)  # 12:00 is "morning"
    no_consent = _patient(clinic["org"], 4, consent=False)
    _wait(clinic, no_consent)
    fits = _patient(clinic["org"], 5)
    good = _wait(clinic, fits, time_pref=TimePreference.MORNING)
    _cancel(taken, django_capture_on_commit_callbacks)
    good.refresh_from_db()
    assert good.status == WaitlistStatus.OFFERED
    assert WaitlistEntry.objects.filter(status=WaitlistStatus.OFFERED).count() == 1


def test_a_slot_taken_before_the_answer(clinic, django_capture_on_commit_callbacks):
    taken = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    entry = _wait(clinic, clinic["p2"])
    _cancel(taken, django_capture_on_commit_callbacks)
    _sent(_rows(Event.WAITLIST_OFFER))
    _book(clinic, clinic["p3"], at(12), django_capture_on_commit_callbacks)  # reception gave it away meanwhile
    with django_capture_on_commit_callbacks(execute=True):
        msg = _reply(clinic, clinic["p2"], "1")
    entry.refresh_from_db()
    assert msg.action == ReplyAction.TOO_LATE and msg.handled_at is None
    assert entry.status == WaitlistStatus.WAITING


def test_accepting_while_booked_later_tells_reception(clinic, django_capture_on_commit_callbacks):
    later = _book(clinic, clinic["p2"], at(12, day=SATURDAY + timedelta(days=10)), django_capture_on_commit_callbacks)
    taken = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    _wait(clinic, clinic["p2"])
    _cancel(taken, django_capture_on_commit_callbacks)
    _sent(_rows(Event.WAITLIST_OFFER))
    with django_capture_on_commit_callbacks(execute=True):
        msg = _reply(clinic, clinic["p2"], "1")
    later.refresh_from_db()
    assert msg.action == ReplyAction.WAITLIST_BOOKED and msg.handled_at is None and "لسه قايم" in msg.action_note
    assert later.status == AppointmentStatus.BOOKED  # never cancelled by the program


def test_a_booking_inside_the_wanted_days_closes_the_request(clinic, django_capture_on_commit_callbacks):
    entry = _wait(clinic, clinic["p2"])
    _book(clinic, clinic["p2"], at(15), django_capture_on_commit_callbacks)
    entry.refresh_from_db()
    assert entry.status == WaitlistStatus.BOOKED


def test_rules_of_the_list(clinic):
    _wait(clinic, clinic["p2"])
    with pytest.raises(waitlist.WaitlistError):
        _wait(clinic, clinic["p2"])  # one open request per patient and doctor
    with pytest.raises(waitlist.WaitlistError):
        _wait(clinic, clinic["p3"], earliest=SATURDAY, latest=SATURDAY - timedelta(days=1))
    with pytest.raises(waitlist.WaitlistError):
        _wait(clinic, clinic["p3"], earliest=date(2025, 12, 1), latest=date(2025, 12, 5))


def test_past_requests_close(clinic, clock):
    entry = _wait(clinic, clinic["p2"], latest=SATURDAY)
    clock.tick(timedelta(days=3))
    waitlist.run()
    entry.refresh_from_db()
    assert entry.status == WaitlistStatus.EXPIRED


def test_nothing_is_offered_when_switched_off_or_too_soon(clinic, clock, django_capture_on_commit_callbacks):
    NotificationSettings.objects.filter(organization=clinic["org"]).update(waitlist_enabled=False)
    taken = _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    entry = _wait(clinic, clinic["p2"])
    _cancel(taken, django_capture_on_commit_callbacks)
    entry.refresh_from_db()
    assert entry.status == WaitlistStatus.WAITING
    NotificationSettings.objects.filter(organization=clinic["org"]).update(waitlist_enabled=True)
    soon = _book(clinic, clinic["p1"], at(10, 40, day=NOW.date()), django_capture_on_commit_callbacks)
    _wait(clinic, clinic["p3"], earliest=NOW.date())
    _cancel(soon, django_capture_on_commit_callbacks)  # 40 minutes ahead: not worth offering
    assert not _rows(Event.WAITLIST_OFFER)


# --- no-show risk ----------------------------------------------------------------------------------------------


def test_a_patient_who_often_misses_gets_one_more_reminder(clinic, django_capture_on_commit_callbacks):
    Patient.objects.filter(pk=clinic["p1"].pk).update(no_show_count=2)
    clinic["p1"].refresh_from_db()
    appt = _book(clinic, clinic["p1"], at(18), django_capture_on_commit_callbacks)
    risk = [r for r in _rows(Event.REMINDER, appointment=appt) if ":risk:" in r.dedupe_key]
    assert len(risk) == 1 and risk[0].send_at == at(15)
    plain = _book(clinic, clinic["p2"], at(19), django_capture_on_commit_callbacks)
    assert not [r for r in _rows(Event.REMINDER, appointment=plain) if ":risk:" in r.dedupe_key]


def test_the_risk_tag_on_the_board_and_booking_panel(client, clinic, django_capture_on_commit_callbacks):
    Patient.objects.filter(pk=clinic["p1"].pk).update(no_show_count=3)
    _book(clinic, clinic["p1"], at(12), django_capture_on_commit_callbacks)
    client.force_login(clinic["staff"])
    html = client.get(reverse("reception:today"), {"date": SATURDAY.isoformat(), "view": "day"}).content.decode()
    assert "غاب 3 مرات" in html
    html = client.get(reverse("scheduling:booking_panel"), {"patient": clinic["p1"].pk}).content.decode()
    assert "غاب 3 مرات" in html


# --- screens -----------------------------------------------------------------------------------------------------


def test_inbox_lists_and_marks_handled(client, clinic):
    msg = _reply(clinic, clinic["p1"], "ممكن أعرف مواعيد الدكتورة يوم الخميس؟")
    client.force_login(clinic["staff"])
    html = client.get(reverse("notifications:inbox")).content.decode()
    assert "مواعيد الدكتورة" in html
    r = client.post(reverse("notifications:inbox_handle", args=[msg.pk]), HTTP_HX_REQUEST="true")
    assert r.status_code == 200
    msg.refresh_from_db()
    assert msg.handled_at is not None and msg.handled_by == clinic["staff"]
    assert "مواعيد الدكتورة" not in client.get(reverse("notifications:inbox")).content.decode()
    # the board says how many wait
    _reply(clinic, clinic["p2"], "سؤال تاني")
    assert "رسالة من مريض محتاجة رد" in client.get(reverse("reception:today")).content.decode()


def test_waitlist_screens(client, clinic):
    client.force_login(clinic["staff"])
    r = client.get(reverse("scheduling:waitlist_add"), {"patient": clinic["p2"].pk, "visit_type": clinic["vt"].pk,
                                                       "day": SATURDAY.isoformat()})  # fmt: skip
    assert r.status_code == 200 and "قايمة الانتظار" in r.content.decode()
    r = client.post(reverse("scheduling:waitlist_add"), {
        "patient": clinic["p2"].pk, "visit_type": clinic["vt"].pk, "earliest": SATURDAY.isoformat(),
        "latest": (SATURDAY + timedelta(days=5)).isoformat(), "time_pref": "any", "notes": "أي ميعاد بدري",
    })  # fmt: skip
    assert r.status_code == 200 and "أي ميعاد بدري" in r.content.decode()
    entry = WaitlistEntry.objects.get()
    assert client.get(reverse("scheduling:waitlist")).status_code == 200
    client.post(reverse("scheduling:waitlist_remove", args=[entry.pk]))
    entry.refresh_from_db()
    assert entry.status == WaitlistStatus.CANCELLED


def test_permissions_and_other_clinics(client, clinic, org_b, make_member):
    entry = _wait(clinic, clinic["p2"])
    msg = _reply(clinic, clinic["p1"], "سؤال")
    viewer = make_member(clinic["org"], "viewer")
    client.force_login(viewer)
    assert client.get(reverse("notifications:inbox")).status_code == 403
    assert client.post(reverse("scheduling:waitlist_remove", args=[entry.pk])).status_code == 403
    client.force_login(make_member(org_b, "owner"))
    assert client.post(reverse("notifications:inbox_handle", args=[msg.pk])).status_code == 404
    assert client.post(reverse("scheduling:waitlist_remove", args=[entry.pk])).status_code == 404
    r = client.get(reverse("scheduling:waitlist_add"), {"patient": clinic["p2"].pk, "visit_type": clinic["vt"].pk})
    assert r.status_code == 404


def test_settings_save_the_new_fields(client, clinic, make_member):
    client.force_login(make_member(clinic["org"], "owner"))
    form = client.get(reverse("notifications:settings")).context["form"]
    data = {k: v for k, v in form.initial.items() if v is not None and v is not False}
    data.update(reply_hint="ابعت 1 للتأكيد", waitlist_hold_minutes=30, no_show_risk_count=3, reply_window_hours=24,
                risk_extra_reminder_hours=0, quiet_start="22:00", quiet_end="09:00", urgent_until="23:00")  # fmt: skip
    data.pop("waitlist_enabled", None)
    r = client.post(reverse("notifications:settings"), data)
    assert r.status_code == 302
    ns = NotificationSettings.objects.get(organization=clinic["org"])
    assert (ns.reply_hint, ns.waitlist_hold_minutes, ns.no_show_risk_count, ns.waitlist_enabled) == (
        "ابعت 1 للتأكيد", 30, 3, False,
    )  # fmt: skip
