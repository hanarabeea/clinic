"""Medical documents (plan 15 §2.3): sick leave, medical report, referral letter, certificate, ultrasound report and
visit summary, in one click from the visit page.

`draft()` fills the clinic's wording of that kind (`DocumentTemplate`, seeded from `DEFAULTS`) with the patient and
the visit; the doctor edits the text, then `issue()` numbers it (`DOC-2026-00001`, per year, under a lock) and fixes
it. A mistake is `void()`ed with a reason, never edited or deleted. `render_pdf()` prints it with the clinic's header.
"""

import string
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.timeutils import CAIRO

from .models import DocumentKind, DocumentSequence, DocumentTemplate, MedicalDocument

# Optional values sit on their own "label: {value}" line: a line whose value is blank is dropped (render_body).
DEFAULTS = {
    DocumentKind.SICK_LEAVE: (
        "إجازة مرضية",
        "أشهد أنا {doctor_name} بأن السيد/ة {patient_name} كان/ت تحت الكشف والعلاج بتاريخ {date}.\n"
        "السن: {age}\nالرقم القومي: {national_id}\nالتشخيص: {diagnosis}\n"
        "ويحتاج/تحتاج إلى راحة لمدة {days} يوم، من {from_date} إلى {to_date}.\n"
        "وقد أعطيت له/ا هذه الشهادة بناءً على طلبه/ا لتقديمها لمن يهمه الأمر.",
    ),
    DocumentKind.MEDICAL_REPORT: (
        "تقرير طبي",
        "السيد/ة {patient_name} تم الكشف عليه/ا بتاريخ {date}.\nالسن: {age}\n"
        "الشكوى: {chief_complaint}\nالفحص: {examination}\nالتشخيص: {diagnosis}\nالخطة: {plan}",
    ),
    DocumentKind.REFERRAL: (
        "خطاب تحويل",
        "الزميل العزيز {recipient}،\nتحية طيبة وبعد،\n"
        "برجاء التكرم بالكشف على السيد/ة {patient_name}.\nالسن: {age}\n"
        "الشكوى: {chief_complaint}\nالتشخيص المبدئي: {diagnosis}\n"
        "وشكرًا لحسن تعاونكم.",
    ),
    DocumentKind.CERTIFICATE: (
        "شهادة طبية",
        "أشهد أنا {doctor_name} بأن السيدة {patient_name} تمت متابعتها بالعيادة بتاريخ {date}.\nالسن: {age}\n"
        "{pregnancy_line}\n"
        "وقد أعطيت لها هذه الشهادة بناءً على طلبها لتقديمها لمن يهمه الأمر.",
    ),
    DocumentKind.ULTRASOUND: (
        "تقرير سونار",
        "المريضة: {patient_name}\nالسن: {age}\nالتاريخ: {date}\n{pregnancy_line}\n{ultrasound}\nالانطباع: {diagnosis}",
    ),
    DocumentKind.VISIT_SUMMARY: (
        "ملخص زيارة",
        "الزيارة بتاريخ {date} مع {doctor_name}.\n"
        "الشكوى: {chief_complaint}\nالتشخيص: {diagnosis}\nالخطة: {plan}\n{followup_line}",
    ),
}
VARIABLES = (
    "patient_name", "age", "national_id", "file_number", "doctor_name", "clinic_name", "date", "chief_complaint",
    "examination", "diagnosis", "plan", "days", "from_date", "to_date", "recipient", "pregnancy_line", "ultrasound",
    "followup_line",
)  # fmt: skip
MAX_LEAVE_DAYS = 90


class DocumentError(Exception):
    pass


def today() -> date:
    return timezone.localtime(timezone.now(), CAIRO).date()


def fmt(d) -> str:
    from django.utils import dateformat

    return dateformat.format(d, "j F Y") if d else ""


def ensure_templates(org) -> dict:
    """The clinic's wording of every kind (created from DEFAULTS the first time; edited ones are kept)."""
    have = {t.kind: t for t in DocumentTemplate.objects.filter(organization=org)}
    for kind, (title, body) in DEFAULTS.items():
        if kind not in have:
            have[kind] = DocumentTemplate.objects.create(organization=org, kind=kind, title=title, body=body)
    return have


class _Blank(dict):
    def __missing__(self, key):
        return ""


def validate_body(body: str) -> None:
    try:
        names = {f for _lit, f, _spec, _conv in string.Formatter().parse(body) if f is not None}
    except ValueError as e:
        raise DocumentError(_("فيه قوس { أو } مش مقفول في النص.")) from e
    unknown = sorted(n for n in names if n not in VARIABLES)
    if unknown:
        raise DocumentError(_("متغيرات مش معروفة: %(v)s") % {"v": "، ".join("{" + n + "}" for n in unknown)})


def context(patient, visit=None, doctor=None, *, days=None, from_date=None, recipient="") -> dict:
    from apps.obstetrics import services as ob

    doctor = doctor or (visit.doctor if visit else None)
    org_settings = getattr(patient.organization, "settings", None)
    on = timezone.localtime(visit.created_at, CAIRO).date() if visit else today()
    ctx = {
        "patient_name": patient.full_name, "age": patient.age if patient.age is not None else "",
        "national_id": patient.national_id, "file_number": patient.file_number,
        "doctor_name": str(doctor) if doctor else "", "date": fmt(on),
        "clinic_name": (org_settings.clinic_name_ar if org_settings else "") or patient.organization.name_ar,
        "recipient": recipient or _("لمن يهمه الأمر"),
    }  # fmt: skip
    if visit is not None:
        for field in ("chief_complaint", "examination", "diagnosis", "plan"):
            ctx[field] = (getattr(visit, field) or "").strip()
        if visit.followup_after_days:
            ctx["followup_line"] = _("الإعادة بعد %(n)s يوم.") % {"n": visit.followup_after_days}
    if days:
        start = from_date or on
        ctx.update(days=days, from_date=fmt(start), to_date=fmt(start + timedelta(days=days - 1)))
    pregnancy = ob.active_pregnancy(patient) if patient.gender == "female" else None
    if pregnancy is not None:
        age = pregnancy.ga(on)
        ctx["pregnancy_line"] = _("حامل في الأسبوع %(w)s، والولادة المتوقعة %(d)s.") % {
            "w": age.weeks, "d": fmt(pregnancy.edd),
        }  # fmt: skip
        if visit is not None:
            sheet = getattr(visit, "antenatal", None)
            if sheet is not None:
                parts = [
                    f"{label}: {value}"
                    for label, value in (
                        ("EFW", f"{sheet.efw_g} g" if sheet.efw_g else ""), ("FHR", sheet.fetal_heart_rate or ""),
                        ("AFI", f"{sheet.afi_cm} cm" if sheet.afi_cm else ""), ("BPD", sheet.bpd_mm or ""),
                        ("FL", sheet.fl_mm or ""), (_("المشيمة"), sheet.placenta),
                        (_("وضع الجنين"), sheet.get_presentation_display() if sheet.presentation else ""),
                    )
                    if value
                ]  # fmt: skip
                if sheet.ultrasound_notes:
                    parts.append(sheet.ultrasound_notes)
                ctx["ultrasound"] = "\n".join(str(p) for p in parts)
    return ctx


def render_body(body: str, ctx: dict) -> str:
    text = string.Formatter().vformat(body, (), _Blank(ctx))
    lines = [line.rstrip() for line in text.splitlines()]
    out = []
    for line in lines:  # a line left empty by a blank variable is dropped
        if line.strip(" :،.") and not line.strip().endswith(":"):
            out.append(line)
    return "\n".join(out).strip()


@dataclass
class Draft:
    kind: str
    title: str
    body: str
    recipient: str = ""
    days: int | None = None
    from_date: date | None = None


def draft(patient, kind, *, visit=None, doctor=None, days=None, from_date=None, recipient="") -> Draft:
    if kind not in DocumentKind.values:
        raise DocumentError(_("نوع مستند مش معروف."))
    tpl = ensure_templates(patient.organization)[kind]
    if kind == DocumentKind.SICK_LEAVE and not days:
        days = 3
    ctx = context(patient, visit, doctor, days=days, from_date=from_date, recipient=recipient)
    return Draft(kind=kind, title=tpl.title, body=render_body(tpl.body, ctx), recipient=recipient, days=days,
                 from_date=from_date or (today() if days else None))  # fmt: skip


def _next_number(org, year) -> str:
    DocumentSequence.objects.get_or_create(organization=org, year=year)
    seq = DocumentSequence.objects.select_for_update().get(organization=org, year=year)
    seq.last += 1
    seq.save(update_fields=["last", "updated_at"])
    return f"DOC-{year}-{seq.last:05d}"


@transaction.atomic
def issue(patient, *, kind, title, body, doctor, by, visit=None, recipient="", days=None, from_date=None):
    if kind not in DocumentKind.values:
        raise DocumentError(_("نوع مستند مش معروف."))
    title, body = " ".join((title or "").split())[:150], (body or "").strip()
    if not title or not body:
        raise DocumentError(_("العنوان والنص مطلوبين."))
    if visit is not None and visit.patient_id != patient.pk:
        raise DocumentError(_("الزيارة دي مش لنفس المريض."))
    to_date = None
    if kind == DocumentKind.SICK_LEAVE:
        if not days or not 1 <= days <= MAX_LEAVE_DAYS:
            raise DocumentError(_("عدد أيام الإجازة لازم يكون من 1 لـ%(n)s.") % {"n": MAX_LEAVE_DAYS})
        from_date = from_date or today()
        to_date = from_date + timedelta(days=days - 1)
    else:
        days = from_date = None
    now = timezone.now()
    return MedicalDocument.objects.create(
        organization=patient.organization, patient=patient, visit=visit, doctor=doctor, kind=kind,
        number=_next_number(patient.organization, timezone.localtime(now, CAIRO).year), title=title,
        recipient=(recipient or "").strip()[:200], body=body, days=days, from_date=from_date, to_date=to_date,
        issued_at=now, issued_by=by,
    )  # fmt: skip


@transaction.atomic
def void(doc, *, reason, by=None):
    doc = MedicalDocument.objects.select_for_update().get(pk=doc.pk)
    reason = (reason or "").strip()
    if doc.is_void:
        raise DocumentError(_("المستند ده اتلغى قبل كده."))
    if not reason:
        raise DocumentError(_("اكتب سبب الإلغاء."))
    doc.is_void, doc.void_reason, doc.voided_at = True, reason[:200], timezone.now()
    doc.save(update_fields=["is_void", "void_reason", "voided_at", "updated_at"])
    return doc


def render_html(doc) -> str:
    from apps.documents.pdf import font_css, logo_uri

    org_settings = getattr(doc.organization, "settings", None)
    return render_to_string("pdf/medical_document.html", {
        "doc": doc, "patient": doc.patient, "doctor": doc.doctor, "org": doc.organization, "settings": org_settings,
        "font_css": font_css(), "logo_uri": logo_uri(org_settings),
        "issued": fmt(timezone.localtime(doc.issued_at, CAIRO)),
        "from_date": fmt(doc.from_date), "to_date": fmt(doc.to_date),
    })  # fmt: skip


def render_pdf(doc) -> bytes:
    from apps.documents.pdf import render_pdf as engine

    return engine(render_html(doc), format="A4", print_background=True)
