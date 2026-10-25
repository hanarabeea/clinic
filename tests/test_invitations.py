from datetime import timedelta

from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Invitation, User
from apps.organizations.models import Membership

PASSWORD = "Strong-Pass-2026!"


def _invite(client, org, make_member, role="reception"):
    from apps.accounts.roles import ensure_defaults
    from apps.organizations.models import ClinicRole

    ensure_defaults(org)
    value = "owner" if role == "owner" else str(ClinicRole.objects.get(organization=org, key=role).pk)
    client.force_login(make_member(org, "owner"))
    resp = client.post(
        reverse("accounts:invite_create"), {"full_name": "Mona Ali", "email": "Mona@Example.com", "role": value}
    )
    assert resp.status_code == 302
    link = client.session["new_invite_link"]
    client.logout()
    return link


def test_invite_flow_creates_user_and_membership(client, org_a, make_member):
    link = _invite(client, org_a, make_member)
    assert len(mail.outbox) == 1
    assert link in mail.outbox[0].body

    token = link.rstrip("/").rsplit("/", 1)[-1]
    assert Invitation.objects.get().token_hash != token  # only the hash is stored

    path = reverse("accounts:accept_invite", args=[token])
    assert client.get(path).status_code == 200
    resp = client.post(path, {"full_name": "Mona Ali", "password1": PASSWORD, "password2": PASSWORD})
    assert resp.status_code == 302

    user = User.objects.get(email="mona@example.com")
    m = Membership.objects.get(user=user, organization=org_a)
    assert m.role == "member" and m.clinic_role.key == "reception" and m.role_name == "استقبال"
    resp = client.post(reverse("accounts:login"), {"username": "mona@example.com", "password": PASSWORD})
    assert resp.status_code == 302

    # Link is single-use.
    client.logout()
    assert client.get(path).status_code == 404


def test_expired_invite_is_rejected(client, org_a, make_member):
    link = _invite(client, org_a, make_member)
    Invitation.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    token = link.rstrip("/").rsplit("/", 1)[-1]
    assert client.get(reverse("accounts:accept_invite", args=[token])).status_code == 404


def test_weak_password_rejected(client, org_a, make_member):
    link = _invite(client, org_a, make_member)
    token = link.rstrip("/").rsplit("/", 1)[-1]
    resp = client.post(
        reverse("accounts:accept_invite", args=[token]),
        {"full_name": "Mona", "password1": "12345678", "password2": "12345678"},
    )
    assert resp.status_code == 200
    assert not User.objects.filter(email="mona@example.com").exists()


def test_admin_cannot_invite_owner(client, org_a, make_member):
    client.force_login(make_member(org_a, "admin"))
    resp = client.post(reverse("accounts:invite_create"), {"full_name": "X", "email": "x@example.com", "role": "owner"})
    assert resp.status_code == 400
    assert not Invitation.objects.exists()


def test_create_user_command(org_a):
    from django.core.management import call_command

    call_command(
        "create_user", email="Owner@Example.com", name="Owner", role="owner", org="demo-clinic", password=PASSWORD
    )
    user = User.objects.get(email="owner@example.com")
    assert user.memberships.get().role == "owner"
    # Idempotent: running again updates, doesn't duplicate.
    call_command("create_user", email="owner@example.com", name="Owner 2", role="admin", org="demo-clinic")
    user.refresh_from_db()
    assert user.full_name == "Owner 2"
    assert user.memberships.count() == 1
    m = user.memberships.get()
    assert m.role == "member" and m.clinic_role.key == "admin"


def test_seed_org_is_idempotent(db):
    from django.core.management import call_command

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


def test_accepting_in_the_owners_browser_signs_in_the_new_employee(client, org_a, make_member):
    """The owner opens the invite link in their own browser to try it: the owner is signed out, and after accepting the
    browser is the new employee's — with only their role's pages (it used to stay on the owner's account)."""
    from apps.accounts.roles import ensure_defaults
    from apps.organizations.models import ClinicRole

    ensure_defaults(org_a)
    viewer = ClinicRole.objects.get(organization=org_a, key="viewer")
    owner = make_member(org_a, "owner")
    client.force_login(owner)
    client.post(
        reverse("accounts:invite_create"), {"full_name": "Mona", "email": "mona@example.com", "role": viewer.pk}
    )
    path = "/" + client.session["new_invite_link"].split("/", 3)[3]

    page = client.get(path).content.decode()  # still the owner's browser
    assert "اتعمل خروج من حساب" in page
    assert "_auth_user_id" not in client.session

    resp = client.post(path, {"full_name": "Mona", "password1": PASSWORD, "password2": PASSWORD})
    assert resp.status_code == 302 and resp["Location"] == reverse("dashboard:home")
    assert int(client.session["_auth_user_id"]) == User.objects.get(email="mona@example.com").pk
    nav = client.get(reverse("dashboard:home")).content.decode().split("<nav", 1)[1].split("</nav>", 1)[0]
    assert reverse("scheduling:appointments_day") in nav
    for hidden in ("reception:today", "clinical:desk", "accounts:users", "organizations:settings", "billing:cashbox"):
        assert reverse(hidden) not in nav, hidden


def test_login_page_says_who_is_already_signed_in(client, org_a, make_member):
    client.force_login(make_member(org_a, "owner", email="boss@example.com"))
    resp = client.get(reverse("accounts:login"), follow=True)
    assert "إنت داخل بالفعل بحساب boss@example.com" in resp.content.decode()
