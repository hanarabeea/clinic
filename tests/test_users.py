"""Adding employees directly (no invitation links): name + username + password + role, email optional. They sign in
right away with the username or the email; the users screen edits their details and resets a forgotten password."""

import pytest
from django.core.management import call_command
from django.urls import NoReverseMatch, reverse

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.organizations.models import ClinicRole, Membership

PASSWORD = "Strong-Pass-2026!"


@pytest.fixture
def owner(org_a, make_member):
    return make_member(org_a, "owner", email="owner-users@example.com")


def _add(client, org, role_choice, **over):
    data = {
        "full_name": "منى علي",
        "username": "mona",
        "email": "",
        "password1": PASSWORD,
        "password2": PASSWORD,
        "role": role_choice(org, "reception"),
    }
    data.update(over)
    return client.post(reverse("accounts:member_create"), data)


def _login(client, login, password=PASSWORD):
    client.logout()
    return client.post(reverse("accounts:login"), {"username": login, "password": password})


def test_add_user_without_email_and_sign_in_with_the_username(client, org_a, owner, role_choice):
    client.force_login(owner)
    resp = _add(client, org_a, role_choice)
    assert resp.status_code == 302
    user = User.objects.get(username="mona")
    assert user.email is None and user.full_name == "منى علي"
    m = Membership.objects.get(user=user)
    assert m.organization == org_a and m.clinic_role.key == "reception" and m.is_active
    assert AuditEvent.objects.filter(action="user.created", summary__contains="mona").exists()

    assert _login(client, "mona").status_code == 302
    assert int(client.session["_auth_user_id"]) == user.pk
    # reception works in one screen: home is the reception board itself, with no side menu and only her own tools
    home = client.get(reverse("dashboard:home"))
    assert home.status_code == 302 and home.url == reverse("reception:today")
    html = client.get(home.url).content.decode()
    assert "<aside" not in html and reverse("clinical:desk") not in html
    tools = html.split("<nav", 1)[1].split("</nav>", 1)[0]
    assert reverse("patients:list") in tools and reverse("accounts:users") not in tools


def test_with_email_both_work_and_case_does_not_matter(client, org_a, owner, role_choice):
    client.force_login(owner)
    _add(client, org_a, role_choice, username="Mona.Ali", email="Mona@Example.com")
    user = User.objects.get(username="mona.ali")
    assert user.email == "mona@example.com"
    for login in ("mona.ali", "MONA.ALI", "mona@example.com", " Mona@Example.com "):
        assert _login(client, login).status_code == 302, login
        assert int(client.session["_auth_user_id"]) == user.pk


def test_wrong_password_or_unknown_user(client, org_a, owner, role_choice):
    client.force_login(owner)
    _add(client, org_a, role_choice)
    for login, password in (("mona", "wrong-pass-123"), ("nobody", PASSWORD)):
        resp = _login(client, login, password)
        assert resp.status_code == 200
        assert "اسم المستخدم أو كلمة المرور غلط" in resp.content.decode()
    assert AuditEvent.objects.filter(action="auth.login_failed", summary="nobody").exists()


def test_stopped_account_cannot_sign_in(client, org_a, owner, role_choice):
    client.force_login(owner)
    _add(client, org_a, role_choice)
    user = User.objects.get(username="mona")
    user.is_active = False
    user.save()
    assert _login(client, "mona").status_code == 200


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"username": "mo na"}, "username"),
        ({"username": "mona@x.com"}, "username"),
        ({"username": ""}, "username"),
        ({"email": "not-an-email"}, "email"),
        ({"password2": "Other-Pass-2026!"}, "password2"),
        ({"password1": "12345678", "password2": "12345678"}, "password1"),
        ({"full_name": ""}, "full_name"),
        ({"role": "999999"}, "role"),
    ],
)
def test_invalid_input_is_refused(client, org_a, owner, role_choice, over, field):
    client.force_login(owner)
    resp = _add(client, org_a, role_choice, **over)
    assert resp.status_code == 400
    assert field in resp.context["form"].errors
    assert not User.objects.filter(full_name="منى علي").exists()


def test_username_and_email_must_be_free(client, org_a, owner, role_choice, make_member):
    make_member(org_a, "viewer", email="taken@example.com")
    client.force_login(owner)
    assert _add(client, org_a, role_choice, username="taken@example.com").status_code == 400
    _add(client, org_a, role_choice)
    for over in ({"username": "MONA"}, {"username": "other", "email": "taken@example.com"}):
        resp = _add(client, org_a, role_choice, **over)
        assert resp.status_code == 400, over
    assert User.objects.filter(username="mona").count() == 1


def test_only_roles_within_your_permissions(client, org_a, make_member, role_choice):
    client.force_login(make_member(org_a, "admin"))
    for key in ("owner", "doctor"):
        assert _add(client, org_a, role_choice, username=f"x-{key}", role=role_choice(org_a, key)).status_code == 400
    assert _add(client, org_a, role_choice, username="x-viewer", role=role_choice(org_a, "viewer")).status_code == 302


def test_needs_user_manage(client, org_a, make_member, role_choice):
    client.force_login(make_member(org_a, "doctor"))
    assert _add(client, org_a, role_choice).status_code == 403
    assert not User.objects.filter(username="mona").exists()


def test_invitation_links_are_gone(client, org_a, owner):
    for name, args in (("accounts:accept_invite", ["x"]), ("accounts:invite_create", [])):
        with pytest.raises(NoReverseMatch):
            reverse(name, args=args)
    client.force_login(owner)
    html = client.get(reverse("accounts:users")).content.decode()
    assert "دعوة" not in html and "إضافة مستخدم" in html


# ---------------------------------------------------------------- editing / resetting a password


def test_edit_details_and_reset_password(client, org_a, owner, role_choice):
    client.force_login(owner)
    _add(client, org_a, role_choice)
    user = User.objects.get(username="mona")
    m = Membership.objects.get(user=user)
    url = reverse("accounts:member_edit", args=[m.pk])
    assert url in client.get(reverse("accounts:users")).content.decode()
    page = client.get(url)
    assert page.status_code == 200 and 'value="mona"' in page.content.decode()

    new_password = "Another-Pass-2026!"
    data = {"full_name": "منى", "username": "mona2", "email": "m@example.com"}
    resp = client.post(url, {**data, "password1": new_password, "password2": new_password})
    assert resp.status_code == 302
    user.refresh_from_db()
    assert (user.full_name, user.username, user.email) == ("منى", "mona2", "m@example.com")
    assert AuditEvent.objects.filter(action="user.edited", summary__contains="password").exists()
    assert _login(client, "mona").status_code == 200  # the old username no longer works
    assert _login(client, "mona2", PASSWORD).status_code == 200  # nor the old password
    assert _login(client, "m@example.com", new_password).status_code == 302


def test_edit_without_password_keeps_it(client, org_a, owner, role_choice):
    client.force_login(owner)
    _add(client, org_a, role_choice, email="mona@example.com")
    m = Membership.objects.get(user__username="mona")
    client.post(reverse("accounts:member_edit", args=[m.pk]), {"full_name": "منى", "username": "mona", "email": ""})
    m.user.refresh_from_db()
    assert m.user.email is None and m.user.full_name == "منى"
    assert _login(client, "mona").status_code == 302


def test_older_account_keeps_its_email_as_username(client, org_a, owner, make_member):
    """Accounts from before usernames have the email as username (it has an "@"): editing them must not force a
    change."""
    m = Membership.objects.get(user=make_member(org_a, "viewer", email="old@example.com"))
    assert m.user.username == "old@example.com"
    client.force_login(owner)
    data = {"full_name": "Old", "username": "old@example.com", "email": "old@example.com"}
    assert client.post(reverse("accounts:member_edit", args=[m.pk]), data).status_code == 302
    assert _login(client, "old@example.com").status_code == 302


def test_edit_follows_the_manage_rules(client, org_a, org_b, owner, make_member):
    admin = make_member(org_a, "admin")
    doctor = Membership.objects.get(user=make_member(org_a, "doctor"))
    stranger = Membership.objects.get(user=make_member(org_b, "viewer"))
    own = Membership.objects.get(user=owner)
    client.force_login(admin)
    data = {"full_name": "x", "username": "hacked", "email": "", "password1": "", "password2": ""}
    for target, status in ((doctor, 403), (own, 403), (stranger, 404), (Membership.objects.get(user=admin), 403)):
        assert client.get(reverse("accounts:member_edit", args=[target.pk])).status_code == status
        assert client.post(reverse("accounts:member_edit", args=[target.pk]), data).status_code == status
    assert not User.objects.filter(username="hacked").exists()


# ---------------------------------------------------------------- terminal + seed


def test_create_user_command(org_a):
    call_command("create_user", username="Boss", name="Owner", role="owner", org="demo-clinic", password=PASSWORD)
    user = User.objects.get(username="boss")
    assert user.email is None and user.memberships.get().role == "owner"
    # Idempotent: running again updates, doesn't duplicate.
    call_command("create_user", username="boss", name="Owner 2", role="admin", org="demo-clinic")
    user.refresh_from_db()
    assert user.full_name == "Owner 2" and user.memberships.count() == 1
    m = user.memberships.get()
    assert m.role == "member" and m.clinic_role.key == "admin"
    # --email alone: the email is the username too
    call_command(
        "create_user", email="Sara@Example.com", name="Sara", role="viewer", org="demo-clinic", password=PASSWORD
    )
    assert User.objects.get(username="sara@example.com").email == "sara@example.com"
    assert ClinicRole.objects.filter(organization=org_a).count() == 8  # the clinic's four roles + the center's four


def test_seed_org_is_idempotent(db):
    from apps.organizations.models import Organization

    call_command("seed_org")
    org = Organization.objects.get(slug="demo-clinic")
    org.settings.phones = ["0100"]
    org.settings.save()
    call_command("seed_org")  # must not clobber UI edits
    org.settings.refresh_from_db()
    assert org.settings.phones == ["0100"]
    call_command("seed_org", force=True)
    org.settings.refresh_from_db()
    assert org.settings.phones == ["01000000000", "0230000000"]
    assert org.settings.clinic_name_ar == "عيادة د. سارة علي"


def test_login_page_says_who_is_already_signed_in(client, org_a, make_member):
    client.force_login(make_member(org_a, "owner", email="boss@example.com"))
    resp = client.get(reverse("accounts:login"), follow=True)
    assert "إنت داخل بالفعل بحساب boss@example.com" in resp.content.decode()
