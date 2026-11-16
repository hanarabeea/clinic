"""Operating room and admissions (plan 14 §14.4): no two operations in one room at once, the step-by-step flow with
its pre-op checklist, deliveries that close the pregnancy file, signed notes, admissions, and the messages."""

import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from apps.center import services as center
from apps.core.timeutils import CAIRO
from apps.doctors.models import Doctor
from apps.notifications.management.commands.seed_notifications import seed_org_notifications
from apps.notifications.models import Event, MessageStatus, NotificationTemplate, ScheduledMessage
from apps.obstetrics import services as obstetrics
from apps.obstetrics.models import Outcome, PregnancyHistory, PregnancyStatus
from apps.organizations.models import Specialty
from apps.patients import services as patient_services
from apps.patients.models import Gender, Patient
from apps.theatre import services as theatre
from apps.theatre.models import (
    Admission,
    AdmissionStatus,
    ChecklistItem,
    DeliveryMode,
    Newborn,
    NewbornStatus,
    NoteStatus,
    Operation,
    OperationStatus,
    ProcedureType,
    Room,
    RoomKind,
    TeamRole,
)
from apps.theatre.services import TheatreError

D = Decimal
NOW = "2026-03-10 09:00:00+02:00"
TODAY = date(2026, 3, 10)


def at(hour, minute=0, day=TODAY):
    return datetime.combine(day, time(hour, minute), tzinfo=CAIRO)


@pytest.fixture
def clock():
    with freeze_time(NOW, tick=True):
        yield


@pytest.fixture
def world(org_a, org_b, make_member, clock):
    org_a.settings.specialty = Specialty.OBGYN
    org_a.settings.save()
    theatre.ensure_defaults(org_a)
    surgeon = Doctor.objects.create(organization=org_a, name_ar="سارة علي", title_ar="د.")
    patients = [
        patient_services.create_patient(
            org_a,
            Patient(full_name=f"مريضة {n}", phone=f"+2010022{n:05d}", gender=Gender.FEMALE, messaging_consent=True),
        )
        for n in range(1, 5)
    ]
    rooms = {r.kind: r for r in Room.objects.filter(organization=org_a).order_by("-id")}
    procs = {p.name_ar: p for p in ProcedureType.objects.filter(organization=org_a)}
    seed_org_notifications(org_a)
    return {
        "org": org_a,
        "surgeon": surgeon,
        "patients": patients,
        "theatre": rooms[RoomKind.OPERATING],
        "delivery_room": rooms[RoomKind.DELIVERY],
        "ward": Room.objects.filter(organization=org_a, kind=RoomKind.INPATIENT).order_by("id").first(),
        "cs": procs["ولادة قيصرية"],
        "dnc": procs["كحت وتنظيف"],
        "doctor_user": make_member(org_a, "doctor"),
        "reception": make_member(org_a, "reception"),
        "viewer": make_member(org_a, "viewer"),
        "other_owner": make_member(org_b, "owner"),
        "other_org": org_b,
    }


def _book(world, patient=None, hour=11, procedure=None, room=None, **kw):
    return theatre.book_operation(
        org=world["org"], patient=patient or world["patients"][0], procedure=procedure or world["dnc"],
        room=room or world["theatre"], surgeon=world["surgeon"], start=at(hour), by=world["doctor_user"], **kw,
    )  # fmt: skip


def _tick_all(op, user):
    for item in op.checklist.filter(done=False):
        theatre.toggle_checklist(item, by=user)


# ================================================================ booking and room clashes


def test_defaults_and_booking_copies_the_procedure(world):
    org = world["org"]
    assert (
        Room.objects.filter(organization=org).count() == 5
        and ProcedureType.objects.filter(organization=org).count() == 7
    )
    theatre.ensure_defaults(org)  # never twice
    assert Room.objects.filter(organization=org).count() == 5
    world["cs"].package_price = D("15000")
    world["cs"].save()
    op = _book(world, procedure=world["cs"], hour=11, notes="قيصرية سابقة")
    assert (op.status, op.date, op.planned_start, op.planned_end) == (OperationStatus.SCHEDULED, TODAY, at(11), at(12))
    assert (op.procedure_name, op.price, op.fasting_hours, op.notes) == ("ولادة قيصرية", D("15000"), 8, "قيصرية سابقة")
    assert op.checklist.count() == 6 and op.checklist.filter(required=True).count() == 5
    roles = {m.role: m for m in op.team.all()}
    assert set(roles) == {
        TeamRole.SURGEON,
        TeamRole.ASSISTANT,
        TeamRole.ANAESTHETIST,
        TeamRole.NURSE,
        TeamRole.PAEDIATRICIAN,
    }
    assert roles[TeamRole.SURGEON].doctor == world["surgeon"]
    assert [e.to_status for e in op.events.all()] == [OperationStatus.SCHEDULED]
    # a later change to the procedure never rewrites a booked operation
    world["cs"].package_price = D("20000")
    world["cs"].checklist = []
    world["cs"].save()
    op.refresh_from_db()
    assert op.price == D("15000") and op.checklist.count() == 6


def test_two_operations_never_overlap_in_one_room(world):
    p1, p2, p3, _p4 = world["patients"]
    first = _book(world, p1, hour=11, duration_minutes=90)  # 11:00 - 12:30
    for hour, minutes in [(11, 30), (12, 15), (10, 45)]:
        with pytest.raises(TheatreError, match="مشغولة"):
            theatre.book_operation(
                org=world["org"],
                patient=p2,
                procedure=world["dnc"],
                room=world["theatre"],
                surgeon=world["surgeon"],
                start=at(hour, minutes),
                by=world["doctor_user"],
            )
    back_to_back = theatre.book_operation(
        org=world["org"],
        patient=p2,
        procedure=world["dnc"],
        room=world["theatre"],
        surgeon=world["surgeon"],
        start=at(12, 30),
        by=world["doctor_user"],
    )
    other_room = _book(world, p3, hour=11, room=world["delivery_room"])  # same time, another room: fine
    with pytest.raises(TheatreError, match="عملية تانية في نفس الوقت"):
        _book(world, p1, hour=12, room=world["delivery_room"])  # the same patient can't be in two rooms
    assert Operation.objects.count() == 3 and back_to_back.planned_start == first.planned_end

    # a cancelled or finished operation frees its time
    theatre.transition(first, OperationStatus.CANCELLED, by=world["doctor_user"], notes="المريضة اعتذرت")
    _book(world, world["patients"][3], hour=11)
    assert other_room.status == OperationStatus.SCHEDULED


def test_booking_rules(world):
    p = world["patients"][0]
    base = {"org": world["org"], "patient": p, "procedure": world["dnc"], "room": world["theatre"],
            "surgeon": world["surgeon"], "by": world["doctor_user"]}  # fmt: skip
    with pytest.raises(TheatreError, match="فات"):
        theatre.book_operation(**base, start=at(7))
    theatre.book_operation(**base, start=at(8, 30), is_emergency=True)  # an emergency may be recorded as started
    with pytest.raises(TheatreError, match="غرفة عمليات"):
        theatre.book_operation(**{**base, "room": world["ward"]}, start=at(14))
    with pytest.raises(TheatreError, match="12 ساعة"):
        theatre.book_operation(**base, start=at(14), duration_minutes=5)
    world["dnc"].is_active = False
    world["dnc"].save()
    with pytest.raises(TheatreError, match="متوقف"):
        theatre.book_operation(**base, start=at(14))
    theatre.ensure_defaults(world["other_org"])
    foreign = Room.objects.filter(organization=world["other_org"], kind=RoomKind.OPERATING).first()
    with pytest.raises(TheatreError, match="مش بتاعة العيادة"):
        theatre.book_operation(**{**base, "room": foreign, "procedure": world["cs"]}, start=at(14))
    assert Operation.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_room_cannot_be_double_booked_under_concurrency(org_a, make_member):
    """Plan 14 DoD: two operations cannot overlap in one room, also when eight people book the same hour at once."""
    with freeze_time(NOW, tick=True):
        theatre.ensure_defaults(org_a)
        surgeon = Doctor.objects.create(organization=org_a, name_ar="سارة")
        room = Room.objects.filter(organization=org_a, kind=RoomKind.OPERATING).first()
        procedure = ProcedureType.objects.filter(organization=org_a, delivery="").first()
        patients = [
            patient_services.create_patient(
                org_a, Patient(full_name=f"م {n}", phone=f"+2010033{n:05d}", gender=Gender.FEMALE)
            )
            for n in range(8)
        ]
        results, barrier = [], threading.Barrier(8)

        def worker(patient):
            try:
                barrier.wait()
                theatre.book_operation(org=org_a, patient=patient, procedure=procedure, room=room, surgeon=surgeon,
                                       start=at(15), by=None, notify=False)  # fmt: skip
                results.append("booked")
            except TheatreError:
                results.append("refused")
            except Exception as e:  # anything else is a bug: reported by the assertion
                results.append(repr(e))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(p,)) for p in patients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert sorted(results) == ["booked"] + ["refused"] * 7
    assert Operation.objects.filter(room=room, status=OperationStatus.SCHEDULED).count() == 1


def test_moving_an_operation_keeps_its_history(world, django_capture_on_commit_callbacks):
    p1, p2 = world["patients"][:2]
    with django_capture_on_commit_callbacks(execute=True):
        op = _book(world, p1, hour=11)
    other = _book(world, p2, hour=13)
    with pytest.raises(TheatreError, match="مشغولة"):
        theatre.move_operation(op, start=at(13, 15), by=world["reception"])
    with django_capture_on_commit_callbacks(execute=True):
        moved = theatre.move_operation(
            op, start=at(15), room=world["delivery_room"], by=world["reception"], reason="طلب المريضة"
        )
    assert (moved.pk, moved.room, moved.planned_start, moved.planned_end) == (
        op.pk,
        world["delivery_room"],
        at(15),
        at(15, 30),
    )
    assert "اتنقلت من" in moved.events.last().notes and "طلب المريضة" in moved.events.last().notes
    assert moved.history.count() >= 2
    theatre.move_operation(other, start=at(11), by=world["reception"])  # the old time is free again
    # the patient hears about it, and the old reminder is withdrawn
    rows = ScheduledMessage.objects.filter(dedupe_key__startswith=f"op:{op.pk}:")
    assert rows.filter(event=Event.OPERATION_BOOKED).count() == 1
    assert rows.filter(event=Event.OPERATION_CHANGED, status=MessageStatus.PENDING).count() == 1
    theatre.transition(moved, OperationStatus.ADMITTED, by=world["doctor_user"])
    _tick_all(moved, world["doctor_user"])
    theatre.transition(moved, OperationStatus.IN_THEATRE, by=world["doctor_user"])
    with pytest.raises(TheatreError, match="مينفعش يتغير"):
        theatre.move_operation(moved, start=at(17), by=world["reception"])


# ================================================================ the day of the operation


def test_the_steps_and_the_checklist(world):
    user = world["doctor_user"]
    op = _book(world, hour=11)
    with pytest.raises(TheatreError, match="مينفعش العملية تتنقل"):
        theatre.transition(op, OperationStatus.IN_THEATRE, by=user)  # no skipping a step
    op = theatre.transition(op, OperationStatus.ADMITTED, by=user)
    assert op.admitted_at is not None
    with pytest.raises(TheatreError, match="قايمة ما قبل العملية"):
        theatre.transition(op, OperationStatus.IN_THEATRE, by=user)
    item = op.checklist.first()
    theatre.toggle_checklist(item, by=user)
    item.refresh_from_db()
    assert item.done and item.done_by == user and item.done_at
    theatre.toggle_checklist(item, by=user)  # ticked by mistake
    item.refresh_from_db()
    assert not item.done and item.done_by is None
    _tick_all(op, user)
    assert op.checklist.filter(required=False, done=False).count() == 0  # _tick_all ticks the optional one too
    op = theatre.transition(op, OperationStatus.IN_THEATRE, by=user)
    op = theatre.transition(op, OperationStatus.RECOVERY, by=user)
    assert op.started_at and op.ended_at
    op = theatre.transition(op, OperationStatus.IN_THEATRE, by=user, notes="رجوع خطوة")  # back one step
    assert op.ended_at is None
    op = theatre.transition(op, OperationStatus.RECOVERY, by=user)
    op = theatre.transition(op, OperationStatus.DONE, by=user)
    assert op.done_at and not op.is_open
    with pytest.raises(TheatreError):
        theatre.transition(op, OperationStatus.CANCELLED, by=user)
    assert [e.to_status for e in op.events.all()] == [
        "scheduled", "admitted", "in_theatre", "recovery", "in_theatre", "recovery", "done",
    ]  # fmt: skip


def test_an_emergency_goes_in_without_the_checklist_and_it_is_logged(world):
    user = world["doctor_user"]
    op = _book(world, hour=9, is_emergency=True)
    op = theatre.transition(op, OperationStatus.ADMITTED, by=user)
    op = theatre.transition(op, OperationStatus.IN_THEATRE, by=user)
    note = op.events.last().notes
    assert op.status == OperationStatus.IN_THEATRE and "طوارئ" in note and "إقرار العملية موقّع" in note


def test_postponed_operation_gets_a_new_date_as_a_new_operation(world):
    op = _book(world, hour=11)
    op = theatre.transition(op, OperationStatus.POSTPONED, by=world["reception"], notes="ضغطها عالي")
    assert op.status == OperationStatus.POSTPONED and op.cancel_reason == "ضغطها عالي"
    assert theatre.waiting_for_a_date(world["org"]) == [op]
    _book(world, world["patients"][1], hour=11)  # its room is free
    new = theatre.rebook_postponed(op, start=at(10, day=TODAY + timedelta(days=3)), by=world["reception"])
    op.refresh_from_db()
    assert op.postponed_to == new and new.status == OperationStatus.SCHEDULED and new.procedure == op.procedure
    assert theatre.waiting_for_a_date(world["org"]) == []
    with pytest.raises(TheatreError, match="ميعاد جديد قبل كده"):
        theatre.rebook_postponed(op, start=at(10, day=TODAY + timedelta(days=4)), by=world["reception"])


# ================================================================ deliveries


def _pregnant(world, patient, weeks=38):
    edd = TODAY + timedelta(days=(40 - weeks) * 7)
    return obstetrics.start_pregnancy(
        patient, obstetrics.Dating(method="ultrasound", us_date=TODAY, us_weeks=weeks), by=world["doctor_user"]
    ), edd


def test_finishing_a_delivery_closes_the_pregnancy_and_writes_the_history(world):
    """Plan 14 DoD: closing a delivery closes the pregnancy and creates the newborn and history rows."""
    user, mother = world["doctor_user"], world["patients"][0]
    pregnancy, _edd = _pregnant(world, mother)
    op = _book(world, mother, hour=11, procedure=world["cs"])
    assert op.pregnancy == pregnancy and op.is_delivery  # the followed pregnancy is picked up by itself
    op = theatre.transition(op, OperationStatus.ADMITTED, by=user)
    _tick_all(op, user)
    op = theatre.transition(op, OperationStatus.IN_THEATRE, by=user)
    op = theatre.transition(op, OperationStatus.RECOVERY, by=user)
    with pytest.raises(TheatreError, match="سجّل المولود"):
        theatre.transition(op, OperationStatus.DONE, by=user)
    op.refresh_from_db()
    assert op.status == OperationStatus.RECOVERY  # nothing half-done

    baby = theatre.add_newborn(op, sex="female", weight_g=3200, apgar_1=8, apgar_5=9, by=user)
    twin = theatre.add_newborn(op, sex="male", weight_g=2900, apgar_1=7, apgar_5=9, status=NewbornStatus.NICU, by=user)
    wrong = theatre.add_newborn(op, sex="male", by=user)
    theatre.remove_newborn(wrong, by=user)
    with pytest.raises(Exception, match="200"):
        theatre.add_newborn(op, sex="male", weight_g=50, by=user)
    assert [b.pk for b in theatre.living_newborns(op)] == [baby.pk, twin.pk] and (baby.order, twin.order) == (1, 2)

    op = theatre.transition(op, OperationStatus.DONE, by=user)
    assert op.delivery_closed
    pregnancy.refresh_from_db()
    assert (pregnancy.status, pregnancy.ended_on) == (PregnancyStatus.DELIVERED, TODAY)
    row = PregnancyHistory.objects.get(pregnancy=pregnancy)
    assert (row.outcome, row.living_children, row.baby_sex, row.baby_weight_g, row.weeks) == (
        Outcome.CESAREAN,
        2,
        "female",
        3200,
        38,
    )
    assert obstetrics.gpal(mother).label == "G1 P1 A0 L2" and obstetrics.active_pregnancy(mother) is None
    assert Newborn.objects.filter(operation=op, is_removed=False).count() == 2
    with pytest.raises(TheatreError, match="اتقفلت"):
        theatre.add_newborn(op, sex="male", by=user)
    with pytest.raises(TheatreError, match="اتقفلت"):
        theatre.remove_newborn(baby, by=user)


def test_stillbirth_and_a_delivery_without_a_followed_pregnancy(world):
    user = world["doctor_user"]
    mother, other = world["patients"][:2]
    pregnancy, _edd = _pregnant(world, mother, weeks=30)
    vaginal = ProcedureType.objects.get(organization=world["org"], delivery=DeliveryMode.VAGINAL)
    op = _book(world, mother, hour=9, procedure=vaginal, room=world["delivery_room"], is_emergency=True)
    for status in (OperationStatus.ADMITTED, OperationStatus.IN_THEATRE, OperationStatus.RECOVERY):
        op = theatre.transition(op, status, by=user)
    theatre.add_newborn(op, sex="male", weight_g=1400, status=NewbornStatus.STILLBORN, by=user)
    theatre.transition(op, OperationStatus.DONE, by=user)
    row = PregnancyHistory.objects.get(pregnancy=pregnancy)
    assert (row.outcome, row.living_children) == (Outcome.STILLBIRTH, 0)

    # a woman who was never followed here: the newborn is recorded, there is no pregnancy file to close
    walk_in = _book(world, other, hour=13, procedure=vaginal, room=world["delivery_room"], is_emergency=True)
    assert walk_in.pregnancy is None
    for status in (OperationStatus.ADMITTED, OperationStatus.IN_THEATRE, OperationStatus.RECOVERY):
        walk_in = theatre.transition(walk_in, status, by=user)
    theatre.add_newborn(walk_in, sex="female", weight_g=3100, by=user)
    walk_in = theatre.transition(walk_in, OperationStatus.DONE, by=user)
    assert walk_in.delivery_closed and not PregnancyHistory.objects.filter(patient=other).exists()
    with pytest.raises(TheatreError, match="مش ولادة"):
        theatre.add_newborn(_book(world, world["patients"][2], hour=15), sex="male", by=user)


# ================================================================ team, consumables, note


def test_team_and_consumables(world):
    user = world["doctor_user"]
    op = _book(world, hour=11)
    member = theatre.save_team_member(op, role=TeamRole.ANAESTHETIST, name="د. هاني (من بره)", fee="1500", by=user)
    theatre.save_team_member(op, role=TeamRole.ANAESTHETIST, name="د. هاني", fee="1750.50", member=member, by=user)
    member.refresh_from_db()
    assert (member.display_name, member.fee) == ("د. هاني", D("1750.50"))
    with pytest.raises(TheatreError, match="بالسالب"):
        theatre.save_team_member(op, role=TeamRole.NURSE, name="ن", fee="-1", by=user)
    with pytest.raises(TheatreError, match="الدور"):
        theatre.save_team_member(op, role="driver", name="س", by=user)
    item = theatre.add_consumable(op, name="خيط فيكريل", quantity="3", unit="بكرة", by=user)
    assert (item.name, item.quantity, item.unit) == ("خيط فيكريل", D("3"), "بكرة")
    with pytest.raises(TheatreError, match="أكبر من صفر"):
        theatre.add_consumable(op, name="شاش", quantity=0, by=user)
    theatre.transition(op, OperationStatus.CANCELLED, by=user)
    with pytest.raises(TheatreError, match="ملغية"):
        theatre.add_consumable(op, name="شاش", by=user)


def test_a_signed_note_never_changes(world):
    user = world["doctor_user"]
    op = _book(world, hour=11)
    note = theatre.draft_note(op, by=user)
    assert theatre.draft_note(op, by=user) == note and note.revision == 0
    with pytest.raises(TheatreError, match="قبل التوقيع"):
        theatre.sign_note(note, by=user)
    theatre.save_note(note, {"procedure_details": "كحت تشخيصي", "blood_loss_ml": "150", "anaesthesia": "كلي"}, by=user)
    with pytest.raises(TheatreError, match="رقم"):
        theatre.save_note(note, {"blood_loss_ml": "كتير"}, by=user)
    signed = theatre.sign_note(note, by=user)
    assert (signed.status, signed.signed_by, signed.blood_loss_ml) == (
        NoteStatus.SIGNED,
        user,
        150,
    ) and signed.signed_at
    with pytest.raises(TheatreError, match="اتوقّع"):
        theatre.save_note(signed, {"procedure_details": "تغيير"}, by=user)
    revision = theatre.draft_note(op, by=user)  # a correction starts a new revision from the signed text
    assert (revision.revision, revision.procedure_details, revision.status) == (1, "كحت تشخيصي", NoteStatus.DRAFT)
    theatre.save_note(revision, {"complications": "لا يوجد"}, by=user)
    theatre.sign_note(revision, by=user)
    signed.refresh_from_db()
    assert signed.complications == "" and theatre.latest_note(op).complications == "لا يوجد"
    assert op.op_notes.count() == 2


# ================================================================ admissions


def test_admission_beds_nights_and_discharge(world):
    org, user = world["org"], world["reception"]
    ward = world["ward"]
    ward.nightly_rate, ward.beds = D("800"), 1
    ward.save()
    p1, p2 = world["patients"][:2]
    stay = theatre.admit(org=org, patient=p1, room=ward, by=user, reason="بعد قيصرية")
    assert (stay.nightly_rate, stay.status, theatre.beds_free(ward)) == (D("800"), AdmissionStatus.ADMITTED, 0)
    with pytest.raises(TheatreError, match="سرير فاضي"):
        theatre.admit(org=org, patient=p2, room=ward, by=user)
    other = Room.objects.filter(organization=org, kind=RoomKind.INPATIENT).exclude(pk=ward.pk).first()
    with pytest.raises(TheatreError, match="مقيمة في غرفة"):
        theatre.admit(org=org, patient=p1, room=other, by=user)
    with pytest.raises(TheatreError, match="إقامة أو إفاقة"):
        theatre.admit(org=org, patient=p2, room=world["theatre"], by=user)
    ward.nightly_rate = D("1000")  # a later price change does not touch a stay that already started
    ward.save()

    assert stay.nights() == 1  # the same day counts as one night
    with freeze_time("2026-03-12 11:00:00+02:00"):
        assert (stay.nights(), stay.room_charge()) == (2, D("1600"))
        done = theatre.discharge(stay, by=user, summary="حالتها مستقرة", instructions="راحة أسبوع")
    assert (done.status, done.discharged_by, done.nights(), done.room_charge()) == (
        AdmissionStatus.DISCHARGED,
        user,
        2,
        D("1600"),
    )
    with pytest.raises(TheatreError, match="مقفولة"):
        theatre.discharge(done, by=user)
    assert theatre.beds_free(ward) == 1 and theatre.admitted_now(org) == []
    wrong = theatre.admit(org=org, patient=p2, room=ward, by=user)
    theatre.cancel_admission(wrong, by=user)
    assert (
        Admission.objects.filter(patient=p2, status=AdmissionStatus.CANCELLED).exists() and theatre.beds_free(ward) == 1
    )


# ================================================================ messages


def test_operation_messages_go_through_the_outbox(world, django_capture_on_commit_callbacks):
    patient = world["patients"][0]
    start = at(10, day=TODAY + timedelta(days=3))
    with django_capture_on_commit_callbacks(execute=True):
        op = theatre.book_operation(org=world["org"], patient=patient, procedure=world["cs"], room=world["theatre"],
                                    surgeon=world["surgeon"], start=start, by=world["reception"])  # fmt: skip
    rows = {r.event: r for r in ScheduledMessage.objects.filter(dedupe_key__startswith=f"op:{op.pk}:")}
    assert set(rows) == {Event.OPERATION_BOOKED, Event.OPERATION_REMINDER}
    reminder = rows[Event.OPERATION_REMINDER]
    assert reminder.status == MessageStatus.PENDING and reminder.appointment is None and reminder.patient == patient
    assert reminder.context["operation_name"] == "ولادة قيصرية" and reminder.context["fasting_hours"] == 8
    assert timezone.localtime(reminder.send_at, CAIRO).date() == TODAY + timedelta(days=2)

    from apps.notifications.dispatcher import Dispatcher

    Dispatcher().render(rows[Event.OPERATION_BOOKED])
    text = rows[Event.OPERATION_BOOKED].rendered_text
    assert "ولادة قيصرية" in text and "د. سارة علي" in text and patient.full_name.split()[0] in text

    with django_capture_on_commit_callbacks(execute=True):
        theatre.transition(op, OperationStatus.CANCELLED, by=world["reception"])
    reminder.refresh_from_db()
    assert reminder.status == MessageStatus.CANCELLED  # nothing reminds her of a cancelled operation

    # an emergency operation sends nothing, and a patient who refused messages gets a "skipped" row, never a message
    with django_capture_on_commit_callbacks(execute=True):
        urgent = _book(world, world["patients"][1], hour=9, is_emergency=True)
    assert not ScheduledMessage.objects.filter(dedupe_key__startswith=f"op:{urgent.pk}:").exists()
    quiet = world["patients"][2]
    quiet.messaging_consent = False
    quiet.save()
    with django_capture_on_commit_callbacks(execute=True):
        silent = _book(world, quiet, hour=14)
    assert set(
        ScheduledMessage.objects.filter(dedupe_key__startswith=f"op:{silent.pk}:").values_list("status", flat=True)
    ) == {MessageStatus.SKIPPED}


def test_older_clinics_get_the_operation_templates(org_a):
    seed_org_notifications(org_a)
    NotificationTemplate.objects.filter(organization=org_a, event__startswith="operation_").delete()
    mine = NotificationTemplate.objects.filter(organization=org_a)
    before = mine.count()
    edited = mine.first()
    edited.body = "نص العيادة"
    edited.save()
    assert seed_org_notifications(org_a) == 3 and seed_org_notifications(org_a) == 0
    assert mine.count() == before + 3
    edited.refresh_from_db()
    assert edited.body == "نص العيادة"


# ================================================================ screens


def test_theatre_screens_and_who_may_do_what(client, world):
    org = world["org"]
    mother = world["patients"][0]
    _pregnant(world, mother)
    op = _book(world, mother, hour=11, procedure=world["cs"])
    stay = theatre.admit(org=org, patient=world["patients"][1], room=world["ward"], by=world["reception"])

    client.force_login(world["doctor_user"])
    board = client.get(reverse("theatre:board"))
    html = board.content.decode()
    assert board.status_code == 200 and mother.full_name in html and "ولادة قيصرية" in html
    assert "الشهر" in html  # the pregnancy week next to her name on the operations list
    assert world["patients"][1].full_name in html  # admitted now
    assert client.get(reverse("theatre:board"), {"view": "week"}).status_code == 200
    page = client.get(reverse("theatre:operation", args=[op.pk])).content.decode()
    for part in ("op-checklist", "op-team", "op-note", "op-newborns", "op-consumables"):
        assert f'id="{part}"' in page, part
    for name, args in [("operation_new", []), ("operation_move", [op.pk]), ("admissions", []), ("admit", []),
                       ("discharge", [stay.pk])]:  # fmt: skip
        assert client.get(reverse(f"theatre:{name}", args=args)).status_code == 200, name

    # reception books and admits, but does not run the theatre and never sees the surgeon's note
    client.force_login(world["reception"])
    page = client.get(reverse("theatre:operation", args=[op.pk])).content.decode()
    assert 'id="op-note"' not in page and reverse("theatre:operation_step", args=[op.pk, "admit"]) not in page
    assert reverse("theatre:operation_move", args=[op.pk]) in page
    assert client.post(reverse("theatre:operation_step", args=[op.pk, "admit"])).status_code == 403
    assert client.post(reverse("theatre:note_save", args=[op.pk]), {"findings": "x"}).status_code == 403
    assert client.post(reverse("theatre:checklist_toggle", args=[op.checklist.first().pk])).status_code == 403
    assert client.get(reverse("theatre:discharge_pdf", args=[stay.pk])).status_code == 403
    assert client.get(reverse("theatre:setup")).status_code == 403

    # the viewer has no theatre at all; another clinic sees nothing of this one
    client.force_login(world["viewer"])
    assert client.get(reverse("theatre:board")).status_code == 403
    client.force_login(world["other_owner"])
    assert client.get(reverse("theatre:operation", args=[op.pk])).status_code == 404
    assert client.get(reverse("theatre:discharge", args=[stay.pk])).status_code == 404
    assert client.post(reverse("theatre:operation_step", args=[op.pk, "admit"])).status_code == 404
    assert mother.full_name not in client.get(reverse("theatre:board")).content.decode()


def test_booking_and_running_an_operation_from_the_screens(client, world):
    org, mother = world["org"], world["patients"][0]
    client.force_login(world["doctor_user"])
    found = client.get(reverse("theatre:patient_search"), {"q": "مريضة 1"}).content.decode()
    assert f"?patient={mother.pk}" in found
    data = {
        "patient": mother.pk,
        "procedure": world["dnc"].pk,
        "room": world["theatre"].pk,
        "surgeon": world["surgeon"].pk,
        "date": "2026-03-10",
        "time": "11:00",
        "duration_minutes": "",
        "notes": "",
    }
    resp = client.post(reverse("theatre:operation_new"), data)
    op = Operation.objects.get(organization=org)
    assert resp.url == reverse("theatre:operation", args=[op.pk]) and op.planned_end == at(11, 30)
    clash = client.post(reverse("theatre:operation_new"), {**data, "patient": world["patients"][1].pk, "time": "11:15"})
    assert clash.status_code == 200 and "مشغولة" in clash.content.decode() and Operation.objects.count() == 1

    client.post(reverse("theatre:operation_step", args=[op.pk, "admit"]))
    blocked = client.post(reverse("theatre:operation_step", args=[op.pk, "start"]), follow=True)
    assert "قايمة ما قبل العملية" in blocked.content.decode()
    for item in ChecklistItem.objects.filter(operation=op):
        part = client.post(reverse("theatre:checklist_toggle", args=[item.pk]))
        assert part.status_code == 200 and 'id="op-checklist"' in part.content.decode()
    client.post(reverse("theatre:operation_step", args=[op.pk, "start"]))
    client.post(reverse("theatre:operation_step", args=[op.pk, "undo"]))
    op.refresh_from_db()
    assert op.status == OperationStatus.ADMITTED
    client.post(reverse("theatre:operation_step", args=[op.pk, "start"]))
    team = client.post(
        reverse("theatre:team_save", args=[op.pk]), {"role": "anaesthetist", "name": "د. هاني", "fee": "1500"}
    )
    assert "د. هاني" in team.content.decode() and "1,500.00" in team.content.decode()
    used = client.post(
        reverse("theatre:consumable_add", args=[op.pk]), {"name": "شاش معقم", "quantity": "5", "unit": "علبة"}
    )
    assert "شاش معقم" in used.content.decode()
    note = client.post(
        reverse("theatre:note_save", args=[op.pk]), {"procedure_details": "كحت", "blood_loss_ml": "100", "sign": "1"}
    )
    assert "موقّع" in note.content.decode() and theatre.latest_note(op).is_signed
    client.post(reverse("theatre:operation_step", args=[op.pk, "recover"]))
    client.post(reverse("theatre:operation_step", args=[op.pk, "done"]))
    op.refresh_from_db()
    assert op.status == OperationStatus.DONE

    # admit after the operation, then discharge and print the summary
    resp = client.post(
        reverse("theatre:admit"), {"patient": mother.pk, "operation": op.pk, "room": world["ward"].pk, "reason": ""}
    )
    stay = Admission.objects.get(patient=mother)
    assert resp.status_code == 302 and stay.operation == op and stay.doctor == world["surgeon"]
    client.post(reverse("theatre:discharge", args=[stay.pk]), {"summary": "مستقرة", "instructions": "راحة"})
    stay.refresh_from_db()
    assert stay.status == AdmissionStatus.DISCHARGED and stay.discharge_summary == "مستقرة"
    pdf = client.get(reverse("theatre:discharge_pdf", args=[stay.pk]))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")


def test_emergency_case_leads_to_an_operation_or_a_room(client, world):
    org = world["org"]
    case = center.register_emergency(org=org, patient=world["patients"][0], doctor=world["surgeon"], triage="critical",
                                     reason="bleeding", by=world["reception"])  # fmt: skip
    center.set_disposition(case, "theatre", by=world["doctor_user"])
    client.force_login(world["doctor_user"])
    log = client.get(reverse("center:emergencies")).content.decode()
    assert f"{reverse('theatre:operation_new')}?emergency_case={case.pk}" in log
    form = client.get(reverse("theatre:operation_new"), {"emergency_case": case.pk})
    assert form.context["patient"] == case.patient and form.context["form"].initial["is_emergency"] is True
    resp = client.post(
        reverse("theatre:operation_new"),
        {
            "patient": case.patient_id,
            "emergency_case": case.pk,
            "procedure": world["dnc"].pk,
            "room": world["theatre"].pk,
            "surgeon": world["surgeon"].pk,
            "date": "2026-03-10",
            "time": "09:00",
            "is_emergency": "on",
            "notes": "",
        },
    )
    op = Operation.objects.get(emergency_case=case)
    assert resp.status_code == 302 and op.is_emergency

    second = center.register_emergency(org=org, patient=world["patients"][1], doctor=world["surgeon"], triage="urgent",
                                       reason="pain", by=world["reception"])  # fmt: skip
    center.set_disposition(second, "admitted", by=world["doctor_user"])
    client.post(
        reverse("theatre:admit"), {"patient": second.patient_id, "emergency_case": second.pk, "room": world["ward"].pk}
    )
    stay = Admission.objects.get(emergency_case=second)
    assert stay.doctor == world["surgeon"] and stay.status == AdmissionStatus.ADMITTED


def test_rooms_and_procedures_setup(client, world, make_member):
    org = world["org"]
    client.force_login(make_member(org, "owner"))
    assert client.get(reverse("theatre:setup")).status_code == 200
    client.post(
        reverse("theatre:room_add"),
        {"name": "جناح خاص", "kind": "inpatient", "beds": 1, "nightly_rate": "2500", "is_active": "on"},
    )
    suite = Room.objects.get(organization=org, name="جناح خاص")
    assert suite.nightly_rate == D("2500")
    again = client.post(
        reverse("theatre:room_add"), {"name": "جناح خاص", "kind": "inpatient", "beds": 1, "nightly_rate": "1"}
    )
    assert "فيه غرفة بالاسم ده" in again.content.decode()
    client.post(
        reverse("theatre:room_edit", args=[suite.pk]),
        {"name": "جناح خاص", "kind": "inpatient", "beds": 2, "nightly_rate": "3000"},
    )
    suite.refresh_from_db()
    assert (suite.beds, suite.nightly_rate, suite.is_active) == (2, D("3000"), False)
    client.post(
        reverse("theatre:procedure_add"),
        {"name_ar": "تركيب حلقة", "name_en": "", "delivery": "", "duration_minutes": 40, "package_price": "4000",
         "fasting_hours": 6, "checklist_text": "إقرار موقّع\n* صورة دم", "is_active": "on"},
    )  # fmt: skip
    proc = ProcedureType.objects.get(organization=org, name_ar="تركيب حلقة")
    assert proc.checklist == [{"label": "إقرار موقّع", "required": True}, {"label": "صورة دم", "required": False}]
    foreign = ProcedureType.objects.filter(organization=world["other_org"]).first()
    theatre.ensure_defaults(world["other_org"])
    foreign = ProcedureType.objects.filter(organization=world["other_org"]).first()
    assert client.get(reverse("theatre:procedure_edit", args=[foreign.pk])).status_code == 404
