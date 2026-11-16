"""Operating room and admissions (docs/plan/14-obgyn-center.md §14.4): rooms, the procedures the center does,
operations with their team, pre-op checklist, signed note and newborns, and inpatient stays.

Medical and financial records: nothing here is hard-deleted. An operation is cancelled or postponed, a signed note
is revised, an admission is discharged.
"""

from django.conf import settings as dj_settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TenantScopedModel
from apps.core.timeutils import CAIRO
from apps.obstetrics.models import BabySex

# --- rooms and procedures (set up once in Settings) ------------------------------------------------------------


class RoomKind(models.TextChoices):
    OPERATING = "operating", _("غرفة عمليات")
    DELIVERY = "delivery", _("غرفة ولادة")
    RECOVERY = "recovery", _("إفاقة")
    INPATIENT = "inpatient", _("غرفة إقامة")


THEATRE_KINDS = (RoomKind.OPERATING, RoomKind.DELIVERY)


class Room(TenantScopedModel):
    name = models.CharField(_("الاسم"), max_length=80)
    kind = models.CharField(_("النوع"), max_length=10, choices=RoomKind.choices, default=RoomKind.OPERATING)
    beds = models.PositiveSmallIntegerField(
        _("عدد السراير"), default=1, validators=[MinValueValidator(1), MaxValueValidator(20)]
    )
    nightly_rate = models.DecimalField(
        _("سعر الليلة"), max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(_("مفعّلة"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("غرفة")
        verbose_name_plural = _("الغرف")
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_room_name_per_org")]

    def __str__(self):
        return self.name

    @property
    def is_theatre(self) -> bool:
        return self.kind in THEATRE_KINDS


class DeliveryMode(models.TextChoices):
    VAGINAL = "vaginal", _("ولادة طبيعية")
    CESAREAN = "cesarean", _("ولادة قيصرية")


class TeamRole(models.TextChoices):
    SURGEON = "surgeon", _("الجراح")
    ASSISTANT = "assistant", _("مساعد")
    ANAESTHETIST = "anaesthetist", _("تخدير")
    PAEDIATRICIAN = "paediatrician", _("أطفال")
    NURSE = "nurse", _("تمريض")


class ProcedureType(TenantScopedModel):
    """A procedure the center does (ولادة طبيعية، قيصرية، كحت، ربط عنق الرحم، منظار...): how long it usually
    takes, its package price, the team it usually needs with their fees, and its pre-op checklist. An operation copies
    all of these when it is booked, so editing a procedure never rewrites a booked operation."""

    name_ar = models.CharField(_("الاسم"), max_length=120)
    name_en = models.CharField(_("الاسم بالإنجليزي"), max_length=120, blank=True)
    # set for the two procedures that end a pregnancy with a birth: closing one closes the pregnancy file
    delivery = models.CharField(_("ولادة"), max_length=10, choices=DeliveryMode.choices, blank=True)
    duration_minutes = models.PositiveSmallIntegerField(
        _("المدة المعتادة (دقيقة)"), default=60, validators=[MinValueValidator(10), MaxValueValidator(720)]
    )
    package_price = models.DecimalField(
        _("سعر الباكدج"), max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    fasting_hours = models.PositiveSmallIntegerField(
        _("ساعات الصيام قبلها"), default=8, validators=[MaxValueValidator(24)]
    )
    default_team = models.JSONField(_("الفريق المعتاد"), default=list, blank=True)  # [{"role": "surgeon", "fee": "0"}]
    checklist = models.JSONField(
        _("قايمة ما قبل العملية"), default=list, blank=True
    )  # [{"label": ..., "required": true}]
    # What the procedure usually takes from the store: [{"item": <StockItem pk>, "quantity": "2"}]. Issued to an
    # operation in one step (theatre.services.issue_kit) and then corrected to what was really used.
    kit = models.JSONField(_("المستلزمات المعتادة"), default=list, blank=True)
    # the ledger account its revenue goes to (system key); blank = the operations revenue account (phase 16)
    revenue_account_key = models.CharField(max_length=40, blank=True, editable=False)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(_("مفعّل"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("نوع عملية")
        verbose_name_plural = _("أنواع العمليات")
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["organization", "name_ar"], name="uniq_procedure_name_per_org")]

    def __str__(self):
        return self.name_ar


# --- operations ------------------------------------------------------------------------------------------------


class RoomDay(TenantScopedModel):
    """One row per room and day. Every booking, move or cancellation of an operation in that room on that day happens
    under `select_for_update()` on it: that is what stops two operations from overlapping in one room (the same
    pattern as scheduling.DayLedger)."""

    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="days")
    date = models.DateField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["room", "date"], name="uniq_room_day")]


class OperationStatus(models.TextChoices):
    SCHEDULED = "scheduled", _("محجوزة")
    ADMITTED = "admitted", _("المريضة دخلت")
    IN_THEATRE = "in_theatre", _("في العمليات")
    RECOVERY = "recovery", _("إفاقة")
    DONE = "done", _("خلصت")
    CANCELLED = "cancelled", _("اتلغت")
    POSTPONED = "postponed", _("اتأجلت")


# the operation still needs its room at its planned time
HOLDS_ROOM = (OperationStatus.SCHEDULED, OperationStatus.ADMITTED, OperationStatus.IN_THEATRE)
OPEN_STATUSES = (*HOLDS_ROOM, OperationStatus.RECOVERY)
ALLOWED_TRANSITIONS = {
    OperationStatus.SCHEDULED: {OperationStatus.ADMITTED, OperationStatus.CANCELLED, OperationStatus.POSTPONED},
    OperationStatus.ADMITTED: {OperationStatus.IN_THEATRE, OperationStatus.SCHEDULED, OperationStatus.CANCELLED},
    OperationStatus.IN_THEATRE: {OperationStatus.RECOVERY, OperationStatus.ADMITTED},
    OperationStatus.RECOVERY: {OperationStatus.DONE, OperationStatus.IN_THEATRE},
    OperationStatus.DONE: set(),
    OperationStatus.CANCELLED: set(),
    OperationStatus.POSTPONED: set(),
}


class Operation(TenantScopedModel):
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="operations")
    pregnancy = models.ForeignKey(
        "obstetrics.Pregnancy", null=True, blank=True, on_delete=models.PROTECT, related_name="operations"
    )
    procedure = models.ForeignKey(ProcedureType, on_delete=models.PROTECT, related_name="operations")
    procedure_name = models.CharField(max_length=120)  # as it was when booked
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="operations")
    surgeon = models.ForeignKey("doctors.Doctor", on_delete=models.PROTECT, related_name="operations")
    date = models.DateField()  # the Cairo day of planned_start
    planned_start = models.DateTimeField(_("الميعاد"))
    planned_end = models.DateTimeField()
    is_emergency = models.BooleanField(_("طوارئ"), default=False)
    status = models.CharField(max_length=10, choices=OperationStatus.choices, default=OperationStatus.SCHEDULED)
    price = models.DecimalField(_("السعر"), max_digits=10, decimal_places=2, default=0)
    fasting_hours = models.PositiveSmallIntegerField(default=8)
    notes = models.CharField(_("ملاحظات"), max_length=300, blank=True)
    emergency_case = models.ForeignKey(
        "center.EmergencyCase", null=True, blank=True, on_delete=models.PROTECT, related_name="operations"
    )
    postponed_to = models.OneToOneField("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    admitted_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)
    delivery_closed = models.BooleanField(default=False)  # the birth was written to the pregnancy file
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("عملية")
        verbose_name_plural = _("العمليات")
        ordering = ["planned_start", "id"]
        indexes = [
            models.Index(fields=["organization", "date"]),
            models.Index(fields=["organization", "patient", "-planned_start"]),
            models.Index(fields=["room", "date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(planned_end__gt=models.F("planned_start")), name="operation_ends_after_start"
            )
        ]

    def __str__(self):
        return f"{self.procedure_name}: {self.patient}"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_delivery(self) -> bool:
        return bool(self.procedure.delivery)


class OperationEvent(TenantScopedModel):
    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="events")
    from_status = models.CharField(max_length=10, blank=True)
    to_status = models.CharField(max_length=10)
    by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.CharField(max_length=300, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]


class OperationTeamMember(TenantScopedModel):
    """Who is in the room and what they are paid for this operation. A doctor of the center, or an outside name."""

    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="team")
    role = models.CharField(_("الدور"), max_length=14, choices=TeamRole.choices)
    doctor = models.ForeignKey("doctors.Doctor", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    name = models.CharField(_("الاسم"), max_length=120, blank=True)
    fee = models.DecimalField(
        _("الأتعاب"), max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("عضو فريق العملية")
        verbose_name_plural = _("فريق العملية")
        ordering = ["id"]

    def __str__(self):
        return f"{self.get_role_display()}: {self.display_name}"

    @property
    def display_name(self) -> str:
        return str(self.doctor) if self.doctor_id else self.name


class ChecklistItem(TenantScopedModel):
    """One line of an operation's pre-op checklist (copied from its procedure when booked)."""

    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="checklist")
    label = models.CharField(max_length=150)
    required = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)
    done = models.BooleanField(default=False)
    done_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.label


class NoteStatus(models.TextChoices):
    DRAFT = "draft", _("مسودة")
    SIGNED = "signed", _("موقّع")


class OperationNote(TenantScopedModel):
    """The surgeon's report. A draft is edited freely; once signed it never changes: a correction is a new revision
    (the same rule as a final prescription)."""

    operation = models.ForeignKey(Operation, on_delete=models.PROTECT, related_name="op_notes")
    revision = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=10, choices=NoteStatus.choices, default=NoteStatus.DRAFT)
    anaesthesia = models.CharField(_("التخدير"), max_length=120, blank=True)
    findings = models.TextField(_("المشاهدات"), blank=True)
    procedure_details = models.TextField(_("اللي اتعمل"), blank=True)
    blood_loss_ml = models.PositiveIntegerField(_("الدم المفقود (مل)"), null=True, blank=True)
    complications = models.TextField(_("المضاعفات"), blank=True)
    post_op_orders = models.TextField(_("تعليمات بعد العملية"), blank=True)
    signed_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    signed_at = models.DateTimeField(null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("تقرير عملية")
        verbose_name_plural = _("تقارير العمليات")
        ordering = ["operation_id", "revision"]
        constraints = [models.UniqueConstraint(fields=["operation", "revision"], name="uniq_op_note_revision")]

    def __str__(self):
        return f"{self.operation} R{self.revision}"

    @property
    def is_signed(self) -> bool:
        return self.status == NoteStatus.SIGNED


class NewbornStatus(models.TextChoices):
    WELL = "well", _("بصحة جيدة")
    NICU = "nicu", _("حضّانة")
    STILLBORN = "stillborn", _("وُلد ميتًا")


class Newborn(TenantScopedModel):
    operation = models.ForeignKey(Operation, on_delete=models.PROTECT, related_name="newborns")
    order = models.PositiveSmallIntegerField(default=1)
    sex = models.CharField(_("النوع"), max_length=6, choices=BabySex.choices)
    weight_g = models.PositiveIntegerField(
        _("الوزن (جرام)"), null=True, blank=True, validators=[MinValueValidator(200), MaxValueValidator(7000)]
    )
    apgar_1 = models.PositiveSmallIntegerField(
        _("أبجار 1 دقيقة"), null=True, blank=True, validators=[MaxValueValidator(10)]
    )
    apgar_5 = models.PositiveSmallIntegerField(
        _("أبجار 5 دقايق"), null=True, blank=True, validators=[MaxValueValidator(10)]
    )
    born_at = models.DateTimeField(_("وقت الولادة"), default=timezone.now)
    status = models.CharField(_("الحالة"), max_length=10, choices=NewbornStatus.choices, default=NewbornStatus.WELL)
    notes = models.CharField(_("ملاحظات"), max_length=300, blank=True)
    is_removed = models.BooleanField(default=False)  # recorded by mistake: kept, out of every count

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("مولود")
        verbose_name_plural = _("المواليد")
        ordering = ["operation_id", "order", "id"]

    def __str__(self):
        return f"{self.get_sex_display()} {self.weight_g or ''}".strip()

    @property
    def is_alive(self) -> bool:
        return self.status != NewbornStatus.STILLBORN


class OperationConsumable(TenantScopedModel):
    """Something used in the case (sutures, a drug, a kit). A store item is issued from the store when it is added
    (its cost becomes an expense of the theatre) and goes back on the shelf if the line is taken off; an item with a
    sale price is also put on the patient's account. Free text is still allowed for what is not in the store."""

    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="consumables")
    item = models.ForeignKey(
        "inventory.StockItem", null=True, blank=True, on_delete=models.PROTECT, related_name="operation_uses"
    )
    stock_move = models.OneToOneField(
        "inventory.StockMove", null=True, blank=True, on_delete=models.PROTECT, related_name="operation_use"
    )
    charge = models.OneToOneField(
        "invoicing.Charge", null=True, blank=True, on_delete=models.PROTECT, related_name="operation_use"
    )
    is_removed = models.BooleanField(default=False)  # taken off again: back in the store, kept for the record
    name = models.CharField(_("الصنف"), max_length=120)
    quantity = models.DecimalField(
        _("الكمية"), max_digits=8, decimal_places=2, default=1, validators=[MinValueValidator(0.01)]
    )
    unit = models.CharField(_("الوحدة"), max_length=20, blank=True)
    added_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("مستلزم عملية")
        verbose_name_plural = _("مستلزمات العمليات")
        ordering = ["id"]

    def __str__(self):
        return self.name


# --- admissions ------------------------------------------------------------------------------------------------


class AdmissionStatus(models.TextChoices):
    ADMITTED = "admitted", _("مقيمة")
    DISCHARGED = "discharged", _("خرجت")
    CANCELLED = "cancelled", _("اتلغى")


class Admission(TenantScopedModel):
    """An inpatient stay in a room, charged per night. One open stay per patient (a partial unique constraint)."""

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="admissions")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="admissions")
    operation = models.ForeignKey(Operation, null=True, blank=True, on_delete=models.PROTECT, related_name="admissions")
    emergency_case = models.ForeignKey(
        "center.EmergencyCase", null=True, blank=True, on_delete=models.PROTECT, related_name="admissions"
    )
    doctor = models.ForeignKey(
        "doctors.Doctor", null=True, blank=True, on_delete=models.PROTECT, related_name="admissions"
    )
    status = models.CharField(max_length=10, choices=AdmissionStatus.choices, default=AdmissionStatus.ADMITTED)
    admitted_at = models.DateTimeField(_("وقت الدخول"), default=timezone.now)
    discharged_at = models.DateTimeField(_("وقت الخروج"), null=True, blank=True)
    nightly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # the room's rate when she came in
    reason = models.CharField(_("سبب الدخول"), max_length=200, blank=True)
    discharge_summary = models.TextField(_("ملخص الخروج"), blank=True)
    discharge_instructions = models.TextField(_("تعليمات الخروج"), blank=True)
    admitted_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    discharged_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("إقامة")
        verbose_name_plural = _("الإقامات")
        ordering = ["-admitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["patient"], condition=Q(status="admitted"), name="uniq_open_admission_per_patient"
            )
        ]
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self):
        return f"{self.patient} @ {self.room}"

    def nights(self, now=None) -> int:
        """Nights charged: the midnights between coming in and leaving (Cairo), at least one."""
        end = self.discharged_at or now or timezone.now()
        days = (timezone.localtime(end, CAIRO).date() - timezone.localtime(self.admitted_at, CAIRO).date()).days
        return max(days, 1)

    def room_charge(self, now=None):
        return self.nightly_rate * self.nights(now)
