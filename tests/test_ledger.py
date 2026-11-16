"""The accounting core (plan 14 §14.5): the rules of the books, in the service and in the database itself."""

import threading
from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apps.ledger import accounts as account_rules
from apps.ledger import chart, reports, services
from apps.ledger.models import (
    Account,
    AccountType,
    CostCenter,
    EntryStatus,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    LedgerSettings,
    PeriodStatus,
)
from apps.ledger.money import allocate, money, percent_of
from apps.ledger.services import LedgerError, PeriodClosed, credit, debit
from apps.patients import services as patient_services
from apps.patients.models import Gender, Patient

D = Decimal
DAY = date(2026, 3, 10)


def _post(org, amount="100", day=DAY, **kw):
    """Cash in the safe against capital: the simplest balanced entry."""
    return services.post(
        org, date=day, memo="إيداع", lines=[debit("cash_main", amount), credit("capital", amount)], **kw
    )


# ================================================================ money


def test_money_rounds_once_half_up_and_refuses_floats():
    assert money("10.005") == D("10.01") and money("10.004") == D("10.00")
    assert money("-10.005") == D("-10.01")  # half up means away from zero
    assert money(7) == D("7.00") and str(money("1.1")) == "1.10"
    assert percent_of("199.99", "3") == D("6.00")
    with pytest.raises(TypeError):
        money(0.1)
    with pytest.raises(ValueError, match="finite"):
        money(D("NaN"))


def test_allocate_parts_always_sum_to_the_whole():
    assert allocate("100", [1, 1, 1]) == [D("33.34"), D("33.33"), D("33.33")]
    assert allocate("0.05", [1, 1, 1]) == [D("0.02"), D("0.02"), D("0.01")]
    assert allocate("10", [3, 1]) == [D("7.50"), D("2.50")]
    assert allocate("-100", [1, 1, 1]) == [D("-33.34"), D("-33.33"), D("-33.33")]
    assert allocate("0", [0, 0]) == [D("0.00"), D("0.00")]
    assert allocate("50", [0, 5]) == [D("0.00"), D("50.00")]
    with pytest.raises(ValueError):
        allocate("1", [0, 0])
    with pytest.raises(ValueError):
        allocate("1", [1, -1])
    with pytest.raises(ValueError):
        allocate("1", [])


@given(
    total=st.decimals(min_value=0, max_value=10**7, places=2),
    weights=st.lists(st.integers(min_value=0, max_value=10**6), min_size=1, max_size=12).filter(lambda w: sum(w) > 0),
)
def test_allocate_is_exact_fair_and_stable(total, weights):
    parts = allocate(total, weights)
    assert sum(parts) == total  # nothing lost, nothing made up
    assert all(p >= 0 and p == p.quantize(D("0.01")) for p in parts)
    whole = sum(weights)
    for part, weight in zip(parts, weights, strict=True):
        assert abs(part - total * weight / whole) < D("0.01")  # never further than a piaster from the exact share
    assert parts == allocate(total, weights)


# ================================================================ chart and periods


def test_chart_is_a_proper_tree_and_ensure_never_overwrites(org_a, org_b):
    chart.ensure(org_a)
    accounts = list(Account.objects.filter(organization=org_a))
    assert len(accounts) == len(chart.CHART)
    by_code = {a.code: a for a in accounts}
    assert [a.code for a in accounts if a.parent_id is None] == ["1", "2", "3", "4", "5"]
    for a in accounts:
        if a.parent_id:
            parent = by_code[chart.parent_code(a.code)]
            assert a.parent_id == parent.pk and parent.is_group and parent.type == a.type
            assert a.code.startswith(parent.code)
        assert a.is_group == a.children.exists()
    keys = [a.system_key for a in accounts if a.system_key]
    assert len(keys) == len(set(keys)) and all(not by_code[c].system_key for c in chart.GROUPS)
    program_keys = (
        "cash_main", "patients_receivable", "suppliers_payable", "cheques_receivable", "cheques_payable", "vat_input",
        "wht_payable", "inventory", "cash_over_short", "retained_earnings", "opening_balances",
    )  # fmt: skip
    for key in program_keys:
        assert chart.account(org_a, key).system_key == key

    safe = chart.account(org_a, "cash_main")
    safe.name_ar = "خزنة الدكتورة"
    safe.save()
    chart.ensure(org_a)
    safe.refresh_from_db()
    assert safe.name_ar == "خزنة الدكتورة" and Account.objects.filter(organization=org_a).count() == len(chart.CHART)
    assert CostCenter.objects.filter(organization=org_a).count() == 5
    assert not Account.objects.filter(organization=org_b).exists()  # each clinic has its own tree


def test_fiscal_year_and_its_twelve_months(org_a):
    period = services.period_for(org_a, date(2026, 2, 28))
    assert (period.start_date, period.end_date, period.number) == (date(2026, 2, 1), date(2026, 2, 28), 2)
    year = period.fiscal_year
    assert (year.start_date, year.end_date, year.name) == (date(2026, 1, 1), date(2026, 12, 31), "2026")
    assert year.periods.count() == 12 and services.period_for(org_a, date(2026, 12, 31)).number == 12
    assert services.fiscal_year_for(org_a, date(2026, 7, 1)) == year  # found, not made again

    settings_row = LedgerSettings.for_org(org_a)
    settings_row.fiscal_year_start_month = 7
    settings_row.save()
    july = services.fiscal_year_for(org_a, date(2027, 8, 15))
    assert (july.start_date, july.end_date, july.name) == (date(2027, 7, 1), date(2028, 6, 30), "2027/2028")
    assert services.period_for(org_a, date(2028, 2, 29)).number == 8  # leap day is inside its month


# ================================================================ posting


def test_post_writes_one_balanced_numbered_entry(org_a, make_member):
    user = make_member(org_a, "owner")
    entry = services.post(
        org_a, date=DAY, memo="شراء أدوات مكتبية نقدًا", by=user,
        lines=[debit("stationery", "150.5", cost_center="admin", memo="ورق"), credit("cash_main", D("150.50"))],
    )  # fmt: skip
    assert (entry.number, entry.status, entry.total, entry.date) == (1, EntryStatus.POSTED, D("150.50"), DAY)
    assert entry.period == services.period_for(org_a, DAY) and entry.created_by == user and entry.posted_at
    lines = list(entry.lines.order_by("line_no"))
    assert [(line.account.system_key, line.debit, line.credit) for line in lines] == [
        ("stationery", D("150.50"), D("0.00")),
        ("cash_main", D("0.00"), D("150.50")),
    ]
    assert lines[0].cost_center.system_key == "admin" and lines[0].memo == "ورق"
    assert _post(org_a).number == 2 and _post(org_a, day=date(2027, 1, 5)).number == 1  # numbers restart each year
    assert services.balance_of(chart.account(org_a, "cash_main")) == D("49.50")  # -150.50 + 100 + 100
    assert services.balance_of(chart.account(org_a, "capital")) == D("200.00")  # a credit balance of equity is positive


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (lambda: [debit("cash_main", 100)], "سطرين"),
        (lambda: [debit("cash_main", 100), credit("capital", 99)], "مش متزن"),
        (lambda: [debit("cash_main", 0), credit("capital", 0)], "مدين أو دائن"),
        (lambda: [services.Line("cash_main", debit=5, credit=5), credit("capital", 5)], "مدين أو دائن"),
        (lambda: [debit("cash_main", -5), credit("capital", -5)], "بالسالب"),
        (lambda: [debit("rent", 100), credit("cash_main", 100)], "مركز تكلفة"),
    ],
)
def test_post_refuses_what_breaks_the_rules(org_a, lines, message):
    with pytest.raises(LedgerError, match=message):
        services.post(org_a, date=DAY, memo="x", lines=lines())
    assert not JournalEntry.objects.exists() and not JournalLine.objects.exists()


def test_post_refuses_groups_switched_off_accounts_and_other_clinics(org_a, org_b):
    chart.ensure(org_a)
    chart.ensure(org_b)
    group = Account.objects.get(organization=org_a, code="1101")
    with pytest.raises(LedgerError, match="حساب رئيسي"):
        services.post(org_a, date=DAY, memo="x", lines=[debit(group, 10), credit("capital", 10)])
    other = chart.account(org_b, "cash_main")
    with pytest.raises(LedgerError, match="مش بتاع العيادة"):
        services.post(org_a, date=DAY, memo="x", lines=[debit(other, 10), credit("capital", 10)])
    sundry = chart.account(org_a, "revenue_other")
    sundry.is_active = False
    sundry.save()
    with pytest.raises(LedgerError, match="موقوف"):
        services.post(
            org_a, date=DAY, memo="x", lines=[debit("cash_main", 10), credit(sundry, 10, cost_center="admin")]
        )
    assert not JournalEntry.objects.exists()


def test_a_refused_entry_never_burns_a_number(org_a):
    assert _post(org_a).number == 1
    for _ in range(3):
        with pytest.raises(LedgerError):
            services.post(org_a, date=DAY, memo="x", lines=[debit("cash_main", 1), credit("capital", 2)])
    assert _post(org_a).number == 2
    numbers = list(JournalEntry.objects.filter(organization=org_a).values_list("number", flat=True))
    assert numbers == [1, 2]


def test_a_document_posts_once(org_a):
    patient = patient_services.create_patient(
        org_a, Patient(full_name="منى", phone="+201001234567", gender=Gender.FEMALE)
    )
    lines = [debit("cash_main", 50), credit("patients_receivable", 50, party=patient)]
    first = services.post(org_a, date=DAY, memo="تحصيل", lines=lines, source=patient)
    again = services.post(org_a, date=DAY, memo="تحصيل تاني", lines=lines, source=patient)
    assert again.pk == first.pk and JournalEntry.objects.count() == 1
    assert services.entry_for(patient) == first and services.entry_for(patient, "refund") is None
    other_event = services.post(org_a, date=DAY, memo="حدث تاني", lines=lines, source=patient, event="refund")
    assert other_event.pk != first.pk
    line = first.lines.get(credit__gt=0)
    assert (line.party_type, line.party_id) == ("patient", patient.pk)
    assert services.balance_of(chart.account(org_a, "patients_receivable"), party=patient) == D("-100.00")


def test_closed_month_takes_nothing_and_months_close_in_order(org_a, make_member):
    user = make_member(org_a, "owner")
    _post(org_a, day=date(2026, 1, 15))
    january, february = services.period_for(org_a, date(2026, 1, 1)), services.period_for(org_a, date(2026, 2, 1))
    with pytest.raises(LedgerError, match="بالترتيب"):
        services.close_period(february, by=user)
    services.close_period(january, by=user)
    january.refresh_from_db()
    assert january.status == PeriodStatus.CLOSED and january.closed_by == user
    with pytest.raises(PeriodClosed):
        _post(org_a, day=date(2026, 1, 20))
    assert _post(org_a, day=date(2026, 2, 1)).number == 2
    services.close_period(february, by=user)
    with pytest.raises(LedgerError, match="من الآخر"):
        services.reopen_period(january, by=user)
    services.reopen_period(february, by=user)
    services.reopen_period(january, by=user)
    assert _post(org_a, day=date(2026, 1, 20)).number == 3


def test_reverse_posts_the_mirror_and_leaves_the_original(org_a, make_member):
    user = make_member(org_a, "owner")
    patient = patient_services.create_patient(
        org_a, Patient(full_name="هدى", phone="+201001234568", gender=Gender.FEMALE)
    )
    entry = services.post(
        org_a, date=DAY, memo="تحصيل",
        lines=[debit("cash_main", 80), credit("patients_receivable", 80, party=patient)],
    )  # fmt: skip
    with freeze_time("2026-03-12 10:00:00+02:00"):
        mirror = services.reverse(entry, by=user, memo="اتسجل بالغلط")
    assert (mirror.number, mirror.reverses, mirror.date, mirror.memo) == (2, entry, date(2026, 3, 12), "اتسجل بالغلط")
    assert [
        (line.account.system_key, line.debit, line.credit, line.party_id) for line in mirror.lines.order_by("line_no")
    ] == [
        ("cash_main", D("0.00"), D("80.00"), None),
        ("patients_receivable", D("80.00"), D("0.00"), patient.pk),
    ]
    entry.refresh_from_db()
    assert entry.status == EntryStatus.POSTED and entry.is_reversed and entry.total == D("80.00")
    assert services.balance_of(chart.account(org_a, "cash_main")) == 0
    assert services.balance_of(chart.account(org_a, "patients_receivable"), party=patient) == 0
    with pytest.raises(LedgerError, match="اتعكس قبل كده"):
        services.reverse(entry, by=user)
    with pytest.raises(LedgerError, match="قيد عكسي"):
        services.reverse(mirror, by=user)


# ================================================================ the database's own rules (raw SQL)


def _sql(statement, params=None):
    with connection.cursor() as cursor:
        cursor.execute(statement, params or [])


def _refused(statement, params=None):
    """The statement must be rejected by PostgreSQL itself."""
    with pytest.raises(IntegrityError), transaction.atomic():
        _sql(statement, params)
        _sql("SET CONSTRAINTS ALL IMMEDIATE")


def test_posted_entries_and_lines_cannot_be_touched_even_with_sql(org_a):
    entry = _post(org_a)
    line = entry.lines.first()
    _refused("UPDATE ledger_journalentry SET memo = 'تزوير' WHERE id = %s", [entry.pk])
    _refused("UPDATE ledger_journalentry SET status = 'draft' WHERE id = %s", [entry.pk])
    _refused("DELETE FROM ledger_journalentry WHERE id = %s", [entry.pk])
    _refused("UPDATE ledger_journalline SET debit = debit + 1 WHERE id = %s", [line.pk])
    _refused("DELETE FROM ledger_journalline WHERE id = %s", [line.pk])
    _refused(
        "INSERT INTO ledger_journalline (created_at, updated_at, organization_id, entry_id, line_no, account_id, debit,"
        " credit, party_type, memo) VALUES (now(), now(), %s, %s, 9, %s, 5, 0, '', '')",
        [org_a.pk, entry.pk, line.account_id],
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        entry.memo = "تعديل من الكود"
        entry.save()
    entry.refresh_from_db()
    assert entry.memo == "إيداع" and entry.lines.count() == 2 and services.balance_of(line.account) != 0


def _draft(org, lines):
    """A draft entry written straight to the tables, as someone with a SQL prompt would."""
    period = services.period_for(org, DAY)
    entry = JournalEntry.objects.create(
        organization=org, fiscal_year=period.fiscal_year, period=period, date=DAY, memo="يدوي"
    )
    for i, (key, d, c) in enumerate(lines, start=1):
        JournalLine.objects.create(
            organization=org, entry=entry, line_no=i, account=chart.account(org, key), debit=d, credit=c
        )
    return entry


def test_an_unbalanced_entry_cannot_become_posted_even_with_sql(org_a):
    entry = _draft(org_a, [("cash_main", 100, 0), ("capital", 0, 90)])
    _refused(
        "UPDATE ledger_journalentry SET status = 'posted', number = 1, posted_at = now() WHERE id = %s", [entry.pk]
    )
    lonely = _draft(org_a, [("cash_main", 100, 0)])
    _refused(
        "UPDATE ledger_journalentry SET status = 'posted', number = 2, posted_at = now() WHERE id = %s", [lonely.pk]
    )
    assert not JournalEntry.objects.filter(status=EntryStatus.POSTED).exists()
    # the same rows, balanced: accepted
    JournalLine.objects.filter(entry=entry, credit__gt=0).update(credit=100)
    with transaction.atomic():
        _sql(
            "UPDATE ledger_journalentry SET status = 'posted', number = 1, posted_at = now() WHERE id = %s", [entry.pk]
        )
        _sql("SET CONSTRAINTS ALL IMMEDIATE")
    assert JournalEntry.objects.get(pk=entry.pk).status == EntryStatus.POSTED


@pytest.mark.django_db(transaction=True)
def test_an_unbalanced_entry_is_rejected_at_commit(org_a):
    """Without anybody asking for the check: the COMMIT itself fails and nothing is saved."""
    with pytest.raises(IntegrityError), transaction.atomic():
        entry = _draft(org_a, [("cash_main", 100, 0), ("capital", 0, 40)])
        _sql(
            "UPDATE ledger_journalentry SET status = 'posted', number = 1, posted_at = now() WHERE id = %s", [entry.pk]
        )
    assert not JournalEntry.objects.exists() and not JournalLine.objects.exists()
    assert _post(org_a).number == 1  # and a proper one commits


def test_database_refuses_bad_lines_and_closed_periods(org_a, org_b):
    chart.ensure(org_b)
    entry = _draft(org_a, [("cash_main", 100, 0), ("capital", 0, 100)])
    base = (
        "INSERT INTO ledger_journalline (created_at, updated_at, organization_id, entry_id, line_no, account_id, debit,"
        " credit, party_type, memo) VALUES (now(), now(), %s, %s, 9, %s, %s, %s, '', '')"
    )
    cash = chart.account(org_a, "cash_main").pk
    _refused(base, [org_a.pk, entry.pk, cash, 5, 5])  # both sides
    _refused(base, [org_a.pk, entry.pk, cash, 0, 0])  # neither
    _refused(base, [org_a.pk, entry.pk, cash, -5, 0])  # negative
    _refused(base, [org_a.pk, entry.pk, Account.objects.get(organization=org_a, code="11").pk, 5, 0])  # a group
    _refused(base, [org_a.pk, entry.pk, chart.account(org_b, "cash_main").pk, 5, 0])  # another clinic's account
    _refused(base, [org_b.pk, entry.pk, cash, 5, 0])  # a line of another clinic in this entry

    FiscalPeriod.objects.filter(pk=entry.period_id).update(status=PeriodStatus.CLOSED)
    _refused(
        "UPDATE ledger_journalentry SET status = 'posted', number = 1, posted_at = now() WHERE id = %s", [entry.pk]
    )
    FiscalPeriod.objects.filter(pk=entry.period_id).update(status=PeriodStatus.OPEN)
    _refused(
        "UPDATE ledger_journalentry SET status = 'posted', posted_at = now() WHERE id = %s", [entry.pk]
    )  # no number
    _refused(
        "UPDATE ledger_journalentry SET status = 'posted', number = 1, posted_at = now(), date = '2026-04-01'"
        " WHERE id = %s",
        [entry.pk],
    )  # a date outside its period


def test_an_account_with_postings_keeps_its_nature(org_a):
    _post(org_a)
    cash = chart.account(org_a, "cash_main")
    _refused("UPDATE ledger_account SET is_group = true WHERE id = %s", [cash.pk])
    _refused("UPDATE ledger_account SET type = 'expense' WHERE id = %s", [cash.pk])
    with pytest.raises(LedgerError, match="البرنامج بيستخدمه"):
        account_rules.delete_account(cash)
    bank = chart.account(org_a, "bank_main")  # nothing posted: free to change
    _sql("UPDATE ledger_account SET type = 'asset' WHERE id = %s", [bank.pk])


@pytest.mark.django_db(transaction=True)
def test_numbers_have_no_gaps_under_concurrency(org_a):
    chart.ensure(org_a)
    services.fiscal_year_for(org_a, DAY)
    errors, barrier = [], threading.Barrier(8)

    def worker(i):
        try:
            barrier.wait()
            _post(org_a, amount=str(10 + i))
        except Exception as e:  # collected: the assertion below reports them
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    numbers = sorted(JournalEntry.objects.filter(organization=org_a).values_list("number", flat=True))
    assert numbers == list(range(1, 9))


# ================================================================ editing the chart


def test_add_rename_move_switch_off_and_remove_accounts(org_a, make_member):
    user = make_member(org_a, "owner")
    chart.ensure(org_a)
    banks = Account.objects.get(organization=org_a, code="1102")
    cib = account_rules.add_account(org_a, parent=banks, name_ar="بنك CIB", by=user)
    assert (cib.code, cib.type, cib.is_group, cib.parent) == ("110202", AccountType.ASSET, False, banks)
    with pytest.raises(LedgerError, match="مستخدم"):
        account_rules.add_account(org_a, parent=banks, name_ar="تاني", code="110202")
    with pytest.raises(LedgerError, match="يبدأ بكود"):
        account_rules.add_account(org_a, parent=banks, name_ar="تاني", code="990001")
    with pytest.raises(LedgerError, match="مجموعة"):
        account_rules.add_account(org_a, parent=cib, name_ar="تحت حساب فرعي")

    account_rules.rename(cib, name_ar="البنك التجاري الدولي", by=user)
    cash_group = Account.objects.get(organization=org_a, code="1101")
    account_rules.move(cib, parent=cash_group, by=user)
    cib.refresh_from_db()
    assert (cib.code, cib.parent, cib.name_ar) == ("110103", cash_group, "البنك التجاري الدولي")
    with pytest.raises(LedgerError, match="نفس النوع"):
        account_rules.move(cib, parent=Account.objects.get(organization=org_a, code="5201"))

    services.post(org_a, date=DAY, memo="إيداع", lines=[debit(cib, 500), credit("capital", 500)])
    with pytest.raises(LedgerError, match="عليه رصيد"):
        account_rules.set_active(cib, False)
    with pytest.raises(LedgerError, match="عليه قيود"):
        account_rules.delete_account(cib)
    services.post(org_a, date=DAY, memo="سحب", lines=[debit("capital", 500), credit(cib, 500)])
    account_rules.set_active(cib, False, by=user)
    with pytest.raises(LedgerError, match="موقوف"):
        services.post(org_a, date=DAY, memo="x", lines=[debit(cib, 1), credit("capital", 1)])
    with pytest.raises(LedgerError, match="البرنامج بيستخدمه"):
        account_rules.set_active(chart.account(org_a, "cash_main"), False)
    with pytest.raises(LedgerError, match="البرنامج بيستخدمه"):
        account_rules.delete_account(chart.account(org_a, "rent"))
    unused = account_rules.add_account(org_a, parent=banks, name_ar="حساب اتفتح بالغلط")
    account_rules.delete_account(unused)
    assert not Account.objects.filter(pk=unused.pk).exists()


# ================================================================ reports


def test_trial_balance_columns_and_roll_up(org_a):
    _post(org_a, "1000", day=date(2026, 1, 10))
    services.post(
        org_a,
        date=date(2026, 2, 5),
        memo="إيجار",
        lines=[debit("rent", 300, cost_center="admin"), credit("cash_main", 300)],
    )
    services.post(
        org_a,
        date=date(2026, 2, 9),
        memo="إيراد",
        lines=[debit("cash_main", 120), credit("revenue_other", 120, cost_center="admin")],
    )

    tb = reports.trial_balance(org_a, date(2026, 2, 1), date(2026, 2, 28))
    rows = {r.account.code: r for r in tb.rows}
    cash = rows["110101"]
    assert (cash.opening_debit, cash.debit, cash.credit, cash.closing_debit) == (
        D("1000.00"),
        D("120.00"),
        D("300.00"),
        D("820.00"),
    )
    assert (rows["310101"].opening_credit, rows["310101"].closing_credit) == (D("1000.00"), D("1000.00"))
    assert (rows["520102"].debit, rows["420101"].credit) == (D("300.00"), D("120.00"))
    for group in ("1", "11", "1101"):  # every level above the safe carries its amounts
        assert rows[group].closing_debit == D("820.00") and rows[group].depth < cash.depth
    assert "110201" not in rows and "1102" not in rows  # nothing moved there: hidden
    t = tb.totals
    assert tb.balanced and (t.opening_debit, t.debit, t.closing_debit) == (D("1000.00"), D("420.00"), D("1120.00"))
    assert (t.opening_credit, t.credit, t.closing_credit) == (t.opening_debit, t.debit, t.closing_debit)

    everything = reports.trial_balance(org_a, date(2026, 2, 1), date(2026, 2, 28), hide_zero=False)
    assert len(everything.rows) == len(chart.CHART)
    top = reports.trial_balance(org_a, date(2026, 2, 1), date(2026, 2, 28), max_depth=1)
    assert [r.account.code for r in top.rows] == ["1", "3", "4", "5"] and top.balanced


def test_account_statement_runs_the_balance(org_a):
    _post(org_a, "1000", day=date(2026, 1, 10))
    services.post(
        org_a,
        date=date(2026, 2, 5),
        memo="إيجار",
        lines=[debit("rent", 300, cost_center="admin"), credit("cash_main", 300)],
    )
    services.post(
        org_a,
        date=date(2026, 2, 9),
        memo="إيراد",
        lines=[debit("cash_main", 120), credit("revenue_other", 120, cost_center="admin")],
    )
    st_ = reports.account_statement(org_a, chart.account(org_a, "cash_main"), date(2026, 2, 1), date(2026, 2, 28))
    assert (st_.opening, st_.debit, st_.credit, st_.closing) == (D("1000.00"), D("120.00"), D("300.00"), D("820.00"))
    assert [row.balance for row in st_.lines] == [D("700.00"), D("820.00")]
    capital = reports.account_statement(org_a, chart.account(org_a, "capital"), date(2026, 1, 1), date(2026, 12, 31))
    assert (capital.opening, capital.closing) == (D("0.00"), D("1000.00"))  # a credit account reads positive
    assert [e.number for e in reports.journal(org_a, date(2026, 2, 1), date(2026, 2, 28))] == [2, 3]
    assert [e.memo for e in reports.journal(org_a, date(2026, 1, 1), date(2026, 12, 31), q="إيجار")] == ["إيجار"]


# ================================================================ any sequence of entries keeps the books right

LEAVES = ["cash_main", "bank_main", "capital", "revenue_other", "rent", "salaries", "suppliers_payable", "inventory"]
NEEDS_CENTER = {"revenue_other", "rent", "salaries"}

entry_strategy = st.lists(
    st.tuples(st.sampled_from(LEAVES), st.decimals(min_value=D("0.01"), max_value=D("99999.99"), places=2)),
    min_size=1,
    max_size=5,
)


def _line(key, amount, side):
    extra = {"cost_center": "admin"} if key in NEEDS_CENTER else {}
    return debit(key, amount, **extra) if side == "debit" else credit(key, amount, **extra)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    entries=st.lists(st.tuples(entry_strategy, st.sampled_from(LEAVES), st.booleans()), min_size=1, max_size=8),
    days=st.lists(st.integers(min_value=0, max_value=364), min_size=8, max_size=8),
)
def test_any_sequence_of_entries_keeps_every_invariant(org_a, entries, days):
    """Random entries (one side spread over up to five accounts, the other side on one), some of them reversed:
    after every step each posted entry balances, the trial balance balances, numbers have no gaps and every account's
    balance equals the sum of its own lines."""
    sid = transaction.savepoint()
    try:
        chart.ensure(org_a)
        expected: dict[str, Decimal] = {}
        for i, (parts, other, flip) in enumerate(entries):
            total = sum(amount for _key, amount in parts)
            many, one = ("debit", "credit") if flip else ("credit", "debit")
            lines = [_line(key, amount, many) for key, amount in parts] + [_line(other, total, one)]
            day = date(2026, 1, 1).fromordinal(date(2026, 1, 1).toordinal() + days[i])
            entry = services.post(org_a, date=day, memo=f"قيد {i}", lines=lines)
            sign = 1 if flip else -1
            for key, amount in parts:
                expected[key] = expected.get(key, D(0)) + sign * amount
            expected[other] = expected.get(other, D(0)) - sign * total
            if i % 3 == 2:
                services.reverse(entry, by=None, date=day)
                for key, amount in parts:
                    expected[key] -= sign * amount
                expected[other] += sign * total

            posted = JournalEntry.objects.filter(organization=org_a, status=EntryStatus.POSTED)
            for e in posted.prefetch_related("lines"):
                lines_ = list(e.lines.all())
                assert len(lines_) >= 2
                assert sum(x.debit for x in lines_) == sum(x.credit for x in lines_) == e.total
            assert sorted(posted.values_list("number", flat=True)) == list(range(1, posted.count() + 1))
            tb = reports.trial_balance(org_a, date(2026, 1, 1), date(2026, 12, 31))
            assert tb.balanced
            for key, net in expected.items():
                account = chart.account(org_a, key)
                signed = net if account.debit_normal else -net
                assert services.balance_of(account) == signed, key
    finally:
        transaction.savepoint_rollback(sid)
