"""Which doctor a screen is about (center mode, plan 14 §14.3).

A clinic has one or several doctors. Screens never guess: the doctor comes from the request (`?doctor=<pk>` or a
hidden form field), else the one the user picked last (session), else the doctor record linked to the signed-in user,
else the clinic's first active doctor. The reception board may also show every doctor at once ("all").
"""

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import Doctor, VisitType

SESSION_KEY = "doctor_pk"
BOARD_KEY = "board_doctor"
ALL = "all"


class DoctorError(Exception):
    pass


def get_doctor(org) -> Doctor:
    """The clinic's first active doctor, created on first use (a clinic always has at least one)."""
    doctor = Doctor.objects.filter(organization=org, is_active=True).order_by("id").first()
    if doctor is None:
        doctor = Doctor.objects.filter(organization=org).order_by("id").first()
    if doctor is None:
        doctor = Doctor.objects.create(organization=org, name_ar=org.settings.clinic_name_ar or org.name_ar)
    return doctor


def active_doctors(org) -> list[Doctor]:
    doctors = list(Doctor.objects.filter(organization=org, is_active=True).order_by("id"))
    return doctors or [get_doctor(org)]


def own_doctor(request) -> Doctor | None:
    """The doctor record of the signed-in user (Settings → الدكاترة links a login to a doctor)."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return None
    return Doctor.objects.filter(organization=request.organization, user=request.user, is_active=True).first()


def _requested(request, param="doctor") -> str:
    return (request.GET.get(param) or request.POST.get(param) or "").strip()


def current_doctor(request, doctors=None) -> Doctor:
    """The doctor this request is about. An explicit `doctor` in the request wins and is remembered."""
    doctors = doctors if doctors is not None else active_doctors(request.organization)
    by_pk = {str(d.pk): d for d in doctors}
    raw = _requested(request)
    if raw in by_pk:
        request.session[SESSION_KEY] = raw
        return by_pk[raw]
    remembered = str(request.session.get(SESSION_KEY, ""))
    if remembered in by_pk:
        return by_pk[remembered]
    own = own_doctor(request)
    if own is not None and str(own.pk) in by_pk:
        return by_pk[str(own.pk)]
    return doctors[0]


def board_doctor(request, doctors=None) -> Doctor | None:
    """The reception board's doctor: one of them, or None for all of them together (the default with several)."""
    doctors = doctors if doctors is not None else active_doctors(request.organization)
    if len(doctors) == 1:
        return doctors[0]
    by_pk = {str(d.pk): d for d in doctors}
    raw = _requested(request)
    if raw == ALL or raw in by_pk:
        request.session[BOARD_KEY] = raw
    choice = str(request.session.get(BOARD_KEY, ALL))
    return by_pk.get(choice)


def settings_doctor(request) -> Doctor:
    """Settings screens edit any doctor, also one that was switched off."""
    doctors = list(Doctor.objects.filter(organization=request.organization).order_by("id"))
    return current_doctor(request, doctors or [get_doctor(request.organization)])


# --- managing the clinic's doctors (Settings → الدكاترة) ---------------------------------------------------------


@transaction.atomic
def add_doctor(org, doctor: Doctor, *, copy_visit_types_from: Doctor | None = None) -> Doctor:
    """A new doctor starts with the same visit types (names, durations, prices) as an existing one, so reception can
    book for them straight away; the clinic then edits them."""
    doctor.organization = org
    doctor.save()
    if copy_visit_types_from is not None:
        source = VisitType.objects.filter(doctor=copy_visit_types_from, is_active=True, is_system=False)
        VisitType.objects.bulk_create(
            VisitType(
                organization=org,
                doctor=doctor,
                name_ar=vt.name_ar,
                duration_minutes=vt.duration_minutes,
                price=vt.price,
                is_followup=vt.is_followup,
                free_followup_days=vt.free_followup_days,
                color=vt.color,
                order=vt.order,
            )
            for vt in source
        )
    return doctor


def upcoming_bookings(doctor) -> int:
    from apps.scheduling.models import Appointment, AppointmentStatus

    return Appointment.objects.filter(
        organization=doctor.organization,
        doctor=doctor,
        start_at__gte=timezone.now(),
        status__in=[AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED],
    ).count()


@transaction.atomic
def set_active(doctor: Doctor, active: bool) -> Doctor:
    """Switching a doctor off hides them from booking and the board; nothing is deleted. Refused while they still have
    bookings ahead (reception moves or cancels those first) and for the clinic's last active doctor."""
    doctor = Doctor.objects.select_for_update().get(pk=doctor.pk)
    if not active:
        others = Doctor.objects.filter(organization=doctor.organization, is_active=True).exclude(pk=doctor.pk)
        if not others.exists():
            raise DoctorError(_("لازم يفضل دكتور واحد على الأقل شغال."))
        ahead = upcoming_bookings(doctor)
        if ahead:
            raise DoctorError(_("عنده %(n)s حجز جاي. أجّلهم أو الغيهم الأول.") % {"n": ahead})
    doctor.is_active = active
    doctor.save(update_fields=["is_active", "updated_at"])
    return doctor
