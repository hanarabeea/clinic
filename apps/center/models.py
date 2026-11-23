"""Center mode (docs/plan/14-obgyn-center.md §14.3): the services a center sells besides the doctor's visit, and
emergency arrivals.

Nothing here is hard-deleted: a service is switched off, an order is cancelled, an emergency case keeps its times.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings as dj_settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TenantScopedModel

CENT = Decimal("0.01")


class CenterSettings(TenantScopedModel):
    """One row per clinic (`for_org` creates it)."""

    emergency_fee = models.DecimalField(
        _("رسوم كشف الطوارئ"), max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    # the waiting-room screen (plan 15 §1.5)
    lobby_show_names = models.BooleanField(
        _("شاشة الانتظار تكتب الاسم"),
        default=True,
        help_text=_("الاسم الأول وأول حرف من اسم الأب بس. من غيرها بيظهر رقم الدور أو الميعاد بس."),
    )
    lobby_sound = models.BooleanField(_("شاشة الانتظار تعمل صوت لما حد يدخل"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("إعدادات المركز")
        verbose_name_plural = _("إعدادات المركز")
        constraints = [models.UniqueConstraint(fields=["organization"], name="uniq_center_settings_per_org")]

    def __str__(self):
        return f"{self.organization} center settings"

    @classmethod
    def for_org(cls, org):
        obj, _created = cls.objects.get_or_create(organization=org)
        return obj


# --- services catalog ------------------------------------------------------------------------------------------


class ServiceCategory(models.TextChoices):
    ULTRASOUND = "ultrasound", _("سونار وأشعة")
    PROCEDURE = "procedure", _("إجراءات")
    LAB = "lab", _("تحاليل وعينات")
    INJECTION = "injection", _("حقن ومحاليل")
    OTHER = "other", _("أخرى")


class Service(TenantScopedModel):
    """Something the center charges for on top of the visit: ultrasound, 4D, CTG, cautery, IUD insertion..."""

    name_ar = models.CharField(_("الاسم"), max_length=120)
    name_en = models.CharField(_("الاسم بالإنجليزي"), max_length=120, blank=True)
    category = models.CharField(
        _("النوع"), max_length=12, choices=ServiceCategory.choices, default=ServiceCategory.PROCEDURE
    )
    price = models.DecimalField(_("السعر"), max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    # What the doctor who does it earns from each one. Paid out through the doctor shares of phase 16; kept on every
    # order line at the time it is ordered, so changing it later never rewrites old orders.
    doctor_share_percent = models.DecimalField(
        _("نسبة الدكتور %"),
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    # The ledger account its revenue goes to (system key of an account). The accounting core (phase 14) reads it;
    # blank = the default services revenue account.
    revenue_account_key = models.CharField(max_length=40, blank=True, editable=False)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(_("مفعّلة"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("خدمة")
        verbose_name_plural = _("الخدمات")
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["organization", "name_ar"], name="uniq_service_name_per_org")]

    def __str__(self):
        return self.name_ar


class OrderStatus(models.TextChoices):
    ORDERED = "ordered", _("مطلوبة")
    CANCELLED = "cancelled", _("ملغية")


class ServiceOrder(TenantScopedModel):
    """One service added to a visit. It is billed with the visit: the appointment's due amount is the visit price
    plus its active orders (billing.services.balance). Name, price and share are copied from the catalog when it is
    ordered."""

    appointment = models.ForeignKey("scheduling.Appointment", on_delete=models.PROTECT, related_name="service_orders")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="orders")
    name = models.CharField(max_length=120)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(99)])
    doctor = models.ForeignKey("doctors.Doctor", on_delete=models.PROTECT, related_name="service_orders")
    doctor_share_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=OrderStatus.choices, default=OrderStatus.ORDERED)
    notes = models.CharField(_("ملاحظة"), max_length=200, blank=True)
    ordered_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    ordered_at = models.DateTimeField(default=timezone.now)
    cancelled_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("خدمة على زيارة")
        verbose_name_plural = _("خدمات الزيارات")
        ordering = ["ordered_at", "id"]
        indexes = [models.Index(fields=["organization", "ordered_at"])]

    def __str__(self):
        return f"{self.name} x{self.quantity}"

    @property
    def is_active(self) -> bool:
        return self.status != OrderStatus.CANCELLED

    @property
    def total(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def doctor_share(self) -> Decimal:
        return (self.total * self.doctor_share_percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)


# --- emergency arrivals ----------------------------------------------------------------------------------------


class Triage(models.TextChoices):
    CRITICAL = "critical", _("حرج")
    URGENT = "urgent", _("عاجل")
    NORMAL = "normal", _("عادي")


class EmergencyReason(models.TextChoices):
    LABOUR = "labour", _("ولادة / طلق")
    BLEEDING = "bleeding", _("نزيف")
    PAIN = "pain", _("ألم شديد")
    WATERS = "waters", _("نزول مياه")
    FAINTING = "fainting", _("إغماء / هبوط")
    PRESSURE = "pressure", _("ضغط عالي")
    OTHER = "other", _("سبب تاني")


class Disposition(models.TextChoices):
    DISCHARGED = "discharged", _("خرجت")
    THEATRE = "theatre", _("دخول عمليات")
    ADMITTED = "admitted", _("حجز غرفة")
    TRANSFERRED = "transferred", _("تحويل لمكان تاني")


class EmergencyCase(TenantScopedModel):
    """An emergency arrival. The appointment (source=emergency) carries the visit, the prescription and the money;
    this row carries what is specific to the emergency: how urgent, why, and the three times that matter."""

    appointment = models.OneToOneField("scheduling.Appointment", on_delete=models.PROTECT, related_name="emergency")
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="emergency_cases")
    doctor = models.ForeignKey("doctors.Doctor", on_delete=models.PROTECT, related_name="emergency_cases")
    triage = models.CharField(_("درجة الخطورة"), max_length=10, choices=Triage.choices, default=Triage.URGENT)
    reason = models.CharField(_("السبب"), max_length=12, choices=EmergencyReason.choices, default=EmergencyReason.OTHER)
    reason_text = models.CharField(_("تفاصيل"), max_length=200, blank=True)
    arrived_at = models.DateTimeField(default=timezone.now)
    seen_at = models.DateTimeField(null=True, blank=True)
    disposition = models.CharField(_("التصرف"), max_length=12, choices=Disposition.choices, blank=True)
    disposition_at = models.DateTimeField(null=True, blank=True)
    disposition_notes = models.CharField(_("ملاحظات التصرف"), max_length=300, blank=True)
    registered_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    disposition_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("حالة طوارئ")
        verbose_name_plural = _("حالات الطوارئ")
        ordering = ["-arrived_at"]
        indexes = [models.Index(fields=["organization", "-arrived_at"])]

    def __str__(self):
        return f"{self.patient} ({self.get_triage_display()})"

    @property
    def reason_label(self) -> str:
        label = self.get_reason_display()
        if self.reason_text:
            return self.reason_text if self.reason == EmergencyReason.OTHER else f"{label}: {self.reason_text}"
        return label

    @property
    def minutes_to_seen(self) -> int | None:
        if self.seen_at is None:
            return None
        return max(int((self.seen_at - self.arrived_at).total_seconds() // 60), 0)
