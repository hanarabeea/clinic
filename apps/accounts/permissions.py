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
    "invoice.view",
    "invoice.issue",
    "theatre.view",
    "theatre.schedule",
    "admission.manage",
    "emergency.register",
    "service.order",
    "pregnancy.view_basic",
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
    "invoice.view",
    "invoice.discount",
    "theatre.view",
    "theatre.schedule",
    "theatre.operate",
    "admission.manage",
    "emergency.register",
    "service.order",
    "pregnancy.view_basic",
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
    "pregnancy.view_basic",
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
# The books and the treasury: nobody's by default except the owner. The accountant and cashier roles (phase 20) and
# any role the clinic makes itself take them from here.
FINANCE = {
    "ledger.view",
    "ledger.post",
    "ledger.close_period",
    "accounts.manage",
    "treasury.view",
    "treasury.operate",
    "treasury.count",
    "invoice.view",
    "invoice.discount",
    "invoice.discount_any",
    "invoice.refund",
    "doctor.payout",
    "payer.manage",
    "purchase.view",
    "supplier.manage",
    "purchase.manage",
    "supplier.pay",
    "cheque.manage",
    "tax.manage",
    "stock.view",
    "stock.move",
    "stock.count",
    "stock.approve_count",
    "report.statements",
    "books.verify",
}
OWNER = ADMIN | DOCTOR | RECEPTION | FINANCE

# The center's other default roles (accounts/roles.DEFAULT_ROLES). Starting points the clinic edits like any role.
# The accountant keeps the books but does not give discounts: those stay with the doctors and the owner.
ACCOUNTANT = (FINANCE - {"invoice.discount", "invoice.discount_any"}) | {"report.finance", "appointment.view"}
CASHIER = {
    "appointment.view",
    "patient.view_basic",
    "payment.record",
    "invoice.view",
    "invoice.issue",
    "treasury.view",
    "treasury.operate",
}
STOREKEEPER = {"stock.view", "stock.move", "stock.count", "purchase.view"}
THEATRE_NURSE = {
    "theatre.view",
    "theatre.operate",
    "admission.manage",
    "patient.view_basic",
    "pregnancy.view_basic",
    "vitals.record",
    "appointment.view",
    "stock.view",
}
EXTRA_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "accountant": frozenset(ACCOUNTANT),
    "cashier": frozenset(CASHIER),
    "storekeeper": frozenset(STOREKEEPER),
    "theatre_nurse": frozenset(THEATRE_NURSE),
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.VIEWER: frozenset(VIEWER),
    Role.RECEPTION: frozenset(RECEPTION),
    Role.DOCTOR: frozenset(DOCTOR),
    Role.ADMIN: frozenset(ADMIN),
    Role.OWNER: frozenset(OWNER),
}
ALL_PERMISSIONS = frozenset(OWNER)

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
        ("service.order", "خدمات على الزيارة", "يضيف سونار أو إجراء أو حقنة على الزيارة، ويشيلها قبل الدفع"),
        ("emergency.register", "تسجيل الطوارئ", "زرار الطوارئ في الاستقبال، سجل الحالات، والتصرف في الحالة"),
        ("report.finance", "التقارير والإيرادات", "صفحة التقارير وأرقام الشهر"),
    ]),
    ("العمليات والإقامة", [
        ("theatre.view", "يشوف العمليات والإقامة", "جدول العمليات، صفحة العملية، مين مقيم في الغرف"),
        ("theatre.schedule", "يحجز ويأجل العمليات", "حجز عملية، تغيير الميعاد أو الغرفة، تأجيل وإلغاء"),
        ("theatre.operate", "شغل غرفة العمليات", "خطوات العملية، قايمة ما قبل العملية، الفريق، المولود، المستلزمات"),
        ("admission.manage", "دخول وخروج الغرف", "دخول مريضة غرفة إقامة وخروجها"),
    ]),
    ("الفواتير", [
        ("invoice.view", "يشوف الفواتير وحسابات المرضى", "قايمة الفواتير، حساب المريضة، طباعة الفاتورة والإيصال"),
        ("invoice.issue", "يصدر فواتير", "فاتورة عملية أو إقامة، بنود إضافية على الحساب، جهة تعاقد المريضة"),
        ("invoice.discount", "يعمل خصم", "خصم على الكشف أو على الفاتورة لحد النسبة المسموحة"),
        ("invoice.discount_any", "خصم من غير حد", "خصم أكبر من النسبة المسموحة في الإعدادات"),
        ("invoice.refund", "إشعار دائن", "يرجّع فاتورة أو بنود منها بإشعار دائن"),
        ("doctor.payout", "مستحقات الدكاترة", "كشف حساب الدكتور وصرف مستحقاته"),
        ("payer.manage", "جهات التعاقد", "إضافة الشركات والتأمين، كشف حسابها، وتحصيل منها"),
    ]),
    ("المشتريات والموردين", [
        ("purchase.view", "يشوف المشتريات والموردين", "فواتير المشتريات، الموردين وكشوف حسابهم، السداد، الضرائب"),
        ("supplier.manage", "يضيف ويعدّل الموردين", "بيانات المورد، رقمه الضريبي، مهلة السداد، رصيده الافتتاحي"),
        ("purchase.manage", "يسجّل فواتير المشتريات", "فاتورة مشتريات، مرتجع، وإلغاء فاتورة"),
        ("supplier.pay", "يسدد للموردين", "سداد نقدي أو تحويل أو شيك، وخصم تحت حساب الضريبة"),
        ("cheque.manage", "الشيكات", "تسجيل شيك وارد أو صادر، صرفه وتحصيله وارتداده"),
        ("tax.manage", "أنواع الضرائب", "نسب الضرائب، هل ض.ق.م بتتخصم، وحد الخصم"),
    ]),
    ("المخزن", [
        ("stock.view", "يشوف المخزن", "الأصناف وأرصدتها، كارت الصنف، الصرف والتحويل، الجرد"),
        ("stock.move", "يصرف ويحوّل ويضيف أصناف", "صرف من المخزن، تحويل بين مخزنين، رصيد افتتاحي، إضافة صنف أو مخزن"),
        ("stock.count", "يجرد المخزن", "يبدأ جرد ويسجّل اللي عدّه (من غير ما يشوف رصيد الدفاتر)"),
        ("stock.approve_count", "يعتمد فروق الجرد", "اعتماد جرد عدّه حد تاني، وتسوية الفروق"),
    ]),
    ("الحسابات والخزينة", [
        ("ledger.view", "يشوف الدفاتر", "ميزان المراجعة، دفتر اليومية، شجرة الحسابات وكشوف الحسابات"),
        ("ledger.post", "يسجّل قيود يدوية", "قيد جديد وعكس قيد يدوي"),
        ("ledger.close_period", "يقفل ويفتح الشهور", "قفل الشهر المالي وفتحه تاني"),
        ("accounts.manage", "يعدّل شجرة الحسابات والخزن", "إضافة وتعديل الحسابات، إضافة خزينة أو حساب بنكي"),
        ("treasury.view", "يشوف الخزن وأرصدتها", "أرصدة الخزن والبنوك، حركتها، السندات والجرد"),
        ("treasury.operate", "سندات القبض والصرف", "سند قبض، سند صرف، تحويل بين الخزن، وإلغاء سند"),
        ("treasury.count", "جرد الخزينة واعتماد الفروق", "جرد مفاجئ، واعتماد عجز أو زيادة وردية حد تاني"),
        ("report.statements", "القوائم المالية", "قائمة الدخل والمركز المالي، بالمقارنة وحسب القسم"),
        ("books.verify", "فحص الدفاتر", "يشوف نتيجة فحص سلامة الدفاتر ويشغّله"),
    ]),
    ("المرضى", [
        ("patient.view_basic", "يشوف بيانات المرضى", "قائمة المرضى، البيانات، الحساسية"),
        ("patient.edit_basic", "يعدّل بيانات المرضى", "إضافة وتعديل بيانات المريض"),
        ("patient.view_medical", "يشوف الأمراض المزمنة", "الأمراض المزمنة في ملف المريض"),
        ("patient.edit_medical", "يعدّل الأمراض المزمنة", "إضافة وتعديل الأمراض المزمنة"),
        ("patient.merge", "دمج ملفات مكررة", "دمج مريضين متسجلين مرتين"),
        ("attachment.upload", "يرفع مرفقات", "رفع تحاليل وأشعة على ملف المريض"),
        ("pregnancy.view_basic", "يشوف عمر الحمل", "أسبوع وشهر الحمل وميعاد الولادة جنب اسم المريضة، من غير تفاصيل"),
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
    "clinical.view": {"pregnancy.view_basic"},  # whoever opens the medical file sees the pregnancy week too
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
    "emergency.register": {"appointment.view", "patient.view_basic"},
    "service.order": {"appointment.view"},
    "theatre.schedule": {"theatre.view", "patient.view_basic"},
    "theatre.operate": {"theatre.view"},
    "admission.manage": {"theatre.view", "patient.view_basic"},
    "invoice.issue": {"invoice.view", "patient.view_basic"},
    "invoice.discount": {"invoice.view"},
    "invoice.discount_any": {"invoice.discount"},
    "invoice.refund": {"invoice.view"},
    "doctor.payout": {"invoice.view"},
    "payer.manage": {"invoice.view"},
    "supplier.manage": {"purchase.view"},
    "purchase.manage": {"purchase.view"},
    "supplier.pay": {"purchase.view", "treasury.view"},
    "tax.manage": {"purchase.view"},
    "cheque.manage": {"treasury.view"},
    "stock.move": {"stock.view"},
    "stock.count": {"stock.view"},
    "stock.approve_count": {"stock.view"},
    "ledger.post": {"ledger.view"},
    "ledger.close_period": {"ledger.view"},
    "accounts.manage": {"ledger.view", "treasury.view"},
    "treasury.operate": {"treasury.view"},
    "treasury.count": {"treasury.view"},
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
