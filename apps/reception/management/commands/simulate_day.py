"""Fill today's reception screen with a realistic day for manual testing (demo patients only).

    python manage.py simulate_day            # 10 bookings today: some done & paid, some waiting, one no-show,
                                             # a service on one visit and one emergency arrival
    python manage.py simulate_day --remove   # delete today's simulated bookings again

Goes through the real services (book / transition / mark_no_show / record_payment). No message is ever sent:
every outbox row created for these demo appointments is cancelled at the end.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import Payment
from apps.billing.services import record_payment
from apps.center import services as center
from apps.center.models import EmergencyCase, EmergencyReason, ServiceOrder, Triage
from apps.clinical.models import Visit, Vitals
from apps.core.timeutils import CAIRO
from apps.doctors.models import BookingMode, VisitType
from apps.doctors.services import get_doctor
from apps.notifications.models import MessageStatus, ScheduledMessage
from apps.organizations.models import Organization
from apps.patients.management.commands.seed_demo import MARKER
from apps.patients.models import Patient
from apps.scheduling import services as booking
from apps.scheduling.availability import periods_for
from apps.scheduling.models import Appointment, AppointmentStatus

NOTE = "simulate_day"
KEPT = "simulate_day (kept)"  # a simulated booking that became a medical record: no longer the simulation's to remove


class Command(BaseCommand):
    help = "Book 10 demo patients for today and walk some of them through the day (for trying the reception screen)."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo-clinic")
        parser.add_argument("--remove", action="store_true")

    def handle(self, *args, **o):
        org = Organization.objects.filter(slug=o["org"]).first()
        if org is None:
            raise CommandError(f"No organization '{o['org']}'")
        today = timezone.localtime(timezone.now(), CAIRO).date()
        if o["remove"]:
            self._remove(org)
            return
        if Appointment.objects.filter(organization=org, notes=NOTE, date=today).exists():
            self.stdout.write("Today is already simulated. Run with --remove first to start again.")
            return
        doctor = get_doctor(org)
        periods = periods_for(doctor, today)
        if not periods:
            raise CommandError("The doctor doesn't work today — add a working period or an 'extra' exception first.")
        visit_type = VisitType.objects.filter(organization=org, doctor=doctor, is_active=True).order_by("id").first()
        if visit_type is None:
            raise CommandError("No visit types — add one in Settings first.")
        if Patient.objects.filter(organization=org, notes=MARKER).count() < 10:
            call_command("seed_demo", org=org.slug)
        here_today = Appointment.objects.filter(organization=org, date=today).values("patient_id")
        patients = list(
            Patient.objects.filter(organization=org, notes=MARKER).exclude(pk__in=here_today).order_by("id")[:10]
        )
        with transaction.atomic():
            appts = self._book(doctor, visit_type, periods, patients)
            self._walk_through(appts)
            self._center(org, doctor, appts)
            self._theatre(org, doctor)
        simulated = Appointment.objects.filter(notes=NOTE)
        rebooked = Appointment.objects.filter(auto_rebooked_from__in=simulated)
        ScheduledMessage.objects.filter(appointment__in=list(simulated) + list(rebooked)).exclude(
            status=MessageStatus.SENT
        ).update(status=MessageStatus.CANCELLED, status_reason="محاكاة — مش هتتبعت")
        self.stdout.write(self.style.SUCCESS(f"Simulated {len(appts)} bookings for {today}. Open /reception/."))

    def _book(self, doctor, visit_type, periods, patients):
        appts = []
        step = timedelta(minutes=visit_type.duration_minutes or doctor.slot_minutes)
        start = periods[0].start_at
        for patient in patients:
            if doctor.booking_mode == BookingMode.SLOTS:
                appt = booking.book(patient=patient, doctor=doctor, visit_type=visit_type, by=None, start_at=start,
                                    overbook=True, notify=False)  # fmt: skip
                start += step
            else:
                appt = booking.book(patient=patient, doctor=doctor, visit_type=visit_type, by=None,
                                    day=periods[0].start_at.date(), period_key=periods[0].period_id,
                                    overbook=True, notify=False)  # fmt: skip
            Appointment.objects.filter(pk=appt.pk).update(notes=NOTE)
            appts.append(appt)
        return appts

    def _walk_through(self, appts):
        s = AppointmentStatus
        # 0-2 done and paid, 3 with the doctor, 4-5 waiting (with vitals), 6 no-show, 7-9 still booked.
        for i, appt in enumerate(appts):
            if i <= 5:
                booking.transition(appt, s.ARRIVED, by=None)
                if i in (4, 5):
                    visit = Visit.for_appointment(appt)
                    Vitals.objects.create(organization=appt.organization, visit=visit, weight_kg=70 + i,
                                          bp_systolic=120, bp_diastolic=80, pulse=78)  # fmt: skip
            if i <= 3:
                booking.transition(appt, s.IN_CONSULTATION, by=None)
            if i <= 2:
                booking.transition(appt, s.COMPLETED, by=None)
                if appt.price - appt.discount > 0:
                    record_payment(appt, amount=appt.price - appt.discount, by=None)
            if i == 6:
                booking.mark_no_show(appt, by=None)

    def _center(self, org, doctor, appts):
        """Center mode: a service on the visit that is with the doctor, and one emergency arrival waiting."""
        service = center.active_services(org).order_by("order", "id").first()
        if service is not None:
            center.order_service(appts[3], service, by=None)
        patient = (
            Patient.objects.filter(organization=org, notes=MARKER, gender="female")
            .exclude(pk__in=Appointment.objects.filter(organization=org, date=appts[0].date).values("patient_id"))
            .order_by("id")
            .first()
        )
        if patient is not None and center.open_case(patient) is None:
            case = center.register_emergency(
                org=org, patient=patient, doctor=doctor, triage=Triage.URGENT, reason=EmergencyReason.BLEEDING, by=None
            )
            Appointment.objects.filter(pk=case.appointment_id).update(notes=NOTE)

    def _theatre(self, org, doctor):
        """An operation later today and one patient in a room, so the theatre screens have something to show."""
        from apps.theatre import services as theatre
        from apps.theatre.models import Admission, AdmissionStatus, Operation, ProcedureType, Room, RoomKind

        theatre.ensure_defaults(org)
        today = timezone.localtime(timezone.now(), CAIRO).date()
        busy = Appointment.objects.filter(organization=org, date=today).values("patient_id")
        women = list(
            Patient.objects.filter(organization=org, notes=MARKER, gender="female")
            .exclude(pk__in=busy)
            .order_by("-id")[:2]
        )
        room = Room.objects.filter(organization=org, kind=RoomKind.OPERATING, is_active=True).first()
        procedure = ProcedureType.objects.filter(organization=org, is_active=True, delivery="").first()
        if (
            women
            and room
            and procedure
            and not Operation.objects.filter(organization=org, notes=NOTE, date=today).exists()
        ):
            start = (timezone.now() + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
            if timezone.localtime(start, CAIRO).date() == today:
                try:
                    # a price even when the clinic has not priced its operations yet, so the account has something
                    theatre.book_operation(org=org, patient=women[0], procedure=procedure, room=room, surgeon=doctor,
                                           start=start, by=None, notes=NOTE, notify=False,
                                           price=procedure.package_price or Decimal("3500"))  # fmt: skip
                except theatre.TheatreError:
                    pass  # the room is taken at that hour: the demo simply has no operation
        ward = Room.objects.filter(organization=org, kind=RoomKind.INPATIENT, is_active=True).first()
        if (
            len(women) > 1
            and ward
            and not Admission.objects.filter(patient=women[1], status=AdmissionStatus.ADMITTED).exists()
        ):
            try:
                theatre.admit(org=org, patient=women[1], room=ward, by=None, doctor=doctor, reason=NOTE)
            except theatre.TheatreError:
                pass

    def _remove(self, org):
        from apps.theatre.models import Admission, Operation

        # what was invoiced (or has charges on it) is in the books: it stays
        billed = {"invoice_lines__isnull": True, "charges__isnull": True}
        Admission.objects.filter(organization=org, reason=NOTE, **billed).delete()
        Operation.objects.filter(
            organization=org,
            notes=NOTE,
            op_notes__isnull=True,
            newborns__isnull=True,
            admissions__isnull=True,
            **billed,
        ).delete()
        appts = Appointment.objects.filter(organization=org, notes=NOTE)
        rebooked = Appointment.objects.filter(auto_rebooked_from__in=appts)
        everything = list(rebooked) + list(appts)
        removed = kept = refunded = 0
        for appt in everything:
            if Payment.objects.filter(appointment=appt).exists():
                # money was taken and is in the books: nothing financial is ever deleted. Give it back (a refund
                # entry) and cancel the booking, so the cashbox and the ledger stay the same numbers.
                refunded += self._refund_and_cancel(appt)
                continue
            # one by one: a simulated visit somebody then wrote a prescription or a pregnancy sheet on is a medical
            # record now, so it stays (with everything that hangs off it) instead of failing the whole clean-up
            try:
                with transaction.atomic():
                    ScheduledMessage.objects.filter(appointment=appt).delete()
                    ServiceOrder.objects.filter(appointment=appt).delete()
                    EmergencyCase.objects.filter(appointment=appt).delete()
                    Payment.objects.filter(appointment=appt).delete()
                    Vitals.objects.filter(visit__appointment=appt).delete()
                    Visit.objects.filter(appointment=appt).delete()
                    appt.delete()
                removed += 1
            except ProtectedError:
                Appointment.objects.filter(pk=appt.pk).update(notes=KEPT)
                kept += 1
        self.stdout.write(self.style.SUCCESS(f"Removed {removed} simulated booking(s)."))
        if kept:
            self.stdout.write(f"Kept {kept}: they were invoiced, or a prescription / pregnancy sheet was written.")
        if refunded:
            self.stdout.write(f"Refunded and cancelled {refunded} paid booking(s): payments stay on record.")

    def _refund_and_cancel(self, appt) -> int:
        with transaction.atomic():
            Appointment.objects.filter(pk=appt.pk).update(notes=KEPT)
            for payment in Payment.objects.filter(appointment=appt, is_refund=False):
                paid = billing.balance(appt).paid
                if paid <= 0:
                    break
                try:
                    record_payment(appt, amount=min(paid, payment.amount), method=payment.method, by=None,
                                   is_refund=True, note="simulate_day --remove")  # fmt: skip
                except billing.PaymentError:
                    break  # the drawer was emptied (shift closed): the payment simply stays
            if appt.is_active:
                booking.cancel(appt, by=None, reason="simulate_day --remove", notify=False)
        return 1
