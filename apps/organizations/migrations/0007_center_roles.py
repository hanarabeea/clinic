from django.db import migrations

# The roles and their permissions as of this migration (frozen: later changes to the defaults do not rewrite a
# clinic's roles, which are the clinic's own to edit).
ROLES = [
    ("accountant", "محاسب", "الدفاتر والقوائم المالية، الخزن والشيكات، المشتريات والموردين، الفواتير ومستحقات الدكاترة",
     "books", [
         "accounts.manage", "appointment.view", "books.verify", "cheque.manage", "doctor.payout", "invoice.refund",
         "invoice.view", "ledger.close_period", "ledger.post", "ledger.view", "payer.manage", "purchase.manage",
         "purchase.view", "report.finance", "report.statements", "stock.approve_count", "stock.count", "stock.move",
         "stock.view", "supplier.manage", "supplier.pay", "tax.manage", "treasury.count", "treasury.operate",
         "treasury.view",
     ]),
    ("cashier", "كاشير", "الخزنة والتحصيل، سندات القبض والصرف، وفواتير المرضى", "cashbox", [
        "appointment.view", "invoice.issue", "invoice.view", "patient.view_basic", "payment.record",
        "treasury.operate", "treasury.view",
    ]),
    ("storekeeper", "أمين مخزن", "الأصناف والصرف والتحويل وجرد المخزن", "stock", [
        "purchase.view", "stock.count", "stock.move", "stock.view",
    ]),
    ("theatre_nurse", "تمريض عمليات", "جدول العمليات وخطواتها، المستلزمات، ودخول وخروج الغرف", "theatre", [
        "admission.manage", "appointment.view", "patient.view_basic", "pregnancy.view_basic", "stock.view",
        "theatre.operate", "theatre.view", "vitals.record",
    ]),
]  # fmt: skip


def add_center_roles(apps, schema_editor):
    """Clinics that already have their roles get the four center roles too (unless a role of that name or origin is
    already there). A clinic with no roles yet gets all eight when its screens are first opened."""
    ClinicRole = apps.get_model("organizations", "ClinicRole")
    Organization = apps.get_model("organizations", "Organization")
    for org in Organization.objects.all():
        mine = ClinicRole.objects.filter(organization=org)
        if not mine.exists():
            continue
        order = max(mine.values_list("order", flat=True), default=0)
        for key, name, description, screen, perms in ROLES:
            if mine.filter(key=key).exists() or mine.filter(name=name).exists():
                continue
            order += 1
            ClinicRole.objects.create(
                organization=org, key=key, name=name, description=description, permissions=sorted(perms),
                start_screen=screen, order=order,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0006_specialty"),
    ]

    operations = [
        migrations.RunPython(add_center_roles, migrations.RunPython.noop),
    ]
