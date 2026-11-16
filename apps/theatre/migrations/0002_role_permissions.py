from django.db import migrations

RECEPTION = ["theatre.view", "theatre.schedule", "admission.manage"]
DOCTOR = [*RECEPTION, "theatre.operate"]


def roles_get_theatre_permissions(apps, schema_editor):
    """The theatre permissions go to the clinic roles that started as reception / doctor, like the built-in sets.
    Roles the clinic made itself are left for the owner to decide."""
    ClinicRole = apps.get_model("organizations", "ClinicRole")
    for key, new in (("reception", RECEPTION), ("doctor", DOCTOR)):
        for role in ClinicRole.objects.filter(key=key):
            perms = set(role.permissions or [])
            if not set(new) <= perms:
                role.permissions = sorted(perms | set(new))
                role.save(update_fields=["permissions"])


class Migration(migrations.Migration):
    dependencies = [
        ("theatre", "0001_initial"),
        ("organizations", "0006_specialty"),
    ]

    operations = [
        migrations.RunPython(roles_get_theatre_permissions, migrations.RunPython.noop),
    ]
