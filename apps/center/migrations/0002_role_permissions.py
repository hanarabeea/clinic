from django.db import migrations

NEW = ["emergency.register", "service.order"]


def roles_get_center_permissions(apps, schema_editor):
    """The two permissions of center mode go to the clinic roles that started as reception / doctor, like the
    built-in sets. Roles the clinic made itself are left for the owner to decide."""
    ClinicRole = apps.get_model("organizations", "ClinicRole")
    for role in ClinicRole.objects.filter(key__in=["reception", "doctor"]):
        perms = set(role.permissions or [])
        if not set(NEW) <= perms:
            role.permissions = sorted(perms | set(NEW))
            role.save(update_fields=["permissions"])


class Migration(migrations.Migration):
    dependencies = [
        ("center", "0001_initial"),
        ("organizations", "0006_specialty"),
    ]

    operations = [
        migrations.RunPython(roles_get_center_permissions, migrations.RunPython.noop),
    ]
