# ruff: noqa: F811  (the `world` fixture is imported from test_prescriptions and used as a parameter)
"""Phase 21 (plan 15 §0.1, §0.2, §2.5): pregnancy / breastfeeding checks, drug-drug interactions, and the patient's
current medications."""

from datetime import date, timedelta

import pytest
from django.urls import reverse

from apps.obstetrics import services as ob
from apps.obstetrics.models import Outcome
from apps.patients import services as patient_services
from apps.patients.models import CurrentMedication, Gender
from apps.prescriptions import safety, services
from apps.prescriptions.models import Drug, PrescriptionSettings, RxStatus
from tests.test_prescriptions import _drug, _rx_with, fake_render, world  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("midday")

TODAY = date(2026, 10, 5)  # the `midday` clock


def _woman(world):
    p = world["patient"]
    p.gender = Gender.FEMALE
    p.save()
    return p


def _pregnant(world, weeks):
    patient = _woman(world)
    return ob.start_pregnancy(patient, ob.Dating(lmp=TODAY - timedelta(weeks=weeks)), by=world["doc"])


def _make_drug(world, name, generic, form="قرص"):
    return Drug.objects.create(organization=world["org"], name=name, generic_name=generic, form=form)


def _rx(world, *names):
    """The visit's draft with these lines; its patient re-read (the visit's cached copy predates `_woman`)."""
    rx = _rx_with(world, *names)
    rx.patient.refresh_from_db()
    return rx


def _kinds(rx, **kw):
    rx.patient.refresh_from_db()
    return [(w["kind"], w["level"]) for w in safety.warnings_for(rx, **kw)]


# --- pregnancy ---------------------------------------------------------------------------------------------------


def test_statin_in_pregnancy_is_red_and_blocks_finalize(world):
    _pregnant(world, 10)
    rx = _rx(world, "Lipitor 20", "Panadol 500")
    warnings = safety.warnings_for(rx)
    assert [(w["kind"], w["level"]) for w in warnings] == [("pregnancy", "avoid")]
    assert "الأسبوع 10" in warnings[0]["text"] and "Lipitor 20" in warnings[0]["text"]
    with pytest.raises(services.PrescriptionError):
        services.finalize(rx, by=world["doc"])
    assert services.finalize(rx, by=world["doc"], acknowledged=[warnings[0]["key"]]).status == RxStatus.FINAL


def test_nsaid_is_amber_early_and_red_from_20_weeks(world):
    pregnancy = _pregnant(world, 12)
    rx = _rx(world, "Brufen 400")
    assert _kinds(rx) == [("pregnancy", "caution")]
    ob.set_dating(pregnancy, ob.Dating(lmp=TODAY - timedelta(weeks=24)))
    assert _kinds(rx) == [("pregnancy", "avoid")]


def test_low_dose_aspirin_and_paracetamol_are_fine_in_pregnancy(world):
    _pregnant(world, 30)
    assert _kinds(_rx(world, "Aspocid 75", "Panadol 500", "Ferrofol")) == []


def _flags(world, drug):
    return [f["level"] for f in safety.drug_flags(drug, safety.patient_state(world["patient"]))]


def test_creams_follow_their_own_level(world):
    _pregnant(world, 8)
    assert _flags(world, _make_drug(world, "Acnelofix 20", "ISOTRETINOIN")) == ["avoid"]
    assert _flags(world, _make_drug(world, "Isotrex gel", "ISOTRETINOIN", form="جل")) == ["caution"]
    # the tetracycline rule does not apply to local forms
    assert _flags(world, _make_drug(world, "Doxy cream", "DOXYCYCLINE", form="كريم")) == []
    assert _kinds(_rx(world, "Isotrex gel")) == [("pregnancy", "caution")]


def test_terms_match_whole_ingredient_words_only(world):
    _pregnant(world, 8)
    # "valpro" must match valproate; "tetracycline" must not match inside an unrelated word
    assert _flags(world, _make_drug(world, "Depakine 500", "SODIUM VALPROATE")) == ["avoid"]
    assert _flags(world, _make_drug(world, "Odd name", "XTETRACYCLINEX")) == []


def test_free_text_line_is_checked_by_its_name(world):
    _pregnant(world, 8)
    rx = services.draft_for_visit(world["visit"])
    services.add_item(rx, drug=None, drug_name="warfarin 5mg")
    assert _kinds(rx) == [("pregnancy", "avoid")]


def test_men_and_women_without_a_followed_pregnancy_get_no_pregnancy_warning(world):
    rx = _rx(world, "Lipitor 20")
    assert _kinds(rx) == []
    _woman(world)
    rx.patient.refresh_from_db()
    assert _kinds(rx) == []


def test_search_results_carry_a_pregnancy_tag(client, world):
    _pregnant(world, 22)
    rx = services.draft_for_visit(world["visit"])
    client.force_login(world["doc"])
    html = client.get(reverse("prescriptions:drug_search", args=[rx.pk]), {"q": "Lipitor"}).content.decode()
    assert "ممنوع في الحمل" in html
    html = client.get(reverse("prescriptions:drug_search", args=[rx.pk]), {"q": "Panadol"}).content.decode()
    assert "في الحمل" not in html


def test_builder_says_the_checks_are_on(client, world):
    _pregnant(world, 22)
    client.force_login(world["doc"])
    html = client.get(reverse("prescriptions:builder", args=[world["visit"].pk])).content.decode()
    assert 'id="rx-state"' in html and "الأسبوع 22" in html


# --- breastfeeding -----------------------------------------------------------------------------------------------


def _delivered(world, days_ago, outcome=Outcome.VAGINAL):
    from apps.obstetrics.models import Pregnancy

    pregnancy = ob.start_pregnancy(_woman(world), ob.Dating(lmp=TODAY - timedelta(weeks=39)), by=world["doc"])
    ob.close_pregnancy(pregnancy, outcome=outcome, ended_on=TODAY - timedelta(days=1), by=world["doc"])
    # a birth further back than the pregnancy's own dates allow (the service refuses an end before its start)
    Pregnancy.objects.filter(pk=pregnancy.pk).update(ended_on=TODAY - timedelta(days=days_ago))


def test_breastfeeding_after_a_live_birth_for_the_set_months(world):
    _make_drug(world, "Dostinex", "CABERGOLINE")
    _delivered(world, 30)
    rx = _rx(world, "Dostinex")
    assert _kinds(rx) == [("lactation", "avoid")]
    settings = PrescriptionSettings.for_org(world["org"])
    settings.lactation_months = 0
    settings.save()
    assert _kinds(rx) == []


def test_no_breastfeeding_warning_after_a_loss_or_long_after_birth(world):
    _make_drug(world, "Dostinex", "CABERGOLINE")
    _delivered(world, 300)
    assert _kinds(_rx(world, "Dostinex")) == []


def test_no_breastfeeding_warning_after_a_stillbirth(world):
    _make_drug(world, "Dostinex", "CABERGOLINE")
    _delivered(world, 10, outcome=Outcome.STILLBIRTH)
    assert _kinds(_rx(world, "Dostinex")) == []


# --- interactions ------------------------------------------------------------------------------------------------


def test_two_lines_that_interact(world):
    _make_drug(world, "Klacid 500", "CLARITHROMYCIN")
    rx = _rx(world, "Motilium 10", "Klacid 500")
    warnings = safety.warnings_for(rx)
    assert [(w["kind"], w["level"]) for w in warnings] == [("interaction", "avoid")]
    assert "Motilium 10" in warnings[0]["text"] and "Klacid 500" in warnings[0]["text"]


def test_an_interaction_is_reported_once_whatever_the_order(world):
    _make_drug(world, "Klacid 500", "CLARITHROMYCIN")
    assert len(safety.warnings_for(_rx(world, "Klacid 500", "Motilium 10"))) == 1


def test_spacing_advice_needs_no_acknowledgement(world):
    _make_drug(world, "Calcimate", "CALCIUM CARBONATE + VITAMIN D3")
    rx = _rx(world, "Ferrofol", "Calcimate")
    warnings = safety.warnings_for(rx)
    assert [(w["kind"], w["level"], w["needs_ack"]) for w in warnings] == [("interaction", "info", False)]
    assert services.finalize(rx, by=world["doc"]).status == RxStatus.FINAL


def test_one_product_does_not_interact_with_itself(world):
    _make_drug(world, "Iron cal", "FERROUS FUMARATE + CALCIUM")
    assert _kinds(_rx(world, "Iron cal")) == []


def test_a_line_against_a_current_medication(world):
    warfarin = _make_drug(world, "Marevan 5", "WARFARIN")
    patient_services.add_medication(world["patient"], name="Marevan 5", dose="1 قرص", by=world["doc"])
    rx = _rx(world, "Brufen 400")
    warnings = safety.warnings_for(rx)
    assert [(w["kind"], w["level"]) for w in warnings] == [("interaction", "avoid")]
    assert "من الأدوية اللي بتاخدها" in warnings[0]["text"]
    assert CurrentMedication.objects.get().drug == warfarin  # linked to the catalog by its exact name


def test_two_current_medications_alone_do_not_warn(world):
    _make_drug(world, "Marevan 5", "WARFARIN")
    patient_services.add_medication(world["patient"], name="Marevan 5", by=world["doc"])
    patient_services.add_medication(world["patient"], name="Brufen 400", by=world["doc"])
    assert _kinds(_rx(world, "Panadol 500")) == []


def test_a_stopped_medication_no_longer_counts(world):
    _make_drug(world, "Marevan 5", "WARFARIN")
    med = patient_services.add_medication(world["patient"], name="Marevan 5", by=world["doc"])
    patient_services.stop_medication(med, by=world["doc"])
    assert _kinds(_rx(world, "Brufen 400")) == []
    med.refresh_from_db()
    assert med.stopped_on == TODAY and CurrentMedication.objects.count() == 1  # kept, never deleted


# --- current medications: services and screens ---------------------------------------------------------------------


def test_the_same_medicine_twice_is_refused(world):
    patient_services.add_medication(world["patient"], name="Concor 5", by=world["doc"])
    with pytest.raises(patient_services.MedicationError):
        patient_services.add_medication(world["patient"], name="concor 5", by=world["doc"])


def test_doctor_adds_and_stops_from_the_screen(client, world):
    client.force_login(world["doc"])
    p = world["patient"]
    r = client.post(reverse("patients:medication_add", args=[p.pk]), {"name": "Concor 5", "dose": "قرص الصبح"})
    assert r.status_code == 200 and r["HX-Trigger"] == "meds-changed"
    med = CurrentMedication.objects.get(patient=p)
    assert med.dose == "قرص الصبح" and med.drug is not None
    r = client.post(reverse("patients:medication_stop", args=[p.pk, med.pk]))
    assert r.status_code == 200
    med.refresh_from_db()
    assert med.stopped_on is not None
    html = client.get(reverse("patients:detail", args=[p.pk])).content.decode()
    assert "الأدوية المستمرة" in html


def test_visit_header_lists_current_medications(client, world):
    patient_services.add_medication(world["patient"], name="Concor 5", by=world["doc"])
    client.force_login(world["doc"])
    html = client.get(reverse("clinical:visit", args=[world["visit"].pk])).content.decode()
    assert 'id="header-meds"' in html and "Concor 5" in html


def test_reception_gets_403_on_medications(client, world):
    client.force_login(world["reception"])
    p = world["patient"]
    assert client.get(reverse("patients:medications", args=[p.pk])).status_code == 403
    assert client.post(reverse("patients:medication_add", args=[p.pk]), {"name": "X"}).status_code == 403
    assert client.get(reverse("patients:medication_drugs", args=[p.pk]), {"name": "Con"}).status_code == 403


def test_medications_are_per_clinic(client, world):
    med = patient_services.add_medication(world["patient"], name="Concor 5", by=world["doc"])
    client.force_login(world["other"])
    p = world["patient"]
    assert client.get(reverse("patients:medications", args=[p.pk])).status_code == 404
    assert client.post(reverse("patients:medication_stop", args=[p.pk, med.pk])).status_code == 404


def test_merging_patients_moves_current_medications(world):
    from apps.patients.models import Patient

    dup = Patient.objects.create(organization=world["org"], full_name="محمد احمد", phone="+201001234567",
                                 gender=Gender.MALE, file_number=99)  # fmt: skip
    patient_services.add_medication(dup, name="Concor 5", by=world["doc"])
    patient_services.merge_patients(world["patient"], dup)
    assert world["patient"].medications.count() == 1


# --- dates and the desk layout -------------------------------------------------------------------------------------


def test_day_filter_writes_the_month_name_in_an_rtl_isolate():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apps.core.templatetags.clock import day

    assert day(date(2026, 10, 4)) == '<bdi dir="rtl">4 أكتوبر 2026</bdi>'
    assert day(date(2026, 10, 4), "short") == '<bdi dir="rtl">4 أكتوبر</bdi>'
    # 23:30 UTC is already the next day in Cairo
    assert "5 أكتوبر" in day(datetime(2026, 10, 4, 23, 30, tzinfo=ZoneInfo("UTC")))
    assert day(None) == ""


def test_emergency_alert_never_sits_over_the_patient_header():
    from pathlib import Path

    html = Path("templates/clinical/partials/emergency_alert.html").read_text(encoding="utf-8")
    assert "top-16" not in html and "bottom-[calc(var(--action-bar-h" in html


def test_visit_page_offers_side_by_side(client, world):
    client.force_login(world["doc"])
    html = client.get(reverse("clinical:visit", args=[world["visit"].pk])).content.decode()
    assert 'id="split-toggle"' in html and 'id="panes"' in html and "--action-bar-h" in html
    assert "{#" not in html and "#}" not in html  # a template comment over two lines prints as text
