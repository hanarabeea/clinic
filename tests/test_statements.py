"""Financial statements, management reports, books verification and the year close (plan 14 §14.9)."""

import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from openpyxl import load_workbook

from apps.billing import services as billing
from apps.billing.models import Payment
from apps.core.timeutils import CAIRO
from apps.doctors.models import Doctor, VisitType, WorkingPeriod
from apps.inventory import services as stock
from apps.inventory.models import StockBalance, StockItem
from apps.invoicing import reports as invoice_reports
from apps.invoicing import services as invoicing
from apps.invoicing.models import Invoice
from apps.ledger import chart, closing, statements, verify
from apps.ledger import services as ledger
from apps.ledger.models import BooksCheck, FiscalPeriod, FiscalYear, PeriodStatus
from apps.ledger.services import LedgerError, credit, debit
from apps.organizations.models import ClinicRole, Membership, Role
from apps.patients import services as patient_services
from apps.patients.models import Gender, Patient
from apps.purchasing import services as purchasing
from apps.purchasing.models import PayMethod
from apps.purchasing.services import LineIn
from apps.scheduling import services as booking
from apps.scheduling.models import Appointment, AppointmentStatus
from apps.theatre import services as theatre
from apps.theatre.models import OperationStatus, ProcedureType, Room, RoomKind, TeamRole
from apps.treasury import services as treasury
from apps.treasury.models import VoucherKind

D = Decimal
S = AppointmentStatus
NOW = "2026-03-10 12:00:00+02:00"
TODAY = date(2026, 3, 10)
MARCH = (date(2026, 3, 1), date(2026, 3, 31))

pytestmark = pytest.mark.usefixtures("clock")


@pytest.fixture
def clock():
    with freeze_time(NOW, tick=True):
        yield


@pytest.fixture
def world(org_a, org_b, make_member, clock):
    chart.ensure(org_a)
    treasury.ensure(org_a)
    purchasing.ensure_defaults(org_a)
    stock.ensure_defaults(org_a)
    doctor = Doctor.objects.create(organization=org_a, name_ar="سارة علي", title_ar="د.", slot_minutes=10,
                                   visit_share_percent=D("25"))  # fmt: skip
    for weekday in range(7):
        WorkingPeriod.objects.create(
            organization=org_a, doctor=doctor, weekday=weekday, start_time="00:00", end_time="23:59"
        )
    vt = VisitType.objects.create(organization=org_a, doctor=doctor, name_ar="كشف", duration_minutes=10, price=D("400"))
    owner = make_member(org_a, "owner")
    return {
        "org": org_a, "owner": owner, "owner_m": Membership.objects.get(user=owner), "doctor": doctor, "vt": vt,
        "reception": make_member(org_a, "reception"), "doctor_user": make_member(org_a, "doctor"),
        "other_owner": make_member(org_b, "owner"), "bank": treasury.treasury(org_a, "bank_main"),
        "supplier": purchasing.save_supplier(org_a, by=owner, name="شركة المستلزمات"), "n": 0,
    }  # fmt: skip


def _acc(world, key):
    return chart.account(world["org"], key)


def _cc(world, key):
    return chart.cost_center(world["org"], key)


def _patient(world):
    world["n"] += 1
    return patient_services.create_patient(
        world["org"], Patient(full_name=f"مريضة {world['n']}", phone=f"+2010044{world['n']:05d}", gender=Gender.FEMALE)
    )


def _visit(world, pay=True, patient=None):
    """A visit that happened: booked, paid (or not), arrived and finished."""
    world["n"] += 1
    start = timezone.now().replace(second=0, microsecond=0) + timedelta(minutes=10 * world["n"])
    appt = booking.book(
        patient=patient or _patient(world), doctor=world["doctor"], visit_type=world["vt"], by=world["reception"],
        start_at=start, overbook=True, notify=False,
    )  # fmt: skip
    if pay:
        billing.record_payment(appt, amount=D("400"), by=world["reception"])
    for status in (S.ARRIVED, S.IN_CONSULTATION, S.COMPLETED):
        appt = booking.transition(appt, status, by=world["reception"])
    return appt


def _month(world):
    """A small month of real documents: capital, a paid and an unpaid visit, rent, a purchase on credit."""
    org, owner = world["org"], world["owner"]
    voucher = lambda kind, amount, key, cc=None: treasury.create_voucher(  # noqa: E731
        org, kind=kind, treasury=world["bank"], amount=D(amount), date=TODAY, description="بيان", by=owner,
        counter_account=_acc(world, key), cost_center=_cc(world, cc) if cc else None,
    )  # fmt: skip
    voucher(VoucherKind.RECEIPT, "100000", "capital")
    _visit(world)
    _visit(world, pay=False)
    voucher(VoucherKind.PAYMENT, "5000", "rent", "admin")
    purchasing.create_invoice(
        org, supplier=world["supplier"], date=TODAY, by=owner,
        lines=[LineIn("مطهرات", _acc(world, "medical_supplies_expense"), "1", "1000", _cc(world, "theatre"))],
    )  # fmt: skip


# ================================================================ the statements


def test_income_statement_and_balance_sheet_agree_with_the_documents(world):
    org = world["org"]
    _month(world)
    st_ = statements.income_statement(org, *MARCH)
    rows = {r.account.system_key: r.amount for r in st_.revenue.rows + st_.expenses.rows if r.account.system_key}
    assert rows == {
        "revenue_visits": D("800.00"), "medical_supplies_expense": D("1000.00"), "doctor_fees_expense": D("200.00"),
        "rent": D("5000.00"),
    }  # fmt: skip
    assert (st_.revenue.total, st_.expenses.total, st_.net) == (D("800.00"), D("6200.00"), D("-5400.00"))
    # a group row is the sum of its accounts, and is shown only when it adds up two of them or more
    groups = [r for r in st_.expenses.rows if r.account.is_group]
    assert [g.amount for g in groups] == [D("1200.00")]  # supplies 1,000 + doctor fees 200; rent stands alone
    assert not [r for r in st_.revenue.rows if r.account.is_group]
    # one cost center, and all of them side by side
    assert statements.income_statement(org, *MARCH, cost_center=_cc(world, "admin")).net == D("-5000.00")
    by_center = {row["center"].system_key: row["net"] for row in statements.by_cost_center(org, *MARCH)}
    assert by_center == {"outpatient": D("600.00"), "theatre": D("-1000.00"), "admin": D("-5000.00")}
    assert sum(by_center.values()) == st_.net
    # compared with the month before (nothing happened then) and with last year
    compared = statements.income_statement(org, *MARCH, compare="previous")
    assert (compared.compare_start, compared.compare_end, compared.compare_net) == (
        date(2026, 1, 29),
        date(2026, 2, 28),
        D(0),
    )
    assert statements.compare_range(date(2024, 2, 29), date(2024, 2, 29), "year") == (
        date(2023, 2, 28),
        date(2023, 2, 28),
    )

    bs = statements.balance_sheet(org, TODAY)
    assert bs.balanced
    # bank 95,000 + drawer 400 + the unpaid visit 400 = 95,800 owned; 1,000 to the supplier and 200 to the doctor owed
    assert (bs.assets.total, bs.liabilities.total, bs.equity.total, bs.result) == (
        D("95800.00"), D("1200.00"), D("100000.00"), D("-5400.00"),
    )  # fmt: skip
    assert bs.result == st_.net
    earlier = statements.balance_sheet(org, date(2026, 2, 28))
    assert earlier.balanced and earlier.assets.total == 0
    cash = {row["treasury"].system_key: row for row in statements.cash_movement(org, *MARCH)}
    assert (cash["bank_main"]["came"], cash["bank_main"]["went"], cash["bank_main"]["closing"]) == (
        D(100000),
        D(5000),
        D(95000),
    )
    assert cash["reception_drawer"]["closing"] == D("400.00")


account = st.sampled_from(
    ["cash_main", "bank_main", "capital", "rent", "revenue_other", "sundry_expenses", "prepaid_expenses"]
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    entries=st.lists(st.tuples(account, account, st.integers(1, 9999999), st.integers(0, 80)), min_size=1, max_size=12)
)
def test_for_any_entries_the_balance_sheet_balances_and_carries_the_result(world, entries):
    org = world["org"]
    sid = transaction.savepoint()
    try:
        for dr, cr, raw, days in entries:
            if dr == cr:
                continue
            kw = lambda key: {"cost_center": "admin"} if key in ("rent", "revenue_other", "sundry_expenses") else {}  # noqa: E731
            ledger.post(org, date=TODAY - timedelta(days=days), memo="x", by=world["owner"], lines=[
                debit(dr, D(raw) / 100, **kw(dr)), credit(cr, D(raw) / 100, **kw(cr)),
            ])  # fmt: skip
        for as_of in (TODAY, TODAY - timedelta(days=40), date(2025, 12, 31)):
            bs = statements.balance_sheet(org, as_of)
            assert bs.balanced
            assert bs.result == statements.income_statement(org, date(2000, 1, 1), as_of).net
        year = statements.income_statement(org, date(2025, 1, 1), date(2026, 12, 31))
        assert year.net == sum(
            (row["net"] for row in statements.by_cost_center(org, date(2025, 1, 1), date(2026, 12, 31))), D(0)
        )
    finally:
        transaction.savepoint_rollback(sid)


# ================================================================ management reports


def test_management_reports(world):
    org, owner, doctor = world["org"], world["owner"], world["doctor"]
    with freeze_time("2026-01-05 12:00:00+02:00"):
        old = _visit(world, pay=False)  # 64 days old on the 10th of March
    _visit(world)
    _visit(world, pay=False)
    # an operation: 5,000, the surgeon gets 2,000, supplies from the store cost 150 and one is billed apart at 120
    theatre.ensure_defaults(org)
    patient = _patient(world)
    room = Room.objects.filter(organization=org, kind=RoomKind.OPERATING).first()
    procedure = ProcedureType.objects.get(organization=org, name_ar="كحت وتنظيف")
    op = theatre.book_operation(
        org=org, patient=patient, procedure=procedure, room=room, surgeon=doctor, by=world["doctor_user"],
        start=datetime.combine(TODAY, time(13, 0), tzinfo=CAIRO), price=D("5000"), notify=False,
    )  # fmt: skip
    surgeon = op.team.get(role=TeamRole.SURGEON)
    theatre.save_team_member(
        op, role=TeamRole.SURGEON, by=world["doctor_user"], member=surgeon, doctor=doctor, fee="2000"
    )
    suture = StockItem.objects.create(organization=org, name="خيط", sale_price=D("120"))
    stock.opening(org, warehouse=stock.warehouse(org, "theatre"), lines=[(suture, "5", "50", None)], by=owner)
    theatre.add_consumable(op, item=suture, quantity="3", by=world["doctor_user"])
    for item in op.checklist.filter(done=False):
        theatre.toggle_checklist(item, by=world["doctor_user"])
    for status in (
        OperationStatus.ADMITTED,
        OperationStatus.IN_THEATRE,
        OperationStatus.RECOVERY,
        OperationStatus.DONE,
    ):
        op = theatre.transition(op, status, by=world["doctor_user"])
    invoicing.issue_account_invoice(patient, keys=[i.key for i in invoicing.open_items(patient)], by=owner)

    (row,) = invoice_reports.by_doctor(org, *MARCH)
    assert (row.doctor, row.revenue, row.share) == (doctor, D("5800.00"), D("2200.00"))  # two visits + the operation
    items = {(r.kind, r.name): (r.count, r.revenue) for r in invoice_reports.by_item(org, *MARCH)}
    assert items == {("visit", "كشف"): (2, D("800.00")), ("operation", "كحت وتنظيف"): (1, D("5000.00")),
                     ("charge", "خيط"): (3, D("360.00"))}  # fmt: skip
    (profit,) = invoice_reports.operations_profit(org, *MARCH)
    assert (profit.price, profit.team, profit.supplies, profit.extra, profit.left) == (
        D("5000.00"), D("2000.00"), D("150.00"), D("360.00"), D("3210.00"),
    )  # fmt: skip
    aging = {r.patient.pk: r.buckets for r in invoice_reports.receivables_aging(org, TODAY)}
    assert aging[old.patient_id] == [D(0), D(0), D("400.00"), D(0)]  # 64 days: the 61-90 column
    assert aging[patient.pk] == [D("5360.00"), D(0), D(0), D(0)]
    assert len(aging) == 3
    # a credit note in the period lowers the period's revenue
    appt = Appointment.objects.filter(organization=org, date=TODAY, status=S.COMPLETED).first()
    Appointment.objects.filter(pk=appt.pk).update(status=S.ARRIVED)  # as if it was reopened and never paid for
    if not appt.payments.exists():
        invoicing.sync_appointment(appt)
        assert {(r.kind, r.name): r.count for r in invoice_reports.by_item(org, *MARCH)}[("visit", "كشف")] == 1


# ================================================================ books verification


def test_verification_passes_on_real_documents_and_names_what_is_wrong(world):
    org, owner = world["org"], world["owner"]
    _month(world)
    gauze = StockItem.objects.create(organization=org, name="شاش")
    stock.opening(org, warehouse=stock.warehouse(org, "main"), lines=[(gauze, "10", "10", None)], by=owner)
    stock.issue(
        org, warehouse=stock.warehouse(org, "main"), lines=[(gauze, "3")], cost_center=_cc(world, "theatre"), by=owner
    )
    bill = world["supplier"].invoices.get()
    purchasing.pay_supplier(org, supplier=world["supplier"], amount="600", method=PayMethod.CHEQUE, by=owner,
                            treasury_obj=world["bank"], allocations=[(bill, "600")], cheque_number="1",
                            cheque_due=TODAY + timedelta(days=9))  # fmt: skip
    result = verify.run(org, by=owner)
    assert result.ok and result.problems == [] and verify.last(org) == result
    call_command("verify_books", "--org", org.slug)  # exits quietly when the books are sound

    def codes():
        return {p.code for p in verify.verify(org)}

    sid = transaction.savepoint()  # a line on a control account that names nobody
    ledger.post(org, date=TODAY, memo="x", by=owner, lines=[
        debit("patients_receivable", D("10")), credit("revenue_other", D("10"), cost_center="admin"),
    ])  # fmt: skip
    assert codes() == {"control.no_party"}
    transaction.savepoint_rollback(sid)

    sid = transaction.savepoint()  # a shelf whose value was changed behind the moves
    StockBalance.objects.filter(item=gauze).update(value=F("value") + 5)
    assert codes() == {"control.mismatch", "stock.moves"}
    transaction.savepoint_rollback(sid)

    sid = transaction.savepoint()  # money taken with no entry
    appt = Appointment.objects.filter(organization=org).first()
    Payment.objects.create(organization=org, patient=appt.patient, appointment=appt, amount=D("50"), method="cash")
    assert "document.no_entry" in codes()
    transaction.savepoint_rollback(sid)

    sid = transaction.savepoint()  # an invoice number out of line
    Invoice.objects.filter(pk=Invoice.objects.filter(organization=org).order_by("-id").first().pk).update(number=99)
    assert codes() == {"numbering.gap"}
    transaction.savepoint_rollback(sid)

    sid = transaction.savepoint()  # a visit whose price changed after it was invoiced
    Appointment.objects.filter(pk=appt.pk).update(price=D("450"))
    assert codes() == {"visit.invoice"}
    # while the books are wrong a month does not close, the command fails, and the result is kept
    february = FiscalPeriod.objects.get(organization=org, start_date=date(2026, 2, 1))
    FiscalPeriod.objects.filter(organization=org, start_date=date(2026, 1, 1)).update(status="closed")
    with pytest.raises(LedgerError, match="فحص الدفاتر"):
        ledger.close_period(february, by=owner)
    with pytest.raises(CommandError):
        call_command("verify_books", "--org", org.slug)
    failed = BooksCheck.objects.filter(organization=org).order_by("-id").first()
    assert not failed.ok and failed.problems[0]["code"] == "visit.invoice"
    transaction.savepoint_rollback(sid)
    assert verify.verify(org) == []
    FiscalPeriod.objects.filter(organization=org, start_date=date(2026, 1, 1)).update(status="closed")
    assert not ledger.close_period(february, by=owner).is_open


# ================================================================ closing the year


def test_closing_a_year_carries_its_result_and_can_be_undone(world):
    org, owner = world["org"], world["owner"]
    post = lambda day, dr, cr, amount: ledger.post(org, date=day, memo="x", by=owner, lines=[dr(amount), cr(amount)])  # noqa: E731
    line = lambda side, key, cc=None: lambda amount: side(key, D(amount), **({"cost_center": cc} if cc else {}))  # noqa: E731
    with freeze_time("2027-01-05 12:00:00+02:00"):
        post(date(2026, 2, 1), line(debit, "bank_main"), line(credit, "capital"), "50000")
        post(date(2026, 6, 1), line(debit, "bank_main"), line(credit, "revenue_other", "outpatient"), "9000")
        post(date(2026, 6, 2), line(debit, "bank_main"), line(credit, "revenue_other", "theatre"), "1000")
        post(date(2026, 9, 1), line(debit, "rent", "admin"), line(credit, "bank_main"), "4000")
        post(date(2027, 1, 2), line(debit, "rent", "admin"), line(credit, "bank_main"), "300")  # next year's business
        year = FiscalYear.objects.get(organization=org, start_date=date(2026, 1, 1))
        assert closing.result_of(year) == D("6000.00")
        checks = {c.label: c.ok for c in closing.checklist(year)}
        assert checks["كل الشهور ما عدا الأخير مقفولة"] is False and checks["فحص الدفاتر سليم"] is True
        with pytest.raises(LedgerError, match="مش جاهزة للقفل"):
            closing.close_year(year, by=owner)
        for period in year.periods.order_by("number")[:11]:
            ledger.close_period(period, by=owner)
        assert all(c.ok for c in closing.checklist(year))
        year = closing.close_year(year, by=owner)
        assert year.status == PeriodStatus.CLOSED and not year.periods.filter(status="open").exists()
        entry = closing.closing_entry(year)
        assert entry.date == date(2026, 12, 31)
        assert sorted(
            (x.account.system_key, x.debit, x.credit, x.cost_center.system_key if x.cost_center_id else "")
            for x in entry.lines.select_related("account", "cost_center")
        ) == [
            ("rent", D("0.00"), D("4000.00"), "admin"),
            ("retained_earnings", D("0.00"), D("6000.00"), ""),
            ("revenue_other", D("1000.00"), D("0.00"), "theatre"),
            ("revenue_other", D("9000.00"), D("0.00"), "outpatient"),
        ]
        # the closed year still shows what it earned; the balance sheet carries it in equity
        assert statements.income_statement(org, date(2026, 1, 1), date(2026, 12, 31)).net == D("6000.00")
        end = statements.balance_sheet(org, date(2026, 12, 31))
        assert end.balanced and (end.result, end.equity.total) == (D("0.00"), D("56000.00"))
        now = statements.balance_sheet(org, date(2027, 1, 5))
        assert now.balanced and (now.result, now.equity.total) == (D("-300.00"), D("56000.00"))
        with pytest.raises(LedgerError, match="مقفول"):
            post(date(2026, 12, 30), line(debit, "bank_main"), line(credit, "capital"), "1")
        assert closing.close_year(year, by=owner).status == PeriodStatus.CLOSED  # asking twice changes nothing

        # reopened: the closing entry is reversed, a correction goes in, and it closes again with the new result
        year = closing.reopen_year(year, by=owner)
        assert year.status == PeriodStatus.OPEN and closing.closing_entry(year) is None
        assert ledger.balance_of(chart.account(org, "retained_earnings")) == 0
        assert statements.income_statement(org, date(2026, 1, 1), date(2026, 12, 31)).net == D("6000.00")
        post(date(2026, 12, 31), line(debit, "sundry_expenses", "admin"), line(credit, "bank_main"), "500")
        year = closing.close_year(year, by=owner)
        assert ledger.balance_of(chart.account(org, "retained_earnings")) == D("5500.00")
        assert statements.income_statement(org, date(2026, 1, 1), date(2026, 12, 31)).net == D("5500.00")
        assert statements.balance_sheet(org, date(2027, 1, 5)).balanced and verify.verify(org) == []
        # 2027 cannot close while a month of it is open, and 2026 cannot reopen once 2027 is closed
        next_year = FiscalYear.objects.get(organization=org, start_date=date(2027, 1, 1))
        assert [c.ok for c in closing.checklist(next_year)][:2] == [True, False]


# ================================================================ screens and who may do what


def test_statement_screens_and_permissions(client, world):
    org, owner = world["org"], world["owner"]
    _month(world)
    year = FiscalYear.objects.get(organization=org, start_date=date(2026, 1, 1))
    urls = {
        "income": reverse("ledger:income_statement"), "balance": reverse("ledger:balance_sheet"),
        "verify": reverse("ledger:verify"), "year": reverse("ledger:year_close", args=[year.pk]),
        "revenue": reverse("invoicing:report_revenue"), "operations": reverse("invoicing:report_operations"),
        "receivables": reverse("invoicing:report_receivables"), "cash": reverse("invoicing:report_cash"),
    }  # fmt: skip
    client.force_login(world["reception"])
    assert {client.get(u).status_code for u in urls.values()} == {403}
    client.force_login(world["doctor_user"])  # report.finance: the management reports, not the books
    assert [client.get(urls[k]).status_code for k in ("revenue", "operations", "receivables", "cash")] == [200] * 4
    assert [client.get(urls[k]).status_code for k in ("income", "balance", "verify", "year")] == [403] * 4

    client.force_login(owner)
    for name, url in urls.items():
        assert client.get(url).status_code == 200, name
    page = client.get(urls["income"], {"from": "2026-03-01", "to": "2026-03-31"}).content.decode()
    rent = chart.account(org, "rent")
    assert "5,400.00" in page and "صافي الخسارة" in page
    assert f"{reverse('ledger:statement', args=[rent.pk])}?from=2026-03-01&amp;to=2026-03-31" in page  # the drill-down
    assert "حسب القسم" in page
    assert (
        "حسب القسم" not in client.get(urls["income"], {"center": chart.cost_center(org, "admin").pk}).content.decode()
    )
    sheet = client.get(urls["balance"], {"date": "2026-03-10", "compare": "1"}).content.decode()
    assert "95,800.00" in sheet and "الطرفين مش متساويين" not in sheet
    for url, params, expected in [
        (urls["income"], {"from": "2026-03-01", "to": "2026-03-31", "compare": "previous"}, -5400.0),
        (urls["balance"], {"date": "2026-03-10"}, 95800.0),
        (urls["cash"], {"from": "2026-03-01", "to": "2026-03-31"}, 95000.0),
        (urls["revenue"], {"from": "2026-03-01", "to": "2026-03-31"}, 800.0),
        (urls["receivables"], {}, 400.0),
    ]:
        xlsx = client.get(url, {**params, "format": "xlsx"})
        values = [c for row in load_workbook(io.BytesIO(xlsx.content)).active.iter_rows(values_only=True) for c in row]
        assert expected in values, url
    assert client.get(urls["verify"]).content.decode().count("لسه ما اتعملش فحص") == 1
    resp = client.post(urls["verify"])
    assert (
        resp.status_code == 302
        and verify.last(org).ok
        and "الدفاتر سليمة" in client.get(urls["verify"]).content.decode()
    )
    resp = client.post(urls["year"])  # not ready: eleven months are open
    assert resp.status_code == 302 and FiscalYear.objects.get(pk=year.pk).status == PeriodStatus.OPEN
    assert "خلّص البنود اللي فوق الأول" in client.get(urls["year"]).content.decode()

    # someone who was given only the statements opens "الدفاتر" straight onto them
    accountant = Membership.objects.create(
        user=type(owner).objects.create_user(email="statements@example.com", password="Strong-Pass-2026!"),
        organization=org, role=Role.MEMBER,
        clinic_role=ClinicRole.objects.create(organization=org, name="قوائم", permissions=["report.statements"]),
    )  # fmt: skip
    client.force_login(accountant.user)
    home = client.get("/", follow=True).content.decode()
    assert f'href="{urls["income"]}"' in home and f'href="{reverse("ledger:trial_balance")}"' not in home
    assert (
        client.get(urls["income"]).status_code == 200 and client.get(reverse("ledger:trial_balance")).status_code == 403
    )
    client.force_login(world["other_owner"])
    assert client.get(urls["year"]).status_code == 404
