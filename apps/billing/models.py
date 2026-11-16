from django.conf import settings as dj_settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel


class PaymentMethod(models.TextChoices):
    CASH = "cash", _("كاش")
    CARD = "card", _("فيزا")
    INSTAPAY = "instapay", _("InstaPay")
    WALLET = "wallet", _("محفظة")


class PaymentKind(models.TextChoices):
    PAYMENT = "payment", _("تحصيل")
    DEPOSIT = "deposit", _("مقدم")


class Payment(TenantScopedModel):
    """Money a patient paid (or got back). For a visit: `appointment` is set and what is due is computed
    (billing.services.balance). On her account (operations, stays): no appointment. A **deposit** (مقدم) is money
    held for her until an invoice takes it: it sits in "دفعات مقدمة من المرضى", not on her receivable."""

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="payments")
    appointment = models.ForeignKey(
        "scheduling.Appointment", null=True, blank=True, on_delete=models.PROTECT, related_name="payments"
    )
    kind = models.CharField(max_length=10, choices=PaymentKind.choices, default=PaymentKind.PAYMENT)
    amount = models.DecimalField(_("المبلغ"), max_digits=10, decimal_places=2)
    method = models.CharField(
        _("طريقة الدفع"), max_length=10, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    received_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    received_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(_("ملاحظة"), max_length=200, blank=True)
    is_refund = models.BooleanField(_("استرداد"), default=False)
    # Where the money physically went (the reception drawer for cash, the card / InstaPay / wallet collection
    # accounts otherwise). Set when the payment is recorded; its journal entry debits that treasury's account.
    treasury = models.ForeignKey(
        "treasury.Treasury", null=True, blank=True, on_delete=models.PROTECT, related_name="payments"
    )

    class Meta:
        verbose_name = _("دفعة")
        verbose_name_plural = _("المدفوعات")
        ordering = ["received_at", "id"]
        indexes = [models.Index(fields=["organization", "received_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind="payment") | models.Q(appointment__isnull=True),
                name="payment_deposit_is_on_the_account",
            )
        ]

    def __str__(self):
        return f"{'-' if self.is_refund else ''}{self.amount} ({self.get_method_display()})"

    @property
    def signed_amount(self):
        return -self.amount if self.is_refund else self.amount
