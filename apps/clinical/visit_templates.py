"""Visit templates (plan 15 §2.1): one click fills the visit, the prescription, the investigations and the follow-up.

`save_from_visit` keeps what the doctor wrote in a visit (only the filled fields, the draft or final prescription's
lines, its requests and advice); `apply` puts it into another visit the same way "استخدم الزيارة اللي فاتت" does:
an empty field is filled, a field already written keeps its text and gets the template's added below it (never twice),
prescription lines already there are skipped. Nothing is removed by applying a template.
"""

from django.db import transaction
from django.db.models import F
from django.utils.translation import gettext as _

from . import services
from .models import VisitTemplate
from .workspace import REUSABLE

MAX_PER_DOCTOR = 40


class TemplateError(Exception):
    pass


def for_doctor(doctor):
    return VisitTemplate.objects.filter(doctor=doctor, is_active=True)


def _prescription(visit):
    """The prescription this visit carries: the open draft, else its last final one (None without any)."""
    from apps.prescriptions.models import Prescription, RxStatus

    rx = Prescription.objects.filter(visit=visit).exclude(status=RxStatus.VOIDED).order_by("-created_at").first()
    return rx


@transaction.atomic
def save_from_visit(visit, name: str, *, by=None) -> VisitTemplate:
    name = " ".join((name or "").split())[:100]
    if not name:
        raise TemplateError(_("اكتب اسم للقالب."))
    if for_doctor(visit.doctor).count() >= MAX_PER_DOCTOR:
        raise TemplateError(_("عندك %(n)s قالب. شيل قالب قديم الأول.") % {"n": MAX_PER_DOCTOR})
    fields = {}
    for field in REUSABLE:
        value = getattr(visit, field)
        if value not in ("", None):
            fields[field] = value if isinstance(value, str) else str(value)
    rx = _prescription(visit)
    rx_items, requests, advice = [], [], ""
    if rx is not None:
        rx_items = [
            {"drug": it.drug_id, "drug_name": it.drug_name, "form": it.form, "instructions": it.instructions,
             "duration": it.duration}
            for it in rx.items.order_by("order", "id")
        ]  # fmt: skip
        requests = [{"name": r.name, "kind": r.kind} for r in rx.requests.order_by("order", "id")]
        advice = rx.advice or ""
    if not (fields or rx_items or requests):
        raise TemplateError(_("الكشف فاضي، مفيش حاجة تتحفظ."))
    existing = for_doctor(visit.doctor).filter(name=name).first()
    if existing is not None:  # same name: the doctor is updating their template
        existing.fields, existing.rx_items, existing.requests, existing.advice = fields, rx_items, requests, advice
        existing.save()
        return existing
    return VisitTemplate.objects.create(
        organization=visit.organization, doctor=visit.doctor, name=name, fields=fields, rx_items=rx_items,
        requests=requests, advice=advice,
    )  # fmt: skip


@transaction.atomic
def apply(visit, template: VisitTemplate, *, by=None, with_prescription=True) -> list[str]:
    """Returns what was filled (field names, "prescription", "requests")."""
    from apps.prescriptions import services as rx_services
    from apps.prescriptions.models import Drug

    if template.doctor_id != visit.doctor_id or not template.is_active:
        raise TemplateError(_("القالب ده مش للدكتور ده."))
    done = []
    visit.refresh_from_db()
    for field, text in (template.fields or {}).items():
        if field not in REUSABLE or text in ("", None):
            continue
        current = getattr(visit, field)
        if field == "followup_after_days":
            if current:
                continue
            value = str(text)
        elif not (current or "").strip():
            value = text
        elif text.strip() in current:
            continue
        else:
            value = f"{current.rstrip()}\n{text}"
        services.save_field(visit, field, value, by=by)
        done.append(field)
    if with_prescription and (template.rx_items or template.requests or template.advice):
        draft = rx_services.current_for_visit(visit)
        if draft.is_editable:
            have = {it.drug_name.strip().lower() for it in draft.items.all()}
            wanted = [i.get("drug") for i in template.rx_items if i.get("drug")]
            drugs = {d.pk: d for d in Drug.objects.filter(organization=visit.organization, pk__in=wanted)}
            added = 0
            for item in template.rx_items:
                if item.get("drug_name", "").strip().lower() in have:
                    continue
                rx_services.add_item(
                    draft, drug=drugs.get(item.get("drug")), drug_name=item.get("drug_name", ""),
                    instructions=item.get("instructions", ""), duration=item.get("duration", ""),
                    form=item.get("form") or None,
                )  # fmt: skip
                added += 1
            if added:
                done.append("prescription")
            asked = {r.name.lower() for r in draft.requests.all()}
            new_requests = [r for r in template.requests if r.get("name", "").lower() not in asked]
            for r in new_requests:
                rx_services.add_request(draft, r["name"], kind=r.get("kind") or None)
            if new_requests:
                done.append("requests")
            if template.advice and not (draft.advice or "").strip():
                draft.advice = template.advice
                draft.save(update_fields=["advice", "updated_at"])
    VisitTemplate.objects.filter(pk=template.pk).update(usage_count=F("usage_count") + 1)
    return done


def remove(template: VisitTemplate) -> None:
    template.is_active = False
    template.save(update_fields=["is_active", "updated_at"])
