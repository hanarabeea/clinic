"""Center mode screens: the services catalog (Settings), services on a visit, and emergency arrivals. Thin views:
everything that changes data goes through center.services / scheduling.services."""

import json
from datetime import date

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import has_perm, require_perm
from apps.audit import services as audit
from apps.billing import services as billing
from apps.core.ratelimit import search_limit
from apps.core.timeutils import CAIRO
from apps.doctors import services as doctor_services
from apps.obstetrics import services as obstetrics
from apps.patients import services as patient_services
from apps.patients.models import Gender, Patient
from apps.scheduling.models import Appointment

from . import services
from .forms import CenterSettingsForm, DispositionForm, EmergencyForm, ServiceForm
from .models import CenterSettings, EmergencyCase, Service, ServiceCategory, ServiceOrder, Triage


def _today():
    return timezone.localtime(timezone.now(), CAIRO).date()


def _events(resp, **events):
    resp["HX-Trigger"] = json.dumps(events)
    return resp


def _toast(text, error=False):
    return {"text": text, "error": error}


# --- Settings → الخدمات ----------------------------------------------------------------------------------------

CATEGORY_ORDER = {value: i for i, value in enumerate(ServiceCategory.values)}


def _category_rank(service) -> int:
    return CATEGORY_ORDER.get(service.category, len(CATEGORY_ORDER))


def _catalog_ctx(request, form=None, fee_form=None):
    org = request.organization
    return {
        "services": sorted(Service.objects.for_org(org).order_by("order", "id"), key=_category_rank),
        "form": form or ServiceForm(org=org),
        "fee_form": fee_form or CenterSettingsForm(instance=CenterSettings.for_org(org)),
        "tab": "services",
    }


@require_perm("settings.manage")
def services_settings(request):
    org = request.organization
    form = fee_form = None
    if request.method == "POST" and request.POST.get("action") == "fee":
        fee_form = CenterSettingsForm(request.POST, instance=CenterSettings.for_org(org))
        if fee_form.is_valid():
            fee_form.save()
            audit.log("settings.center", request=request, fields=sorted(fee_form.changed_data))
            messages.success(request, _("رسوم الطوارئ اتحفظت."))
            return redirect("center:services")
    elif request.method == "POST":
        form = ServiceForm(request.POST, org=org)
        if form.is_valid():
            service = form.save(commit=False)
            service.organization = org
            service.order = Service.objects.for_org(org).count()
            service.save()
            audit.log("settings.service", request=request, target=service, summary=service.name_ar)
            messages.success(request, _("%(s)s اتضافت.") % {"s": service.name_ar})
            return redirect("center:services")
    return render(request, "center/services_settings.html", _catalog_ctx(request, form, fee_form))


@require_perm("settings.manage")
def service_edit(request, pk):
    service = get_object_or_404(Service.objects.for_org(request.organization), pk=pk)
    form = ServiceForm(request.POST or None, instance=service, org=request.organization)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit.log("settings.service", request=request, target=service, fields=sorted(form.changed_data))
        messages.success(request, _("%(s)s اتحفظت.") % {"s": service.name_ar})
        return redirect("center:services")
    return render(request, "center/service_edit.html", {"form": form, "service": service, "tab": "services"})


@require_POST
@require_perm("settings.manage")
def service_toggle(request, pk):
    service = get_object_or_404(Service.objects.for_org(request.organization), pk=pk)
    service.is_active = not service.is_active
    service.save(update_fields=["is_active", "updated_at"])
    audit.log("settings.service", request=request, target=service, summary=f"{service.name_ar}: {service.is_active}")
    return redirect("center:services")


# --- services on a visit ---------------------------------------------------------------------------------------


def _appt(request, pk):
    return get_object_or_404(
        Appointment.objects.for_org(request.organization).select_related("patient", "doctor", "visit_type"), pk=pk
    )


def _orders_ctx(request, appt, error="", adding=False):
    orders = services.orders_for(appt)
    by_category = []
    catalog = list(services.active_services(request.organization).order_by("category", "order", "id"))
    for value, label in ServiceCategory.choices:
        items = [s for s in catalog if s.category == value]
        if items:
            by_category.append((label, items))
    return {
        "a": appt,
        "orders": orders,
        "balance": billing.balance(appt, orders=orders),
        "visit_due": appt.price - appt.discount,
        "by_category": by_category,
        "open": appt.status not in services.NOT_BILLABLE,
        "error": error,
        # inline = inside the doctor's visit form: the list only, the catalog folded until asked for
        "inline": bool(request.GET.get("inline")),
        "adding": adding,  # just added one: keep the catalog open for the next
    }


@require_perm("service.order")
def orders(request, appt_pk):
    """The services of one visit: in a dialog on the reception board, or inline in the doctor's workspace."""
    appt = _appt(request, appt_pk)
    template = "center/partials/orders.html" if request.GET.get("inline") else "center/partials/orders_modal.html"
    return render(request, template, _orders_ctx(request, appt))


@require_POST
@require_perm("service.order")
def order_add(request, appt_pk):
    appt = _appt(request, appt_pk)
    service = get_object_or_404(Service.objects.for_org(request.organization), pk=request.POST.get("service") or 0)
    error = ""
    try:
        order = services.order_service(appt, service, by=request.user, quantity=request.POST.get("quantity") or 1)
    except services.CenterError as e:
        error = str(e)
    else:
        audit.log("service.ordered", request=request, target=appt, summary=f"{appt.patient.full_name}: {order.name}")
    resp = render(request, "center/partials/orders.html", _orders_ctx(request, appt, error, adding=True))
    return _events(resp, **{"orders-changed": True})


@require_POST
@require_perm("service.order")
def order_cancel(request, pk):
    order = get_object_or_404(
        ServiceOrder.objects.for_org(request.organization).select_related("appointment__patient"), pk=pk
    )
    appt = _appt(request, order.appointment_id)
    error = ""
    try:
        services.cancel_order(order, by=request.user)
    except services.CenterError as e:
        error = str(e)
    else:
        audit.log("service.cancelled", request=request, target=appt, summary=f"{appt.patient.full_name}: {order.name}")
    resp = render(request, "center/partials/orders.html", _orders_ctx(request, appt, error))
    return _events(resp, **{"orders-changed": True})


# --- emergency arrivals ----------------------------------------------------------------------------------------


def _register_ctx(request, form, patient=None):
    doctors = doctor_services.active_doctors(request.organization)
    ctx = {"form": form, "patient": patient, "doctors": doctors, "triages": Triage.choices}
    if patient is not None and has_perm(request.membership, "pregnancy.view_basic"):
        ctx["ga"] = obstetrics.badge_for(patient)
    return ctx


@require_perm("emergency.register")
def emergency_register(request):
    """The red button on the reception board: who (an existing patient or just a name and a phone), how urgent, why,
    which doctor. She lands on the doctor's queue ahead of everyone, already arrived."""
    org = request.organization
    doctors = doctor_services.active_doctors(org)
    if request.method == "GET":
        pk = request.GET.get("patient", "")
        patient = Patient.objects.for_org(org).filter(pk=pk, is_active=True).first() if pk.isdigit() else None
        chosen = doctor_services.board_doctor(request, doctors) or doctor_services.own_doctor(request)
        initial = {
            "patient": patient.pk if patient else None,
            "doctor": chosen.pk if chosen else (doctors[0].pk if len(doctors) == 1 else None),
            "gender": Gender.FEMALE if obstetrics.enabled(org) else "",
            "triage": request.GET.get("triage") or Triage.URGENT,
        }
        form = EmergencyForm(initial=initial, org=org, doctors=doctors)
        return render(request, "center/partials/emergency_modal.html", _register_ctx(request, form, patient))

    form = EmergencyForm(request.POST, org=org, doctors=doctors)
    if form.is_valid():
        d = form.cleaned_data
        patient = form.patient_obj
        try:
            if patient is None:
                patient = patient_services.create_patient(
                    org, Patient(full_name=d["full_name"], phone=d["phone"], gender=d["gender"]), by=request.user
                )
                audit.log("patient.created", request=request, target=patient, summary=patient.full_name)
            case = services.register_emergency(
                org=org, patient=patient, doctor=d["doctor_obj"], triage=d["triage"], reason=d["reason"],
                reason_text=d["reason_text"], by=request.user,
            )  # fmt: skip
        except services.CenterError as e:
            form.add_error(None, str(e))
        else:
            audit.log(
                "emergency.registered", request=request, target=case,
                summary=f"{patient.full_name}: {case.get_triage_display()}",
            )  # fmt: skip
            done = _("%(p)s اتسجلت طوارئ عند %(d)s") % {"p": patient.full_name, "d": case.doctor}
            return _events(HttpResponse(""), toast=_toast(done), **{"board-refresh": True})
    return render(request, "center/partials/emergency_modal.html", _register_ctx(request, form, form.patient_obj))


@require_perm("emergency.register")
@search_limit
def emergency_search(request):
    q = request.GET.get("q", "").strip()
    patients = Patient.objects.for_org(request.organization).filter(is_active=True).search(q)[:6] if len(q) >= 3 else []
    return render(request, "center/partials/emergency_search.html", {"patients": patients, "q": q})


def _case(request, pk):
    return get_object_or_404(
        EmergencyCase.objects.for_org(request.organization).select_related("patient", "doctor", "appointment"), pk=pk
    )


@require_perm("emergency.register")
def emergency_disposition(request, pk):
    """Where she went after the emergency visit (home, theatre, a room, another hospital)."""
    case = _case(request, pk)
    form = DispositionForm(
        request.POST or None, initial={"disposition": case.disposition, "notes": case.disposition_notes}
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        case = services.set_disposition(case, d["disposition"], notes=d["notes"], by=request.user)
        audit.log(
            "emergency.disposition", request=request, target=case,
            summary=f"{case.patient.full_name}: {case.get_disposition_display()}",
        )  # fmt: skip
        done = _("%(p)s: %(d)s") % {"p": case.patient.full_name, "d": case.get_disposition_display()}
        return _events(HttpResponse(""), toast=_toast(done), **{"board-refresh": True, "emergency-changed": True})
    return render(request, "center/partials/disposition_modal.html", {"case": case, "form": form})


@require_POST
@require_perm("emergency.register")
def emergency_cancel(request, pk):
    case = _case(request, pk)
    try:
        services.cancel_emergency(case, by=request.user, reason=request.POST.get("reason", ""))
    except (services.CenterError, services.booking.BookingError) as e:
        return _events(HttpResponse(status=204), toast=_toast(str(e), error=True))
    audit.log("emergency.cancelled", request=request, target=case, summary=case.patient.full_name)
    done = _("تسجيل الطوارئ اتلغى")
    return _events(HttpResponse(status=204), toast=_toast(done), **{"board-refresh": True})


@require_perm("emergency.register")
def emergency_band(request, pk):
    """The emergency strip of the doctor's visit page (re-read after the disposition changes)."""
    return render(request, "center/partials/emergency_band.html", {"emergency": _case(request, pk)})


@require_perm("emergency.register")
def emergencies(request):
    """The emergency log: one day's arrivals with their three times (arrived, seen, what happened next)."""
    try:
        day = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        day = _today()
    cases = services.cases_on(request.organization, day)
    seen = [c.minutes_to_seen for c in cases if c.minutes_to_seen is not None]
    pregnant = {}
    if has_perm(request.membership, "pregnancy.view_basic"):
        pregnant = obstetrics.badges_for(request.organization, [c.patient_id for c in cases], on=day)
    for c in cases:
        c.ga = pregnant.get(c.patient_id)
    ctx = {
        "day": day,
        "today": _today(),
        "cases": cases,
        "critical_count": sum(1 for c in cases if c.triage == Triage.CRITICAL),
        "avg_wait": round(sum(seen) / len(seen)) if seen else None,
        "undecided": sum(1 for c in cases if not c.disposition),
    }
    return render(request, "center/emergencies.html", ctx)
