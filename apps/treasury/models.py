"""Where the money physically is (plan 14 §14.5 "Treasury"): cash drawers, the safe, bank accounts and the
electronic collection accounts, the vouchers that move money in and out, cashier shifts and cash counts.

Each treasury is tied to one leaf account of the chart. The treasury's balance is never stored: it is that account's
posted balance, so the cashbox and the books cannot disagree.
"""

from django.conf import settings as dj_settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.billing.models import PaymentMethod
from apps.core.models import TenantScopedModel


class TreasuryKind(models.TextChoices):
    DRAWER = "drawer", _("درج كاشير")
    SAFE = "safe", _("خزينة")
    BANK = "bank", _("حساب بنكي")
    ELECTRONIC = "electronic", _("تحصيل إلكتروني")


class Treasury(TenantScopedModel):
    name = models.CharField(_("الاسم"), max_length=80)
    kind = models.CharField(_("النوع"), max_length=12, choices=TreasuryKind.choices, default=TreasuryKind.DRAWER)
    account = models.OneToOneField("ledger.Account", on_delete=models.PROTECT, related_name="treasury")
    # which way of paying lands here when reception takes money (cash → the drawer, card → card receipts...)
    receives_method = models.CharField(_("بيستقبل"), max_length=10, choices=PaymentMethod.choices, blank=True)
    system_key = models.CharField(max_length=40, blank=True, editable=False)
    is_active = models.BooleanField(_("مفعّلة"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("خزينة")
        verbose_name_plural = _("الخزن والبنوك")
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_treasury_name_per_org"),
            models.UniqueConstraint(
                fields=["organization", "receives_method"],
                condition=~Q(receives_method=""),
                name="uniq_treasury_per_payment_method",
            ),
            models.UniqueConstraint(
                fields=["organization", "system_key"], condition=~Q(system_key=""), name="uniq_treasury_system_key"
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def holds_cash(self) -> bool:
        return self.kind in (TreasuryKind.DRAWER, TreasuryKind.SAFE)


# --- vouchers --------------------------------------------------------------------------------------------------


class VoucherKind(models.TextChoices):
    RECEIPT = "receipt", _("سند قبض")
    PAYMENT = "payment", _("سند صرف")
    TRANSFER = "transfer", _("تحويل بين الخزن")


class VoucherStatus(models.TextChoices):
    POSTED = "posted", _("مرحّل")
    CANCELLED = "cancelled", _("ملغي")


class VoucherSequence(TenantScopedModel):
    """Last voucher number per kind per fiscal year (taken under `select_for_update`, like entry numbers)."""

    fiscal_year = models.ForeignKey("ledger.FiscalYear", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=VoucherKind.choices)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["fiscal_year", "kind"], name="uniq_voucher_sequence")]


class Voucher(TenantScopedModel):
    """Money in (receipt), money out (payment) or money moved between two treasuries (transfer). Posted the moment it
    is saved; a wrong voucher is cancelled, which posts the reversal and keeps both on record."""

    kind = models.CharField(_("النوع"), max_length=10, choices=VoucherKind.choices)
    fiscal_year = models.ForeignKey("ledger.FiscalYear", on_delete=models.PROTECT, related_name="+")
    number = models.PositiveIntegerField()
    date = models.DateField(_("التاريخ"))
    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, related_name="vouchers")
    # receipt / payment: the other side of the entry. transfer: the treasury the money goes to.
    counter_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    to_treasury = models.ForeignKey(Treasury, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(_("المبلغ"), max_digits=14, decimal_places=2, validators=[MinValueValidator(0.01)])
    cost_center = models.ForeignKey(
        "ledger.CostCenter", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    party_name = models.CharField(_("الطرف"), max_length=150, blank=True)
    # Whose account inside a control account the voucher moves (paying a doctor, money from a payer...): the
    # same (type, id) pair the journal line carries.
    party_type = models.CharField(max_length=10, blank=True)
    party_id = models.PositiveBigIntegerField(null=True, blank=True)
    # made from a recurring expense (rent, salaries...): how the screen knows this month's is already recorded
    template = models.ForeignKey(
        "treasury.ExpenseTemplate", null=True, blank=True, on_delete=models.PROTECT, related_name="vouchers"
    )
    description = models.CharField(_("البيان"), max_length=300)
    status = models.CharField(max_length=10, choices=VoucherStatus.choices, default=VoucherStatus.POSTED)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    cancelled_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("سند")
        verbose_name_plural = _("السندات")
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "fiscal_year", "kind", "number"], name="uniq_voucher_number"
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="voucher_amount_positive"),
            models.CheckConstraint(
                condition=(Q(kind="transfer") & Q(to_treasury__isnull=False) & Q(counter_account__isnull=True))
                | (~Q(kind="transfer") & Q(counter_account__isnull=False) & Q(to_treasury__isnull=True)),
                name="voucher_other_side_matches_kind",
            ),
        ]
        indexes = [models.Index(fields=["organization", "date"])]

    def __str__(self):
        return f"{self.get_kind_display()} {self.number}"

    @property
    def is_cancelled(self) -> bool:
        return self.status == VoucherStatus.CANCELLED


# --- shifts and counts -----------------------------------------------------------------------------------------


class ShiftStatus(models.TextChoices):
    OPEN = "open", _("مفتوحة")
    CLOSED = "closed", _("مقفولة")


class Shift(TenantScopedModel):
    """A cashier's shift on one cash treasury: opened when the first money is taken, closed with a count. The
    expected amount is the treasury account's balance; the difference between it and the count waits for approval
    (by someone other than the person who counted, unless that person is the owner) and then posts to
    "عجز وزيادة الخزينة"."""

    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, related_name="shifts")
    status = models.CharField(max_length=10, choices=ShiftStatus.choices, default=ShiftStatus.OPEN)
    opened_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    opened_at = models.DateTimeField(default=timezone.now)
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    closed_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    handed_over = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("وردية")
        verbose_name_plural = _("الورديات")
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["treasury"], condition=Q(status="open"), name="uniq_open_shift_per_treasury"
            )
        ]

    def __str__(self):
        return f"{self.treasury} {self.opened_at:%Y-%m-%d %H:%M}"


class CountKind(models.TextChoices):
    SHIFT_CLOSE = "shift_close", _("تقفيل وردية")
    SURPRISE = "surprise", _("جرد مفاجئ")


class VarianceStatus(models.TextChoices):
    NONE = "none", _("مطابق")
    PENDING = "pending", _("مستني اعتماد")
    APPROVED = "approved", _("معتمد")


class CashCount(TenantScopedModel):
    """Cash counted by denomination against what the books expect. A shift-close count with a difference posts it to
    "عجز وزيادة الخزينة" once approved; a surprise count only records what was found and who witnessed it."""

    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, related_name="counts")
    shift = models.OneToOneField(Shift, null=True, blank=True, on_delete=models.PROTECT, related_name="count")
    kind = models.CharField(max_length=12, choices=CountKind.choices)
    counted_at = models.DateTimeField(default=timezone.now)
    denominations = models.JSONField(default=dict, blank=True)  # {"200": 3, "100": 5, ...}
    counted_total = models.DecimalField(max_digits=14, decimal_places=2)
    expected_total = models.DecimalField(max_digits=14, decimal_places=2)
    counted_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    witness_name = models.CharField(_("الشاهد"), max_length=120, blank=True)
    notes = models.CharField(_("ملاحظات"), max_length=300, blank=True)
    variance_status = models.CharField(max_length=10, choices=VarianceStatus.choices, default=VarianceStatus.NONE)
    approved_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("جرد خزينة")
        verbose_name_plural = _("جرد الخزينة")
        ordering = ["-counted_at"]

    def __str__(self):
        return f"{self.treasury} {self.counted_at:%Y-%m-%d}"

    @property
    def difference(self):
        """Counted less expected: negative = short (عجز), positive = over (زيادة)."""
        return self.counted_total - self.expected_total


# --- cheques ---------------------------------------------------------------------------------------------------


class ChequeDirection(models.TextChoices):
    ISSUED = "issued", _("صادر (أوراق دفع)")
    RECEIVED = "received", _("وارد (أوراق قبض)")


class ChequeStatus(models.TextChoices):
    # issued by the clinic
    ISSUED = "issued", _("صادر، لسه ما اتصرفش")
    CLEARED = "cleared", _("اتصرف من البنك")
    RETURNED = "returned", _("رجع")
    CANCELLED = "cancelled", _("اتلغى")
    # received by the clinic
    IN_HAND = "in_hand", _("في الخزنة")
    DEPOSITED = "deposited", _("في البنك للتحصيل")
    COLLECTED = "collected", _("اتحصّل")
    BOUNCED = "bounced", _("ارتد")
    ENDORSED = "endorsed", _("اتظهّر لمورد")


# a cheque in one of these still stands in its control account (أوراق دفع / أوراق قبض)
CHEQUE_OPEN = {
    ChequeDirection.ISSUED: (ChequeStatus.ISSUED,),
    ChequeDirection.RECEIVED: (ChequeStatus.IN_HAND, ChequeStatus.DEPOSITED),
}


class Cheque(TenantScopedModel):
    """A post-dated cheque. Issued: it sits in "شيكات صادرة لم تُصرف" until the bank pays it. Received: it sits in
    "شيكات تحت التحصيل" until it is collected, bounces or is endorsed to a supplier. Every step that moves money
    posts its entry (treasury/cheques.py) and is written to `ChequeEvent`."""

    direction = models.CharField(max_length=10, choices=ChequeDirection.choices)
    number = models.CharField(_("رقم الشيك"), max_length=40)
    bank_name = models.CharField(_("البنك المسحوب عليه"), max_length=120, blank=True)
    # issued: the clinic's bank account it is drawn on. received: the account it was deposited into.
    bank = models.ForeignKey(Treasury, null=True, blank=True, on_delete=models.PROTECT, related_name="cheques")
    due_date = models.DateField(_("تاريخ الاستحقاق"))
    amount = models.DecimalField(_("المبلغ"), max_digits=14, decimal_places=2, validators=[MinValueValidator(0.01)])
    # who it was written to / who gave it (a supplier, a payer), and the account the entry used on the other side
    party_type = models.CharField(max_length=10, blank=True)
    party_id = models.PositiveBigIntegerField(null=True, blank=True)
    party_name = models.CharField(_("الطرف"), max_length=150, blank=True)
    counter_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cost_center = models.ForeignKey(
        "ledger.CostCenter", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    endorsed_to_id = models.PositiveBigIntegerField(null=True, blank=True)  # the supplier a received cheque went to
    status = models.CharField(max_length=10, choices=ChequeStatus.choices)
    notes = models.CharField(_("ملاحظات"), max_length=300, blank=True)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("شيك")
        verbose_name_plural = _("الشيكات")
        ordering = ["due_date", "id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="cheque_amount_positive")]
        indexes = [models.Index(fields=["organization", "direction", "status", "due_date"])]

    def __str__(self):
        return f"{self.get_direction_display()} {self.number}"

    @property
    def is_open(self) -> bool:
        return self.status in CHEQUE_OPEN[self.direction]


class ChequeEvent(TenantScopedModel):
    """One step in a cheque's life, with the entry it posted (none for a step that moves no money)."""

    cheque = models.ForeignKey(Cheque, on_delete=models.PROTECT, related_name="events")
    from_status = models.CharField(max_length=10, blank=True)
    to_status = models.CharField(max_length=10, choices=ChequeStatus.choices)
    entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    undone = models.BooleanField(default=False)
    is_undo = models.BooleanField(default=False)
    notes = models.CharField(max_length=200, blank=True)
    by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]


# --- expenses that come back every month ------------------------------------------------------------------------


class ExpenseTemplate(TenantScopedModel):
    """Rent, salaries, the electricity bill: a payment voucher the clinic writes every month. The template only
    fills the voucher in; a person still presses the button, so nothing leaves a treasury by itself."""

    name = models.CharField(_("الاسم"), max_length=120)
    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, related_name="+", verbose_name=_("من خزينة"))
    counter_account = models.ForeignKey(
        "ledger.Account", on_delete=models.PROTECT, related_name="+", verbose_name=_("حساب المصروف")
    )
    cost_center = models.ForeignKey(
        "ledger.CostCenter", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=_("مركز التكلفة"),
    )  # fmt: skip
    amount = models.DecimalField(
        _("المبلغ المعتاد"), max_digits=14, decimal_places=2, validators=[MinValueValidator(0.01)]
    )
    day_of_month = models.PositiveSmallIntegerField(
        _("يوم الاستحقاق في الشهر"), default=1, validators=[MinValueValidator(1), MaxValueValidator(28)]
    )
    party_name = models.CharField(_("الطرف"), max_length=150, blank=True)
    is_active = models.BooleanField(_("مفعّل"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("مصروف متكرر")
        verbose_name_plural = _("المصروفات المتكررة")
        ordering = ["day_of_month", "id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_expense_template_name_per_org")
        ]

    def __str__(self):
        return self.name
