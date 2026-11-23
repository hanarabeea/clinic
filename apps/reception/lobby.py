"""The waiting-room screen (plan 15 §1.5): a TV in the waiting room shows, for each doctor working today, who is
inside now and who comes next, and chimes when someone is called in ("دخّل" on the board or "التالي" on the desk).

Privacy: only the first name and the first letter of the next one ("منى إ.") or, with
`CenterSettings.lobby_show_names` off, only the queue number / time. Nothing else of the file is on the page.
"""

from dataclasses import dataclass, field

from django.utils import timezone

from apps.center.models import CenterSettings
from apps.core.templatetags.clock import ampm
from apps.core.timeutils import CAIRO
from apps.doctors import services as doctor_services
from apps.scheduling.models import URGENT_FIRST, Appointment, AppointmentStatus

NEXT_SHOWN = 4


def short_name(full_name: str) -> str:
    parts = (full_name or "").split()
    if not parts:
        return ""
    return f"{parts[0]} {parts[1][0]}." if len(parts) > 1 else parts[0]


@dataclass
class Entry:
    label: str  # "رقم 7" / "2:40 PM"
    name: str


@dataclass
class DoctorLine:
    doctor: object
    inside: Entry | None = None
    next: list = field(default_factory=list)


@dataclass
class Lobby:
    lines: list
    call_sig: str  # changes whenever someone new is called in: the page chimes
    sound: bool


def _entry(appt, show_names) -> Entry:
    label = f"#{appt.queue_number}" if appt.queue_number else ampm(appt.start_at)
    return Entry(label=label, name=short_name(appt.patient.full_name) if show_names else "")


def state(org, now=None) -> Lobby:
    now = now or timezone.now()
    day = timezone.localtime(now, CAIRO).date()
    settings = CenterSettings.for_org(org)
    doctors = doctor_services.active_doctors(org)
    rows = list(
        Appointment.objects.for_org(org)
        .filter(date=day, doctor__in=doctors, status__in=(AppointmentStatus.ARRIVED, AppointmentStatus.IN_CONSULTATION))
        .select_related("patient")
        .order_by(URGENT_FIRST, "queue_number", "start_at", "id")
    )
    lines, last_call = [], None
    for doctor in doctors:
        mine = [a for a in rows if a.doctor_id == doctor.pk]
        inside = [a for a in mine if a.status == AppointmentStatus.IN_CONSULTATION]
        current = max(inside, key=lambda a: a.called_at or a.updated_at) if inside else None
        waiting = [a for a in mine if a.status == AppointmentStatus.ARRIVED]
        line = DoctorLine(doctor=doctor, inside=_entry(current, settings.lobby_show_names) if current else None,
                          next=[_entry(a, settings.lobby_show_names) for a in waiting[:NEXT_SHOWN]])  # fmt: skip
        lines.append(line)
        if current and current.called_at and (last_call is None or current.called_at > last_call[0]):
            last_call = (current.called_at, current.pk)
    sig = f"{last_call[1]}:{last_call[0].timestamp():.0f}" if last_call else ""
    return Lobby(lines=lines, call_sig=sig, sound=settings.lobby_sound)
