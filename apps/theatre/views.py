"""Theatre screens: the day in the center (operations, recovery, who is admitted), one operation's page, admissions,
and the rooms / procedures set-up. Thin views: every change goes through theatre.services."""

from datetime import date, datetime, timedelta

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import has_perm, require_perm
from apps.audit import services as audit
from apps.center.models import EmergencyCase
from apps.core.ratelimit import search_limit
from apps.core.timeutils import CAIRO, WEEKDAY_LABELS, weekday_egypt
from apps.doctors import services as doctor_services
from apps.notifications.templating import fmt_date
from apps.obstetrics import services as obstetrics
from apps.patients.models import Patient
from apps.scheduling.views import week_start

from . import services
from .forms import AdmitForm, DischargeForm, NewbornForm, OperationForm, ProcedureForm, RoomForm, TeamMemberForm
from .models import (
    Admission,
    AdmissionStatus,
    ChecklistItem,
    Newborn,
    Operation,
    OperationConsumable,
    OperationStatus,
    OperationTeamMember,
    ProcedureType,
    Room,
)

STEP_TARGETS = {
    "admit": OperationStatus.ADMITTED,
    "start": OperationStatus.IN_THEATRE,
    "recover": OperationStatus.RECOVERY,
    "done": OperationStatus.DONE,
}
UNDO_TARGETS = {
    OperationStatus.ADMITTED: OperationStatus.SCHEDULED,
    OperationStatus.IN_THEATRE: OperationStatus.ADMITTED,
    OperationStatus.RECOVERY: OperationStatus.IN_THEATRE,
}
# the next step of an open operation: (action, button label)
NEXT_STEP = {
    OperationStatus.SCHEDULED: ("admit", _("المريضة وصلت")),
    OperationStatus.ADMITTED: ("start", _("دخلت العمليات")),
    OperationStatus.IN_THEATRE: ("recover", _("خرجت للإفاقة")),
    OperationStatus.RECOVERY: ("done", _("خلصت")),
}


def _parse_day(raw, default=None):
    try:
        return date.fromisoformat(raw or "")
    except ValueError:
        return default or services.today()


def _with_weeks(org, operations, request):
    """The pregnancy week next to each patient's name, as of the day of her operation."""
    for op in operations:
        op.step = NEXT_STEP.get(op.status)  # the one next step, as a button on the board
    if not operations or not has_perm(request.membership, "pregnancy.view_basic"):
        return operations
    by_day: dict = {}
    for op in operations:
        by_day.setdefault(op.date, []).append(op)
    for day, ops in by_day.items():
        badges = obstetrics.badges_for(org, [op.patient_id for op in ops], on=day)
        for op in ops:
            op.ga = badges.get(op.patient_id)
    return operations


# --- the board -------------------------------------------------------------------------------------------------


@require_perm("theatre.view")
def board(request):
    """The day in the center: today's operations room by room, who is in recovery, who is admitted. Or the week."""
    org = request.organization
    services.ensure_defaults(org)
    day = _parse_day(request.GET.get("date"))
    today = services.today()
    view = "week" if request.GET.get("view") == "week" else "day"
    ctx = {"day": day, "today": today, "view": view}
    if view == "week":
        start = week_start(day)
        days = [start + timedelta(days=i) for i in range(7)]
        ops = Operation.objects.for_org(org).filter(date__gte=days[0], date__lte=days[-1])
        ops = ops.exclude(status__in=[OperationStatus.POSTPONED, OperationStatus.CANCELLED])
        ops = _with_weeks(
            org, list(ops.select_related("patient", "room", "surgeon").order_by("planned_start")), request
        )
        ctx.update(
            columns=[
                {
                    "day": d,
                    "label": WEEKDAY_LABELS[weekday_egypt(d)],
                    "is_today": d == today,
                    "operations": [op for op in ops if op.date == d],
                }
                for d in days
            ],
            title=f"{fmt_date(days[0])} - {fmt_date(days[-1])}",
            prev_day=start - timedelta(days=7),
            next_day=start + timedelta(days=7),
        )
    else:
        ops = _with_weeks(org, services.operations_on(org, day), request)
        rooms = [r for r in Room.objects.for_org(org).filter(is_active=True) if r.is_theatre]
        shown = [op for op in ops if op.status != OperationStatus.CANCELLED]
        ctx.update(
            rooms=[{"room": r, "operations": [op for op in shown if op.room_id == r.pk]} for r in rooms],
            cancelled=[op for op in ops if op.status == OperationStatus.CANCELLED],
            title=fmt_date(day),
            prev_day=day - timedelta(days=1),
            next_day=day + timedelta(days=1),
            admitted=services.admitted_now(org) if day == today else [],
            waiting=services.waiting_for_a_date(org) if day == today else [],
        )
    return render(request, "theatre/board.html", ctx)


# --- booking ---------------------------------------------------------------------------------------------------


def _patient_results(request, target):
    """Patients matching what is typed; picking one goes to `target` with ?patient=."""
    q = request.GET.get("q", "").strip()
    patients = Patient.objects.for_org(request.organization).filter(is_active=True).search(q)[:8] if len(q) >= 2 else []
    return render(request, "theatre/partials/patient_results.html", {"patients": patients, "q": q, "target": target})


@require_perm("theatre.schedule")
@search_limit
def patient_search(request):
    return _patient_results(request, reverse("theatre:operation_new"))


@require_perm("admission.manage")
@search_limit
def admit_search(request):
    return _patient_results(request, reverse("theatre:admit"))


@require_perm("theatre.schedule")
def operation_new(request):
    org = request.organization
    services.ensure_defaults(org)
    pk = request.POST.get("patient") or request.GET.get("patient") or ""
    patient = Patient.objects.for_org(org).filter(pk=pk, is_active=True).first() if str(pk).isdigit() else None
    case_pk = request.POST.get("emergency_case") or request.GET.get("emergency_case") or ""
    case = EmergencyCase.objects.for_org(org).filter(pk=case_pk).first() if str(case_pk).isdigit() else None
    if case is not None and patient is None:
        patient = case.patient
    now = timezone.localtime(timezone.now(), CAIRO)
    initial = {
        "date": _parse_day(request.GET.get("date")),
        "time": (now + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0).time()
        if case is None
        else now.time().replace(second=0, microsecond=0),
        "surgeon": case.doctor_id if case else doctor_services.current_doctor(request).pk,
        "is_emergency": case is not None,
        "room": request.GET.get("room"),
    }
    form = OperationForm(request.POST or None, org=org, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if patient is None:
            form.add_error(None, _("اختار المريضة."))
        else:
            try:
                op = services.book_operation(
                    org=org, patient=patient, procedure=d["procedure"], room=d["room"], surgeon=d["surgeon"],
                    start=form.start(), duration_minutes=d["duration_minutes"], is_emergency=d["is_emergency"],
                    notes=d["notes"], emergency_case=case, by=request.user,
                )  # fmt: skip
            except services.TheatreError as e:
                form.add_error(None, str(e))
            else:
                audit.log(
                    "theatre.booked", request=request, target=op, summary=f"{patient.full_name}: {op.procedure_name}"
                )
                messages.success(request, _("العملية اتحجزت."))
                return redirect("theatre:operation", pk=op.pk)
    ctx = {"form": form, "patient": patient, "case": case}
    if patient is not None and has_perm(request.membership, "pregnancy.view_basic"):
        ctx["ga"] = obstetrics.badge_for(patient)
    return render(request, "theatre/operation_new.html", ctx)


# --- one operation ---------------------------------------------------------------------------------------------


def _op(request, pk):
    return get_object_or_404(
        Operation.objects.for_org(request.organization).select_related(
            "patient", "room", "surgeon", "procedure", "pregnancy", "postponed_to"
        ),
        pk=pk,
    )


def _stock_items(request):
    from apps.inventory.models import StockItem

    return list(StockItem.objects.for_org(request.organization).filter(is_active=True))


def _op_ctx(request, op):
    can_note = has_perm(request.membership, "clinical.view")
    ctx = {
        "op": op,
        "next": NEXT_STEP.get(op.status),
        "can_undo": op.status in UNDO_TARGETS,
        "checklist": list(op.checklist.all()),
        "team": list(op.team.select_related("doctor")),
        "team_form": TeamMemberForm(org=request.organization),
        "consumables": list(op.consumables.filter(is_removed=False).select_related("item")),
        "stock_items": _stock_items(request),
        "kit": services.kit_rows(op.procedure),
        "newborns": services.living_newborns(op) if op.is_delivery else [],
        "newborn_form": NewbornForm(),
        "events": list(op.events.select_related("by")),
        "admission": Admission.objects.filter(operation=op).exclude(status=AdmissionStatus.CANCELLED).first(),
        "editable": op.status not in (OperationStatus.CANCELLED, OperationStatus.POSTPONED),
        "team_total": sum((m.fee for m in op.team.all()), 0),
    }
    ctx["open_required"] = [i for i in ctx["checklist"] if i.required and not i.done]
    labels = dict(OperationStatus.choices)
    for event in ctx["events"]:
        event.to_status_label = labels.get(event.to_status, event.to_status)
    if can_note:
        ctx["note"] = services.latest_note(op)
    if has_perm(request.membership, "pregnancy.view_basic"):
        ctx["ga"] = obstetrics.badge_for(op.patient, on=op.date)
    return ctx


@require_perm("theatre.view")
def operation(request, pk):
    op = _op(request, pk)
    return render(request, "theatre/operation.html", _op_ctx(request, op))


def _section(request, op, name, error=""):
    """Re-render one part of the operation page after an HTMX action."""
    ctx = _op_ctx(request, op)
    ctx["error"] = error
    return render(request, f"theatre/partials/op_{name}.html", ctx)


@require_POST
@require_perm("theatre.operate")
def operation_step(request, pk, action):
    op = _op(request, pk)
    try:
        if action in STEP_TARGETS:
            services.transition(op, STEP_TARGETS[action], by=request.user)
        elif action == "undo" and op.status in UNDO_TARGETS:
            services.transition(op, UNDO_TARGETS[op.status], by=request.user, notes=_("رجوع خطوة"))
        else:
            return HttpResponse(status=400)
    except services.TheatreError as e:
        messages.error(request, str(e))
    else:
        audit.log("theatre.step", request=request, target=op, summary=f"{op.patient.full_name}: {action}")
    if request.POST.get("back") == "board":
        return redirect("theatre:board")
    return redirect("theatre:operation", pk=op.pk)


@require_perm("theatre.schedule")
def operation_move(request, pk):
    op = _op(request, pk)
    start = timezone.localtime(op.planned_start, CAIRO)
    initial = {"room": op.room_id, "date": start.date(), "time": start.time(),
               "duration_minutes": int((op.planned_end - op.planned_start).total_seconds() // 60)}  # fmt: skip
    form = OperationForm(request.POST or None, org=request.organization, moving=True, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            if op.status == OperationStatus.POSTPONED:
                op = services.rebook_postponed(op, start=form.start(), room=d["room"], by=request.user,
                                               duration_minutes=d["duration_minutes"])  # fmt: skip
            else:
                services.move_operation(op, start=form.start(), room=d["room"], duration_minutes=d["duration_minutes"],
                                        by=request.user, reason=request.POST.get("reason", ""))  # fmt: skip
        except services.TheatreError as e:
            form.add_error(None, str(e))
        else:
            audit.log("theatre.moved", request=request, target=op, summary=op.patient.full_name)
            messages.success(request, _("ميعاد العملية اتغير."))
            return redirect("theatre:operation", pk=op.pk)
    return render(request, "theatre/operation_move.html", {"op": op, "form": form})


@require_POST
@require_perm("theatre.schedule")
def operation_close(request, pk, how):
    """Cancel an operation, or postpone it without a new date yet."""
    op = _op(request, pk)
    target = {"cancel": OperationStatus.CANCELLED, "postpone": OperationStatus.POSTPONED}.get(how)
    if target is None:
        return HttpResponse(status=400)
    try:
        services.transition(op, target, by=request.user, notes=request.POST.get("reason", ""))
    except services.TheatreError as e:
        messages.error(request, str(e))
    else:
        audit.log(f"theatre.{how}", request=request, target=op, summary=op.patient.full_name)
        messages.success(
            request, _("العملية اتلغت.") if how == "cancel" else _("العملية اتأجلت. احجزلها ميعاد جديد لما يتحدد.")
        )
    return redirect("theatre:operation", pk=op.pk)


@require_POST
@require_perm("theatre.operate")
def checklist_toggle(request, pk):
    item = get_object_or_404(ChecklistItem.objects.for_org(request.organization).select_related("operation"), pk=pk)
    error = ""
    try:
        services.toggle_checklist(item, by=request.user)
    except services.TheatreError as e:
        error = str(e)
    return _section(request, _op(request, item.operation_id), "checklist", error)


@require_POST
@require_perm("theatre.operate")
def team_save(request, pk):
    op = _op(request, pk)
    member = None
    if request.POST.get("member"):
        member = get_object_or_404(OperationTeamMember.objects.filter(operation=op), pk=request.POST["member"])
    form = TeamMemberForm(request.POST, org=request.organization)
    error = ""
    if form.is_valid():
        d = form.cleaned_data
        try:
            services.save_team_member(op, role=d["role"], doctor=d["doctor"], name=d["name"], fee=d["fee"] or 0,
                                      member=member, by=request.user)  # fmt: skip
        except services.TheatreError as e:
            error = str(e)
    else:
        error = " ".join(str(e) for errors in form.errors.values() for e in errors)
    return _section(request, op, "team", error)


@require_POST
@require_perm("theatre.operate")
def team_remove(request, pk):
    member = get_object_or_404(OperationTeamMember.objects.for_org(request.organization), pk=pk)
    op = _op(request, member.operation_id)
    error = ""
    if op.status in (OperationStatus.CANCELLED, OperationStatus.POSTPONED):
        error = _("العملية دي ملغية أو متأجلة.")
    else:
        member.delete()  # a planning row, not a record: fees become financial only when the operation is invoiced
    return _section(request, op, "team", error)


@require_POST
@require_perm("theatre.operate")
def consumable_add(request, pk):
    op = _op(request, pk)
    error = ""
    item = None
    if request.POST.get("item"):
        item = next((i for i in _stock_items(request) if str(i.pk) == request.POST["item"]), None)
    try:
        services.add_consumable(op, name=request.POST.get("name", ""), quantity=request.POST.get("quantity") or 1,
                                unit=request.POST.get("unit", ""), item=item, by=request.user)  # fmt: skip
    except services.TheatreError as e:
        error = str(e)
    return _section(request, op, "consumables", error)


@require_POST
@require_perm("theatre.operate")
def consumable_kit(request, pk):
    op = _op(request, pk)
    error = ""
    try:
        services.issue_kit(op, by=request.user)
    except services.TheatreError as e:
        error = str(e)
    return _section(request, op, "consumables", error)


@require_POST
@require_perm("theatre.operate")
def consumable_remove(request, pk):
    item = get_object_or_404(OperationConsumable.objects.for_org(request.organization), pk=pk)
    op = _op(request, item.operation_id)
    error = ""
    try:
        services.remove_consumable(item, by=request.user)
    except services.TheatreError as e:
        error = str(e)
    return _section(request, op, "consumables", error)


@require_POST
@require_perm("theatre.operate")
def newborn_add(request, pk):
    op = _op(request, pk)
    form = NewbornForm(request.POST)
    error = ""
    if form.is_valid():
        d = form.cleaned_data
        born_at = None
        if d["born_time"]:
            born_at = datetime.combine(services.today(), d["born_time"], tzinfo=CAIRO)
        try:
            baby = services.add_newborn(op, sex=d["sex"], weight_g=d["weight_g"], apgar_1=d["apgar_1"],
                                        apgar_5=d["apgar_5"], born_at=born_at, status=d["status"], notes=d["notes"],
                                        by=request.user)  # fmt: skip
        except services.TheatreError as e:
            error = str(e)
        except Exception as e:  # model validation (weight / Apgar out of range)
            error = " ".join(getattr(e, "messages", [str(e)]))
        else:
            audit.log("theatre.newborn", request=request, target=baby, summary=op.patient.full_name)
    else:
        error = " ".join(str(e) for errors in form.errors.values() for e in errors)
    return _section(request, op, "newborns", error)


@require_POST
@require_perm("theatre.operate")
def newborn_remove(request, pk):
    baby = get_object_or_404(Newborn.objects.for_org(request.organization).select_related("operation"), pk=pk)
    error = ""
    try:
        services.remove_newborn(baby, by=request.user)
    except services.TheatreError as e:
        error = str(e)
    return _section(request, _op(request, baby.operation_id), "newborns", error)


# --- the surgeon's note (medical: clinical.view / clinical.edit) -----------------------------------------------


@require_POST
@require_perm("clinical.edit")
def note_save(request, pk):
    op = _op(request, pk)
    error = ""
    try:
        note = services.draft_note(op, by=request.user)
        services.save_note(note, request.POST, by=request.user)
        if request.POST.get("sign") == "1":
            services.sign_note(note, by=request.user)
            audit.log("theatre.note_signed", request=request, target=op, summary=op.patient.full_name)
    except services.TheatreError as e:
        error = str(e)
    return _section(request, op, "note", error)


@require_POST
@require_perm("clinical.edit")
def note_revise(request, pk):
    op = _op(request, pk)
    error = ""
    try:
        services.draft_note(op, by=request.user)
    except services.TheatreError as e:
        error = str(e)
    return _section(request, op, "note", error)


# --- admissions ------------------------------------------------------------------------------------------------


@require_perm("theatre.view")
def admissions(request):
    org = request.organization
    services.ensure_defaults(org)
    rooms = list(Room.objects.for_org(org).filter(is_active=True, kind__in=services.BED_KINDS))
    current = services.admitted_now(org)
    for room in rooms:
        room.patients = [a for a in current if a.room_id == room.pk]
        room.free = max(room.beds - len(room.patients), 0)
    past = Admission.objects.for_org(org).exclude(status=AdmissionStatus.ADMITTED).select_related("patient", "room")
    return render(request, "theatre/admissions.html", {"rooms": rooms, "past": past.order_by("-discharged_at")[:40]})


@require_perm("admission.manage")
def admit(request):
    org = request.organization
    pk = request.POST.get("patient") or request.GET.get("patient") or ""
    patient = Patient.objects.for_org(org).filter(pk=pk, is_active=True).first() if str(pk).isdigit() else None
    op_pk = request.POST.get("operation") or request.GET.get("operation") or ""
    op = Operation.objects.for_org(org).filter(pk=op_pk).first() if str(op_pk).isdigit() else None
    case_pk = request.POST.get("emergency_case") or request.GET.get("emergency_case") or ""
    case = EmergencyCase.objects.for_org(org).filter(pk=case_pk).first() if str(case_pk).isdigit() else None
    patient = patient or (op.patient if op else None) or (case.patient if case else None)
    form = AdmitForm(request.POST or None, org=org)
    if request.method == "POST" and form.is_valid():
        if patient is None:
            form.add_error(None, _("اختار المريضة."))
        else:
            try:
                admission = services.admit(
                    org=org, patient=patient, room=form.cleaned_data["room"], reason=form.cleaned_data["reason"],
                    operation=op, emergency_case=case, doctor=case.doctor if case else None, by=request.user,
                )  # fmt: skip
            except services.TheatreError as e:
                form.add_error(None, str(e))
            else:
                audit.log("theatre.admitted", request=request, target=admission, summary=patient.full_name)
                messages.success(request, _("%(p)s دخلت %(r)s.") % {"p": patient.full_name, "r": admission.room.name})
                return redirect("theatre:admissions")
    for room in form.fields["room"].queryset:
        room.free = services.beds_free(room)
    return render(request, "theatre/admit.html", {"form": form, "patient": patient, "op": op, "case": case})


def _admission(request, pk):
    return get_object_or_404(
        Admission.objects.for_org(request.organization).select_related("patient", "room", "doctor", "operation"), pk=pk
    )


@require_perm("admission.manage")
def discharge(request, pk):
    admission = _admission(request, pk)
    form = DischargeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            if request.POST.get("action") == "cancel":
                services.cancel_admission(admission, by=request.user)
                messages.success(request, _("الإقامة اتلغت."))
            else:
                d = form.cleaned_data
                # the summary is medical text: it is saved only when the person may write in the medical file
                clinical = has_perm(request.membership, "clinical.edit")
                services.discharge(admission, by=request.user, summary=d["summary"] if clinical else "",
                                   instructions=d["instructions"])  # fmt: skip
                messages.success(request, _("%(p)s خرجت.") % {"p": admission.patient.full_name})
        except services.TheatreError as e:
            form.add_error(None, str(e))
        else:
            audit.log("theatre.discharged", request=request, target=admission, summary=admission.patient.full_name)
            return redirect("theatre:admissions")
    ctx = {"admission": admission, "form": form, "nights": admission.nights(), "charge": admission.room_charge()}
    return render(request, "theatre/discharge.html", ctx)


@require_perm("clinical.view")
def discharge_pdf(request, pk):
    """The discharge summary as a PDF (medical: clinical.view)."""
    from apps.documents.pdf import font_css, logo_uri, render_pdf

    admission = _admission(request, pk)
    org_settings = getattr(request.organization, "settings", None)
    html = render_to_string(
        "pdf/discharge.html",
        {"admission": admission, "org": request.organization, "settings": org_settings, "font_css": font_css(),
         "logo_uri": logo_uri(org_settings), "nights": admission.nights(),
         "newborns": services.living_newborns(admission.operation) if admission.operation_id else []},
    )  # fmt: skip
    audit.log("theatre.discharge_printed", request=request, target=admission, summary=admission.patient.full_name)
    resp = HttpResponse(render_pdf(html, format="A4", print_background=True), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="discharge-{admission.pk}.pdf"'
    return resp


# --- set-up (Settings → العمليات والغرف) -------------------------------------------------------------------------


@require_perm("settings.manage")
def setup(request):
    org = request.organization
    services.ensure_defaults(org)
    ctx = {
        "rooms": Room.objects.for_org(org),
        "procedures": ProcedureType.objects.for_org(org),
        "tab": "theatre",
    }
    return render(request, "theatre/setup.html", ctx)


def _edit(request, model, form_class, pk, label):
    org = request.organization
    instance = get_object_or_404(model.objects.for_org(org), pk=pk) if pk else None
    form = form_class(request.POST or None, instance=instance, org=org)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.organization = org
        if instance is None:
            obj.order = model.objects.for_org(org).count()
        obj.save()
        audit.log("settings.theatre", request=request, target=obj, summary=str(obj))
        messages.success(request, _("%(o)s اتحفظ.") % {"o": obj})
        return redirect("theatre:setup")
    return render(
        request, "theatre/setup_form.html", {"form": form, "instance": instance, "label": label, "tab": "theatre"}
    )


@require_perm("settings.manage")
def room_edit(request, pk=None):
    return _edit(request, Room, RoomForm, pk, _("غرفة"))


@require_perm("settings.manage")
def procedure_edit(request, pk=None):
    return _edit(request, ProcedureType, ProcedureForm, pk, _("نوع عملية"))
