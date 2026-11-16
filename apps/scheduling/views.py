import json
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import has_perm, require_perm
from apps.audit import services as audit
from apps.core.ratelimit import search_limit
from apps.core.templatetags.clock import ampm
from apps.core.timeutils import CAIRO, weekday_egypt
from apps.doctors import services as doctor_services
from apps.doctors.models import BookingMode, ScheduleException, ScheduleExceptionKind, VisitType
from apps.notifications.models import Event, MessageStatus
from apps.notifications.templating import fmt_date
from apps.obstetrics import services as obstetrics
from apps.patients import services as patient_services
from apps.patients.forms import PatientForm
from apps.patients.models import Patient

from . import services
from .models import ACTIVE_STATUSES, Appointment, AppointmentStatus


def _parse_day(raw) -> date:
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    return date.today()


def _board(day=None, view=None, **extra) -> str:
    """The reception board (reception:today) — bookings, the day and the week all live there now."""
    params = {k: v for k, v in {"date": day.isoformat() if day else None, "view": view, **extra}.items() if v}
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return reverse("reception:today") + (f"?{query}" if query else "")


def _toast(resp, text, error=False, refresh=True):
    """HTMX: close the booking panel (empty #modal), show a toast and refresh the board."""
    events = {"toast": {"text": text, "error": error}}
    if refresh:
        events["board-refresh"] = True
    resp["HX-Trigger"] = json.dumps(events)
    return resp


# --- booking (a panel on the reception board) ---------------------------------------------------


@require_perm("appointment.book")
def booking_home(request):
    return redirect(_board(book="1"))


@require_perm("appointment.book")
def booking_panel(request):
    """The booking panel: find / add the patient, pick the visit type and a time — opened from "+ حجز جديد" or a free
    cell of the board (`at` = that slot, or `day` + `hour` from the week table)."""
    org = request.organization
    doctor = doctor_services.current_doctor(request)
    pk = request.GET.get("patient", "")
    patient = Patient.objects.for_org(org).filter(pk=pk, is_active=True).first() if pk.isdigit() else None
    return render(request, "scheduling/partials/booking_panel.html", _panel_ctx(request, doctor, patient))


def _panel_ctx(request, doctor, patient, error=""):
    data = request.GET if request.method == "GET" else request.POST
    at = None
    if data.get("at"):
        try:
            at = datetime.fromisoformat(data["at"])
            at = at if at.tzinfo else at.replace(tzinfo=CAIRO)
        except ValueError:
            at = None
    day = at.astimezone(CAIRO).date() if at else _parse_day(data.get("day"))
    visit_types = list(
        VisitType.objects.for_org(request.organization).filter(doctor=doctor, is_active=True, is_system=False)
    )
    chosen = str(data.get("visit_type") or "")
    visit_type = next((vt for vt in visit_types if str(vt.pk) == chosen), visit_types[0] if visit_types else None)
    ctx = {
        "patient": patient,
        "doctor": doctor,
        "doctors": doctor_services.active_doctors(request.organization),  # several: the panel asks which one
        "day": day,
        "at": at.isoformat() if at else "",
        "at_dt": at,
        "hour": data.get("hour", ""),
        "visit_types": visit_types,
        "visit_type": visit_type,
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1),
        "is_today": day == timezone.localtime(timezone.now(), CAIRO).date(),
        "slots_mode": doctor.booking_mode == BookingMode.SLOTS,
        "error": error,
    }
    if patient is not None and has_perm(request.membership, "pregnancy.view_basic"):
        ctx["ga"] = obstetrics.badge_for(patient, on=day)  # how far along she will be on the day being booked
    if patient is None or visit_type is None:
        return ctx
    ctx["upcoming"] = (
        Appointment.objects.for_org(request.organization)
        .filter(patient=patient, status__in=ACTIVE_STATUSES, start_at__gte=timezone.now())
        .select_related("visit_type")
        .order_by("start_at")[:3]
    )
    if ctx["slots_mode"]:
        slots = services.free_slots(doctor, visit_type, day)
        ctx["slots"] = slots
        hour = int(ctx["hour"]) if str(ctx["hour"]).isdigit() else None
        pick = next((s for s in slots if at and s == at), None) or next(
            (s for s in slots if hour is not None and s.astimezone(CAIRO).hour == hour), None
        )
        ctx["picked"] = pick.isoformat() if pick else ""
    else:
        ctx["periods"] = services.queue_availability(doctor, day)
    return ctx


@require_perm("appointment.book")
@search_limit
def booking_search(request):
    q = request.GET.get("q", "").strip()
    patients = Patient.objects.for_org(request.organization).filter(is_active=True).search(q)[:8] if q else []
    digits = sum(ch.isdigit() for ch in q)
    looks_like_phone = bool(q) and digits >= max(len(q) - 2, 1)
    initial = {"phone": q} if looks_like_phone else {"full_name": q}
    form = PatientForm(initial=initial) if q else None
    ctx = {"q": q, "patients": patients, "form": form, "day": request.GET.get("day", ""),
           "at": request.GET.get("at", ""), "hour": request.GET.get("hour", ""),
           "doctor": doctor_services.current_doctor(request)}  # fmt: skip
    return render(request, "scheduling/partials/search_results.html", ctx)


@require_perm("appointment.book")
def booking_new_patient(request):
    """Add the patient, then straight to picking their time (the panel); an invalid form comes back in place."""
    form = PatientForm(request.POST)
    if form.is_valid():
        patient = patient_services.create_patient(request.organization, form.save(commit=False), by=request.user)
        audit.log("patient.created", request=request, target=patient, summary=patient.full_name)
        if request.htmx:
            ctx = _panel_ctx(request, doctor_services.current_doctor(request), patient)
            resp = render(request, "scheduling/partials/booking_panel.html", ctx)
            resp["HX-Retarget"], resp["HX-Reswap"] = "#modal", "innerHTML"
            return resp
        return redirect(_board(book=patient.pk))
    ctx = {"form": form, "day": request.POST.get("day", ""), "at": request.POST.get("at", ""),
           "hour": request.POST.get("hour", ""), "doctor": doctor_services.current_doctor(request)}  # fmt: skip
    return render(request, "scheduling/partials/new_patient_form.html", ctx)


def _patient(request, pk):
    return get_object_or_404(Patient.objects.for_org(request.organization), pk=pk)


def _visit_type(request, pk):
    """The visit type being booked. It also says which doctor: every doctor has their own visit types."""
    types = VisitType.objects.for_org(request.organization).filter(is_system=False, doctor__is_active=True)
    pk = str(pk or "")
    return get_object_or_404(types.select_related("doctor"), pk=pk if pk.isdigit() else 0)


@require_perm("appointment.book")
def booking_patient(request, patient_pk):
    """Old full-page booking (bookmarks / follow-up links): the board opens the booking panel for this patient."""
    patient = _patient(request, patient_pk)
    return redirect(_board(day=_parse_day(request.GET.get("day")), book=patient.pk))


@require_perm("appointment.book")
def booking_slots(request, patient_pk):
    patient = _patient(request, patient_pk)
    day = _parse_day(request.GET.get("day"))
    visit_type = _visit_type(request, request.GET.get("visit_type"))
    doctor = visit_type.doctor
    ctx = {"patient": patient, "doctor": doctor, "visit_type": visit_type, "day": day}
    if doctor.booking_mode == BookingMode.SLOTS:
        ctx["slots"] = services.free_slots(doctor, visit_type, day)
    else:
        ctx["periods"] = services.queue_availability(doctor, day)
    return render(request, "scheduling/partials/slots.html", ctx)


@require_perm("appointment.book")
def booking_confirm(request, patient_pk):
    patient = _patient(request, patient_pk)
    visit_type = _visit_type(request, request.POST.get("visit_type"))
    doctor = visit_type.doctor
    overbook = request.POST.get("overbook") == "1" and has_perm(request.membership, "appointment.overbook")
    try:
        if doctor.booking_mode == BookingMode.SLOTS:
            start_at = datetime.fromisoformat(request.POST["start_at"])
            if start_at.tzinfo is None:
                start_at = start_at.replace(tzinfo=CAIRO)
            appt = services.book(
                patient=patient,
                doctor=doctor,
                visit_type=visit_type,
                by=request.user,
                start_at=start_at,
                overbook=overbook,
            )
        else:
            day = _parse_day(request.POST.get("day"))
            appt = services.book(
                patient=patient,
                doctor=doctor,
                visit_type=visit_type,
                by=request.user,
                day=day,
                period_key=request.POST.get("period_key"),
                overbook=overbook,
            )
    except services.BookingError as e:
        if request.htmx:
            return render(request, "scheduling/partials/booking_panel.html",
                          _panel_ctx(request, doctor, patient, error=str(e)))  # fmt: skip
        messages.error(request, str(e))
        return redirect(_board(day=_parse_day(request.POST.get("day")), book=patient.pk))

    audit.log(
        "appointment.booked", request=request, target=appt, summary=f"{patient.full_name}: {appt.visit_type.name_ar}"
    )
    label = f"#{appt.queue_number}" if appt.queue_number else f"{fmt_date(appt.date)} {ampm(appt.start_at)}"
    done = _("اتحجز لـ%(p)s، %(w)s") % {"p": patient.full_name, "w": label}
    if request.htmx:
        if not patient.messaging_consent:
            done += " " + _("مفيش رسايل هتتبعت، المريض مش موافق على الرسايل.")
        return _toast(HttpResponse(""), done)
    messages.success(request, done)
    scheduled = appt.messages.filter(status=MessageStatus.PENDING).order_by("send_at")
    if scheduled:
        parts = [
            _("تأكيد الآن")
            if m.event == Event.BOOKING_CONFIRMED
            else _("تذكير %(t)s") % {"t": m.send_at.astimezone(CAIRO).strftime("%m/%d %H:%M")}
            for m in scheduled
        ]
        messages.info(request, _("الرسايل: %(l)s") % {"l": " · ".join(parts)})
    elif not patient.messaging_consent:
        messages.warning(request, _("مفيش رسايل هتتبعت، المريض مش موافق على الرسايل."))
    return redirect(_board(day=appt.date))


# --- day / week calendars → the reception board ---------------------------------------------------


@require_perm("appointment.view")
def appointments_day(request):
    return redirect(_board(day=_parse_day(request.GET.get("date")), view="day"))


def _appt(request, pk):
    return get_object_or_404(Appointment.objects.for_org(request.organization), pk=pk)


@require_perm("appointment.reschedule")
def appointment_confirm(request, pk):
    appt = _appt(request, pk)
    try:
        services.transition(appt, AppointmentStatus.CONFIRMED, by=request.user)
    except services.InvalidTransition as e:
        messages.error(request, str(e))
    if request.htmx:
        return _toast(HttpResponse(""), _("الحجز اتأكد."), refresh=True)
    return redirect(_board(day=appt.date))


@require_perm("appointment.cancel")
def appointment_cancel(request, pk):
    appt = _appt(request, pk)
    try:
        services.cancel(appt, by=request.user, reason=request.POST.get("reason", ""))
        audit.log("appointment.cancelled", request=request, target=appt, summary=appt.patient.full_name)
        messages.success(request, _("اتلغى الحجز."))
    except services.BookingError as e:
        messages.error(request, str(e))
    return redirect(_board(day=appt.date))


# --- affected appointments (closing a day) ------------------------------------------------------


@require_perm("schedule.manage")
def affected_appointments(request):
    """Bookings that no longer fit after a day was closed: one doctor's (`?doctor=`), or every doctor's."""
    doctors = doctor_services.active_doctors(request.organization)
    if request.GET.get("doctor", "").isdigit():
        doctors = [doctor_services.current_doctor(request, doctors)]
    day = _parse_day(request.GET.get("date"))
    affected = [pair for d in doctors for pair in services.affected_by_closing(d, day)]
    ctx = {"doctor": doctors[0], "several": len(doctors) > 1, "day": day, "affected": affected}
    return render(request, "scheduling/affected.html", ctx)


@require_perm("schedule.manage")
def affected_reschedule(request, pk):
    appt = _appt(request, pk)
    try:
        new_appt = services.reschedule_to_suggestion(appt, by=request.user)
        audit.log("appointment.rescheduled", request=request, target=new_appt, summary=appt.patient.full_name)
        messages.success(request, _("اتأجل حجز %(p)s.") % {"p": appt.patient.full_name})
    except services.BookingError as e:
        messages.error(request, str(e))
    return redirect(f"{reverse('scheduling:affected')}?date={appt.date}&doctor={appt.doctor_id}")


# --- week calendar (03 §3.3) -----------------------------------------------------------------


def week_start(day: date) -> date:
    """Egyptian week: Saturday → Friday."""
    return day - timedelta(days=weekday_egypt(day))


@require_perm("appointment.view")
def appointments_week(request):
    return redirect(_board(day=_parse_day(request.GET.get("date")), view="week"))


@require_perm("appointment.reschedule")
def reschedule_options(request, pk):
    """Modal for moving an appointment (drag to another day, or the "تأجيل" button): that day's free times."""
    appt = _appt(request, pk)
    if not appt.is_active or appt.status not in (AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED):
        return render(request, "scheduling/partials/reschedule_modal.html",
                      {"a": appt, "error": _("الحجز ده مينفعش يتأجل دلوقتي.")})  # fmt: skip
    doctor = appt.doctor
    day = _parse_day(request.GET.get("day") or appt.date.isoformat())
    ctx = {"a": appt, "day": day, "doctor": doctor}
    if doctor.booking_mode == BookingMode.SLOTS:
        ctx["slots"] = services.free_slots(doctor, appt.visit_type, day)
    else:
        ctx["periods"] = services.queue_availability(doctor, day)
    return render(request, "scheduling/partials/reschedule_modal.html", ctx)


@require_POST
@require_perm("appointment.reschedule")
def reschedule_confirm(request, pk):
    appt = _appt(request, pk)
    week = request.POST.get("week") or appt.date.isoformat()
    try:
        if appt.doctor.booking_mode == BookingMode.SLOTS:
            start_at = datetime.fromisoformat(request.POST["start_at"])
            if start_at.tzinfo is None:
                start_at = start_at.replace(tzinfo=CAIRO)
            new = services.reschedule(appt, by=request.user, new_start_at=start_at, reason=_("تأجيل من التقويم"))
        else:
            new = services.reschedule(appt, by=request.user, new_day=_parse_day(request.POST.get("day")),
                                      new_period_key=request.POST.get("period_key"),
                                      reason=_("تأجيل من التقويم"))  # fmt: skip
    except (KeyError, ValueError, services.BookingError) as e:
        messages.error(request, str(e) or _("اختار ميعاد."))
        return redirect(_board(day=_parse_day(week)))
    audit.log("appointment.rescheduled", request=request, target=new, summary=appt.patient.full_name)
    when = f"#{new.queue_number}" if new.queue_number else f"{fmt_date(new.date)} {ampm(new.start_at)}"
    messages.success(request, _("اتأجل لـ%(w)s، والمريض هتوصله رسالة بالتعديل.") % {"w": when})
    return redirect(_board(day=new.date))


@require_POST
@require_perm("schedule.manage")
def close_day(request):
    """ "قفل يوم" from the calendar → closed exception → the affected-bookings screen if anything is booked (12 §5)."""
    doctors = doctor_services.active_doctors(request.organization)
    whole_center = request.POST.get("doctor") == doctor_services.ALL
    if not whole_center:  # one doctor's day; "all" (the board showing everyone) closes the center for the day
        doctors = [doctor_services.current_doctor(request, doctors)]
    day = _parse_day(request.POST.get("date"))
    affected = False
    for doctor in doctors:
        _exc, created = ScheduleException.objects.get_or_create(
            organization=request.organization, doctor=doctor, date=day, kind=ScheduleExceptionKind.CLOSED,
            defaults={"reason": request.POST.get("reason", "")[:150]},
        )  # fmt: skip
        if created:
            audit.log("settings.doctor", request=request, summary=f"{_('قفل يوم')}: {day} ({doctor})")
        affected = affected or bool(services.affected_by_closing(doctor, day))
    if affected:
        query = f"?date={day.isoformat()}" + ("" if whole_center else f"&doctor={doctors[0].pk}")
        return redirect(reverse("scheduling:affected") + query)
    messages.success(request, _("اليوم اتقفل."))
    return redirect(_board(day=day))
