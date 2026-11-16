"""Stock of medical supplies (plan 14 §14.8).

Quantities and values change only through `StockMove` rows written by inventory.services under a lock on the item's
balance row. The value of what is in a store is kept next to its quantity (`StockBalance.value`), so the weighted
average cost is `value / quantity` and taking stock out never leaves a rounding remainder behind: the last unit takes
whatever value is left. The sum of all balances' values is the balance of the inventory account in the books.
"""

from decimal import Decimal

from django.conf import settings as dj_settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TenantScopedModel

QTY = {"max_digits": 12, "decimal_places": 3}
VALUE = {"max_digits": 14, "decimal_places": 2}


class InventorySettings(TenantScopedModel):
    expiry_alert_days = models.PositiveSmallIntegerField(_("تنبيه قبل انتهاء الصلاحية (يوم)"), default=60)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("إعدادات المخزن")
        verbose_name_plural = _("إعدادات المخزن")
        constraints = [models.UniqueConstraint(fields=["organization"], name="uniq_inventory_settings_per_org")]

    @classmethod
    def for_org(cls, org) -> "InventorySettings":
        return cls.objects.get_or_create(organization=org)[0]


class Warehouse(TenantScopedModel):
    name = models.CharField(_("الاسم"), max_length=80)
    system_key = models.CharField(max_length=20, blank=True, editable=False)  # "main" / "theatre"
    is_active = models.BooleanField(_("مفعّل"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("مخزن")
        verbose_name_plural = _("المخازن")
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_warehouse_name_per_org")]

    def __str__(self):
        return self.name


class StockItem(TenantScopedModel):
    name = models.CharField(_("الصنف"), max_length=150)
    unit = models.CharField(_("الوحدة"), max_length=20, default="قطعة")
    category = models.CharField(_("التصنيف"), max_length=60, blank=True)
    reorder_level = models.DecimalField(_("حد إعادة الطلب"), default=0, validators=[MinValueValidator(0)], **QTY)
    track_expiry = models.BooleanField(_("له تاريخ صلاحية"), default=False)
    # What a patient is charged when it is used in her operation. 0 = inside the operation's price, not billed apart.
    sale_price = models.DecimalField(
        _("سعر البيع للمريضة"), max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    is_active = models.BooleanField(_("مفعّل"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("صنف")
        verbose_name_plural = _("الأصناف")
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_stock_item_name_per_org")]

    def __str__(self):
        return self.name


class StockBalance(TenantScopedModel):
    """What one store holds of one item: how many, and what they cost in total. Never below zero (CHECK)."""

    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="balances")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="balances")
    quantity = models.DecimalField(default=0, **QTY)
    value = models.DecimalField(default=0, **VALUE)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["item", "warehouse"], name="uniq_stock_balance"),
            models.CheckConstraint(condition=Q(quantity__gte=0) & Q(value__gte=0), name="stock_balance_not_negative"),
            # nothing on the shelf is worth nothing: an empty balance has no value left on it
            models.CheckConstraint(condition=Q(quantity__gt=0) | Q(value=0), name="stock_balance_empty_has_no_value"),
        ]

    @property
    def average_cost(self) -> Decimal:
        return self.value / self.quantity if self.quantity else Decimal(0)


class StockBatch(TenantScopedModel):
    """A lot of an expiry-tracked item in a store. The batches of a balance add up to its quantity."""

    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="batches")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="batches")
    expiry_date = models.DateField(_("تاريخ الصلاحية"), null=True, blank=True)
    lot = models.CharField(_("رقم التشغيلة"), max_length=40, blank=True)
    quantity = models.DecimalField(default=0, **QTY)

    class Meta:
        ordering = ["expiry_date", "id"]
        constraints = [
            models.UniqueConstraint(fields=["item", "warehouse", "expiry_date", "lot"], name="uniq_stock_batch"),
            models.CheckConstraint(condition=Q(quantity__gte=0), name="stock_batch_not_negative"),
        ]
        indexes = [models.Index(fields=["organization", "expiry_date"])]


class DocKind(models.TextChoices):
    ISSUE = "issue", _("صرف")
    TRANSFER = "transfer", _("تحويل بين مخزنين")
    ADJUSTMENT = "adjustment", _("تسوية جرد")
    OPENING = "opening", _("رصيد افتتاحي")


class StockSequence(TenantScopedModel):
    fiscal_year = models.ForeignKey("ledger.FiscalYear", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=12, choices=DocKind.choices)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["fiscal_year", "kind"], name="uniq_stock_sequence")]


class StockDoc(TenantScopedModel):
    """A store document: an issue to a department or an operation, a transfer, a count adjustment, an opening
    balance. Its moves are its lines; its entry (when it moves value in or out of the stores) names it as source."""

    kind = models.CharField(max_length=12, choices=DocKind.choices)
    fiscal_year = models.ForeignKey("ledger.FiscalYear", on_delete=models.PROTECT, related_name="+")
    number = models.PositiveIntegerField()
    date = models.DateField(_("التاريخ"))
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="docs")
    to_warehouse = models.ForeignKey(Warehouse, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    cost_center = models.ForeignKey(
        "ledger.CostCenter", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    operation = models.ForeignKey(
        "theatre.Operation", null=True, blank=True, on_delete=models.PROTECT, related_name="stock_docs"
    )
    notes = models.CharField(_("ملاحظات"), max_length=300, blank=True)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        verbose_name = _("مستند مخزن")
        verbose_name_plural = _("مستندات المخزن")
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "fiscal_year", "kind", "number"], name="uniq_stock_doc_no")
        ]

    PREFIX = {"issue": "ISS", "transfer": "TRF", "adjustment": "ADJ", "opening": "OPN"}

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        return f"{self.PREFIX[self.kind]}-{self.fiscal_year.start_date:%Y}-{self.number:05d}"


class MoveKind(models.TextChoices):
    RECEIPT = "receipt", _("استلام من مشتريات")
    PURCHASE_RETURN = "purchase_return", _("مرتجع لمورد")
    ISSUE = "issue", _("صرف")
    ISSUE_RETURN = "issue_return", _("رجوع من صرف")
    TRANSFER_OUT = "transfer_out", _("تحويل خارج")
    TRANSFER_IN = "transfer_in", _("تحويل داخل")
    ADJUST_IN = "adjust_in", _("زيادة جرد")
    ADJUST_OUT = "adjust_out", _("عجز جرد")
    OPENING = "opening", _("رصيد افتتاحي")


MOVES_IN = (MoveKind.RECEIPT, MoveKind.ISSUE_RETURN, MoveKind.TRANSFER_IN, MoveKind.ADJUST_IN, MoveKind.OPENING)


class StockMove(TenantScopedModel):
    """One change of one item in one store. `quantity` and `value` are positive; the kind says in or out.
    Never edited or deleted: a mistake is another move the other way."""

    kind = models.CharField(max_length=16, choices=MoveKind.choices)
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="moves")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="moves")
    date = models.DateField()
    quantity = models.DecimalField(validators=[MinValueValidator(Decimal("0.001"))], **QTY)
    value = models.DecimalField(**VALUE)
    expiry_date = models.DateField(null=True, blank=True)
    lot = models.CharField(max_length=40, blank=True)
    doc = models.ForeignKey(StockDoc, null=True, blank=True, on_delete=models.PROTECT, related_name="moves")
    purchase_line = models.ForeignKey(
        "purchasing.PurchaseLine", null=True, blank=True, on_delete=models.PROTECT, related_name="stock_moves"
    )
    return_line = models.ForeignKey(
        "purchasing.PurchaseReturnLine", null=True, blank=True, on_delete=models.PROTECT, related_name="stock_moves"
    )
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by")
    # the balance after this move (for the item card)
    quantity_after = models.DecimalField(**QTY)
    value_after = models.DecimalField(**VALUE)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = _("حركة مخزن")
        verbose_name_plural = _("حركات المخزن")
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0) & Q(value__gte=0), name="stock_move_positive"),
        ]
        indexes = [models.Index(fields=["organization", "item", "warehouse", "id"])]

    @property
    def is_in(self) -> bool:
        return self.kind in MOVES_IN

    @property
    def unit_cost(self) -> Decimal:
        return self.value / self.quantity if self.quantity else Decimal(0)


class CountKind(models.TextChoices):
    FULL = "full", _("جرد كامل")
    SPOT = "spot", _("جرد مفاجئ (عينة)")


class CountStatus(models.TextChoices):
    OPEN = "open", _("جاري العد")
    SUBMITTED = "submitted", _("مستني الاعتماد")
    APPROVED = "approved", _("معتمد")
    CANCELLED = "cancelled", _("ملغي")


class StockCount(TenantScopedModel):
    """جرد: someone counts what is on the shelves without seeing what the books expect; the differences wait for a
    second person, and approving them writes the adjustment moves and their entry."""

    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="counts")
    kind = models.CharField(max_length=6, choices=CountKind.choices, default=CountKind.FULL)
    status = models.CharField(max_length=10, choices=CountStatus.choices, default=CountStatus.OPEN)
    notes = models.CharField(_("ملاحظات"), max_length=300, blank=True)
    counted_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    doc = models.OneToOneField(StockDoc, null=True, blank=True, on_delete=models.PROTECT, related_name="count")

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("جرد مخزن")
        verbose_name_plural = _("جرد المخازن")
        ordering = ["-started_at", "-id"]
        constraints = [
            # one count at a time per store: what it expects must not move under it unnoticed
            models.UniqueConstraint(
                fields=["warehouse"], condition=Q(status__in=["open", "submitted"]), name="uniq_running_count_per_store"
            )
        ]


class StockCountLine(TenantScopedModel):
    count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="+")
    counted = models.DecimalField(null=True, blank=True, **QTY)  # blank = not counted yet
    # filled when the count is submitted: what the books held at that moment
    expected = models.DecimalField(null=True, blank=True, **QTY)
    expected_value = models.DecimalField(null=True, blank=True, **VALUE)

    class Meta:
        ordering = ["item__name", "id"]
        constraints = [models.UniqueConstraint(fields=["count", "item"], name="uniq_count_line_per_item")]

    @property
    def difference(self):
        if self.counted is None or self.expected is None:
            return None
        return self.counted - self.expected
