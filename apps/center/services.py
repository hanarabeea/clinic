"""Center mode logic: services added to a visit, and emergency arrivals. Views stay thin; appointment changes still go
through scheduling.services."""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.timeutils import CAIRO
from apps.doctors.models import VisitType
from apps.scheduling import services as booking
from apps.scheduling.models import Appointment, AppointmentStatus

from .models import (
    CenterSettings,
    Disposition,
    EmergencyCase,
    EmergencyReason,
    OrderStatus,
    Service,
    ServiceOrder,
    Triage,
)

EMERGENCY_COLOR = "#B91C1C"
EMERGENCY_NAME = "طوارئ"
NOT_BILLABLE = (AppointmentStatus.CANCELLED, AppointmentStatus.RESCHEDULED, AppointmentStatus.NO_SHOW)
OPEN_STATUSES = (AppointmentStatus.ARRIVED, AppointmentStatus.IN_CONSULTATION)
# the most urgent first, then whoever has been here longest
TRIAGE_RANK = models.Case(
    models.When(triage=Triage.CRITICAL, then=models.Value(0)),
    models.When(triage=Triage.URGENT, then=models.Value(1)),
    default=models.Value(2),
    output_field=models.IntegerField(),
)


class CenterError(Exception):
    pass


# --- services on a visit ---------------------------------------------------------------------------------------


def active_services(org):
    return Service.objects.for_org(org).filter(is_active=True)


def orders_for(appt) -> list[ServiceOrder]:
    return [o for o in appt.service_orders.all() if o.is_active]


def orders_total(orders) -> Decimal:
    return sum((o.total for o in orders if o.is_active), Decimal(0))


@transaction.atomic
def order_service(appt, service, *, by, quantity=1, doctor=None, notes="") -> ServiceOrder:
    """Add a service to a visit. The price, name and doctor's share are copied now: a later change in the catalog
    never rewrites what a patient was charged."""
    appt = Appointment.objects.select_for_update().get(pk=appt.pk)
    if service.organization_id != appt.organization_id:
        raise CenterError(_("الخدمة دي مش في العيادة دي."))
    if not service.is_active:
        raise CenterError(_("الخدمة دي متوقفة."))
    if appt.status in NOT_BILLABLE:
        raise CenterError(_("الحجز ده مش نشط، مينفعش يتضاف عليه خدمات."))
    try:
        quantity = int(quantity)
    except (TypeError, ValueError) as e:
        raise CenterError(_("اكتب العدد رقم.")) from e
    if not 1 <= quantity <= 99:
        raise CenterError(_("العدد لازم يكون بين 1 و99."))
    doctor = doctor or appt.doctor
    if doctor.organization_id != appt.organization_id:
        raise CenterError(_("الدكتور ده مش في العيادة دي."))
    order = ServiceOrder.objects.create(
        organization=appt.organization,
        appointment=appt,
        service=service,
        name=service.name_ar,
        unit_price=service.price,
        quantity=quantity,
        doctor=doctor,
        doctor_share_percent=service.doctor_share_percent,
        notes=(notes or "")[:200],
        ordered_by=by,
    )
    from apps.invoicing import services as invoicing

    invoicing.sync_quietly(appt, by=by)  # a visit already invoiced gets one more invoice for this service
    return order


@transaction.atomic
def cancel_order(order, *, by) -> ServiceOrder:
    """Take a service off the visit (kept as a cancelled row). Refused once it was paid for: the money has to go back
    through a refund first, so the cashbox never holds more than the visit is worth."""
    from apps.billing import services as billing

    appt = Appointment.objects.select_for_update().get(pk=order.appointment_id)
    order = ServiceOrder.objects.select_for_update().get(pk=order.pk)
    if not order.is_active:
        return order
    others = [o for o in appt.service_orders.all() if o.pk != order.pk]
    if billing.balance(appt).paid > billing.balance(appt, orders=others).due:
        raise CenterError(_("اتدفع تمنها. سجّل استرداد الأول وبعدين الغيها."))
    order.status = OrderStatus.CANCELLED
    order.cancelled_by, order.cancelled_at = by, timezone.now()
    order._history_user = by
    order.save(update_fields=["status", "cancelled_by", "cancelled_at", "updated_at"])
    from apps.invoicing import services as invoicing

    invoicing.sync_quietly(appt, by=by)  # if the visit was invoiced, the service's line is taken back
    return order


# --- emergency arrivals ----------------------------------------------------------------------------------------


def emergency_visit_type(doctor, fee) -> VisitType:
    """Every appointment needs a visit type; emergencies use a hidden one per doctor (never offered in booking)."""
    vt, _created = VisitType.objects.get_or_create(
        organization=doctor.organization,
        doctor=doctor,
        is_system=True,
        name_ar=EMERGENCY_NAME,
        defaults={
            "duration_minutes": doctor.slot_minutes,
            "price": fee,
            "color": EMERGENCY_COLOR,
            "is_active": False,
            "order": 99,
        },
    )
    return vt


def open_case(patient) -> EmergencyCase | None:
    return (
        EmergencyCase.objects.filter(patient=patient, appointment__status__in=OPEN_STATUSES)
        .select_related("appointment")
        .first()
    )


@transaction.atomic
def register_emergency(
    *, org, patient, doctor, triage, reason, by, reason_text="", now=None
) -> EmergencyCase:  # fmt: skip
    """The patient is at the door: she is on the doctor's queue ahead of everyone, already arrived. The emergency fee
    (Settings → الخدمات) is the visit's price."""
    if patient.organization_id != org.pk or doctor.organization_id != org.pk:
        raise CenterError(_("المريض أو الدكتور مش في العيادة دي."))
    if triage not in Triage.values or reason not in EmergencyReason.values:
        raise CenterError(_("اختار درجة الخطورة والسبب."))
    if open_case(patient) is not None:
        raise CenterError(_("المريضة دي متسجلة طوارئ دلوقتي."))
    now = now or timezone.now()
    fee = CenterSettings.for_org(org).emergency_fee
    appt = booking.emergency_arrival(
        patient=patient, doctor=doctor, visit_type=emergency_visit_type(doctor, fee), by=by, price=fee, now=now
    )
    return EmergencyCase.objects.create(
        organization=org,
        appointment=appt,
        patient=patient,
        doctor=doctor,
        triage=triage,
        reason=reason,
        reason_text=(reason_text or "").strip()[:200],
        arrived_at=appt.arrived_at or now,
        registered_by=by,
    )


def mark_seen(appt, now=None) -> None:
    """The doctor called her in: the "seen" time is written once (called by clinical.services.start_visit and by
    reception's "دخّل")."""
    EmergencyCase.objects.filter(appointment=appt, seen_at__isnull=True).update(
        seen_at=now or timezone.now(), updated_at=timezone.now()
    )


@transaction.atomic
def set_disposition(case, disposition, *, by, notes="") -> EmergencyCase:
    """Where she went after the emergency visit. Can be corrected; history keeps every version."""
    if disposition not in Disposition.values:
        raise CenterError(_("اختار التصرف."))
    case = EmergencyCase.objects.select_for_update().get(pk=case.pk)
    case.disposition = disposition
    case.disposition_notes = (notes or "").strip()[:300]
    case.disposition_at = timezone.now()
    case.disposition_by = by
    case._history_user = by
    case.save(update_fields=["disposition", "disposition_notes", "disposition_at", "disposition_by", "updated_at"])
    return case


@transaction.atomic
def cancel_emergency(case, *, by, reason="") -> EmergencyCase:
    """Registered by mistake: only before the doctor saw her. The case row stays (with its cancelled appointment)."""
    appt = Appointment.objects.get(pk=case.appointment_id)
    if appt.status != AppointmentStatus.ARRIVED:
        raise CenterError(_("مينفعش تتلغي بعد ما الدكتور شافها."))
    booking.cancel(appt, by=by, reason=reason or _("تسجيل طوارئ بالغلط"), notify=False)
    return case


def _cases(org):
    return (
        EmergencyCase.objects.for_org(org).select_related("patient", "doctor", "appointment").annotate(rank=TRIAGE_RANK)
    )


def active_cases(org, day, doctors=None) -> list[EmergencyCase]:
    """Who is in the emergency flow right now (waiting or with the doctor), most urgent first."""
    qs = _cases(org).filter(appointment__date=day, appointment__status__in=OPEN_STATUSES)
    if doctors is not None:
        qs = qs.filter(doctor__in=doctors)
    return list(qs.order_by("rank", "arrived_at"))


def cases_on(org, day) -> list[EmergencyCase]:
    start = datetime.combine(day, time.min, tzinfo=CAIRO)
    qs = _cases(org).filter(arrived_at__gte=start, arrived_at__lt=start + timedelta(days=1))
    return list(qs.exclude(appointment__status=AppointmentStatus.CANCELLED).order_by("-arrived_at"))


def case_of(appt) -> EmergencyCase | None:
    """The appointment's emergency case, if it is one (no query for ordinary bookings)."""
    from apps.scheduling.models import BookingSource

    if appt.source != BookingSource.EMERGENCY:
        return None
    return getattr(appt, "emergency", None)
