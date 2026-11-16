"""Patient invoicing (plan 14 §14.6): the exact entry of every document, visits invoiced behind the scenes and kept
right as they change, account invoices for operations and stays, deposits, doctor shares, payers, credit notes, and
the proof that for any sequence of events the invoices, the subledgers and the books say the same thing."""

import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from openpyxl import load_workbook

from apps.billing import services as billing
from apps.billing.models import Payment, PaymentKind
from apps.center import services as center
from apps.center.models import Service
from apps.core.timeutils import CAIRO
from apps.doctors.models import Doctor, VisitType, WorkingPeriod
from apps.invoicing import services as invoicing
from apps.invoicing.models import Invoice, InvoiceKind, InvoiceLine, InvoiceSettings, InvoiceShare, PatientCoverage
from apps.invoicing.services import InvoiceError
from apps.ledger import chart, reports
from apps.ledger import services as ledger
from apps.ledger.bootstrap import invoice_visits
from apps.ledger.models import EntryStatus, FiscalPeriod, JournalEntry
from apps.organizations.models import Membership
from apps.patients import services as patient_services
from apps.patients.models import Gender, Patient
from apps.scheduling import services as booking
from apps.scheduling.models import Appointment, AppointmentStatus
from apps.theatre import services as theatre
from apps.theatre.models import OperationStatus, ProcedureType, Room, RoomKind, TeamRole
from apps.treasury import services as treasury
from apps.treasury.models import Voucher, VoucherKind

D = Decimal
S = AppointmentStatus
NOW = "2026-03-10 12:00:00+02:00"
TODAY = date(2026, 3, 10)

pytestmark = pytest.mark.usefixtures("clock")


@pytest.fixture
def clock():
    with freeze_time(NOW, tick=True):
        yield


@pytest.fixture
def world(org_a, org_b, make_member, clock):
    doctor = Doctor.objects.create(organization=org_a, name_ar="سارة علي", title_ar="د.", slot_minutes=10)
    for weekday in range(7):
        WorkingPeriod.objects.create(
            organization=org_a, doctor=doctor, weekday=weekday, start_time="00:00", end_time="23:59"
        )
    vt = VisitType.objects.create(organization=org_a, doctor=doctor, name_ar="كشف", duration_minutes=10, price=D("400"))
    sonar = Service.objects.create(organization=org_a, name_ar="سونار", price=D("300"), doctor_share_percent=D("50"))
    theatre.ensure_defaults(org_a)
    treasury.ensure(org_a)
    owner = make_member(org_a, "owner")
    return {
        "org": org_a,
        "doctor": doctor,
        "vt": vt,
        "sonar": sonar,
        "owner": owner,
        "owner_m": Membership.objects.get(user=owner),
        "reception": make_member(org_a, "reception"),
        "doctor_user": make_member(org_a, "doctor"),
        "viewer": make_member(org_a, "viewer"),
        "other_owner": make_member(org_b, "owner"),
        "other_org": org_b,
        "n": 0,
    }


def _patient(world):
    world["n"] += 1
    n = world["n"]
    return patient_services.create_patient(
        world["org"], Patient(full_name=f"مريضة {n}", phone=f"+2010033{n:05d}", gender=Gender.FEMALE)
    )


def _appt(world, patient=None, price=None):
    patient = patient or _patient(world)
    world["n"] += 1
    start = timezone.now().replace(second=0, microsecond=0) + timedelta(minutes=10 * world["n"])
    appt = booking.book(
        patient=patient, doctor=world["doctor"], visit_type=world["vt"], by=world["reception"], start_at=start,
        overbook=True, notify=False,
    )  # fmt: skip
    if price is not None:
        appt.price = price
        appt.save(update_fields=["price"])
    return appt


def _go(appt, *statuses, by=None):
    for status in statuses:
        appt = booking.transition(appt, status, by=by)
    return appt


def _lines(source, event=""):
    """An entry as a sorted list of (account key, debit, credit, cost center key, party type, party id)."""
    entry = ledger.entry_for(source, event)
    assert entry is not None, f"no entry for {source} / {event!r}"
    return sorted(
        (
            line.account.system_key, line.debit, line.credit,
            line.cost_center.system_key if line.cost_center_id else "", line.party_type, line.party_id,
        )
        for line in entry.lines.select_related("account", "cost_center")
    )  # fmt: skip


def _bal(org, key):
    return ledger.balance_of(chart.account(org, key))


def _invoices(appt_or_patient):
    key = "appointment" if isinstance(appt_or_patient, Appointment) else "patient"
    return list(Invoice.objects.filter(**{key: appt_or_patient}).order_by("id"))


# ================================================================ a visit is invoiced behind the scenes


def test_a_paid_visit_is_invoiced_once_the_patient_is_in_the_center(world):
    org, user = world["org"], world["reception"]
    appt = _appt(world)
    patient = appt.patient
    billing.record_payment(appt, amount=D("400"), by=user)
    assert _invoices(appt) == []  # paid at booking, not here yet: her money is a credit on her account
    assert invoicing.patient_balance(patient) == D("-400")

    appt = _go(appt, S.ARRIVED, by=user)
    (invoice,) = _invoices(appt)
    assert (invoice.kind, invoice.label, invoice.date) == (InvoiceKind.INVOICE, "INV-2026-00001", TODAY)
    assert (invoice.gross, invoice.discount, invoice.total, invoice.patient_share) == (D(400), D(0), D(400), D(400))
    assert _lines(invoice) == [
        ("patients_receivable", D("400.00"), D("0.00"), "", "patient", patient.pk),
        ("revenue_visits", D("0.00"), D("400.00"), "outpatient", "", None),
    ]
    assert ledger.entry_for(invoice, "shares") is None  # this doctor has no share of the visit fee
    assert invoicing.patient_balance(patient) == 0

    _go(appt, S.IN_CONSULTATION, S.COMPLETED, by=user)
    assert invoicing.sync_appointment(appt) == []  # asking again changes nothing
    assert len(_invoices(appt)) == 1 and _bal(org, "revenue_visits") == D("400")


def test_a_finished_visit_that_was_not_paid_is_invoiced_and_owed(world):
    user = world["reception"]
    appt = _go(_appt(world), S.ARRIVED, by=user)
    assert _invoices(appt) == []  # here, but nothing paid and not finished: nothing to recognise yet
    appt = _go(appt, S.IN_CONSULTATION, S.COMPLETED, by=user)
    assert len(_invoices(appt)) == 1
    assert invoicing.patient_balance(appt.patient) == D("400")
    states, left = invoicing.account_state(appt.patient)
    assert (states[0].paid, states[0].remaining, left) == (D(0), D(400), D(0))
    billing.record_payment(appt, amount=D("150"), by=user)
    states, left = invoicing.account_state(appt.patient)
    assert (states[0].paid, states[0].remaining) == (D(150), D(250))
    assert invoicing.patient_balance(appt.patient) == D("250") and len(_invoices(appt)) == 1


def test_services_and_doctor_shares_post_exactly(world):
    org, user, doctor = world["org"], world["reception"], world["doctor"]
    doctor.visit_share_percent = D("40")
    doctor.save()
    appt = _appt(world)
    center.order_service(appt, world["sonar"], by=user)
    billing.record_payment(appt, amount=D("700"), by=user)
    appt = _go(appt, S.ARRIVED, by=user)
    (invoice,) = _invoices(appt)
    assert [(line.kind, line.gross) for line in invoice.lines.all()] == [("visit", D(400)), ("service", D(300))]
    assert _lines(invoice) == [
        ("patients_receivable", D("700.00"), D("0.00"), "", "patient", appt.patient_id),
        ("revenue_services", D("0.00"), D("300.00"), "outpatient", "", None),
        ("revenue_visits", D("0.00"), D("400.00"), "outpatient", "", None),
    ]
    # 40% of the visit (160) + 50% of the scan (150)
    assert _lines(invoice, "shares") == [
        ("doctor_fees_expense", D("310.00"), D("0.00"), "outpatient", "", None),
        ("doctor_payables", D("0.00"), D("310.00"), "", "doctor", doctor.pk),
    ]
    assert invoicing.doctor_balance(doctor) == D("310")

    # one more scan after the visit was invoiced: an invoice of its own
    second = center.order_service(appt, world["sonar"], by=user)
    assert [i.total for i in _invoices(appt)] == [D(700), D(300)]
    assert invoicing.patient_balance(appt.patient) == D("300") and invoicing.doctor_balance(doctor) == D("460")
    # taken off again (not paid for): its line goes back by a credit note, and so does the doctor's share of it
    center.cancel_order(second, by=user)
    kinds = [(i.kind, i.total) for i in _invoices(appt)]
    assert kinds == [("invoice", D(700)), ("invoice", D(300)), ("credit", D(300))]
    note = _invoices(appt)[-1]
    assert note.label == "CN-2026-00001" and note.credit_of == _invoices(appt)[1]
    assert _lines(note) == [
        ("patients_receivable", D("0.00"), D("300.00"), "", "patient", appt.patient_id),
        ("revenue_services", D("300.00"), D("0.00"), "outpatient", "", None),
    ]
    assert _lines(note, "shares") == [
        ("doctor_fees_expense", D("0.00"), D("150.00"), "outpatient", "", None),
        ("doctor_payables", D("150.00"), D("0.00"), "", "doctor", doctor.pk),
    ]
    assert invoicing.patient_balance(appt.patient) == 0 and invoicing.doctor_balance(doctor) == D("310")
    assert _bal(org, "revenue_services") == D("300") and _bal(org, "doctor_fees_expense") == D("310")
    assert invoicing.sync_appointment(appt) == []


def test_an_emergency_visit_goes_to_emergency_revenue(world):
    from apps.center.models import CenterSettings, Triage

    org, user = world["org"], world["reception"]
    settings_row = CenterSettings.for_org(org)
    settings_row.emergency_fee = D("600")
    settings_row.save()
    case = center.register_emergency(
        org=org, patient=_patient(world), doctor=world["doctor"], by=user, triage=Triage.URGENT, reason="bleeding"
    )
    appt = case.appointment
    billing.record_payment(appt, amount=D("600"), by=user)
    (invoice,) = _invoices(appt)
    assert _lines(invoice) == [
        ("patients_receivable", D("600.00"), D("0.00"), "", "patient", appt.patient_id),
        ("revenue_emergency", D("0.00"), D("600.00"), "emergency", "", None),
    ]


def test_a_visit_that_is_undone_or_cancelled_gives_its_revenue_back(world):
    org, user = world["org"], world["reception"]
    appt = _appt(world)
    billing.record_payment(appt, amount=D("400"), by=user)
    appt = _go(appt, S.ARRIVED, by=user)
    assert _bal(org, "revenue_visits") == D("400")
    appt = _go(appt, S.BOOKED, by=user)  # "رجوع خطوة": she is not here after all
    assert [i.kind for i in _invoices(appt)] == ["invoice", "credit"]
    assert _bal(org, "revenue_visits") == 0 and invoicing.patient_balance(appt.patient) == D("-400")
    booking.cancel(appt, by=user, notify=False)
    assert len(_invoices(appt)) == 2  # nothing more to take back
    billing.record_payment(appt, amount=D("400"), by=user, is_refund=True)
    assert invoicing.patient_balance(appt.patient) == 0
    assert reports.trial_balance(org, date(2026, 1, 1), date(2026, 12, 31)).balanced


def test_money_paid_in_advance_follows_a_rescheduled_visit(world):
    user = world["reception"]
    appt = _appt(world)
    billing.record_payment(appt, amount=D("400"), by=user)
    new = booking.reschedule(appt, by=user, new_start_at=appt.start_at + timedelta(days=1))
    assert billing.balance(new).status == "paid" and not appt.payments.exists()
    with freeze_time(new.start_at):
        new = _go(new, S.ARRIVED, by=user)
        assert [i.total for i in _invoices(new)] == [D(400)] and invoicing.patient_balance(new.patient) == 0


def test_merging_two_files_moves_everything_and_refuses_an_open_account(world):
    from apps.patients import services as patients

    user = world["reception"]
    keep, extra = _patient(world), _patient(world)
    paid = _appt(world, patient=extra)
    billing.record_payment(paid, amount=D("400"), by=user)
    _go(paid, S.ARRIVED, S.IN_CONSULTATION, S.COMPLETED, by=user)
    unpaid = _go(_appt(world, patient=extra), S.ARRIVED, S.IN_CONSULTATION, S.COMPLETED, by=user)
    with pytest.raises(patients.MergeError, match="حساب مفتوح"):  # she still owes for a visit: settle it first
        patients.merge_patients(keep, extra)
    billing.record_payment(unpaid, amount=D("400"), by=user)

    merged = patients.merge_patients(keep, extra, by=user)
    extra.refresh_from_db()
    assert (merged, extra.merged_into, extra.is_active) == (keep, keep, False)
    assert (
        Appointment.objects.filter(patient=keep).count() == 2 and not Appointment.objects.filter(patient=extra).exists()
    )
    assert Invoice.objects.filter(patient=keep).count() == 2 and Payment.objects.filter(patient=keep).count() == 2
    states, left = invoicing.account_state(keep)
    assert [state.remaining for state in states] == [D(0), D(0)] and left == 0
    assert invoicing.patient_balance(keep) == 0
    with pytest.raises(patients.MergeError, match="متدمج بالفعل"):
        patients.merge_patients(keep, extra)
    with pytest.raises(patients.MergeError, match="متدمج في ملف تاني"):
        patients.merge_patients(extra, _patient(world))


# ================================================================ discounts


def test_visit_discount_needs_the_permission_and_respects_the_limit(world):
    org, user = world["org"], world["reception"]
    doctor_m = Membership.objects.get(user=world["doctor_user"])
    reception_m = Membership.objects.get(user=user)
    assert InvoiceSettings.for_org(org).discount_limit_percent == D("10")
    appt = _appt(world)
    with pytest.raises(InvoiceError, match="مش مسموح"):
        invoicing.set_visit_discount(appt, amount=D("20"), reason="قريبة", by=user, membership=reception_m)
    with pytest.raises(InvoiceError, match="سبب"):
        invoicing.set_visit_discount(appt, amount=D("20"), reason=" ", by=user, membership=doctor_m)
    with pytest.raises(InvoiceError, match="أكبر من الحد"):
        invoicing.set_visit_discount(appt, amount=D("100"), reason="قريبة", by=user, membership=doctor_m)
    with pytest.raises(InvoiceError, match="بين صفر وسعر الكشف"):
        invoicing.set_visit_discount(appt, amount=D("401"), reason="قريبة", by=user, membership=world["owner_m"])
    appt = invoicing.set_visit_discount(appt, amount=D("40"), reason="قريبة", by=user, membership=doctor_m)
    assert (appt.discount, appt.discount_reason, billing.balance(appt).due) == (D(40), "قريبة", D(360))

    billing.record_payment(appt, amount=D("360"), by=user)
    appt = _go(appt, S.ARRIVED, by=user)
    (invoice,) = _invoices(appt)
    assert (invoice.gross, invoice.discount, invoice.total) == (D(400), D(40), D(360))
    assert _lines(invoice) == [
        ("discounts_allowed", D("40.00"), D("0.00"), "outpatient", "", None),
        ("patients_receivable", D("360.00"), D("0.00"), "", "patient", appt.patient_id),
        ("revenue_visits", D("0.00"), D("400.00"), "outpatient", "", None),
    ]
    # the owner gives more after the invoice: the old invoice is taken back whole and a right one issued
    invoicing.set_visit_discount(appt, amount=D("100"), reason="حالة خاصة", by=user, membership=world["owner_m"])
    docs = _invoices(appt)
    assert [(d.kind, d.total) for d in docs] == [("invoice", D(360)), ("credit", D(360)), ("invoice", D(300))]
    assert _bal(org, "revenue_visits") == D("400") and _bal(org, "discounts_allowed") == D("-100")
    assert invoicing.patient_balance(appt.patient) == D("-60")  # she paid 360 for a visit that is now 300
    assert invoicing.sync_appointment(appt) == []


# ================================================================ payers (جهات التعاقد)


def test_a_covered_patient_pays_her_part_and_the_payer_owes_the_rest(world):
    org, user = world["org"], world["reception"]
    payer = invoicing.save_payer(org, name="شركة النيل", by=user, default_percent=D("80"))
    appt = _appt(world)
    patient = appt.patient
    invoicing.set_coverage(patient, payer=payer, percent=D("80"), member_no="N-77", by=user)
    bal = billing.balance(appt)
    assert (bal.due, bal.covered, bal.status) == (D("80.00"), D("320.00"), "unpaid")
    with pytest.raises(billing.PaymentError):
        billing.record_payment(appt, amount=D("0"), by=user)
    billing.record_payment(appt, amount=D("80"), by=user)
    assert billing.balance(appt).status == "paid"
    appt = _go(appt, S.ARRIVED, by=user)
    (invoice,) = _invoices(appt)
    assert (invoice.payer, invoice.coverage_percent) == (payer, D("80"))
    assert (invoice.total, invoice.payer_share, invoice.patient_share) == (D(400), D(320), D(80))
    assert _lines(invoice) == [
        ("patients_receivable", D("80.00"), D("0.00"), "", "patient", patient.pk),
        ("payers_receivable", D("320.00"), D("0.00"), "", "payer", payer.pk),
        ("revenue_visits", D("0.00"), D("400.00"), "outpatient", "", None),
    ]
    assert invoicing.patient_balance(patient) == 0 and invoicing.payer_balance(payer) == D("320")

    bank = treasury.treasury(org, "bank_main")
    with pytest.raises(InvoiceError, match="أكبر من المستحق"):
        invoicing.receive_from_payer(payer, amount=D("321"), treasury_obj=bank, by=world["owner"])
    voucher = invoicing.receive_from_payer(payer, amount=D("300"), treasury_obj=bank, by=world["owner"])
    assert (voucher.kind, voucher.party_type, voucher.party_id) == (VoucherKind.RECEIPT, "payer", payer.pk)
    assert _lines(voucher) == [
        ("bank_main", D("300.00"), D("0.00"), "", "", None),
        ("payers_receivable", D("0.00"), D("300.00"), "", "payer", payer.pk),
    ]
    assert invoicing.payer_balance(payer) == D("20") and treasury.balance(bank) == D("300")
    treasury.cancel_voucher(voucher, by=world["owner"])  # the reversal keeps the payer on its lines
    assert invoicing.payer_balance(payer) == D("320")

    # taken off the payer: invoices already issued stay, new ones are all hers
    invoicing.set_coverage(patient, payer=None, percent=0, by=user)
    assert not PatientCoverage.objects.get(patient=patient).is_active
    assert billing.balance(_appt(world, patient=patient)).covered == 0


# ================================================================ the patient's account: operations, stays, charges


def _operation(world, patient, price="5000", surgeon_fee="2000", outside_fee="800"):
    org, user = world["org"], world["doctor_user"]
    room = Room.objects.filter(organization=org, kind=RoomKind.OPERATING).first()
    procedure = ProcedureType.objects.get(organization=org, name_ar="كحت وتنظيف")
    start = datetime.combine(TODAY, time(13, 0), tzinfo=CAIRO)
    op = theatre.book_operation(
        org=org, patient=patient, procedure=procedure, room=room, surgeon=world["doctor"], start=start, by=user,
        price=D(price), notify=False,
    )  # fmt: skip
    team = {m.role: m for m in op.team.all()}
    theatre.save_team_member(
        op, role=TeamRole.SURGEON, by=user, member=team[TeamRole.SURGEON], doctor=world["doctor"], fee=surgeon_fee
    )
    theatre.save_team_member(
        op, role=TeamRole.ANAESTHETIST, by=user, member=team[TeamRole.ANAESTHETIST], name="د. تخدير", fee=outside_fee
    )
    return op


def _finish(op, user):
    for item in op.checklist.filter(done=False):
        theatre.toggle_checklist(item, by=user)
    for status in (
        OperationStatus.ADMITTED,
        OperationStatus.IN_THEATRE,
        OperationStatus.RECOVERY,
        OperationStatus.DONE,
    ):
        op = theatre.transition(op, status, by=user)
    return op


def _stay(world, patient, nights=2, rate="800", op=None):
    org, user = world["org"], world["reception"]
    ward = Room.objects.filter(organization=org, kind=RoomKind.INPATIENT).first()
    ward.nightly_rate = D(rate)
    ward.save()
    stay = theatre.admit(
        org=org, patient=patient, room=ward, by=user, operation=op, now=timezone.now() - timedelta(days=nights)
    )
    return stay


def test_an_operation_and_a_stay_are_invoiced_from_the_account_with_the_deposit_applied(world):
    org, user, owner = world["org"], world["reception"], world["owner"]
    patient = _patient(world)
    op = _operation(world, patient)
    deposit = billing.record_account_payment(patient, amount=D("3000"), by=user, kind=PaymentKind.DEPOSIT)
    assert _lines(deposit) == [
        ("cash_reception", D("3000.00"), D("0.00"), "", "", None),
        ("patient_deposits", D("0.00"), D("3000.00"), "", "patient", patient.pk),
    ]
    assert (invoicing.deposit_balance(patient), invoicing.patient_balance(patient)) == (D(3000), D(0))
    stay = _stay(world, patient, nights=2, op=op)
    charge = invoicing.add_charge(patient, description="مستلزمات", unit_price="75", quantity=2, by=user)

    items = {i.key: i for i in invoicing.open_items(patient)}
    assert set(items) == {f"operation:{op.pk}", f"admission:{stay.pk}", f"charge:{charge.pk}"}
    assert [items[k].ready for k in sorted(items)] == [False, True, False]  # admission, charge, operation
    with pytest.raises(InvoiceError, match="مينفعش يتفوتر دلوقتي"):
        invoicing.issue_account_invoice(patient, keys=list(items), by=user)

    op = _finish(op, world["doctor_user"])
    theatre.discharge(stay, by=user, summary="بخير")
    keys = [i.key for i in invoicing.open_items(patient)]
    with pytest.raises(InvoiceError, match="مش مسموح"):
        invoicing.issue_account_invoice(
            patient, keys=keys, by=user, discount="500", reason="اتفاق", membership=Membership.objects.get(user=user)
        )
    invoice = invoicing.issue_account_invoice(
        patient, keys=keys, by=owner, discount="500", reason="اتفاق", membership=world["owner_m"]
    )
    # 5000 + 2 x 800 + 150 = 6750, less 500 spread by price (370.37 / 118.52 / 11.11)
    assert (invoice.gross, invoice.discount, invoice.total, invoice.appointment) == (D(6750), D(500), D(6250), None)
    by_kind = {line.kind: line for line in invoice.lines.all()}
    assert (by_kind["operation"].discount, by_kind["room"].discount, by_kind["charge"].discount) == (
        D("370.37"), D("118.52"), D("11.11"),
    )  # fmt: skip
    assert (by_kind["room"].quantity, by_kind["room"].unit_price, by_kind["room"].gross) == (2, D(800), D(1600))
    assert _lines(invoice) == [
        ("discounts_allowed", D("11.11"), D("0.00"), "outpatient", "", None),
        ("discounts_allowed", D("118.52"), D("0.00"), "inpatient", "", None),
        ("discounts_allowed", D("370.37"), D("0.00"), "theatre", "", None),
        ("patients_receivable", D("6250.00"), D("0.00"), "", "patient", patient.pk),
        ("revenue_operations", D("0.00"), D("5000.00"), "theatre", "", None),
        ("revenue_rooms", D("0.00"), D("1600.00"), "inpatient", "", None),
        ("revenue_services", D("0.00"), D("150.00"), "outpatient", "", None),
    ]
    # the team is paid out of the package: the surgeon on her account, the outside anaesthetist on the outside one
    assert _lines(invoice, "shares") == [
        ("doctor_fees_expense", D("2800.00"), D("0.00"), "theatre", "", None),
        ("doctor_payables", D("0.00"), D("2000.00"), "", "doctor", world["doctor"].pk),
        ("external_fees_payable", D("0.00"), D("800.00"), "", "", None),
    ]
    assert _lines(invoice, "deposit") == [
        ("patient_deposits", D("3000.00"), D("0.00"), "", "patient", patient.pk),
        ("patients_receivable", D("0.00"), D("3000.00"), "", "patient", patient.pk),
    ]
    assert invoice.deposit_applied == D("3000")
    assert (invoicing.deposit_balance(patient), invoicing.patient_balance(patient)) == (D(0), D(3250))
    assert invoicing.open_items(patient) == []

    # paying it: not more than she owes; then her account is clear
    with pytest.raises(billing.PaymentError, match="أكبر من المستحق"):
        billing.record_account_payment(patient, amount=D("3251"), by=user)
    billing.record_account_payment(patient, amount=D("3000"), by=user, method="card")
    states, left = invoicing.account_state(patient)
    assert (states[0].paid, states[0].remaining, left) == (D(6000), D(250), D(0))
    billing.record_account_payment(patient, amount=D("250"), by=user)
    assert invoicing.patient_balance(patient) == 0
    with pytest.raises(billing.PaymentError, match="أكبر من اللي ليها"):
        billing.record_account_payment(patient, amount=D("1"), by=user, is_refund=True)
    with pytest.raises(billing.PaymentError, match="أكبر من المقدم"):
        billing.record_account_payment(patient, amount=D("1"), by=user, kind=PaymentKind.DEPOSIT, is_refund=True)
    assert reports.trial_balance(org, date(2026, 1, 1), date(2026, 12, 31)).balanced


def test_nothing_is_invoiced_twice_and_a_deposit_can_go_back(world):
    user = world["reception"]
    patient = _patient(world)
    charge = invoicing.add_charge(patient, description="حقنة", unit_price="200", by=user)
    billing.record_account_payment(patient, amount=D("500"), by=user, kind=PaymentKind.DEPOSIT)
    invoice = invoicing.issue_account_invoice(patient, keys=[f"charge:{charge.pk}"], by=user)
    assert (invoice.deposit_applied, invoicing.deposit_balance(patient), invoicing.patient_balance(patient)) == (
        D(200), D(300), D(0),
    )  # fmt: skip
    with pytest.raises(InvoiceError, match="اختار البنود"):
        invoicing.issue_account_invoice(patient, keys=[f"charge:{charge.pk}"], by=user)
    with pytest.raises(InvoiceError, match="اتفوتر"):
        invoicing.cancel_charge(charge, by=user)
    line = invoice.lines.get()
    with pytest.raises(IntegrityError), transaction.atomic():  # the database refuses a second live line for it too
        InvoiceLine.objects.create(
            organization=invoice.organization, invoice=invoice, kind="charge", description="x", unit_price=D(200),
            gross=D(200), net=D(200), patient_amount=D(200), revenue_account=line.revenue_account,
            cost_center=line.cost_center, charge=charge,
        )  # fmt: skip
    with pytest.raises(IntegrityError), transaction.atomic():  # and an invoice whose amounts do not add up
        Invoice.objects.filter(pk=invoice.pk).update(total=D("199"))
    refund = billing.record_account_payment(patient, amount=D("300"), by=user, kind=PaymentKind.DEPOSIT, is_refund=True)
    assert _lines(refund) == [
        ("cash_reception", D("0.00"), D("300.00"), "", "", None),
        ("patient_deposits", D("300.00"), D("0.00"), "", "patient", patient.pk),
    ]
    assert invoicing.deposit_balance(patient) == 0


def test_a_credit_note_takes_an_account_invoice_back_and_reopens_its_items(world):
    org, user, owner = world["org"], world["reception"], world["owner"]
    patient = _patient(world)
    op = _finish(_operation(world, patient), world["doctor_user"])
    charge = invoicing.add_charge(patient, description="مستلزمات", unit_price="150", by=user)
    invoice = invoicing.issue_account_invoice(patient, keys=[f"operation:{op.pk}", f"charge:{charge.pk}"], by=user)
    assert invoicing.doctor_balance(world["doctor"]) == D("2000")
    with pytest.raises(InvoiceError, match="سبب"):
        invoicing.credit_invoice(invoice, reason="", by=owner)
    op_line = invoice.lines.get(kind="operation")
    note = invoicing.credit_invoice(invoice, line_ids=[op_line.pk], reason="السعر غلط", by=owner)
    assert (note.kind, note.credit_of, note.total) == ("credit", invoice, D(5000))
    assert _lines(note) == [
        ("patients_receivable", D("0.00"), D("5000.00"), "", "patient", patient.pk),
        ("revenue_operations", D("5000.00"), D("0.00"), "theatre", "", None),
    ]
    assert invoicing.doctor_balance(world["doctor"]) == 0 and _bal(org, "external_fees_payable") == 0
    assert [i.key for i in invoicing.open_items(patient)] == [f"operation:{op.pk}"]  # back on the account
    assert invoicing.patient_balance(patient) == D("150")
    states, _left = invoicing.account_state(patient)
    assert (states[0].due, states[0].remaining) == (D(150), D(150))
    with pytest.raises(InvoiceError, match="إشعار دائن، مش فاتورة"):
        invoicing.credit_invoice(note, reason="x", by=owner)
    # re-invoiced right
    op.price = D("4500")
    op.save(update_fields=["price"])
    again = invoicing.issue_account_invoice(patient, keys=[f"operation:{op.pk}"], by=user)
    assert again.total == D("4500") and _bal(org, "revenue_operations") == D("4500")

    # a visit's invoice is corrected through the visit, never by hand
    appt = _appt(world)
    billing.record_payment(appt, amount=D("400"), by=user)
    appt = _go(appt, S.ARRIVED, by=user)
    with pytest.raises(InvoiceError, match="من الزيارة نفسها"):
        invoicing.credit_invoice(_invoices(appt)[0], reason="x", by=owner)


# ================================================================ paying doctors, control accounts


def test_paying_a_doctor_is_a_voucher_on_their_own_account(world):
    org, user, owner, doctor = world["org"], world["reception"], world["owner"], world["doctor"]
    doctor.visit_share_percent = D("50")
    doctor.save()
    appt = _appt(world)
    billing.record_payment(appt, amount=D("400"), by=user)
    _go(appt, S.ARRIVED, by=user)
    assert invoicing.doctor_balance(doctor) == D("200")
    drawer = treasury.treasury(org, "reception_drawer")
    with pytest.raises(InvoiceError, match="أكبر من مستحقات"):
        invoicing.pay_doctor(doctor, amount=D("200.01"), treasury_obj=drawer, by=owner)
    voucher = invoicing.pay_doctor(doctor, amount=D("150"), treasury_obj=drawer, by=owner)
    assert (voucher.kind, voucher.party_type, voucher.party_id) == (VoucherKind.PAYMENT, "doctor", doctor.pk)
    assert _lines(voucher) == [
        ("cash_reception", D("0.00"), D("150.00"), "", "", None),
        ("doctor_payables", D("150.00"), D("0.00"), "", "doctor", doctor.pk),
    ]
    assert invoicing.doctor_balance(doctor) == D("50") and treasury.balance(drawer) == D("250")
    main = treasury.treasury(org, "main_safe")
    with pytest.raises(InvoiceError):  # the safe is empty: cash can never go below zero
        invoicing.pay_doctor(doctor, amount=D("50"), treasury_obj=main, by=owner)
    assert invoicing.party_balances(org, "doctor_payables", "doctor") == {doctor.pk: D("50.00")}


def test_control_accounts_move_only_with_a_party(world):
    from apps.treasury.forms import VoucherForm

    org, owner = world["org"], world["owner"]
    safe = treasury.treasury(org, "bank_main")
    for key in ("patients_receivable", "doctor_payables", "patient_deposits", "payers_receivable"):
        with pytest.raises(treasury.TreasuryError, match="مش بسند حر"):
            treasury.create_voucher(
                org, kind=VoucherKind.RECEIPT, treasury=safe, amount=D("10"), date=TODAY, description="x", by=owner,
                counter_account=chart.account(org, key),
            )  # fmt: skip
    assert not Voucher.objects.filter(organization=org).exists()
    offered = set(VoucherForm(org=org).fields["counter_account"].queryset.values_list("system_key", flat=True))
    assert not offered & chart.CONTROL_KEYS and "rent" in offered


# ================================================================ numbering, old data, the safety net


def test_invoice_numbers_have_no_gaps_and_a_refused_invoice_burns_none(world):
    org, user = world["org"], world["reception"]
    for _i in range(3):
        appt = _appt(world)
        billing.record_payment(appt, amount=D("400"), by=user)
        _go(appt, S.ARRIVED, by=user)
    patient = _patient(world)
    charge = invoicing.add_charge(patient, description="حقنة", unit_price="200", by=user)
    period = FiscalPeriod.objects.get(organization=org, start_date=date(2026, 3, 1))
    FiscalPeriod.objects.filter(pk=period.pk).update(status="closed")
    with pytest.raises(InvoiceError, match="مقفول"):
        invoicing.issue_account_invoice(patient, keys=[f"charge:{charge.pk}"], by=user)
    FiscalPeriod.objects.filter(pk=period.pk).update(status="open")
    invoice = invoicing.issue_account_invoice(patient, keys=[f"charge:{charge.pk}"], by=user)
    numbers = list(
        Invoice.objects.filter(organization=org, kind="invoice").order_by("number").values_list("number", flat=True)
    )
    assert numbers == [1, 2, 3, 4] and invoice.label == "INV-2026-00004"


def test_visits_from_before_invoicing_get_their_invoice_once(world):
    org, user = world["org"], world["reception"]
    appt = _appt(world)
    billing.record_payment(appt, amount=D("400"), by=user)
    Appointment.objects.filter(pk=appt.pk).update(status=S.COMPLETED)  # as if it happened before this phase
    assert _invoices(appt) == []
    assert invoice_visits(org) == 1
    (invoice,) = _invoices(appt)
    assert invoice.date == appt.date and invoicing.patient_balance(appt.patient) == 0
    assert invoice_visits(org) == 0


def test_a_failed_invoice_never_stops_the_visit_and_is_made_later(world, settings, monkeypatch):
    user = world["reception"]
    settings.INVOICING_STRICT = False
    appt = _appt(world)
    real = invoicing.sync_appointment

    def broken(*args, **kwargs):
        raise InvoiceError("الحسابات واقفة")

    monkeypatch.setattr(invoicing, "sync_appointment", broken)
    billing.record_payment(appt, amount=D("400"), by=user)
    appt = _go(appt, S.ARRIVED, S.IN_CONSULTATION, S.COMPLETED, by=user)
    assert appt.status == S.COMPLETED and _invoices(appt) == []
    monkeypatch.setattr(invoicing, "sync_appointment", real)
    assert invoicing.sync_recent() == 1 and len(_invoices(appt)) == 1
    assert invoicing.sync_recent() == 0


# ================================================================ screens and who may do what


def test_invoice_screens_and_permissions(client, world):
    org, user = world["org"], world["reception"]
    doctor = world["doctor"]
    doctor.visit_share_percent = D("25")
    doctor.save()
    patient = _patient(world)
    appt = _appt(world, patient=patient)
    billing.record_payment(appt, amount=D("400"), by=user)
    _go(appt, S.ARRIVED, by=user)
    visit_invoice = _invoices(appt)[0]
    charge = invoicing.add_charge(patient, description="حقنة", unit_price="1234.56", by=user)
    payer = invoicing.save_payer(org, name="شركة النيل", by=user)
    urls = {
        "list": reverse("invoicing:list"),
        "accounts": reverse("invoicing:accounts"),
        "account": reverse("invoicing:account", args=[patient.pk]),
        "invoice": reverse("invoicing:invoice", args=[visit_invoice.pk]),
        "doctors": reverse("invoicing:doctors"),
        "doctor": reverse("invoicing:doctor", args=[doctor.pk]),
        "payers": reverse("invoicing:payers"),
        "payer": reverse("invoicing:payer", args=[payer.pk]),
        "payer_new": reverse("invoicing:payer_new"),
        "settings": reverse("invoicing:settings"),
    }
    client.force_login(world["viewer"])
    assert {client.get(u).status_code for u in urls.values()} == {403}

    client.force_login(user)  # reception: sees and issues, but no credit notes, doctors, payers or settings
    for name in ("list", "accounts", "account", "invoice"):
        assert client.get(urls[name]).status_code == 200, name
    for name in ("doctors", "doctor", "payers", "payer", "payer_new", "settings"):
        assert client.get(urls[name]).status_code == 403, name
    page = client.get(urls["account"]).content.decode()
    assert "حقنة" in page and "1,234.56" in page and "إصدار الفاتورة" in page
    resp = client.post(reverse("invoicing:account_issue", args=[patient.pk]), {"item": [f"charge:{charge.pk}"]})
    invoice = Invoice.objects.filter(patient=patient, appointment__isnull=True).get()
    assert resp.status_code == 302 and resp["Location"] == reverse("invoicing:invoice", args=[invoice.pk])
    assert client.post(reverse("invoicing:invoice_credit", args=[invoice.pk]), {"reason": "x"}).status_code == 403
    resp = client.post(
        reverse("invoicing:account_pay", args=[patient.pk]), {"move": "pay", "amount": "1234.56", "method": "cash"}
    )
    assert resp.status_code == 302 and invoicing.patient_balance(patient) == 0
    payment = Payment.objects.filter(patient=patient, appointment__isnull=True).get()
    for url in (
        reverse("invoicing:invoice_pdf", args=[invoice.pk]),
        reverse("invoicing:receipt_pdf", args=[payment.pk]),
    ):
        pdf = client.get(url)
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-"), url
    resp = client.post(
        reverse("invoicing:coverage", args=[patient.pk]), {"payer": payer.pk, "percent": "70", "member_no": "A1"}
    )
    assert resp.status_code == 302 and PatientCoverage.objects.get(patient=patient).percent == D("70")
    xlsx = client.get(urls["list"], {"format": "xlsx", "from": "2026-03-01", "to": "2026-03-31"})
    values = [c for row in load_workbook(io.BytesIO(xlsx.content)).active.iter_rows(values_only=True) for c in row]
    assert 1234.56 in values and invoice.label in values

    client.force_login(world["owner"])
    for name, url in urls.items():
        assert client.get(url).status_code == 200, name
    assert "100.00" in client.get(urls["doctor"]).content.decode()  # 25% of the 400 visit
    drawer = treasury.treasury(org, "reception_drawer")
    resp = client.post(reverse("invoicing:doctor_pay", args=[doctor.pk]), {"amount": "100", "treasury": drawer.pk})
    assert resp.status_code == 302 and invoicing.doctor_balance(doctor) == 0
    resp = client.post(reverse("invoicing:invoice_credit", args=[invoice.pk]), {"reason": "غلط"})
    note = Invoice.objects.get(credit_of=invoice)
    assert resp["Location"] == reverse("invoicing:invoice", args=[note.pk])
    resp = client.post(
        urls["payer_new"], {"name": "تأمين الدلتا", "kind": "insurance", "default_percent": "90", "is_active": "on"}
    )
    assert resp.status_code == 302
    assert (
        client.post(
            urls["payer_new"], {"name": "تأمين الدلتا", "kind": "insurance", "default_percent": "90"}
        ).status_code
        == 200
    )
    resp = client.post(urls["settings"], {"discount_limit_percent": "15", "tax_registration_number": "123-456-789"})
    assert resp.status_code == 302 and InvoiceSettings.for_org(org).discount_limit_percent == D("15")

    client.force_login(world["other_owner"])  # another clinic sees none of it
    for name in ("account", "invoice", "doctor", "payer"):
        assert client.get(urls[name]).status_code == 404, name
    assert client.post(reverse("invoicing:charge_cancel", args=[charge.pk])).status_code == 404


def test_reception_discount_dialog(client, world):
    appt = _appt(world)
    url = reverse("reception:discount", args=[appt.pk])
    client.force_login(world["reception"])
    assert client.get(url).status_code == 403
    client.force_login(world["doctor_user"])
    assert "خصم على الكشف" in client.get(url).content.decode()
    resp = client.post(url, {"amount": "200", "reason": "قريبة"}, HTTP_HX_REQUEST="true")
    assert resp["HX-Retarget"] == "#modal" and "أكبر من الحد" in resp.content.decode()
    resp = client.post(url, {"amount": "40", "reason": "قريبة"}, HTTP_HX_REQUEST="true")
    appt.refresh_from_db()
    assert resp.status_code == 200 and "HX-Retarget" not in resp and appt.discount == D("40")


# ================================================================ any sequence of events keeps everything in step

event = st.one_of(
    st.tuples(st.just("new"), st.integers(0, 60000)),
    st.tuples(st.just("pay"), st.integers(1, 70000)),
    st.tuples(st.just("forward"), st.integers(0, 9)),
    st.tuples(st.just("back"), st.integers(0, 9)),
    st.tuples(st.just("service"), st.integers(1, 3)),
    st.tuples(st.just("drop_service"), st.integers(0, 9)),
    st.tuples(st.just("discount"), st.integers(0, 60000)),
    st.tuples(st.just("refund"), st.integers(1, 70000)),
    st.tuples(st.just("cover"), st.integers(0, 100)),
    st.tuples(st.just("cancel"), st.integers(0, 9)),
)
FORWARD = {S.BOOKED: S.ARRIVED, S.ARRIVED: S.IN_CONSULTATION, S.IN_CONSULTATION: S.COMPLETED}
BACK = {S.ARRIVED: S.BOOKED, S.IN_CONSULTATION: S.ARRIVED}


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(events=st.lists(event, min_size=1, max_size=16))
def test_any_sequence_of_events_keeps_invoices_and_books_in_step(world, events):
    """Random visits, payments, steps forward and back, services added and removed, discounts, refunds, payer
    coverage and cancellations (amounts in piasters). After every step: every entry balances, the visit's live
    invoice lines are exactly what the visit is worth (a new sync has nothing to do), and each control account
    equals what its documents say: patients, payers, doctors, revenue and discounts."""
    org, user, owner_m, doctor = world["org"], world["owner"], world["owner_m"], world["doctor"]
    sid = transaction.savepoint()
    try:
        doctor.visit_share_percent = D("37.5")
        doctor.save()
        payer = invoicing.save_payer(org, name="جهة", by=user)
        appts = []
        for kind, raw in events:
            amount = D(raw) / 100
            appt = appts[raw % len(appts)] if appts and kind in ("forward", "back", "drop_service", "cancel") else (
                appts[-1] if appts else None
            )  # fmt: skip
            if appt is not None:
                appt.refresh_from_db()
            try:
                if kind == "new" or appt is None:
                    appts.append(_appt(world, price=amount))
                elif kind == "pay":
                    billing.record_payment(appt, amount=amount, by=user, method="card")
                elif kind == "refund":
                    billing.record_payment(appt, amount=amount, by=user, method="card", is_refund=True)
                elif kind == "forward" and appt.status in FORWARD:
                    booking.transition(appt, FORWARD[appt.status], by=user)
                elif kind == "back" and appt.status in BACK:
                    booking.transition(appt, BACK[appt.status], by=user)
                elif kind == "service":
                    center.order_service(appt, world["sonar"], by=user, quantity=raw)
                elif kind == "drop_service":
                    order = appt.service_orders.filter(status="ordered").first()
                    if order is not None:
                        center.cancel_order(order, by=user)
                elif kind == "discount":
                    invoicing.set_visit_discount(appt, amount=amount, reason="سبب", by=user, membership=owner_m)
                elif kind == "cover":
                    invoicing.set_coverage(appt.patient, payer=payer if raw else None, percent=raw, by=user)
                elif kind == "cancel":
                    booking.cancel(appt, by=user, notify=False)
            except (billing.PaymentError, InvoiceError, center.CenterError, booking.BookingError):
                pass  # a refused action must leave nothing behind: the checks below prove it

            posted = JournalEntry.objects.filter(organization=org, status=EntryStatus.POSTED)
            for entry in posted.prefetch_related("lines"):
                lines = list(entry.lines.all())
                assert len(lines) >= 2 and sum(x.debit for x in lines) == sum(x.credit for x in lines) == entry.total
            assert reports.trial_balance(org, date(2026, 1, 1), date(2026, 12, 31)).balanced
            for a in appts:
                assert invoicing.sync_appointment(a) == [], "a visit's invoices were not what the visit is worth"

            def total(kind_, field, model=Invoice):
                rows = model.objects.filter(organization=org, kind=kind_)
                return sum((getattr(r, field) for r in rows), D(0))

            net = {
                f: total("invoice", f) - total("credit", f)
                for f in ("gross", "discount", "payer_share", "patient_share")
            }
            paid = sum((p.signed_amount for p in Payment.objects.filter(organization=org)), D(0))
            assert _bal(org, "patients_receivable") == net["patient_share"] - paid
            assert _bal(org, "payers_receivable") == net["payer_share"]
            assert _bal(org, "revenue_visits") + _bal(org, "revenue_services") == net["gross"]
            assert -_bal(org, "discounts_allowed") == net["discount"]
            shares = sum(
                (s.amount if s.line.invoice.kind == "invoice" else -s.amount
                 for s in InvoiceShare.objects.filter(organization=org).select_related("line__invoice")),
                D(0),
            )  # fmt: skip
            assert _bal(org, "doctor_payables") == shares == _bal(org, "doctor_fees_expense")
            # what is live is exactly what was invoiced and not taken back
            live = InvoiceLine.objects.filter(organization=org, is_live=True, reverses__isnull=True)
            assert sum((x.net for x in live), D(0)) == net["gross"] - net["discount"]
            for k in ("invoice", "credit"):
                numbers = list(
                    Invoice.objects.filter(organization=org, kind=k).order_by("number").values_list("number", flat=True)
                )
                assert numbers == list(range(1, len(numbers) + 1))
    finally:
        transaction.savepoint_rollback(sid)
