from django.core.management.base import BaseCommand

from apps.prescriptions import speech


class Command(BaseCommand):
    help = "Write the drug names the offline speech service listens for (SPEECH_DATA_DIR/boost.txt). Run by run.bat."

    def handle(self, *args, **opts):
        n = speech.write_boost()
        self.stdout.write(self.style.SUCCESS(f"Speech drug-name list: {n} phrases"))
