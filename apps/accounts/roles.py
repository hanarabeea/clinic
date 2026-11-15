"""The clinic's own roles (ClinicRole): a name + a set of permissions, created and edited by the clinic.

Every org starts with editable copies of the built-in roles (DEFAULT_ROLES); only "المالك" (owner) stays fixed — it
always has every permission, so the clinic can never lock itself out. An employee's permissions are their role's,
plus / minus their own adjustments (Membership.granted/revoked_permissions, see permissions.effective_permissions).

Nobody can give what they don't have: a role can be assigned, created or edited only within the editor's own
permissions (the owner has all of them).
"""

from django.db import transaction
from django.utils.translation import gettext as _

from apps.organizations.models import ClinicRole, Membership, Role

from .permissions import ALL_PERMISSIONS, ROLE_PERMISSIONS, effective_permissions, with_implied

OWNER_CHOICE = "owner"

DEFAULT_ROLES = [
    (Role.ADMIN, "مدير النظام", "المستخدمين والإعدادات وسجل النشاط والمواعيد والمرضى، من غير الملف الطبي"),
    (Role.DOCTOR, "دكتور", "الكشف والروشتة والملف الطبي والمواعيد والاستقبال والتقارير"),
    (Role.RECEPTION, "استقبال", "الحجز والاستقبال والخزنة وبيانات المرضى، من غير الملف الطبي"),
    (Role.VIEWER, "مشاهد", "يشوف المواعيد بس"),
]
# Roles that work in one screen open straight onto it, with no side menu (ClinicRole.start_screen).
DEFAULT_START_SCREENS = {Role.RECEPTION: "reception"}


def ensure_defaults(org):
    """Give an org without roles the built-in ones, and move members still on a built-in role (created before roles
    were editable) onto the matching clinic role. Idempotent; never recreates a role the clinic deleted."""
    if not ClinicRole.objects.filter(organization=org).exists():
        ClinicRole.objects.bulk_create(
            ClinicRole(
                organization=org,
                key=key,
                name=name,
                description=desc,
                permissions=sorted(ROLE_PERMISSIONS[key]),
                order=i,
                start_screen=DEFAULT_START_SCREENS.get(key, ""),
            )
            for i, (key, name, desc) in enumerate(DEFAULT_ROLES)
        )
    by_key = {r.key: r for r in ClinicRole.objects.filter(organization=org).exclude(key="")}
    for m in Membership.objects.filter(organization=org, clinic_role__isnull=True).exclude(role=Role.OWNER):
        role = by_key.get(m.role)
        if role is not None:
            m.clinic_role, m.role = role, Role.MEMBER
            m.save(update_fields=["clinic_role", "role", "updated_at"])


def role_perms(role) -> frozenset[str]:
    return with_implied(set(role.permissions or ()) & ALL_PERMISSIONS)


def can_give(editor, perms) -> bool:
    return editor.role == Role.OWNER or set(perms) <= effective_permissions(editor)


def choices_for(editor, org):
    """(value, label) of the roles this editor may assign: the owner any (and "المالك"), others only roles that don't
    exceed their own permissions."""
    roles = [r for r in ClinicRole.objects.filter(organization=org) if can_give(editor, role_perms(r))]
    out = [(str(r.pk), r.name) for r in roles]
    if editor.role == Role.OWNER:
        out.insert(0, (OWNER_CHOICE, Role.OWNER.label))
    return out


def value_of(m) -> str:
    """The choice value a member currently has (for the role select)."""
    if m.role == Role.OWNER:
        return OWNER_CHOICE
    return str(m.clinic_role_id) if m.clinic_role_id else ""


def resolve(org, value):
    """Choice value → (Role, ClinicRole | None)."""
    if value == OWNER_CHOICE:
        return Role.OWNER, None
    return Role.MEMBER, ClinicRole.objects.get(organization=org, pk=int(value))


@transaction.atomic
def save_role(role, *, name, description, perms, start_screen=None):
    """Create / update a role. Members' own adjustments that became redundant are dropped (a permission they were
    given that the role now has, or a removed one the role no longer has). A work screen the role's permissions don't
    open is cleared (None leaves it as it is)."""
    from apps.core.navigation import BY_KEY  # here: navigation imports the permission tables this module builds on

    role.name, role.description = name, description
    role.permissions = sorted(with_implied(set(perms) & ALL_PERMISSIONS))
    if start_screen is not None:
        role.start_screen = start_screen
    screen = BY_KEY.get(role.start_screen)
    if screen is None or not screen.work_screen or not screen.allowed(set(role.permissions)):
        role.start_screen = ""
    if role.order == 0 and role.pk is None:
        last = ClinicRole.objects.filter(organization=role.organization).order_by("-order").first()
        role.order = (last.order + 1) if last else 0
    role.save()
    base = set(role.permissions)
    for m in Membership.objects.filter(clinic_role=role):
        granted = sorted(set(m.granted_permissions or ()) - base)
        revoked = sorted(set(m.revoked_permissions or ()) & base)
        if granted != (m.granted_permissions or []) or revoked != (m.revoked_permissions or []):
            m.granted_permissions, m.revoked_permissions = granted, revoked
            m.save(update_fields=["granted_permissions", "revoked_permissions", "updated_at"])
    return role


def in_use(role) -> str:
    """Why a role can't be deleted ("" when it can)."""
    n = Membership.objects.filter(clinic_role=role).count()
    if n:
        return _("فيه %(n)s موظف على الدور ده. انقلهم لدور تاني الأول.") % {"n": n}
    return ""
