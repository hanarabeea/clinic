"""What the OB/GYN layer adds to other apps' pages (the doctor's patient header and visit page). Kept apart from
views.py so apps/clinical can import it without a circle."""

from django.utils import timezone

from apps.accounts.permissions import has_perm
from apps.core.timeutils import CAIRO

from . import services
from .models import AntenatalVisit, ObstetricProfile


def overview(patient, on=None) -> dict:
    """What the header tag and the band show for one patient (None values when she isn't pregnant)."""
    on = on or services.today()
    pregnancy = services.active_pregnancy(patient)
    profile = ObstetricProfile.objects.filter(patient=patient).first()
    rows = list(services.history_rows(patient))
    counts = services.gpal(patient, rows, pregnant=pregnancy is not None)
    ctx = {
        "ob_patient": patient,
        "ob_pregnancy": pregnancy,
        "ob_profile": profile,
        "ob_counts": counts,
        "ob_has_history": bool(rows),
        "ga": None,
        "ob_flags": [],
        "ob_due": [],
        "ob_overdue": 0,
    }
    if pregnancy is not None:
        ctx["ga"] = pregnancy.ga(on)
        ctx["ob_flags"] = services.risk_flags(patient, pregnancy, profile, counts, on)
        rows = services.milestones_for(pregnancy, profile, on)
        ctx["ob_due"] = [m for m in rows if m.state == "due"]
        # not ticked here and past its weeks: one quiet count (a woman first seen late had them done elsewhere)
        ctx["ob_overdue"] = sum(1 for m in rows if m.state == "overdue")
    return ctx


def antenatal_ctx(request, visit) -> dict:
    """The antenatal sheet on the visit page. Shown for a woman with a followed pregnancy, or a visit that already
    has a sheet (an old visit of a pregnancy since closed)."""
    sheet = services.antenatal_for(visit)
    pregnancy = sheet.pregnancy if sheet else services.active_pregnancy(visit.patient)
    if pregnancy is None:
        return {"visit": visit, "an_pregnancy": None}
    day = timezone.localtime(visit.started_at or visit.created_at, CAIRO).date()
    ga = pregnancy.ga(day)

    def field(name):
        return AntenatalVisit._meta.get_field(name)

    def value(name):
        v = getattr(sheet, name, None) if sheet else None
        return "" if v is None else v

    numbers = [
        # name, unit, step
        ("fundal_height_cm", "cm", "0.5"),
        ("fetal_heart_rate", "bpm", "1"),
        ("hemoglobin", "g/dL", "0.1"),
    ]
    scan_numbers = [("efw_g", "g", "1"), ("afi_cm", "cm", "0.1")]
    selects = ["fetal_movement", "presentation", "edema", "urine_albumin", "urine_sugar"]
    return {
        "visit": visit,
        "an_pregnancy": pregnancy,
        "an_sheet": sheet,
        "an_ga": ga,
        "an_edit": has_perm(request.membership, "clinical.edit"),
        "an_numbers": [(n, field(n).verbose_name, value(n), unit, step) for n, unit, step in numbers],
        "an_scan_numbers": [(n, field(n).verbose_name, value(n), unit, step) for n, unit, step in scan_numbers],
        "an_selects": [(n, field(n).verbose_name, value(n), field(n).choices) for n in selects],
        "an_placenta": value("placenta"),
        "an_scan_notes": value("ultrasound_notes"),
        "an_next_days": ga.next_visit_weeks * 7,
        "an_next_weeks": ga.next_visit_weeks,
    }
