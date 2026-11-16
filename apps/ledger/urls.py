from django.urls import path

from . import views

app_name = "ledger"

urlpatterns = [
    path("books/", views.trial_balance, name="trial_balance"),
    path("books/journal/", views.journal, name="journal"),
    path("books/journal/new/", views.entry_new, name="entry_new"),
    path("books/journal/<int:pk>/", views.entry_detail, name="entry"),
    path("books/journal/<int:pk>/reverse/", views.entry_reverse, name="entry_reverse"),
    path("books/accounts/", views.accounts, name="accounts"),
    path("books/accounts/add/", views.account_add, name="account_add"),
    path("books/accounts/<int:pk>/", views.account_statement, name="statement"),
    path("books/accounts/<int:pk>/edit/", views.account_edit, name="account_edit"),
    path("books/income/", views.income_statement, name="income_statement"),
    path("books/balance-sheet/", views.balance_sheet, name="balance_sheet"),
    path("books/verify/", views.verify_books, name="verify"),
    path("books/years/<int:pk>/", views.year_close, name="year_close"),
    path("books/periods/", views.periods, name="periods"),
    path("books/periods/<int:pk>/toggle/", views.period_toggle, name="period_toggle"),
]
