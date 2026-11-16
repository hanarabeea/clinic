from django.urls import path

from . import views

app_name = "treasury"

urlpatterns = [
    path("treasury/", views.home, name="home"),
    path("treasury/add/", views.treasury_edit, name="add"),
    path("treasury/<int:pk>/", views.movement, name="movement"),
    path("treasury/<int:pk>/edit/", views.treasury_edit, name="edit"),
    path("treasury/<int:pk>/count/", views.surprise_count, name="surprise_count"),
    path("treasury/vouchers/", views.vouchers, name="vouchers"),
    path("treasury/vouchers/new/", views.voucher_new, name="voucher_new"),
    path("treasury/vouchers/<int:pk>/", views.voucher_detail, name="voucher"),
    path("treasury/vouchers/<int:pk>/cancel/", views.voucher_cancel, name="voucher_cancel"),
    path("treasury/cheques/", views.cheque_list, name="cheques"),
    path("treasury/cheques/new/", views.cheque_new, name="cheque_new"),
    path("treasury/cheques/<int:pk>/", views.cheque_detail, name="cheque"),
    path("treasury/cheques/<int:pk>/move/", views.cheque_move, name="cheque_move"),
    path("treasury/cheques/<int:pk>/undo/", views.cheque_undo, name="cheque_undo"),
    path("treasury/expenses/", views.expenses, name="expenses"),
    path("treasury/expenses/new/", views.expense_edit, name="expense_new"),
    path("treasury/expenses/<int:pk>/edit/", views.expense_edit, name="expense_edit"),
    path("treasury/expenses/<int:pk>/record/", views.expense_record, name="expense_record"),
    path("treasury/counts/", views.counts, name="counts"),
    path("treasury/counts/<int:pk>/approve/", views.approve_variance, name="approve_variance"),
]
