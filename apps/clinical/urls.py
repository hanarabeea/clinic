from django.urls import path

from . import views

app_name = "clinical"

urlpatterns = [
    path("desk/", views.desk, name="desk"),
    path("desk/search/", views.desk_search, name="search"),
    path("desk/lock/", views.desk_lock, name="lock"),
    path("desk/unlock/", views.desk_unlock, name="unlock"),
    path("desk/call/<int:appt_pk>/", views.desk_call, name="call"),
    path("desk/visits/<int:pk>/", views.visit_view, name="visit"),
    path("desk/visits/<int:pk>/field/", views.visit_field, name="visit_field"),
    path("desk/visits/<int:pk>/finish/", views.visit_finish, name="visit_finish"),
    path("desk/visits/<int:pk>/compare/<int:other_pk>/", views.visit_compare, name="visit_compare"),
    path("desk/visits/<int:pk>/reuse/", views.visit_reuse, name="visit_reuse"),
    path("desk/visits/<int:pk>/templates/<int:tpl_pk>/apply/", views.visit_template_apply, name="visit_template_apply"),
    path("desk/visits/<int:pk>/templates/save/", views.visit_template_save, name="visit_template_save"),
    path(
        "desk/visits/<int:pk>/templates/<int:tpl_pk>/remove/", views.visit_template_remove, name="visit_template_remove"
    ),
    path("desk/visits/<int:pk>/documents/new/", views.document_new, name="document_new"),
    path("desk/visits/<int:pk>/documents/issue/", views.document_issue, name="document_issue"),
    path("desk/patients/<int:pk>/documents/", views.patient_documents, name="documents"),
    path("desk/documents/<int:pk>.pdf", views.document_pdf, name="document_pdf"),
    path("desk/documents/<int:pk>/void/", views.document_void, name="document_void"),
    path("org/settings/documents/", views.document_templates, name="document_templates"),
    path("desk/poll/", views.desk_poll, name="poll"),
    path("desk/emergency-alert/", views.emergency_alert, name="emergency_alert"),
    path("desk/patients/<int:pk>/", views.patient_record, name="record"),
    path("desk/patients/<int:pk>/history/", views.patient_history, name="history"),
    path("desk/patients/<int:pk>/attachments/", views.patient_attachments, name="attachments"),
    path("desk/patients/<int:pk>/data/", views.patient_data, name="data"),
    path("desk/patients/<int:pk>/export.pdf", views.patient_export, name="export"),
    path("patients/<int:pk>/attachments/upload/", views.attachment_upload, name="attachment_upload"),
    path("attachments/<int:pk>/", views.attachment_file, name="attachment_file"),
]
