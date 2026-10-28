"""The reception board (reception:today): today's reception work, the day and week schedule as tables with AM/PM
times, free slots that open the booking panel, and every action re-rendering the day the user is on."""

import re
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from freezegun import freeze_time

from apps.core.templatetags.clock import ampm
from apps.core.timeutils import local_dt
from apps.doctors.models import BookingMode, Doctor, VisitType, WorkingPeriod
from apps.patients.models import Gender, Patient
from apps.scheduling import services
from apps.scheduling.models import Appointment, AppointmentStatus

SATURDAY = date(2026, 1, 3)
NOW = "2026-01-03 12:00:00+02:00"  # Saturday noon, before the 2 PM – 5 PM period


@pytest.fixture
def world(org_a, org_b, make_member):
    doctor = Doctor.objects.create(organization=org_a, name_ar="سارة", booking_mode=BookingMode.SLOTS, slot_minutes=30)
    WorkingPeriod.objects.create(organization=org_a, doctor=doctor, weekday=0, start_time="14:00", end_time="17:00")
    WorkingPeriod.objects.create(organization=org_a, doctor=doctor, weekday=1, start_time="14:00", end_time="17:00")
    vt = VisitType.objects.create(organization=org_a, doctor=doctor, name_ar="كشف", duration_minutes=30,
                                  price=Decimal("300"))  # fmt: skip
    patient = Patient.objects.create(organization=org_a, full_name="منى سعيد", phone="+201001234567",
                                     gender=Gender.FEMALE, file_number=1)  # fmt: skip
    reception = make_member(org_a, "reception")
    with freeze_time(NOW):
        appt = services.book(patient=patient, doctor=doctor, visit_type=vt, by=reception,
                             start_at=local_dt(SATURDAY, time(15, 0)))  # fmt: skip
    return {"org": org_a, "doctor": doctor, "vt": vt, "patient": patient, "appt": appt, "reception": reception,
            "viewer": make_member(org_a, "viewer"), "other": make_member(org_b, "owner")}  # fmt: skip


def test_ampm_filter():
    assert ampm(time(0, 5)) == "12:05 AM"
    assert ampm(time(12, 0)) == "12:00 PM"
    assert ampm(local_dt(SATURDAY, time(14, 30))) == "2:30 PM"
    assert ampm(None) == ""


def test_day_table_has_every_slot_booked_or_free(client, world):
    client.force_login(world["reception"])
    with freeze_time(NOW):
        resp = client.get(reverse("reception:today"), {"view": "day", "date": SATURDAY.isoformat()})
    html = resp.content.decode()
    slots = resp.context["board"]["slots"]
    assert [s.time.strftime("%H:%M") for s in slots] == ["14:00", "14:30", "15:00", "15:30", "16:00", "16:30"]
    booked = next(s for s in slots if s.entries)
    assert booked.entries[0].a.pk == world["appt"].pk and not booked.bookable
    assert all(s.bookable for s in slots if not s.entries)
    for label in ("2:00 PM", "2:30 PM", "3:00 PM", "4:30 PM"):
        assert label in html
    assert html.count(reverse("scheduling:booking_panel") + "?at=") == 5  # one "+ احجز" per free slot
    assert reverse("reception:arrive", args=[world["appt"].pk]) in html  # today: the reception steps


def test_past_free_slots_are_hidden_and_a_long_booking_covers_the_next_slot(client, world):
    long_vt = VisitType.objects.create(organization=world["org"], doctor=world["doctor"], name_ar="جلسة",
                                       duration_minutes=60, price=Decimal("500"))  # fmt: skip
    other = Patient.objects.create(organization=world["org"], full_name="سامي", phone="+201001234568",
                                   gender=Gender.MALE, file_number=2)  # fmt: skip
    with freeze_time(NOW):
        services.book(patient=other, doctor=world["doctor"], visit_type=long_vt, by=world["reception"],
                      start_at=local_dt(SATURDAY, time(16, 0)))  # fmt: skip
    client.force_login(world["reception"])
    with freeze_time("2026-01-03 14:45:00+02:00"):
        slots = client.get(reverse("reception:today"), {"date": SATURDAY.isoformat()}).context["board"]["slots"]
    by_time = {s.time.strftime("%H:%M"): s for s in slots}
    assert by_time["14:00"].past and not by_time["14:00"].bookable
    assert by_time["16:30"].covered and not by_time["16:30"].bookable  # inside the 4 PM hour-long session


def test_booking_outside_the_periods_is_still_shown(client, world):
    other = Patient.objects.create(organization=world["org"], full_name="خارج المواعيد", phone="+201001234569",
                                   gender=Gender.MALE, file_number=3)  # fmt: skip
    with freeze_time(NOW):
        services.book(patient=other, doctor=world["doctor"], visit_type=world["vt"], by=world["reception"],
                      start_at=local_dt(SATURDAY, time(20, 0)), overbook=True)  # fmt: skip
        client.force_login(world["reception"])
        resp = client.get(reverse("reception:today"), {"date": SATURDAY.isoformat()})
    slot = next(s for s in resp.context["board"]["slots"] if s.time.hour == 20)
    assert slot.outside and slot.entries and "8:00 PM" in resp.content.decode() and "استثنائي" in resp.content.decode()


def test_other_days_have_no_reception_steps_but_keep_the_menu(client, world):
    client.force_login(world["reception"])
    with freeze_time("2026-01-02 12:00:00+02:00"):  # the day before
        html = client.get(reverse("reception:today"), {"date": SATURDAY.isoformat()}).content.decode()
    assert reverse("reception:arrive", args=[world["appt"].pk]) not in html
    assert reverse("scheduling:reschedule_options", args=[world["appt"].pk]) in html
    assert reverse("scheduling:appointment_cancel", args=[world["appt"].pk]) in html


def test_the_board_remembers_the_view_and_actions_rerender_that_day(client, world):
    client.force_login(world["reception"])
    with freeze_time(NOW):
        client.get(reverse("reception:today"), {"view": "week", "date": SATURDAY.isoformat()})
        assert client.get(reverse("reception:today")).context["view"] == "week"  # the view is remembered
        client.get(reverse("reception:today"), {"view": "day", "date": SATURDAY.isoformat()})
        resp = client.post(reverse("reception:step", args=[world["appt"].pk, "no_show"]), HTTP_HX_REQUEST="true")
    assert resp.templates[0].name == "reception/partials/board.html"
    assert resp.context["view"] == "day" and resp.context["day"] == SATURDAY


def test_polling_signature_includes_the_view(client, world):
    client.force_login(world["reception"])
    with freeze_time(NOW):
        html = client.get(reverse("reception:rows"), {"view": "day"}).content.decode()
        sig = re.search(r'id="rows-sig" value="(\w+)"', html).group(1)
        assert client.get(reverse("reception:rows"), {"view": "day", "sig": sig}).status_code == 204
        assert client.get(reverse("reception:rows"), {"view": "week", "sig": sig}).status_code == 200


def test_walk_in_from_the_booking_panel(client, world):
    newcomer = Patient.objects.create(organization=world["org"], full_name="جديد", phone="+201001234570",
                                      gender=Gender.MALE, file_number=4)  # fmt: skip
    client.force_login(world["reception"])
    with freeze_time("2026-01-03 14:10:00+02:00"):
        panel = client.get(reverse("scheduling:booking_panel"), {"patient": newcomer.pk}).content.decode()
        assert reverse("reception:walk_in") in panel  # today only
        resp = client.post(reverse("reception:walk_in"), {"patient": newcomer.pk, "visit_type": world["vt"].pk},
                           HTTP_HX_REQUEST="true")  # fmt: skip
    assert resp.status_code == 200 and "board-refresh" in resp["HX-Trigger"]
    appt = Appointment.objects.get(patient=newcomer)
    assert appt.status == AppointmentStatus.ARRIVED and appt.source == "walk_in"


def test_queue_mode_board(client, world):
    doctor = world["doctor"]
    doctor.booking_mode = BookingMode.QUEUE
    doctor.queue_avg_minutes = 15
    doctor.save()
    WorkingPeriod.objects.filter(doctor=doctor).update(max_patients=10)
    other = Patient.objects.create(organization=world["org"], full_name="دور", phone="+201001234571",
                                   gender=Gender.MALE, file_number=5)  # fmt: skip
    with freeze_time(NOW):
        services.book(patient=other, doctor=doctor, visit_type=world["vt"], by=world["reception"], day=SATURDAY,
                      period_key=f"wp-{WorkingPeriod.objects.get(doctor=doctor, weekday=0).pk}")  # fmt: skip
        client.force_login(world["reception"])
        resp = client.get(reverse("reception:today"), {"date": SATURDAY.isoformat()})
    html = resp.content.decode()
    [q] = resp.context["board"]["queue"]
    assert [e.a.queue_number for e in q["entries"] if e.a.queue_number] == [1]  # (+ the slot-mode booking)
    assert "#1" in html and "/ 10" in html and "احجز رقم في الفترة دي" in html


def test_week_cell_opens_the_panel_on_that_hour(client, world):
    client.force_login(world["reception"])
    with freeze_time(NOW):
        html = client.get(reverse("reception:today"), {"view": "week", "date": SATURDAY.isoformat()}).content.decode()
    assert f"{reverse('scheduling:booking_panel')}?day=2026-01-03&hour=14" in html
    assert f"{reverse('scheduling:booking_panel')}?day=2026-01-05&hour=14" not in html  # Monday: no work


def test_other_clinic_sees_nothing(client, world):
    client.force_login(world["other"])
    with freeze_time(NOW):
        html = client.get(reverse("reception:today"), {"date": SATURDAY.isoformat()}).content.decode()
    assert "منى سعيد" not in html


def test_viewer_sees_the_schedule_read_only(client, world):
    client.force_login(world["viewer"])
    with freeze_time(NOW):
        html = client.get(reverse("reception:today"), {"date": SATURDAY.isoformat()}).content.decode()
    assert "منى سعيد" in html and "3:00 PM" in html
    for name in ("scheduling:booking_panel", "reception:walk_in", "billing:cashbox"):
        assert reverse(name) not in html
    assert reverse("reception:arrive", args=[world["appt"].pk]) not in html


def test_cell_after_now_is_bookable_until_the_horizon(client, world):
    world["doctor"].booking_horizon_days = 3
    world["doctor"].save()
    client.force_login(world["reception"])
    far = SATURDAY + timedelta(days=7)
    with freeze_time(NOW):
        board = client.get(reverse("reception:today"), {"date": far.isoformat()}).context["board"]
    assert board["slots"] and not any(s.bookable for s in board["slots"])
