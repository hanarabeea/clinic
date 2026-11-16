from django.core.management.base import BaseCommand, CommandError

from apps.ledger import verify
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Verify every clinic's books: entries balance, every document has its entry, every control account equals "
        "its subledger, numbering has no gaps, no drawer or shelf is negative. Exits 1 when a problem is found."
    )

    def add_arguments(self, parser):
        parser.add_argument("--org", help="Organization slug (default: all)")
        parser.add_argument("--alert", action="store_true", help="E-mail the owners when problems are found")

    def handle(self, *args, **opts):
        orgs = Organization.objects.all()
        if opts["org"]:
            orgs = orgs.filter(slug=opts["org"])
        failing = 0
        for org in orgs:
            result = verify.run(org)
            if result.ok:
                self.stdout.write(self.style.SUCCESS(f"{org.slug}: books are sound ({result.seconds}s)"))
                continue
            failing += 1
            self.stdout.write(self.style.ERROR(f"{org.slug}: {len(result.problems)} problem(s)"))
            for problem in result.problems:
                self.stdout.write(f"  [{problem['code']}] {problem['message']}")
            if opts["alert"]:
                verify._alert(org, result)
        if failing:
            raise CommandError(f"{failing} clinic(s) with problems in their books")
