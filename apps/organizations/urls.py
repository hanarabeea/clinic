from django.urls import path

from . import views

app_name = "organizations"

urlpatterns = [
    path("settings/", views.settings_company, name="settings"),
    path("logo/", views.logo, name="logo"),
    path("brand-logo/", views.public_logo, name="public_logo"),
]
