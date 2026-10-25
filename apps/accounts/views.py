from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.organizations.models import ClinicRole, Membership, Role

from . import roles
from .forms import ClinicRoleForm, EmailAuthenticationForm, RoleForm, UserCreateForm, UserDetailsForm
from .models import User
from .permissions import (
    IMPLIES,
    PERMISSION_GROUPS,
    PERMISSION_LABELS,
    base_permissions,
    effective_permissions,
    require_perm,
    with_implied,
)


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and request.method == "GET":
            # already signed in: say so, instead of silently landing on that account's home page
            messages.info(
                request,
                _("إنت داخل بالفعل بحساب %(name)s — اعمل «تسجيل الخروج» الأول لو عايز تدخل بحساب تاني.")
                % {"name": request.user.username},
            )
        return super().dispatch(request, *args, **kwargs)


class LogoutView(auth_views.LogoutView):
    pass


class PasswordResetView(auth_views.PasswordResetView):
    template_name = "accounts/password_reset.html"
    email_template_name = "accounts/emails/password_reset.txt"
    subject_template_name = "accounts/emails/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")


class PasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"


def _can_edit(request, m) -> bool:
    """Never yourself. The owner manages everyone else; anyone else with "user.manage" only members whose permissions
    they all have themselves (never an owner). What they can *give* is limited too (roles.can_give)."""
    editor = request.membership
    if m.user_id == request.user.id:
        return False
    if editor.role == Role.OWNER:
        return True
    return m.role != Role.OWNER and effective_permissions(m) <= effective_permissions(editor)


def _users_ctx(request, form=None):
    org = request.organization
    roles.ensure_defaults(org)
    choices = roles.choices_for(request.membership, org)
    allowed = {value for value, _label in choices}
    memberships = list(
        Membership.objects.filter(organization=org)
        .select_related("user", "clinic_role")
        .order_by("-is_active", "user__full_name")
    )
    for m in memberships:
        m.can_edit = _can_edit(request, m)
        m.role_value = roles.value_of(m)
        m.role_editable = m.role_value in allowed
    return {
        "memberships": memberships,
        "form": form or UserCreateForm(organization=org, choices=choices),
        "role_choices": choices,
    }


@require_perm("user.manage")
def users_list(request):
    return render(request, "accounts/users.html", _users_ctx(request))


@require_POST
@require_perm("user.manage")
def member_create(request):
    """Add an employee directly: they can sign in right away with the username (or the email, if given)."""
    org = request.organization
    roles.ensure_defaults(org)
    form = UserCreateForm(request.POST, organization=org, choices=roles.choices_for(request.membership, org))
    if not form.is_valid():
        return render(request, "accounts/users.html", _users_ctx(request, form), status=400)
    d = form.cleaned_data
    role, clinic_role = roles.resolve(org, d["role"])
    with transaction.atomic():
        user = User.objects.create_user(
            username=d["username"], email=d["email"], password=d["password1"], full_name=d["full_name"]
        )
        Membership.objects.create(user=user, organization=org, role=role, clinic_role=clinic_role)
    audit.log(
        "user.created",
        request=request,
        target=user,
        summary=f"{user.full_name} ({user.username}) — {clinic_role or role.label}",
    )
    messages.success(
        request,
        _("اتضاف %(name)s. يقدر يدخل دلوقتي باسم المستخدم «%(username)s» وكلمة السر.")
        % {"name": user.full_name, "username": user.username},
    )
    return redirect("accounts:users")


def _editable_membership(request, pk):
    m = get_object_or_404(
        Membership.objects.filter(organization=request.organization).select_related("clinic_role"), pk=pk
    )
    if not _can_edit(request, m):
        raise PermissionDenied
    return m


@require_perm("user.manage")
def member_edit(request, pk):
    """Name, username, email and (optionally) a new password for an employee — how a forgotten password is reset for
    someone without an email."""
    m = _editable_membership(request, pk)
    user = m.user
    form = UserDetailsForm(request.POST or None, user=user)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        changed = [f for f in ("full_name", "username", "email") if getattr(user, f) != d[f]]
        user.full_name, user.username, user.email = d["full_name"], d["username"], d["email"]
        if d["password1"]:
            user.set_password(d["password1"])
            changed.append("password")
        user.save()
        audit.log("user.edited", request=request, target=user, summary=f"{user.username}: {', '.join(changed) or '—'}")
        messages.success(
            request, _("البيانات اتحفظت.") if "password" not in changed else _("البيانات وكلمة السر اتحفظوا.")
        )
        return redirect("accounts:users")
    return render(request, "accounts/member_edit.html", {"member": m, "form": form}, status=400 if form.errors else 200)


@require_POST
@require_perm("user.manage")
def membership_role(request, pk):
    m = _editable_membership(request, pk)
    form = RoleForm(request.POST, choices=roles.choices_for(request.membership, request.organization))
    if form.is_valid():
        m.role, m.clinic_role = roles.resolve(request.organization, form.cleaned_data["role"])
        # a new role starts from that role's permissions: what was added / taken away for the old role is dropped
        m.granted_permissions, m.revoked_permissions = [], []
        m.save(update_fields=["role", "clinic_role", "granted_permissions", "revoked_permissions", "updated_at"])
        audit.log("user.role", request=request, summary=f"{m.user.username} → {m.role_name}")
        messages.success(request, _("الدور اتغير."))
    else:
        messages.error(request, _("الدور ده مش مسموح."))
    return redirect("accounts:users")


def _perm_groups(*, checked, from_role, editable):
    """The grouped checkboxes shared by the employee and role screens."""
    return [
        (
            title,
            [
                {
                    "perm": p,
                    "label": label,
                    "help": help_text,
                    "checked": p in checked,
                    "from_role": p in from_role,
                    "editable": p in editable,
                    "implies": " ".join(sorted(IMPLIES.get(p, ()))),
                }
                for p, label, help_text in items
            ],
        )
        for title, items in PERMISSION_GROUPS
    ]


def _wanted(request, *, mine, current):
    """Checked boxes → permissions, where a permission the editor doesn't have keeps its current state."""
    checked = set(request.POST.getlist("perms"))
    return with_implied({p for p in mine if p in checked} | {p for p in current if p not in mine})


def _change_summary(before, after):
    return "، ".join(
        [f"+{PERMISSION_LABELS[p]}" for p in sorted(after - before)]
        + [f"−{PERMISSION_LABELS[p]}" for p in sorted(before - after)]
    )


@require_perm("user.manage")
def membership_permissions(request, pk):
    """One employee's permissions: the role is the starting set, and each permission can be given or taken away for
    this person only. Whoever edits can only give permissions they have themselves."""
    m = _editable_membership(request, pk)
    if m.role == Role.OWNER:
        raise PermissionDenied  # the owner always has everything
    mine = effective_permissions(request.membership)
    current = effective_permissions(m)
    base = base_permissions(m)
    if request.method == "POST":
        if request.POST.get("action") == "reset":
            m.granted_permissions, m.revoked_permissions = [], []
            summary = "رجعت لصلاحيات الدور"
        else:
            wanted = _wanted(request, mine=mine, current=current)
            m.granted_permissions = sorted(wanted - base)
            m.revoked_permissions = sorted(base - wanted)
            summary = _change_summary(current, wanted)
        m.save(update_fields=["granted_permissions", "revoked_permissions", "updated_at"])
        audit.log("user.permissions", request=request, summary=f"{m.user.username}: {summary or 'من غير تغيير'}")
        messages.success(request, _("الصلاحيات اتحفظت."))
        return redirect("accounts:membership_permissions", pk=m.pk)
    return render(
        request,
        "accounts/permissions.html",
        {
            "member": m,
            "groups": _perm_groups(checked=current, from_role=base, editable=mine),
            "customised": bool(m.granted_permissions or m.revoked_permissions),
        },
    )


@require_POST
@require_perm("user.manage")
def membership_toggle(request, pk):
    m = _editable_membership(request, pk)
    m.is_active = not m.is_active
    m.save(update_fields=["is_active", "updated_at"])
    audit.log("user.toggled", request=request, summary=f"{m.user.username}: {'مفعّل' if m.is_active else 'موقوف'}")
    messages.success(request, _("الحساب اتفعّل.") if m.is_active else _("الحساب اتوقف."))
    return redirect("accounts:users")


# --- the clinic's own roles (apps/accounts/roles.py) ---------------------------------------------------------------


@require_perm("user.manage")
def roles_list(request):
    org = request.organization
    roles.ensure_defaults(org)
    items = list(ClinicRole.objects.filter(organization=org).annotate(n_members=Count("members")))
    for r in items:
        r.perm_count = len(roles.role_perms(r))
        r.can_edit = roles.can_give(request.membership, roles.role_perms(r))
    return render(request, "accounts/roles.html", {"roles": items})


@require_perm("user.manage")
def role_edit(request, pk=None):
    """New role (optionally a copy of another: ?from=<pk>) or edit one. Only within the editor's own permissions: a
    role holding permissions they don't have can't be edited by them at all."""
    org = request.organization
    roles.ensure_defaults(org)
    mine = effective_permissions(request.membership)
    if pk:
        role = get_object_or_404(ClinicRole.objects.filter(organization=org), pk=pk)
        current = roles.role_perms(role)
        if not roles.can_give(request.membership, current):
            raise PermissionDenied
    else:
        role, current = ClinicRole(organization=org), frozenset()
        source_pk = request.GET.get("from", "")
        if source_pk.isdigit():
            source = get_object_or_404(ClinicRole.objects.filter(organization=org), pk=source_pk)
            current = roles.role_perms(source) & mine
            role.description = source.description
    form = ClinicRoleForm(request.POST or None, instance=role)
    if request.method == "POST" and form.is_valid():
        wanted = _wanted(request, mine=mine, current=current)
        roles.save_role(
            role, name=form.cleaned_data["name"], description=form.cleaned_data["description"], perms=wanted
        )
        audit.log(
            "role.updated" if pk else "role.created",
            request=request,
            summary=f"{role.name}: {_change_summary(current, wanted) or '—'}",
        )
        messages.success(request, _("الدور اتحفظ."))
        return redirect("accounts:roles")
    if request.method == "POST":  # invalid name: keep what was ticked
        current = _wanted(request, mine=mine, current=current)
    return render(
        request,
        "accounts/role_edit.html",
        {
            "role": role,
            "form": form,
            "groups": _perm_groups(checked=current, from_role=frozenset(), editable=mine),
            "members": list(Membership.objects.filter(clinic_role=role).select_related("user")) if pk else [],
        },
        status=400 if request.method == "POST" else 200,
    )


@require_POST
@require_perm("user.manage")
def role_delete(request, pk):
    role = get_object_or_404(ClinicRole.objects.filter(organization=request.organization), pk=pk)
    if not roles.can_give(request.membership, roles.role_perms(role)):
        raise PermissionDenied
    reason = roles.in_use(role)
    if reason:
        messages.error(request, reason)
        return redirect("accounts:role_edit", pk=role.pk)
    name = role.name
    role.delete()
    audit.log("role.deleted", request=request, summary=name)
    messages.success(request, _("الدور اتمسح."))
    return redirect("accounts:roles")
