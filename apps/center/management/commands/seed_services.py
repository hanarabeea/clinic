import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.center.models import CenterSettings, Service
from apps.organizations.management.commands.seed_org import DEFAULT_CLINIC
from apps.organizations.models import Organization


def seed_org_services(org, *, clinic_data=None, force=False) -> int:
    """The starting services catalog and the emergency fee for one clinic. A clinic that already has services (or
    settings) keeps them unless `force`; then services are matched by name and updated."""
    data = clinic_data or {}
    fee = (data.get("center") or {}).get("emergency_fee")
    exists = CenterSettings.objects.filter(organization=org).exists()
    if fee is not None and (force or not exists):
        settings = CenterSettings.for_org(org)
        settings.emergency_fee = fee
        settings.save()
    if Service.objects.filter(organization=org).exists() and not force:
        return 0
    written = 0
    for order, s in enumerate(data.get("services") or []):
        Service.objects.update_or_create(
            organization=org,
            name_ar=s["name_ar"],
            defaults={
                "name_en": s.get("name_en", ""),
                "category": s.get("category", "procedure"),
                "price": s["price"],
                "doctor_share_percent": s.get("doctor_share_percent", 0),
                "order": order,
            },
        )
        written += 1
    return written


class Command(BaseCommand):
    help = "Starting services catalog + emergency fee for every clinic (or --org), from clinic.json. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--org", help="Organization slug (default: all)")
        parser.add_argument("--clinic", default=str(DEFAULT_CLINIC))
        parser.add_argument("--force", action="store_true", help="Reapply clinic.json over existing services.")

    @transaction.atomic
    def handle(self, *args, **opts):
        data = json.loads(Path(opts["clinic"]).read_text(encoding="utf-8"))
        orgs = Organization.objects.all()
        if opts["org"]:
            orgs = orgs.filter(slug=opts["org"])
        for org in orgs:
            n = seed_org_services(org, clinic_data=data, force=opts["force"])
            self.stdout.write(f"{org.slug}: {n} service(s) written")
