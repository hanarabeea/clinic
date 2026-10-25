"""The clinic's own roles (apps/accounts/roles.py): created / renamed / copied / edited / deleted from the screen,
assigned to employees, and an employee's pages and buttons follow their role at once."""

import pytest
from django.urls import reverse

from apps.accounts.permissions import ROLE_PERMISSIONS, effective_permissions
from apps.accounts.roles import ensure_defaults
from apps.organizations.models import ClinicRole, Membership, Role

PASSWORD = "Strong-Pass-2026!"


@pytest.fixture
def owner(org_a, make_member):
    return make_member(org_a, "owner", email="owner-roles@example.com")


def _roles(org):
    ensure_defaults(org)
    return {r.key or r.name: r for r in ClinicRole.objects.filter(organization=org)}


def _save_role(client, url, name, perms, description=""):
    return client.post(url, {"name": name, "description": description, "perms": sorted(perms)})


# ---------------------------------------------------------------- defaults & existing members


def test_every_clinic_starts_with_editable_copies_of_the_built_in_roles(org_a, make_member):
    legacy = Membership.objects.get(user=make_member(org_a, "reception"))
    before = effective_permissions(legacy)
    roles = _roles(org_a)
    assert set(roles) == {"admin", "doctor", "reception", "viewer"}
    for key in ("admin", "doctor", "reception", "viewer"):
        assert set(roles[key].permissions) == ROLE_PERMISSIONS[key]
    legacy.refresh_from_db()
    assert legacy.role == Role.MEMBER and legacy.clinic_role == roles["reception"]
    assert effective_permissions(legacy) == before  # nobody gains or loses anything by the move
    assert legacy.role_name == "استقبال"
    ensure_defaults(org_a)  # idempotent
    assert ClinicRole.objects.filter(organization=org_a).count() == 4


def test_a_deleted_default_role_is_not_recreated(org_a):
    _roles(org_a)["viewer"].delete()
    ensure_defaults(org_a)
    assert not ClinicRole.objects.filter(organization=org_a, key="viewer").exists()


# ---------------------------------------------------------------- the roles screen


def test_create_a_role_and_give_it_to_an_employee(client, org_a, owner, make_member):
    client.force_login(owner)
    assert client.get(reverse("accounts:roles")).status_code == 200
    resp = _save_role(client, reverse("accounts:role_new"), "ممرضة", {"vitals.record", "clinical.view"}, "القياسات")
    assert resp.status_code == 302
    nurse = ClinicRole.objects.get(organization=org_a, name="ممرضة")
    assert nurse.description == "القياسات" and nurse.key == ""
    assert set(nurse.permissions) == {"vitals.record", "clinical.view"}

    emp = make_member(org_a, "viewer")
    m = Membership.objects.get(user=emp)
    users = client.get(reverse("accounts:users")).content.decode()
    assert f'value="{nurse.pk}"' in users and "ممرضة" in users
    client.post(reverse("accounts:membership_role", args=[m.pk]), {"role": str(nurse.pk)})
    m.refresh_from_db()
    assert m.clinic_role == nurse and m.role_name == "ممرضة"

    client.force_login(emp)
    nav = client.get(reverse("dashboard:home")).content.decode()
    assert reverse("clinical:desk") in nav and reverse("scheduling:appointments_day") not in nav
    assert "ممرضة" in nav  # shown under the user's name
    assert client.get(reverse("clinical:desk")).status_code == 200
    assert client.get(reverse("scheduling:appointments_day")).status_code == 403


def test_editing_a_role_changes_everyone_on_it_at_once(client, org_a, owner, make_member):
    roles = _roles(org_a)
    rec_role = roles["reception"]
    a = Membership.objects.get(user=make_member(org_a, "reception", email="a@example.com"))
    b = Membership.objects.get(user=make_member(org_a, "reception", email="b@example.com"))
    ensure_defaults(org_a)
    b.refresh_from_db()
    b.granted_permissions, b.revoked_permissions = ["report.finance"], ["messages.retry"]
    b.save()
    client.force_login(owner)
    perms = (set(rec_role.permissions) - {"payment.record", "messages.retry"}) | {"report.finance"}
    resp = _save_role(client, reverse("accounts:role_edit", args=[rec_role.pk]), "استقبال", perms)
    assert resp.status_code == 302
    for m in (a, b):
        m.refresh_from_db()
        assert "payment.record" not in effective_permissions(m)
        assert "report.finance" in effective_permissions(m)
    # b's own adjustments became redundant (the role now has / lacks them) and are dropped
    assert b.granted_permissions == [] and b.revoked_permissions == []
    client.force_login(a.user)
    assert client.get(reverse("billing:cashbox")).status_code == 403
    assert client.get(reverse("dashboard:reports")).status_code == 200


def test_rename_and_copy(client, org_a, owner):
    roles = _roles(org_a)
    client.force_login(owner)
    _save_role(client, reverse("accounts:role_edit", args=[roles["viewer"].pk]), "متدرب", roles["viewer"].permissions)
    roles["viewer"].refresh_from_db()
    assert roles["viewer"].name == "متدرب"

    page = client.get(reverse("accounts:role_new") + f"?from={roles['doctor'].pk}").content.decode()
    assert 'value="clinical.edit"' in page and "checked" in page.split('value="clinical.edit"', 1)[1][:200]
    _save_role(
        client, reverse("accounts:role_new"), "دكتور مساعد", set(roles["doctor"].permissions) - {"report.finance"}
    )
    copy = ClinicRole.objects.get(organization=org_a, name="دكتور مساعد")
    assert set(copy.permissions) == ROLE_PERMISSIONS[Role.DOCTOR] - {"report.finance"}


@pytest.mark.parametrize("name", ["استقبال", "  استقبال ", "مالك", ""])
def test_role_names_must_be_unique_and_not_the_owner(client, org_a, owner, name):
    _roles(org_a)
    client.force_login(owner)
    resp = _save_role(client, reverse("accounts:role_new"), name, {"appointment.view"})
    assert resp.status_code == 400
    assert ClinicRole.objects.filter(organization=org_a).count() == 4


def test_implied_permissions_are_added_to_a_role(client, org_a, owner):
    client.force_login(owner)
    _save_role(client, reverse("accounts:role_new"), "كاتب روشتات", {"prescription.write"})
    role = ClinicRole.objects.get(organization=org_a, name="كاتب روشتات")
    assert {"prescription.write", "prescription.print", "clinical.view"} <= set(role.permissions)


def test_delete_only_when_nobody_uses_it(client, org_a, owner, make_member):
    roles = _roles(org_a)
    client.force_login(owner)
    m = Membership.objects.get(user=make_member(org_a, "viewer"))
    ensure_defaults(org_a)
    resp = client.post(reverse("accounts:role_delete", args=[roles["viewer"].pk]), follow=True)
    assert "انقلهم لدور تاني" in resp.content.decode()
    assert ClinicRole.objects.filter(pk=roles["viewer"].pk).exists()

    m.clinic_role = roles["reception"]
    m.save()
    client.post(reverse("accounts:role_delete", args=[roles["viewer"].pk]))
    assert not ClinicRole.objects.filter(pk=roles["viewer"].pk).exists()


def test_add_a_user_on_a_custom_role(client, org_a, owner):
    client.force_login(owner)
    _save_role(client, reverse("accounts:role_new"), "محاسب", {"payment.record", "report.finance"})
    role = ClinicRole.objects.get(organization=org_a, name="محاسب")
    data = {"full_name": "Sara", "username": "sara", "password1": PASSWORD, "password2": PASSWORD, "role": role.pk}
    assert client.post(reverse("accounts:member_create"), data).status_code == 302
    m = Membership.objects.get(user__username="sara")
    assert "محاسب" in client.get(reverse("accounts:users")).content.decode()
    assert m.clinic_role == role
    assert effective_permissions(m) == {"payment.record", "report.finance"}


# ---------------------------------------------------------------- who may do what


def test_non_owner_only_within_their_own_permissions(client, org_a, make_member):
    roles = _roles(org_a)
    admin = make_member(org_a, "admin")
    client.force_login(admin)
    page = client.get(reverse("accounts:roles")).content.decode()
    assert reverse("accounts:role_edit", args=[roles["admin"].pk]) in page
    assert reverse("accounts:role_edit", args=[roles["doctor"].pk]) not in page  # has clinical permissions
    assert client.get(reverse("accounts:role_edit", args=[roles["doctor"].pk])).status_code == 403
    assert client.post(reverse("accounts:role_delete", args=[roles["doctor"].pk])).status_code == 403
    perms = set(roles["doctor"].permissions)
    resp = _save_role(client, reverse("accounts:role_edit", args=[roles["doctor"].pk]), "x", set())
    assert resp.status_code == 403
    roles["doctor"].refresh_from_db()
    assert set(roles["doctor"].permissions) == perms

    # a new role: the clinical boxes they don't have are simply not taken
    _save_role(client, reverse("accounts:role_new"), "سكرتارية", {"appointment.book", "clinical.edit"})
    sec = ClinicRole.objects.get(organization=org_a, name="سكرتارية")
    assert "clinical.edit" not in sec.permissions and "appointment.book" in sec.permissions
    # the owner choice and roles above them are not offered
    users = client.get(reverse("accounts:users")).content.decode()
    assert 'value="owner"' not in users and f'value="{roles["doctor"].pk}"' not in users


def test_other_clinics_roles_are_invisible(client, org_a, org_b, owner, make_member):
    theirs = _roles(org_b)["doctor"]
    client.force_login(owner)
    assert client.get(reverse("accounts:role_edit", args=[theirs.pk])).status_code == 404
    assert client.post(reverse("accounts:role_delete", args=[theirs.pk])).status_code == 404
    m = Membership.objects.get(user=make_member(org_a, "viewer"))
    client.post(reverse("accounts:membership_role", args=[m.pk]), {"role": str(theirs.pk)})
    m.refresh_from_db()
    assert m.clinic_role_id != theirs.pk
    page = client.get(reverse("accounts:roles")).content.decode()
    assert reverse("accounts:role_edit", args=[theirs.pk]) not in page


def test_roles_screen_needs_user_manage(client, org_a, make_member):
    client.force_login(make_member(org_a, "doctor"))
    assert client.get(reverse("accounts:roles")).status_code == 403
    assert client.post(reverse("accounts:role_new"), {"name": "x"}).status_code == 403


def test_role_changes_are_audited(client, org_a, owner):
    from apps.audit.models import AuditEvent

    client.force_login(owner)
    _save_role(client, reverse("accounts:role_new"), "ممرضة", {"vitals.record"})
    role = ClinicRole.objects.get(organization=org_a, name="ممرضة")
    _save_role(client, reverse("accounts:role_edit", args=[role.pk]), "ممرضة", {"vitals.record", "clinical.view"})
    client.post(reverse("accounts:role_delete", args=[role.pk]))
    actions = list(AuditEvent.objects.filter(action__startswith="role.").values_list("action", flat=True))
    assert sorted(actions) == ["role.created", "role.deleted", "role.updated"]
