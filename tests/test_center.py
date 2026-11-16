"""Center mode (plan 14 §14.3): several doctors in the interface, the services catalog with services billed on a
visit, and emergency arrivals."""

import io
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from apps.accounts.permissions import ROLE_PERMISSIONS
from apps.billing import services as billing
from apps.center import services as center
from apps.center.models import (
    CenterSettings,
    Disposition,
    EmergencyCase,
    EmergencyReason,
    OrderStatus,
    Service,
    ServiceOrder,
    Triage,
)
from apps.clinical import services as clinical
from apps.core.timeutils import local_dt
from apps.doctors import services as doctor_services
from apps.doctors.models import BookingMode, Doctor, ScheduleException, VisitType, WorkingPeriod
from apps.notifications.models import ScheduledMessage
from apps.organizations.models import ClinicRole, Role
from apps.patients import services as patient_services
from apps.patients.models import Gender, Patient
from apps.scheduling import services as booking
from apps.scheduling.models import Appointment, AppointmentStatus, BookingSource

SATURDAY = date(2026, 1, 3)
NOW = "2026-01-03 12:00:00+02:00"  # Saturday noon: Mona's queue (10-2) is running, Sara's slots (2-5) come later
NIGHT = "2026-01-03 23:30:00+02:00"  # nobody works


def _patient(org, n, name="منى سعيد"):
    patient = Patient(full_name=f"{name} {n}", phone=f"+2010012345{n:02d}", gender=Gender.FEMALE)
    return patient_services.create_patient(org, patient)  # file numbers 1, 2, 3... from the clinic's sequence


@pytest.fixture
def world(org_a, org_b, make_member):
    sara = Doctor.objects.create(
        organization=org_a, name_ar="سارة علي", title_ar="د.", booking_mode=BookingMode.SLOTS, slot_minutes=30
    )
    mona = Doctor.objects.create(
        organization=org_a, name_ar="منى حسن", title_ar="د.", booking_mode=BookingMode.QUEUE, queue_avg_minutes=15
    )
    WorkingPeriod.objects.create(organization=org_a, doctor=sara, weekday=0, start_time="14:00", end_time="17:00")
    WorkingPeriod.objects.create(
        organization=org_a, doctor=mona, weekday=0, start_time="10:00", end_time="14:00", max_patients=5
    )
    vt_sara = VisitType.objects.create(
        organization=org_a, doctor=sara, name_ar="كشف", duration_minutes=30, price=Decimal("400")
    )
    vt_mona = VisitType.objects.create(
        organization=org_a, doctor=mona, name_ar="كشف سونار", duration_minutes=15, price=Decimal("300")
    )
    ultrasound = Service.objects.create(
        organization=org_a, name_ar="سونار", category="ultrasound", price=Decimal("250"), doctor_share_percent=40
    )
    settings = CenterSettings.for_org(org_a)
    settings.emergency_fee = Decimal("600")
    settings.save()
    doctor_user = make_member(org_a, "doctor")
    mona.user = doctor_user
    mona.save()
    other_doctor = Doctor.objects.create(organization=org_b, name_ar="طبيب تاني")
    return {
        "org": org_a,
        "sara": sara,
        "mona": mona,
        "vt_sara": vt_sara,
        "vt_mona": vt_mona,
        "ultrasound": ultrasound,
        "patients": [_patient(org_a, n) for n in range(1, 6)],
        "owner": make_member(org_a, "owner"),
        "reception": make_member(org_a, "reception"),
        "doctor_user": doctor_user,
        "viewer": make_member(org_a, "viewer"),
        "other_owner": make_member(org_b, "owner"),
        "other_doctor": other_doctor,
        "other_service": Service.objects.create(organization=org_b, name_ar="خدمة تانية", price=Decimal("10")),
    }


def _book_sara(world, patient, hour=15, minute=0):
    return booking.book(
        patient=patient, doctor=world["sara"], visit_type=world["vt_sara"], by=world["reception"],
        start_at=local_dt(SATURDAY, time(hour, minute)),
    )  # fmt: skip


def _book_mona(world, patient):
    return booking.book(
        patient=patient, doctor=world["mona"], visit_type=world["vt_mona"], by=world["reception"], day=SATURDAY
    )


def _emergency(world, patient, doctor=None, triage=Triage.URGENT, reason=EmergencyReason.BLEEDING):
    return center.register_emergency(
        org=world["org"], patient=patient, doctor=doctor or world["sara"], triage=triage, reason=reason,
        by=world["reception"],
    )  # fmt: skip


# ================================================================ several doctors


def test_roles_carry_the_center_permissions(org_a):
    for role in (Role.RECEPTION, Role.DOCTOR, Role.OWNER):
        assert {"emergency.register", "service.order"} <= ROLE_PERMISSIONS[role]
    for role in (Role.VIEWER, Role.ADMIN):
        assert not {"emergency.register", "service.order"} & ROLE_PERMISSIONS[role]
    from apps.accounts.roles import ensure_defaults

    ensure_defaults(org_a)
    reception = ClinicRole.objects.get(organization=org_a, key="reception")
    assert {"emergency.register", "service.order"} <= set(reception.permissions)


def test_board_shows_every_doctor_or_one(client, world):
    p1, p2 = world["patients"][:2]
    with freeze_time(NOW):
        _book_sara(world, p1)
        _book_mona(world, p2)
        client.force_login(world["reception"])
        resp = client.get(reverse("reception:today"), {"view": "day", "date": SATURDAY.isoformat()})
        html = resp.content.decode()
        assert [b["doctor"] for b in resp.context["boards"]] == [world["sara"], world["mona"]]
        assert resp.context["selected_doctor"] is None
        assert p1.full_name in html and p2.full_name in html
        assert "كل الدكاترة" in html and "كل مواعيده" in html

        resp = client.get(reverse("reception:today"), {"view": "day", "doctor": world["mona"].pk})
        html = resp.content.decode()
        assert [b["doctor"] for b in resp.context["boards"]] == [world["mona"]]
        assert p2.full_name in html and p1.full_name not in html

        # remembered: the poll and the next visit to the board stay on that doctor
        assert client.get(reverse("reception:today")).context["selected_doctor"] == world["mona"]
        rows = client.get(reverse("reception:rows"), {"sig": "x"})
        assert rows.status_code == 200 and p1.full_name not in rows.content.decode()
        assert client.get(reverse("reception:rows"), {"sig": rows.context["sig"]}).status_code == 204

        resp = client.get(reverse("reception:today"), {"doctor": "all", "view": "week"})
        assert len(resp.context["boards"]) == 2 and resp.status_code == 200


def test_single_doctor_clinic_has_no_picker(client, org_a, make_member):
    Doctor.objects.create(organization=org_a, name_ar="وحيد")
    client.force_login(make_member(org_a, "reception"))
    resp = client.get(reverse("reception:today"))
    assert len(resp.context["boards"]) == 1 and resp.context["selected_doctor"] is not None
    assert "doctor=all" not in resp.content.decode()


def test_booking_panel_asks_which_doctor_and_books_with_her(client, world):
    patient = world["patients"][0]
    client.force_login(world["reception"])
    with freeze_time(NOW):
        url = reverse("scheduling:booking_panel")
        html = client.get(url, {"patient": patient.pk, "doctor": world["sara"].pk, "day": SATURDAY.isoformat()})
        text = html.content.decode()
        assert str(world["sara"]) in text and str(world["mona"]) in text  # the doctor chips
        assert html.context["visit_types"] == [world["vt_sara"]] and html.context["slots_mode"] is True

        other = client.get(url, {"patient": patient.pk, "doctor": world["mona"].pk, "day": SATURDAY.isoformat()})
        assert other.context["visit_types"] == [world["vt_mona"]] and other.context["slots_mode"] is False

        # the panel was last opened on Mona, but the visit type posted is Sara's: the booking is Sara's
        resp = client.post(
            reverse("scheduling:booking_confirm", args=[patient.pk]),
            {"visit_type": world["vt_sara"].pk, "start_at": local_dt(SATURDAY, time(15, 0)).isoformat()},
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 200
    appt = Appointment.objects.get(patient=patient)
    assert appt.doctor == world["sara"] and appt.price == Decimal("400")


def test_walk_in_goes_to_the_visit_types_doctor(client, world):
    patient = world["patients"][0]
    client.force_login(world["reception"])
    with freeze_time(NOW):
        client.post(reverse("reception:walk_in"), {"patient": patient.pk, "visit_type": world["vt_mona"].pk})
    appt = Appointment.objects.get(patient=patient)
    assert appt.doctor == world["mona"] and appt.status == AppointmentStatus.ARRIVED and appt.queue_number == 1


def test_doctor_login_opens_her_own_queue(client, world):
    p1, p2 = world["patients"][:2]
    with freeze_time(NOW):
        booking.transition(_book_sara(world, p1), AppointmentStatus.ARRIVED, by=world["reception"])
        booking.transition(_book_mona(world, p2), AppointmentStatus.ARRIVED, by=world["reception"])
        client.force_login(world["doctor_user"])  # linked to Mona
        resp = client.get(reverse("clinical:desk"))
        assert resp.context["doctor"] == world["mona"]
        assert [a.patient for a in resp.context["q_waiting"]] == [p2]
        resp = client.get(reverse("clinical:desk"), {"doctor": world["sara"].pk})  # covering for a colleague
        assert [a.patient for a in resp.context["q_waiting"]] == [p1]

        client.force_login(world["owner"])  # no doctor record: the first doctor, and the picker
        resp = client.get(reverse("clinical:desk"))
        assert resp.context["doctor"] == world["sara"]
        assert f"?doctor={world['mona'].pk}" in resp.content.decode()


def test_settings_add_a_doctor_with_the_same_visit_types(client, world):
    client.force_login(world["owner"])
    page = client.get(reverse("doctors:list"))
    assert page.status_code == 200 and "منى حسن" in page.content.decode()
    resp = client.post(
        reverse("doctors:list"),
        {
            "title_ar": "د.",
            "name_ar": "هالة فؤاد",
            "specialty_ar": "نسا",
            "booking_mode": "slots",
            "copy_visit_types": "1",
        },
    )
    hala = Doctor.objects.get(organization=world["org"], name_ar="هالة فؤاد")
    assert resp.status_code == 302 and resp.url == f"{reverse('doctors:schedule')}?doctor={hala.pk}"
    assert [(v.name_ar, v.price) for v in VisitType.objects.filter(doctor=hala)] == [("كشف", Decimal("400"))]

    # her own schedule: the form names the doctor, whatever doctor the session last looked at
    client.get(reverse("doctors:schedule"), {"doctor": world["sara"].pk})
    client.post(
        f"{reverse('doctors:period_add')}?doctor={hala.pk}", {"weekday": 2, "start_time": "09:00", "end_time": "12:00"}
    )
    assert WorkingPeriod.objects.filter(doctor=hala).count() == 1
    assert WorkingPeriod.objects.filter(doctor=world["sara"]).count() == 1
    client.post(
        f"{reverse('doctors:visit_type_add')}?doctor={hala.pk}",
        {"name_ar": "استشارة", "duration_minutes": 10, "price": "150", "color": "#112233", "is_active": "on",
         "free_followup_days": 0},
    )  # fmt: skip
    assert VisitType.objects.filter(doctor=hala, name_ar="استشارة").exists()
    assert not VisitType.objects.filter(doctor=world["sara"], name_ar="استشارة").exists()


def test_doctor_is_switched_off_not_deleted(client, world):
    client.force_login(world["owner"])
    with freeze_time(NOW):
        appt = _book_sara(world, world["patients"][0])
        client.post(reverse("doctors:toggle", args=[world["sara"].pk]))
        world["sara"].refresh_from_db()
        assert world["sara"].is_active  # she still has a booking ahead
        booking.cancel(appt, by=world["owner"])
        client.post(reverse("doctors:toggle", args=[world["sara"].pk]))
        world["sara"].refresh_from_db()
        assert not world["sara"].is_active and Doctor.objects.filter(pk=world["sara"].pk).exists()

        # gone from the board and from booking, still editable in settings
        resp = client.get(reverse("reception:today"))
        assert [b["doctor"] for b in resp.context["boards"]] == [world["mona"]]
        panel = client.get(reverse("scheduling:booking_panel"), {"patient": world["patients"][1].pk})
        assert panel.context["doctor"] == world["mona"]
        gone = client.post(
            reverse("scheduling:booking_confirm", args=[world["patients"][1].pk]),
            {"visit_type": world["vt_sara"].pk, "start_at": local_dt(SATURDAY, time(15, 0)).isoformat()},
        )
        assert gone.status_code == 404
        assert client.get(reverse("doctors:schedule"), {"doctor": world["sara"].pk}).context["doctor"] == world["sara"]

        # the last active doctor can't be switched off
        client.post(reverse("doctors:toggle", args=[world["mona"].pk]))
        world["mona"].refresh_from_db()
        assert world["mona"].is_active
        client.post(reverse("doctors:toggle", args=[world["sara"].pk]))
        world["sara"].refresh_from_db()
        assert world["sara"].is_active


def test_used_visit_type_is_switched_off_instead_of_deleted(client, world):
    client.force_login(world["owner"])
    with freeze_time(NOW):
        _book_sara(world, world["patients"][0])
    client.post(reverse("doctors:visit_type_delete", args=[world["vt_sara"].pk]))
    world["vt_sara"].refresh_from_db()
    assert world["vt_sara"].is_active is False
    client.post(reverse("doctors:visit_type_delete", args=[world["vt_mona"].pk]))
    assert not VisitType.objects.filter(pk=world["vt_mona"].pk).exists()


def test_doctors_of_another_clinic_are_out_of_reach(client, world):
    client.force_login(world["owner"])
    other = world["other_doctor"]
    assert client.post(reverse("doctors:toggle", args=[other.pk])).status_code == 404
    # a foreign pk in ?doctor= is ignored: the page stays on this clinic's doctor
    assert client.get(reverse("doctors:schedule"), {"doctor": other.pk}).context["doctor"].organization == world["org"]
    assert client.get(reverse("reception:today"), {"doctor": other.pk}).context["selected_doctor"] is None
    assert doctor_services.active_doctors(world["org"]) == [world["sara"], world["mona"]]
    client.force_login(world["reception"])
    assert client.get(reverse("doctors:list")).status_code == 403
    assert client.post(reverse("doctors:toggle", args=[world["sara"].pk])).status_code == 403


def test_closing_the_day_for_everyone_or_one_doctor(client, world):
    client.force_login(world["owner"])
    with freeze_time(NOW):
        _book_mona(world, world["patients"][0])
        resp = client.post(reverse("scheduling:close_day"), {"date": "2026-01-10", "doctor": world["sara"].pk})
        assert resp.status_code == 302
        assert ScheduleException.objects.filter(date=date(2026, 1, 10)).count() == 1
        resp = client.post(reverse("scheduling:close_day"), {"date": SATURDAY.isoformat(), "doctor": "all"})
        assert ScheduleException.objects.filter(date=SATURDAY).count() == 2
        assert resp.url == reverse("scheduling:affected") + f"?date={SATURDAY}"
        page = client.get(resp.url)
        assert page.context["several"] and len(page.context["affected"]) == 1


# ================================================================ services


def test_services_catalog_settings(client, world):
    client.force_login(world["owner"])
    page = client.get(reverse("center:services"))
    assert page.status_code == 200 and "سونار" in page.content.decode()
    data = {"name_ar": "تركيب لولب", "name_en": "IUD", "category": "procedure", "price": "700",
            "doctor_share_percent": "50", "is_active": "on"}  # fmt: skip
    assert client.post(reverse("center:services"), data).status_code == 302
    iud = Service.objects.get(organization=world["org"], name_ar="تركيب لولب")
    assert iud.price == Decimal("700") and iud.doctor_share_percent == Decimal("50")
    again = client.post(reverse("center:services"), data)
    assert again.status_code == 200 and "فيه خدمة بالاسم ده" in again.content.decode()

    assert client.post(reverse("center:service_edit", args=[iud.pk]), {**data, "price": "800"}).status_code == 302
    iud.refresh_from_db()
    assert iud.price == Decimal("800")
    client.post(reverse("center:service_toggle", args=[iud.pk]))
    iud.refresh_from_db()
    assert not iud.is_active and iud.history.count() >= 3

    assert client.post(reverse("center:services"), {"action": "fee", "emergency_fee": "750"}).status_code == 302
    assert CenterSettings.for_org(world["org"]).emergency_fee == Decimal("750")

    # another clinic's service, and people who don't manage settings
    assert client.get(reverse("center:service_edit", args=[world["other_service"].pk])).status_code == 404
    assert client.post(reverse("center:service_toggle", args=[world["other_service"].pk])).status_code == 404
    client.force_login(world["reception"])
    assert client.get(reverse("center:services")).status_code == 403
    assert client.post(reverse("center:service_toggle", args=[iud.pk])).status_code == 403


def test_a_service_is_billed_with_the_visit(client, world):
    patient = world["patients"][0]
    with freeze_time(NOW):
        appt = _book_sara(world, patient)
        assert billing.balance(appt).due == Decimal("400")
        client.force_login(world["reception"])
        modal = client.get(reverse("center:orders", args=[appt.pk]))
        assert modal.status_code == 200 and "سونار" in modal.content.decode()
        resp = client.post(reverse("center:order_add", args=[appt.pk]), {"service": world["ultrasound"].pk})
        assert resp.status_code == 200 and "orders-changed" in resp["HX-Trigger"]
        order = ServiceOrder.objects.get(appointment=appt)
        assert (order.name, order.unit_price, order.doctor, order.doctor_share) == (
            "سونار", Decimal("250"), world["sara"], Decimal("100.00"),
        )  # fmt: skip
        bal = billing.balance(appt)
        assert (bal.due, bal.services, bal.remaining, bal.status) == (
            Decimal("650"),
            Decimal("250"),
            Decimal("650"),
            "unpaid",
        )

        # the board shows the new amount and the service under the visit type
        board = client.get(reverse("reception:today"), {"doctor": world["sara"].pk, "date": SATURDAY.isoformat()})
        entry = next(e for e in board.context["board"]["entries"] if e.a.pk == appt.pk)
        assert entry.balance.due == Decimal("650") and [o.name for o in entry.services] == ["سونار"]
        assert "+ سونار" in board.content.decode()

        # a later price change in the catalog never rewrites what was ordered
        world["ultrasound"].price = Decimal("999")
        world["ultrasound"].save()
        assert billing.balance(appt).due == Decimal("650")

        # paid in full: the service can't be taken off before the money goes back
        billing.record_payment(appt, amount=650, by=world["reception"])
        resp = client.post(reverse("center:order_cancel", args=[order.pk]))
        assert "اتدفع تمنها" in resp.content.decode()
        order.refresh_from_db()
        assert order.is_active
        billing.record_payment(appt, amount=250, by=world["reception"], is_refund=True)
        client.post(reverse("center:order_cancel", args=[order.pk]))
        order.refresh_from_db()
        assert order.status == OrderStatus.CANCELLED and order.cancelled_by == world["reception"]
        assert ServiceOrder.objects.filter(pk=order.pk).exists()  # kept, never deleted
        bal = billing.balance(appt)
        assert (bal.due, bal.paid, bal.status) == (Decimal("400"), Decimal("400"), "paid")


def test_service_order_rules(world):
    with freeze_time(NOW):
        appt = _book_sara(world, world["patients"][0])
        with pytest.raises(center.CenterError):
            center.order_service(appt, world["other_service"], by=world["reception"])  # another clinic's
        with pytest.raises(center.CenterError):
            center.order_service(appt, world["ultrasound"], by=world["reception"], quantity=0)
        order = center.order_service(appt, world["ultrasound"], by=world["reception"], quantity=3)
        assert order.total == Decimal("750.00") and billing.balance(appt).due == Decimal("1150")
        world["ultrasound"].is_active = False
        world["ultrasound"].save()
        with pytest.raises(center.CenterError):
            center.order_service(appt, world["ultrasound"], by=world["reception"])
        world["ultrasound"].is_active = True
        world["ultrasound"].save()
        booking.cancel(appt, by=world["reception"])
        with pytest.raises(center.CenterError):
            center.order_service(appt, world["ultrasound"], by=world["reception"])


def test_service_orders_permissions_and_tenants(client, world):
    with freeze_time(NOW):
        appt = _book_sara(world, world["patients"][0])
        order = center.order_service(appt, world["ultrasound"], by=world["reception"])
        client.force_login(world["viewer"])  # sees the board, can't touch services
        assert client.get(reverse("center:orders", args=[appt.pk])).status_code == 403
        assert (
            client.post(reverse("center:order_add", args=[appt.pk]), {"service": world["ultrasound"].pk}).status_code
            == 403
        )
        assert "خدمات على الزيارة" not in client.get(reverse("reception:today")).content.decode()

        client.force_login(world["other_owner"])
        assert client.get(reverse("center:orders", args=[appt.pk])).status_code == 404
        assert client.post(reverse("center:order_cancel", args=[order.pk])).status_code == 404

        client.force_login(world["reception"])  # this clinic's visit, another clinic's service
        resp = client.post(reverse("center:order_add", args=[appt.pk]), {"service": world["other_service"].pk})
        assert resp.status_code == 404
    assert ServiceOrder.objects.filter(appointment=appt).count() == 1


def test_doctor_adds_a_service_from_the_visit_page(client, world):
    with freeze_time(NOW):
        appt = _book_mona(world, world["patients"][0])
        booking.transition(appt, AppointmentStatus.ARRIVED, by=world["reception"])
        visit = clinical.start_visit(appt, by=world["doctor_user"])
        client.force_login(world["doctor_user"])
        page = client.get(reverse("clinical:visit", args=[visit.pk])).content.decode()
        assert f"{reverse('center:orders', args=[appt.pk])}?inline=1" in page
        inline = client.get(reverse("center:orders", args=[appt.pk]), {"inline": "1"}).content.decode()
        assert 'id="orders-box"' in inline and 'class="overlay"' not in inline
        client.post(reverse("center:order_add", args=[appt.pk]), {"service": world["ultrasound"].pk})
    order = ServiceOrder.objects.get(appointment=appt)
    assert order.doctor == world["mona"] and order.ordered_by == world["doctor_user"]


# ================================================================ emergency arrivals


def test_emergency_with_only_a_name_and_a_phone(client, world, django_capture_on_commit_callbacks):
    client.force_login(world["reception"])
    with freeze_time(NIGHT):  # outside every doctor's hours
        form = client.get(reverse("center:emergency_register"))
        assert form.status_code == 200 and "تسجيل طوارئ" in form.content.decode()
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(
                reverse("center:emergency_register"),
                {"full_name": "هدى كمال", "phone": "01055512345", "gender": "female", "triage": "critical",
                 "reason": "bleeding", "reason_text": "حامل في السابع", "doctor": world["sara"].pk},
            )  # fmt: skip
        assert resp.status_code == 200 and "board-refresh" in resp["HX-Trigger"]
        patient = Patient.objects.get(organization=world["org"], full_name="هدى كمال")
        assert patient.phone == "+201055512345" and patient.file_number == 6
        case = EmergencyCase.objects.get(patient=patient)
        appt = case.appointment
        assert (case.triage, case.reason, case.doctor) == (Triage.CRITICAL, EmergencyReason.BLEEDING, world["sara"])
        assert case.reason_label == "نزيف: حامل في السابع" and case.registered_by == world["reception"]
        assert (appt.source, appt.status, appt.price) == (
            BookingSource.EMERGENCY,
            AppointmentStatus.ARRIVED,
            Decimal("600"),
        )
        assert appt.start_at == timezone.now() and appt.arrived_at is not None and appt.queue_number is None
        assert appt.visit_type.is_system and not appt.visit_type.is_active
        assert not ScheduledMessage.objects.filter(appointment=appt).exists()  # she is here: nothing to send

        # the same patient can't be registered twice while she is still in
        again = client.post(
            reverse("center:emergency_register"),
            {"patient": patient.pk, "triage": "urgent", "reason": "pain", "doctor": world["sara"].pk},
        )
        assert "متسجلة طوارئ دلوقتي" in again.content.decode()
        assert EmergencyCase.objects.count() == 1


def test_emergency_form_needs_who_and_which_doctor(client, world):
    client.force_login(world["reception"])
    resp = client.post(reverse("center:emergency_register"), {"triage": "urgent", "reason": "pain"})
    html = resp.content.decode()
    assert "اكتب الاسم" in html and "اكتب رقم موبايل" in html and "اختار الدكتور" in html
    resp = client.post(
        reverse("center:emergency_register"),
        {"full_name": "س", "phone": "123", "gender": "female", "triage": "urgent", "reason": "pain",
         "doctor": world["other_doctor"].pk},
    )  # fmt: skip
    assert "مش صحيح" in resp.content.decode() and not EmergencyCase.objects.exists()
    found = client.get(reverse("center:emergency_search"), {"q": "منى سعيد"}).content.decode()
    assert world["patients"][0].full_name in found
    assert client.get(reverse("center:emergency_search"), {"q": "من"}).content.decode().strip() == ""


def test_emergency_jumps_the_queue(client, world):
    p_slot, p_queue, p_em1, p_em2 = world["patients"][:4]
    with freeze_time(NOW) as clock:
        booked = _book_mona(world, p_queue)
        booking.transition(booked, AppointmentStatus.ARRIVED, by=world["reception"])
        clock.tick(timedelta(minutes=20))
        urgent = _emergency(world, p_em1, doctor=world["mona"])  # queue mode, arrived after number 1
        clock.tick(timedelta(minutes=5))
        critical = _emergency(world, p_em2, doctor=world["mona"], triage=Triage.CRITICAL)
        _current, waiting, _n = clinical.today_queue(world["mona"])
        assert [a.patient for a in waiting][-1] == p_queue  # both emergencies are ahead of the booked number
        assert {a.patient for a in waiting[:2]} == {p_em1, p_em2}
        assert [c.patient for c in center.active_cases(world["org"], SATURDAY)] == [p_em2, p_em1]  # critical first

        client.force_login(world["reception"])
        _book_sara(world, p_slot)
        board = client.get(reverse("reception:today"), {"doctor": world["sara"].pk})  # another doctor's board
        html = board.content.decode()
        assert [c.pk for c in board.context["emergencies"]] == [critical.pk, urgent.pk]
        assert "حالات الطوارئ" in html and p_em2.full_name in html  # still on top: the whole center sees it
        mona_board = client.get(reverse("reception:today"), {"doctor": world["mona"].pk}).context["board"]
        assert [e.a.patient for e in mona_board["queue_outside"]] == [p_em1, p_em2]
        assert all(e.emergency for e in mona_board["queue_outside"])
        assert mona_board["counts"]["arrived"] == 3


def test_emergency_times_seen_and_disposition(client, world):
    patient = world["patients"][0]
    with freeze_time(NOW) as clock:
        case = _emergency(world, patient)
        clock.tick(timedelta(minutes=7))
        client.force_login(world["doctor_user"])
        alert = client.get(reverse("clinical:emergency_alert"))
        assert alert.context["alerts"] == []  # she is Sara's patient, this desk is Mona's
        alert = client.get(reverse("clinical:emergency_alert"), {"doctor": world["sara"].pk})
        assert [c.pk for c in alert.context["alerts"]] == [case.pk] and alert.context["alerts"][0].waiting == 7
        assert "طوارئ مستنية" in alert.content.decode()

        visit = clinical.start_visit(case.appointment, by=world["doctor_user"])
        case.refresh_from_db()
        assert case.minutes_to_seen == 7
        clock.tick(timedelta(minutes=30))
        clinical.start_visit(case.appointment, by=world["doctor_user"])  # reopening never moves the time
        case.refresh_from_db()
        assert case.minutes_to_seen == 7
        assert client.get(reverse("clinical:emergency_alert")).context["alerts"] == []

        page = client.get(reverse("clinical:visit", args=[visit.pk]))
        html = page.content.decode()
        assert page.context["emergency"] == case and 'id="emergency-band"' in html and "دخلت بعد 7 دقيقة" in html
        # the band re-reads itself with outerHTML; its button must not inherit that and replace the dialog container
        assert (
            f'hx-get="{reverse("center:emergency_disposition", args=[case.pk])}" hx-target="#modal" hx-swap="innerHTML"'
            in html
        )
        assert 'id="modal"' in html

        modal = client.get(reverse("center:emergency_disposition", args=[case.pk]))
        assert modal.status_code == 200 and "دخول عمليات" in modal.content.decode()
        resp = client.post(
            reverse("center:emergency_disposition", args=[case.pk]), {"disposition": "theatre", "notes": "قيصرية عاجلة"}
        )
        assert resp.status_code == 200 and "emergency-changed" in resp["HX-Trigger"]
        case.refresh_from_db()
        assert case.disposition == Disposition.THEATRE and case.disposition_by == world["doctor_user"]
        assert case.disposition_at == timezone.now() and case.history.count() >= 2
        band = client.get(reverse("center:emergency_band", args=[case.pk])).content.decode()
        assert "دخول عمليات" in band and "قيصرية عاجلة" in band

        log = client.get(reverse("center:emergencies"), {"date": SATURDAY.isoformat()})
        assert log.status_code == 200 and log.context["cases"] == [case]
        assert (log.context["avg_wait"], log.context["undecided"]) == (7, 0)
        assert client.get(reverse("center:emergencies"), {"date": "2026-01-02"}).context["cases"] == []


def test_reception_call_also_marks_seen(client, world):
    with freeze_time(NOW) as clock:
        case = _emergency(world, world["patients"][0])
        clock.tick(timedelta(minutes=3))
        client.force_login(world["reception"])
        client.post(reverse("reception:step", args=[case.appointment_id, "call"]))
    case.refresh_from_db()
    assert case.minutes_to_seen == 3


def test_emergency_registered_by_mistake(client, world):
    client.force_login(world["reception"])
    with freeze_time(NOW):
        case = _emergency(world, world["patients"][0])
        # "back a step" would turn it into an ordinary booking that the no-show job then chases: refused
        resp = client.post(reverse("reception:step", args=[case.appointment_id, "undo"]))
        assert "طوارئ" in resp["HX-Trigger"].encode().decode("unicode_escape")
        case.appointment.refresh_from_db()
        assert case.appointment.status == AppointmentStatus.ARRIVED
        assert booking.due_no_shows() == []
        assert booking.affected_by_closing(world["sara"], SATURDAY) == []  # it never had a slot

        resp = client.post(reverse("center:emergency_cancel", args=[case.pk]))
        assert resp.status_code == 204 and "board-refresh" in resp["HX-Trigger"]
        case.appointment.refresh_from_db()
        assert case.appointment.status == AppointmentStatus.CANCELLED
        assert center.active_cases(world["org"], SATURDAY) == [] and EmergencyCase.objects.filter(pk=case.pk).exists()

        # once the doctor took her in it can't be cancelled any more
        seen = _emergency(world, world["patients"][1])
        clinical.start_visit(seen.appointment, by=world["doctor_user"])
        resp = client.post(reverse("center:emergency_cancel", args=[seen.pk]))
        assert "board-refresh" not in resp["HX-Trigger"]
        seen.appointment.refresh_from_db()
        assert seen.appointment.status == AppointmentStatus.IN_CONSULTATION


def test_emergency_permissions_and_tenants(client, world):
    with freeze_time(NOW):
        case = _emergency(world, world["patients"][0])
        client.force_login(world["viewer"])
        for name, args in [("emergency_register", []), ("emergencies", []), ("emergency_search", []),
                           ("emergency_disposition", [case.pk]), ("emergency_band", [case.pk])]:  # fmt: skip
            assert client.get(reverse(f"center:{name}", args=args)).status_code == 403, name
        assert client.post(reverse("center:emergency_cancel", args=[case.pk])).status_code == 403
        board = client.get(reverse("reception:today")).content.decode()
        assert "حالات الطوارئ" in board  # the viewer sees who is in, without the reason or any of the buttons
        assert case.get_reason_display() not in board
        assert reverse("center:emergency_register") not in board
        assert reverse("center:emergency_disposition", args=[case.pk]) not in board

        client.force_login(world["other_owner"])
        assert client.get(reverse("center:emergency_disposition", args=[case.pk])).status_code == 404
        assert client.get(reverse("center:emergency_band", args=[case.pk])).status_code == 404
        assert client.post(reverse("center:emergency_cancel", args=[case.pk])).status_code == 404
        assert client.get(reverse("center:emergencies")).context["cases"] == []
        assert "حالات الطوارئ" not in client.get(reverse("reception:today")).content.decode()
        # a patient of another clinic can't be registered here
        resp = client.post(
            reverse("center:emergency_register"),
            {
                "patient": world["patients"][1].pk,
                "triage": "urgent",
                "reason": "pain",
                "doctor": world["other_doctor"].pk,
            },
        )
        assert "مش موجود" in resp.content.decode()
    assert EmergencyCase.objects.count() == 1


def test_emergency_visit_type_never_shows_in_booking_or_settings(client, world):
    with freeze_time(NOW):
        _emergency(world, world["patients"][0])
        client.force_login(world["owner"])
        panel = client.get(
            reverse("scheduling:booking_panel"), {"patient": world["patients"][1].pk, "doctor": world["sara"].pk}
        )
        assert panel.context["visit_types"] == [world["vt_sara"]]
        types = client.get(reverse("doctors:visit_types"), {"doctor": world["sara"].pk})
        assert list(types.context["visit_types"]) == [world["vt_sara"]]
        system = VisitType.objects.get(doctor=world["sara"], is_system=True)
        assert client.post(reverse("doctors:visit_type_delete", args=[system.pk])).status_code == 404


# ================================================================ seeds


def test_seed_services_and_the_simulated_day(db):
    from apps.organizations.models import Organization

    out = io.StringIO()
    call_command("seed_org", stdout=out)
    org = Organization.objects.get(slug="demo-clinic")
    mine = Service.objects.filter(organization=org)
    call_command("seed_services", stdout=out)
    assert mine.count() == 13 and CenterSettings.objects.get(organization=org).emergency_fee == Decimal("600")
    mine.filter(name_ar="دوبلر").update(price=1)
    call_command("seed_services", stdout=out)  # idempotent: the clinic's own edits stay
    assert mine.count() == 13 and mine.get(name_ar="دوبلر").price == 1

    with freeze_time("2026-10-05 12:00:00+03:00", tick=True):
        call_command("seed_demo", stdout=out)
        call_command("simulate_day", stdout=out)
        case = EmergencyCase.objects.get(organization=org)
        assert case.appointment.status == AppointmentStatus.ARRIVED and case.appointment.price == Decimal("600")
        assert ServiceOrder.objects.filter(organization=org).count() == 1
        assert not ScheduledMessage.objects.filter(appointment=case.appointment).exists()
        call_command("simulate_day", stdout=out)  # a day is never simulated twice
        assert EmergencyCase.objects.filter(organization=org).count() == 1
        call_command("simulate_day", remove=True, stdout=out)
    assert not EmergencyCase.objects.filter(organization=org).exists()
    assert not ServiceOrder.objects.filter(organization=org).exists()
