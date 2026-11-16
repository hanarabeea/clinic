from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import CenterSettings, EmergencyCase, Service, ServiceOrder


@admin.register(CenterSettings)
class CenterSettingsAdmin(SimpleHistoryAdmin):
    list_display = ("organization", "emergency_fee")


@admin.register(Service)
class ServiceAdmin(SimpleHistoryAdmin):
    list_display = ("name_ar", "organization", "category", "price", "doctor_share_percent", "is_active")
    list_filter = ("organization", "category", "is_active")
    search_fields = ("name_ar", "name_en")


@admin.register(ServiceOrder)
class ServiceOrderAdmin(SimpleHistoryAdmin):
    list_display = ("name", "organization", "appointment", "quantity", "unit_price", "doctor", "status", "ordered_at")
    list_filter = ("organization", "status")
    raw_id_fields = ("appointment", "service", "doctor", "ordered_by", "cancelled_by")


@admin.register(EmergencyCase)
class EmergencyCaseAdmin(SimpleHistoryAdmin):
    list_display = ("patient", "organization", "doctor", "triage", "reason", "arrived_at", "seen_at", "disposition")
    list_filter = ("organization", "triage", "reason", "disposition")
    raw_id_fields = ("appointment", "patient", "doctor", "registered_by", "disposition_by")
    date_hierarchy = "arrived_at"
