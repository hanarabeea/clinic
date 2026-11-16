from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("cashbox/", views.cashbox, name="cashbox"),
    path("cashbox/close-shift/", views.shift_close, name="shift_close"),
]
