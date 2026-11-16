"""Payments on a patient's account (phase 16): every payment names its patient, the visit becomes optional, and a
payment can be a deposit. Existing rows take the patient of their visit."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0002_payment_treasury"),
        ("patients", "0001_initial"),
        ("scheduling", "0002_emergency_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="patient",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="patients.patient"
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="kind",
            field=models.CharField(
                choices=[("payment", "تحصيل"), ("deposit", "مقدم")], default="payment", max_length=10
            ),
        ),
        migrations.AlterField(
            model_name="payment",
            name="appointment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payments",
                to="scheduling.appointment",
            ),
        ),
        migrations.RunSQL(
            "UPDATE billing_payment AS p SET patient_id = a.patient_id "
            "FROM scheduling_appointment AS a WHERE a.id = p.appointment_id AND p.patient_id IS NULL",
            migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="payment",
            name="patient",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="patients.patient"
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind", "payment"), ("appointment__isnull", True), _connector="OR"),
                name="payment_deposit_is_on_the_account",
            ),
        ),
    ]
