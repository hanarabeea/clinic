from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    Account,
    BooksCheck,
    CostCenter,
    FiscalPeriod,
    FiscalYear,
    JournalEntry,
    JournalLine,
    LedgerSettings,
)


@admin.register(LedgerSettings)
class LedgerSettingsAdmin(SimpleHistoryAdmin):
    list_display = ("organization", "fiscal_year_start_month")


@admin.register(Account)
class AccountAdmin(SimpleHistoryAdmin):
    list_display = ("code", "name_ar", "organization", "type", "is_group", "is_active", "system_key")
    list_filter = ("organization", "type", "is_group", "is_active")
    search_fields = ("code", "name_ar", "name_en")
    raw_id_fields = ("parent",)


@admin.register(CostCenter)
class CostCenterAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "organization", "is_active")
    list_filter = ("organization",)


@admin.register(FiscalYear)
class FiscalYearAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "start_date", "end_date", "status")
    list_filter = ("organization", "status")


@admin.register(FiscalPeriod)
class FiscalPeriodAdmin(SimpleHistoryAdmin):
    list_display = ("__str__", "organization", "fiscal_year", "number", "status", "closed_at")
    list_filter = ("organization", "status")


class ReadOnlyAdmin(admin.ModelAdmin):
    """The journal is written by ledger.services.post() only: the admin can look, never change."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0
    can_delete = False
    fields = ("line_no", "account", "debit", "credit", "cost_center", "party_type", "party_id", "memo")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(JournalEntry)
class JournalEntryAdmin(ReadOnlyAdmin):
    list_display = ("number", "date", "organization", "memo", "total", "status", "source_type", "source_id")
    list_filter = ("organization", "status", "source_type")
    search_fields = ("memo",)
    date_hierarchy = "date"
    inlines = [JournalLineInline]


@admin.register(JournalLine)
class JournalLineAdmin(ReadOnlyAdmin):
    list_display = ("entry", "line_no", "account", "debit", "credit", "cost_center", "party_type", "party_id")
    list_filter = ("organization",)
    raw_id_fields = ("entry", "account")


@admin.register(BooksCheck)
class BooksCheckAdmin(admin.ModelAdmin):
    list_display = ("ran_at", "organization", "ok", "seconds", "ran_by")
    list_filter = ("organization", "ok")
    readonly_fields = ("organization", "ran_at", "ok", "problems", "seconds", "ran_by")

    def has_add_permission(self, request):
        return False
