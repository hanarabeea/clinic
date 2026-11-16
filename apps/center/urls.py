from django.urls import path

from . import views

app_name = "center"

urlpatterns = [
    path("org/settings/services/", views.services_settings, name="services"),
    path("org/settings/services/<int:pk>/", views.service_edit, name="service_edit"),
    path("org/settings/services/<int:pk>/toggle/", views.service_toggle, name="service_toggle"),
    path("appointments/<int:appt_pk>/services/", views.orders, name="orders"),
    path("appointments/<int:appt_pk>/services/add/", views.order_add, name="order_add"),
    path("service-orders/<int:pk>/cancel/", views.order_cancel, name="order_cancel"),
    path("emergency/", views.emergencies, name="emergencies"),
    path("emergency/register/", views.emergency_register, name="emergency_register"),
    path("emergency/search/", views.emergency_search, name="emergency_search"),
    path("emergency/<int:pk>/disposition/", views.emergency_disposition, name="emergency_disposition"),
    path("emergency/<int:pk>/cancel/", views.emergency_cancel, name="emergency_cancel"),
    path("emergency/<int:pk>/band/", views.emergency_band, name="emergency_band"),
]
