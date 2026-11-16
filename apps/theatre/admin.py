from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    Admission,
    ChecklistItem,
    Newborn,
    Operation,
    OperationConsumable,
    OperationNote,
    OperationTeamMember,
    ProcedureType,
    Room,
)


@admin.register(Room)
class RoomAdmin(SimpleHistoryAdmin):
    list_display = ("name", "organization", "kind", "beds", "nightly_rate", "is_active")
    list_filter = ("organization", "kind", "is_active")


@admin.register(ProcedureType)
class ProcedureTypeAdmin(SimpleHistoryAdmin):
    list_display = ("name_ar", "organization", "delivery", "duration_minutes", "package_price", "is_active")
    list_filter = ("organization", "delivery", "is_active")


class TeamInline(admin.TabularInline):
    model = OperationTeamMember
    extra = 0
    raw_id_fields = ("doctor",)


class ChecklistInline(admin.TabularInline):
    model = ChecklistItem
    extra = 0
    raw_id_fields = ("done_by",)


@admin.register(Operation)
class OperationAdmin(SimpleHistoryAdmin):
    list_display = ("procedure_name", "patient", "organization", "room", "surgeon", "planned_start", "status")
    list_filter = ("organization", "status", "room", "is_emergency")
    raw_id_fields = ("patient", "pregnancy", "procedure", "surgeon", "emergency_case", "postponed_to", "created_by")
    date_hierarchy = "planned_start"
    inlines = [TeamInline, ChecklistInline]


@admin.register(OperationNote)
class OperationNoteAdmin(SimpleHistoryAdmin):
    list_display = ("operation", "organization", "revision", "status", "signed_by", "signed_at")
    list_filter = ("organization", "status")
    raw_id_fields = ("operation", "signed_by")


@admin.register(Newborn)
class NewbornAdmin(SimpleHistoryAdmin):
    list_display = ("operation", "organization", "sex", "weight_g", "apgar_1", "apgar_5", "status", "is_removed")
    list_filter = ("organization", "status", "sex")
    raw_id_fields = ("operation",)


@admin.register(OperationConsumable)
class OperationConsumableAdmin(SimpleHistoryAdmin):
    list_display = ("name", "operation", "organization", "quantity", "unit")
    list_filter = ("organization",)
    raw_id_fields = ("operation", "added_by")


@admin.register(Admission)
class AdmissionAdmin(SimpleHistoryAdmin):
    list_display = ("patient", "organization", "room", "status", "admitted_at", "discharged_at", "nightly_rate")
    list_filter = ("organization", "status", "room")
    raw_id_fields = ("patient", "operation", "emergency_case", "doctor", "admitted_by", "discharged_by")
    date_hierarchy = "admitted_at"
