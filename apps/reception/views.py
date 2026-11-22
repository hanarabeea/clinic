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
from apps.center import services as center
from apps.clinical import services as clinical
from apps.clinical.models import Visit, Vitals
from apps.core.ratelimit import search_limit
from apps.core.timeutils import CAIRO
from apps.doctors import services as doctor_services
from apps.doctors.models import VisitType
from apps.notifications import replies
from apps.notifications.templating import fmt_date, fmt_time
from apps.obstetrics import services as obstetrics
from apps.patients.models import Patient
from apps.scheduling import services as booking
from apps.scheduling import waitlist
from apps.scheduling.models import Appointment, AppointmentStatus, BookingSource
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


def _signature(appts, emergencies, view, day, who) -> str:
    raw = f"{view}:{day}:{who}:" + "|".join(
        f"{a.pk}:{a.status}:{a.updated_at.timestamp()}:{len(a.payments.all())}"
        f":{sum(1 for o in a.service_orders.all() if o.is_active)}"
        for a in appts
    )
    raw += "#" + "|".join(f"{c.pk}:{c.appointment.status}:{c.triage}:{c.disposition}" for c in emergencies)
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]


def _board_ctx(request, view, day):
    """One board per doctor shown: the picked doctor, or every active doctor one under the other ("all")."""
    org = request.organization
    doctors = doctor_services.active_doctors(org)
    selected = doctor_services.board_doctor(request, doctors)
    shown = [selected] if selected else doctors
    now = timezone.now()
    today = _today()
    if view == "week":
        start = week_start(day)
        boards = [board.week_board(org, d, start, now) for d in shown]
        prev_day, next_day = start - timedelta(days=7), start + timedelta(days=7)
        title = f"{fmt_date(start)} – {fmt_date(start + timedelta(days=6))}"
        is_today = start <= today <= start + timedelta(days=6)
    else:
        boards = [board.day_board(org, d, day, now) for d in shown]
        prev_day, next_day = day - timedelta(days=1), day + timedelta(days=1)
        title = fmt_date(day)
        is_today = day == today
    # emergencies in the building right now, whoever's board is open: the whole center has to see them
    emergencies = center.active_cases(org, today)
    pregnant = obstetrics.badges_for(org, [c.patient_id for c in emergencies]) if emergencies else {}
    for case in emergencies:
        case.ga = pregnant.get(case.patient_id)
        arrived = case.appointment.status == AppointmentStatus.ARRIVED
        case.waiting = int((now - case.arrived_at).total_seconds() // 60) if arrived else None
    appts = [a for b in boards for a in b["appointments"]]
    return {
        "boards": boards,
        "board": boards[0],
        "view": view,
        "day": day,
        "today": today,
        "is_today": is_today,
        "title": title,
        "prev_day": prev_day,
        "next_day": next_day,
        "doctors": doctors,
        "selected_doctor": selected,
        "doctor": selected or shown[0],
        "doctor_param": selected.pk if selected else doctor_services.ALL,
        "slots_mode": boards[0]["slots_mode"],
        "emergencies": emergencies,
        "sig": _signature(appts, emergencies, view, day, selected.pk if selected else "all"),
    }


@require_perm("appointment.view")
def today(request):
    """The reception board: today's arrivals and payments, the day / week schedule and new bookings — one screen."""
    view, day = _state(request)
    ctx = _board_ctx(request, view, day)
    if has_perm(request.membership, "reception.operate"):
        ctx["followups"] = clinical.followups_due(request.organization)
    ctx["waitlist_count"] = waitlist.open_entries(request.organization).count()
    if has_perm(request.membership, "messages.view"):  # patients' messages nobody handled yet (plan 15 §1.2)
        ctx["inbox_count"] = replies.unhandled(request.organization).count()
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
            if action == "call":
                center.mark_seen(appt)  # an emergency arrival: the time the doctor took her in
            return _refresh(request)
        if action == "no_show":
            rebooked = booking.mark_no_show(appt, by=request.user)
            if rebooked:
                when = f"{fmt_date(rebooked.date)} {fmt_time(rebooked.start_at)}"
                return _refresh(request, toast=_("لم يحضر، اتحجزله %(w)s") % {"w": when})
            return _refresh(request, toast=_("لم يحضر"))
        if action == "undo":
            if appt.source == BookingSource.EMERGENCY and appt.status == AppointmentStatus.ARRIVED:
                # "not arrived" makes no sense for someone who came in as an emergency
                return _refresh(request, error=_("دي حالة طوارئ. لو اتسجلت بالغلط الغي تسجيلها."))
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


@require_perm("invoice.discount")
def discount(request, pk):
    """A discount on the visit fee, with its reason. Above the clinic's limit it needs "invoice.discount_any"."""
    from apps.invoicing import services as invoicing
    from apps.invoicing.models import InvoiceSettings

    appt = _appt(request, pk)
    error = ""
    if request.method == "POST":
        try:
            invoicing.set_visit_discount(
                appt, amount=request.POST.get("amount") or 0, reason=request.POST.get("reason", ""),
                by=request.user, membership=request.membership,
            )  # fmt: skip
        except invoicing.InvoiceError as e:
            error = str(e)
        else:
            audit.log("invoice.visit_discount", request=request, target=appt, summary=request.POST.get("amount", "0"))
            return _refresh(request, toast=_("الخصم اتسجل"))
    ctx = {"a": appt, "error": error, "limit": InvoiceSettings.for_org(request.organization).discount_limit_percent,
           "amount": request.POST.get("amount", appt.discount or ""),
           "reason": request.POST.get("reason", appt.discount_reason)}  # fmt: skip
    resp = render(request, "reception/partials/discount_modal.html", ctx)
    if request.method == "POST":
        resp["HX-Retarget"] = "#modal"
    return resp


# --- walk-in (12 §8) -------------------------------------------------------------------------


@require_perm("reception.operate")
@search_limit
def walk_in(request):
    doctor = doctor_services.current_doctor(request)
    active_types = VisitType.objects.for_org(request.organization).filter(is_active=True, doctor__is_active=True)
    visit_types = active_types.filter(doctor=doctor)
    if request.method == "POST":
        patient = get_object_or_404(Patient.objects.for_org(request.organization), pk=request.POST.get("patient"))
        # the visit type says which doctor: each doctor has their own
        visit_type = get_object_or_404(active_types.select_related("doctor"), pk=request.POST.get("visit_type"))
        doctor = visit_type.doctor
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
    several = len(doctor_services.active_doctors(request.organization)) > 1
    if several:
        visit_types = active_types.select_related("doctor").order_by("doctor_id", "order", "id")
    ctx = {"q": q, "patients": patients, "visit_types": visit_types.filter(is_system=False), "several": several}
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
