from django.db import migrations, models


def roles_see_the_pregnancy_week(apps, schema_editor):
    """The new `pregnancy.view_basic` permission (the gestational-age tag next to a patient's name) goes to the
    clinic roles that started as reception / doctor / admin, like the built-in sets. Roles the clinic made itself
    are left for the owner to decide."""
    ClinicRole = apps.get_model("organizations", "ClinicRole")
    for role in ClinicRole.objects.filter(key__in=["reception", "doctor", "admin"]):
        perms = set(role.permissions or [])
        if "pregnancy.view_basic" not in perms:
            role.permissions = sorted(perms | {"pregnancy.view_basic"})
            role.save(update_fields=["permissions"])


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0005_role_start_screen"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationsettings",
            name="specialty",
            field=models.CharField(
                choices=[("general", "عام (كل التخصصات)"), ("obgyn", "نسا وتوليد")],
                default="general",
                max_length=10,
                verbose_name="تخصص العيادة",
            ),
        ),
        migrations.RunPython(roles_see_the_pregnancy_week, migrations.RunPython.noop),
    ]
