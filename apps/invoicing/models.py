"""Patient invoicing (plan 14 §14.6).

An invoice says what the center earned from a patient: the visit, the services on it, an operation, the nights of a
stay, extra charges. It is numbered, never edited once issued, and posted to the books the moment it is made. A
mistake is fixed by a credit note (إشعار دائن) that takes whole lines back; both stay on record.

Money a patient pays is a `billing.Payment`; what a contracted company or insurer owes is the payer's share of the
invoice; what a doctor earns from a line is an `InvoiceShare`.
"""

from decimal import Decimal

from django.conf import settings as dj_settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TenantScopedModel

PERCENT = [MinValueValidator(0), MaxValueValidator(100)]


class InvoiceSettings(TenantScopedModel):
    """One row per clinic."""

    # A discount above this share of the amount needs someone with "invoice.discount_any" (the owner by default).
    discount_limit_percent = models.DecimalField(
        _("أقصى خصم من غير موافقة %"), max_digits=5, decimal_places=2, default=Decimal("10"), validators=PERCENT
    )
    tax_registration_number = models.CharField(_("رقم التسجيل الضريبي"), max_length=40, blank=True)
    commercial_register = models.CharField(_("السجل التجاري"), max_length=40, blank=True)
    invoice_footer = models.CharField(_("سطر أسفل الفاتورة"), max_length=200, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("إعدادات الفواتير")
        verbose_name_plural = _("إعدادات الفواتير")
        constraints = [models.UniqueConstraint(fields=["organization"], name="uniq_invoice_settings_per_org")]

    def __str__(self):
        return f"InvoiceSettings<{self.organization_id}>"

    @classmethod
    def for_org(cls, org) -> "InvoiceSettings":
        return cls.objects.get_or_create(organization=org)[0]


class PayerKind(models.TextChoices):
    COMPANY = "company", _("شركة")
    INSURANCE = "insurance", _("تأمين طبي")
    OTHER = "other", _("جهة تانية")


class Payer(TenantScopedModel):
    """جهة تعاقد: a company or an insurer that pays part of its people's invoices. What it owes is its own account
    in the books (جهات التعاقد: ذمم مدينة, one subledger per payer)."""

    name = models.CharField(_("الاسم"), max_length=150)
    kind = models.CharField(_("النوع"), max_length=10, choices=PayerKind.choices, default=PayerKind.COMPANY)
    default_percent = models.DecimalField(
        _("بتتحمل كام % من الفاتورة"), max_digits=5, decimal_places=2, default=Decimal("100"), validators=PERCENT
    )
    phone = models.CharField(_("التليفون"), max_length=30, blank=True)
    contact_name = models.CharField(_("المسؤول"), max_length=120, blank=True)
    notes = models.CharField(_("ملاحظات"), max_length=300, blank=True)
    is_active = models.BooleanField(_("مفعّلة"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("جهة تعاقد")
        verbose_name_plural = _("جهات التعاقد")
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_payer_name_per_org")]

    def __str__(self):
        return self.name


class PatientCoverage(TenantScopedModel):
    """The patient is covered by a payer: every invoice issued while this is on is split between her and the payer."""

    patient = models.OneToOneField("patients.Patient", on_delete=models.CASCADE, related_name="coverage")
    payer = models.ForeignKey(Payer, on_delete=models.PROTECT, related_name="patients")
    percent = models.DecimalField(_("نسبة التحمل %"), max_digits=5, decimal_places=2, validators=PERCENT)
    member_no = models.CharField(_("رقم الكارنيه"), max_length=60, blank=True)
    is_active = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("تغطية مريض")
        verbose_name_plural = _("تغطيات المرضى")

    def __str__(self):
        return f"{self.patient} / {self.payer} {self.percent}%"


class ChargeKind(models.TextChoices):
    SERVICE = "service", _("خدمة")
    SUPPLY = "supply", _("مستلزمات وأدوية")
    FEE = "fee", _("أتعاب")
    OTHER = "other", _("بند تاني")


class Charge(TenantScopedModel):
    """An extra line on a patient's account (something used during a stay, a fee billed apart...). It waits on the
    account until an invoice takes it; a wrong one is cancelled, never deleted."""

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="charges")
    kind = models.CharField(_("النوع"), max_length=10, choices=ChargeKind.choices, default=ChargeKind.SERVICE)
    description = models.CharField(_("البند"), max_length=150)
    quantity = models.PositiveSmallIntegerField(
        _("العدد"), default=1, validators=[MinValueValidator(1), MaxValueValidator(999)]
    )
    unit_price = models.DecimalField(_("السعر"), max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    doctor = models.ForeignKey(
        "doctors.Doctor", null=True, blank=True, on_delete=models.PROTECT, related_name="charges"
    )
    doctor_share_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0, validators=PERCENT)
    operation = models.ForeignKey(
        "theatre.Operation", null=True, blank=True, on_delete=models.PROTECT, related_name="charges"
    )
    admission = models.ForeignKey(
        "theatre.Admission", null=True, blank=True, on_delete=models.PROTECT, related_name="charges"
    )
    is_cancelled = models.BooleanField(default=False)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("بند على حساب مريض")
        verbose_name_plural = _("بنود على حسابات المرضى")
        ordering = ["id"]
        indexes = [models.Index(fields=["organization", "patient"])]

    def __str__(self):
        return f"{self.description} x{self.quantity}"

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity


class InvoiceKind(models.TextChoices):
    INVOICE = "invoice", _("فاتورة")
    CREDIT = "credit", _("إشعار دائن")


PREFIX = {InvoiceKind.INVOICE: "INV", InvoiceKind.CREDIT: "CN"}


class InvoiceSequence(TenantScopedModel):
    """Last number per kind per fiscal year (taken under `select_for_update`, so there are no gaps)."""

    fiscal_year = models.ForeignKey("ledger.FiscalYear", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=InvoiceKind.choices)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["fiscal_year", "kind"], name="uniq_invoice_sequence")]


class Invoice(TenantScopedModel):
    """`gross - discount = total = payer_share + patient_share` (CHECK constraints). A credit note carries the same
    amounts, positive: its entry is the mirror of an invoice's."""

    kind = models.CharField(max_length=10, choices=InvoiceKind.choices, default=InvoiceKind.INVOICE)
    fiscal_year = models.ForeignKey("ledger.FiscalYear", on_delete=models.PROTECT, related_name="+")
    number = models.PositiveIntegerField()
    date = models.DateField(_("التاريخ"))
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="invoices")
    # A visit's invoice (made by the program as the visit is paid for or finished). Blank = an account invoice.
    appointment = models.ForeignKey(
        "scheduling.Appointment", null=True, blank=True, on_delete=models.PROTECT, related_name="invoices"
    )
    payer = models.ForeignKey(Payer, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices")
    coverage_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    credit_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="credit_notes")
    gross = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    payer_share = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    patient_share = models.DecimalField(max_digits=12, decimal_places=2)
    # the part of the patient's deposit used to pay this invoice when it was issued
    deposit_applied = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reason = models.CharField(max_length=200, blank=True)  # of the discount, or of the credit note
    issued_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    issued_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = _("فاتورة")
        verbose_name_plural = _("الفواتير")
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "fiscal_year", "kind", "number"], name="uniq_invoice_number"
            ),
            models.CheckConstraint(condition=Q(total=F("gross") - F("discount")), name="invoice_total_adds_up"),
            models.CheckConstraint(
                condition=Q(total=F("payer_share") + F("patient_share")), name="invoice_shares_add_up"
            ),
            models.CheckConstraint(
                condition=Q(gross__gte=0) & Q(discount__gte=0) & Q(payer_share__gte=0) & Q(patient_share__gte=0),
                name="invoice_amounts_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(kind="invoice", credit_of__isnull=True) | Q(kind="credit", credit_of__isnull=False),
                name="invoice_credit_note_has_its_invoice",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "date"]),
            models.Index(fields=["organization", "patient", "date"]),
        ]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        return f"{PREFIX[self.kind]}-{self.fiscal_year.start_date:%Y}-{self.number:05d}"

    @property
    def is_credit(self) -> bool:
        return self.kind == InvoiceKind.CREDIT


class LineKind(models.TextChoices):
    VISIT = "visit", _("كشف")
    SERVICE = "service", _("خدمة")
    OPERATION = "operation", _("عملية")
    ROOM = "room", _("إقامة")
    CHARGE = "charge", _("بند")


class InvoiceLine(TenantScopedModel):
    """`gross - discount = net = payer_amount + patient_amount`. A line names what it bills (the visit, the service
    order, the operation, the stay, the charge): while it is live nothing else can bill the same thing (partial
    unique constraints). A credit note's line points at the line it takes back, which stops being live."""

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="lines")
    order = models.PositiveSmallIntegerField(default=0)
    kind = models.CharField(max_length=10, choices=LineKind.choices)
    description = models.CharField(max_length=200)
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    gross = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=12, decimal_places=2)
    payer_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    patient_amount = models.DecimalField(max_digits=12, decimal_places=2)
    revenue_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")
    cost_center = models.ForeignKey("ledger.CostCenter", on_delete=models.PROTECT, related_name="+")
    doctor = models.ForeignKey("doctors.Doctor", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    # what the line bills (one of these)
    appointment = models.ForeignKey(
        "scheduling.Appointment", null=True, blank=True, on_delete=models.PROTECT, related_name="invoice_lines"
    )
    service_order = models.ForeignKey(
        "center.ServiceOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="invoice_lines"
    )
    operation = models.ForeignKey(
        "theatre.Operation", null=True, blank=True, on_delete=models.PROTECT, related_name="invoice_lines"
    )
    admission = models.ForeignKey(
        "theatre.Admission", null=True, blank=True, on_delete=models.PROTECT, related_name="invoice_lines"
    )
    charge = models.ForeignKey(Charge, null=True, blank=True, on_delete=models.PROTECT, related_name="invoice_lines")
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by")
    is_live = models.BooleanField(default=True)  # an invoice line no credit note took back

    class Meta:
        verbose_name = _("بند فاتورة")
        verbose_name_plural = _("بنود الفواتير")
        ordering = ["invoice_id", "order", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(net=F("gross") - F("discount")), name="invoice_line_net_adds_up"),
            models.CheckConstraint(
                condition=Q(net=F("payer_amount") + F("patient_amount")), name="invoice_line_shares_add_up"
            ),
            models.CheckConstraint(
                condition=Q(gross__gte=0) & Q(discount__gte=0) & Q(payer_amount__gte=0) & Q(patient_amount__gte=0),
                name="invoice_line_amounts_not_negative",
            ),
            *[
                models.UniqueConstraint(
                    fields=[source], condition=Q(is_live=True, reverses__isnull=True), name=f"uniq_live_line_{source}"
                )
                for source in ("appointment", "service_order", "operation", "admission", "charge")
            ],
        ]

    def __str__(self):
        return f"{self.description}: {self.net}"

    SOURCES = ("appointment", "service_order", "operation", "admission", "charge")

    @property
    def source_key(self) -> tuple:
        for name in self.SOURCES:
            pk = getattr(self, f"{name}_id")
            if pk:
                return name, pk
        return "", None


class InvoiceShare(TenantScopedModel):
    """What someone earns from an invoice line: the doctor's percentage of a visit or a service, a team member's fee
    for an operation. An internal doctor's share goes to their account in "أتعاب أطباء مستحقة"; someone from outside
    (a name only) goes to "أتعاب فريق خارجي مستحقة"."""

    line = models.ForeignKey(InvoiceLine, on_delete=models.PROTECT, related_name="shares")
    doctor = models.ForeignKey("doctors.Doctor", null=True, blank=True, on_delete=models.PROTECT, related_name="shares")
    payee_name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=60, blank=True)
    percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])

    class Meta:
        verbose_name = _("نصيب من فاتورة")
        verbose_name_plural = _("أنصبة الفواتير")
        ordering = ["id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="invoice_share_positive")]

    def __str__(self):
        return f"{self.doctor or self.payee_name}: {self.amount}"
