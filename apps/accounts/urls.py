from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("password-reset/", views.PasswordResetView.as_view(), name="password_reset"),
    path("password-reset/sent/", views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("reset/done/", views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("users/", views.users_list, name="users"),
    path("users/add/", views.member_create, name="member_create"),
    path("users/<int:pk>/edit/", views.member_edit, name="member_edit"),
    path("users/<int:pk>/role/", views.membership_role, name="membership_role"),
    path("users/<int:pk>/toggle/", views.membership_toggle, name="membership_toggle"),
    path("users/<int:pk>/permissions/", views.membership_permissions, name="membership_permissions"),
    path("roles/", views.roles_list, name="roles"),
    path("roles/new/", views.role_edit, name="role_new"),
    path("roles/<int:pk>/", views.role_edit, name="role_edit"),
    path("roles/<int:pk>/delete/", views.role_delete, name="role_delete"),
]
