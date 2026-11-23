# ruff: noqa: F811  (the `world` fixture is imported from test_prescriptions and used as a parameter)
"""Phase 23 (plan 15 §1.4, §1.5, §1.7, §2.1, §2.3, §2.6): national ID, the waiting-room screen, families on one
number, visit templates, medical documents, past history."""

from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.center.models import CenterSettings
from apps.clinical import documents, visit_templates
from apps.clinical import services as clinical
from apps.clinical.models import DocumentKind, DocumentTemplate, MedicalDocument, VisitTemplate
from apps.core import national_id
from apps.patients import services as patient_services
from apps.patients.forms import PatientForm
from apps.patients.models import Gender, PastHistoryItem, Patient
from apps.prescriptions import services as rx_services
from apps.reception import lobby
from apps.scheduling import services as booking
from apps.scheduling.models import AppointmentStatus
from tests.test_prescriptions import _drug, fake_render, world  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("midday")

MALE_ID = "29001010123456"  # born 1 Jan 1990, Cairo (01), 13th digit 5 = male
FEMALE_ID = "30205151234461"  # born 15 May 2002, Dakahlia (12), 13th digit 6 = female


# --- national ID -------------------------------------------------------------------------------------------------


def test_national_id_reads_birth_date_gender_and_governorate():
    nid = national_id.parse(MALE_ID)
    assert (nid.birth_date, nid.gender, nid.governorate) == (date(1990, 1, 1), "male", "القاهرة")
    nid = national_id.parse("٣٠٢٠٥١٥١٢٣٤٤٦١")  # Arabic digits
    assert (nid.birth_date, nid.gender, nid.governorate) == (date(2002, 5, 15), "female", "الدقهلية")


@pytest.mark.parametrize("raw", ["123", "19001010123456", "29013010123456", "29001019923456", "39901010123456"])
def test_impossible_national_ids_are_refused(raw):
    with pytest.raises(national_id.InvalidNationalId):
        national_id.parse(raw, today=date(2026, 10, 5))


def _form(org, **data):
    base = {"full_name": "منى سعيد", "phone": "01001112223", "preferred_channel": "whatsapp",
            "whatsapp_same_as_phone": "on"}  # fmt: skip
    return PatientForm({**base, **data}, org=org)


def test_the_form_fills_birth_date_and_gender_from_the_id(org_a):
    form = _form(org_a, national_id=FEMALE_ID)
    assert form.is_valid(), form.errors
    patient = patient_services.create_patient(org_a, form.save(commit=False))
    assert (patient.gender, patient.date_of_birth, patient.dob_is_estimated) == ("female", date(2002, 5, 15), False)
    assert patient in Patient.objects.for_org(org_a).search(FEMALE_ID)


def test_the_id_must_agree_with_what_was_typed_and_be_unique(org_a):
    assert not _form(org_a, national_id=FEMALE_ID, gender=Gender.MALE).is_valid()
    assert not _form(org_a, national_id=FEMALE_ID, gender="female", date_of_birth="2001-01-01").is_valid()
    form = _form(org_a, national_id=FEMALE_ID)
    assert form.is_valid()
    patient_services.create_patient(org_a, form.save(commit=False))
    again = _form(org_a, national_id=FEMALE_ID, full_name="ملف تاني")
    assert not again.is_valid() and "مسجل لملف تاني" in str(again.errors)
    assert not _form(org_a).is_valid()  # no gender and no ID


def test_quick_add_in_booking_takes_the_id(client, world):
    from apps.patients.models import PatientSequence

    # the fixture's patient took file number 1 without the sequence
    PatientSequence.objects.update_or_create(organization=world["org"], defaults={"last_value": 10})
    client.force_login(world["reception"])
    r = client.post(reverse("scheduling:booking_new_patient"), {
        "full_name": "هدى علي", "phone": "01002223334", "national_id": FEMALE_ID, "whatsapp_same_as_phone": "on",
        "preferred_channel": "whatsapp",
    }, HTTP_HX_REQUEST="true")  # fmt: skip
    assert r.status_code == 200
    assert Patient.objects.get(national_id=FEMALE_ID).gender == "female"


# --- families on one number --------------------------------------------------------------------------------------


def test_booking_search_shows_files_sharing_a_number(client, world):
    org = world["org"]
    for i, name in enumerate(["سارة محمود", "ياسين محمود"]):
        Patient.objects.create(organization=org, full_name=name, phone="+201009990000", gender="female",
                               file_number=100 + i, date_of_birth=date(2000 + i * 18, 1, 1))  # fmt: skip
    client.force_login(world["reception"])
    html = client.get(reverse("scheduling:booking_search"), {"q": "01009990000"}).content.decode()
    assert html.count("نفس الرقم") == 2 and "سنة" in html


# --- past history ------------------------------------------------------------------------------------------------


def test_past_history_lines(client, world):
    p = world["patient"]
    client.force_login(world["doc"])
    add = reverse("patients:past_history_add", args=[p.pk])
    r = client.post(add, {"kind": "surgical", "text": "قيصرية", "year": 2019})
    assert r.status_code == 200 and "قيصرية" in r.content.decode() and r["HX-Trigger"] == "history-changed"
    item = PastHistoryItem.objects.get()
    client.post(reverse("patients:past_history_remove", args=[p.pk, item.pk]))
    item.refresh_from_db()
    assert item.is_removed and PastHistoryItem.objects.count() == 1  # hidden, never deleted
    r = client.post(add, {"kind": "surgical", "text": "x", "year": 1700})
    assert "السنة مش منطقية" in r.content.decode()


def test_past_history_permissions(client, world):
    p = world["patient"]
    client.force_login(world["reception"])
    assert client.get(reverse("patients:past_history", args=[p.pk])).status_code == 403
    client.force_login(world["other"])
    assert client.get(reverse("patients:past_history", args=[p.pk])).status_code == 404


# --- visit templates ---------------------------------------------------------------------------------------------


def _fill(world):
    visit = world["visit"]
    clinical.save_field(visit, "chief_complaint", "متابعة حمل", by=world["doc"])
    clinical.save_field(visit, "plan", "فوليك أسيد", by=world["doc"])
    clinical.save_field(visit, "followup_after_days", "30", by=world["doc"])
    rx = rx_services.draft_for_visit(visit)
    rx_services.add_item(rx, drug=_drug(world, "Ferrofol"), instructions="كبسولة بعد الغدا")
    rx_services.add_request(rx, "CBC")
    return visit


def _second_visit(world):
    appt = booking.book(patient=world["patient"], doctor=world["doctor"], visit_type=world["vt"], by=world["reception"],
                        start_at=timezone.now() + timedelta(hours=3), overbook=True)  # fmt: skip
    booking.transition(appt, AppointmentStatus.ARRIVED, by=world["reception"])
    return clinical.start_visit(appt, by=world["doc"])


def test_save_a_visit_as_template_and_apply_it(world):
    tpl = visit_templates.save_from_visit(_fill(world), "متابعة حمل أولى", by=world["doc"])
    assert tpl.fields == {"chief_complaint": "متابعة حمل", "plan": "فوليك أسيد", "followup_after_days": "30"}
    assert [i["drug_name"] for i in tpl.rx_items] == ["Ferrofol"] and tpl.requests[0]["name"] == "CBC"
    visit = _second_visit(world)
    clinical.save_field(visit, "chief_complaint", "صداع", by=world["doc"])
    done = visit_templates.apply(visit, tpl, by=world["doc"])
    visit.refresh_from_db()
    assert visit.chief_complaint == "صداع\nمتابعة حمل"  # what was written stays, the template's text is added
    assert visit.plan == "فوليك أسيد" and visit.followup_after_days == 30
    rx = rx_services.current_for_visit(visit)
    assert [i.drug_name for i in rx.items.all()] == ["Ferrofol"] and [r.name for r in rx.requests.all()] == ["CBC"]
    assert set(done) >= {"chief_complaint", "plan", "prescription", "requests"}
    visit_templates.apply(visit, tpl, by=world["doc"])  # twice: nothing doubled
    visit.refresh_from_db()
    assert visit.chief_complaint == "صداع\nمتابعة حمل" and rx.items.count() == 1 and rx.requests.count() == 1
    tpl.refresh_from_db()
    assert tpl.usage_count == 2


def test_template_screens(client, world):
    _fill(world)
    client.force_login(world["doc"])
    r = client.post(reverse("clinical:visit_template_save", args=[world["visit"].pk]), {"name": "متابعة"})
    assert r.status_code == 200 and "اتحفظ القالب" in r.content.decode()
    tpl = VisitTemplate.objects.get()
    visit = _second_visit(world)
    html = client.get(reverse("clinical:visit", args=[visit.pk])).content.decode()
    assert 'id="visit-templates"' in html and "متابعة" in html
    r = client.post(reverse("clinical:visit_template_apply", args=[visit.pk, tpl.pk]))
    assert r.status_code == 204 and r["HX-Refresh"] == "true"
    client.post(reverse("clinical:visit_template_remove", args=[visit.pk, tpl.pk]))
    tpl.refresh_from_db()
    assert not tpl.is_active
    assert visit_templates.TemplateError  # an empty visit cannot be a template
    with pytest.raises(visit_templates.TemplateError):
        visit_templates.save_from_visit(_second_visit(world), "فاضي")


# --- medical documents -------------------------------------------------------------------------------------------


@pytest.fixture
def fake_pdf(monkeypatch):
    monkeypatch.setattr(documents, "render_pdf", lambda doc: b"%PDF-1.4 doc")


def test_sick_leave_is_filled_numbered_and_fixed(world):
    visit = world["visit"]
    clinical.save_field(visit, "diagnosis", "نزلة برد", by=world["doc"])
    visit.refresh_from_db()
    d = documents.draft(visit.patient, DocumentKind.SICK_LEAVE, visit=visit, days=3, from_date=date(2026, 10, 5))
    assert "نزلة برد" in d.body and "3 يوم" in d.body and "5 أكتوبر 2026" in d.body and "7 أكتوبر 2026" in d.body
    first = documents.issue(visit.patient, kind=d.kind, title=d.title, body=d.body, doctor=visit.doctor,
                            by=world["doc"], visit=visit, days=3, from_date=date(2026, 10, 5))  # fmt: skip
    second = documents.issue(visit.patient, kind=DocumentKind.REFERRAL, title="تحويل", body="برجاء الكشف",
                             doctor=visit.doctor, by=world["doc"], visit=visit, days=5)  # fmt: skip
    assert (first.number, second.number) == ("DOC-2026-00001", "DOC-2026-00002")
    assert first.to_date == date(2026, 10, 7) and second.days is None  # days only mean something on a sick leave
    with pytest.raises(documents.DocumentError):
        documents.issue(visit.patient, kind=DocumentKind.SICK_LEAVE, title="x", body="y", doctor=visit.doctor,
                        by=world["doc"], days=120)  # fmt: skip
    with pytest.raises(documents.DocumentError):
        documents.void(first, reason="", by=world["doc"])
    documents.void(first, reason="التاريخ غلط", by=world["doc"])
    first.refresh_from_db()
    assert first.is_void and MedicalDocument.objects.count() == 2


def test_document_screens(client, world, fake_pdf):
    client.force_login(world["doc"])
    visit = world["visit"]
    html = client.get(reverse("clinical:document_new", args=[visit.pk]), {"kind": "medical_report"}).content.decode()
    assert "تقرير طبي" in html and 'name="body"' in html
    data = {"kind": "medical_report", "title": "تقرير طبي", "body": "النص", "recipient": "لمن يهمه الأمر"}
    r = client.post(reverse("clinical:document_issue", args=[visit.pk]), data)
    assert r.status_code == 200 and "DOC-2026-00001" in r.content.decode()
    doc = MedicalDocument.objects.get()
    r = client.get(reverse("clinical:document_pdf", args=[doc.pk]))
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
    assert "DOC-2026-00001" in client.get(reverse("clinical:documents", args=[visit.patient.pk])).content.decode()
    client.force_login(world["reception"])
    assert client.get(reverse("clinical:document_pdf", args=[doc.pk])).status_code == 403
    client.force_login(world["other"])
    assert client.get(reverse("clinical:document_pdf", args=[doc.pk])).status_code == 404


def test_the_real_pdf_renders(world):
    from apps.documents import pdf as engine

    if not engine._browser_candidates():
        pytest.skip("no Chromium")
    visit = world["visit"]
    doc = documents.issue(visit.patient, kind=DocumentKind.SICK_LEAVE, title="إجازة مرضية", body="نص الإجازة",
                          doctor=visit.doctor, by=world["doc"], visit=visit, days=2)  # fmt: skip
    assert documents.render_pdf(doc).startswith(b"%PDF")


def test_document_wording_is_the_clinics(client, world, make_member):
    client.force_login(make_member(world["org"], "owner"))
    assert client.get(reverse("clinical:document_templates")).status_code == 200
    r = client.post(reverse("clinical:document_templates"),
                    {"kind": "referral", "title": "تحويل", "body": "الزميل {recipient}، {unknown}"})  # fmt: skip
    assert "متغيرات مش معروفة" in r.content.decode()
    client.post(
        reverse("clinical:document_templates"), {"kind": "referral", "title": "تحويل", "body": "الزميل {recipient}"}
    )
    assert DocumentTemplate.objects.get(organization=world["org"], kind="referral").body == "الزميل {recipient}"
    d = documents.draft(world["patient"], DocumentKind.REFERRAL, recipient="د. أحمد")
    assert d.body == "الزميل د. أحمد"


# --- the waiting-room screen -----------------------------------------------------------------------------------


def test_lobby_shows_who_is_inside_and_next(client, world):
    org = world["org"]
    inside = world["visit"].appointment  # started by the fixture: in consultation
    waiting = Patient.objects.create(organization=org, full_name="منى إبراهيم السيد", phone="+201001110000",
                                     gender="female", file_number=50)  # fmt: skip
    appt = booking.book(patient=waiting, doctor=world["doctor"], visit_type=world["vt"], by=world["reception"],
                        start_at=timezone.now() + timedelta(hours=2), overbook=True)  # fmt: skip
    booking.transition(appt, AppointmentStatus.ARRIVED, by=world["reception"])
    state = lobby.state(org)
    line = state.lines[0]
    assert line.inside.name == "محمد أ." and [e.name for e in line.next] == ["منى إ."]
    assert state.call_sig.startswith(f"{inside.pk}:")
    CenterSettings.objects.filter(organization=org).update(lobby_show_names=False)
    assert lobby.state(org).lines[0].inside.name == ""
    client.force_login(world["reception"])
    assert client.get(reverse("reception:lobby")).status_code == 200
    assert "منى" not in client.get(reverse("reception:lobby_board")).content.decode()  # names off
    client.logout()
    assert client.get(reverse("reception:lobby")).status_code == 302
