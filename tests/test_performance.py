"""Phase 9 N+1 review: query counts of the busy screens must not grow with the number of rows."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.billing.services import record_payment
from apps.center import services as center
from apps.center.models import Service
from apps.clinical import services as clinical
from apps.clinical.models import Vitals
from apps.doctors.models import Doctor, VisitType, WorkingPeriod
from apps.patients.models import Allergy, Gender, Patient
from apps.scheduling import services as booking
from apps.scheduling.models import AppointmentStatus

pytestmark = pytest.mark.usefixtures("midday")


@pytest.fixture
def clinic(org_a, make_member):
    doctor = Doctor.objects.create(organization=org_a, name_ar="سارة", slot_minutes=10)
    for weekday in range(7):
        WorkingPeriod.objects.create(organization=org_a, doctor=doctor, weekday=weekday,
                                     start_time="00:00", end_time="23:59")  # fmt: skip
    vt = VisitType.objects.create(organization=org_a, doctor=doctor, name_ar="كشف", duration_minutes=10,
                                  price=Decimal("300"))  # fmt: skip
    service = Service.objects.create(organization=org_a, name_ar="سونار", price=Decimal("250"))
    return {"org": org_a, "doctor": doctor, "vt": vt, "reception": make_member(org_a, "reception"),
            "doc": make_member(org_a, "doctor"), "n": 0, "service": service}  # fmt: skip


def _add_patients(clinic, count):
    """Each patient: booked today, arrived (with vitals), paid; half of them also completed visits before."""
    base = timezone.now().replace(second=0, microsecond=0)
    patients = []
    for _ in range(count):
        clinic["n"] += 1
        n = clinic["n"]
        p = Patient.objects.create(organization=clinic["org"], full_name=f"مريض {n}", phone=f"+2010012{n:05d}",
                                   gender=Gender.MALE, file_number=n)  # fmt: skip
        Allergy.objects.create(organization=clinic["org"], patient=p, name="Penicillin")
        a = booking.book(patient=p, doctor=clinic["doctor"], visit_type=clinic["vt"], by=clinic["reception"],
                         start_at=base + timedelta(minutes=10 * n), overbook=True)  # fmt: skip
        booking.transition(a, AppointmentStatus.ARRIVED, by=clinic["reception"])
        record_payment(a, amount=150, by=clinic["reception"])
        center.order_service(a, clinic["service"], by=clinic["reception"])  # center mode: a service on every visit
        if n % 3 == 0:  # and every third patient also came in as an emergency before
            e = Patient.objects.create(organization=clinic["org"], full_name=f"طوارئ {n}", phone=f"+2010099{n:05d}",
                                       gender=Gender.FEMALE, file_number=100000 + n)  # fmt: skip
            center.register_emergency(org=clinic["org"], patient=e, doctor=clinic["doctor"], triage="urgent",
                                      reason="pain", by=clinic["reception"])  # fmt: skip
        patients.append((p, a))
    return patients


def _queries(client, url):
    """Warm measurement: the first hit may do one-off writes (hourly audit event, get-or-create settings rows)."""
    client.get(url)
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(url)
    assert resp.status_code == 200, url
    return len(ctx.captured_queries)


def test_reception_screen_is_constant_in_queries(client, clinic):
    client.force_login(clinic["reception"])
    _add_patients(clinic, 3)
    small = (_queries(client, reverse("reception:today")), _queries(client, reverse("reception:rows")))
    _add_patients(clinic, 12)
    big = (_queries(client, reverse("reception:today")), _queries(client, reverse("reception:rows")))
    assert big == small, (small, big)


def test_doctor_screens_are_constant_in_queries(client, clinic):
    client.force_login(clinic["doc"])
    first = _add_patients(clinic, 3)
    patient, appt = first[0]

    def history_rich(count):
        """Give the first patient `count` finished visits with vitals and diagnoses."""
        for _ in range(count):
            _p2, a2 = _add_patients(clinic, 1)[0]
            a2.patient = patient
            a2.save(update_fields=["patient"])
            v = clinical.start_visit(a2, by=clinic["doc"])
            Vitals.objects.create(organization=clinic["org"], visit=v, weight_kg=80, bp_systolic=120, bp_diastolic=80)
            clinical.save_field(v, "diagnosis", "برد")
            clinical.finish_visit(v, by=clinic["doc"])

    visit = clinical.start_visit(appt, by=clinic["doc"])
    urls = [
        reverse("clinical:visit", args=[visit.pk]),
        reverse("clinical:history", args=[patient.pk]),
        reverse("clinical:record", args=[patient.pk]),
        reverse("prescriptions:patient_list", args=[patient.pk]),
    ]
    history_rich(2)
    small = [_queries(client, u) for u in urls]
    history_rich(8)
    _add_patients(clinic, 8)  # a longer queue too
    big = [_queries(client, u) for u in urls]
    assert big == small, list(zip(urls, small, big, strict=True))


# --- the money screens (phases 14-19): lists must not cost a query per row ------------------------------------------


@pytest.fixture
def books(clinic, make_member):
    from apps.inventory import services as stock
    from apps.ledger import chart
    from apps.purchasing import services as purchasing
    from apps.treasury import services as treasury
    from apps.treasury.models import VoucherKind

    org = clinic["org"]
    owner = make_member(org, "owner")
    chart.ensure(org)
    treasury.ensure(org)
    purchasing.ensure_defaults(org)
    stock.ensure_defaults(org)
    bank = treasury.treasury(org, "bank_main")
    treasury.create_voucher(
        org, kind=VoucherKind.RECEIPT, treasury=bank, amount=Decimal("500000"), date=timezone.localdate(),
        description="رأس المال", by=owner, counter_account=chart.account(org, "capital"),
    )  # fmt: skip
    return {**clinic, "owner": owner, "bank": bank, "k": 0}


def _add_documents(books, count):
    """`count` of everything: an invoiced visit, a supplier with a bill (one stock line, one expense line) paid by
    cheque, a stock issue, a voucher."""
    from apps.inventory import services as stock
    from apps.inventory.models import StockItem
    from apps.ledger import chart
    from apps.purchasing import services as purchasing
    from apps.purchasing.models import PayMethod
    from apps.purchasing.services import LineIn
    from apps.treasury import services as treasury
    from apps.treasury.models import VoucherKind

    org, owner, today = books["org"], books["owner"], timezone.localdate()
    main = stock.warehouse(org, "main")
    for _patient, appt in _add_patients(books, count):
        record_payment(appt, amount=400, by=owner)  # the rest of the visit and its service: invoiced and settled
    for _ in range(count):
        books["k"] += 1
        k = books["k"]
        item = StockItem.objects.create(organization=org, name=f"صنف {k}", reorder_level=Decimal("50"))
        supplier = purchasing.save_supplier(org, by=owner, name=f"مورد {k}")
        bill = purchasing.create_invoice(
            org, supplier=supplier, date=today, by=owner, supplier_ref=f"R{k}",
            lines=[
                LineIn("", None, "10", "20", item=item, warehouse=main),
                LineIn("صيانة", chart.account(org, "sundry_expenses"), "1", "300", chart.cost_center(org, "admin")),
            ],
        )  # fmt: skip
        purchasing.pay_supplier(
            org, supplier=supplier, amount="200", method=PayMethod.CHEQUE, treasury_obj=books["bank"], by=owner,
            allocations=[(bill, "200")], cheque_number=f"C{k}", cheque_due=today + timedelta(days=3),
        )  # fmt: skip
        stock.issue(org, warehouse=main, lines=[(item, "2")], cost_center=chart.cost_center(org, "theatre"), by=owner)
        treasury.create_voucher(
            org, kind=VoucherKind.PAYMENT, treasury=books["bank"], amount=Decimal("100"), date=today,
            description=f"مصروف {k}", by=owner, counter_account=chart.account(org, "rent"),
            cost_center=chart.cost_center(org, "admin"),
        )  # fmt: skip


MONEY_SCREENS = [
    "invoicing:list", "invoicing:accounts", "invoicing:doctors", "invoicing:payers", "purchasing:invoices",
    "purchasing:suppliers", "purchasing:payments", "purchasing:aging", "purchasing:taxes", "inventory:items",
    "inventory:docs", "treasury:home", "treasury:cheques", "treasury:vouchers", "treasury:expenses",
    "ledger:trial_balance", "ledger:income_statement", "ledger:balance_sheet", "ledger:journal",
    "invoicing:report_revenue", "invoicing:report_receivables", "invoicing:report_cash", "billing:cashbox",
]  # fmt: skip


def test_money_screens_are_constant_in_queries(client, books):
    client.force_login(books["owner"])
    urls = [reverse(name) for name in MONEY_SCREENS]
    _add_documents(books, 2)
    small = [_queries(client, u) for u in urls]
    _add_documents(books, 7)
    big = [_queries(client, u) for u in urls]
    grew = [(u, a, b) for u, a, b in zip(urls, small, big, strict=True) if b > a]
    assert not grew, grew
