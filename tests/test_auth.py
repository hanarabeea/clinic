from django.urls import reverse

from apps.accounts.models import User

PASSWORD = "Strong-Pass-2026!"


def test_login_and_logout(client, org_a, make_member):
    make_member(org_a, "reception", email="sales@example.com")
    resp = client.post(reverse("accounts:login"), {"username": "sales@example.com", "password": PASSWORD})
    assert resp.status_code == 302
    assert resp.url == reverse("dashboard:home")
    assert client.get(reverse("dashboard:home")).status_code == 200

    resp = client.post(reverse("accounts:logout"))
    assert resp.status_code == 302
    assert client.get(reverse("dashboard:home")).status_code == 302


def test_login_email_is_case_insensitive(client, org_a, make_member):
    make_member(org_a, "reception", email="sales@example.com")
    resp = client.post(reverse("accounts:login"), {"username": "Sales@Example.com", "password": PASSWORD})
    assert resp.status_code == 302


def test_wrong_password_shows_error(client, org_a, make_member):
    make_member(org_a, "reception", email="sales@example.com")
    resp = client.post(reverse("accounts:login"), {"username": "sales@example.com", "password": "wrong-pass"})
    assert resp.status_code == 200
    assert "غلط" in resp.content.decode()


def test_lockout_after_five_failures(client, org_a, make_member):
    make_member(org_a, "reception", email="sales@example.com")
    for _ in range(5):
        client.post(reverse("accounts:login"), {"username": "sales@example.com", "password": "nope"})
    resp = client.post(reverse("accounts:login"), {"username": "sales@example.com", "password": PASSWORD})
    assert resp.status_code == 429
    assert "مقفول" in resp.content.decode()


def test_anonymous_is_redirected_to_login(client, db):
    resp = client.get(reverse("dashboard:home"))
    assert resp.status_code == 302
    assert resp.url.startswith(reverse("accounts:login"))


def test_passwords_are_hashed_with_argon2(settings, db):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
    user = User.objects.create_user(email="x@example.com", password=PASSWORD, full_name="X")
    assert user.password.startswith("argon2")


def test_user_without_membership_gets_403(client, db):
    client.force_login(User.objects.create_user(email="lonely@example.com", password=PASSWORD, full_name="Lonely"))
    assert client.get(reverse("dashboard:home")).status_code == 403


def test_inactive_membership_gets_403(client, org_a, make_member):
    user = make_member(org_a, "reception")
    user.memberships.update(is_active=False)
    client.force_login(user)
    assert client.get(reverse("dashboard:home")).status_code == 403


def test_lockout_counts_email_case_insensitively(client, org_a, make_member):
    make_member(org_a, "reception", email="sales@example.com")
    for i in range(5):
        email = "SALES@example.com" if i % 2 else "sales@example.com"
        client.post(reverse("accounts:login"), {"username": email, "password": "nope"})
    resp = client.post(reverse("accounts:login"), {"username": "sales@example.com", "password": PASSWORD})
    assert resp.status_code == 429


def test_clinic_colours_apply_to_the_whole_interface(client, org_a, make_member):
    """Settings → الألوان used to change only the printed prescription; now every page uses them."""
    from apps.organizations.models import OrganizationSettings

    OrganizationSettings.objects.filter(organization=org_a).update(primary_color="#246770", dark_color="#1B4F56",
                                                                   neutral_color="#3EA0BC")  # fmt: skip
    client.force_login(make_member(org_a, "owner"))
    html = client.get("/").content.decode()
    assert "--color-brand: #246770" in html and "--color-brand-dark: #1B4F56" in html
    assert "color-mix(in srgb, #3EA0BC 18%, white)" in html  # neutral only as a light background tint


def test_clinic_logo_is_the_browser_tab_icon(client, org_a, make_member, tmp_path, settings):
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    settings.MEDIA_ROOT = str(tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "teal").save(buf, "PNG")
    s = org_a.settings
    s.logo = SimpleUploadedFile("logo.png", buf.getvalue(), content_type="image/png")
    s.save()
    client.force_login(make_member(org_a, "owner"))
    html = client.get("/").content.decode()
    assert '<link rel="icon" href="/org/logo/?v=' in html
    assert client.get("/org/logo/").status_code == 200


def _with_logo(org, tmp_path, settings):
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    settings.MEDIA_ROOT = str(tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "teal").save(buf, "PNG")
    s = org.settings
    s.logo = SimpleUploadedFile("logo.png", buf.getvalue(), content_type="image/png")
    s.clinic_name_ar = "عيادة النور"
    s.primary_color = "#246770"
    s.save()


def test_login_page_wears_the_clinic_brand(client, org_a, tmp_path, settings):
    """A single-clinic install: the sign-in pages show the clinic's colours, name and logo before anyone signs in."""
    from apps.organizations.models import Organization

    # the test DB is shared with module fixtures that keep their own clinic alive (voice robustness): only org_a here
    Organization.objects.exclude(pk=org_a.pk).update(is_active=False)
    _with_logo(org_a, tmp_path, settings)
    for name in ("accounts:login", "accounts:password_reset"):
        html = client.get(reverse(name)).content.decode()
        assert "--color-brand: #246770" in html and "عيادة النور" in html, name
        assert reverse("organizations:public_logo") in html, name
    resp = client.get(reverse("organizations:public_logo"))
    assert resp.status_code == 200 and resp["Content-Type"] == "image/png"


def test_shared_server_shows_no_clinic_before_sign_in(client, org_a, org_b, tmp_path, settings):
    """Two clinics on one server: the sign-in page stays neutral, and no clinic's logo is served publicly."""
    _with_logo(org_a, tmp_path, settings)
    html = client.get(reverse("accounts:login")).content.decode()
    assert "عيادة النور" not in html and "--color-brand: #246770" not in html
    assert reverse("organizations:public_logo") not in html
    assert client.get(reverse("organizations:public_logo")).status_code == 404


def test_public_logo_404_without_a_logo(client, org_a):
    assert client.get(reverse("organizations:public_logo")).status_code == 404
