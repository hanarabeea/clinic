from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Charge, Invoice, InvoiceLine, InvoiceSettings, InvoiceShare, PatientCoverage, Payer


@admin.register(InvoiceSettings)
class InvoiceSettingsAdmin(SimpleHistoryAdmin):
    list_display = ("organization", "discount_limit_percent", "tax_registration_number")


@admin.register(Payer)
class PayerAdmin(SimpleHistoryAdmin):
    list_display = ("name", "organization", "kind", "default_percent", "is_active")
    list_filter = ("organization", "kind", "is_active")


@admin.register(PatientCoverage)
class PatientCoverageAdmin(SimpleHistoryAdmin):
    list_display = ("patient", "payer", "percent", "is_active")
    raw_id_fields = ("patient",)


@admin.register(Charge)
class ChargeAdmin(SimpleHistoryAdmin):
    list_display = ("description", "patient", "quantity", "unit_price", "is_cancelled")
    list_filter = ("organization", "kind", "is_cancelled")
    raw_id_fields = ("patient", "operation", "admission")


class _ReadOnly:
    """Invoices are documents of record: looked at here, never edited."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class LineInline(_ReadOnly, admin.TabularInline):
    model = InvoiceLine
    extra = 0
    fields = ("kind", "description", "quantity", "unit_price", "gross", "discount", "net", "is_live")
    readonly_fields = fields
    fk_name = "invoice"


@admin.register(Invoice)
class InvoiceAdmin(_ReadOnly, admin.ModelAdmin):
    list_display = ("label", "organization", "date", "patient", "total", "payer_share", "patient_share")
    list_filter = ("organization", "kind")
    date_hierarchy = "date"
    inlines = [LineInline]


@admin.register(InvoiceShare)
class InvoiceShareAdmin(_ReadOnly, admin.ModelAdmin):
    list_display = ("line", "doctor", "payee_name", "role", "amount")
