from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    PaymentAllocation,
    PurchaseInvoice,
    PurchaseLine,
    PurchaseReturn,
    PurchaseSettings,
    Supplier,
    SupplierPayment,
    TaxCode,
)


@admin.register(PurchaseSettings)
class PurchaseSettingsAdmin(SimpleHistoryAdmin):
    list_display = ("organization", "wht_threshold", "cheque_alert_days")


@admin.register(TaxCode)
class TaxCodeAdmin(SimpleHistoryAdmin):
    list_display = ("name", "organization", "kind", "rate", "recoverable", "is_active")
    list_filter = ("organization", "kind", "is_active")


@admin.register(Supplier)
class SupplierAdmin(SimpleHistoryAdmin):
    list_display = ("name", "organization", "phone", "tax_registration_number", "is_active")
    list_filter = ("organization", "is_active")
    search_fields = ("name",)


class _ReadOnly:
    """Posted documents: looked at here, never edited."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class LineInline(_ReadOnly, admin.TabularInline):
    model = PurchaseLine
    extra = 0
    fields = ("description", "account", "quantity", "unit_price", "discount", "net", "vat_amount", "total")
    readonly_fields = fields


@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(_ReadOnly, admin.ModelAdmin):
    list_display = ("label", "organization", "date", "supplier", "total", "status")
    list_filter = ("organization", "status")
    date_hierarchy = "date"
    inlines = [LineInline]


@admin.register(PurchaseReturn)
class PurchaseReturnAdmin(_ReadOnly, admin.ModelAdmin):
    list_display = ("label", "organization", "date", "invoice", "total")


class AllocationInline(_ReadOnly, admin.TabularInline):
    model = PaymentAllocation
    extra = 0
    fields = ("invoice", "amount")
    readonly_fields = fields


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(_ReadOnly, admin.ModelAdmin):
    list_display = ("label", "organization", "date", "supplier", "method", "amount", "wht_amount", "status")
    list_filter = ("organization", "method", "status")
    date_hierarchy = "date"
    inlines = [AllocationInline]
