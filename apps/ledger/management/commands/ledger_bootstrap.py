from django.core.management.base import BaseCommand

from apps.ledger.bootstrap import bootstrap, invoice_visits
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Bring every clinic's books up to date: the chart of accounts, the treasuries, and a journal entry for every "
        "payment that has none (so the ledger agrees with the cashbox). Safe to run any number of times."
    )

    def add_arguments(self, parser):
        parser.add_argument("--org", help="Organization slug (default: all)")

    def handle(self, *args, **opts):
        orgs = Organization.objects.all()
        if opts["org"]:
            orgs = orgs.filter(slug=opts["org"])
        for org in orgs:
            posted = bootstrap(org)
            invoiced = invoice_visits(org)
            self.stdout.write(
                f"{org.slug}: chart and treasuries ready, {posted} payment(s) posted, {invoiced} invoice(s) made"
            )
