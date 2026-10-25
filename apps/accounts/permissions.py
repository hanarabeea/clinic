"""Single source of truth for role → permission mapping (docs/plan/04-auth-permissions-security.md §4.3).

Each phase wires up the permissions its new apps need; clinical, patient and billing permissions
from the matrix are added as those apps land.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from apps.organizations.models import Role

VIEWER = {
    "appointment.view",
}
RECEPTION = VIEWER | {
    "attachment.upload",
    "reception.operate",
    "vitals.record",
    "payment.record",
    "patient.view_basic",
    "patient.edit_basic",
    "appointment.book",
    "appointment.reschedule",
    "appointment.cancel",
    "messages.view",
    "messages.retry",
}
DOCTOR = VIEWER | {
    "prescription.write",
    "prescription.print",
    "drug.manage",
    "rx_template.manage",
    "clinical.view",
    "clinical.edit",
    "attachment.upload",
    "reception.operate",
    "vitals.record",
    "payment.record",
    "report.finance",
    "schedule.manage",
    "patient.view_basic",
    "patient.edit_basic",
    "patient.view_medical",
    "patient.edit_medical",
    "patient.merge",
    "appointment.book",
    "appointment.reschedule",
    "appointment.cancel",
    "appointment.overbook",
    "messages.view",
    "messages.retry",
}
ADMIN = VIEWER | {
    "user.manage",
    "settings.manage",
    "audit.view",
    "schedule.manage",
    "patient.view_basic",
    "patient.edit_basic",
    "patient.view_medical",
    "patient.edit_medical",
    "patient.merge",
    "appointment.book",
    "appointment.reschedule",
    "appointment.cancel",
    "messages.view",
    "messages.retry",
}
OWNER = ADMIN | DOCTOR | RECEPTION

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.VIEWER: frozenset(VIEWER),
    Role.RECEPTION: frozenset(RECEPTION),
    Role.DOCTOR: frozenset(DOCTOR),
    Role.ADMIN: frozenset(ADMIN),
    Role.OWNER: frozenset(OWNER),
}
ALL_PERMISSIONS = frozenset(ADMIN | DOCTOR | RECEPTION)

# What each permission opens, grouped for the per-employee permissions screen (every permission must be listed here).
PERMISSION_GROUPS = [
    ("المواعيد والحجز", [
        ("appointment.view", "يشوف المواعيد والجدول", "صفحة المواعيد (يوم / أسبوع)"),
        ("appointment.book", "يحجز مواعيد", "شاشة حجز جديد + إضافة مريض سريع"),
        ("appointment.reschedule", "يأجل / يغيّر ميعاد", "تأجيل الحجز ونقله ليوم تاني"),
        ("appointment.cancel", "يلغي حجز", "إلغاء الحجوزات"),
        ("appointment.overbook", "حجز استثنائي", "يحجز حتى لو الفترة مليانة"),
        ("schedule.manage", "مواعيد الدكتور والإجازات", "إعدادات أيام وساعات الشغل، قفل يوم، أنواع الزيارات"),
    ]),
    ("الاستقبال والحسابات", [
        ("reception.operate", "شاشة الاستقبال", "وصول المريض، دخوله للدكتور، خلص، غياب"),
        ("vitals.record", "تسجيل القياسات", "الوزن والضغط والسكر عند الوصول"),
        ("payment.record", "الخزنة والدفع", "تسجيل الفلوس وشاشة الخزنة"),
        ("report.finance", "التقارير والإيرادات", "صفحة التقارير وأرقام الشهر"),
    ]),
    ("المرضى", [
        ("patient.view_basic", "يشوف بيانات المرضى", "قائمة المرضى، البيانات، الحساسية"),
        ("patient.edit_basic", "يعدّل بيانات المرضى", "إضافة وتعديل بيانات المريض"),
        ("patient.view_medical", "يشوف الأمراض المزمنة", "الأمراض المزمنة في ملف المريض"),
        ("patient.edit_medical", "يعدّل الأمراض المزمنة", "إضافة وتعديل الأمراض المزمنة"),
        ("patient.merge", "دمج ملفات مكررة", "دمج مريضين متسجلين مرتين"),
        ("attachment.upload", "يرفع مرفقات", "رفع تحاليل وأشعة على ملف المريض"),
    ]),
    ("الكشف والروشتة", [
        ("clinical.view", "شاشة الدكتور والملف الطبي", "يشوف الكشوفات والتاريخ المرضي والمرفقات"),
        ("clinical.edit", "يكتب في الكشف", "الشكوى والفحص والتشخيص وإنهاء الكشف"),
        ("prescription.write", "يكتب الروشتة", "إضافة الأدوية والطلبات، الصوت، الاعتماد"),
        ("prescription.print", "يطبع الروشتات", "طباعة وإعادة طباعة الروشتة"),
        ("drug.manage", "كتالوج الأدوية والتصنيفات", "إضافة وتعديل الأدوية وتصنيفاتها"),
        ("rx_template.manage", "قوالب الروشتات", "حفظ ومسح قوالب الروشتات"),
    ]),
    ("الرسايل", [
        ("messages.view", "سجل الرسايل", "يشوف الرسايل اللي اتبعتت للمرضى"),
        ("messages.retry", "إعادة إرسال رسالة", "يعيد إرسال رسالة فشلت"),
    ]),
    ("الإدارة", [
        ("settings.manage", "إعدادات العيادة", "بيانات العيادة، الرسايل، الواتساب، الإيميل، الروشتة، ملف المريض"),
        ("user.manage", "المستخدمين والصلاحيات", "دعوة موظفين وتغيير أدوارهم وصلاحياتهم"),
        ("audit.view", "سجل النشاط", "مين عمل إيه وإمتى"),
    ]),
]  # fmt: skip
PERMISSION_LABELS = {p: label for _g, items in PERMISSION_GROUPS for p, label, _d in items}

# A permission that only makes sense with another one brings it along (edit → view).
IMPLIES = {
    "clinical.edit": {"clinical.view"},
    "prescription.write": {"clinical.view", "prescription.print"},
    "patient.edit_basic": {"patient.view_basic"},
    "patient.view_medical": {"patient.view_basic"},
    "patient.edit_medical": {"patient.view_medical", "patient.view_basic"},
    "patient.merge": {"patient.view_basic", "patient.edit_basic"},
    "appointment.book": {"appointment.view", "patient.view_basic"},
    "appointment.reschedule": {"appointment.view"},
    "appointment.cancel": {"appointment.view"},
    "appointment.overbook": {"appointment.book"},
    "reception.operate": {"appointment.view", "patient.view_basic"},
    "messages.retry": {"messages.view"},
}


def with_implied(perms) -> frozenset[str]:
    out, todo = set(perms), list(perms)
    while todo:
        for extra in IMPLIES.get(todo.pop(), ()):
            if extra not in out:
                out.add(extra)
                todo.append(extra)
    return frozenset(out & ALL_PERMISSIONS)


def role_permissions(role) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def base_permissions(membership) -> frozenset[str]:
    """What the member's role gives: the clinic's own role (ClinicRole, apps/accounts/roles.py), or — for a member
    still on a built-in role — that role's defaults."""
    if membership.role == Role.OWNER:
        return ROLE_PERMISSIONS[Role.OWNER]
    if membership.clinic_role_id:
        return with_implied(set(membership.clinic_role.permissions or ()) & ALL_PERMISSIONS)
    return role_permissions(membership.role)


def effective_permissions(membership) -> frozenset[str]:
    """The role's permissions, plus what was granted to this person, minus what was taken away. The owner always has
    everything (the clinic can never lock itself out)."""
    if membership is None or not membership.is_active:
        return frozenset()
    if membership.role == Role.OWNER:
        return ROLE_PERMISSIONS[Role.OWNER]
    role_perms = tuple(membership.clinic_role.permissions or ()) if membership.clinic_role_id else ()
    key = (
        membership.role,
        membership.clinic_role_id,
        role_perms,
        tuple(membership.granted_permissions or ()),
        tuple(membership.revoked_permissions or ()),
    )
    cached = getattr(membership, "_perm_cache", None)
    if cached and cached[0] == key:
        return cached[1]
    base = set(base_permissions(membership)) | set(membership.granted_permissions or ())
    perms = with_implied(base - set(membership.revoked_permissions or ()))
    membership._perm_cache = (key, perms)
    return perms


def has_perm(membership, perm: str) -> bool:
    if perm not in ALL_PERMISSIONS:
        raise ValueError(f"Unknown permission: {perm}")
    return perm in effective_permissions(membership)


class _Can(dict):
    """Template helper: `{% if can.settings_manage %}` (dots become underscores)."""

    def __missing__(self, key):
        return False


def permissions_for(membership) -> _Can:
    return _Can({p.replace(".", "_"): True for p in effective_permissions(membership)})


def require_perm(perm: str):
    """View decorator. Anonymous → login redirect; no org or missing perm → 403."""
    if perm not in ALL_PERMISSIONS:
        raise ValueError(f"Unknown permission: {perm}")

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not has_perm(getattr(request, "membership", None), perm):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        wrapper.required_perm = perm  # read by the permission audit / tests
        return wrapper

    return decorator


def require_membership(view):
    """Any active member of the current organization."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if getattr(request, "membership", None) is None:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    wrapper.required_perm = "member"  # any active member — read by the permission audit / tests
    return wrapper
