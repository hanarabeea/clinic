"""Users sign in with a username or the email; the email becomes optional; invitations are gone (employees are added
directly from the users screen). Every existing user gets their email as username, so nobody's login changes."""

import django.core.validators
from django.db import migrations, models


def usernames_from_emails(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.all():
        user.username = (user.email or f"user{user.pk}").strip().lower()
        user.email = (user.email or "").strip().lower() or None
        user.save(update_fields=["username", "email"])


def back_to_emails(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.filter(email__isnull=True):
        user.email = f"{user.username}@no-email.invalid"
        user.save(update_fields=["email"])


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_clinic_roles_data"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="email",
            field=models.EmailField(blank=True, max_length=254, null=True, unique=True, verbose_name="الإيميل"),
        ),
        migrations.AddField(
            model_name="user",
            name="username",
            field=models.CharField(max_length=150, null=True, verbose_name="اسم المستخدم"),
        ),
        migrations.RunPython(usernames_from_emails, back_to_emails),
        migrations.AlterField(
            model_name="user",
            name="username",
            field=models.CharField(
                max_length=150,
                unique=True,
                validators=[
                    django.core.validators.RegexValidator(
                        "^[\\w.\\-]+\\Z", "حروف وأرقام و . _ - بس، من غير مسافات ولا @."
                    )
                ],
                verbose_name="اسم المستخدم",
            ),
        ),
        migrations.DeleteModel(name="Invitation"),
    ]
