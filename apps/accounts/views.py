from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth import views as auth_views
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.organizations.models import ClinicRole, Membership, Role

from . import roles
from .forms import AcceptInviteForm, ClinicRoleForm, EmailAuthenticationForm, InviteForm, RoleForm
from .models import Invitation, User
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
                _("إنت داخل بالفعل بحساب %(email)s — اعمل «تسجيل الخروج» الأول لو عايز تدخل بحساب تاني.")
                % {"email": request.user.email},
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


def _send_invite_email(request, invitation, link):
    body = render_to_string(
        "accounts/emails/invitation.txt",
        {"invitation": invitation, "link": link, "org": invitation.organization},
    )
    send_mail(_("دعوة للانضمام إلى DigiClinic"), body, None, [invitation.email])


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
    invitations = list(Invitation.objects.for_org(org).pending().select_related("clinic_role").order_by("-created_at"))
    for inv in invitations:
        inv.can_revoke = roles.value_of(inv) in allowed
    return {
        "memberships": memberships,
        "invitations": invitations,
        "form": form or InviteForm(organization=org, choices=choices),
        "role_choices": choices,
    }


@require_perm("user.manage")
def users_list(request):
    ctx = _users_ctx(request)
    ctx["new_link"] = request.session.pop("new_invite_link", None)
    return render(request, "accounts/users.html", ctx)


@require_POST
@require_perm("user.manage")
def invite_create(request):
    org = request.organization
    roles.ensure_defaults(org)
    form = InviteForm(request.POST, organization=org, choices=roles.choices_for(request.membership, org))
    if not form.is_valid():
        return render(request, "accounts/users.html", _users_ctx(request, form), status=400)
    role, clinic_role = roles.resolve(org, form.cleaned_data["role"])
    inv, token = Invitation.create_for(
        organization=org,
        email=form.cleaned_data["email"],
        full_name=form.cleaned_data["full_name"],
        role=role,
        clinic_role=clinic_role,
        invited_by=request.user,
    )
    link = request.build_absolute_uri(reverse("accounts:accept_invite", args=[token]))
    _send_invite_email(request, inv, link)
    request.session["new_invite_link"] = link
    audit.log("user.invited", request=request, summary=f"{inv.full_name} <{inv.email}> — {inv.role_name}")
    messages.success(request, _("اتعملت الدعوة. ابعت الرابط للمستخدم، وصالح لمدة 48 ساعة."))
    return redirect("accounts:users")


@require_POST
@require_perm("user.manage")
def invite_revoke(request, pk):
    org = request.organization
    inv = get_object_or_404(Invitation.objects.for_org(org).pending(), pk=pk)
    if roles.value_of(inv) not in {v for v, _l in roles.choices_for(request.membership, org)}:
        raise PermissionDenied
    inv.revoked_at = timezone.now()
    inv.save(update_fields=["revoked_at", "updated_at"])
    audit.log("user.invite_revoked", request=request, summary=inv.email)
    messages.success(request, _("الدعوة اتلغت."))
    return redirect("accounts:users")


def _editable_membership(request, pk):
    m = get_object_or_404(
        Membership.objects.filter(organization=request.organization).select_related("clinic_role"), pk=pk
    )
    if not _can_edit(request, m):
        raise PermissionDenied
    return m


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
        audit.log("user.role", request=request, summary=f"{m.user.email} → {m.role_name}")
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
        audit.log("user.permissions", request=request, summary=f"{m.user.email}: {summary or 'من غير تغيير'}")
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
    audit.log("user.toggled", request=request, summary=f"{m.user.email}: {'مفعّل' if m.is_active else 'موقوف'}")
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


def accept_invite(request, token):
    inv = Invitation.from_token(token)
    if inv is None:
        return render(request, "accounts/invite_invalid.html", status=404)
    if request.user.is_authenticated and request.user.email.lower() != inv.email:
        # someone else is signed in in this browser (often the owner opening the link to try it): sign them out,
        # or the new employee would end up looking at the owner's account
        other = request.user.email
        logout(request)
        messages.info(request, _("اتعمل خروج من حساب %(email)s عشان تكمل الدعوة.") % {"email": other})

    existing = User.objects.filter(email=inv.email).first()
    if existing is not None:
        if request.method == "POST":
            with transaction.atomic():
                Membership.objects.update_or_create(
                    user=existing,
                    organization=inv.organization,
                    defaults={"role": inv.role, "clinic_role": inv.clinic_role, "is_active": True},
                )
                inv.accepted_at = timezone.now()
                inv.save(update_fields=["accepted_at", "updated_at"])
            if request.user.is_authenticated:  # it's them
                messages.success(request, _("اتضافت للعيادة."))
                return redirect("dashboard:home")
            messages.success(request, _("اتضافت للعيادة. ادخل بحسابك."))
            return redirect("accounts:login")
        return render(request, "accounts/accept_invite.html", {"invitation": inv, "existing": True})

    form = AcceptInviteForm(request.POST or None, email=inv.email, initial={"full_name": inv.full_name})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = User.objects.create_user(
                email=inv.email, password=form.cleaned_data["password1"], full_name=form.cleaned_data["full_name"]
            )
            Membership.objects.create(
                user=user, organization=inv.organization, role=inv.role, clinic_role=inv.clinic_role
            )
            audit.log("user.joined", request=request, org=inv.organization, actor=user, summary=user.email)
            inv.accepted_at = timezone.now()
            inv.save(update_fields=["accepted_at", "updated_at"])
        # signed in as the new employee right away: they land on their own home page, with only what's opened to them
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, _("أهلًا! الحساب اتعمل. المرة الجاية ادخل بالإيميل وكلمة المرور."))
        return redirect("dashboard:home")
    return render(request, "accounts/accept_invite.html", {"invitation": inv, "form": form})
