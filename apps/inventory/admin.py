from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    InventorySettings,
    StockBalance,
    StockBatch,
    StockCount,
    StockCountLine,
    StockDoc,
    StockItem,
    StockMove,
    Warehouse,
)


@admin.register(InventorySettings)
class InventorySettingsAdmin(SimpleHistoryAdmin):
    list_display = ("organization", "expiry_alert_days")


@admin.register(Warehouse)
class WarehouseAdmin(SimpleHistoryAdmin):
    list_display = ("name", "organization", "is_active")


@admin.register(StockItem)
class StockItemAdmin(SimpleHistoryAdmin):
    list_display = ("name", "organization", "unit", "reorder_level", "track_expiry", "sale_price", "is_active")
    list_filter = ("organization", "is_active", "track_expiry")
    search_fields = ("name",)


class _ReadOnly:
    """Quantities and values change through store documents only: looked at here, never edited."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockBalance)
class StockBalanceAdmin(_ReadOnly, admin.ModelAdmin):
    list_display = ("item", "warehouse", "quantity", "value")
    list_filter = ("organization", "warehouse")


@admin.register(StockBatch)
class StockBatchAdmin(_ReadOnly, admin.ModelAdmin):
    list_display = ("item", "warehouse", "expiry_date", "lot", "quantity")


@admin.register(StockMove)
class StockMoveAdmin(_ReadOnly, admin.ModelAdmin):
    list_display = ("date", "kind", "item", "warehouse", "quantity", "value", "quantity_after")
    list_filter = ("organization", "kind", "warehouse")
    date_hierarchy = "date"


@admin.register(StockDoc)
class StockDocAdmin(_ReadOnly, admin.ModelAdmin):
    list_display = ("label", "organization", "kind", "date", "warehouse")
    list_filter = ("organization", "kind")


class CountLineInline(_ReadOnly, admin.TabularInline):
    model = StockCountLine
    extra = 0
    fields = ("item", "expected", "counted")
    readonly_fields = fields


@admin.register(StockCount)
class StockCountAdmin(_ReadOnly, SimpleHistoryAdmin):
    list_display = ("warehouse", "kind", "status", "counted_by", "approved_by", "started_at")
    list_filter = ("organization", "status", "kind")
    inlines = [CountLineInline]
