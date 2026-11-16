from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import require_perm
from apps.audit import services as audit
from apps.core.timeutils import WEEKDAY_LABELS

from . import services
from .forms import DoctorForm, NewDoctorForm, ScheduleExceptionForm, VisitTypeForm, WorkingPeriodForm
from .models import Doctor, ScheduleException, VisitType, WorkingPeriod
from .services import settings_doctor as _get_doctor


def _all_doctors(request):
    return list(Doctor.objects.filter(organization=request.organization).select_related("user").order_by("id"))


def _schedule_ctx(request, doctor, form=None):
    periods = WorkingPeriod.objects.filter(doctor=doctor).order_by("weekday", "start_time")
    by_weekday = [(i, label, [p for p in periods if p.weekday == i]) for i, label in enumerate(WEEKDAY_LABELS)]
    exceptions = ScheduleException.objects.filter(doctor=doctor).order_by("date")
    return {
        "doctor": doctor,
        "doctors": _all_doctors(request),
        "form": form or DoctorForm(instance=doctor),
        "by_weekday": by_weekday,
        "exceptions": exceptions,
        "period_form": WorkingPeriodForm(),
        "exception_form": ScheduleExceptionForm(),
        "tab": "schedule",
    }


def _to(name, doctor):
    return f"{reverse(name)}?doctor={doctor.pk}"


# --- the clinic's doctors -----------------------------------------------------------------------


@require_perm("schedule.manage")
def doctors_list(request):
    """Settings → الدكاترة: who works here, add one, switch one off (never deleted: their visits stay)."""
    _get_doctor(request)  # a clinic always has at least one
    doctors = _all_doctors(request)
    form = NewDoctorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        source = next((d for d in doctors if d.is_active), None) if request.POST.get("copy_visit_types") else None
        doctor = services.add_doctor(request.organization, form.save(commit=False), copy_visit_types_from=source)
        audit.log("settings.doctor", request=request, target=doctor, summary=f"{_('إضافة دكتور')}: {doctor}")
        messages.success(request, _("%(d)s اتضاف. ظبّط مواعيده.") % {"d": doctor})
        return redirect(_to("doctors:schedule", doctor))
    for d in doctors:
        d.upcoming = services.upcoming_bookings(d) if d.is_active else 0
    return render(request, "doctors/settings_doctors.html", {"doctors": doctors, "form": form, "tab": "doctors"})


@require_POST
@require_perm("schedule.manage")
def doctor_toggle(request, pk):
    doctor = get_object_or_404(Doctor.objects.for_org(request.organization), pk=pk)
    try:
        services.set_active(doctor, not doctor.is_active)
    except services.DoctorError as e:
        messages.error(request, str(e))
    else:
        verb = _("إيقاف دكتور") if doctor.is_active else _("تفعيل دكتور")
        audit.log("settings.doctor", request=request, target=doctor, summary=f"{verb}: {doctor}")
    return redirect("doctors:list")


# --- one doctor: profile, weekly schedule, exceptions ---------------------------------------------


@require_perm("schedule.manage")
def schedule_settings(request):
    doctor = _get_doctor(request)
    form = DoctorForm(request.POST or None, instance=doctor) if request.method == "POST" else None
    if form and form.is_valid():
        form.save()
        audit.log("settings.doctor", request=request, fields=sorted(form.changed_data))
        messages.success(request, _("بيانات الدكتور اتحفظت."))
        return redirect(_to("doctors:schedule", doctor))
    return render(request, "doctors/settings_schedule.html", _schedule_ctx(request, doctor, form))


def _periods_partial(request, doctor):
    return render(request, "doctors/partials/periods.html", _schedule_ctx(request, doctor))


@require_perm("schedule.manage")
def period_add(request):
    doctor = _get_doctor(request)
    form = WorkingPeriodForm(request.POST)
    if form.is_valid():
        period = form.save(commit=False)
        period.organization = request.organization
        period.doctor = doctor
        try:
            period.full_clean()
        except ValidationError as e:
            for field, errs in e.message_dict.items():
                form.add_error(field if field in form.fields else None, errs)
        else:
            period.save()
            audit.log("settings.doctor", request=request, summary=_("إضافة فترة عمل"))
    if not form.is_valid():
        ctx = _schedule_ctx(request, doctor)
        ctx["period_form"] = form
        return render(request, "doctors/partials/periods.html", ctx)
    return _periods_partial(request, doctor)


@require_perm("schedule.manage")
def period_delete(request, pk):
    period = get_object_or_404(WorkingPeriod.objects.for_org(request.organization).select_related("doctor"), pk=pk)
    doctor = period.doctor  # the row says whose it is: never the "current" doctor
    period.delete()
    audit.log("settings.doctor", request=request, summary=_("مسح فترة عمل"))
    return _periods_partial(request, doctor)


def _exceptions_partial(request, doctor):
    return render(request, "doctors/partials/exceptions.html", _schedule_ctx(request, doctor))


@require_perm("schedule.manage")
def exception_add(request):
    doctor = _get_doctor(request)
    form = ScheduleExceptionForm(request.POST)
    if not form.is_valid():
        ctx = _schedule_ctx(request, doctor)
        ctx["exception_form"] = form
        return render(request, "doctors/partials/exceptions.html", ctx)
    exc = form.save(commit=False)
    exc.organization = request.organization
    exc.doctor = doctor
    exc.save()
    audit.log("settings.doctor", request=request, summary=f"{_('استثناء جدول')}: {exc.date}")
    if exc.kind in ("closed", "custom"):
        from apps.scheduling.services import affected_by_closing

        if affected_by_closing(doctor, exc.date):
            url = f"{reverse('scheduling:affected')}?date={exc.date}&doctor={doctor.pk}"
            return HttpResponse(headers={"HX-Redirect": url})
    return _exceptions_partial(request, doctor)


@require_perm("schedule.manage")
def exception_delete(request, pk):
    exc = get_object_or_404(ScheduleException.objects.for_org(request.organization).select_related("doctor"), pk=pk)
    doctor = exc.doctor
    exc.delete()
    audit.log("settings.doctor", request=request, summary=_("مسح استثناء جدول"))
    return _exceptions_partial(request, doctor)


# --- visit types -----------------------------------------------------------------------------


def _visit_types_ctx(request, doctor):
    return {
        "doctor": doctor,
        "doctors": _all_doctors(request),
        "visit_types": VisitType.objects.filter(doctor=doctor, is_system=False).order_by("order", "id"),
        "form": VisitTypeForm(),
        "tab": "visit_types",
    }


@require_perm("schedule.manage")
def visit_types_settings(request):
    doctor = _get_doctor(request)
    return render(request, "doctors/settings_visit_types.html", _visit_types_ctx(request, doctor))


def _visit_types_partial(request, doctor):
    return render(request, "doctors/partials/visit_types.html", _visit_types_ctx(request, doctor))


@require_perm("schedule.manage")
def visit_type_add(request):
    doctor = _get_doctor(request)
    form = VisitTypeForm(request.POST)
    if form.is_valid():
        vt = form.save(commit=False)
        vt.organization = request.organization
        vt.doctor = doctor
        vt.order = VisitType.objects.filter(doctor=doctor).count()
        vt.save()
        audit.log("settings.visit_type", request=request, summary=vt.name_ar)
        return _visit_types_partial(request, doctor)
    ctx = _visit_types_ctx(request, doctor)
    ctx["form"] = form
    return render(request, "doctors/partials/visit_types.html", ctx)


@require_perm("schedule.manage")
def visit_type_delete(request, pk):
    vt = get_object_or_404(
        VisitType.objects.for_org(request.organization).filter(is_system=False).select_related("doctor"), pk=pk
    )
    doctor = vt.doctor
    if vt.appointments.exists():
        # bookings were made with it: it can't disappear from their records, so it is only switched off
        vt.is_active = False
        vt.save(update_fields=["is_active", "updated_at"])
        audit.log("settings.visit_type", request=request, summary=f"{_('إيقاف نوع زيارة')}: {vt.name_ar}")
    else:
        vt.delete()
        audit.log("settings.visit_type", request=request, summary=_("مسح نوع زيارة"))
    return _visit_types_partial(request, doctor)
