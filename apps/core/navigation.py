"""The system's screens, defined once: the side menu, and the "work screen" mode where a role opens straight onto
one screen laid out like a program of its own (no side menu), both come from ITEMS.

A role's work screen is `ClinicRole.start_screen` (blank = the whole system with the side menu). It changes what the
person sees, never what they may do: access is still decided by permissions, view by view.
"""

from dataclasses import dataclass, field

from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.accounts.permissions import effective_permissions
from apps.organizations.models import Role

CLINIC, FOLLOW_UP, BOOKS, ADMIN = _("العيادة"), _("المتابعة"), _("الحسابات"), _("الإدارة")


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    icon: str
    url_name: str
    perms: tuple = ()  # any of these opens it; () = every member
    group: str = ""
    exact: tuple = ()  # view names that belong to this screen...
    prefixes: tuple = ()  # ...or view-name prefixes (exact names win over prefixes)
    work_screen: bool = False  # can be a role's work screen
    by_perm: tuple = field(default=())  # ((perm, url_name), ...): the first allowed one is where the item goes

    def allowed(self, perms) -> bool:
        return not self.perms or any(p in perms for p in self.perms)

    def url(self, perms) -> str:
        for perm, url_name in self.by_perm:
            if perm in perms:
                return reverse(url_name)
        return reverse(self.url_name)


ITEMS = (
    Item("home", _("الرئيسية"), "house", "dashboard:home", exact=("dashboard:home",)),
    Item("reception", _("الاستقبال والمواعيد"), "calendar-check", "reception:today", ("appointment.view",), CLINIC,
         prefixes=("reception:", "scheduling:"), work_screen=True),
    Item("desk", _("شاشة الدكتور"), "stethoscope", "clinical:desk", ("clinical.view",), CLINIC,
         prefixes=("clinical:", "prescriptions:"), work_screen=True),
    Item("patients", _("المرضى"), "users", "patients:list", ("patient.view_basic",), CLINIC, prefixes=("patients:",)),
    Item("theatre", _("العمليات والإقامة"), "first-aid-kit", "theatre:board", ("theatre.view",), CLINIC,
         prefixes=("theatre:",), work_screen=True),
    Item("emergencies", _("الطوارئ"), "siren", "center:emergencies", ("emergency.register",), CLINIC,
         exact=("center:emergencies",)),
    Item("inbox", _("رسايل المرضى"), "chat-circle-text", "notifications:inbox", ("messages.view",), FOLLOW_UP,
         exact=("notifications:inbox",)),
    Item("messages", _("الرسايل"), "chats", "notifications:log", ("messages.view",), FOLLOW_UP,
         exact=("notifications:log",)),
    Item("cashbox", _("الخزنة"), "cash-register", "billing:cashbox", ("payment.record",), FOLLOW_UP,
         prefixes=("billing:",), work_screen=True),
    Item("reports", _("التقارير"), "chart-bar", "dashboard:reports", ("report.finance",), FOLLOW_UP,
         exact=("dashboard:reports", "invoicing:report_revenue", "invoicing:report_operations",
                "invoicing:report_receivables", "invoicing:report_cash")),
    Item("invoices", _("الفواتير"), "receipt", "invoicing:list", ("invoice.view",), BOOKS,
         prefixes=("invoicing:",), work_screen=True),
    Item("purchases", _("المشتريات والموردين"), "truck", "purchasing:invoices", ("purchase.view",), BOOKS,
         prefixes=("purchasing:",), work_screen=True),
    Item("stock", _("المخزن"), "package", "inventory:items", ("stock.view",), BOOKS,
         prefixes=("inventory:",), work_screen=True),
    Item("treasury", _("الخزن والسندات"), "bank", "treasury:home", ("treasury.view",), BOOKS,
         prefixes=("treasury:",), work_screen=True),
    Item("books", _("الدفاتر"), "scales", "ledger:trial_balance", ("ledger.view", "report.statements", "books.verify"),
         BOOKS, prefixes=("ledger:",), work_screen=True,
         by_perm=(("ledger.view", "ledger:trial_balance"), ("report.statements", "ledger:income_statement"),
                  ("books.verify", "ledger:verify"))),
    Item("users", _("المستخدمين"), "user-gear", "accounts:users", ("user.manage",), ADMIN, prefixes=("accounts:",)),
    Item("settings", _("الإعدادات"), "gear-six", "organizations:settings",
         ("settings.manage", "schedule.manage", "drug.manage"), ADMIN,
         exact=("organizations:settings", "patients:fields_settings", "prescriptions:categories",
                "prescriptions:settings", "center:services", "center:service_edit", "theatre:setup",
                "theatre:room_add", "theatre:room_edit", "theatre:procedure_add", "theatre:procedure_edit",
                "invoicing:settings", "obstetrics:milestones", "obstetrics:milestone_new",
                "obstetrics:milestone_edit"),
         prefixes=("doctors:", "messaging:", "notifications:"),
         # each settings tab has its own permission: open the first one this person may see
         by_perm=(("settings.manage", "organizations:settings"), ("schedule.manage", "doctors:schedule"),
                  ("drug.manage", "prescriptions:categories"))),
    Item("audit", _("سجل النشاط"), "clock-counter-clockwise", "audit:activity", ("audit.view",), ADMIN,
         prefixes=("audit:",)),
)  # fmt: skip
BY_KEY = {i.key: i for i in ITEMS}
WORK_SCREENS = [(i.key, i.label) for i in ITEMS if i.work_screen]


def active_key(view_name: str) -> str:
    for item in ITEMS:
        if view_name in item.exact:
            return item.key
    for item in ITEMS:
        if any(view_name.startswith(p) for p in item.prefixes):
            return item.key
    return ""


def work_screen(membership) -> Item | None:
    """The single screen this member works in, or None for the whole system with its side menu. The owner always gets
    the whole system; so does anyone whose own permissions no longer include their role's screen."""
    if membership is None or membership.role == Role.OWNER or not membership.clinic_role_id:
        return None
    item = BY_KEY.get(membership.clinic_role.start_screen or "")
    if item is None or not item.work_screen or not item.allowed(effective_permissions(membership)):
        return None
    return item


def for_request(request) -> dict:
    """Template context: `nav_groups` for the side menu, or `work` for the one-screen layout."""
    membership = getattr(request, "membership", None)
    if membership is None:
        return {"nav_groups": [], "work": None}
    perms = effective_permissions(membership)
    match = getattr(request, "resolver_match", None)
    current = active_key(match.view_name if match else "")
    links = [
        {
            "key": i.key,
            "label": i.label,
            "icon": i.icon,
            "url": i.url(perms),
            "active": i.key == current,
            "group": i.group,
            "badge": 0,
        }
        for i in ITEMS
        if i.allowed(perms)
    ]
    for link in links:
        if link["key"] == "inbox":  # patients' messages nobody handled yet
            from apps.notifications.replies import unhandled

            link["badge"] = unhandled(membership.organization).count()
    screen = work_screen(membership)
    if screen is not None:
        home = next(link for link in links if link["key"] == screen.key)
        return {
            "nav_groups": [],
            "work": {
                "home": home,
                # everything else this person may open sits in the top bar as tools (no dashboard: it is their screen)
                "tools": [link for link in links if link["key"] not in (screen.key, "home")],
                "on_home": current == screen.key,
            },
        }
    groups = []
    for link in links:
        if not groups or groups[-1]["label"] != link["group"]:
            groups.append({"label": link["group"], "links": []})
        groups[-1]["links"].append(link)
    return {"nav_groups": groups, "work": None}
