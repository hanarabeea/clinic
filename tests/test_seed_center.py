"""`seed_center`: a month of a center's life made through the real services. The proof that everything built in
phases 13 to 19 works together: after it, the books verify, the statements balance, and every money screen opens."""

import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from apps.inventory import services as stock
from apps.invoicing.models import Invoice
from apps.ledger import services as ledger
from apps.ledger import statements, verify
from apps.notifications.models import ScheduledMessage
from apps.organizations.models import Organization
from apps.purchasing.models import PurchaseInvoice, SupplierPayment
from apps.theatre.models import Operation
from apps.treasury.models import Cheque, Voucher


@pytest.fixture
def seeded(db, make_member):
    out = io.StringIO()
    call_command("seed_org", stdout=out)
    org = Organization.objects.get(slug="demo-clinic")
    owner = make_member(org, "owner", email="seed-owner@example.com")
    call_command("seed_notifications", org=org.slug, stdout=out)
    call_command("seed_center", org=org.slug, days=12, stdout=out)
    return org, owner, out.getvalue()


def test_a_seeded_month_verifies_and_every_money_screen_opens(client, seeded):
    org, owner, output = seeded
    assert "books verified" in output
    assert verify.verify(org) == []
    # it really did the work: visits invoiced, stock bought and paid for, cheques, operations, vouchers
    assert Invoice.objects.filter(organization=org).count() > 40
    assert PurchaseInvoice.objects.filter(organization=org).exists()
    assert (
        SupplierPayment.objects.filter(organization=org).exists() and Cheque.objects.filter(organization=org).exists()
    )
    assert Operation.objects.filter(organization=org, status="done").exists()
    assert Voucher.objects.filter(organization=org, template__isnull=False).exists()  # rent / salaries
    # nothing it queued will ever be sent
    assert not ScheduledMessage.objects.filter(organization=org, status="pending").exists()

    today = ledger.today()
    start = today - timedelta(days=40)
    sheet = statements.balance_sheet(org, today)
    income = statements.income_statement(org, start, today)
    assert sheet.balanced and income.revenue.total > 0 and income.expenses.total > 0
    assert sheet.result == statements.income_statement(org, start.replace(year=2000), today).net
    assert ledger.balance_of(ledger_account(org, "inventory")) == stock.stock_value(org) > 0

    client.force_login(owner)
    names = [
        "ledger:trial_balance", "ledger:income_statement", "ledger:balance_sheet", "ledger:journal", "ledger:verify",
        "treasury:home", "treasury:vouchers", "treasury:cheques", "treasury:expenses", "invoicing:list",
        "invoicing:accounts", "invoicing:doctors", "invoicing:payers", "purchasing:invoices", "purchasing:suppliers",
        "purchasing:aging", "purchasing:payments", "purchasing:taxes", "inventory:items", "inventory:docs",
        "inventory:counts", "invoicing:report_revenue", "invoicing:report_operations", "invoicing:report_receivables",
        "invoicing:report_cash", "dashboard:reports", "billing:cashbox", "theatre:board", "theatre:admissions",
    ]  # fmt: skip
    for name in names:
        resp = client.get(reverse(name), {"from": f"{start:%Y-%m-%d}", "to": f"{today:%Y-%m-%d}"})
        assert resp.status_code == 200, name

    with pytest.raises(CommandError, match="already seeded"):
        call_command("seed_center", org=org.slug, days=3, stdout=io.StringIO())


def ledger_account(org, key):
    from apps.ledger import chart

    return chart.account(org, key)


def test_seed_center_needs_an_owner(db):
    call_command("seed_org", stdout=io.StringIO())
    with pytest.raises(CommandError, match="owner"):
        call_command("seed_center", org="demo-clinic", days=2, stdout=io.StringIO())
    assert timezone.now()  # the clock was not left frozen
