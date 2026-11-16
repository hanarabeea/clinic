"""Everything that reads or changes the OB/GYN layer (views stay thin; docs/plan/14-obgyn-center.md §14.2)."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.timeutils import CAIRO
from apps.patients.models import Gender

from . import calc
from .models import (
    BIRTHS,
    LIVE_BIRTHS,
    AntenatalVisit,
    EddMethod,
    Milestone,
    MilestoneCheck,
    MilestoneKind,
    ObstetricProfile,
    Outcome,
    Pregnancy,
    PregnancyHistory,
    PregnancyStatus,
    RiskLevel,
)


class ObstetricError(Exception):
    pass


def today() -> date:
    return timezone.localtime(timezone.now(), CAIRO).date()


def enabled(org) -> bool:
    """The layer is on for clinics whose specialty is OB/GYN (Settings → العيادة)."""
    settings = getattr(org, "settings", None) if org else None
    return bool(settings and settings.specialty == "obgyn")


def applies_to(patient) -> bool:
    return patient.gender == Gender.FEMALE


# --- dating ----------------------------------------------------------------------------------------------------


@dataclass
class Dating:
    """What the doctor entered to date a pregnancy (one method's fields are used, the others kept as typed)."""

    method: str = EddMethod.LMP
    lmp: date | None = None
    cycle_days: int = calc.STANDARD_CYCLE
    us_date: date | None = None
    us_weeks: int | None = None
    us_days: int = 0
    transfer_date: date | None = None
    embryo_age_days: int = 5

    def edd(self, on: date) -> date:
        """The expected date of delivery by the chosen method; ValidationError says what is missing or impossible."""
        if self.method == EddMethod.LMP:
            if not self.lmp:
                raise ValidationError(_("اكتب أول يوم في آخر دورة."))
            if self.lmp > on:
                raise ValidationError(_("تاريخ آخر دورة مينفعش يكون في المستقبل."))
            if not 15 <= self.cycle_days <= 60:
                raise ValidationError(_("طول الدورة لازم يكون بين 15 و60 يوم."))
            edd = calc.edd_from_lmp(self.lmp, self.cycle_days)
        elif self.method == EddMethod.ULTRASOUND:
            if not self.us_date or self.us_weeks is None:
                raise ValidationError(_("اكتب تاريخ السونار وعمر الحمل اللي طلع فيه."))
            if self.us_date > on:
                raise ValidationError(_("تاريخ السونار مينفعش يكون في المستقبل."))
            if not (0 <= self.us_weeks <= 44 and 0 <= self.us_days <= 6):
                raise ValidationError(_("عمر الحمل في السونار مش منطقي."))
            edd = calc.edd_from_ultrasound(self.us_date, self.us_weeks, self.us_days)
        elif self.method == EddMethod.IVF:
            if not self.transfer_date:
                raise ValidationError(_("اكتب تاريخ نقل الأجنة."))
            if self.transfer_date > on:
                raise ValidationError(_("تاريخ النقل مينفعش يكون في المستقبل."))
            if not 2 <= self.embryo_age_days <= 6:
                raise ValidationError(_("عمر الجنين عند النقل لازم يكون من 2 لـ6 أيام."))
            edd = calc.edd_from_transfer(self.transfer_date, self.embryo_age_days)
        else:
            raise ValidationError(_("طريقة حساب مش معروفة."))
        age = calc.ga_days(edd, on)
        if age > calc.MAX_REASONABLE_DAYS:
            raise ValidationError(_("بالتواريخ دي الحمل عدّى 44 أسبوع. راجع التاريخ."))
        return edd


def _apply_dating(pregnancy, dating: Dating, on: date):
    pregnancy.edd = dating.edd(on)
    pregnancy.edd_method = dating.method
    pregnancy.lmp, pregnancy.cycle_days = dating.lmp, dating.cycle_days
    pregnancy.us_date, pregnancy.us_weeks, pregnancy.us_days = dating.us_date, dating.us_weeks, dating.us_days
    pregnancy.transfer_date, pregnancy.embryo_age_days = dating.transfer_date, dating.embryo_age_days


def dating_of(pregnancy) -> Dating:
    return Dating(
        method=pregnancy.edd_method,
        lmp=pregnancy.lmp,
        cycle_days=pregnancy.cycle_days,
        us_date=pregnancy.us_date,
        us_weeks=pregnancy.us_weeks,
        us_days=pregnancy.us_days,
        transfer_date=pregnancy.transfer_date,
        embryo_age_days=pregnancy.embryo_age_days,
    )


# --- the pregnancy ---------------------------------------------------------------------------------------------


def profile_for(patient) -> ObstetricProfile:
    profile, _created = ObstetricProfile.objects.get_or_create(
        patient=patient, defaults={"organization": patient.organization}
    )
    return profile


def active_pregnancy(patient) -> Pregnancy | None:
    return Pregnancy.objects.filter(patient=patient, status=PregnancyStatus.ACTIVE).first()


@transaction.atomic
def start_pregnancy(patient, dating: Dating, *, by, doctor=None, fetuses=1, on=None) -> Pregnancy:
    """Start following a pregnancy. One at a time per woman (also a database constraint)."""
    on = on or today()
    if not applies_to(patient):
        raise ObstetricError(_("متابعة الحمل للمريضات بس."))
    if active_pregnancy(patient):
        raise ObstetricError(_("فيه حمل متابَع بالفعل للمريضة دي."))
    pregnancy = Pregnancy(
        organization=patient.organization, patient=patient, doctor=doctor, fetuses=fetuses, created_by=by
    )
    try:
        _apply_dating(pregnancy, dating, on)
    except ValidationError as e:
        raise ObstetricError(" ".join(e.messages)) from e
    if dating.method == EddMethod.LMP:  # her usual cycle goes on the profile too, if it wasn't known
        profile = profile_for(patient)
        if not profile.cycle_days and dating.cycle_days != calc.STANDARD_CYCLE:
            profile.cycle_days = dating.cycle_days
            profile.save(update_fields=["cycle_days", "updated_at"])
    try:
        with transaction.atomic():
            pregnancy.save()
    except IntegrityError as e:  # two tabs at once
        raise ObstetricError(_("فيه حمل متابَع بالفعل للمريضة دي.")) from e
    return pregnancy


@transaction.atomic
def set_dating(pregnancy, dating: Dating, *, on=None) -> Pregnancy:
    """Change how the pregnancy is dated (e.g. the first scan disagrees with the period): the EDD moves, and with it
    the gestational age on every screen."""
    pregnancy = Pregnancy.objects.select_for_update().get(pk=pregnancy.pk)
    if not pregnancy.is_active:
        raise ObstetricError(_("الحمل ده اتقفل، التواريخ مش بتتعدل."))
    try:
        _apply_dating(pregnancy, dating, on or today())
    except ValidationError as e:
        raise ObstetricError(" ".join(e.messages)) from e
    pregnancy.save()
    return pregnancy


@transaction.atomic
def update_details(pregnancy, *, fetuses, risk_level, risk_notes, notes) -> Pregnancy:
    pregnancy = Pregnancy.objects.select_for_update().get(pk=pregnancy.pk)
    if not 1 <= fetuses <= 5:
        raise ObstetricError(_("عدد الأجنة مش منطقي."))
    if risk_level not in RiskLevel.values:
        raise ObstetricError(_("مستوى الخطورة مش معروف."))
    pregnancy.fetuses, pregnancy.risk_level = fetuses, risk_level
    pregnancy.risk_notes, pregnancy.notes = risk_notes.strip()[:300], notes.strip()
    pregnancy.save()
    return pregnancy


@transaction.atomic
def close_pregnancy(
    pregnancy, *, outcome, ended_on, by, living_children=None, baby_sex="", baby_weight_g=None, place="",
    complications="",
) -> PregnancyHistory:  # fmt: skip
    """End the follow-up with how it ended. Writes the obstetric-history row, so G/P/A/L move by themselves."""
    pregnancy = Pregnancy.objects.select_for_update().get(pk=pregnancy.pk)
    if not pregnancy.is_active:
        raise ObstetricError(_("الحمل ده اتقفل قبل كده."))
    if outcome not in Outcome.values:
        raise ObstetricError(_("اختار الحمل انتهى بإيه."))
    if not ended_on or ended_on > today():
        raise ObstetricError(_("تاريخ الانتهاء مينفعش يكون في المستقبل."))
    if calc.ga_days(pregnancy.edd, ended_on) < 0:
        raise ObstetricError(_("تاريخ الانتهاء قبل بداية الحمل."))
    if living_children is None:
        living_children = pregnancy.fetuses if outcome in LIVE_BIRTHS else 0
    if outcome not in LIVE_BIRTHS:
        living_children = 0
    if not 0 <= living_children <= 6:
        raise ObstetricError(_("عدد الأطفال مش منطقي."))
    pregnancy.status = PregnancyStatus.DELIVERED if outcome in BIRTHS else PregnancyStatus.LOST
    pregnancy.ended_on = ended_on
    pregnancy.save()
    if outcome in LIVE_BIRTHS:  # nobody is congratulated after a loss
        transaction.on_commit(lambda: notify_postpartum(pregnancy, by=by))
    return PregnancyHistory.objects.create(
        organization=pregnancy.organization,
        patient=pregnancy.patient,
        pregnancy=pregnancy,
        year=ended_on.year,
        outcome=outcome,
        weeks=min(calc.ga_days(pregnancy.edd, ended_on) // 7, 44),
        living_children=living_children,
        baby_sex=baby_sex if outcome in BIRTHS else "",
        baby_weight_g=baby_weight_g if outcome in BIRTHS else None,
        place=place.strip()[:100],
        complications=complications.strip()[:300],
        recorded_by=by,
    )


@transaction.atomic
def transfer_pregnancy(pregnancy, *, ended_on=None) -> Pregnancy:
    """She is followed elsewhere now (or stopped coming): off the active list, with no outcome recorded."""
    pregnancy = Pregnancy.objects.select_for_update().get(pk=pregnancy.pk)
    if not pregnancy.is_active:
        raise ObstetricError(_("الحمل ده اتقفل قبل كده."))
    pregnancy.status, pregnancy.ended_on = PregnancyStatus.TRANSFERRED, ended_on or today()
    pregnancy.save()
    return pregnancy


@transaction.atomic
def reopen_pregnancy(pregnancy) -> Pregnancy:
    """Closed by mistake: follow it again. The history row it wrote is taken out of the count (never deleted)."""
    pregnancy = Pregnancy.objects.select_for_update().get(pk=pregnancy.pk)
    if pregnancy.is_active:
        return pregnancy
    if active_pregnancy(pregnancy.patient):
        raise ObstetricError(_("فيه حمل تاني متابَع دلوقتي."))
    row = PregnancyHistory.objects.filter(pregnancy=pregnancy).first()
    if row is not None:
        row.is_removed, row.pregnancy = True, None
        row.save()
    pregnancy.status, pregnancy.ended_on = PregnancyStatus.ACTIVE, None
    pregnancy.save()
    return pregnancy


# --- obstetric history and the counts --------------------------------------------------------------------------


def history_rows(patient):
    return PregnancyHistory.objects.filter(patient=patient, is_removed=False)


def add_history(patient, *, outcome, by, year=None, weeks=None, living_children=None, baby_sex="", baby_weight_g=None,
                place="", complications="") -> PregnancyHistory:  # fmt: skip
    if outcome not in Outcome.values:
        raise ObstetricError(_("اختار الحمل انتهى بإيه."))
    if year is not None and not 1950 <= year <= today().year:
        raise ObstetricError(_("السنة مش منطقية."))
    if weeks is not None and not 0 <= weeks <= 44:
        raise ObstetricError(_("الأسبوع مش منطقي."))
    if living_children is None:
        living_children = 1 if outcome in LIVE_BIRTHS else 0
    if outcome not in LIVE_BIRTHS:
        living_children = 0
    return PregnancyHistory.objects.create(
        organization=patient.organization,
        patient=patient,
        outcome=outcome,
        year=year,
        weeks=weeks,
        living_children=min(living_children, 6),
        baby_sex=baby_sex if outcome in BIRTHS else "",
        baby_weight_g=baby_weight_g if outcome in BIRTHS else None,
        place=place.strip()[:100],
        complications=complications.strip()[:300],
        recorded_by=by,
    )


def remove_history(row) -> None:
    if row.pregnancy_id:
        raise ObstetricError(_("السطر ده جاي من حمل اتابع هنا. لو اتقفل غلط، افتح الحمل تاني."))
    row.is_removed = True
    row.save(update_fields=["is_removed", "updated_at"])


@dataclass(frozen=True)
class Gpal:
    """Gravida / Para / Abortus / Living, counted from the history rows and the current pregnancy."""

    gravida: int
    para: int
    abortus: int
    living: int
    cesareans: int

    @property
    def label(self) -> str:
        return f"G{self.gravida} P{self.para} A{self.abortus} L{self.living}"


def gpal(patient, rows=None, pregnant=None) -> Gpal:
    rows = list(history_rows(patient)) if rows is None else rows
    if pregnant is None:
        pregnant = active_pregnancy(patient) is not None
    return Gpal(
        gravida=len(rows) + (1 if pregnant else 0),
        para=sum(1 for r in rows if r.outcome in BIRTHS),
        abortus=sum(1 for r in rows if r.outcome not in BIRTHS),
        living=sum(r.living_children for r in rows),
        cesareans=sum(1 for r in rows if r.outcome == Outcome.CESAREAN),
    )


def risk_flags(patient, pregnancy, profile, counts: Gpal, on: date) -> list[str]:
    """Short reasons this pregnancy needs extra care, from what is recorded (never inferred beyond it)."""
    flags = []
    if pregnancy.risk_level == RiskLevel.HIGH:
        flags.append(pregnancy.risk_notes or _("حمل عالي الخطورة"))
    if pregnancy.fetuses == 2:
        flags.append(_("توأم"))
    elif pregnancy.fetuses > 2:
        flags.append(_("%(n)s أجنة") % {"n": pregnancy.fetuses})
    if counts.cesareans == 1:
        flags.append(_("قيصرية سابقة"))
    elif counts.cesareans > 1:
        flags.append(_("%(n)s قيصريات سابقة") % {"n": counts.cesareans})
    if profile is not None and profile.rh_negative:
        flags.append(_("Rh سالب"))
    if patient.date_of_birth and not patient.dob_is_estimated:
        dob = patient.date_of_birth
        age = on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
        if age >= 35:
            flags.append(_("السن %(n)s") % {"n": age})
        elif age < 18:
            flags.append(_("السن %(n)s") % {"n": age})
    return flags


# --- what the screens show ---------------------------------------------------------------------------------------


def badges_for(org, patient_ids, on=None) -> dict:
    """{patient_id: GestationalAge} for the women in `patient_ids` with a followed pregnancy: one query, for lists
    (reception board, patients list). Empty when the layer is off."""
    if not patient_ids or not enabled(org):
        return {}
    on = on or today()
    rows = Pregnancy.objects.for_org(org).filter(patient_id__in=set(patient_ids), status=PregnancyStatus.ACTIVE)
    return {p.patient_id: p.ga(on) for p in rows}


def badge_for(patient, on=None):
    """The gestational age of one patient's followed pregnancy, or None."""
    if not applies_to(patient) or not enabled(patient.organization):
        return None
    pregnancy = active_pregnancy(patient)
    return pregnancy.ga(on or today()) if pregnancy else None


@dataclass
class MilestoneRow:
    milestone: Milestone
    state: str  # done / due / overdue / upcoming
    check: MilestoneCheck | None
    from_date: date
    to_date: date


def milestones_for(pregnancy, profile=None, on=None) -> list[MilestoneRow]:
    """The clinic's schedule laid over this pregnancy: what is done, due now, overdue or still ahead."""
    on = on or today()
    rh_negative = bool(profile and profile.rh_negative)
    age = pregnancy.ga(on).days
    checks = {c.milestone_id: c for c in pregnancy.milestone_checks.all()}
    ensure_milestones(pregnancy.organization)  # a clinic that just switched the layer on gets the usual schedule
    start = pregnancy.edd - timedelta(days=calc.TERM_DAYS)
    out = []
    for m in Milestone.objects.for_org(pregnancy.organization).filter(is_active=True):
        check = checks.get(m.pk)
        if m.rh_negative_only and not rh_negative and check is None:
            continue
        first, last = m.from_week * 7, m.to_week * 7 + 6
        if check is not None:
            state = "done"
        elif age < first:
            state = "upcoming"
        elif age <= last:
            state = "due"
        else:
            state = "overdue"
        out.append(MilestoneRow(m, state, check, start + timedelta(days=first), start + timedelta(days=last)))
    return out


def toggle_milestone(pregnancy, milestone, *, by, on=None) -> None:
    check = MilestoneCheck.objects.filter(pregnancy=pregnancy, milestone=milestone).first()
    if check is not None:
        check.delete()  # a tick taken back; simple-history keeps that it was there
        return
    MilestoneCheck.objects.create(
        organization=pregnancy.organization, pregnancy=pregnancy, milestone=milestone, done_on=on or today(),
        recorded_by=by,
    )  # fmt: skip


DEFAULT_MILESTONES = [
    # name, kind, from week, to week, Rh-negative only
    ("سونار تأكيد الحمل وتحديد العمر", MilestoneKind.SCAN, 6, 10, False),
    ("تحاليل أول الحمل (صورة دم، فصيلة وRh، سكر، بول، فيروسات)", MilestoneKind.LAB, 6, 12, False),
    ("سونار الشفافية القفوية (NT)", MilestoneKind.SCAN, 11, 13, False),
    ("سونار رباعي الأبعاد / فحص التشوهات", MilestoneKind.SCAN, 18, 22, False),
    ("منحنى السكر (GTT)", MilestoneKind.LAB, 24, 28, False),
    ("حقنة Anti-D", MilestoneKind.OTHER, 28, 30, True),
    ("صورة دم إعادة", MilestoneKind.LAB, 28, 32, False),
    ("سونار النمو ووضع الجنين", MilestoneKind.SCAN, 32, 36, False),
    ("مسحة GBS", MilestoneKind.LAB, 35, 37, False),
]


def ensure_milestones(org) -> int:
    """Give a clinic without a schedule the usual one (idempotent; never recreates what the clinic switched off)."""
    if Milestone.objects.for_org(org).exists():
        return 0
    Milestone.objects.bulk_create(
        Milestone(organization=org, name=name, kind=kind, from_week=a, to_week=b, rh_negative_only=rh, order=i)
        for i, (name, kind, a, b, rh) in enumerate(DEFAULT_MILESTONES)
    )
    return len(DEFAULT_MILESTONES)


# --- the antenatal sheet of a visit ---------------------------------------------------------------------------

ANTENATAL_NUMBERS = {
    # field: (type, min, max)
    "fundal_height_cm": (Decimal, 5, 60),
    "fetal_heart_rate": (int, 50, 240),
    "hemoglobin": (Decimal, 3, 20),
    "efw_g": (int, 0, 7000),
    "afi_cm": (Decimal, 0, 50),
    "bpd_mm": (int, 5, 120),
    "hc_mm": (int, 20, 420),
    "ac_mm": (int, 20, 450),
    "fl_mm": (int, 3, 95),
}
ANTENATAL_CHOICES = ("fetal_movement", "presentation", "edema", "urine_albumin", "urine_sugar")
ANTENATAL_TEXT = {"placenta": 100, "ultrasound_notes": None}
ANTENATAL_FIELDS = (*ANTENATAL_NUMBERS, *ANTENATAL_CHOICES, *ANTENATAL_TEXT)


def antenatal_for(visit, create=False) -> AntenatalVisit | None:
    """The antenatal sheet of a visit. Created on the first value written, for a woman with a followed pregnancy."""
    sheet = AntenatalVisit.objects.filter(visit=visit).select_related("pregnancy").first()
    if sheet is not None or not create:
        return sheet
    pregnancy = active_pregnancy(visit.patient)
    if pregnancy is None:
        return None
    day = timezone.localtime(visit.started_at or visit.created_at, CAIRO).date()
    return AntenatalVisit.objects.create(
        organization=visit.organization, visit=visit, pregnancy=pregnancy, ga_days=max(pregnancy.ga(day).days, 0)
    )


def save_antenatal_field(visit, name, raw) -> AntenatalVisit:
    """Autosave of one antenatal field (same idea as clinical.services.save_field: a whitelist, validated)."""
    if name not in ANTENATAL_FIELDS:
        raise ValidationError(_("حقل مش معروف."))
    sheet = antenatal_for(visit, create=True)
    if sheet is None:
        raise ValidationError(_("مفيش حمل متابَع للمريضة دي."))
    raw = (raw or "").strip()
    if name in ANTENATAL_NUMBERS:
        kind, low, high = ANTENATAL_NUMBERS[name]
        if raw == "":
            value = None
        else:
            try:
                value = kind(raw)
            except (ValueError, InvalidOperation) as e:
                raise ValidationError(_("اكتب رقم.")) from e
            if not low <= value <= high:
                raise ValidationError(_("الرقم بره المدى المعقول (%(a)s - %(b)s).") % {"a": low, "b": high})
    elif name in ANTENATAL_CHOICES:
        allowed = {c for c, _label in AntenatalVisit._meta.get_field(name).choices}
        if raw and raw not in allowed:
            raise ValidationError(_("اختيار مش معروف."))
        value = raw
    else:
        limit = ANTENATAL_TEXT[name]
        value = raw[:limit] if limit else raw
    setattr(sheet, name, value)
    sheet.save(update_fields=[name, "updated_at"])
    return sheet


# --- messages about the pregnancy (all through the outbox, apps/notifications) ---------------------------------

POSTPARTUM_AFTER_DAYS = 33  # a week before the six-week check
MILESTONE_HOUR = 11


def _at(day, hour=MILESTONE_HOUR):
    from datetime import datetime, time

    return datetime.combine(day, time(hour, 0), tzinfo=CAIRO)


def notify_postpartum(pregnancy, by=None) -> list:
    """After a birth: one message a week before the six-week check. Idempotent per pregnancy."""
    from apps.notifications import services as outbox
    from apps.notifications.models import Event

    if pregnancy.status != PregnancyStatus.DELIVERED or not pregnancy.ended_on:
        return []
    return outbox.enqueue_for_patient(
        org=pregnancy.organization, patient=pregnancy.patient, event=Event.POSTPARTUM,
        send_at=_at(pregnancy.ended_on + timedelta(days=POSTPARTUM_AFTER_DAYS)), context={},
        key=f"pp:{pregnancy.pk}", by=by,
    )  # fmt: skip


def notify_milestones(now=None) -> int:
    """The scheduler's job: every followed pregnancy inside a milestone's window that has not had it gets one
    message about it. Idempotent per pregnancy and milestone, so running it again sends nothing twice. Returns how
    many pregnancy / milestone pairs are due."""
    from apps.notifications import services as outbox
    from apps.notifications.models import Event

    on = timezone.localtime(now or timezone.now(), CAIRO).date()
    queued = 0
    following = Pregnancy.objects.filter(status=PregnancyStatus.ACTIVE).select_related("patient", "organization")
    for pregnancy in following.prefetch_related("milestone_checks"):
        if not enabled(pregnancy.organization):
            continue
        age = pregnancy.ga(on)
        profile = ObstetricProfile.objects.filter(patient=pregnancy.patient).first()
        for row in milestones_for(pregnancy, profile, on=on):
            if row.state != "due":
                continue
            rows = outbox.enqueue_for_patient(
                org=pregnancy.organization, patient=pregnancy.patient, event=Event.MILESTONE_DUE,
                send_at=_at(on), key=f"ms:{pregnancy.pk}:{row.milestone.pk}",
                context={"milestone_name": row.milestone.name, "pregnancy_week": age.weeks,
                         "pregnancy_month": age.month},
            )  # fmt: skip
            queued += 1 if rows else 0
    return queued
