from django.urls import path

from . import views

app_name = "obstetrics"

urlpatterns = [
    path("desk/patients/<int:pk>/pregnancy/", views.panel, name="panel"),
    path("desk/patients/<int:pk>/pregnancy/tag/", views.tag, name="tag"),
    path("desk/patients/<int:pk>/pregnancy/band/", views.band, name="band"),
    path("desk/patients/<int:pk>/pregnancy/start/", views.pregnancy_start, name="start"),
    path("desk/patients/<int:pk>/pregnancy/history/add/", views.history_add, name="history_add"),
    path("desk/patients/<int:pk>/pregnancy/profile/", views.profile_save, name="profile"),
    path("desk/pregnancies/<int:pk>/dating/", views.pregnancy_dating, name="dating"),
    path("desk/pregnancies/<int:pk>/details/", views.pregnancy_details, name="details"),
    path("desk/pregnancies/<int:pk>/close/", views.pregnancy_close, name="close"),
    path("desk/pregnancies/<int:pk>/transfer/", views.pregnancy_transfer, name="transfer"),
    path("desk/pregnancies/<int:pk>/reopen/", views.pregnancy_reopen, name="reopen"),
    path("desk/pregnancies/<int:pk>/milestones/<int:milestone_pk>/", views.milestone_toggle, name="milestone"),
    path("desk/pregnancy-history/<int:pk>/remove/", views.history_remove, name="history_remove"),
    path("desk/visits/<int:pk>/antenatal/", views.antenatal, name="antenatal"),
    path("desk/visits/<int:pk>/antenatal/field/", views.antenatal_field, name="antenatal_field"),
]
