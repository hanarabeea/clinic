"""Roles become the clinic's own (ClinicRole): every org gets editable copies of the built-in roles, and every
non-owner member / pending invitation moves onto the matching one. The permission lists are frozen here as they were
on 2026-10-25 (apps/accounts/roles.DEFAULT_ROLES keeps the live copy for new clinics)."""

from django.db import migrations

DEFAULTS = [
    (
        "admin",
        "مدير النظام",
        "المستخدمين والإعدادات وسجل النشاط والمواعيد والمرضى — من غير الملف الطبي",
        [
            "appointment.book",
            "appointment.cancel",
            "appointment.reschedule",
            "appointment.view",
            "audit.view",
            "messages.retry",
            "messages.view",
            "patient.edit_basic",
            "patient.edit_medical",
            "patient.merge",
            "patient.view_basic",
            "patient.view_medical",
            "schedule.manage",
            "settings.manage",
            "user.manage",
        ],
    ),
    (
        "doctor",
        "دكتور",
        "الكشف والروشتة والملف الطبي والمواعيد والاستقبال والتقارير",
        [
            "appointment.book",
            "appointment.cancel",
            "appointment.overbook",
            "appointment.reschedule",
            "appointment.view",
            "attachment.upload",
            "clinical.edit",
            "clinical.view",
            "drug.manage",
            "messages.retry",
            "messages.view",
            "patient.edit_basic",
            "patient.edit_medical",
            "patient.merge",
            "patient.view_basic",
            "patient.view_medical",
            "payment.record",
            "prescription.print",
            "prescription.write",
            "reception.operate",
            "report.finance",
            "rx_template.manage",
            "schedule.manage",
            "vitals.record",
        ],
    ),
    (
        "reception",
        "استقبال",
        "الحجز والاستقبال والخزنة وبيانات المرضى — من غير الملف الطبي",
        [
            "appointment.book",
            "appointment.cancel",
            "appointment.reschedule",
            "appointment.view",
            "attachment.upload",
            "messages.retry",
            "messages.view",
            "patient.edit_basic",
            "patient.view_basic",
            "payment.record",
            "reception.operate",
            "vitals.record",
        ],
    ),
    ("viewer", "مشاهد", "يشوف المواعيد بس", ["appointment.view"]),
]


def forwards(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    ClinicRole = apps.get_model("organizations", "ClinicRole")
    Membership = apps.get_model("organizations", "Membership")
    Invitation = apps.get_model("accounts", "Invitation")
    for org in Organization.objects.all():
        if ClinicRole.objects.filter(organization=org).exists():
            continue
        by_key = {}
        for i, (key, name, desc, perms) in enumerate(DEFAULTS):
            by_key[key] = ClinicRole.objects.create(
                organization=org, key=key, name=name, description=desc, permissions=perms, order=i
            )
        for m in Membership.objects.filter(organization=org).exclude(role="owner"):
            if m.role in by_key:
                m.clinic_role, m.role = by_key[m.role], "member"
                m.save(update_fields=["clinic_role", "role"])
        for inv in Invitation.objects.filter(organization=org, clinic_role__isnull=True).exclude(role="owner"):
            if inv.role in by_key:
                inv.clinic_role, inv.role = by_key[inv.role], "member"
                inv.save(update_fields=["clinic_role", "role"])


def backwards(apps, schema_editor):
    Membership = apps.get_model("organizations", "Membership")
    Invitation = apps.get_model("accounts", "Invitation")
    for model in (Membership, Invitation):
        for row in model.objects.filter(role="member").exclude(clinic_role__isnull=True).select_related("clinic_role"):
            row.role = row.clinic_role.key or "viewer"
            row.save(update_fields=["role"])


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_invitation_clinic_role"),
        ("organizations", "0003_clinic_roles"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
