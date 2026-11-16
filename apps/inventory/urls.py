from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("stock/", views.items, name="items"),
    path("stock/items/new/", views.item_edit, name="item_new"),
    path("stock/items/<int:pk>/", views.item_detail, name="item"),
    path("stock/items/<int:pk>/edit/", views.item_edit, name="item_edit"),
    path("stock/docs/", views.docs, name="docs"),
    path("stock/docs/issue/", views.issue_new, name="issue_new"),
    path("stock/docs/transfer/", views.transfer_new, name="transfer_new"),
    path("stock/docs/opening/", views.opening_new, name="opening_new"),
    path("stock/counts/", views.counts, name="counts"),
    path("stock/counts/start/", views.count_start, name="count_start"),
    path("stock/counts/<int:pk>/", views.count_detail, name="count"),
    path("stock/counts/<int:pk>/save/", views.count_save, name="count_save"),
    path("stock/counts/<int:pk>/approve/", views.count_approve, name="count_approve"),
    path("stock/counts/<int:pk>/cancel/", views.count_cancel, name="count_cancel"),
    path("stock/setup/", views.setup, name="setup"),
    path("stock/setup/stores/new/", views.warehouse_edit, name="warehouse_new"),
    path("stock/setup/stores/<int:pk>/", views.warehouse_edit, name="warehouse_edit"),
]
