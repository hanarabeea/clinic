"""Demo data for the OB/GYN layer: gives the women created by `seed_demo` pregnancies at every stage, obstetric
history and blood groups, through the real services. Idempotent (a woman who already has a followed pregnancy or
history is skipped); `--remove` is not needed: `seed_demo --remove` removes the patients, and their pregnancy records
are demo-only.

    manage.py seed_pregnancies [--org demo-clinic]
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.doctors.models import Doctor
from apps.obstetrics import services
from apps.obstetrics.models import EddMethod, Outcome, Pregnancy, PregnancyHistory, RiskLevel
from apps.organizations.models import Organization
from apps.patients.management.commands.seed_demo import MARKER
from apps.patients.models import Gender, Patient

# (weeks, extra days) along today, how it is dated, fetuses, high risk reason, blood group, age, history outcomes
STORIES = [
    (7, 2, "lmp", 1, "", "A+", 24, []),
    (12, 5, "ultrasound", 1, "", "O+", 29, [("vaginal", 3)]),
    (19, 0, "lmp", 1, "", "B+", 31, [("cesarean", 4), ("miscarriage", 2)]),
    (24, 3, "lmp", 1, "سكر حمل", "O-", 36, [("cesarean", 6), ("cesarean", 3)]),
    (29, 6, "ivf", 2, "", "AB+", 34, []),
    (33, 1, "ultrasound", 1, "", "A-", 27, [("vaginal", 2)]),
    (37, 4, "lmp", 1, "ضغط مرتفع", "B+", 38, [("vaginal", 9), ("vaginal", 6), ("miscarriage", 4)]),
    (40, 5, "lmp", 1, "", "O+", 22, []),
]


class Command(BaseCommand):
    help = "Demo pregnancies for the seed_demo patients (OB/GYN clinics)."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo-clinic")

    @transaction.atomic
    def handle(self, *args, **o):
        try:
            org = Organization.objects.get(slug=o["org"])
        except Organization.DoesNotExist as e:
            raise CommandError(f"Organization '{o['org']}' not found. Run `manage.py seed_org` first.") from e
        if not services.enabled(org):
            raise CommandError(
                'This clinic\'s specialty is not OB/GYN (Settings, or clinic.json "specialty": "obgyn").'
            )
        services.ensure_milestones(org)
        today = services.today()
        doctor = Doctor.objects.for_org(org).first()
        seeded = Patient.objects.for_org(org).filter(notes=MARKER, gender=Gender.FEMALE).order_by("file_number")
        if Pregnancy.objects.filter(patient__in=seeded).exists():
            self.stdout.write("Demo pregnancies are already there.")
            return
        women = [p for p in seeded if not PregnancyHistory.objects.filter(patient=p).exists()]
        made = 0
        for patient, (weeks, days, method, fetuses, risk, blood, age, past) in zip(women, STORIES, strict=False):
            age_days = weeks * 7 + days
            if method == "lmp":
                dating = services.Dating(method=EddMethod.LMP, lmp=today - timedelta(days=age_days))
            elif method == "ultrasound":
                # dated by a scan four weeks ago (or at 8 weeks, whichever came later)
                back = min(28, max(age_days - 56, 0))
                at_scan = age_days - back
                dating = services.Dating(
                    method=EddMethod.ULTRASOUND, us_date=today - timedelta(days=back), us_weeks=at_scan // 7,
                    us_days=at_scan % 7,
                )  # fmt: skip
            else:
                dating = services.Dating(
                    method=EddMethod.IVF, transfer_date=today - timedelta(days=age_days - 19), embryo_age_days=5
                )
            patient.date_of_birth = date(today.year - age, 3, 15)
            patient.save(update_fields=["date_of_birth", "updated_at"])
            profile = services.profile_for(patient)
            profile.blood_group = blood
            profile.save(update_fields=["blood_group", "updated_at"])
            for outcome, years_ago in past:
                services.add_history(patient, outcome=Outcome(outcome), by=None, year=today.year - years_ago)
            pregnancy = services.start_pregnancy(patient, dating, by=None, doctor=doctor, fetuses=fetuses, on=today)
            if risk:
                services.update_details(
                    pregnancy, fetuses=fetuses, risk_level=RiskLevel.HIGH, risk_notes=risk, notes=""
                )
            made += 1
        self.stdout.write(self.style.SUCCESS(f"{made} pregnancies seeded for '{org.slug}'."))
