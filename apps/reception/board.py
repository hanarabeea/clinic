"""The reception board — one screen for the day and the week (read-only; every change still goes through the
scheduling / billing services).

Day: every slot of the doctor's working periods (or every queue number), booked or free, plus bookings outside them
(exceptional ones). Week: an hour × day table, each cell holding that hour's bookings. Free cells inside working hours
that can still be booked carry `bookable` — the screen turns them into "+ احجز".
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.db.models import Prefetch

from apps.billing import services as billing
from apps.billing.models import Payment
from apps.core.timeutils import CAIRO, WEEKDAY_LABELS, local_dt, weekday_egypt
from apps.doctors.models import BookingMode, ScheduleException
from apps.notifications.templating import fmt_date
from apps.obstetrics import calc
from apps.obstetrics import services as obstetrics
from apps.scheduling.availability import periods_for
from apps.scheduling.models import ACTIVE_STATUSES, Appointment, AppointmentStatus

SHOWN_STATUSES = [
    AppointmentStatus.BOOKED,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.ARRIVED,
    AppointmentStatus.IN_CONSULTATION,
    AppointmentStatus.COMPLETED,
    AppointmentStatus.NO_SHOW,
]
LATE_AFTER_MINUTES = 5


@dataclass
class Entry:
    """One booking on the board."""

    a: Appointment
    balance: object
    waiting: int | None = None  # minutes since arrival
    late_minutes: int | None = None  # booked / confirmed and past its time


@dataclass
class Slot:
    """A row of the day table: a time (slots mode) and what's booked at it."""

    time: datetime
    entries: list = field(default_factory=list)
    covered: bool = False  # inside a longer booking that started earlier
    outside: bool = False  # a booking outside the working periods (exceptional)
    bookable: bool = False
    past: bool = False


def appointments(org, doctor, start: date, end: date) -> list[Appointment]:
    qs = (
        Appointment.objects.for_org(org)
        .filter(doctor=doctor, date__gte=start, date__lte=end, status__in=SHOWN_STATUSES)
        .select_related("patient", "visit_type")
        .prefetch_related(Prefetch("payments", queryset=Payment.objects.order_by("id")))
    )
    order = ("date", "queue_number", "start_at") if doctor.booking_mode == BookingMode.QUEUE else ("start_at", "id")
    rows = list(qs.order_by(*order))
    # OB/GYN clinics: the pregnancy week next to the name, as of each booking's own day (one query for the board)
    pregnant = obstetrics.badges_for(org, [a.patient_id for a in rows])
    for a in rows:
        ga = pregnant.get(a.patient_id)
        a.ga = calc.gestational_age(ga.edd, a.date) if ga else None
    return rows


def _entry(a, now) -> Entry:
    waiting = int((now - a.arrived_at).total_seconds() // 60) if a.status == AppointmentStatus.ARRIVED else None
    late = (
        a.status in (AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED)
        and a.queue_number is None
        and (now - a.start_at).total_seconds() > LATE_AFTER_MINUTES * 60
    )
    return Entry(
        a=a,
        balance=billing.balance(a, list(a.payments.all())),
        waiting=waiting,
        late_minutes=int((now - a.start_at).total_seconds() // 60) if late else None,
    )


def counts(appts) -> dict:
    c = Counter(a.status for a in appts)
    return {
        "booked": c[AppointmentStatus.BOOKED] + c[AppointmentStatus.CONFIRMED],
        "arrived": c[AppointmentStatus.ARRIVED],
        "in_consultation": c[AppointmentStatus.IN_CONSULTATION],
        "completed": c[AppointmentStatus.COMPLETED],
        "no_show": c[AppointmentStatus.NO_SHOW],
    }


def _bookable_from(doctor, now):
    return now + timedelta(minutes=doctor.min_notice_minutes)


def _within_horizon(doctor, day, now) -> bool:
    return day <= (now + timedelta(days=doctor.booking_horizon_days)).astimezone(CAIRO).date()


def day_board(org, doctor, day: date, now: datetime) -> dict:
    appts = appointments(org, doctor, day, day)
    entries = [_entry(a, now) for a in appts]
    periods = periods_for(doctor, day)
    board = {"appointments": appts, "entries": entries, "counts": counts(appts), "periods": periods, "day": day}
    if doctor.booking_mode == BookingMode.QUEUE:
        board["queue"] = [
            {"period": p, "entries": [e for e in entries if e.a.period_key == p.period_id]} for p in periods
        ]
        known = {p.period_id for p in periods}
        board["queue_outside"] = [e for e in entries if e.a.period_key not in known]
        return board

    step = timedelta(minutes=doctor.slot_minutes)
    rows: dict[datetime, Slot] = {}
    for p in periods:
        t = p.start_at
        while t + step <= p.end_at:
            rows[t] = Slot(time=t)
            t += step
    for e in entries:
        start = e.a.start_at.astimezone(CAIRO)
        if start not in rows:
            rows[start] = Slot(time=start, outside=True)
        rows[start].entries.append(e)
    busy = [(e.a.start_at, e.a.end_at) for e in entries if e.a.status in ACTIVE_STATUSES]
    earliest, open_day = _bookable_from(doctor, now), _within_horizon(doctor, day, now)
    for slot in rows.values():
        slot.covered = not slot.entries and any(s < slot.time < end for s, end in busy)
        slot.past = slot.time + step <= now
        slot.bookable = (
            not slot.entries and not slot.covered and not slot.outside and open_day and slot.time >= earliest
        )
    board["slots"] = [rows[t] for t in sorted(rows)]
    return board


def week_board(org, doctor, start: date, now: datetime) -> dict:
    days = [start + timedelta(days=i) for i in range(7)]
    appts = appointments(org, doctor, days[0], days[-1])
    today = now.astimezone(CAIRO).date()
    exceptions: dict[date, list] = {}
    for exc in ScheduleException.objects.filter(doctor=doctor, date__gte=days[0], date__lte=days[-1]):
        exceptions.setdefault(exc.date, []).append(exc)

    open_hours: dict[date, set[int]] = {}
    periods: dict[date, list] = {}
    for d in days:
        periods[d] = periods_for(doctor, d)
        hours = set()
        for p in periods[d]:
            h = p.start_at.astimezone(CAIRO).replace(minute=0, second=0, microsecond=0)
            while h < p.end_at:
                hours.add(h.hour)
                h += timedelta(hours=1)
        open_hours[d] = hours
    by_cell: dict[tuple[date, int], list] = {}
    for a in appts:
        by_cell.setdefault((a.date, a.start_at.astimezone(CAIRO).hour), []).append(_entry(a, now))
    hours = sorted(set().union(*open_hours.values()) | {h for _d, h in by_cell})

    earliest = _bookable_from(doctor, now)
    rows = []
    for h in hours:
        cells = []
        for d in days:
            is_open = h in open_hours[d]
            cell_end = local_dt(d, time(h)) + timedelta(hours=1)
            cells.append(
                {
                    "day": d,
                    "hour": h,
                    "entries": by_cell.get((d, h), []),
                    "open": is_open,
                    "bookable": is_open and cell_end > earliest and _within_horizon(doctor, d, now),
                    "is_today": d == today,
                }
            )
        rows.append({"hour": time(h), "cells": cells})
    columns = [
        {
            "day": d,
            "label": WEEKDAY_LABELS[weekday_egypt(d)],
            "periods": periods[d],
            "exceptions": exceptions.get(d, []),
            "is_today": d == today,
            "is_past": d < today,
            "count": sum(len(by_cell.get((d, h), [])) for h in hours),
        }
        for d in days
    ]
    return {"columns": columns, "rows": rows, "appointments": appts, "days": days, "start": days[0], "end": days[-1]}


def day_label(day: date) -> str:
    return fmt_date(day)
