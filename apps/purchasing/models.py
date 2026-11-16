"""Suppliers, purchase invoices and returns, supplier payments and the taxes on them (plan 14 §14.7).

Like every money document here: numbered per fiscal year with no gaps, posted to the books in the transaction that
makes it, never edited afterwards. A mistake is cancelled (its entry is reversed) or returned, and both stay on record.
"""

import secrets
from decimal import Decimal

from django.conf import settings as dj_settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TenantScopedModel

PERCENT = [MinValueValidator(0), MaxValueValidator(100)]


class PurchaseSettings(TenantScopedModel):
    """One row per clinic."""

    # Egyptian practice: nothing is withheld from a payment whose taxable value is this much or less.
    wht_threshold = models.DecimalField(
        _("حد الخصم تحت حساب الضريبة"), max_digits=10, decimal_places=2, default=Decimal("300"),
        validators=[MinValueValidator(0)],
    )  # fmt: skip
    cheque_alert_days = models.PositiveSmallIntegerField(_("تنبيه الشيكات قبل الاستحقاق (يوم)"), default=7)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("إعدادات المشتريات")
        verbose_name_plural = _("إعدادات المشتريات")
        constraints = [models.UniqueConstraint(fields=["organization"], name="uniq_purchase_settings_per_org")]

    @classmethod
    def for_org(cls, org) -> "PurchaseSettings":
        return cls.objects.get_or_create(organization=org)[0]


class TaxKind(models.TextChoices):
    VAT = "vat", _("ضريبة قيمة مضافة")
    WITHHOLDING = "withholding", _("خصم تحت حساب الضريبة")


class TaxCode(TenantScopedModel):
    """A tax as the clinic uses it. VAT on a purchase is either recoverable (it goes to "ض.ق.م على المشتريات") or,
    for a clinic that is not VAT-registered, part of what the thing cost. Withholding is taken from what a supplier
    is paid and owed to the tax authority."""

    name = models.CharField(_("الاسم"), max_length=80)
    kind = models.CharField(_("النوع"), max_length=12, choices=TaxKind.choices)
    rate = models.DecimalField(_("النسبة %"), max_digits=5, decimal_places=2, validators=PERCENT)
    recoverable = models.BooleanField(_("تتخصم (العيادة مسجلة في القيمة المضافة)"), default=False)
    system_key = models.CharField(max_length=20, blank=True, editable=False)
    is_active = models.BooleanField(_("مفعّلة"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("ضريبة")
        verbose_name_plural = _("الضرائب")
        ordering = ["kind", "rate", "id"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_tax_code_name_per_org")]

    def __str__(self):
        return self.name


class Supplier(TenantScopedModel):
    """What the clinic owes a supplier is that supplier's own account inside "الموردون" (one subledger each)."""

    name = models.CharField(_("الاسم"), max_length=150)
    tax_registration_number = models.CharField(_("رقم التسجيل الضريبي"), max_length=40, blank=True)
    tax_file_number = models.CharField(_("رقم الملف الضريبي"), max_length=40, blank=True)
    commercial_register = models.CharField(_("السجل التجاري"), max_length=40, blank=True)
    phone = models.CharField(_("التليفون"), max_length=30, blank=True)
    address = models.CharField(_("العنوان"), max_length=200, blank=True)
    payment_terms_days = models.PositiveSmallIntegerField(_("مهلة السداد (يوم)"), default=0)
    default_withholding = models.ForeignKey(
        TaxCode, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=_("الخصم تحت حساب الضريبة المعتاد"),
    )  # fmt: skip
    notes = models.CharField(_("ملاحظات"), max_length=300, blank=True)
    is_active = models.BooleanField(_("مفعّل"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("مورد")
        verbose_name_plural = _("الموردين")
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_supplier_name_per_org")]

    def __str__(self):
        return self.name


class DocKind(models.TextChoices):
    INVOICE = "invoice", _("فاتورة مشتريات")
    RETURN = "return", _("مرتجع مشتريات")
    PAYMENT = "payment", _("سداد مورد")


class PurchaseSequence(TenantScopedModel):
    fiscal_year = models.ForeignKey("ledger.FiscalYear", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=DocKind.choices)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["fiscal_year", "kind"], name="uniq_purchase_sequence")]


class DocStatus(models.TextChoices):
    POSTED = "posted", _("مرحّل")
    CANCELLED = "cancelled", _("ملغي")


def scan_path(instance, filename):
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return f"org_{instance.organization_id}/purchases/{secrets.token_hex(16)}{ext}"


class PurchaseInvoice(TenantScopedModel):
    """`subtotal + vat_total = total` is what the supplier is owed. `subtotal` is after line discounts, before VAT."""

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="invoices")
    fiscal_year = models.ForeignKey("ledger.FiscalYear", on_delete=models.PROTECT, related_name="+")
    number = models.PositiveIntegerField()
    supplier_ref = models.CharField(_("رقم فاتورة المورد"), max_length=60, blank=True)
    date = models.DateField(_("التاريخ"))
    due_date = models.DateField(_("تاريخ الاستحقاق"))
    prices_include_vat = models.BooleanField(_("الأسعار شاملة الضريبة"), default=False)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2)
    vat_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    notes = models.CharField(_("ملاحظات"), max_length=300, blank=True)
    # a scan of the paper invoice (private file, served by a permission-checked view only)
    scan = models.FileField(upload_to=scan_path, blank=True)
    scan_content_type = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=10, choices=DocStatus.choices, default=DocStatus.POSTED)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    cancelled_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = _("فاتورة مشتريات")
        verbose_name_plural = _("فواتير المشتريات")
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "fiscal_year", "number"], name="uniq_purchase_number"),
            # the same paper invoice of a supplier cannot be entered twice
            models.UniqueConstraint(
                fields=["supplier", "supplier_ref"],
                condition=Q(status="posted") & ~Q(supplier_ref=""),
                name="uniq_supplier_invoice_ref",
            ),
            models.CheckConstraint(condition=Q(total=F("subtotal") + F("vat_total")), name="purchase_total_adds_up"),
            models.CheckConstraint(condition=Q(subtotal__gte=0) & Q(vat_total__gte=0), name="purchase_not_negative"),
        ]
        indexes = [models.Index(fields=["organization", "date"]), models.Index(fields=["supplier", "due_date"])]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        return f"PINV-{self.fiscal_year.start_date:%Y}-{self.number:05d}"

    @property
    def is_cancelled(self) -> bool:
        return self.status == DocStatus.CANCELLED


class PurchaseLine(TenantScopedModel):
    """One thing bought. `net` is after the discount and before VAT; `vat_amount` is its tax, rounded once here.
    The line's cost in the books is `net`, plus the VAT when it is not recoverable."""

    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.PROTECT, related_name="lines")
    order = models.PositiveSmallIntegerField(default=0)
    description = models.CharField(max_length=200)
    account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")
    cost_center = models.ForeignKey(
        "ledger.CostCenter", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))])
    unit_price = models.DecimalField(max_digits=14, decimal_places=4, validators=[MinValueValidator(0)])
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=14, decimal_places=2)
    # A stock item: the line's account is the inventory account, and the goods go on the shelf of `warehouse` at
    # the line's cost (apps/inventory). Blank = bought straight to an expense or asset account.
    item = models.ForeignKey(
        "inventory.StockItem", null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_lines"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    expiry_date = models.DateField(null=True, blank=True)
    lot = models.CharField(max_length=40, blank=True)
    tax_code = models.ForeignKey(TaxCode, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    vat_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    vat_recoverable = models.BooleanField(default=False)
    total = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        verbose_name = _("بند فاتورة مشتريات")
        verbose_name_plural = _("بنود فواتير المشتريات")
        ordering = ["invoice_id", "order", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(total=F("net") + F("vat_amount")), name="purchase_line_adds_up"),
            models.CheckConstraint(
                condition=Q(net__gte=0) & Q(vat_amount__gte=0) & Q(discount__gte=0), name="purchase_line_not_negative"
            ),
        ]

    def __str__(self):
        return f"{self.description}: {self.total}"

    @property
    def cost(self) -> Decimal:
        """What the books charge to the line's account."""
        return self.net if self.vat_recoverable else self.net + self.vat_amount


class PurchaseReturn(TenantScopedModel):
    """Goods sent back (or a price taken off) against one purchase invoice: the mirror of part of its entry."""

    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.PROTECT, related_name="returns")
    fiscal_year = models.ForeignKey("ledger.FiscalYear", on_delete=models.PROTECT, related_name="+")
    number = models.PositiveIntegerField()
    date = models.DateField(_("التاريخ"))
    reason = models.CharField(_("السبب"), max_length=200)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2)
    vat_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        verbose_name = _("مرتجع مشتريات")
        verbose_name_plural = _("مرتجعات المشتريات")
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "fiscal_year", "number"], name="uniq_purchase_return_no"),
            models.CheckConstraint(condition=Q(total=F("subtotal") + F("vat_total")), name="purchase_return_adds_up"),
        ]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        return f"PRET-{self.fiscal_year.start_date:%Y}-{self.number:05d}"


class PurchaseReturnLine(TenantScopedModel):
    ret = models.ForeignKey(PurchaseReturn, on_delete=models.PROTECT, related_name="lines")
    line = models.ForeignKey(PurchaseLine, on_delete=models.PROTECT, related_name="returned")
    quantity = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))])
    net = models.DecimalField(max_digits=14, decimal_places=2)
    vat_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    # for a stock line: the value that left the store with the goods (today's average, not what was paid for them)
    stock_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(total=F("net") + F("vat_amount")), name="purchase_return_line_adds_up")
        ]

    @property
    def cost(self) -> Decimal:
        return self.net if self.line.vat_recoverable else self.net + self.vat_amount


class PayMethod(models.TextChoices):
    CASH = "cash", _("نقدي من خزينة")
    TRANSFER = "transfer", _("تحويل بنكي")
    CHEQUE = "cheque", _("شيك")


class SupplierPayment(TenantScopedModel):
    """`amount` is what comes off the supplier's account. Part of it may be withheld for the tax authority
    (`wht_amount`); the rest (`net_paid`) leaves a treasury now, or is a cheque that leaves the bank when it clears."""

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="payments")
    fiscal_year = models.ForeignKey("ledger.FiscalYear", on_delete=models.PROTECT, related_name="+")
    number = models.PositiveIntegerField()
    date = models.DateField(_("التاريخ"))
    method = models.CharField(_("طريقة السداد"), max_length=10, choices=PayMethod.choices)
    treasury = models.ForeignKey("treasury.Treasury", on_delete=models.PROTECT, related_name="supplier_payments")
    cheque = models.OneToOneField(
        "treasury.Cheque", null=True, blank=True, on_delete=models.PROTECT, related_name="supplier_payment"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    wht_code = models.ForeignKey(TaxCode, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    wht_base = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    wht_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    wht_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_paid = models.DecimalField(max_digits=14, decimal_places=2)
    notes = models.CharField(_("ملاحظات"), max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=DocStatus.choices, default=DocStatus.POSTED)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    cancelled_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = _("سداد مورد")
        verbose_name_plural = _("مدفوعات الموردين")
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "fiscal_year", "number"], name="uniq_supplier_payment_no"),
            models.CheckConstraint(
                condition=Q(net_paid=F("amount") - F("wht_amount")), name="supplier_payment_adds_up"
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0) & Q(wht_amount__gte=0) & Q(net_paid__gte=0),
                name="supplier_payment_not_negative",
            ),
        ]
        indexes = [models.Index(fields=["organization", "date"])]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        return f"SPAY-{self.fiscal_year.start_date:%Y}-{self.number:05d}"

    @property
    def is_cancelled(self) -> bool:
        return self.status == DocStatus.CANCELLED


class PaymentAllocation(TenantScopedModel):
    """Which invoice a payment settles (in part or whole). What is not allocated stays on the supplier's account."""

    payment = models.ForeignKey(SupplierPayment, on_delete=models.PROTECT, related_name="allocations")
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.PROTECT, related_name="allocations")
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "invoice"], name="uniq_allocation_per_invoice"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="allocation_positive"),
        ]
