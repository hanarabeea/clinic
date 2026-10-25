from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.organizations.models import Membership, Role

from .forms import AcceptInviteForm, EmailAuthenticationForm, InviteForm, RoleForm
from .models import Invitation, User
from .permissions import (
    ASSIGNABLE_ROLES,
    IMPLIES,
    PERMISSION_GROUPS,
    PERMISSION_LABELS,
    effective_permissions,
    require_perm,
    role_permissions,
    with_implied,
)


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True


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


def _allowed_roles(request):
    """Owner/admin: the 04 matrix. Anyone else given "user.manage": only roles not above their own permissions."""
    m = request.membership
    if m.role in ASSIGNABLE_ROLES:
        return ASSIGNABLE_ROLES[m.role]
    mine = effective_permissions(m)
    return [r for r in ASSIGNABLE_ROLES[Role.ADMIN] if role_permissions(r) <= mine]


def _can_edit(request, m) -> bool:
    """Never yourself; an owner only by an owner; and someone who isn't owner/admin (but was given "user.manage") can
    only manage members whose permissions they all have themselves."""
    editor = request.membership
    if m.user_id == request.user.id:
        return False
    if m.role == Role.OWNER and editor.role != Role.OWNER:
        return False
    if editor.role not in ASSIGNABLE_ROLES and not effective_permissions(m) <= effective_permissions(editor):
        return False
    return True


def _send_invite_email(request, invitation, link):
    body = render_to_string(
        "accounts/emails/invitation.txt",
        {"invitation": invitation, "link": link, "org": invitation.organization},
    )
    send_mail(_("دعوة للانضمام إلى DigiClinic"), body, None, [invitation.email])


@require_perm("user.manage")
def users_list(request):
    org = request.organization
    allowed = _allowed_roles(request)
    memberships = list(
        Membership.objects.filter(organization=org).select_related("user").order_by("-is_active", "user__full_name")
    )
    for m in memberships:
        m.can_edit = _can_edit(request, m)
        m.role_editable = m.role in allowed
    invitations = list(Invitation.objects.for_org(org).pending().order_by("-created_at"))
    for inv in invitations:
        inv.can_revoke = inv.role in allowed
    form = InviteForm(organization=org, allowed_roles=allowed)
    new_link = request.session.pop("new_invite_link", None)
    return render(
        request,
        "accounts/users.html",
        {
            "memberships": memberships,
            "invitations": invitations,
            "form": form,
            "allowed_roles": allowed,
            "new_link": new_link,
        },
    )


@require_POST
@require_perm("user.manage")
def invite_create(request):
    org = request.organization
    form = InviteForm(request.POST, organization=org, allowed_roles=_allowed_roles(request))
    if not form.is_valid():
        memberships = Membership.objects.filter(organization=org).select_related("user")
        invitations = Invitation.objects.for_org(org).pending()
        return render(
            request,
            "accounts/users.html",
            {
                "memberships": memberships,
                "invitations": invitations,
                "form": form,
                "allowed_roles": _allowed_roles(request),
            },
            status=400,
        )
    inv, token = Invitation.create_for(
        organization=org,
        email=form.cleaned_data["email"],
        full_name=form.cleaned_data["full_name"],
        role=form.cleaned_data["role"],
        invited_by=request.user,
    )
    link = request.build_absolute_uri(reverse("accounts:accept_invite", args=[token]))
    _send_invite_email(request, inv, link)
    request.session["new_invite_link"] = link
    audit.log("user.invited", request=request, summary=f"{inv.full_name} <{inv.email}> — {inv.get_role_display()}")
    messages.success(request, _("اتعملت الدعوة. ابعت الرابط للمستخدم، وصالح لمدة 48 ساعة."))
    return redirect("accounts:users")


@require_POST
@require_perm("user.manage")
def invite_revoke(request, pk):
    inv = get_object_or_404(Invitation.objects.for_org(request.organization).pending(), pk=pk)
    if inv.role not in _allowed_roles(request):
        raise PermissionDenied
    inv.revoked_at = timezone.now()
    inv.save(update_fields=["revoked_at", "updated_at"])
    audit.log("user.invite_revoked", request=request, summary=inv.email)
    messages.success(request, _("الدعوة اتلغت."))
    return redirect("accounts:users")


def _editable_membership(request, pk):
    m = get_object_or_404(Membership.objects.filter(organization=request.organization), pk=pk)
    if not _can_edit(request, m):
        raise PermissionDenied
    return m


@require_POST
@require_perm("user.manage")
def membership_role(request, pk):
    m = _editable_membership(request, pk)
    form = RoleForm(request.POST, allowed_roles=_allowed_roles(request))
    if form.is_valid():
        m.role = form.cleaned_data["role"]
        # a new role starts from that role's permissions: what was added / taken away for the old role is dropped
        m.granted_permissions, m.revoked_permissions = [], []
        m.save(update_fields=["role", "granted_permissions", "revoked_permissions", "updated_at"])
        audit.log("user.role", request=request, summary=f"{m.user.email} → {m.get_role_display()}")
        messages.success(request, _("الدور اتغير."))
    else:
        messages.error(request, _("الدور ده مش مسموح."))
    return redirect("accounts:users")


@require_perm("user.manage")
def membership_permissions(request, pk):
    """One employee's permissions: the role is the starting set, and each permission can be given or taken away for
    this person only. Whoever edits can only give permissions they have themselves."""
    m = _editable_membership(request, pk)
    if m.role == Role.OWNER:
        raise PermissionDenied  # the owner always has everything
    mine = effective_permissions(request.membership)
    current = effective_permissions(m)
    if request.method == "POST":
        if request.POST.get("action") == "reset":
            m.granted_permissions, m.revoked_permissions = [], []
            summary = "رجعت لصلاحيات الدور"
        else:
            checked = set(request.POST.getlist("perms"))
            # a permission the editor doesn't have can't be changed by them: it keeps its current state
            wanted = {p for p in mine if p in checked} | {p for p in current if p not in mine}
            wanted = with_implied(wanted)
            base = role_permissions(m.role)
            m.granted_permissions = sorted(wanted - base)
            m.revoked_permissions = sorted(base - wanted)
            added, removed = wanted - current, current - wanted
            summary = "، ".join(
                [f"+{PERMISSION_LABELS[p]}" for p in sorted(added)]
                + [f"−{PERMISSION_LABELS[p]}" for p in sorted(removed)]
            )
        m.save(update_fields=["granted_permissions", "revoked_permissions", "updated_at"])
        audit.log("user.permissions", request=request, summary=f"{m.user.email}: {summary or 'من غير تغيير'}")
        messages.success(request, _("الصلاحيات اتحفظت."))
        return redirect("accounts:membership_permissions", pk=m.pk)
    base = role_permissions(m.role)
    groups = [
        (
            title,
            [
                {
                    "perm": p,
                    "label": label,
                    "help": help_text,
                    "checked": p in current,
                    "from_role": p in base,
                    "editable": p in mine,
                    "implies": " ".join(sorted(IMPLIES.get(p, ()))),
                }
                for p, label, help_text in items
            ],
        )
        for title, items in PERMISSION_GROUPS
    ]
    return render(
        request,
        "accounts/permissions.html",
        {"member": m, "groups": groups, "customised": bool(m.granted_permissions or m.revoked_permissions)},
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


def accept_invite(request, token):
    inv = Invitation.from_token(token)
    if inv is None:
        return render(request, "accounts/invite_invalid.html", status=404)

    existing = User.objects.filter(email=inv.email).first()
    if existing is not None:
        if request.method == "POST":
            with transaction.atomic():
                Membership.objects.update_or_create(
                    user=existing, organization=inv.organization, defaults={"role": inv.role, "is_active": True}
                )
                inv.accepted_at = timezone.now()
                inv.save(update_fields=["accepted_at", "updated_at"])
            messages.success(request, _("اتضافت للشركة. ادخل بحسابك."))
            return redirect("accounts:login")
        return render(request, "accounts/accept_invite.html", {"invitation": inv, "existing": True})

    form = AcceptInviteForm(request.POST or None, email=inv.email, initial={"full_name": inv.full_name})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = User.objects.create_user(
                email=inv.email, password=form.cleaned_data["password1"], full_name=form.cleaned_data["full_name"]
            )
            Membership.objects.create(user=user, organization=inv.organization, role=inv.role)
            audit.log("user.joined", request=request, org=inv.organization, actor=user, summary=user.email)
            inv.accepted_at = timezone.now()
            inv.save(update_fields=["accepted_at", "updated_at"])
        messages.success(request, _("الحساب اتعمل. ادخل بالإيميل وكلمة المرور."))
        return redirect("accounts:login")
    return render(request, "accounts/accept_invite.html", {"invitation": inv, "form": form})
