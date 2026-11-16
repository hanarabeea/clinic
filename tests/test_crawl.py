"""Whole-app smoke crawl: a fully populated clinic (seed_org + drugs + 30 patients + a simulated day), then every role
follows every link and every hx-get it can reach. No page may crash (500), and pages a role can't use must answer
403/404/redirect — never a server error. Catches broken templates, missing context, bad URLs after refactors."""

import io
import re
from collections import deque
from urllib.parse import urldefrag, urlparse

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.usefixtures("midday")

ROLES = ("owner", "doctor", "reception", "admin", "viewer")
LINK = re.compile(r'(?:href|hx-get|data-drawer)="(/[^"]*)"')
# report exports (?format=xlsx|pdf) are not followed: every report links two per date range, and each print renders a
# PDF. They have their own tests (test_treasury.test_reports_export_to_excel_and_pdf).
SKIP = re.compile(
    r"logout|/pdf/|\.pdf|export|calibration|/static/|/media/|password-reset|/admin/|/lock/|healthz|format=(?:xlsx|pdf)"
    r"|[?&]from=\d{4}"  # a report's date presets: the same page again for another period
)
# pages that exist once per account of the chart (about ninety of each, one template): a few are enough
FANOUT = re.compile(r"^/books/accounts/\d+/(?:edit/)?$")
FANOUT_CAP = 3


def _over_fanout(url, shapes) -> bool:
    path = urlparse(url).path
    if not FANOUT.match(path):
        return False
    shape = re.sub(r"\d+", "N", path)
    shapes[shape] = shapes.get(shape, 0) + 1
    return shapes[shape] > FANOUT_CAP


MAX_PAGES = 700


@pytest.fixture
def clinic(db, make_member):
    from apps.organizations.models import Organization

    out = io.StringIO()
    call_command("seed_org", stdout=out)
    org = Organization.objects.get(slug="demo-clinic")
    for cmd in ("seed_notifications", "seed_services", "import_drugs"):
        call_command(cmd, org=org.slug, stdout=out)
    call_command("seed_demo", org=org.slug, stdout=out)
    call_command("seed_pregnancies", org=org.slug, stdout=out)  # the OB/GYN layer has something to show
    call_command("simulate_day", org=org.slug, stdout=out)
    return {role: make_member(org, role, email=f"crawl-{role}@example.com") for role in ROLES}


def _crawl(client, start="/"):
    seen, queue, errors, ok, shapes = set(), deque([start]), [], set(), {}
    while queue and len(seen) < MAX_PAGES:
        url = urldefrag(queue.popleft())[0]
        if url in seen or SKIP.search(url) or _over_fanout(url, shapes):
            continue
        seen.add(url)
        try:
            resp = client.get(url, HTTP_HX_REQUEST="true" if "hx" in url else None, follow=False)
        except Exception as e:  # a template/view crash
            errors.append((url, repr(e)[:200]))
            continue
        if resp.status_code >= 500:
            errors.append((url, resp.status_code))
            continue
        if resp.status_code in (301, 302) and resp.get("Location", "").startswith("/"):
            queue.append(resp["Location"])
        if resp.status_code == 200:
            ok.add(url)
        ctype = resp.get("Content-Type", "")
        if resp.status_code == 200 and "text/html" in ctype:
            for link in LINK.findall(resp.content.decode(errors="ignore")):
                link = link.replace("&amp;", "&")
                if urlparse(link).path and "{" not in link:
                    queue.append(link)
    return seen, errors, ok


@pytest.mark.parametrize("role", ROLES)
def test_every_reachable_page_renders(client, clinic, role):
    client.force_login(clinic[role])
    seen, errors, ok = _crawl(client)
    print(f"[crawl] {role}: {len(seen)} pages")
    assert len(seen) > 5, seen
    assert errors == [], errors
    if role in ("reception", "viewer", "admin"):  # no medical record pages for non-clinical roles
        assert not [u for u in ok if u.startswith("/desk/")], sorted(u for u in ok if u.startswith("/desk/"))


def test_the_books_verify_after_a_seeded_and_simulated_day(clinic):
    """Everything the seeds and the simulated day did (payments, an emergency, services, an operation, an admission)
    went through the real services: the books must be sound afterwards."""
    from apps.ledger import verify
    from apps.organizations.models import Organization

    assert verify.verify(Organization.objects.get(slug="demo-clinic")) == []
