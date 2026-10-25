"""A user of org B must never see or change anything in org A, even with direct IDs in the URL."""

from django.urls import reverse

from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.organizations.models import Membership


def test_admin_of_b_cannot_toggle_member_of_a(client, org_a, org_b, make_member):
    victim = make_member(org_a, "reception")
    client.force_login(make_member(org_b, "owner"))
    m = Membership.objects.get(user=victim)
    assert client.post(reverse("accounts:membership_toggle", args=[m.pk])).status_code == 404
    m.refresh_from_db()
    assert m.is_active


def test_admin_of_b_cannot_change_role_in_a(client, org_a, org_b, make_member):
    victim = make_member(org_a, "reception")
    client.force_login(make_member(org_b, "owner"))
    m = Membership.objects.get(user=victim)
    resp = client.post(reverse("accounts:membership_role", args=[m.pk]), {"role": "admin"})
    assert resp.status_code == 404
    m.refresh_from_db()
    assert m.role == "reception"


def test_users_page_lists_only_own_org(client, org_a, org_b, make_member):
    make_member(org_a, "reception", email="a-person@example.com")
    client.force_login(make_member(org_b, "owner"))
    html = client.get(reverse("accounts:users")).content.decode()
    assert "a-person@example.com" not in html


def test_settings_edit_only_touches_own_org(client, org_a, org_b, make_member):
    client.force_login(make_member(org_b, "owner"))
    data = {
        "org-name_ar": "اسم جديد",
        "org-name_en": "",
        "clinic_name_ar": "",
        "clinic_name_en": "",
        "address_ar": "",
        "map_link": "",
        "working_hours_text": "",
        "phones_text": "",
        "primary_color": "#AE171C",
        "dark_color": "#111111",
        "neutral_color": "#F5F2EF",
    }
    assert client.post(reverse("organizations:settings"), data).status_code == 302
    org_a.refresh_from_db()
    org_b.refresh_from_db()
    assert org_a.name_ar == "عيادة ديمو"
    assert org_b.name_ar == "اسم جديد"


def test_for_org_scopes_querysets(org_a, org_b):
    audit.log("settings.company", org=org_a, summary="x")
    assert AuditEvent.objects.for_org(org_a).count() == 1
    assert AuditEvent.objects.for_org(org_b).count() == 0
    assert AuditEvent.objects.for_org(None).count() == 0


def test_logo_is_private(client, org_a, db):
    assert client.get(reverse("organizations:logo")).status_code == 302
