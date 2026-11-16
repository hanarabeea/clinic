"""OB/GYN layer on the medical file (docs/plan/14-obgyn-center.md §14.2). Switched on per clinic with
`OrganizationSettings.specialty = "obgyn"`.

Medical records: nothing here is hard-deleted (a wrong obstetric-history row is marked `is_removed`), and every model
keeps its history.
"""

from django.conf import settings as dj_settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TenantScopedModel

from . import calc


class BloodGroup(models.TextChoices):
    A_POS = "A+", "A+"
    A_NEG = "A-", "A-"
    B_POS = "B+", "B+"
    B_NEG = "B-", "B-"
    AB_POS = "AB+", "AB+"
    AB_NEG = "AB-", "AB-"
    O_POS = "O+", "O+"
    O_NEG = "O-", "O-"


class ObstetricProfile(TenantScopedModel):
    """What stays true of the woman between pregnancies. G/P/A/L are *not* stored here: they are counted from her
    obstetric history and her current pregnancy (services.gpal), so they can never disagree with it."""

    patient = models.OneToOneField("patients.Patient", on_delete=models.PROTECT, related_name="obstetric_profile")
    blood_group = models.CharField(_("فصيلة الدم"), max_length=3, choices=BloodGroup.choices, blank=True)
    husband_blood_group = models.CharField(_("فصيلة دم الزوج"), max_length=3, choices=BloodGroup.choices, blank=True)
    cycle_days = models.PositiveSmallIntegerField(
        _("طول الدورة (يوم)"), null=True, blank=True, validators=[MinValueValidator(15), MaxValueValidator(60)]
    )
    cycle_regular = models.BooleanField(_("الدورة منتظمة"), null=True, blank=True)
    contraception = models.CharField(_("وسيلة منع الحمل"), max_length=100, blank=True)
    notes = models.TextField(_("ملاحظات نسا وتوليد"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("ملف نسا وتوليد")
        verbose_name_plural = _("ملفات النسا والتوليد")

    def __str__(self):
        return f"{self.patient} (OB)"

    @property
    def rh_negative(self) -> bool:
        return self.blood_group.endswith("-")


class Outcome(models.TextChoices):
    VAGINAL = "vaginal", _("ولادة طبيعية")
    CESAREAN = "cesarean", _("قيصرية")
    STILLBIRTH = "stillbirth", _("ولادة ميتة")
    MISCARRIAGE = "miscarriage", _("إجهاض")
    ECTOPIC = "ectopic", _("حمل خارج الرحم")
    MOLAR = "molar", _("حمل عنقودي")


# a birth (counts in "P"); the rest ended early (count in "A")
BIRTHS = (Outcome.VAGINAL, Outcome.CESAREAN, Outcome.STILLBIRTH)
LIVE_BIRTHS = (Outcome.VAGINAL, Outcome.CESAREAN)


class BabySex(models.TextChoices):
    MALE = "male", _("ولد")
    FEMALE = "female", _("بنت")


class PregnancyStatus(models.TextChoices):
    ACTIVE = "active", _("متابَع")
    DELIVERED = "delivered", _("اتولد")
    LOST = "lost", _("انتهى من غير ولادة")
    TRANSFERRED = "transferred", _("المتابعة اتنقلت")


class EddMethod(models.TextChoices):
    LMP = "lmp", _("آخر دورة")
    ULTRASOUND = "ultrasound", _("السونار")
    IVF = "ivf", _("حقن مجهري / نقل أجنة")


class RiskLevel(models.TextChoices):
    LOW = "low", _("عادي")
    HIGH = "high", _("حمل عالي الخطورة")


class Pregnancy(TenantScopedModel):
    """One pregnancy being followed. `edd` is the single stored anchor: the dating fields say how it was found
    (services.set_dating computes it), and the gestational age is always worked out from it for the day asked
    (`ga()`), never stored."""

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="pregnancies")
    doctor = models.ForeignKey(
        "doctors.Doctor", null=True, blank=True, on_delete=models.SET_NULL, related_name="pregnancies"
    )
    status = models.CharField(max_length=12, choices=PregnancyStatus.choices, default=PregnancyStatus.ACTIVE)

    edd_method = models.CharField(_("طريقة الحساب"), max_length=10, choices=EddMethod.choices, default=EddMethod.LMP)
    edd = models.DateField(_("ميعاد الولادة المتوقع"))
    lmp = models.DateField(_("أول يوم في آخر دورة"), null=True, blank=True)
    cycle_days = models.PositiveSmallIntegerField(
        _("طول الدورة (يوم)"), default=calc.STANDARD_CYCLE, validators=[MinValueValidator(15), MaxValueValidator(60)]
    )
    us_date = models.DateField(_("تاريخ السونار"), null=True, blank=True)
    us_weeks = models.PositiveSmallIntegerField(
        _("عمر الحمل في السونار (أسابيع)"), null=True, blank=True, validators=[MaxValueValidator(44)]
    )
    us_days = models.PositiveSmallIntegerField(_("وأيام"), default=0, validators=[MaxValueValidator(6)])
    transfer_date = models.DateField(_("تاريخ نقل الأجنة"), null=True, blank=True)
    embryo_age_days = models.PositiveSmallIntegerField(
        _("عمر الجنين عند النقل (يوم)"), default=5, validators=[MinValueValidator(2), MaxValueValidator(6)]
    )

    fetuses = models.PositiveSmallIntegerField(
        _("عدد الأجنة"), default=1, validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    risk_level = models.CharField(_("الخطورة"), max_length=5, choices=RiskLevel.choices, default=RiskLevel.LOW)
    risk_notes = models.CharField(_("سبب الخطورة"), max_length=300, blank=True)
    notes = models.TextField(_("ملاحظات"), blank=True)

    ended_on = models.DateField(_("تاريخ الانتهاء"), null=True, blank=True)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("حمل")
        verbose_name_plural = _("حالات الحمل")
        ordering = ["-created_at"]
        constraints = [
            # one followed pregnancy per woman, enforced by the database (two tabs, two doctors)
            models.UniqueConstraint(
                fields=["patient"], condition=Q(status="active"), name="uniq_active_pregnancy_per_patient"
            ),
        ]
        indexes = [models.Index(fields=["organization", "status", "edd"])]

    def __str__(self):
        return f"{self.patient} EDD {self.edd:%Y-%m-%d}"

    @property
    def is_active(self) -> bool:
        return self.status == PregnancyStatus.ACTIVE

    def ga(self, on) -> calc.GestationalAge:
        """Gestational age on a day; for a pregnancy that ended, never past the day it ended."""
        if self.ended_on and on > self.ended_on:
            on = self.ended_on
        return calc.gestational_age(self.edd, on)


class PregnancyHistory(TenantScopedModel):
    """A previous pregnancy and how it ended. Rows the doctor types for the past, plus the row written
    automatically when a followed pregnancy is closed (`pregnancy`)."""

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="pregnancy_history")
    pregnancy = models.OneToOneField(
        Pregnancy, null=True, blank=True, on_delete=models.PROTECT, related_name="history_row"
    )
    year = models.PositiveSmallIntegerField(
        _("السنة"), null=True, blank=True, validators=[MinValueValidator(1950), MaxValueValidator(2100)]
    )
    outcome = models.CharField(_("انتهى بـ"), max_length=12, choices=Outcome.choices)
    weeks = models.PositiveSmallIntegerField(_("في الأسبوع"), null=True, blank=True, validators=[MaxValueValidator(44)])
    living_children = models.PositiveSmallIntegerField(
        _("أطفال عايشين منه"), default=0, validators=[MaxValueValidator(6)]
    )
    baby_sex = models.CharField(_("النوع"), max_length=6, choices=BabySex.choices, blank=True)
    baby_weight_g = models.PositiveIntegerField(
        _("وزن المولود (جرام)"), null=True, blank=True, validators=[MaxValueValidator(7000)]
    )
    place = models.CharField(_("المكان"), max_length=100, blank=True)
    complications = models.CharField(_("مضاعفات / ملاحظات"), max_length=300, blank=True)
    is_removed = models.BooleanField(default=False)  # entered by mistake: hidden and not counted, never deleted
    recorded_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("حمل سابق")
        verbose_name_plural = _("التاريخ الولادي")
        ordering = ["year", "id"]

    def __str__(self):
        return f"{self.patient}: {self.get_outcome_display()} {self.year or ''}"


class FetalMovement(models.TextChoices):
    GOOD = "good", _("كويسة")
    REDUCED = "reduced", _("قليلة")
    ABSENT = "absent", _("مفيش")


class Presentation(models.TextChoices):
    CEPHALIC = "cephalic", _("رأسي")
    BREECH = "breech", _("مقعدي")
    TRANSVERSE = "transverse", _("مستعرض")


class Dipstick(models.TextChoices):
    NEGATIVE = "neg", _("سلبي")
    TRACE = "trace", _("آثار")
    ONE = "1", "+"
    TWO = "2", "++"
    THREE = "3", "+++"


class Edema(models.TextChoices):
    NONE = "none", _("مفيش")
    ONE = "1", "+"
    TWO = "2", "++"
    THREE = "3", "+++"


class AntenatalVisit(TenantScopedModel):
    """The antenatal sheet of one visit (weight and blood pressure stay in clinical.Vitals). `ga_days` is the
    gestational age on the visit's day, kept as written so an old visit still reads right if the dating is changed
    later (the pregnancy itself always shows the current dating)."""

    visit = models.OneToOneField("clinical.Visit", on_delete=models.PROTECT, related_name="antenatal")
    pregnancy = models.ForeignKey(Pregnancy, on_delete=models.PROTECT, related_name="visits")
    ga_days = models.PositiveSmallIntegerField()
    fundal_height_cm = models.DecimalField(
        _("ارتفاع الرحم (سم)"), max_digits=4, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(60)],
    )  # fmt: skip
    fetal_heart_rate = models.PositiveSmallIntegerField(
        _("نبض الجنين"), null=True, blank=True, validators=[MinValueValidator(50), MaxValueValidator(240)]
    )
    fetal_movement = models.CharField(_("حركة الجنين"), max_length=8, choices=FetalMovement.choices, blank=True)
    presentation = models.CharField(_("وضع الجنين"), max_length=10, choices=Presentation.choices, blank=True)
    edema = models.CharField(_("تورم"), max_length=4, choices=Edema.choices, blank=True)
    urine_albumin = models.CharField(_("زلال البول"), max_length=5, choices=Dipstick.choices, blank=True)
    urine_sugar = models.CharField(_("سكر البول"), max_length=5, choices=Dipstick.choices, blank=True)
    hemoglobin = models.DecimalField(
        _("الهيموجلوبين"), max_digits=3, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(3), MaxValueValidator(20)],
    )  # fmt: skip
    efw_g = models.PositiveIntegerField(
        _("وزن الجنين التقديري (جرام)"), null=True, blank=True, validators=[MaxValueValidator(7000)]
    )
    afi_cm = models.DecimalField(
        _("السائل الأمنيوسي AFI (سم)"), max_digits=4, decimal_places=1, null=True, blank=True,
        validators=[MaxValueValidator(50)],
    )  # fmt: skip
    placenta = models.CharField(_("المشيمة"), max_length=100, blank=True)
    ultrasound_notes = models.TextField(_("ملاحظات السونار"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("متابعة حمل")
        verbose_name_plural = _("متابعات الحمل")

    def __str__(self):
        return f"ANC {self.visit_id} ({self.ga_days} d)"

    @property
    def ga_label(self) -> str:
        """Weeks+days on the visit's day, the way it is written in the notes ("24+3")."""
        return f"{self.ga_days // 7}+{self.ga_days % 7}"


class MilestoneKind(models.TextChoices):
    LAB = "lab", _("تحاليل")
    SCAN = "scan", _("سونار")
    OTHER = "other", _("أخرى")


class Milestone(TenantScopedModel):
    """Something every pregnancy should get in a window of weeks (the clinic's own list, seeded with the usual
    schedule). Data, not code: the clinic edits the weeks and the names."""

    name = models.CharField(_("الاسم"), max_length=120)
    kind = models.CharField(_("النوع"), max_length=5, choices=MilestoneKind.choices, default=MilestoneKind.OTHER)
    from_week = models.PositiveSmallIntegerField(_("من الأسبوع"), validators=[MaxValueValidator(42)])
    to_week = models.PositiveSmallIntegerField(_("لحد الأسبوع"), validators=[MaxValueValidator(42)])
    rh_negative_only = models.BooleanField(_("للـRh السالب بس"), default=False)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("محطة متابعة")
        verbose_name_plural = _("محطات متابعة الحمل")
        ordering = ["from_week", "order", "id"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_milestone_name")]

    def __str__(self):
        return f"{self.name} ({self.from_week}-{self.to_week})"


class MilestoneCheck(TenantScopedModel):
    """A milestone done (or deliberately skipped) for one pregnancy."""

    pregnancy = models.ForeignKey(Pregnancy, on_delete=models.CASCADE, related_name="milestone_checks")
    milestone = models.ForeignKey(Milestone, on_delete=models.PROTECT, related_name="checks")
    done_on = models.DateField()
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("محطة اتعملت")
        verbose_name_plural = _("محطات اتعملت")
        constraints = [models.UniqueConstraint(fields=["pregnancy", "milestone"], name="uniq_milestone_check")]

    def __str__(self):
        return f"{self.milestone} {self.done_on:%Y-%m-%d}"
