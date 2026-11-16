"""The pregnancy file on the doctor's desk (docs/plan/14-obgyn-center.md §14.2). Clinical views: `clinical.view` to
read, `clinical.edit` to change, behind the desk's idle lock; reception never reaches them (it only gets the
gestational-age tag next to a patient's name, with `pregnancy.view_basic`).

Every change answers with the fresh panel and an `ob-changed` event, so the tag in the patient header, the band
under it and the antenatal sheet of the open visit re-read themselves: one dating change moves every screen."""

import json

from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.permissions import has_perm, require_perm
from apps.audit import services as audit
from apps.clinical.models import Visit
from apps.clinical.services import log_clinical_view
from apps.clinical.views import desk_unlocked
from apps.doctors.services import get_doctor
from apps.patients.models import Patient

from . import services
from .context import antenatal_ctx, overview
from .forms import DatingForm, DetailsForm, OutcomeForm, ProfileForm
from .models import AntenatalVisit, Milestone, Pregnancy, PregnancyHistory


def _patient(request, pk):
    if not services.enabled(request.organization):
        raise Http404
    return get_object_or_404(Patient.objects.for_org(request.organization), pk=pk)


def _pregnancy(request, pk):
    if not services.enabled(request.organization):
        raise Http404
    return get_object_or_404(Pregnancy.objects.for_org(request.organization).select_related("patient"), pk=pk)


def _panel_ctx(request, patient, *, error="", forms=None):
    forms = forms or {}
    on = services.today()
    ctx = overview(patient, on)
    pregnancy, profile = ctx["ob_pregnancy"], services.profile_for(patient)
    ctx["ob_profile"] = profile
    rows = list(services.history_rows(patient).select_related("pregnancy"))
    sheets = []
    if pregnancy is not None:
        sheets = list(
            AntenatalVisit.objects.filter(pregnancy=pregnancy)
            .select_related("visit", "visit__vitals")
            .order_by("visit__created_at")
        )
        ctx["milestones"] = services.milestones_for(pregnancy, profile, on)
    last_closed = (
        Pregnancy.objects.filter(patient=patient).exclude(status="active").order_by("-ended_on", "-id").first()
        if pregnancy is None
        else None
    )
    dating = services.dating_of(pregnancy) if pregnancy else services.Dating(cycle_days=profile.cycle_days or 28)
    ctx.update(
        {
            "patient": patient,
            "applies": services.applies_to(patient),
            "can_edit": has_perm(request.membership, "clinical.edit"),
            "error": error,
            "rows": rows,
            "sheets": sheets,
            "last_closed": last_closed,
            "today": on,
            "dating_form": forms.get("dating")
            or DatingForm(
                initial={
                    "method": dating.method,
                    "lmp": dating.lmp,
                    "cycle_days": dating.cycle_days,
                    "us_date": dating.us_date,
                    "us_weeks": dating.us_weeks,
                    "us_days": dating.us_days,
                    "transfer_date": dating.transfer_date,
                    "embryo_age_days": dating.embryo_age_days,
                    "fetuses": pregnancy.fetuses if pregnancy else 1,
                }
            ),
            "details_form": forms.get("details")
            or DetailsForm(
                initial={
                    "fetuses": pregnancy.fetuses,
                    "risk_level": pregnancy.risk_level,
                    "risk_notes": pregnancy.risk_notes,
                    "notes": pregnancy.notes,
                }
                if pregnancy
                else None
            ),
            "close_form": forms.get("close") or OutcomeForm(initial={"ended_on": on}, prefix="close"),
            "history_form": forms.get("history") or OutcomeForm(prefix="past"),
            "profile_form": forms.get("profile") or ProfileForm(instance=profile),
        }
    )
    return ctx


def _panel(request, patient, *, changed=False, error="", forms=None):
    resp = render(
        request, "obstetrics/partials/panel.html", _panel_ctx(request, patient, error=error, forms=forms),
        status=200,
    )  # fmt: skip
    if changed:
        resp["HX-Trigger"] = json.dumps({"ob-changed": {"patient": patient.pk}})
    return resp


def _form_error(form) -> str:
    return " ".join(
        f"{form.fields[name].label}: {' '.join(errs)}" for name, errs in form.errors.items() if name in form.fields
    )


# --- read ------------------------------------------------------------------------------------------------------


@require_perm("clinical.view")
@desk_unlocked
def panel(request, pk):
    """The whole pregnancy file of a patient (drawer on the visit page, tab on her medical file)."""
    patient = _patient(request, pk)
    log_clinical_view(request, patient)
    return _panel(request, patient)


@require_perm("clinical.view")
@desk_unlocked
def tag(request, pk):
    """The gestational-age tag in the sticky patient header (re-read after a change)."""
    return render(request, "obstetrics/partials/header_tag.html", overview(_patient(request, pk)))


@require_perm("clinical.view")
@desk_unlocked
def band(request, pk):
    """The band under the patient header: EDD, counts, risk flags, what is due."""
    return render(request, "obstetrics/partials/band.html", overview(_patient(request, pk)))


@require_perm("clinical.view")
@desk_unlocked
def antenatal(request, pk):
    """The antenatal sheet of a visit (re-read when a pregnancy starts or its dating changes)."""
    visit = _visit(request, pk)
    return render(request, "obstetrics/partials/antenatal.html", antenatal_ctx(request, visit))


def _visit(request, pk):
    if not services.enabled(request.organization):
        raise Http404
    return get_object_or_404(Visit.objects.for_org(request.organization).select_related("patient"), pk=pk)


# --- write -----------------------------------------------------------------------------------------------------


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def pregnancy_start(request, pk):
    patient = _patient(request, pk)
    form = DatingForm(request.POST)
    if not form.is_valid():
        return _panel(request, patient, error=_form_error(form), forms={"dating": form})
    try:
        pregnancy = services.start_pregnancy(
            patient, form.dating(), by=request.user, doctor=get_doctor(request.organization),
            fetuses=form.cleaned_data.get("fetuses") or 1,
        )  # fmt: skip
    except services.ObstetricError as e:
        return _panel(request, patient, error=str(e), forms={"dating": form})
    audit.log("pregnancy.started", request=request, target=patient, summary=f"EDD {pregnancy.edd:%Y-%m-%d}")
    return _panel(request, patient, changed=True)


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def pregnancy_dating(request, pk):
    pregnancy = _pregnancy(request, pk)
    form = DatingForm(request.POST)
    if not form.is_valid():
        return _panel(request, pregnancy.patient, error=_form_error(form), forms={"dating": form})
    before = pregnancy.edd
    try:
        pregnancy = services.set_dating(pregnancy, form.dating())
    except services.ObstetricError as e:
        return _panel(request, pregnancy.patient, error=str(e), forms={"dating": form})
    audit.log(
        "pregnancy.dated", request=request, target=pregnancy.patient,
        summary=f"EDD {before:%Y-%m-%d} > {pregnancy.edd:%Y-%m-%d} ({pregnancy.edd_method})",
    )  # fmt: skip
    return _panel(request, pregnancy.patient, changed=True)


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def pregnancy_details(request, pk):
    pregnancy = _pregnancy(request, pk)
    form = DetailsForm(request.POST)
    if not form.is_valid():
        return _panel(request, pregnancy.patient, error=_form_error(form), forms={"details": form})
    try:
        services.update_details(pregnancy, **form.cleaned_data)
    except services.ObstetricError as e:
        return _panel(request, pregnancy.patient, error=str(e), forms={"details": form})
    return _panel(request, pregnancy.patient, changed=True)


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def pregnancy_close(request, pk):
    pregnancy = _pregnancy(request, pk)
    form = OutcomeForm(request.POST, prefix="close")
    if not form.is_valid():
        return _panel(request, pregnancy.patient, error=_form_error(form), forms={"close": form})
    try:
        row = services.close_pregnancy(
            pregnancy, outcome=form.cleaned_data["outcome"], ended_on=form.cleaned_data.get("ended_on"),
            by=request.user, **form.details(),
        )  # fmt: skip
    except services.ObstetricError as e:
        return _panel(request, pregnancy.patient, error=str(e), forms={"close": form})
    audit.log("pregnancy.closed", request=request, target=pregnancy.patient, summary=row.get_outcome_display())
    return _panel(request, pregnancy.patient, changed=True)


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def pregnancy_transfer(request, pk):
    pregnancy = _pregnancy(request, pk)
    try:
        services.transfer_pregnancy(pregnancy)
    except services.ObstetricError as e:
        return _panel(request, pregnancy.patient, error=str(e))
    audit.log("pregnancy.closed", request=request, target=pregnancy.patient, summary="transferred")
    return _panel(request, pregnancy.patient, changed=True)


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def pregnancy_reopen(request, pk):
    pregnancy = _pregnancy(request, pk)
    try:
        services.reopen_pregnancy(pregnancy)
    except services.ObstetricError as e:
        return _panel(request, pregnancy.patient, error=str(e))
    audit.log("pregnancy.reopened", request=request, target=pregnancy.patient)
    return _panel(request, pregnancy.patient, changed=True)


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def history_add(request, pk):
    patient = _patient(request, pk)
    form = OutcomeForm(request.POST, prefix="past")
    if not form.is_valid():
        return _panel(request, patient, error=_form_error(form), forms={"history": form})
    try:
        services.add_history(
            patient, outcome=form.cleaned_data["outcome"], by=request.user, year=form.cleaned_data.get("year"),
            weeks=form.cleaned_data.get("weeks"), **form.details(),
        )  # fmt: skip
    except services.ObstetricError as e:
        return _panel(request, patient, error=str(e), forms={"history": form})
    return _panel(request, patient, changed=True)


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def history_remove(request, pk):
    if not services.enabled(request.organization):
        raise Http404
    row = get_object_or_404(PregnancyHistory.objects.for_org(request.organization).select_related("patient"), pk=pk)
    try:
        services.remove_history(row)
    except services.ObstetricError as e:
        return _panel(request, row.patient, error=str(e))
    audit.log("pregnancy.history_removed", request=request, target=row.patient, summary=row.get_outcome_display())
    return _panel(request, row.patient, changed=True)


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def profile_save(request, pk):
    patient = _patient(request, pk)
    form = ProfileForm(request.POST, instance=services.profile_for(patient))
    if not form.is_valid():
        return _panel(request, patient, error=_form_error(form), forms={"profile": form})
    form.save()
    return _panel(request, patient, changed=True)


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def milestone_toggle(request, pk, milestone_pk):
    pregnancy = _pregnancy(request, pk)
    milestone = get_object_or_404(Milestone.objects.for_org(request.organization), pk=milestone_pk)
    services.toggle_milestone(pregnancy, milestone, by=request.user)
    return _panel(request, pregnancy.patient, changed=True)


@require_POST
@require_perm("clinical.edit")
@desk_unlocked
def antenatal_field(request, pk):
    visit = _visit(request, pk)
    try:
        services.save_antenatal_field(visit, request.POST.get("field", ""), request.POST.get("value", ""))
    except ValidationError as e:
        return render(request, "clinical/partials/autosave.html", {"error": " ".join(e.messages)})
    return render(request, "clinical/partials/autosave.html", {"saved_at": timezone.now()})
