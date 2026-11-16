from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import AntenatalVisit, Milestone, MilestoneCheck, ObstetricProfile, Pregnancy, PregnancyHistory


@admin.register(ObstetricProfile)
class ObstetricProfileAdmin(SimpleHistoryAdmin):
    list_display = ("patient", "organization", "blood_group", "cycle_days")
    list_filter = ("organization", "blood_group")
    raw_id_fields = ("patient",)


@admin.register(Pregnancy)
class PregnancyAdmin(SimpleHistoryAdmin):
    list_display = ("patient", "organization", "status", "edd", "edd_method", "fetuses", "risk_level")
    list_filter = ("organization", "status", "edd_method", "risk_level")
    raw_id_fields = ("patient", "doctor", "created_by")
    date_hierarchy = "edd"


@admin.register(PregnancyHistory)
class PregnancyHistoryAdmin(SimpleHistoryAdmin):
    list_display = ("patient", "organization", "year", "outcome", "weeks", "living_children", "is_removed")
    list_filter = ("organization", "outcome", "is_removed")
    raw_id_fields = ("patient", "pregnancy", "recorded_by")


@admin.register(AntenatalVisit)
class AntenatalVisitAdmin(SimpleHistoryAdmin):
    list_display = ("visit", "organization", "pregnancy", "ga_days", "fundal_height_cm", "fetal_heart_rate")
    list_filter = ("organization",)
    raw_id_fields = ("visit", "pregnancy")


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "kind", "from_week", "to_week", "rh_negative_only", "is_active")
    list_filter = ("organization", "kind", "is_active")


@admin.register(MilestoneCheck)
class MilestoneCheckAdmin(SimpleHistoryAdmin):
    list_display = ("pregnancy", "milestone", "done_on", "recorded_by")
    raw_id_fields = ("pregnancy", "milestone", "recorded_by")
