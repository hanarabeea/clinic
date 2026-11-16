from django.urls import path

from . import views

app_name = "invoicing"

urlpatterns = [
    path("invoices/", views.invoice_list, name="list"),
    path("invoices/<int:pk>/", views.invoice_detail, name="invoice"),
    path("invoices/<int:pk>/print.pdf", views.invoice_pdf, name="invoice_pdf"),
    path("invoices/<int:pk>/credit/", views.invoice_credit, name="invoice_credit"),
    path("invoices/payments/<int:pk>/receipt.pdf", views.receipt_pdf, name="receipt_pdf"),
    path("invoices/accounts/", views.accounts, name="accounts"),
    path("invoices/patients/<int:pk>/", views.account, name="account"),
    path("invoices/patients/<int:pk>/issue/", views.account_issue, name="account_issue"),
    path("invoices/patients/<int:pk>/pay/", views.account_pay, name="account_pay"),
    path("invoices/patients/<int:pk>/charge/", views.charge_add, name="charge_add"),
    path("invoices/patients/<int:pk>/coverage/", views.coverage, name="coverage"),
    path("invoices/charges/<int:pk>/cancel/", views.charge_cancel, name="charge_cancel"),
    path("invoices/doctors/", views.doctors, name="doctors"),
    path("invoices/doctors/<int:pk>/", views.doctor, name="doctor"),
    path("invoices/doctors/<int:pk>/pay/", views.doctor_pay, name="doctor_pay"),
    path("invoices/payers/", views.payers, name="payers"),
    path("invoices/payers/new/", views.payer_edit, name="payer_new"),
    path("invoices/payers/<int:pk>/", views.payer, name="payer"),
    path("invoices/payers/<int:pk>/edit/", views.payer_edit, name="payer_edit"),
    path("invoices/payers/<int:pk>/receive/", views.payer_receive, name="payer_receive"),
    path("org/settings/invoices/", views.settings, name="settings"),
    path("reports/revenue/", views.report_revenue, name="report_revenue"),
    path("reports/operations/", views.report_operations, name="report_operations"),
    path("reports/receivables/", views.report_receivables, name="report_receivables"),
    path("reports/cash/", views.report_cash, name="report_cash"),
]
