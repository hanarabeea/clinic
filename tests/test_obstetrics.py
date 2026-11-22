"""OB/GYN layer (docs/plan/14-obgyn-center.md §14.2): pregnancy dating, the gestational age on every screen, the
obstetric history and its counts, milestones, the antenatal sheet, and who may see what."""

import io
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.urls import get_resolver, reverse
from django.utils import timezone
from freezegun import freeze_time

from apps.clinical import services as clinical
from apps.doctors.models import Doctor, VisitType, WorkingPeriod
from apps.obstetrics import calc, services
from apps.obstetrics.models import (
    AntenatalVisit,
    EddMethod,
    Milestone,
    Outcome,
    Pregnancy,
    PregnancyHistory,
    PregnancyStatus,
    RiskLevel,
)
from apps.organizations.models import OrganizationSettings
from apps.patients.models import Gender, Patient
from apps.scheduling import services as booking
from apps.scheduling.models import AppointmentStatus

NOW = "2026-06-10 10:00:00"  # 13:00 in Cairo
TODAY = date(2026, 6, 10)
LMP = date(2025, 12, 21)  # 24 weeks + 3 days before TODAY
EDD = date(2026, 9, 27)

# --- the arithmetic (no database) ----------------------------------------------------------------------------------


def test_naegele_and_the_cycle_length():
    assert calc.edd_from_lmp(date(2026, 1, 1)) == date(2026, 10, 8)
    assert calc.edd_from_lmp(date(2026, 1, 1), cycle_days=35) == date(2026, 10, 15)  # ovulates a week later
    assert calc.edd_from_lmp(date(2026, 1, 1), cycle_days=21) == date(2026, 10, 1)
    assert calc.edd_from_lmp(LMP) == EDD


def test_ultrasound_and_ivf_dating_land_on_the_same_scale():
    assert calc.edd_from_ultrasound(date(2026, 3, 1), 8, 0) == date(2026, 10, 11)
    assert calc.edd_from_ultrasound(date(2026, 6, 3), 22, 0) == date(2026, 10, 7)
    # a day-5 embryo moved on 20 Jan is 2 weeks + 5 days "pregnant" that day
    edd = calc.edd_from_transfer(date(2026, 1, 20), 5)
    assert edd == date(2026, 10, 8) and calc.ga_days(edd, date(2026, 1, 20)) == 19
    assert calc.edd_from_transfer(date(2026, 1, 20), 3) == date(2026, 10, 10)


def test_gestational_age_and_its_wording():
    ga = calc.gestational_age(EDD, TODAY)
    assert (ga.days, ga.weeks, ga.extra_days) == (171, 24, 3)
    assert ga.weeks_label == "24 أسبوع و3 أيام" and ga.short_label == "24+3"
    assert ga.month_label == "الشهر السادس" and ga.trimester_label == "الثلث الثاني"
    assert ga.days_left == 109 and ga.left_label == "باقي 109 يوم" and not ga.overdue and not ga.implausible
    assert calc.gestational_age(EDD, TODAY - timedelta(days=3)).weeks_label == "24 أسبوع"
    assert calc.gestational_age(EDD, TODAY - timedelta(days=2)).weeks_label == "24 أسبوع ويوم"
    assert calc.gestational_age(EDD, TODAY - timedelta(days=1)).weeks_label == "24 أسبوع ويومين"
    assert calc.gestational_age(EDD, EDD).left_label == "النهارده"
    late = calc.gestational_age(EDD, EDD + timedelta(days=5))
    assert late.overdue and late.left_label == "عدّى بـ5 أيام" and late.weeks_label == "40 أسبوع و5 أيام"
    assert calc.gestational_age(EDD, EDD - timedelta(days=2)).left_label == "باقي يومين"
    assert calc.gestational_age(EDD, LMP - timedelta(days=1)).implausible
    assert calc.gestational_age(EDD, EDD + timedelta(days=40)).implausible


@pytest.mark.parametrize(
    ("weeks", "days", "month", "trimester"),
    [
        (0, 0, 1, 1), (4, 0, 1, 1), (4, 3, 2, 1), (8, 0, 2, 1), (12, 0, 3, 1), (13, 0, 3, 1), (13, 1, 4, 1),
        (13, 6, 4, 1), (14, 0, 4, 2), (20, 0, 5, 2), (24, 3, 6, 2), (27, 6, 7, 2), (28, 0, 7, 3), (32, 0, 8, 3),
        (36, 0, 9, 3), (40, 0, 9, 3), (41, 6, 9, 3),
    ],
)  # fmt: skip
def test_month_the_way_patients_count_it_and_the_trimester(weeks, days, month, trimester):
    age = weeks * 7 + days
    assert (calc.month(age), calc.trimester(age)) == (month, trimester)


def test_term_and_the_visit_rhythm():
    assert [calc.term_status(w * 7) for w in (30, 36, 37, 41, 42)] == [
        "preterm",
        "preterm",
        "term",
        "term",
        "post_term",
    ]
    assert calc.term_status(36 * 7 + 6) == "preterm" and calc.term_status(41 * 7 + 6) == "term"
    assert [calc.next_visit_weeks(w * 7) for w in (8, 27, 28, 35, 36, 40)] == [4, 4, 2, 2, 1, 1]


# --- fixtures ------------------------------------------------------------------------------------------------------


@pytest.fixture
def ob(org_a, org_b, make_member):
    """An OB/GYN clinic with one woman, plus the same in another clinic."""
    OrganizationSettings.objects.filter(organization__in=[org_a, org_b]).update(specialty="obgyn")
    org_a.settings.refresh_from_db()
    org_b.settings.refresh_from_db()
    doctor = Doctor.objects.create(organization=org_a, name_ar="سارة", slot_minutes=20)
    for weekday in range(7):
        WorkingPeriod.objects.create(organization=org_a, doctor=doctor, weekday=weekday,
                                     start_time="00:00", end_time="23:59")  # fmt: skip
    vt = VisitType.objects.create(organization=org_a, doctor=doctor, name_ar="كشف", duration_minutes=20,
                                  price=Decimal("300"))  # fmt: skip
    patient = Patient.objects.create(organization=org_a, full_name="منى سعيد", phone="+201001234567",
                                     gender=Gender.FEMALE, file_number=1, date_of_birth=date(1996, 2, 1))  # fmt: skip
    theirs = Patient.objects.create(organization=org_b, full_name="هدى فؤاد", phone="+201009999999",
                                    gender=Gender.FEMALE, file_number=1)  # fmt: skip
    return {
        "org": org_a, "doctor": doctor, "vt": vt, "patient": patient, "theirs": theirs,
        "doc": make_member(org_a, "doctor"), "reception": make_member(org_a, "reception"),
        "viewer": make_member(org_a, "viewer"), "other": make_member(org_b, "doctor"),
    }  # fmt: skip


def _pregnant(ob, lmp=LMP, **kw):
    with freeze_time(NOW):
        return services.start_pregnancy(
            ob["patient"], services.Dating(method=EddMethod.LMP, lmp=lmp), by=ob["doc"], doctor=ob["doctor"], **kw
        )


def _visit(ob, patient=None):
    """A visit open on the doctor's desk today."""
    patient = patient or ob["patient"]
    appt = booking.book(patient=patient, doctor=ob["doctor"], visit_type=ob["vt"], by=ob["reception"],
                        start_at=timezone.now() + timedelta(hours=1), overbook=True)  # fmt: skip
    booking.transition(appt, AppointmentStatus.ARRIVED, by=ob["reception"])
    return clinical.start_visit(appt, by=ob["doc"])


# --- the pregnancy -------------------------------------------------------------------------------------------------


def test_start_one_pregnancy_at_a_time(ob):
    p = _pregnant(ob)
    assert (p.edd, p.edd_method, p.status, p.doctor) == (EDD, "lmp", PregnancyStatus.ACTIVE, ob["doctor"])
    assert p.ga(TODAY).weeks_label == "24 أسبوع و3 أيام"
    with freeze_time(NOW), pytest.raises(services.ObstetricError, match="فيه حمل متابَع بالفعل"):
        services.start_pregnancy(ob["patient"], services.Dating(lmp=LMP), by=ob["doc"])
    # and the database refuses it even past the service (two tabs, two doctors)
    with pytest.raises(IntegrityError), transaction.atomic():
        Pregnancy.objects.create(organization=ob["org"], patient=ob["patient"], edd=EDD)


@freeze_time(NOW)
def test_dates_that_cannot_be_right_are_refused(ob):
    start = lambda **kw: services.start_pregnancy(ob["patient"], services.Dating(**kw), by=ob["doc"])  # noqa: E731
    with pytest.raises(services.ObstetricError, match="المستقبل"):
        start(lmp=TODAY + timedelta(days=1))
    with pytest.raises(services.ObstetricError, match="44 أسبوع"):
        start(lmp=TODAY - timedelta(days=330))
    with pytest.raises(services.ObstetricError, match="آخر دورة"):
        start(lmp=None)
    with pytest.raises(services.ObstetricError, match="السونار"):
        start(method=EddMethod.ULTRASOUND, us_date=TODAY)
    with pytest.raises(services.ObstetricError, match="نقل الأجنة"):
        start(method=EddMethod.IVF)
    man = Patient.objects.create(organization=ob["org"], full_name="علي", phone="+201005555555", gender=Gender.MALE,
                                 file_number=2)  # fmt: skip
    with pytest.raises(services.ObstetricError, match="للمريضات"):
        services.start_pregnancy(man, services.Dating(lmp=LMP), by=ob["doc"])
    assert not Pregnancy.objects.exists()


@freeze_time(NOW)
def test_changing_the_dating_moves_the_expected_date(ob):
    p = _pregnant(ob)
    p = services.set_dating(
        p, services.Dating(method=EddMethod.ULTRASOUND, lmp=LMP, us_date=date(2026, 6, 3), us_weeks=22, us_days=0)
    )
    assert p.edd == date(2026, 10, 7) and p.edd_method == "ultrasound" and p.lmp == LMP  # the period stays on file
    assert p.ga(TODAY).short_label == "23+0"
    with pytest.raises(services.ObstetricError, match="المستقبل"):
        services.set_dating(p, services.Dating(lmp=TODAY + timedelta(days=3)))
    assert Pregnancy.objects.get(pk=p.pk).edd == date(2026, 10, 7)  # a refused change changes nothing
    assert p.history.count() == 2  # simple-history keeps the first dating


@freeze_time(NOW)
def test_closing_writes_the_history_and_the_counts_follow(ob):
    patient = ob["patient"]
    services.add_history(patient, outcome=Outcome.CESAREAN, by=ob["doc"], year=2021, baby_weight_g=3200)
    services.add_history(patient, outcome=Outcome.MISCARRIAGE, by=ob["doc"], year=2023, weeks=9)
    assert services.gpal(patient).label == "G2 P1 A1 L1"
    p = _pregnant(ob, lmp=date(2025, 9, 20), fetuses=2)
    counts = services.gpal(patient)
    assert counts.label == "G3 P1 A1 L1" and counts.cesareans == 1

    row = services.close_pregnancy(p, outcome=Outcome.CESAREAN, ended_on=date(2026, 6, 8), by=ob["doc"],
                                   baby_weight_g=2400, place="المركز")  # fmt: skip
    p.refresh_from_db()
    assert (p.status, p.ended_on) == (PregnancyStatus.DELIVERED, date(2026, 6, 8))
    assert (row.year, row.weeks, row.living_children, row.pregnancy) == (2026, 37, 2, p)  # twins: two living
    counts = services.gpal(patient)
    assert counts.label == "G3 P2 A1 L3" and counts.cesareans == 2
    assert p.ga(TODAY).weeks_label == "37 أسبوع ويومين"  # frozen at the day it ended, two days ago
    assert services.badge_for(patient) is None  # no longer pregnant on the screens

    with pytest.raises(services.ObstetricError, match="اتقفل قبل كده"):
        services.close_pregnancy(p, outcome=Outcome.VAGINAL, ended_on=TODAY, by=ob["doc"])
    with pytest.raises(services.ObstetricError, match="التواريخ مش بتتعدل"):
        services.set_dating(p, services.Dating(lmp=LMP))
    with pytest.raises(services.ObstetricError, match="حمل اتابع هنا"):
        services.remove_history(row)  # its own row: reopen the pregnancy instead


@freeze_time(NOW)
def test_a_loss_counts_as_abortus_and_a_wrong_close_can_be_reopened(ob):
    p = _pregnant(ob, lmp=date(2026, 4, 1))
    with pytest.raises(services.ObstetricError, match="المستقبل"):
        services.close_pregnancy(p, outcome=Outcome.MISCARRIAGE, ended_on=TODAY + timedelta(days=1), by=ob["doc"])
    with pytest.raises(services.ObstetricError, match="قبل بداية الحمل"):
        services.close_pregnancy(p, outcome=Outcome.MISCARRIAGE, ended_on=date(2026, 3, 1), by=ob["doc"])
    row = services.close_pregnancy(p, outcome=Outcome.MISCARRIAGE, ended_on=TODAY, by=ob["doc"], living_children=1,
                                   baby_sex="male")  # fmt: skip
    assert (row.living_children, row.baby_sex) == (0, "")  # not a birth: nothing about a baby is kept
    assert Pregnancy.objects.get(pk=p.pk).status == PregnancyStatus.LOST
    assert services.gpal(ob["patient"]).label == "G1 P0 A1 L0"

    services.reopen_pregnancy(p)
    assert Pregnancy.objects.get(pk=p.pk).is_active
    assert services.gpal(ob["patient"]).label == "G1 P0 A0 L0"  # the mistaken row no longer counts...
    assert PregnancyHistory.objects.filter(pk=row.pk, is_removed=True).exists()  # ...and was never deleted

    services.transfer_pregnancy(p)
    assert Pregnancy.objects.get(pk=p.pk).status == PregnancyStatus.TRANSFERRED
    assert services.gpal(ob["patient"]).gravida == 0  # followed elsewhere: no outcome recorded here


@freeze_time(NOW)
def test_risk_flags_come_only_from_what_is_recorded(ob):
    patient = ob["patient"]
    p = _pregnant(ob)
    flags = lambda: services.risk_flags(  # noqa: E731
        patient, Pregnancy.objects.get(pk=p.pk), services.profile_for(patient), services.gpal(patient), TODAY
    )
    assert flags() == []
    profile = services.profile_for(patient)
    profile.blood_group = "O-"
    profile.save()
    services.add_history(patient, outcome=Outcome.CESAREAN, by=ob["doc"], year=2022)
    services.add_history(patient, outcome=Outcome.CESAREAN, by=ob["doc"], year=2024)
    services.update_details(p, fetuses=2, risk_level=RiskLevel.HIGH, risk_notes="سكر حمل", notes="")
    assert flags() == ["سكر حمل", "توأم", "2 قيصريات سابقة", "Rh سالب"]
    patient.date_of_birth = date(1988, 1, 1)
    assert flags()[-1] == "السن 38"


# --- milestones ----------------------------------------------------------------------------------------------------


@freeze_time(NOW)
def test_the_schedule_laid_over_a_pregnancy(ob):
    p = _pregnant(ob)  # 24 weeks + 3 days
    rows = services.milestones_for(p, services.profile_for(ob["patient"]))
    assert Milestone.objects.for_org(ob["org"]).count() == len(services.DEFAULT_MILESTONES)  # seeded on first use
    state = {r.milestone.name: r.state for r in rows}
    assert state["سونار الشفافية القفوية (NT)"] == "overdue"
    assert state["منحنى السكر (GTT)"] == "due"
    assert state["مسحة GBS"] == "upcoming"
    assert "حقنة Anti-D" not in state  # only for Rh-negative women

    profile = services.profile_for(ob["patient"])
    profile.blood_group = "A-"
    profile.save()
    rows = services.milestones_for(p, profile)
    anti_d = next(r for r in rows if r.milestone.name == "حقنة Anti-D")
    assert anti_d.state == "upcoming" and anti_d.from_date == LMP + timedelta(days=28 * 7)

    gtt = next(r.milestone for r in rows if r.milestone.name == "منحنى السكر (GTT)")
    services.toggle_milestone(p, gtt, by=ob["doc"])
    done = next(r for r in services.milestones_for(p, profile) if r.milestone == gtt)
    assert done.state == "done" and done.check.done_on == TODAY
    services.toggle_milestone(p, gtt, by=ob["doc"])
    assert next(r for r in services.milestones_for(p, profile) if r.milestone == gtt).state == "due"

    Milestone.objects.for_org(ob["org"]).update(is_active=False)  # the clinic switched its list off: not re-seeded
    assert services.milestones_for(p, profile) == [] and services.ensure_milestones(ob["org"]) == 0


# --- the antenatal sheet -------------------------------------------------------------------------------------------


@freeze_time(NOW)
def test_antenatal_fields_save_one_at_a_time_and_keep_the_age_of_that_day(ob):
    p = _pregnant(ob)
    visit = _visit(ob)
    assert services.antenatal_for(visit) is None  # nothing written yet: no empty sheet
    sheet = services.save_antenatal_field(visit, "fundal_height_cm", "24.5")
    assert (sheet.pregnancy, sheet.ga_days, sheet.ga_label, sheet.fundal_height_cm) == (p, 171, "24+3", Decimal("24.5"))
    services.save_antenatal_field(visit, "fetal_heart_rate", "144")
    services.save_antenatal_field(visit, "presentation", "cephalic")
    services.save_antenatal_field(visit, "ultrasound_notes", "  مشيمة أمامية  ")
    services.save_antenatal_field(visit, "fundal_height_cm", "")  # cleared
    sheet = AntenatalVisit.objects.get(visit=visit)
    assert (sheet.fundal_height_cm, sheet.fetal_heart_rate, sheet.presentation) == (None, 144, "cephalic")
    assert sheet.ultrasound_notes == "مشيمة أمامية" and AntenatalVisit.objects.count() == 1
    for name, bad in [("fetal_heart_rate", "20"), ("hemoglobin", "abc"), ("presentation", "sideways"),
                      ("ga_days", "5"), ("pregnancy", "1")]:  # fmt: skip
        with pytest.raises(ValidationError):
            services.save_antenatal_field(visit, name, bad)

    # the dating changes later: the old visit keeps the age written that day, the pregnancy shows the new one
    services.set_dating(p, services.Dating(method=EddMethod.ULTRASOUND, us_date=date(2026, 6, 3), us_weeks=22))
    assert AntenatalVisit.objects.get(visit=visit).ga_label == "24+3"
    assert Pregnancy.objects.get(pk=p.pk).ga(TODAY).short_label == "23+0"


@freeze_time(NOW)
def test_no_sheet_without_a_followed_pregnancy(ob):
    visit = _visit(ob)
    with pytest.raises(ValidationError, match="مفيش حمل"):
        services.save_antenatal_field(visit, "fetal_heart_rate", "140")


# --- every screen --------------------------------------------------------------------------------------------------


@freeze_time(NOW)
def test_the_week_shows_everywhere_and_follows_a_new_dating(client, ob):
    """The owner's requirement: whoever opens the patient sees how far along she is, and one change moves it all."""
    patient = ob["patient"]
    p = _pregnant(ob)
    services.add_history(patient, outcome=Outcome.CESAREAN, by=ob["doc"], year=2022)
    visit = _visit(ob)

    def pages(user, names):
        client.force_login(user)
        return {name: client.get(url).content.decode() for name, url in names.items()}

    doctor_pages = {
        "visit": reverse("clinical:visit", args=[visit.pk]),
        "file": reverse("clinical:record", args=[patient.pk]),
        "panel": reverse("obstetrics:panel", args=[patient.pk]),
    }
    reception_pages = {
        "board": reverse("reception:today"),
        "patient": reverse("patients:detail", args=[patient.pk]),
        "list": reverse("patients:list"),
        "booking": reverse("scheduling:booking_panel") + f"?patient={patient.pk}",
    }
    for html in pages(ob["doc"], doctor_pages).values():
        assert "24 أسبوع و3 أيام" in html and "الشهر السادس" in html and "27 سبتمبر 2026" in html
    seen = pages(ob["doc"], doctor_pages)
    assert "G2 P1 A0 L1" in seen["visit"] and "قيصرية سابقة" in seen["visit"] and "الثلث الثاني" in seen["visit"]
    assert "منحنى السكر (GTT)" in seen["visit"]  # due now: named on the band
    assert "محطات فات ميعادها" in seen["visit"]  # earlier ones not ticked: one quiet count, not a red list
    assert 'id="antenatal"' in seen["visit"] and "متابعة الحمل" in seen["visit"]
    # reception: the month and the week next to her name (the patient page spells the week out, with the date)
    for name, html in pages(ob["reception"], reception_pages).items():
        assert "الشهر السادس" in html, name
        assert ("24 أسبوع و3 أيام" in html and "27 سبتمبر 2026" in html) if name == "patient" else "24+3" in html, name

    # the first scan disagrees with the period: the doctor re-dates from the pregnancy file
    client.force_login(ob["doc"])
    resp = client.post(
        reverse("obstetrics:dating", args=[p.pk]),
        {"method": "ultrasound", "us_date": "2026-06-03", "us_weeks": "22", "us_days": "0"},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200 and "ob-changed" in resp["HX-Trigger"]
    for html in pages(ob["doc"], doctor_pages).values():
        assert "23 أسبوع" in html and "24 أسبوع و3 أيام" not in html and "7 أكتوبر 2026" in html
    for name, html in pages(ob["reception"], reception_pages).items():
        assert "24+3" not in html and "24 أسبوع و3 أيام" not in html, name
        assert ("23 أسبوع" in html and "7 أكتوبر 2026" in html) if name == "patient" else "23+0" in html, name
    client.force_login(ob["doc"])
    for name in ("tag", "band"):  # what the open visit page re-reads on ob-changed
        assert "23 أسبوع" in client.get(reverse(f"obstetrics:{name}", args=[patient.pk])).content.decode()


@freeze_time(NOW)
def test_the_board_shows_the_week_of_the_booking_day_in_one_query(client, ob, django_assert_max_num_queries):
    _pregnant(ob)
    later = TODAY + timedelta(days=14)
    booking.book(patient=ob["patient"], doctor=ob["doctor"], visit_type=ob["vt"], by=ob["reception"],
                 start_at=timezone.now() + timedelta(days=14))  # fmt: skip
    client.force_login(ob["reception"])
    html = client.get(reverse("reception:today"), {"view": "day", "date": later.isoformat()}).content.decode()
    assert "26+3" in html and "الشهر السابع" in html  # how far along she will be when she comes

    women = [
        Patient.objects.create(
            organization=ob["org"],
            full_name=f"مريضة {i}",
            phone=f"+2010077700{i:02d}",
            gender=Gender.FEMALE,
            file_number=10 + i,
        )
        for i in range(12)
    ]
    for w in women:
        services.start_pregnancy(w, services.Dating(lmp=LMP), by=ob["doc"])
    with django_assert_max_num_queries(1):
        assert len(services.badges_for(ob["org"], [w.pk for w in women])) == 12


@freeze_time(NOW)
def test_reception_sees_the_week_but_nothing_of_the_file(client, ob):
    """04 §4.3 for the new layer: the tag and the expected date, never the history, blood group or risk."""
    patient = ob["patient"]
    p = _pregnant(ob)
    services.add_history(patient, outcome=Outcome.MISCARRIAGE, by=ob["doc"], year=2024, complications="نزيف")
    services.update_details(p, fetuses=1, risk_level=RiskLevel.HIGH, risk_notes="ضغط مرتفع", notes="سر طبي")
    profile = services.profile_for(patient)
    profile.blood_group = "AB-"
    profile.save()
    _visit(ob)

    client.force_login(ob["reception"])
    seen = "".join(
        client.get(url).content.decode()
        for url in (reverse("reception:today"), reverse("patients:detail", args=[patient.pk]), reverse("patients:list"))
    )
    assert "الشهر السادس" in seen and "27 سبتمبر 2026" in seen
    seen = re.sub(r"[A-Za-z0-9]{32,}", "", seen)  # security tokens are random letters: they can spell anything
    for secret in ("إجهاض", "نزيف", "ضغط مرتفع", "سر طبي", "AB-", "G2", "Rh"):
        assert secret not in seen, secret

    # every URL of the layer is the doctor's
    for pattern in get_resolver().url_patterns:
        if getattr(pattern, "app_name", "") != "obstetrics":
            continue
        for route in pattern.url_patterns:
            kwargs = {k: (p.pk if "pregnanc" in str(route.pattern) else patient.pk) for k in route.pattern.converters}
            url = reverse(f"obstetrics:{route.name}", kwargs=kwargs)
            assert client.get(url).status_code in (403, 405), route.name
            assert client.post(url).status_code == 403, route.name

    client.force_login(ob["viewer"])  # no pregnancy.view_basic: no tag at all
    assert "24+3" not in client.get(reverse("reception:today")).content.decode()


@freeze_time(NOW)
def test_another_clinic_and_a_clinic_without_the_layer(client, ob):
    patient, p = ob["patient"], _pregnant(ob)
    visit = _visit(ob)
    client.force_login(ob["other"])  # a doctor of another clinic
    for name, pk in [("panel", patient.pk), ("tag", patient.pk), ("band", patient.pk), ("antenatal", visit.pk)]:
        assert client.get(reverse(f"obstetrics:{name}", args=[pk])).status_code == 404, name
    for name, pk in [("dating", p.pk), ("close", p.pk), ("reopen", p.pk), ("transfer", p.pk), ("details", p.pk),
                     ("start", patient.pk), ("history_add", patient.pk), ("profile", patient.pk),
                     ("antenatal_field", visit.pk)]:  # fmt: skip
        assert client.post(reverse(f"obstetrics:{name}", args=[pk]), {"method": "lmp"}).status_code == 404, name
    assert Pregnancy.objects.get(pk=p.pk).is_active

    # a general clinic: the layer is nowhere, and its URLs don't exist
    OrganizationSettings.objects.filter(organization=ob["org"]).update(specialty="general")
    client.force_login(ob["doc"])
    assert client.get(reverse("obstetrics:panel", args=[patient.pk])).status_code == 404
    html = client.get(reverse("clinical:visit", args=[visit.pk])).content.decode()
    assert "ob-band" not in html and "حامل" not in html and 'id="antenatal"' not in html
    client.force_login(ob["reception"])
    assert "24+3" not in client.get(reverse("reception:today")).content.decode()
    from apps.organizations.models import Organization

    assert services.badges_for(Organization.objects.get(pk=ob["org"].pk), [patient.pk]) == {}


@freeze_time(NOW)
def test_the_doctor_runs_a_pregnancy_from_the_panel(client, ob):
    patient = ob["patient"]
    client.force_login(ob["doc"])
    post = lambda name, pk, data=None: client.post(  # noqa: E731
        reverse(f"obstetrics:{name}", args=[pk]), data or {}, HTTP_HX_REQUEST="true"
    )

    html = client.get(reverse("obstetrics:panel", args=[patient.pk])).content.decode()
    assert "تسجيل حمل جديد" in html and "مفيش حمل سابق متسجل" in html

    resp = post("start", patient.pk, {"method": "lmp", "lmp": "2026-07-01"})  # a future period: refused, form kept
    assert "المستقبل" in resp.content.decode() and "HX-Trigger" not in resp and not Pregnancy.objects.exists()
    resp = post("start", patient.pk, {"method": "lmp", "lmp": LMP.isoformat(), "cycle_days": "28", "fetuses": "2"})
    p = Pregnancy.objects.get()
    assert (p.edd, p.fetuses, p.created_by, p.doctor) == (EDD, 2, ob["doc"], ob["doctor"])
    assert "24 أسبوع و3 أيام" in resp.content.decode() and "ob-changed" in resp["HX-Trigger"]

    post("details", p.pk, {"fetuses": "2", "risk_level": "high", "risk_notes": "توأم", "notes": ""})
    post("profile", patient.pk, {"blood_group": "O-", "cycle_days": "30", "cycle_regular": "1", "contraception": "",
                                 "husband_blood_group": "", "notes": ""})  # fmt: skip
    post("history_add", patient.pk, {"past-outcome": "vaginal", "past-year": "2023", "past-baby_sex": "female"})
    p.refresh_from_db()
    profile = services.profile_for(patient)
    assert p.risk_level == "high" and (profile.blood_group, profile.cycle_days, profile.cycle_regular) == (
        "O-",
        30,
        True,
    )
    html = client.get(reverse("obstetrics:panel", args=[patient.pk])).content.decode()
    assert "G2 P1 A0 L1" in html and "Rh سالب" in html and "حقنة Anti-D" in html

    anti_d = Milestone.objects.for_org(ob["org"]).get(name="حقنة Anti-D")
    resp = client.post(reverse("obstetrics:milestone", args=[p.pk, anti_d.pk]), HTTP_HX_REQUEST="true")
    assert resp.status_code == 200 and p.milestone_checks.filter(milestone=anti_d).exists()

    visit = _visit(ob)
    resp = post("antenatal_field", visit.pk, {"field": "fetal_heart_rate", "value": "150"})
    assert "اتحفظ" in resp.content.decode() and AntenatalVisit.objects.get(visit=visit).fetal_heart_rate == 150
    resp = post("antenatal_field", visit.pk, {"field": "fetal_heart_rate", "value": "900"})
    assert "المعقول" in resp.content.decode() and AntenatalVisit.objects.get(visit=visit).fetal_heart_rate == 150
    assert "150" in client.get(reverse("obstetrics:antenatal", args=[visit.pk])).content.decode()

    row = PregnancyHistory.objects.get(year=2023)
    post("history_remove", row.pk)
    assert services.gpal(patient).label == "G1 P0 A0 L0"

    resp = post("close", p.pk, {"close-outcome": "cesarean", "close-ended_on": "2026-06-09",
                                "close-baby_weight_g": "2300", "close-place": "المركز"})  # fmt: skip
    p.refresh_from_db()
    assert p.status == "delivered" and services.gpal(patient).label == "G1 P1 A0 L2"
    html = resp.content.decode()
    assert "تسجيل حمل جديد" in html and "اتابع هنا" in html and "افتح متابعته تاني" in html
    post("reopen", p.pk)
    assert Pregnancy.objects.get(pk=p.pk).is_active and services.gpal(patient).label == "G1 P0 A0 L0"
    post("transfer", p.pk)
    assert Pregnancy.objects.get(pk=p.pk).status == "transferred"


def test_roles_and_permissions_for_the_layer(org_a, make_member):
    from apps.accounts.permissions import ROLE_PERMISSIONS, effective_permissions, with_implied
    from apps.organizations.models import Membership

    for role in ("reception", "doctor", "admin", "owner"):
        assert "pregnancy.view_basic" in ROLE_PERMISSIONS[role]
    assert "pregnancy.view_basic" not in ROLE_PERMISSIONS["viewer"]
    assert "pregnancy.view_basic" in with_implied({"clinical.view"})  # whoever opens the file sees the week
    m = Membership.objects.get(user=make_member(org_a, "reception"))
    m.revoked_permissions = ["pregnancy.view_basic"]
    assert "pregnancy.view_basic" not in effective_permissions(m)  # the owner can take it away per employee


def test_demo_data_and_seeded_clinic(db):
    out = io.StringIO()
    call_command("seed_org", stdout=out)
    call_command("seed_demo", stdout=out)
    call_command("seed_pregnancies", stdout=out)
    from apps.organizations.models import Organization

    org = Organization.objects.get(slug="demo-clinic")
    assert org.settings.specialty == "obgyn" and services.enabled(org)
    active = Pregnancy.objects.for_org(org).filter(status="active")
    assert active.count() == 8 and Milestone.objects.for_org(org).exists()
    ages = sorted(p.ga(services.today()).weeks for p in active)
    assert ages == [7, 12, 19, 24, 29, 33, 37, 40]
    call_command("seed_pregnancies", stdout=out)  # again: nothing doubled
    assert Pregnancy.objects.for_org(org).count() == 8


# --- phase 20: measurements, the clinic's own schedule, messages, the export ----------------------------------------


@freeze_time(NOW)
def test_ultrasound_measurements_are_saved_and_shown(client, ob):
    _pregnant(ob)
    visit = _visit(ob)
    for name, value in [("bpd_mm", "61"), ("hc_mm", "224"), ("ac_mm", "198"), ("fl_mm", "44")]:
        services.save_antenatal_field(visit, name, value)
    sheet = AntenatalVisit.objects.get(visit=visit)
    assert (sheet.bpd_mm, sheet.hc_mm, sheet.ac_mm, sheet.fl_mm) == (61, 224, 198, 44)
    with pytest.raises(ValidationError):
        services.save_antenatal_field(visit, "fl_mm", "200")
    client.force_login(ob["doc"])
    page = client.get(reverse("obstetrics:antenatal", args=[visit.pk])).content.decode()
    assert 'id="an-bpd_mm"' in page and 'value="61"' in page and "<details" in page and " open" in page


@freeze_time(NOW)
def test_the_clinic_edits_its_own_schedule(client, ob, make_member):
    owner = make_member(ob["org"], "owner")
    urls = [reverse("obstetrics:milestones"), reverse("obstetrics:milestone_new")]
    client.force_login(ob["doc"])
    assert [client.get(u).status_code for u in urls] == [403, 403]  # settings are the owner's and the admin's
    client.force_login(owner)
    assert "متابعة الحمل" in client.get(reverse("organizations:settings")).content.decode()
    page = client.get(urls[0]).content.decode()
    first = Milestone.objects.filter(organization=ob["org"]).order_by("from_week", "order", "id").first()
    assert first.name in page
    new = {"name": "سونار رباعي الأبعاد", "kind": "scan", "from_week": "26", "to_week": "30", "is_active": "on"}
    resp = client.post(urls[1], new)
    assert resp.status_code == 302 and Milestone.objects.filter(organization=ob["org"], name=new["name"]).exists()
    bad = client.post(urls[1], {**new, "from_week": "30", "to_week": "26"})
    assert bad.status_code == 200 and "نفس الاسم" in bad.content.decode() and "بعد أول أسبوع" in bad.content.decode()
    edit = reverse("obstetrics:milestone_edit", args=[first.pk])
    client.post(edit, {"name": first.name, "kind": first.kind, "from_week": first.from_week, "to_week": first.to_week})
    first.refresh_from_db()
    assert not first.is_active  # switched off, not deleted
    p = _pregnant(ob)
    assert first.pk not in {row.milestone.pk for row in services.milestones_for(p)}
    # another clinic cannot touch this one's rows, and a clinic without the layer has no such screen
    client.force_login(ob["other"])
    assert client.get(edit).status_code in (403, 404)
    OrganizationSettings.objects.filter(organization=ob["org"]).update(specialty="general")
    client.force_login(owner)
    assert client.get(urls[0]).status_code == 404


@freeze_time(NOW)
def test_pregnancy_messages_go_through_the_outbox_once(ob, django_capture_on_commit_callbacks):
    from apps.notifications.management.commands.seed_notifications import seed_org_notifications
    from apps.notifications.models import Event, NotificationTemplate, ScheduledMessage
    from apps.notifications.templating import appointment_ctx, render

    org, patient = ob["org"], ob["patient"]
    Patient.objects.filter(pk=patient.pk).update(messaging_consent=True)
    patient.refresh_from_db()
    seed_org_notifications(org)
    events = set(NotificationTemplate.objects.filter(organization=org).values_list("event", flat=True))
    assert {Event.MILESTONE_DUE, Event.POSTPARTUM} <= events  # an OB/GYN clinic gets the two pregnancy templates
    p = _pregnant(ob)  # 24 weeks + 3 days today

    # the week is available to every appointment message of a pregnant woman
    appt = booking.book(patient=patient, doctor=ob["doctor"], visit_type=ob["vt"], by=ob["reception"],
                        start_at=timezone.now() + timedelta(days=7), overbook=True, notify=False)  # fmt: skip
    ctx = appointment_ctx(appt)
    assert (ctx["pregnancy_week"], ctx["pregnancy_month"]) == (25, 6)  # on the day of the visit, a week from now
    assert render("أسبوع {pregnancy_week}", ctx) == "أسبوع 25"

    # what is due in her week is announced once, however often the job runs
    due = [row.milestone for row in services.milestones_for(p) if row.state == "due"]
    assert due
    assert services.notify_milestones() == len(due) and services.notify_milestones() == len(due)
    queued = ScheduledMessage.objects.filter(organization=org, event=Event.MILESTONE_DUE)
    assert queued.count() == len(due)
    first = queued.order_by("id").first()
    assert first.context["milestone_name"] in {m.name for m in due} and first.context["pregnancy_week"] == 24
    services.toggle_milestone(p, due[0], by=ob["doc"])  # done: nothing more is queued for it
    assert services.notify_milestones() == len(due) - 1

    # a birth queues the post-partum follow-up; a loss queues nothing
    with django_capture_on_commit_callbacks(execute=True):
        services.close_pregnancy(p, outcome=Outcome.VAGINAL, ended_on=TODAY, by=ob["doc"])
    row = ScheduledMessage.objects.get(organization=org, event=Event.POSTPARTUM)
    assert timezone.localtime(row.send_at).date() >= TODAY + timedelta(days=33)
    assert row.dedupe_key.startswith(f"pp:{p.pk}")
    with django_capture_on_commit_callbacks(execute=True):
        dating = services.Dating(method=EddMethod.LMP, lmp=TODAY - timedelta(days=60))
        lost = services.start_pregnancy(patient, dating, by=ob["doc"], doctor=ob["doctor"])
        services.close_pregnancy(lost, outcome=Outcome.MISCARRIAGE, ended_on=TODAY, by=ob["doc"])
    assert ScheduledMessage.objects.filter(organization=org, event=Event.POSTPARTUM).count() == 1


def test_a_general_clinic_gets_no_pregnancy_templates(org_a):
    from apps.notifications.management.commands.seed_notifications import seed_org_notifications
    from apps.notifications.models import Event, NotificationTemplate

    seed_org_notifications(org_a)
    events = set(NotificationTemplate.objects.filter(organization=org_a).values_list("event", flat=True))
    assert not {Event.MILESTONE_DUE, Event.POSTPARTUM} & events
    OrganizationSettings.objects.filter(organization=org_a).update(specialty="obgyn")
    org_a.settings.refresh_from_db()
    assert seed_org_notifications(org_a) == 2  # switched to OB/GYN later: the two are added, nothing else is touched


@freeze_time(NOW)
def test_the_exported_record_carries_the_pregnancy_file(ob):
    from apps.obstetrics.context import export_ctx

    org, patient = ob["org"], ob["patient"]
    assert export_ctx(org, patient) == {}  # nothing recorded: no empty section
    services.add_history(patient, outcome=Outcome.CESAREAN, by=ob["doc"], year=2021)
    p = _pregnant(ob)
    visit = _visit(ob)
    services.save_antenatal_field(visit, "bpd_mm", "61")
    data = export_ctx(org, patient)["ob"]
    assert (data["gpal"].label, data["pregnancy"], data["ga"].weeks, len(data["history"]), len(data["sheets"])) == (
        "G2 P1 A0 L1", p, 24, 1, 1,
    )  # fmt: skip
    man = Patient.objects.create(organization=org, full_name="أحمد", phone="+201001112223", gender=Gender.MALE,
                                 file_number=77)  # fmt: skip
    assert export_ctx(org, man) == {}
