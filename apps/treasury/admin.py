from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import CashCount, Cheque, ExpenseTemplate, Shift, Treasury, Voucher


@admin.register(Treasury)
class TreasuryAdmin(SimpleHistoryAdmin):
    list_display = ("name", "organization", "kind", "account", "receives_method", "is_active")
    list_filter = ("organization", "kind", "is_active")
    raw_id_fields = ("account",)


@admin.register(Voucher)
class VoucherAdmin(SimpleHistoryAdmin):
    list_display = ("__str__", "organization", "date", "treasury", "amount", "status", "created_by")
    list_filter = ("organization", "kind", "status")
    raw_id_fields = ("treasury", "to_treasury", "counter_account", "cost_center", "created_by", "cancelled_by")
    date_hierarchy = "date"


@admin.register(Shift)
class ShiftAdmin(SimpleHistoryAdmin):
    list_display = ("treasury", "organization", "status", "opened_by", "opened_at", "closed_by", "closed_at")
    list_filter = ("organization", "status")
    raw_id_fields = ("treasury", "opened_by", "closed_by")


@admin.register(CashCount)
class CashCountAdmin(SimpleHistoryAdmin):
    list_display = (
        "treasury",
        "organization",
        "kind",
        "counted_at",
        "counted_total",
        "expected_total",
        "variance_status",
    )
    list_filter = ("organization", "kind", "variance_status")
    raw_id_fields = ("treasury", "shift", "counted_by", "approved_by")


@admin.register(Cheque)
class ChequeAdmin(SimpleHistoryAdmin):
    list_display = ("number", "organization", "direction", "status", "due_date", "amount", "party_name")
    list_filter = ("organization", "direction", "status")
    raw_id_fields = ("bank", "counter_account", "cost_center", "created_by")
    date_hierarchy = "due_date"


@admin.register(ExpenseTemplate)
class ExpenseTemplateAdmin(SimpleHistoryAdmin):
    list_display = ("name", "organization", "treasury", "amount", "day_of_month", "is_active")
    list_filter = ("organization", "is_active")
    raw_id_fields = ("treasury", "counter_account", "cost_center")
