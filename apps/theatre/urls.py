from django.urls import path

from . import views

app_name = "theatre"

urlpatterns = [
    path("theatre/", views.board, name="board"),
    path("theatre/patients/", views.patient_search, name="patient_search"),
    path("theatre/operations/new/", views.operation_new, name="operation_new"),
    path("theatre/operations/<int:pk>/", views.operation, name="operation"),
    path("theatre/operations/<int:pk>/move/", views.operation_move, name="operation_move"),
    path("theatre/operations/<int:pk>/step/<str:action>/", views.operation_step, name="operation_step"),
    path("theatre/operations/<int:pk>/close/<str:how>/", views.operation_close, name="operation_close"),
    path("theatre/operations/<int:pk>/team/", views.team_save, name="team_save"),
    path("theatre/operations/<int:pk>/consumables/", views.consumable_add, name="consumable_add"),
    path("theatre/operations/<int:pk>/consumables/kit/", views.consumable_kit, name="consumable_kit"),
    path("theatre/operations/<int:pk>/newborns/", views.newborn_add, name="newborn_add"),
    path("theatre/operations/<int:pk>/note/", views.note_save, name="note_save"),
    path("theatre/operations/<int:pk>/note/revise/", views.note_revise, name="note_revise"),
    path("theatre/checklist/<int:pk>/toggle/", views.checklist_toggle, name="checklist_toggle"),
    path("theatre/team/<int:pk>/remove/", views.team_remove, name="team_remove"),
    path("theatre/consumables/<int:pk>/remove/", views.consumable_remove, name="consumable_remove"),
    path("theatre/newborns/<int:pk>/remove/", views.newborn_remove, name="newborn_remove"),
    path("theatre/admissions/", views.admissions, name="admissions"),
    path("theatre/admissions/new/", views.admit, name="admit"),
    path("theatre/admissions/patients/", views.admit_search, name="admit_search"),
    path("theatre/admissions/<int:pk>/discharge/", views.discharge, name="discharge"),
    path("theatre/admissions/<int:pk>/summary.pdf", views.discharge_pdf, name="discharge_pdf"),
    path("org/settings/theatre/", views.setup, name="setup"),
    path("org/settings/theatre/rooms/add/", views.room_edit, name="room_add"),
    path("org/settings/theatre/rooms/<int:pk>/", views.room_edit, name="room_edit"),
    path("org/settings/theatre/procedures/add/", views.procedure_edit, name="procedure_add"),
    path("org/settings/theatre/procedures/<int:pk>/", views.procedure_edit, name="procedure_edit"),
]
