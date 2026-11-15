"""The side menu and the "work screen" layout (apps/core/navigation.py): a role can open straight onto one screen,
laid out like a program of its own with no side menu. Layout only: access stays with permissions."""

import pytest
from django.urls import reverse

from apps.accounts import roles
from apps.core import navigation
from apps.organizations.models import ClinicRole, Membership, Role

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(org_a, make_member):
    """(user, membership) on one of the clinic's own roles, as the users screen creates them."""
    roles.ensure_defaults(org_a)

    def _make(key, email=None):
        user = make_member(org_a, key, email=email or f"{key}-nav@example.com")
        m = Membership.objects.get(user=user)
        m.clinic_role, m.role = ClinicRole.objects.get(organization=org_a, key=key), Role.MEMBER
        m.save()
        return user, m

    return _make


def test_default_reception_role_works_in_the_reception_screen(org_a):
    roles.ensure_defaults(org_a)
    by_key = {r.key: r.start_screen for r in ClinicRole.objects.filter(organization=org_a)}
    assert by_key == {"admin": "", "doctor": "", "reception": "reception", "viewer": ""}


def test_work_screen_is_the_roles_unless_the_person_cannot_open_it(org_a, make_member, staff):
    owner = Membership.objects.get(user=make_member(org_a, "owner"))
    assert navigation.work_screen(owner) is None  # the owner always gets the whole system
    assert navigation.work_screen(None) is None

    _user, m = staff("reception")
    assert navigation.work_screen(m).key == "reception"

    m.revoked_permissions = ["appointment.view", "appointment.book", "appointment.reschedule", "appointment.cancel",
                             "reception.operate"]  # fmt: skip
    m.save()
    assert navigation.work_screen(Membership.objects.get(pk=m.pk)) is None  # can't open it: back to the whole system

    built_in = Membership.objects.get(user=make_member(org_a, "reception", email="old-style@example.com"))
    assert navigation.work_screen(built_in) is None  # a member not yet on a clinic role


def test_reception_gets_one_screen_with_no_side_menu(client, staff):
    user, _m = staff("reception")
    client.force_login(user)

    home = client.get(reverse("dashboard:home"))
    assert home.status_code == 302 and home.url == reverse("reception:today")  # her screen is her home

    html = client.get(reverse("reception:today")).content.decode()
    assert "<aside" not in html and "--side-w: 0px" in html
    tools = html.split("<nav", 1)[1].split("</nav>", 1)[0]
    for name in ("patients:list", "billing:cashbox", "notifications:log"):
        assert reverse(name) in tools
    for name in ("clinical:desk", "accounts:users", "dashboard:reports", "audit:activity"):
        assert reverse(name) not in html  # nothing she can't open is offered anywhere

    # a tool page keeps the same bar and shows the way back to her screen
    html = client.get(reverse("patients:list")).content.decode()
    assert "<aside" not in html and html.count(f'href="{reverse("reception:today")}"') >= 2
    assert 'aria-current="page"' in html.split("<nav", 1)[1].split("</nav>", 1)[0]

    # layout is not access: the doctor's screen is still refused by its permission
    assert client.get(reverse("clinical:desk")).status_code == 403


def test_whole_system_keeps_the_side_menu(client, org_a, make_member):
    client.force_login(make_member(org_a, "owner"))
    html = client.get(reverse("dashboard:home")).content.decode()
    assert "<aside" in html and "--side-w: 15rem" in html
    side = html.split("<aside", 1)[1].split("</aside>", 1)[0]
    for name in ("reception:today", "clinical:desk", "patients:list", "billing:cashbox", "accounts:users",
                 "organizations:settings", "audit:activity"):  # fmt: skip
        assert f'href="{reverse(name)}"' in side
    assert side.count('aria-current="page"') == 1

    html = client.get(reverse("doctors:schedule")).content.decode()  # a settings tab lights the settings item
    side = html.split("<aside", 1)[1].split("</aside>", 1)[0]
    current = side.split('aria-current="page"', 1)[0].rsplit("<a ", 1)[1]
    assert reverse("organizations:settings") in current


@pytest.mark.parametrize(
    ("view_name", "key"),
    [
        ("dashboard:home", "home"),
        ("reception:today", "reception"),
        ("scheduling:booking_panel", "reception"),
        ("clinical:visit", "desk"),
        ("prescriptions:drugs", "desk"),
        ("prescriptions:categories", "settings"),  # exact names win over the "prescriptions:" prefix
        ("patients:detail", "patients"),
        ("patients:fields_settings", "settings"),
        ("notifications:log", "messages"),
        ("notifications:settings", "settings"),
        ("billing:cashbox", "cashbox"),
        ("accounts:roles", "users"),
        ("unknown:view", ""),
    ],
)
def test_each_page_lights_its_own_screen(view_name, key):
    assert navigation.active_key(view_name) == key


def test_settings_item_opens_the_first_tab_the_person_may_see():
    item = navigation.BY_KEY["settings"]
    assert item.url({"settings.manage", "schedule.manage"}) == reverse("organizations:settings")
    assert item.url({"schedule.manage"}) == reverse("doctors:schedule")
    assert item.url({"drug.manage"}) == reverse("prescriptions:categories")


def test_owner_sets_a_roles_work_screen_from_the_role_editor(client, org_a, make_member):
    roles.ensure_defaults(org_a)
    client.force_login(make_member(org_a, "owner"))
    doctor = ClinicRole.objects.get(organization=org_a, key="doctor")
    url = reverse("accounts:role_edit", args=[doctor.pk])
    assert 'name="start_screen"' in client.get(url).content.decode()

    data = {"name": doctor.name, "description": doctor.description, "perms": doctor.permissions, "start_screen": "desk"}
    assert client.post(url, data).status_code == 302
    doctor.refresh_from_db()
    assert doctor.start_screen == "desk"
    assert "شاشة واحدة" in client.get(reverse("accounts:roles")).content.decode()

    # a screen the role's own permissions don't open is not kept
    viewer = ClinicRole.objects.get(organization=org_a, key="viewer")
    data = {"name": viewer.name, "description": viewer.description, "perms": viewer.permissions, "start_screen": "desk"}
    resp = client.post(reverse("accounts:role_edit", args=[viewer.pk]), data, follow=True)
    viewer.refresh_from_db()
    assert viewer.start_screen == "" and "شاشة العمل اتشالت" in resp.content.decode()

    data["start_screen"] = "no-such-screen"
    assert client.post(reverse("accounts:role_edit", args=[viewer.pk]), data).status_code == 400


def test_a_doctor_role_can_work_in_the_desk_alone(client, org_a, staff):
    user, m = staff("doctor")
    ClinicRole.objects.filter(pk=m.clinic_role_id).update(start_screen="desk")
    client.force_login(user)
    home = client.get(reverse("dashboard:home"))
    assert home.status_code == 302 and home.url == reverse("clinical:desk")
    html = client.get(reverse("clinical:desk"), follow=True).content.decode()
    assert "<aside" not in html and reverse("reception:today") in html  # the rest of what a doctor may open: tools
