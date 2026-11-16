from django.urls import path

from . import views

app_name = "purchasing"

urlpatterns = [
    path("purchases/", views.invoices, name="invoices"),
    path("purchases/new/", views.invoice_new, name="invoice_new"),
    path("purchases/<int:pk>/", views.invoice_detail, name="invoice"),
    path("purchases/<int:pk>/scan/", views.invoice_scan, name="invoice_scan"),
    path("purchases/<int:pk>/cancel/", views.invoice_cancel, name="invoice_cancel"),
    path("purchases/<int:pk>/return/", views.return_new, name="return_new"),
    path("purchases/suppliers/", views.suppliers, name="suppliers"),
    path("purchases/suppliers/new/", views.supplier_edit, name="supplier_new"),
    path("purchases/suppliers/aging/", views.aging, name="aging"),
    path("purchases/suppliers/<int:pk>/", views.supplier_detail, name="supplier"),
    path("purchases/suppliers/<int:pk>/edit/", views.supplier_edit, name="supplier_edit"),
    path("purchases/payments/", views.payments, name="payments"),
    path("purchases/payments/new/", views.payment_new, name="payment_new"),
    path("purchases/payments/<int:pk>/", views.payment_detail, name="payment"),
    path("purchases/payments/<int:pk>/cancel/", views.payment_cancel, name="payment_cancel"),
    path("purchases/taxes/", views.taxes, name="taxes"),
    path("purchases/taxes/new/", views.tax_edit, name="tax_new"),
    path("purchases/taxes/<int:pk>/edit/", views.tax_edit, name="tax_edit"),
    path("purchases/taxes/settings/", views.settings, name="settings"),
]
