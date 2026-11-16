"""Business logic for patients: file numbers, duplicate warnings, merging."""

from django.db import transaction
from django.utils.translation import gettext as _

from apps.core.phones import to_e164

from .models import Patient, PatientSequence


class MergeError(Exception):
    pass


def next_file_number(org) -> int:
    """Must run inside `transaction.atomic()`."""
    PatientSequence.objects.get_or_create(organization=org)
    seq = PatientSequence.objects.select_for_update().get(organization=org)
    seq.last_value += 1
    seq.save(update_fields=["last_value", "updated_at"])
    return seq.last_value


@transaction.atomic
def create_patient(org, instance: Patient, *, by=None) -> Patient:
    """`instance` is an unsaved Patient (e.g. `PatientForm.save(commit=False)`)."""
    instance.organization = org
    instance.file_number = next_file_number(org)
    instance.full_clean(exclude=["file_number", "name_normalized", "organization"])
    if instance.messaging_consent and by is not None:
        instance.consent_recorded_by = by
    instance.save()
    return instance


def possible_duplicates(org, phone: str):
    """Active patients already on this phone number, for the "already exists?" warning (never blocks)."""
    try:
        e164 = to_e164(phone)
    except Exception:
        return Patient.objects.none()
    return Patient.objects.for_org(org).filter(phone=e164, is_active=True)


# Everything that hangs off a patient and simply follows her to the file that is kept (app label, model, field).
MERGE_MOVES = [
    ("scheduling", "Appointment"),
    ("notifications", "ScheduledMessage"),
    ("notifications", "InboundMessage"),
    ("clinical", "Visit"),
    ("clinical", "Attachment"),
    ("prescriptions", "Prescription"),
    ("obstetrics", "Pregnancy"),
    ("obstetrics", "PregnancyHistory"),
    ("center", "EmergencyCase"),
    ("theatre", "Operation"),
    ("theatre", "Admission"),
    ("invoicing", "Charge"),
    ("invoicing", "Invoice"),
    ("billing", "Payment"),
]


@transaction.atomic
def merge_patients(primary: Patient, duplicate: Patient, *, by=None) -> Patient:
    """Two files of the same person become one: everything on `duplicate` moves to `primary` (visits, bookings,
    prescriptions, attachments, the pregnancy file, operations, invoices and payments), and the duplicate is switched
    off and remembers where it went. Nothing is deleted.

    Refused when the two files cannot be one without losing a fact: the duplicate still owes money or has a deposit
    (her account in the books carries her own name: settle it first), or both have a pregnancy being followed, or
    both are in a room right now."""
    from django.apps import apps

    from apps.invoicing import services as invoicing
    from apps.invoicing.models import PatientCoverage
    from apps.obstetrics.models import ObstetricProfile, Pregnancy, PregnancyStatus
    from apps.theatre.models import Admission, AdmissionStatus

    if primary.organization_id != duplicate.organization_id:
        raise MergeError(_("المريضين لازم يكونوا من نفس العيادة."))
    if primary.pk == duplicate.pk:
        raise MergeError(_("مينفعش تدمج المريض في نفسه."))
    primary = Patient.objects.select_for_update().get(pk=primary.pk)
    duplicate = Patient.objects.select_for_update().get(pk=duplicate.pk)
    if duplicate.merged_into_id:
        raise MergeError(_("المريض ده متدمج بالفعل."))
    if primary.merged_into_id:
        raise MergeError(_("الملف اللي بتدمج فيه نفسه متدمج في ملف تاني."))

    owed, held = invoicing.patient_balance(duplicate), invoicing.deposit_balance(duplicate)
    if owed or held:
        raise MergeError(
            _("الملف المكرر عليه حساب مفتوح (رصيد %(o)s، مقدم %(h)s). سوّي حسابه الأول وبعدين ادمج.")
            % {"o": owed, "h": held}
        )
    following = Pregnancy.objects.filter(patient__in=(primary, duplicate), status=PregnancyStatus.ACTIVE)
    if following.count() > 1:
        raise MergeError(_("الملفين عليهم حمل متابَع. اقفل واحد منهم الأول."))
    staying = Admission.objects.filter(patient__in=(primary, duplicate), status=AdmissionStatus.ADMITTED)
    if staying.count() > 1:
        raise MergeError(_("الملفين مسجلين إقامة دلوقتي. سجّل خروج واحد منهم الأول."))

    duplicate.allergies.update(patient=primary)
    duplicate.chronic_conditions.update(patient=primary)
    for app_label, model_name in MERGE_MOVES:
        apps.get_model(app_label, model_name)._base_manager.filter(patient=duplicate).update(patient=primary)
    # one-per-patient records: the kept file's own wins; otherwise the duplicate's comes along
    for model in (ObstetricProfile, PatientCoverage):
        if not model.objects.filter(patient=primary).exists():
            model.objects.filter(patient=duplicate).update(patient=primary)

    for name in ("email", "date_of_birth", "national_id", "address", "whatsapp"):
        if hasattr(primary, name) and not getattr(primary, name) and getattr(duplicate, name, None):
            setattr(primary, name, getattr(duplicate, name))
    primary.no_show_count = (primary.no_show_count or 0) + (duplicate.no_show_count or 0)
    primary.save()
    duplicate.merged_into = primary
    duplicate.is_active = False
    duplicate.save(update_fields=["merged_into", "is_active", "updated_at"])
    return primary
