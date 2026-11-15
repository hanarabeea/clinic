from django.db import migrations, models


def reception_works_in_one_screen(apps, schema_editor):
    """The default reception role opens straight onto the reception board (a role the clinic created itself, or one it
    already pointed elsewhere, is left alone)."""
    ClinicRole = apps.get_model("organizations", "ClinicRole")
    ClinicRole.objects.filter(key="reception", start_screen="").update(start_screen="reception")
    # the default descriptions, reworded without dashes (only where the clinic never changed them)
    for old, new in (
        ("المستخدمين والإعدادات وسجل النشاط والمواعيد والمرضى — من غير الملف الطبي",
         "المستخدمين والإعدادات وسجل النشاط والمواعيد والمرضى، من غير الملف الطبي"),
        ("الحجز والاستقبال والخزنة وبيانات المرضى — من غير الملف الطبي",
         "الحجز والاستقبال والخزنة وبيانات المرضى، من غير الملف الطبي"),
    ):  # fmt: skip
        ClinicRole.objects.filter(description=old).update(description=new)


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0004_new_default_colours"),
    ]

    operations = [
        migrations.AddField(
            model_name="clinicrole",
            name="start_screen",
            field=models.CharField(blank=True, max_length=20, verbose_name="شاشة العمل"),
        ),
        migrations.RunPython(reception_works_in_one_screen, migrations.RunPython.noop),
    ]
