"""Reception "today" screen (03 §3.4). Thin views: every state change goes through scheduling/billing services."""

import hashlib
import json
from datetime import datetime, timedelta

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import has_perm, require_perm
from apps.audit import services as audit
from apps.billing import services as billing
from apps.clinical import services as clinical
from apps.clinical.models import Visit, Vitals
from apps.core.ratelimit import search_limit
from apps.core.timeutils import CAIRO
from apps.doctors.models import BookingMode, VisitType
from apps.doctors.services import get_doctor
from apps.notifications.templating import fmt_date, fmt_time
from apps.patients.models import Patient
from apps.scheduling import services as booking
from apps.scheduling.models import Appointment, AppointmentStatus
from apps.scheduling.views import week_start

from . import board
from .forms import PaymentForm, VitalsForm

VIEWS = ("day", "week")


def _today():
    return timezone.localtime(timezone.now(), CAIRO).date()


def _parse_day(raw):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
    except ValueError:
        return None


def _state(request):
    """The day / week this user is looking at: the query string, else the view they last used (the date resets to
    today) — remembered in the session, so actions (arrive, pay...) re-render the board the user is on."""
    view = request.GET.get("view") or request.session.get("board_view") or "day"
    view = view if view in VIEWS else "day"
    day = _parse_day(request.GET.get("date")) or _today()
    request.session["board_view"], request.session["board_date"] = view, day.isoformat()
    return view, day


def _session_state(request):
    view = request.session.get("board_view") if request.session.get("board_view") in VIEWS else "day"
    return view, _parse_day(request.session.get("board_date")) or _today()


def _signature(appts, view, day) -> str:
    raw = f"{view}:{day}:" + "|".join(
        f"{a.pk}:{a.status}:{a.updated_at.timestamp()}:{len(a.payments.all())}" for a in appts
    )
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]


def _board_ctx(request, view, day):
    doctor = get_doctor(request.organization)
    now = timezone.now()
    if view == "week":
        start = week_start(day)
        b = board.week_board(request.organization, doctor, start, now)
        prev_day, next_day = start - timedelta(days=7), start + timedelta(days=7)
        title = f"{fmt_date(b['start'])} – {fmt_date(b['end'])}"
    else:
        b = board.day_board(request.organization, doctor, day, now)
        prev_day, next_day = day - timedelta(days=1), day + timedelta(days=1)
        title = fmt_date(day)
    today = _today()
    return {
        "board": b,
        "view": view,
        "day": day,
        "today": today,
        "is_today": day == today if view == "day" else b["start"] <= today <= b["end"],
        "title": title,
        "prev_day": prev_day,
        "next_day": next_day,
        "doctor": doctor,
        "slots_mode": doctor.booking_mode == BookingMode.SLOTS,
        "sig": _signature(b["appointments"], view, day),
    }


@require_perm("appointment.view")
def today(request):
    """The reception board: today's arrivals and payments, the day / week schedule and new bookings — one screen."""
    view, day = _state(request)
    ctx = _board_ctx(request, view, day)
    if has_perm(request.membership, "reception.operate"):
        ctx["followups"] = clinical.followups_due(request.organization)
    ctx["book"] = request.GET.get("book", "")  # ?book=<patient> opens the booking panel (follow-ups, old links)
    return render(request, "reception/today.html", ctx)


@require_perm("appointment.view")
def rows(request):
    """Polled every 5 s; 204 (no swap) when nothing changed since the client's signature."""
    view, day = _state(request)
    ctx = _board_ctx(request, view, day)
    if request.GET.get("sig") == ctx["sig"]:
        return HttpResponse(status=204)
    return render(request, "reception/partials/board.html", ctx)


def _appt(request, pk):
    return get_object_or_404(
        Appointment.objects.for_org(request.organization).select_related("patient", "doctor", "visit_type"), pk=pk
    )


def _refresh(request, toast=None, error=None):
    """Re-render the board the user is on (HTMX) — the modal closes client-side on success."""
    resp = render(request, "reception/partials/board.html", _board_ctx(request, *_session_state(request)))
    if toast or error:
        resp["HX-Trigger"] = json.dumps({"toast": {"text": toast or error, "error": bool(error)}})
    return resp


# --- check-in (+ optional vitals) ------------------------------------------------------------


@require_perm("reception.operate")
def arrive(request, pk):
    appt = _appt(request, pk)
    can_vitals = has_perm(request.membership, "vitals.record")
    if request.method == "GET":
        return render(
            request, "reception/partials/arrive_modal.html", {"a": appt, "form": VitalsForm(), "can_vitals": can_vitals}
        )
    form = VitalsForm(request.POST) if can_vitals else None
    if form is not None and not form.is_valid():
        resp = render(request, "reception/partials/arrive_modal.html", {"a": appt, "form": form, "can_vitals": True})
        resp["HX-Retarget"] = "#modal"
        return resp
    try:
        booking.transition(appt, AppointmentStatus.ARRIVED, by=request.user)
    except booking.InvalidTransition as e:
        return _refresh(request, error=str(e))
    if form is not None:
        vitals = form.save(commit=False)
        if not vitals.is_empty:
            visit = Visit.for_appointment(appt)
            vitals.organization, vitals.visit, vitals.recorded_by = request.organization, visit, request.user
            vitals.save()
    return _refresh(request, toast=_("%(p)s وصل") % {"p": appt.patient.full_name})


@require_perm("vitals.record")
def vitals(request, pk):
    appt = _appt(request, pk)
    visit = Visit.for_appointment(appt)
    instance = Vitals.objects.filter(visit=visit).first()
    form = VitalsForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        v = form.save(commit=False)
        v.organization, v.visit, v.recorded_by = request.organization, visit, request.user
        v.save()
        return _refresh(request, toast=_("العلامات الحيوية اتسجلت"))
    resp = render(request, "reception/partials/vitals_modal.html", {"a": appt, "form": form})
    if request.method == "POST":
        resp["HX-Retarget"] = "#modal"
    return resp


# --- status steps ----------------------------------------------------------------------------

STEP_TARGETS = {
    "call": AppointmentStatus.IN_CONSULTATION,
    "complete": AppointmentStatus.COMPLETED,
}
UNDO_TARGETS = {
    AppointmentStatus.ARRIVED: AppointmentStatus.BOOKED,
    AppointmentStatus.IN_CONSULTATION: AppointmentStatus.ARRIVED,
}


@require_POST
@require_perm("reception.operate")
def step(request, pk, action):
    appt = _appt(request, pk)
    try:
        if action in STEP_TARGETS:
            booking.transition(appt, STEP_TARGETS[action], by=request.user)
            return _refresh(request)
        if action == "no_show":
            rebooked = booking.mark_no_show(appt, by=request.user)
            if rebooked:
                when = f"{fmt_date(rebooked.date)} {fmt_time(rebooked.start_at)}"
                return _refresh(request, toast=_("لم يحضر — اتحجزله %(w)s") % {"w": when})
            return _refresh(request, toast=_("لم يحضر"))
        if action == "undo":
            if appt.status == AppointmentStatus.NO_SHOW:
                booking.undo_no_show(appt, by=request.user)
            elif appt.status in UNDO_TARGETS:
                booking.transition(appt, UNDO_TARGETS[appt.status], by=request.user, notes=_("رجوع خطوة"))
            else:
                return _refresh(request, error=_("مفيش خطوة ترجعلها."))
            return _refresh(request, toast=_("رجعنا خطوة"))
    except (booking.InvalidTransition, booking.BookingError) as e:
        return _refresh(request, error=str(e))
    return HttpResponse(status=400)


# --- payments --------------------------------------------------------------------------------


@require_perm("payment.record")
def payment(request, pk):
    appt = _appt(request, pk)
    bal = billing.balance(appt)
    form = PaymentForm(request.POST or None, initial={"amount": bal.remaining or None, "method": "cash"})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            p = billing.record_payment(
                appt, amount=d["amount"], method=d["method"], note=d["note"], is_refund=d["is_refund"], by=request.user
            )
        except billing.PaymentError as e:
            form.add_error("amount", str(e))
        else:
            audit.log("payment.recorded", request=request, target=appt, summary=str(p))
            return _refresh(request, toast=_("اتسجل %(a)s") % {"a": p})
    resp = render(request, "reception/partials/payment_modal.html", {"a": appt, "form": form, "balance": bal})
    if request.method == "POST":
        resp["HX-Retarget"] = "#modal"
    return resp


# --- walk-in (12 §8) -------------------------------------------------------------------------


@require_perm("reception.operate")
@search_limit
def walk_in(request):
    doctor = get_doctor(request.organization)
    visit_types = VisitType.objects.for_org(request.organization).filter(doctor=doctor, is_active=True)
    if request.method == "POST":
        patient = get_object_or_404(Patient.objects.for_org(request.organization), pk=request.POST.get("patient"))
        visit_type = get_object_or_404(visit_types, pk=request.POST.get("visit_type"))
        overbook = request.POST.get("overbook") == "1" and has_perm(request.membership, "appointment.overbook")
        try:
            appt = booking.walk_in(
                patient=patient,
                doctor=doctor,
                visit_type=visit_type,
                by=request.user,
                overbook=overbook,
            )
        except booking.BookingError as e:
            if request.htmx:  # from the booking panel: say why, keep the panel open
                resp = HttpResponse(status=204)
                resp["HX-Trigger"] = json.dumps({"toast": {"text": str(e), "error": True}})
                return resp
            messages.error(request, str(e))
            return redirect(f"{request.path}?q={patient.phone}")
        audit.log("appointment.walk_in", request=request, target=appt, summary=patient.full_name)
        done = _("%(p)s اتسجل حضور مباشر") % {"p": patient.full_name}
        if request.htmx:  # the panel closes (#modal emptied) and the board refreshes
            resp = HttpResponse("")
            resp["HX-Trigger"] = json.dumps({"toast": {"text": done, "error": False}, "board-refresh": True})
            return resp
        messages.success(request, done)
        return redirect("reception:today")
    q = request.GET.get("q", "").strip()
    patients = Patient.objects.for_org(request.organization).filter(is_active=True).search(q)[:15] if q else []
    ctx = {"q": q, "patients": patients, "visit_types": visit_types}
    if request.htmx:
        return render(request, "reception/partials/walk_in_results.html", ctx)
    return render(request, "reception/walk_in.html", ctx)


@require_POST
@require_perm("reception.operate")
def followup_dismiss(request, visit_pk):
    """Reception's "إعادات محتاجة حجز" task: hide one that won't be booked (patient declined, booked by phone...)."""
    visit = get_object_or_404(Visit.objects.for_org(request.organization), pk=visit_pk)
    Visit.objects.filter(pk=visit.pk).update(followup_handled=True)
    return redirect("reception:today")
