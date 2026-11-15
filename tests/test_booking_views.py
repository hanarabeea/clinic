from datetime import date, time
from decimal import Decimal

import pytest
from django.urls import reverse
from freezegun import freeze_time

from apps.core.timeutils import local_dt
from apps.doctors.models import BookingMode, Doctor, ScheduleExceptionKind, VisitType, WorkingPeriod
from apps.patients import services as patient_services
from apps.patients.models import Gender, Patient
from apps.scheduling import services
from apps.scheduling.models import Appointment

SATURDAY = date(2026, 1, 3)


@pytest.fixture
def world(org_a, make_member):
    owner = make_member(org_a, "owner")
    reception = make_member(org_a, "reception")
    doctor = Doctor.objects.create(organization=org_a, name_ar="سارة", booking_mode=BookingMode.SLOTS, slot_minutes=10)
    WorkingPeriod.objects.create(organization=org_a, doctor=doctor, weekday=0, start_time="14:00", end_time="21:00")
    vt = VisitType.objects.create(
        organization=org_a, doctor=doctor, name_ar="كشف", duration_minutes=20, price=Decimal("400")
    )
    patient = patient_services.create_patient(
        org_a, Patient(organization=org_a, full_name="محمد أحمد", phone="+201001234567", gender=Gender.MALE)
    )
    return {"org": org_a, "owner": owner, "reception": reception, "doctor": doctor, "vt": vt, "patient": patient}


def test_booking_search_finds_existing_patient(client, world):
    client.force_login(world["reception"])
    r = client.get(reverse("scheduling:booking_search"), {"q": "0100 123"})
    assert "محمد أحمد" in r.content.decode()


def test_book_existing_patient_end_to_end(client, world):
    """The booking panel on the reception board: the patient's visit types and free times (AM/PM) → احجز → the panel
    closes (empty response), a toast and a board refresh are triggered."""
    client.force_login(world["reception"])
    with freeze_time("2026-01-01 06:00:00"):
        panel = client.get(
            reverse("scheduling:booking_panel"), {"patient": world["patient"].pk, "day": SATURDAY.isoformat()}
        ).content.decode()
        assert "كشف" in panel and "2:00 PM" in panel and "محمد أحمد" in panel
        start = local_dt(SATURDAY, time(14, 0))
        r = client.post(
            reverse("scheduling:booking_confirm", args=[world["patient"].pk]),
            {"visit_type": world["vt"].pk, "start_at": start.isoformat(), "day": SATURDAY.isoformat()},
            HTTP_HX_REQUEST="true",
        )
    assert r.status_code == 200 and r.content == b""
    assert "board-refresh" in r["HX-Trigger"] and "2:00 PM" in r["HX-Trigger"]
    assert Appointment.objects.filter(patient=world["patient"]).exists()


def test_booking_without_htmx_goes_back_to_the_board(client, world):
    client.force_login(world["reception"])
    with freeze_time("2026-01-01 06:00:00"):
        r = client.post(
            reverse("scheduling:booking_confirm", args=[world["patient"].pk]),
            {"visit_type": world["vt"].pk, "start_at": local_dt(SATURDAY, time(14, 0)).isoformat()},
        )
    assert r.status_code == 302 and r.url == f"{reverse('reception:today')}?date={SATURDAY}"


def test_a_taken_slot_keeps_the_panel_open_with_the_reason(client, world):
    client.force_login(world["reception"])
    start = local_dt(SATURDAY, time(14, 0))
    with freeze_time("2026-01-01 06:00:00"):
        services.book(patient=world["patient"], doctor=world["doctor"], visit_type=world["vt"], by=world["owner"],
                      start_at=start)  # fmt: skip
        r = client.post(
            reverse("scheduling:booking_confirm", args=[world["patient"].pk]),
            {"visit_type": world["vt"].pk, "start_at": start.isoformat(), "day": SATURDAY.isoformat()},
            HTTP_HX_REQUEST="true",
        )
    html = r.content.decode()
    assert r.status_code == 200 and 'id="booking-panel"' in html and "notice-danger" in html


def test_a_free_cell_preselects_its_time(client, world):
    client.force_login(world["reception"])
    at = local_dt(SATURDAY, time(15, 30))
    with freeze_time("2026-01-01 06:00:00"):
        html = client.get(
            reverse("scheduling:booking_panel"), {"patient": world["patient"].pk, "at": at.isoformat()}
        ).content.decode()
        by_hour = client.get(
            reverse("scheduling:booking_panel"),
            {"patient": world["patient"].pk, "day": SATURDAY.isoformat(), "hour": "17"},
        ).content.decode()
    assert f'value="{at.isoformat()}" class="sr-only" required checked' in html and "3:30 PM" in html
    five = local_dt(SATURDAY, time(17, 0)).isoformat()
    assert f'value="{five}" class="sr-only" required checked' in by_hour  # the week table's hour → its first slot


def test_new_patient_quick_add_then_book(client, world):
    """Added from the booking panel: the panel moves straight on to that patient's times."""
    client.force_login(world["reception"])
    data = {
        "full_name": "منى حسن", "gender": "female", "phone": "01099998888", "whatsapp": "",
        "whatsapp_same_as_phone": "on", "preferred_channel": "whatsapp", "messaging_consent": "on",
        "day": SATURDAY.isoformat(),
    }  # fmt: skip
    r = client.post(reverse("scheduling:booking_new_patient"), data, HTTP_HX_REQUEST="true")
    patient = Patient.objects.get(full_name="منى حسن")
    assert r.status_code == 200 and r["HX-Retarget"] == "#modal"
    assert 'id="booking-panel"' in r.content.decode() and f"#{patient.file_number}" in r.content.decode()
    bad = client.post(reverse("scheduling:booking_new_patient"), {**data, "full_name": ""}, HTTP_HX_REQUEST="true")
    assert 'id="new-patient-form"' in bad.content.decode() and "HX-Retarget" not in bad  # errors in place
    plain = client.post(reverse("scheduling:booking_new_patient"), {**data, "phone": "01099997777", "full_name": "x"})
    assert plain.status_code == 302 and "book=" in plain.url


def test_booking_mode_switch_changes_the_screen(client, world):
    client.force_login(world["reception"])
    with freeze_time("2026-01-01 06:00:00"):
        slots = client.get(
            reverse("scheduling:booking_slots", args=[world["patient"].pk]),
            {"visit_type": world["vt"].pk, "day": SATURDAY.isoformat()},
        ).content.decode()
    assert "2:00 PM" in slots and "رقم الدور الجاي" not in slots

    world["doctor"].booking_mode = BookingMode.QUEUE
    world["doctor"].queue_avg_minutes = 15
    world["doctor"].save()
    WorkingPeriod.objects.filter(doctor=world["doctor"]).update(max_patients=20)

    with freeze_time("2026-01-01 06:00:00"):
        queue_view = client.get(
            reverse("scheduling:booking_slots", args=[world["patient"].pk]),
            {"visit_type": world["vt"].pk, "day": SATURDAY.isoformat()},
        ).content.decode()
    assert "رقم الدور الجاي" in queue_view


def test_closing_a_day_with_bookings_shows_affected_with_suggestions(client, world):
    client.force_login(world["owner"])
    with freeze_time("2026-01-01 06:00:00"):
        for hour in (14, 15, 16, 17, 18):
            services.book(
                patient=world["patient"], doctor=world["doctor"], visit_type=world["vt"], by=world["owner"],
                start_at=local_dt(SATURDAY, time(hour, 0)),
            )  # fmt: skip
        assert Appointment.objects.filter(doctor=world["doctor"], date=SATURDAY).count() == 5

        r = client.post(
            reverse("doctors:exception_add"), {"date": SATURDAY.isoformat(), "kind": ScheduleExceptionKind.CLOSED}
        )
        assert r.status_code == 200 and r.headers.get("HX-Redirect")
        assert r.headers["HX-Redirect"].endswith(f"?date={SATURDAY}")

        page = client.get(r.headers["HX-Redirect"]).content.decode()
    assert page.count("أجّل للمقترح") == 5


def test_reception_cannot_manage_schedule_but_can_book(client, world):
    client.force_login(world["reception"])
    assert client.get(reverse("doctors:schedule")).status_code == 403
    assert client.get(reverse("scheduling:booking_panel")).status_code == 200


def test_viewer_cannot_book(client, world, make_member):
    viewer = make_member(world["org"], "viewer")
    client.force_login(viewer)
    assert client.get(reverse("scheduling:booking_panel")).status_code == 403
    board = client.get(reverse("reception:today"))
    assert board.status_code == 200  # the schedule, view-only
    assert reverse("scheduling:booking_panel") not in board.content.decode()


@pytest.mark.parametrize(
    ("name", "args", "query", "expected"),
    [
        ("scheduling:booking_home", [], {}, "?book=1"),
        ("scheduling:appointments_day", [], {"date": "2026-01-03"}, "?date=2026-01-03&view=day"),
        ("scheduling:appointments_week", [], {"date": "2026-01-03"}, "?date=2026-01-03&view=week"),
    ],
)
def test_old_screens_open_the_board(client, world, name, args, query, expected):
    client.force_login(world["reception"])
    r = client.get(reverse(name, args=args), query)
    assert r.status_code == 302 and r.url == reverse("reception:today") + expected


def test_old_booking_link_opens_the_panel_for_that_patient(client, world):
    client.force_login(world["reception"])
    r = client.get(reverse("scheduling:booking_patient", args=[world["patient"].pk]), {"day": SATURDAY.isoformat()})
    assert r.url == f"{reverse('reception:today')}?date={SATURDAY}&book={world['patient'].pk}"
    page = client.get(r.url).content.decode()
    assert f"{reverse('scheduling:booking_panel')}?day={SATURDAY}&patient={world['patient'].pk}" in page


def test_cross_tenant_appointment_isolation(client, world, org_b, make_member):
    with freeze_time("2026-01-01 06:00:00"):
        appt = services.book(
            patient=world["patient"], doctor=world["doctor"], visit_type=world["vt"], by=world["owner"],
            start_at=local_dt(SATURDAY, time(14, 0)),
        )  # fmt: skip
    client.force_login(make_member(org_b, "owner"))
    assert client.post(reverse("scheduling:appointment_cancel", args=[appt.pk])).status_code == 404
