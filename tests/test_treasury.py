"""Treasury and the cashier front end (plan 14 §14.5): payments land in a treasury and post, vouchers, shifts, cash
counts, and the proof that the cashbox and the ledger are the same numbers."""

import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from openpyxl import load_workbook

from apps.billing import services as billing
from apps.billing.models import Payment, PaymentMethod
from apps.billing.posting import post_payment
from apps.doctors.models import Doctor, VisitType, WorkingPeriod
from apps.ledger import chart, reports
from apps.ledger import services as ledger
from apps.ledger.bootstrap import bootstrap
from apps.ledger.models import Account, EntryStatus, FiscalPeriod, JournalEntry
from apps.organizations.models import ClinicRole, Membership, Role
from apps.patients import services as patient_services
from apps.patients.models import Gender, Patient
from apps.scheduling import services as booking
from apps.treasury import services as treasury
from apps.treasury.models import (
    CashCount,
    CountKind,
    Shift,
    ShiftStatus,
    Treasury,
    VarianceStatus,
    Voucher,
    VoucherKind,
)

D = Decimal
NOW = "2026-03-10 12:00:00+02:00"
TODAY = date(2026, 3, 10)
FINANCE = ["ledger.view", "ledger.post", "ledger.close_period", "accounts.manage", "treasury.view", "treasury.operate",
           "treasury.count"]  # fmt: skip

pytestmark = pytest.mark.usefixtures("clock")


@pytest.fixture
def clock():
    with freeze_time(NOW, tick=True):
        yield


@pytest.fixture
def world(org_a, org_b, make_member):
    doctor = Doctor.objects.create(organization=org_a, name_ar="سارة", slot_minutes=10)
    for weekday in range(7):
        WorkingPeriod.objects.create(
            organization=org_a, doctor=doctor, weekday=weekday, start_time="00:00", end_time="23:59"
        )
    vt = VisitType.objects.create(organization=org_a, doctor=doctor, name_ar="كشف", duration_minutes=10, price=D("400"))
    reception = make_member(org_a, "reception")
    accountant = make_member(org_a, "viewer", email="accountant@example.com")
    role = ClinicRole.objects.create(organization=org_a, name="محاسب", permissions=FINANCE)
    Membership.objects.filter(user=accountant).update(role=Role.MEMBER, clinic_role=role)
    return {
        "org": org_a,
        "doctor": doctor,
        "vt": vt,
        "reception": reception,
        "accountant": accountant,
        "owner": make_member(org_a, "owner"),
        "other_owner": make_member(org_b, "owner"),
        "other_org": org_b,
        "n": 0,
    }


def _appt(world, price=None):
    world["n"] += 1
    n = world["n"]
    patient = patient_services.create_patient(
        world["org"], Patient(full_name=f"مريضة {n}", phone=f"+2010011{n:05d}", gender=Gender.FEMALE)
    )
    start = timezone.now().replace(second=0, microsecond=0) + timedelta(minutes=10 * n)
    appt = booking.book(
        patient=patient, doctor=world["doctor"], visit_type=world["vt"], by=world["reception"], start_at=start,
        overbook=True, notify=False,
    )  # fmt: skip
    if price is not None:
        appt.price = price
        appt.save(update_fields=["price"])
    return appt


def _bal(org, key):
    return ledger.balance_of(chart.account(org, key))


def _lines(entry):
    return [(line.account.system_key, line.debit, line.credit) for line in entry.lines.order_by("line_no")]


# ================================================================ the treasuries


def test_every_clinic_starts_with_its_treasuries(org_a, org_b):
    treasury.ensure(org_a)
    treasury.ensure(org_a)
    rows = {t.system_key: t for t in Treasury.objects.filter(organization=org_a)}
    assert set(rows) == {"reception_drawer", "main_safe", "bank_main", "card_clearing", "instapay", "wallet"}
    assert {t.account.system_key for t in rows.values()} == {
        "cash_reception", "cash_main", "bank_main", "card_clearing", "instapay_clearing", "wallet_clearing",
    }  # fmt: skip
    assert treasury.for_method(org_a, PaymentMethod.CASH) == rows["reception_drawer"]
    assert treasury.for_method(org_a, PaymentMethod.CARD) == rows["card_clearing"]
    assert rows["reception_drawer"].holds_cash and not rows["bank_main"].holds_cash
    assert not Treasury.objects.filter(organization=org_b).exists()


# ================================================================ payments post


def test_a_cash_payment_lands_in_the_drawer_and_posts(world):
    org, appt = world["org"], _appt(world)
    payment = billing.record_payment(appt, amount="400", by=world["reception"])
    drawer = treasury.treasury(org, "reception_drawer")
    assert payment.treasury == drawer and payment.amount == D("400.00")
    entry = ledger.entry_for(payment)
    assert _lines(entry) == [
        ("cash_reception", D("400.00"), D("0.00")),
        ("patients_receivable", D("0.00"), D("400.00")),
    ]
    assert (entry.date, entry.created_by, entry.status) == (TODAY, world["reception"], EntryStatus.POSTED)
    credit_line = entry.lines.get(credit__gt=0)
    assert (credit_line.party_type, credit_line.party_id) == ("patient", appt.patient_id)
    assert treasury.balance(drawer) == D("400.00")
    assert ledger.balance_of(chart.account(org, "patients_receivable"), party=appt.patient) == D("-400.00")
    # taking the first money opened the cashier's shift
    shift = treasury.current_shift(drawer)
    assert shift.opened_by == world["reception"] and shift.opening_balance == 0
    assert post_payment(payment) == entry and JournalEntry.objects.filter(organization=org).count() == 1  # once


def test_refunds_and_other_ways_of_paying(world):
    org = world["org"]
    appt = _appt(world)
    billing.record_payment(appt, amount=400, by=world["reception"])
    refund = billing.record_payment(appt, amount=150, by=world["reception"], is_refund=True)
    assert _lines(ledger.entry_for(refund)) == [
        ("patients_receivable", D("150.00"), D("0.00")),
        ("cash_reception", D("0.00"), D("150.00")),
    ]
    assert _bal(org, "cash_reception") == D("250.00")

    card = billing.record_payment(_appt(world), amount=400, method=PaymentMethod.CARD, by=world["reception"])
    assert card.treasury.system_key == "card_clearing" and _bal(org, "card_clearing") == D("400.00")
    insta = billing.record_payment(_appt(world), amount=400, method=PaymentMethod.INSTAPAY, by=world["reception"])
    wallet = billing.record_payment(_appt(world), amount=400, method=PaymentMethod.WALLET, by=world["reception"])
    assert (insta.treasury.system_key, wallet.treasury.system_key) == ("instapay", "wallet")
    assert Shift.objects.count() == 1  # only cash opens a cashier's shift

    # a cash refund can't take out more cash than the drawer holds
    paid_by_card = _appt(world)
    billing.record_payment(paid_by_card, amount=400, method=PaymentMethod.CARD, by=world["reception"])
    with pytest.raises(billing.PaymentError, match="مفيش كاش كفاية"):
        billing.record_payment(paid_by_card, amount=300, by=world["reception"], is_refund=True)
    assert _bal(org, "cash_reception") == D("250.00")


def test_a_payment_cannot_go_into_a_closed_month(world):
    org, appt = world["org"], _appt(world)
    ledger.fiscal_year_for(org, TODAY)
    FiscalPeriod.objects.filter(organization=org).update(status="closed")
    with pytest.raises(billing.PaymentError, match="مقفول"):
        billing.record_payment(appt, amount=400, by=world["reception"])
    assert not Payment.objects.exists() and not JournalEntry.objects.exists()  # the payment is not half-saved


def test_cashbox_and_ledger_are_the_same_numbers(world):
    org = world["org"]
    for method, amounts in [("cash", [400, 250, 100]), ("card", [400, 400]), ("instapay", [75]), ("wallet", [20])]:
        for amount in amounts:
            billing.record_payment(_appt(world), amount=amount, method=method, by=world["reception"])
    paid = _appt(world)
    billing.record_payment(paid, amount=400, by=world["reception"])
    billing.record_payment(paid, amount="99.50", by=world["reception"], is_refund=True)

    box = billing.cashbox(org, TODAY)
    by_label = dict(box.by_method)
    labels = dict(PaymentMethod.choices)
    assert by_label[labels["cash"]] == _bal(org, "cash_reception") == D("1050.50")
    assert by_label[labels["card"]] == _bal(org, "card_clearing") == D("800.00")
    assert by_label[labels["instapay"]] == _bal(org, "instapay_clearing") == D("75.00")
    assert by_label[labels["wallet"]] == _bal(org, "wallet_clearing") == D("20.00")
    assert box.total == D("1945.50") == -_bal(org, "patients_receivable")  # what patients paid is credited to them
    assert reports.trial_balance(org, TODAY, TODAY).balanced
    assert box.drawer.system_key == "reception_drawer" and box.shift is not None


def test_bootstrap_posts_the_payments_that_came_before_the_ledger(world):
    """A clinic upgraded from before phase 14: payments exist with no treasury and no entry."""
    org = world["org"]
    old = []
    for i, (amount, method, refund) in enumerate([(400, "cash", False), (300, "card", False), (100, "cash", True)]):
        appt = _appt(world)
        old.append(
            Payment.objects.create(
                organization=org,
                patient=appt.patient,
                appointment=appt,
                amount=amount,
                method=method,
                is_refund=refund,
                received_at=timezone.now() - timedelta(days=40 - i),
            )
        )
    assert bootstrap(org) == 3 and bootstrap(org) == 0  # safe to run again
    entries = list(JournalEntry.objects.filter(organization=org).order_by("number"))
    assert [e.source_id for e in entries] == [p.pk for p in old]  # numbered in the order the money came in
    assert [e.date for e in entries] == [date(2026, 1, 29), date(2026, 1, 30), date(2026, 1, 31)]
    for payment in old:
        payment.refresh_from_db()
        assert payment.treasury is not None
    assert (_bal(org, "cash_reception"), _bal(org, "card_clearing")) == (D("300.00"), D("300.00"))
    assert Treasury.objects.filter(organization=org).count() == 6 and Account.objects.filter(organization=org).exists()
    assert not Shift.objects.exists()  # old money opens no shift


# ================================================================ vouchers


def _voucher(world, kind, amount, **kw):
    org = world["org"]
    defaults = {
        "treasury": treasury.treasury(org, "main_safe"),
        "date": TODAY,
        "description": "بيان",
        "by": world["accountant"],
    }
    return treasury.create_voucher(org, kind=kind, amount=amount, **{**defaults, **kw})


def test_receipt_payment_and_transfer_vouchers_post_exactly(world):
    org = world["org"]
    receipt = _voucher(world, VoucherKind.RECEIPT, "5000", counter_account=chart.account(org, "capital"))
    assert (receipt.number, receipt.fiscal_year.name) == (1, "2026")
    assert _lines(ledger.entry_for(receipt)) == [
        ("cash_main", D("5000.00"), D("0.00")),
        ("capital", D("0.00"), D("5000.00")),
    ]

    rent = _voucher(
        world, VoucherKind.PAYMENT, "1200.50", counter_account=chart.account(org, "rent"),
        cost_center=chart.cost_center(org, "admin"), description="إيجار مارس", party_name="المالك",
    )  # fmt: skip
    entry = ledger.entry_for(rent)
    assert _lines(entry) == [("rent", D("1200.50"), D("0.00")), ("cash_main", D("0.00"), D("1200.50"))]
    assert entry.lines.get(debit__gt=0).cost_center.system_key == "admin" and "إيجار مارس" in entry.memo
    assert rent.number == 1  # each kind has its own numbers

    bank = treasury.treasury(org, "bank_main")
    move = _voucher(world, VoucherKind.TRANSFER, 2000, to_treasury=bank)
    assert _lines(ledger.entry_for(move)) == [
        ("bank_main", D("2000.00"), D("0.00")),
        ("cash_main", D("0.00"), D("2000.00")),
    ]
    assert (_bal(org, "cash_main"), _bal(org, "bank_main")) == (D("1799.50"), D("2000.00"))
    assert _voucher(world, VoucherKind.RECEIPT, 1, counter_account=chart.account(org, "capital")).number == 2


def test_voucher_rules(world):
    org = world["org"]
    safe, bank = treasury.treasury(org, "main_safe"), treasury.treasury(org, "bank_main")
    rent = chart.account(org, "rent")
    with pytest.raises(treasury.TreasuryError, match="رصيد كفاية"):  # there is no such thing as minus banknotes
        _voucher(world, VoucherKind.PAYMENT, 10, counter_account=rent, cost_center=chart.cost_center(org, "admin"))
    with pytest.raises(treasury.TreasuryError, match="رصيد كفاية"):
        _voucher(world, VoucherKind.TRANSFER, 10, to_treasury=bank)
    _voucher(world, VoucherKind.PAYMENT, 10, treasury=bank, counter_account=rent,
             cost_center=chart.cost_center(org, "admin"))  # a bank account may be overdrawn  # fmt: skip
    with pytest.raises(treasury.TreasuryError, match="مركز تكلفة"):
        _voucher(world, VoucherKind.PAYMENT, 10, treasury=bank, counter_account=rent)
    with pytest.raises(treasury.TreasuryError, match="تحويل"):
        _voucher(world, VoucherKind.RECEIPT, 10, counter_account=bank.account)
    with pytest.raises(treasury.TreasuryError, match="أكبر من صفر"):
        _voucher(world, VoucherKind.RECEIPT, 0, counter_account=chart.account(org, "capital"))
    with pytest.raises(treasury.TreasuryError, match="رايحة لها"):
        _voucher(world, VoucherKind.TRANSFER, 10, to_treasury=safe)
    with pytest.raises(treasury.TreasuryError, match="البيان"):
        _voucher(world, VoucherKind.RECEIPT, 10, counter_account=chart.account(org, "capital"), description=" ")
    treasury.ensure(world["other_org"])
    with pytest.raises(treasury.TreasuryError, match="مش متاحة"):
        _voucher(world, VoucherKind.RECEIPT, 10, treasury=treasury.treasury(world["other_org"], "main_safe"),
                 counter_account=chart.account(org, "capital"))  # fmt: skip
    assert Voucher.objects.count() == 1 and JournalEntry.objects.count() == 1  # refused ones left nothing, no number


def test_cancelling_a_voucher_reverses_it_and_keeps_both(world):
    org = world["org"]
    receipt = _voucher(world, VoucherKind.RECEIPT, 900, counter_account=chart.account(org, "capital"))
    spent = _voucher(world, VoucherKind.PAYMENT, 800, counter_account=chart.account(org, "rent"),
                     cost_center=chart.cost_center(org, "admin"))  # fmt: skip
    with pytest.raises(treasury.TreasuryError, match="رصيد كفاية"):  # the 900 can't go back out: 800 of it was spent
        treasury.cancel_voucher(receipt, by=world["accountant"])
    treasury.cancel_voucher(spent, by=world["accountant"], reason="اتسجل مرتين")
    spent.refresh_from_db()
    assert spent.is_cancelled and spent.cancel_reason == "اتسجل مرتين" and spent.cancelled_by == world["accountant"]
    original, mirror = ledger.entry_for(spent), ledger.entry_for(spent, "cancel")
    assert mirror.reverses == original and _lines(mirror) == [
        ("rent", D("0.00"), D("800.00")),
        ("cash_main", D("800.00"), D("0.00")),
    ]
    assert (_bal(org, "cash_main"), _bal(org, "rent")) == (D("900.00"), D("0.00"))
    treasury.cancel_voucher(spent, by=world["accountant"])  # asking twice changes nothing
    assert JournalEntry.objects.count() == 3 and Voucher.objects.count() == 2


# ================================================================ shifts and counts


def test_closing_a_shift_with_an_exact_count(world):
    org = world["org"]
    for amount in (400, 250):
        billing.record_payment(_appt(world), amount=amount, by=world["reception"])
    drawer = treasury.treasury(org, "reception_drawer")
    shift = treasury.current_shift(drawer)
    count = treasury.close_shift(shift, denominations={"200": 3, "50": 1}, by=world["reception"], keep=100)
    assert (count.counted_total, count.expected_total, count.difference) == (D("650.00"), D("650.00"), D("0.00"))
    assert count.variance_status == VarianceStatus.NONE and count.kind == CountKind.SHIFT_CLOSE
    assert count.denominations == {"200": 3, "50": 1}
    shift.refresh_from_db()
    assert (shift.status, shift.closed_by, shift.handed_over) == (ShiftStatus.CLOSED, world["reception"], D("550.00"))
    handover = Voucher.objects.get(kind=VoucherKind.TRANSFER)
    assert (handover.treasury, handover.to_treasury.system_key, handover.amount) == (drawer, "main_safe", D("550.00"))
    assert (_bal(org, "cash_reception"), _bal(org, "cash_main")) == (D("100.00"), D("550.00"))
    with pytest.raises(treasury.TreasuryError, match="مقفولة"):
        treasury.close_shift(shift, denominations={}, by=world["reception"])
    # the next money opens a new shift that starts from the float left in the drawer
    billing.record_payment(_appt(world), amount=400, by=world["reception"])
    assert treasury.current_shift(drawer).opening_balance == D("100.00") and Shift.objects.count() == 2


def test_a_cash_shortage_waits_for_someone_else(world):
    org = world["org"]
    billing.record_payment(_appt(world), amount=400, by=world["reception"])
    shift = treasury.current_shift(treasury.treasury(org, "reception_drawer"))
    count = treasury.close_shift(shift, denominations={"100": 3, "50": 1, "20": 2}, by=world["reception"])
    assert (count.counted_total, count.difference, count.variance_status) == (
        D("390.00"),
        D("-10.00"),
        VarianceStatus.PENDING,
    )
    # all the cash went to the safe; the missing 10 still sits on the drawer's account until it is approved
    assert (_bal(org, "cash_main"), _bal(org, "cash_reception"), _bal(org, "cash_over_short")) == (
        D("390.00"),
        D("10.00"),
        0,
    )

    # while it waits, the missing 10 is not "cash in the drawer": it is not expected again and cannot be spent
    drawer = treasury.treasury(org, "reception_drawer")
    assert (treasury.balance(drawer), treasury.pending_difference(drawer), treasury.cash_on_hand(drawer)) == (
        D("10.00"), D("-10.00"), D("0.00"),
    )  # fmt: skip
    billing.record_payment(_appt(world), amount=200, by=world["reception"])
    second = treasury.close_shift(treasury.current_shift(drawer), denominations={"200": 1}, by=world["reception"])
    assert (second.expected_total, second.difference, second.variance_status) == (D("200.00"), 0, VarianceStatus.NONE)
    assert billing.cashbox(org, TODAY).in_drawer == 0
    with pytest.raises(billing.PaymentError, match="مفيش كاش كفاية"):
        billing.record_payment(count.shift.treasury.payments.first().appointment, amount=5, by=world["reception"],
                               is_refund=True)  # fmt: skip

    cashier = Membership.objects.get(user=world["reception"])
    with pytest.raises(treasury.TreasuryError, match="حد تاني"):
        treasury.approve_variance(count, by=world["reception"], membership=cashier)
    accountant = Membership.objects.get(user=world["accountant"])
    treasury.approve_variance(count, by=world["accountant"], membership=accountant)
    count.refresh_from_db()
    assert count.variance_status == VarianceStatus.APPROVED and count.approved_by == world["accountant"]
    entry = ledger.entry_for(count)
    assert _lines(entry) == [("cash_over_short", D("10.00"), D("0.00")), ("cash_reception", D("0.00"), D("10.00"))]
    assert entry.lines.get(debit__gt=0).cost_center.system_key == "admin"
    assert (_bal(org, "cash_reception"), _bal(org, "cash_over_short")) == (0, D("10.00"))
    with pytest.raises(treasury.TreasuryError, match="مفيش فرق"):
        treasury.approve_variance(count, by=world["accountant"], membership=accountant)


def test_cash_over_and_the_owner_approving_her_own_count(world):
    org = world["org"]
    billing.record_payment(_appt(world), amount=400, by=world["owner"])
    shift = treasury.current_shift(treasury.treasury(org, "reception_drawer"))
    count = treasury.close_shift(shift, denominations={"200": 2, "5": 1}, by=world["owner"])
    assert count.difference == D("5.00")
    assert (_bal(org, "cash_main"), _bal(org, "cash_reception")) == (D("405.00"), D("-5.00"))
    treasury.approve_variance(count, by=world["owner"], membership=Membership.objects.get(user=world["owner"]))
    assert _lines(ledger.entry_for(count)) == [
        ("cash_reception", D("5.00"), D("0.00")),
        ("cash_over_short", D("0.00"), D("5.00")),
    ]
    assert (_bal(org, "cash_reception"), _bal(org, "cash_over_short")) == (0, D("-5.00"))


def test_surprise_count_records_and_moves_nothing(world):
    org = world["org"]
    billing.record_payment(_appt(world), amount=400, by=world["reception"])
    drawer = treasury.treasury(org, "reception_drawer")
    before = JournalEntry.objects.count()
    count = treasury.surprise_count(drawer, denominations={"200": 1, "100": 1, "0.5": 3}, by=world["accountant"],
                                    witness_name="أ. منى")  # fmt: skip
    assert (count.kind, count.counted_total, count.expected_total, count.difference) == (
        CountKind.SURPRISE, D("301.50"), D("400.00"), D("-98.50"),
    )  # fmt: skip
    assert count.witness_name == "أ. منى" and count.shift is None and count.variance_status == VarianceStatus.NONE
    assert JournalEntry.objects.count() == before and treasury.current_shift(drawer) is not None
    with pytest.raises(treasury.TreasuryError, match="فئة مش معروفة"):
        treasury.surprise_count(drawer, denominations={"3": 1}, by=world["accountant"])
    with pytest.raises(treasury.TreasuryError, match="بالسالب"):
        treasury.count_total({"200": -1})


# ================================================================ screens


def test_finance_screens_open_for_the_accountant_only(client, world):
    org = world["org"]
    billing.record_payment(_appt(world), amount=400, by=world["reception"])
    voucher = _voucher(world, VoucherKind.RECEIPT, 1000, counter_account=chart.account(org, "capital"),
                       description="إيداع رأس مال العيادة الأولى")  # fmt: skip
    entry = ledger.entry_for(voucher)
    safe = treasury.treasury(org, "main_safe")
    pages = [
        reverse("ledger:trial_balance"), reverse("ledger:journal"), reverse("ledger:entry", args=[entry.pk]),
        reverse("ledger:entry_new"), reverse("ledger:accounts"), reverse("ledger:account_add"),
        reverse("ledger:statement", args=[safe.account_id]), reverse("ledger:account_edit", args=[safe.account_id]),
        reverse("ledger:periods"), reverse("treasury:home"), reverse("treasury:vouchers"),
        reverse("treasury:voucher_new"), reverse("treasury:voucher", args=[voucher.pk]),
        reverse("treasury:movement", args=[safe.pk]), reverse("treasury:counts"),
        reverse("treasury:surprise_count", args=[safe.pk]), reverse("treasury:add"),
        reverse("treasury:edit", args=[safe.pk]),
    ]  # fmt: skip
    client.force_login(world["accountant"])
    for url in pages:
        assert client.get(url).status_code == 200, url
    for who in ("reception", "other_owner"):
        client.force_login(world[who])
        for url in pages:
            expected = 403 if who == "reception" else (404 if any(ch.isdigit() for ch in url) else 200)
            assert client.get(url).status_code == expected, (who, url)
    # the other clinic's owner sees their own (empty) books, never this clinic's entry
    assert "العيادة الأولى" not in client.get(reverse("ledger:journal")).content.decode()
    nav = client.get(reverse("dashboard:home")).content.decode()
    assert reverse("ledger:trial_balance") in nav and reverse("treasury:home") in nav
    client.force_login(world["reception"])
    nav = client.get(reverse("dashboard:home"), follow=True).content.decode()
    assert reverse("ledger:trial_balance") not in nav and reverse("treasury:home") not in nav


def test_manual_entry_and_reversal_from_the_screen(client, world):
    org = world["org"]
    client.force_login(world["accountant"])
    capital, bank, patients = (chart.account(org, k) for k in ("capital", "bank_main", "patients_receivable"))
    rent, admin = chart.account(org, "rent"), chart.cost_center(org, "admin")
    data = {
        "date": "2026-03-01", "memo": "رأس المال وإيجار",
        "account": [bank.pk, rent.pk, capital.pk, ""],
        "debit": ["9,000", "1000", "", ""], "credit": ["", "", "10000.00", ""],
        "cost_center": ["", admin.pk, "", ""], "line_memo": ["إيداع", "", "", ""],
    }  # fmt: skip
    resp = client.post(reverse("ledger:entry_new"), data)
    entry = JournalEntry.objects.get(organization=org)
    assert resp.status_code == 302 and resp.url == reverse("ledger:entry", args=[entry.pk])
    assert (entry.total, entry.date, entry.created_by, entry.source_type) == (
        D("10000.00"),
        date(2026, 3, 1),
        world["accountant"],
        "",
    )
    page = client.get(resp.url).content.decode()
    assert "رأس المال وإيجار" in page and reverse("ledger:entry_reverse", args=[entry.pk]) in page

    for bad, message in [
        ({**data, "credit": ["", "", "9999", ""]}, "مش متزن"),
        ({**data, "cost_center": ["", "", "", ""]}, "مركز تكلفة"),
        ({**data, "account": [patients.pk, rent.pk, capital.pk, ""]}, "مستنداته"),
        ({**data, "debit": ["abc", "1000", "", ""]}, "مش مكتوب صح"),
    ]:
        resp = client.post(reverse("ledger:entry_new"), bad)
        assert resp.status_code == 200 and message in resp.content.decode(), message
    assert JournalEntry.objects.filter(organization=org).count() == 1

    resp = client.post(reverse("ledger:entry_reverse", args=[entry.pk]), {"memo": "غلط"})
    mirror = JournalEntry.objects.get(reverses=entry)
    assert resp.url == reverse("ledger:entry", args=[mirror.pk]) and ledger.balance_of(bank) == 0
    # an entry that came from a document is reversed through its document, not here
    payment = billing.record_payment(_appt(world), amount=400, by=world["reception"])
    auto = ledger.entry_for(payment)
    client.post(reverse("ledger:entry_reverse", args=[auto.pk]))
    assert not JournalEntry.objects.filter(reverses=auto).exists()
    assert (
        reverse("ledger:entry_reverse", args=[auto.pk])
        not in client.get(reverse("ledger:entry", args=[auto.pk])).content.decode()
    )


def test_accounts_periods_and_treasury_settings_from_the_screens(client, world):
    org = world["org"]
    client.force_login(world["accountant"])
    chart.ensure(org)
    banks = Account.objects.get(organization=org, code="1102")
    resp = client.post(reverse("ledger:account_add"), {"parent": banks.pk, "name_ar": "بنك مصر", "code": ""})
    misr = Account.objects.get(organization=org, name_ar="بنك مصر")
    assert resp.status_code == 302 and misr.code == "110202"
    client.post(reverse("ledger:account_edit", args=[misr.pk]), {"name_ar": "بنك مصر فرع زايد", "parent": banks.pk})
    client.post(reverse("ledger:account_edit", args=[misr.pk]), {"action": "toggle"})
    misr.refresh_from_db()
    assert misr.name_ar == "بنك مصر فرع زايد" and not misr.is_active
    client.post(reverse("ledger:account_edit", args=[misr.pk]), {"action": "delete"})
    assert not Account.objects.filter(pk=misr.pk).exists()
    locked = client.post(
        reverse("ledger:account_edit", args=[chart.account(org, "cash_main").pk]), {"action": "delete"}
    )
    assert "البرنامج بيستخدمه" in locked.content.decode()

    resp = client.post(
        reverse("treasury:add"), {"name": "بنك CIB", "kind": "bank", "receives_method": "", "is_active": "on"}
    )
    cib = Treasury.objects.get(organization=org, name="بنك CIB")
    assert resp.status_code == 302 and (cib.account.code, cib.account.parent.code) == ("110202", "1102")
    drawer = treasury.treasury(org, "reception_drawer")
    taken = client.post(
        reverse("treasury:edit", args=[cib.pk]), {"name": "بنك CIB", "receives_method": "cash", "is_active": "on"}
    )
    assert drawer.name in taken.content.decode()  # cash already lands in the drawer

    january = ledger.period_for(org, date(2026, 1, 1))
    client.post(reverse("ledger:period_toggle", args=[january.pk]))
    january.refresh_from_db()
    assert not january.is_open and january.closed_by == world["accountant"]
    client.post(reverse("ledger:period_toggle", args=[january.pk]))
    january.refresh_from_db()
    assert january.is_open
    assert client.get(reverse("ledger:periods")).status_code == 200


def test_vouchers_counts_and_shift_close_from_the_screens(client, world):
    org = world["org"]
    client.force_login(world["accountant"])
    safe, bank = treasury.treasury(org, "main_safe"), treasury.treasury(org, "bank_main")
    resp = client.post(
        reverse("treasury:voucher_new"),
        {"kind": "receipt", "treasury": safe.pk, "amount": "3000", "date": "2026-03-10", "description": "رأس مال",
         "counter_account": chart.account(org, "capital").pk},
    )  # fmt: skip
    voucher = Voucher.objects.get(organization=org)
    assert resp.url == reverse("treasury:voucher", args=[voucher.pk])
    short = client.post(
        reverse("treasury:voucher_new"),
        {"kind": "transfer", "treasury": safe.pk, "amount": "9000", "date": "2026-03-10", "description": "إيداع",
         "to_treasury": bank.pk},
    )  # fmt: skip
    assert "رصيد كفاية" in short.content.decode() and Voucher.objects.count() == 1
    assert "3,000.00" in client.get(reverse("treasury:home")).content.decode()
    assert "رأس مال" in client.get(reverse("treasury:movement", args=[safe.pk])).content.decode()
    client.post(reverse("treasury:voucher_cancel", args=[voucher.pk]), {"reason": "تجربة"})
    voucher.refresh_from_db()
    assert voucher.is_cancelled and treasury.balance(safe) == 0

    # reception closes its own shift from the cashbox screen; someone else approves the difference
    billing.record_payment(_appt(world), amount=437, by=world["reception"])
    client.force_login(world["reception"])
    box = client.get(reverse("billing:cashbox")).content.decode()
    assert reverse("billing:shift_close") in box
    form = client.get(reverse("billing:shift_close")).content.decode()
    assert "437" not in form  # a blind count: the expected amount is not shown
    resp = client.post(
        reverse("billing:shift_close"), {"d_200": "1", "d_100": "1", "d_50": "1", "keep": "", "notes": ""}
    )
    assert resp.status_code == 302
    count = CashCount.objects.get(organization=org)
    assert count.difference == D("-87.00") and count.counted_by == world["reception"]
    assert client.post(reverse("treasury:approve_variance", args=[count.pk])).status_code == 403
    assert reverse("billing:shift_close") not in client.get(reverse("billing:cashbox")).content.decode()

    client.force_login(world["accountant"])
    assert (
        reverse("treasury:approve_variance", args=[count.pk]) in client.get(reverse("treasury:home")).content.decode()
    )
    client.post(reverse("treasury:approve_variance", args=[count.pk]))
    count.refresh_from_db()
    assert count.variance_status == VarianceStatus.APPROVED
    drawer = treasury.treasury(org, "reception_drawer")
    client.post(reverse("treasury:surprise_count", args=[drawer.pk]), {"d_5": "2", "witness_name": "شاهد"})
    assert CashCount.objects.filter(kind=CountKind.SURPRISE, counted_total=D("10.00")).exists()
    assert client.get(reverse("treasury:counts")).status_code == 200


def test_reports_export_to_excel_and_pdf(client, world):
    org = world["org"]
    _voucher(world, VoucherKind.RECEIPT, "1234.56", counter_account=chart.account(org, "capital"))
    client.force_login(world["accountant"])
    safe = treasury.treasury(org, "main_safe")
    urls = [
        reverse("ledger:trial_balance"), reverse("ledger:journal"), reverse("ledger:statement", args=[safe.account_id]),
        reverse("treasury:movement", args=[safe.pk]), reverse("treasury:vouchers"),
    ]  # fmt: skip
    for url in urls:
        resp = client.get(url, {"format": "xlsx", "from": "2026-03-01", "to": "2026-03-31"})
        assert resp.status_code == 200 and "spreadsheetml" in resp["Content-Type"], url
        sheet = load_workbook(io.BytesIO(resp.content)).active
        values = [cell for row in sheet.iter_rows(values_only=True) for cell in row]
        assert 1234.56 in values, url  # amounts are numbers in Excel, not text
        assert sheet.sheet_view.rightToLeft
    pdf = client.get(reverse("ledger:trial_balance"), {"format": "pdf", "from": "2026-03-01", "to": "2026-03-31"})
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf" and pdf.content.startswith(b"%PDF-")


# ================================================================ any sequence of documents keeps the books right

operation = st.one_of(
    st.tuples(st.just("pay"), st.sampled_from(["cash", "card", "instapay", "wallet"]), st.integers(1, 90000)),
    st.tuples(st.just("refund"), st.sampled_from(["cash", "card"]), st.integers(1, 90000)),
    st.tuples(st.just("receipt"), st.sampled_from(["main_safe", "bank_main"]), st.integers(1, 500000)),
    st.tuples(
        st.just("spend"), st.sampled_from(["main_safe", "bank_main", "reception_drawer"]), st.integers(1, 200000)
    ),
    st.tuples(
        st.just("transfer"), st.sampled_from(["main_safe", "bank_main", "reception_drawer"]), st.integers(1, 200000)
    ),
    st.tuples(st.just("cancel"), st.just(""), st.integers(0, 50)),
    st.tuples(st.just("close"), st.just(""), st.integers(-300, 300)),
)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=st.lists(operation, min_size=1, max_size=14))
def test_any_sequence_of_documents_keeps_the_books_right(world, ops):
    """Random payments, refunds, vouchers, cancellations and shift closes (amounts in piasters). After every step:
    each posted entry balances, the trial balance balances, entry numbers have no gaps, no cash treasury is below
    zero unless a counted overage is waiting for approval, and the cashbox equals the ledger."""
    org, user = world["org"], world["owner"]
    owner = Membership.objects.get(user=user)
    sid = transaction.savepoint()
    try:
        treasury.ensure(org)
        paid_appts, vouchers = [], []
        for kind, where, raw in ops:
            amount = D(raw) / 100
            try:
                if kind == "pay":
                    appt = _appt(world, price=amount)
                    billing.record_payment(appt, amount=amount, method=where, by=user)
                    paid_appts.append((appt, where, amount))
                elif kind == "refund" and paid_appts:
                    appt, method, paid = paid_appts.pop()
                    billing.record_payment(appt, amount=min(amount, paid), method=method, by=user, is_refund=True)
                elif kind == "receipt":
                    vouchers.append(_voucher(world, VoucherKind.RECEIPT, amount, treasury=treasury.treasury(org, where),
                                             counter_account=chart.account(org, "capital"), by=user))  # fmt: skip
                elif kind == "spend":
                    vouchers.append(_voucher(world, VoucherKind.PAYMENT, amount, treasury=treasury.treasury(org, where),
                                             counter_account=chart.account(org, "rent"),
                                             cost_center=chart.cost_center(org, "admin"), by=user))  # fmt: skip
                elif kind == "transfer":
                    target = "bank_main" if where != "bank_main" else "main_safe"
                    vouchers.append(_voucher(world, VoucherKind.TRANSFER, amount,
                                             treasury=treasury.treasury(org, where),
                                             to_treasury=treasury.treasury(org, target), by=user))  # fmt: skip
                elif kind == "cancel" and vouchers:
                    treasury.cancel_voucher(vouchers[raw % len(vouchers)], by=user)
                elif kind == "close":
                    drawer = treasury.treasury(org, "reception_drawer")
                    shift = treasury.current_shift(drawer)
                    if shift is not None:
                        counted = max(treasury.balance(drawer) + D(raw), D(0)).quantize(D("1"))
                        count = treasury.close_shift(shift, denominations={"1": int(counted)}, by=user)
                        if count.variance_status == VarianceStatus.PENDING:
                            treasury.approve_variance(count, by=user, membership=owner)
            except (billing.PaymentError, treasury.TreasuryError):
                pass  # a refused document must leave nothing behind: the checks below prove it

            posted = JournalEntry.objects.filter(organization=org, status=EntryStatus.POSTED)
            for entry in posted.prefetch_related("lines"):
                lines = list(entry.lines.all())
                assert len(lines) >= 2 and sum(x.debit for x in lines) == sum(x.credit for x in lines) == entry.total
            assert sorted(posted.values_list("number", flat=True)) == list(range(1, posted.count() + 1))
            assert reports.trial_balance(org, date(2026, 1, 1), date(2026, 12, 31)).balanced
            for t in Treasury.objects.filter(organization=org).select_related("account"):
                if t.holds_cash:
                    assert treasury.balance(t) >= 0 and treasury.cash_on_hand(t) == treasury.balance(t), t.system_key
            # every payment has its entry, and what patients paid equals what the treasuries received from them
            payments = Payment.objects.filter(organization=org)
            assert posted.filter(source_type="billing.payment").count() == payments.count()
            received = sum((p.signed_amount for p in payments), D(0))
            assert -ledger.balance_of(chart.account(org, "patients_receivable")) == received
            numbers = list(
                Voucher.objects.filter(organization=org, kind=VoucherKind.RECEIPT)
                .order_by("number")
                .values_list("number", flat=True)
            )
            assert numbers == list(range(1, len(numbers) + 1))
    finally:
        transaction.savepoint_rollback(sid)
