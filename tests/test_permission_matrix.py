"""Per-employee permissions, end to end (apps/accounts/permissions.py).

1. Every URL of the app: a member without its permission gets 403 (GET and POST), a member with only that permission
   gets through the gate.
2. A fully populated clinic is crawled as many different employees (every role, "only this one permission" for each
   permission, roles with permissions added / taken away). On every page they reach, every link, button, form, HTMX
   call and JS URL is resolved: none may lead to something the employee isn't allowed to use, and no followed link may
   answer 403. So an employee never sees a page or a button that isn't opened to them.
3. The permissions screen itself: grant / revoke / reset, edit → view, an editor can't give what they don't have,
   the owner and yourself can't be edited, changing the role resets, everything audited.
"""

import io
import re
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urlparse

import pytest
from django.core.management import call_command
from django.urls import NoReverseMatch, Resolver404, URLPattern, URLResolver, get_resolver, resolve, reverse

from apps.accounts.permissions import (
    ALL_PERMISSIONS,
    IMPLIES,
    PERMISSION_GROUPS,
    PERMISSION_LABELS,
    ROLE_PERMISSIONS,
    effective_permissions,
    has_perm,
    with_implied,
)
from apps.organizations.models import Membership, Role

PASSWORD = "Strong-Pass-2026!"


def _member(org, role, *, grant=(), revoke=(), email=None, clinic_perms=None):
    """`clinic_perms`: put the member on a role the clinic made itself, with exactly these permissions."""
    from apps.accounts.models import User
    from apps.organizations.models import ClinicRole

    email = email or f"m{User.objects.count()}-{role}@example.com"
    user = User.objects.create_user(email=email, password=PASSWORD, full_name=email.split("@")[0])
    clinic_role = None
    if clinic_perms is not None:
        role = Role.MEMBER
        clinic_role = ClinicRole.objects.create(
            organization=org, name=f"custom {email}", permissions=sorted(clinic_perms)
        )
    Membership.objects.create(
        user=user,
        organization=org,
        role=role,
        clinic_role=clinic_role,
        granted_permissions=sorted(grant),
        revoked_permissions=sorted(revoke),
    )
    return user


def _dependents(perm):
    """Everything that would bring `perm` back through IMPLIES — taking `perm` away takes these too."""
    return {q for q in ALL_PERMISSIONS if perm in with_implied({q})}


def _without(perm):
    """A doctor-role member who has every permission except `perm` (and what needs it)."""
    gone = _dependents(perm)
    return {"role": Role.DOCTOR, "grant": ALL_PERMISSIONS - gone, "revoke": gone}


def _only(perm):
    """A viewer-role member with just `perm` (and what it needs) — the viewer's own permission taken away."""
    return {"role": Role.VIEWER, "grant": {perm}, "revoke": {"appointment.view"} - with_implied({perm})}


def _perms_of(user):
    return effective_permissions(Membership.objects.get(user=user))


# ---------------------------------------------------------------- the model


def test_every_permission_has_a_label_and_a_group():
    listed = [p for _g, items in PERMISSION_GROUPS for p, _l, _h in items]
    assert sorted(listed) == sorted(ALL_PERMISSIONS)
    assert len(listed) == len(set(listed))
    assert set(IMPLIES) <= ALL_PERMISSIONS
    assert set().union(*IMPLIES.values()) <= ALL_PERMISSIONS


@pytest.mark.parametrize("role", list(ROLE_PERMISSIONS))
def test_role_sets_are_already_complete(role):
    """IMPLIES never changes what a plain role can do."""
    assert with_implied(ROLE_PERMISSIONS[role]) == ROLE_PERMISSIONS[role]


def test_grant_revoke_and_implied(org_a):
    rec = Membership.objects.get(
        user=_member(org_a, Role.RECEPTION, grant={"clinical.edit"}, revoke={"payment.record"})
    )
    perms = effective_permissions(rec)
    assert {"clinical.edit", "clinical.view"} <= perms  # edit brings view
    assert "payment.record" not in perms
    assert has_perm(rec, "clinical.view") and not has_perm(rec, "payment.record")

    # taking away a permission that another kept permission needs: it comes back (edit can't work without view)
    doc = Membership.objects.get(user=_member(org_a, Role.DOCTOR, revoke={"clinical.view"}))
    assert "clinical.view" in effective_permissions(doc)
    doc.revoked_permissions = sorted(_dependents("clinical.view"))
    assert "clinical.view" not in effective_permissions(doc)  # cache follows the lists
    assert "prescription.write" not in effective_permissions(doc)


def test_owner_always_has_everything_and_inactive_has_nothing(org_a):
    owner = Membership.objects.get(user=_member(org_a, Role.OWNER, revoke=set(ALL_PERMISSIONS)))
    assert effective_permissions(owner) == ALL_PERMISSIONS
    doc = Membership.objects.get(user=_member(org_a, Role.DOCTOR))
    doc.is_active = False
    assert effective_permissions(doc) == frozenset()


def test_unknown_permissions_in_the_lists_are_ignored(org_a):
    m = Membership.objects.get(user=_member(org_a, Role.VIEWER, grant={"rocket.launch"}))
    assert effective_permissions(m) == ROLE_PERMISSIONS[Role.VIEWER]


def test_templates_only_use_real_permissions():
    """A typo in `{% if can.xxx %}` would silently hide (or never hide) a button."""
    names = {p.replace(".", "_") for p in ALL_PERMISSIONS}
    bad = []
    for path in Path("templates").rglob("*.html"):
        for name in re.findall(r"\bcan\.([a-z_]+)", path.read_text(encoding="utf8")):
            if name not in names:
                bad.append(f"{path}: can.{name}")
    assert bad == []


def test_code_only_uses_real_permissions():
    bad = []
    for path in Path("apps").rglob("*.py"):
        text = path.read_text(encoding="utf8")
        for perm in re.findall(r"""(?:has_perm\([^,()]+,\s*|require_perm\()["']([a-z_.]+)["']""", text):
            if perm not in ALL_PERMISSIONS:
                bad.append(f"{path}: {perm}")
    assert bad == []


# ---------------------------------------------------------------- every URL's gate


def _patterns(patterns=None, prefix="", ns=""):
    for p in patterns if patterns is not None else get_resolver().url_patterns:
        if isinstance(p, URLResolver):
            yield from _patterns(p.url_patterns, prefix + str(p.pattern), p.namespace or ns)
        elif isinstance(p, URLPattern) and p.name and ns != "admin":
            yield f"{ns}:{p.name}" if ns else p.name, p


def _dummy_url(name, pattern):
    kwargs = {}
    for key, conv in pattern.pattern.converters.items():
        kwargs[key] = 999999 if type(conv).__name__ == "IntConverter" else "x"
    try:
        return reverse(name, kwargs=kwargs)
    except NoReverseMatch:
        return None


GATED = sorted(
    (name, p.callback.required_perm)
    for name, p in _patterns()
    if getattr(p.callback, "required_perm", None) in ALL_PERMISSIONS
)


def test_almost_every_url_is_gated():
    """Only login/reset pages, the health check, the gateway webhook and a few "any member" pages (home, logo,
    prescription PDF/list — which check permissions inside) are not behind a named permission."""
    ungated = sorted(
        name
        for name, p in _patterns()
        if getattr(p.callback, "required_perm", None) not in ALL_PERMISSIONS
        and getattr(p.callback, "required_perm", None) != "member"
    )
    assert ungated == sorted(
        [
            "healthz",
            "accounts:login",
            "accounts:logout",
            "accounts:password_reset",
            "accounts:password_reset_done",
            "accounts:password_reset_confirm",
            "accounts:password_reset_complete",
            "messaging:whatsapp_webhook",
        ]
    )
    assert len(GATED) > 100


@pytest.mark.parametrize("perm", sorted(ALL_PERMISSIONS))
def test_every_url_needs_its_permission(client, org_a, perm):
    urls = [
        (name, _dummy_url(name, p)) for name, p in _patterns() if getattr(p.callback, "required_perm", None) == perm
    ]
    urls = [(n, u) for n, u in urls if u]
    if not urls:
        pytest.skip(f"no URL is gated by {perm} alone (checked inside views / templates)")
    lacking = _member(org_a, **_without(perm))
    having = _member(org_a, **_only(perm))
    assert perm not in _perms_of(lacking) and perm in _perms_of(having)
    wrong = []
    client.force_login(lacking)
    for name, url in urls:
        for method in ("get", "post"):
            status = getattr(client, method)(url, HTTP_HX_REQUEST="true").status_code
            if status not in (403, 405):
                wrong.append(f"without {perm}: {method.upper()} {name} → {status}")
    client.force_login(having)
    for name, url in urls:
        for method in ("get", "post"):
            status = getattr(client, method)(url, HTTP_HX_REQUEST="true").status_code
            if status == 403 or status >= 500:
                wrong.append(f"with only {perm}: {method.upper()} {name} → {status}")
    assert wrong == []


# ---------------------------------------------------------------- what each employee sees


URL_IN_PAGE = re.compile(r"""["'](/[^"'\s<>{}]*)["']""")
FOLLOW = re.compile(r'(?:href|hx-get|data-drawer)="(/[^"]*)"')
SKIP = re.compile(r"logout|/pdf/|\.pdf|export|calibration|/static/|/media/|password-reset|/admin/|/lock/|healthz")
MAX_PAGES = 450


@pytest.fixture
def clinic(db):
    from apps.organizations.models import Organization

    out = io.StringIO()
    call_command("seed_org", stdout=out)
    org = Organization.objects.get(slug="demo-clinic")
    for cmd in ("seed_notifications", "import_drugs"):
        call_command(cmd, org=org.slug, stdout=out)
    call_command("seed_demo", org=org.slug, stdout=out)
    call_command("simulate_day", org=org.slug, stdout=out)
    return org


def _visit_as(client, user, max_pages=MAX_PAGES):
    """Crawl from the home page; return the problems: links to something not allowed, links answering 403, crashes."""
    perms = _perms_of(user)
    client.force_login(user)
    seen, queue, problems, pages = set(), deque(["/"]), [], 0
    checked = {}
    while queue and len(seen) < max_pages:
        url = urldefrag(queue.popleft())[0]
        if url in seen or SKIP.search(url):
            continue
        seen.add(url)
        try:
            resp = client.get(url, HTTP_HX_REQUEST="true" if "hx" in url else None, follow=False)
        except Exception as e:
            problems.append(f"{url}: crashed {e!r:.150}")
            continue
        if resp.status_code >= 500:
            problems.append(f"{url}: {resp.status_code}")
        if resp.status_code == 403:
            problems.append(f"{url}: 403 — linked from a page this employee can see")
        if resp.status_code in (301, 302) and resp.get("Location", "").startswith("/"):
            queue.append(resp["Location"])
        if resp.status_code != 200 or "text/html" not in resp.get("Content-Type", ""):
            continue
        pages += 1
        html = resp.content.decode(errors="ignore").replace("&amp;", "&")
        for link in set(URL_IN_PAGE.findall(html)):
            path = urlparse(link).path
            if path in checked or path.startswith(("/static/", "/media/")):
                continue
            try:
                need = getattr(resolve(path).func, "required_perm", None)
            except Resolver404:
                need = None  # a JS prefix ("/booking/" + id) — not a URL by itself
            checked[path] = need
            if need in ALL_PERMISSIONS and need not in perms:
                problems.append(f"{url} shows {path} (needs «{PERMISSION_LABELS[need]}» {need})")
        for link in FOLLOW.findall(html):
            if urlparse(link).path:
                queue.append(link)
    return pages, problems


def _crawl_profiles(client, org, profiles, max_pages=MAX_PAGES):
    report = {}
    for label, spec in profiles.items():
        user = _member(
            org,
            spec["role"],
            grant=spec.get("grant", ()),
            revoke=spec.get("revoke", ()),
            clinic_perms=spec.get("clinic_perms"),
        )
        pages, problems = _visit_as(client, user, max_pages)
        print(f"[perm-crawl] {label}: {pages} pages")
        assert pages >= 1, label
        if problems:
            report[label] = problems[:15]
    text = "\n".join(f"{label}:\n  " + "\n  ".join(problems) for label, problems in report.items())
    if text:
        print(f"[perm-crawl] PROBLEMS\n{text}")
    return text


pytestmark = pytest.mark.usefixtures("midday")


def test_roles_see_only_what_they_may_use(client, clinic):
    profiles = {role: {"role": role} for role in ROLE_PERMISSIONS}
    assert _crawl_profiles(client, clinic, profiles) == ""


def test_single_permission_employees_see_only_their_permission(client, clinic):
    profiles = {f"only {p}": _only(p) for p in sorted(ALL_PERMISSIONS)}
    profiles["nothing at all"] = {"role": Role.VIEWER, "revoke": {"appointment.view"}}
    assert _crawl_profiles(client, clinic, profiles, max_pages=250) == ""


def test_adjusted_roles_see_only_what_they_may_use(client, clinic):
    doctor_minus = [
        "clinical.edit", "prescription.write", "payment.record", "report.finance", "reception.operate",
        "patient.view_medical", "schedule.manage", "drug.manage", "appointment.book", "messages.view",
        "appointment.cancel", "appointment.reschedule", "attachment.upload", "vitals.record",
    ]  # fmt: skip
    profiles = {f"doctor - {p}": {"role": Role.DOCTOR, "revoke": _dependents(p)} for p in doctor_minus}
    for p in ("payment.record", "messages.view", "appointment.book", "reception.operate", "patient.edit_basic"):
        profiles[f"reception - {p}"] = {"role": Role.RECEPTION, "revoke": _dependents(p)}
    profiles["reception + clinical.view"] = {"role": Role.RECEPTION, "grant": {"clinical.view"}}
    profiles["reception + report.finance + settings"] = {
        "role": Role.RECEPTION,
        "grant": {"report.finance", "settings.manage"},
    }
    profiles["admin + clinical.edit"] = {"role": Role.ADMIN, "grant": {"clinical.edit"}}
    profiles["admin - settings.manage"] = {"role": Role.ADMIN, "revoke": {"settings.manage"}}
    # roles the clinic made itself
    profiles["custom: nurse"] = {"role": Role.MEMBER, "clinic_perms": {"vitals.record", "clinical.view"}}
    profiles["custom: accountant"] = {"role": Role.MEMBER, "clinic_perms": {"payment.record", "report.finance"}}
    profiles["custom: secretary + rx print"] = {
        "role": Role.MEMBER,
        "clinic_perms": {"appointment.book", "reception.operate", "prescription.print"},
        "grant": {"messages.view"},
    }
    assert _crawl_profiles(client, clinic, profiles, max_pages=300) == ""


# ---------------------------------------------------------------- menus


NAV = {
    "reception.operate": "reception:today",
    "clinical.view": "clinical:desk",
    "appointment.book": "scheduling:booking_home",
    "appointment.view": "scheduling:appointments_day",
    "patient.view_basic": "patients:list",
    "messages.view": "notifications:log",
    "payment.record": "billing:cashbox",
    "report.finance": "dashboard:reports",
    "user.manage": "accounts:users",
    "audit.view": "audit:activity",
}


def _nav_html(client, user):
    client.force_login(user)
    html = client.get(reverse("dashboard:home")).content.decode()
    return html.split("<nav", 1)[1].split("</nav>", 1)[0]


@pytest.mark.parametrize(("perm", "url_name"), sorted(NAV.items()))
def test_sidebar_link_follows_the_permission(client, org_a, perm, url_name):
    assert f'href="{reverse(url_name)}"' in _nav_html(client, _member(org_a, **_only(perm)))
    assert f'href="{reverse(url_name)}"' not in _nav_html(client, _member(org_a, **_without(perm)))


@pytest.mark.parametrize(
    ("grant", "first_tab", "tabs", "hidden"),
    [
        ({"settings.manage"}, "organizations:settings", ["messaging:whatsapp", "prescriptions:settings"],
         ["doctors:schedule", "prescriptions:categories"]),
        ({"schedule.manage"}, "doctors:schedule", ["doctors:visit_types"],
         ["organizations:settings", "messaging:whatsapp", "prescriptions:categories"]),
        ({"drug.manage"}, "prescriptions:categories", [],
         ["organizations:settings", "doctors:schedule", "notifications:settings"]),
    ],
)  # fmt: skip
def test_settings_menu_and_tabs_follow_permissions(client, org_a, grant, first_tab, tabs, hidden):
    user = _member(org_a, Role.VIEWER, grant=grant)
    assert f'href="{reverse(first_tab)}"' in _nav_html(client, user)
    page = client.get(reverse(first_tab))
    assert page.status_code == 200
    html = page.content.decode()
    for name in tabs:
        assert f'href="{reverse(name)}"' in html, name
    for name in hidden:
        assert f'href="{reverse(name)}"' not in html, name


def test_no_settings_menu_without_any_settings_permission(client, org_a):
    nav = _nav_html(client, _member(org_a, Role.RECEPTION))
    assert "الإعدادات" not in nav and "الإدارة" not in nav


# ---------------------------------------------------------------- the permissions screen


@pytest.fixture
def owner(org_a):
    return _member(org_a, Role.OWNER, email="owner-perm@example.com")


def _membership(user):
    return Membership.objects.get(user=user)


def test_permissions_screen_lists_every_permission_grouped(client, org_a, owner):
    rec = _member(org_a, Role.RECEPTION)
    client.force_login(owner)
    users = client.get(reverse("accounts:users")).content.decode()
    url = reverse("accounts:membership_permissions", args=[_membership(rec).pk])
    assert url in users
    html = client.get(url).content.decode()
    for _title, items in PERMISSION_GROUPS:
        assert _title in html
        for perm, label, _h in items:
            assert f'value="{perm}"' in html and label in html
    assert 'value="payment.record"\n' in html or 'value="payment.record"' in html


def test_grant_and_revoke_from_the_screen(client, org_a, owner):
    from apps.audit.models import AuditEvent

    rec = _member(org_a, Role.RECEPTION)
    m = _membership(rec)
    url = reverse("accounts:membership_permissions", args=[m.pk])
    client.force_login(owner)
    keep = set(ROLE_PERMISSIONS[Role.RECEPTION]) - {"payment.record"}
    resp = client.post(url, {"perms": sorted(keep | {"clinical.edit"})})
    assert resp.status_code == 302
    m.refresh_from_db()
    assert m.granted_permissions == ["clinical.edit", "clinical.view"]
    assert m.revoked_permissions == ["payment.record"]
    event = AuditEvent.objects.filter(action="user.permissions").latest("created_at")
    assert "شاشة الدكتور" in event.summary and "الخزنة" in event.summary

    # the employee's own screens follow at once
    client.force_login(rec)
    assert client.get(reverse("clinical:desk")).status_code == 200
    assert client.get(reverse("billing:cashbox")).status_code == 403
    assert f'href="{reverse("billing:cashbox")}"' not in _nav_html(client, rec)

    # reset to the role
    client.force_login(owner)
    client.post(url, {"action": "reset"})
    m.refresh_from_db()
    assert m.granted_permissions == [] and m.revoked_permissions == []
    client.force_login(rec)
    assert client.get(reverse("clinical:desk")).status_code == 403
    assert client.get(reverse("billing:cashbox")).status_code == 200


def test_unchecking_view_while_keeping_edit_keeps_view(client, org_a, owner):
    doc = _member(org_a, Role.DOCTOR)
    m = _membership(doc)
    client.force_login(owner)
    perms = set(ROLE_PERMISSIONS[Role.DOCTOR]) - {"clinical.view"}
    client.post(reverse("accounts:membership_permissions", args=[m.pk]), {"perms": sorted(perms)})
    m.refresh_from_db()
    assert "clinical.view" in effective_permissions(m)
    assert m.revoked_permissions == []


def test_unknown_permission_posted_is_ignored(client, org_a, owner):
    m = _membership(_member(org_a, Role.VIEWER))
    client.force_login(owner)
    client.post(reverse("accounts:membership_permissions", args=[m.pk]), {"perms": ["appointment.view", "rocket.x"]})
    m.refresh_from_db()
    assert m.granted_permissions == [] and m.revoked_permissions == []


def test_editor_cannot_give_what_they_do_not_have(client, org_a):
    """An admin (no clinical permissions) given the reception permissions can manage a receptionist, but can't hand
    out clinical permissions; a doctor (who has permissions the admin lacks) is out of their reach entirely."""
    admin = _member(org_a, Role.ADMIN, grant=ROLE_PERMISSIONS[Role.RECEPTION])
    rec = _membership(_member(org_a, Role.RECEPTION))
    doc = _membership(_member(org_a, Role.DOCTOR))
    client.force_login(admin)
    html = client.get(reverse("accounts:membership_permissions", args=[rec.pk])).content.decode()
    assert re.search(r'value="clinical\.edit"[^>]*disabled', html)
    client.post(
        reverse("accounts:membership_permissions", args=[rec.pk]),
        {"perms": sorted(ROLE_PERMISSIONS[Role.RECEPTION] | {"clinical.edit", "report.finance", "audit.view"})},
    )
    rec.refresh_from_db()
    assert rec.granted_permissions == ["audit.view"]  # the only one of the three the admin has

    url = reverse("accounts:membership_permissions", args=[doc.pk])
    assert client.get(url).status_code == 403
    assert client.post(url, {"perms": ["appointment.view"]}).status_code == 403
    assert client.post(reverse("accounts:membership_toggle", args=[doc.pk])).status_code == 403
    doc.refresh_from_db()
    assert doc.is_active and "clinical.edit" in effective_permissions(doc)


@pytest.mark.parametrize("target", ["self", "owner"])
def test_owner_and_self_cannot_be_edited(client, org_a, owner, target):
    admin = _member(org_a, Role.ADMIN)
    m = _membership(admin if target == "self" else owner)
    client.force_login(admin)
    url = reverse("accounts:membership_permissions", args=[m.pk])
    assert client.get(url).status_code == 403
    assert client.post(url, {"perms": []}).status_code == 403


def test_owner_cannot_edit_another_owner_permissions(client, org_a, owner):
    other = _membership(_member(org_a, Role.OWNER))
    client.force_login(owner)
    assert client.get(reverse("accounts:membership_permissions", args=[other.pk])).status_code == 403


def test_other_clinic_member_is_404(client, org_a, org_b, owner):
    stranger = _membership(_member(org_b, Role.RECEPTION))
    client.force_login(owner)
    assert client.get(reverse("accounts:membership_permissions", args=[stranger.pk])).status_code == 404
    assert client.post(reverse("accounts:membership_permissions", args=[stranger.pk]), {}).status_code == 404


def test_changing_role_resets_adjustments(client, org_a, owner, role_choice):
    m = _membership(_member(org_a, Role.RECEPTION, grant={"report.finance"}, revoke={"payment.record"}))
    client.force_login(owner)
    client.post(reverse("accounts:membership_role", args=[m.pk]), {"role": role_choice(org_a, "doctor")})
    m.refresh_from_db()
    assert m.clinic_role.key == "doctor" and m.granted_permissions == [] and m.revoked_permissions == []


def test_screen_needs_user_manage(client, org_a):
    # a member with no permissions at all: anyone holding user.manage may edit them
    m = _membership(_member(org_a, Role.VIEWER, revoke={"appointment.view"}))
    client.force_login(_member(org_a, **_without("user.manage")))
    assert client.get(reverse("accounts:membership_permissions", args=[m.pk])).status_code == 403
    client.force_login(_member(org_a, **_only("user.manage")))
    assert client.get(reverse("accounts:membership_permissions", args=[m.pk])).status_code == 200


def test_employee_given_user_manage_cannot_go_above_themselves(client, org_a, role_choice):
    """A receptionist given "user.manage" manages only people whose permissions they all have, assigns only roles
    that don't exceed theirs, and can't touch admins / doctors."""
    boss = _member(org_a, Role.RECEPTION, grant={"user.manage"})
    admin = _membership(_member(org_a, Role.ADMIN))
    viewer = _membership(_member(org_a, Role.VIEWER))
    client.force_login(boss)
    html = client.get(reverse("accounts:users")).content.decode()
    assert reverse("accounts:membership_permissions", args=[viewer.pk]) in html
    assert reverse("accounts:membership_permissions", args=[admin.pk]) not in html
    assert reverse("accounts:membership_toggle", args=[admin.pk]) not in html
    doctor, admin_role, reception = (role_choice(org_a, k) for k in ("doctor", "admin", "reception"))
    assert f'value="{doctor}"' not in html and f'value="{admin_role}"' not in html
    assert f'value="{reception}"' in html and 'value="owner"' not in html

    for name in ("membership_permissions", "membership_toggle", "membership_role"):
        resp = client.post(reverse(f"accounts:{name}", args=[admin.pk]), {"role": role_choice(org_a, "viewer")})
        assert resp.status_code == 403
    assert client.post(reverse("accounts:membership_role", args=[viewer.pk]), {"role": admin_role}).status_code == 302
    viewer.refresh_from_db()
    assert viewer.clinic_role.key == "viewer"  # admin isn't an allowed role for them
    client.post(
        reverse("accounts:membership_permissions", args=[viewer.pk]), {"perms": ["payment.record", "audit.view"]}
    )
    viewer.refresh_from_db()
    assert viewer.granted_permissions == ["payment.record"]
    assert (
        client.post(
            reverse("accounts:member_create"),
            {"full_name": "x", "username": "x", "password1": PASSWORD, "password2": PASSWORD, "role": admin_role},
        ).status_code
        == 400
    )


def test_read_only_medical_file_has_no_write_controls(client, clinic):
    """clinical.view without clinical.edit: the visit opens, but no autosave URL, no finish, no reuse."""
    from apps.clinical.models import Visit

    visit = Visit.objects.filter(organization=clinic).first()
    user = _member(clinic, Role.RECEPTION, grant={"clinical.view"})
    client.force_login(user)
    html = client.get(reverse("clinical:visit", args=[visit.pk])).content.decode()
    assert reverse("clinical:visit_field", args=[visit.pk]) not in html
    assert reverse("clinical:visit_finish", args=[visit.pk]) not in html
    assert (
        client.post(reverse("clinical:visit_field", args=[visit.pk]), {"field": "complaint", "value": "x"}).status_code
        == 403
    )
