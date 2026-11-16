"""Inventory (plan 14 §14.8): weighted-average costing that never strands a piaster, purchases into the store and
returns out of it, expiry lots (first to expire, first out), issues to departments and operations, transfers, blind
counts approved by a second person, and the proof that for any sequence of moves the value on the shelves equals the
inventory account."""

import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from openpyxl import load_workbook

from apps.core.timeutils import CAIRO
from apps.doctors.models import Doctor
from apps.inventory import services as stock
from apps.inventory.models import (
    CountKind,
    CountStatus,
    MoveKind,
    StockBalance,
    StockBatch,
    StockCount,
    StockItem,
    StockMove,
    Warehouse,
)
from apps.inventory.services import StockError
from apps.invoicing import services as invoicing
from apps.invoicing.models import Charge
from apps.ledger import chart, reports
from apps.ledger import services as ledger
from apps.ledger.models import EntryStatus, JournalEntry
from apps.organizations.models import Membership
from apps.patients import services as patient_services
from apps.patients.models import Gender, Patient
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseInvoice, TaxCode
from apps.purchasing.services import LineIn, PurchaseError
from apps.theatre import services as theatre
from apps.theatre.models import OperationStatus, ProcedureType, Room, RoomKind
from apps.theatre.services import TheatreError
from apps.treasury import services as treasury

D = Decimal
NOW = "2026-03-10 12:00:00+02:00"
TODAY = date(2026, 3, 10)
YEAR = (date(2026, 1, 1), date(2026, 12, 31))

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
    owner = make_member(org_a, "owner")
    item = lambda name, **kw: StockItem.objects.create(organization=org_a, name=name, **kw)  # noqa: E731
    return {
        "org": org_a,
        "owner": owner,
        "owner_m": Membership.objects.get(user=owner),
        "keeper": make_member(org_a, "viewer", email="keeper@example.com"),
        "reception": make_member(org_a, "reception"),
        "doctor_user": make_member(org_a, "doctor"),
        "other_owner": make_member(org_b, "owner"),
        "main": stock.warehouse(org_a, "main"),
        "cabinet": stock.warehouse(org_a, "theatre"),
        "gauze": item("شاش معقم", unit="علبة", reorder_level=D("5")),
        "suture": item("خيط جراحي", unit="قطعة", sale_price=D("120")),
        "drug": item("أوكسيتوسين", unit="أمبول", track_expiry=True),
        "supplier": purchasing.save_supplier(org_a, by=owner, name="شركة المستلزمات"),
        "theatre_cc": chart.cost_center(org_a, "theatre"),
        "taxes": {t.system_key: t for t in TaxCode.objects.filter(organization=org_a)},
    }


def _open(world, item, qty, cost, store="main", expiry=None):
    return stock.opening(world["org"], warehouse=world[store], lines=[(item, qty, cost, expiry)], by=world["owner"])


def _bal(world, key):
    return ledger.balance_of(chart.account(world["org"], key))


def _held(item, store):
    b = StockBalance.objects.get(item=item, warehouse=store)
    return b.quantity, b.value


def _lines(source, event=""):
    entry = ledger.entry_for(source, event)
    assert entry is not None, f"no entry for {source} / {event!r}"
    return sorted(
        (line.account.system_key, line.debit, line.credit, line.cost_center.system_key if line.cost_center_id else "")
        for line in entry.lines.select_related("account", "cost_center")
    )


def _buy(world, item, qty, price, tax=None, expiry=None, store="main"):
    return purchasing.create_invoice(
        world["org"], supplier=world["supplier"], date=TODAY, by=world["owner"],
        lines=[LineIn("", None, qty, price, item=item, warehouse=world[store], tax_code=tax, expiry_date=expiry)],
    )  # fmt: skip


# ================================================================ costing


def test_weighted_average_and_the_last_unit_takes_what_is_left(world):
    org, owner, gauze, main, cc = world["org"], world["owner"], world["gauze"], world["main"], world["theatre_cc"]
    assert [w.system_key for w in Warehouse.objects.filter(organization=org)] == ["main", "theatre"]
    doc = _open(world, gauze, "10", "10")
    assert doc.label == "OPN-2026-00001" and _held(gauze, main) == (D("10.000"), D("100.00"))
    assert _lines(doc) == [("inventory", D("100.00"), D("0.00"), ""), ("opening_balances", D("0.00"), D("100.00"), "")]
    _buy(world, gauze, "5", "16")  # 15 units worth 180: the average is 12
    assert _held(gauze, main) == (D("15.000"), D("180.00"))
    issue = stock.issue(org, warehouse=main, lines=[(gauze, "4")], cost_center=cc, by=owner)
    assert issue.label == "ISS-2026-00001" and issue.moves.get().value == D("48.00")
    assert _lines(issue) == [
        ("inventory", D("0.00"), D("48.00"), ""),
        ("medical_supplies_expense", D("48.00"), D("0.00"), "theatre"),
    ]
    assert _held(gauze, main) == (D("11.000"), D("132.00"))
    stock.issue(org, warehouse=main, lines=[(gauze, "11")], cost_center=cc, by=owner)
    assert _held(gauze, main) == (D("0.000"), D("0.00"))

    # three units worth 10.00: 3.33, then 3.34 (half of 6.67 rounds up), then the last takes the 3.33 that is left
    suture = world["suture"]
    stock.opening(org, warehouse=main, lines=[(suture, "3", "3.333333", None)], by=owner)
    assert _held(suture, main) == (D("3.000"), D("10.00"))
    values = [
        stock.issue(org, warehouse=main, lines=[(suture, "1")], cost_center=cc, by=owner).moves.get().value
        for _i in range(3)
    ]
    assert values == [D("3.33"), D("3.34"), D("3.33")] and sum(values) == D("10.00")
    assert _held(suture, main) == (D("0.000"), D("0.00"))
    assert _bal(world, "inventory") == stock.stock_value(org) == 0
    assert reports.trial_balance(org, *YEAR).balanced


def test_what_a_move_refuses(world):
    org, owner, gauze, main, cc = world["org"], world["owner"], world["gauze"], world["main"], world["theatre_cc"]
    _open(world, gauze, "5", "10")
    cases = [
        ({"lines": [(gauze, "6")]}, "مفيش في"),
        ({"lines": [(gauze, "-1")]}, "أكبر من صفر"),
        ({"lines": [(gauze, "abc")]}, "بالأرقام"),
        ({"lines": []}, "صنف واحد"),
        ({"lines": [(gauze, "1"), (gauze, "1")]}, "متكرر"),
        ({"lines": [(gauze, "1")], "cost_center": None}, "مركز التكلفة"),
    ]
    for kw, message in cases:
        with pytest.raises(StockError, match=message):
            stock.issue(org, **{"warehouse": main, "cost_center": cc, "by": owner, **kw})
    assert not StockMove.objects.filter(kind=MoveKind.ISSUE).exists() and _held(gauze, main) == (D(5), D(50))
    with pytest.raises(StockError, match="مخزنين مختلفين"):
        stock.transfer(org, source=main, target=main, lines=[(gauze, "1")], by=owner)
    with pytest.raises(StockError, match="تاريخ صلاحية"):
        _open(world, world["drug"], "5", "10")
    with pytest.raises(IntegrityError), transaction.atomic():  # the database refuses a negative shelf too
        StockBalance.objects.filter(item=gauze, warehouse=main).update(quantity=D("-1"))
    with pytest.raises(IntegrityError), transaction.atomic():  # and value left on an empty one
        StockBalance.objects.filter(item=gauze, warehouse=main).update(quantity=0)


# ================================================================ purchases and returns


def test_a_purchase_puts_stock_on_the_shelf_at_its_cost(world):
    org, owner, gauze, main, supplier = world["org"], world["owner"], world["gauze"], world["main"], world["supplier"]
    invoice = _buy(world, gauze, "10", "10", tax=world["taxes"]["vat14"])  # VAT is not recoverable: it is cost
    line = invoice.lines.get()
    assert (line.item, line.warehouse, line.account.system_key, line.cost, line.description) == (
        gauze, main, "inventory", D("114.00"), "شاش معقم",
    )  # fmt: skip
    assert sorted((x.account.system_key, x.debit, x.credit) for x in ledger.entry_for(invoice).lines.all()) == [
        ("inventory", D("114.00"), D("0.00")),
        ("suppliers_payable", D("0.00"), D("114.00")),
    ]
    assert _held(gauze, main) == (D("10.000"), D("114.00"))
    move = line.stock_moves.get()
    assert (move.kind, move.quantity, move.value, move.quantity_after) == ("receipt", D(10), D("114.00"), D(10))
    with pytest.raises(PurchaseError, match="مرتجع مشتريات"):
        purchasing.cancel_invoice(invoice, by=owner, reason="غلط")
    with pytest.raises(PurchaseError, match="تاريخ صلاحية"):
        _buy(world, world["drug"], "5", "20")
    with pytest.raises(PurchaseError, match="المخزن"):
        purchasing.create_invoice(
            org, supplier=supplier, date=TODAY, by=owner, lines=[LineIn("", None, "1", "5", item=gauze)]
        )
    assert _bal(world, "inventory") == stock.stock_value(org) == D("114.00")


def test_a_return_leaves_at_todays_average_and_the_difference_is_not_lost(world):
    org, owner, gauze, main, cc = world["org"], world["owner"], world["gauze"], world["main"], world["theatre_cc"]
    first = _buy(world, gauze, "10", "10")  # 100
    stock.issue(org, warehouse=main, lines=[(gauze, "9")], cost_center=cc, by=owner)  # 90 out, 1 left worth 10
    _buy(world, gauze, "10", "1")  # 11 on the shelf worth 20
    line = first.lines.get()
    with pytest.raises(PurchaseError, match="أقصى كمية"):
        purchasing.create_return(first, quantities={line.pk: "11"}, reason="x", by=owner)
    ret = purchasing.create_return(first, quantities={line.pk: "10"}, reason="تالف", by=owner)
    back = ret.lines.get()
    # the supplier gives back 100, but what leaves the shelf is carried at 20 x 10/11 = 18.18
    assert (back.net, back.stock_value) == (D("100.00"), D("18.18"))
    assert sorted(
        (x.account.system_key, x.debit, x.credit, x.cost_center.system_key if x.cost_center_id else "")
        for x in ledger.entry_for(ret).lines.select_related("account", "cost_center")
    ) == [
        ("inventory", D("0.00"), D("18.18"), ""),
        ("stock_over_short", D("0.00"), D("81.82"), "admin"),
        ("suppliers_payable", D("100.00"), D("0.00"), ""),
    ]
    assert _held(gauze, main) == (D("1.000"), D("1.82"))
    assert _bal(world, "inventory") == stock.stock_value(org) == D("1.82")
    # nothing on the shelf to send back
    stock.issue(org, warehouse=main, lines=[(gauze, "1")], cost_center=cc, by=owner)
    second = PurchaseInvoice.objects.exclude(pk=first.pk).get()
    with pytest.raises(PurchaseError, match="مفيش في"):
        purchasing.create_return(second, quantities={second.lines.get().pk: "1"}, reason="x", by=owner)
    assert reports.trial_balance(org, *YEAR).balanced


# ================================================================ expiry lots, transfers, returns to the store


def test_first_to_expire_is_first_out_and_expiry_alerts(world):
    org, owner, drug, main, cabinet, cc = (world[k] for k in ("org", "owner", "drug", "main", "cabinet", "theatre_cc"))
    soon, later = TODAY + timedelta(days=20), TODAY + timedelta(days=200)
    _buy(world, drug, "5", "20", expiry=later)
    _open(world, drug, "5", "20", expiry=soon)
    lots = lambda store: [(b.expiry_date, b.quantity) for b in StockBatch.objects.filter(item=drug, warehouse=store)]  # noqa: E731
    assert lots(main) == [(soon, D(5)), (later, D(5))]
    assert [(b.item, b.expiry_date) for b in stock.expiring(org)] == [(drug, soon)]  # within 60 days by default
    stock.issue(org, warehouse=main, lines=[(drug, "7")], cost_center=cc, by=owner)
    assert lots(main) == [(soon, D(0)), (later, D(3))] and stock.expiring(org) == []
    # a transfer carries the lot's date with it, and its value, and the books do not move
    before = (_bal(world, "inventory"), JournalEntry.objects.filter(organization=org).count())
    doc = stock.transfer(org, source=main, target=cabinet, lines=[(drug, "2")], by=owner)
    assert doc.label == "TRF-2026-00001" and ledger.entry_for(doc) is None
    assert (_bal(world, "inventory"), JournalEntry.objects.filter(organization=org).count()) == before
    assert _held(drug, main) == (D(1), D("20.00")) and _held(drug, cabinet) == (D(2), D("40.00"))
    assert lots(cabinet) == [(later, D(2))]
    with freeze_time(datetime.combine(later, time(9), tzinfo=CAIRO)):
        assert {b.warehouse for b in stock.expiring(org, today=later)} == {main, cabinet}
    for batch_total, balance in [(sum(q for _d, q in lots(s)), _held(drug, s)[0]) for s in (main, cabinet)]:
        assert batch_total == balance


def test_an_issue_can_go_back_to_the_store_once(world):
    org, owner, gauze, main, cc = world["org"], world["owner"], world["gauze"], world["main"], world["theatre_cc"]
    _open(world, gauze, "10", "7.5")
    doc = stock.issue(org, warehouse=main, lines=[(gauze, "4")], cost_center=cc, by=owner)
    move = doc.moves.get()
    _buy(world, gauze, "10", "20")  # the average moves in between: what comes back still comes at what it left with
    back = stock.return_issue(move, by=owner)
    assert (back.kind, back.quantity, back.value, back.reverses) == ("issue_return", D(4), D("30.00"), move)
    assert _lines(doc, f"return:{move.pk}") == [
        ("inventory", D("30.00"), D("0.00"), ""),
        ("medical_supplies_expense", D("0.00"), D("30.00"), "theatre"),
    ]
    assert _bal(world, "medical_supplies_expense") == 0 and _held(gauze, main) == (D(20), D("275.00"))
    with pytest.raises(StockError, match="رجع المخزن قبل كده"):
        stock.return_issue(move, by=owner)
    with pytest.raises(StockError, match="مش حركة صرف"):
        stock.return_issue(back, by=owner)


# ================================================================ counts


def test_a_blind_count_is_approved_by_someone_else_and_adjusts_stock_and_books(world):
    org, owner, keeper, main = world["org"], world["owner"], world["keeper"], world["main"]
    gauze, suture, drug = world["gauze"], world["suture"], world["drug"]
    keeper_m = Membership.objects.get(user=keeper)
    Membership.objects.filter(pk=keeper_m.pk).update(granted_permissions=["stock.count", "stock.approve_count"])
    keeper_m.refresh_from_db()
    _open(world, gauze, "10", "10")
    _open(world, suture, "4", "25")
    count = stock.start_count(org, warehouse=main, by=keeper)
    assert count.kind == CountKind.FULL and {line.item for line in count.lines.all()} == {gauze, suture, drug}
    assert all(line.expected is None for line in count.lines.all())  # the sheet does not say what the books hold
    with pytest.raises(StockError, match="جرد مفتوح"):
        stock.start_count(org, warehouse=main, by=owner)
    stock.save_count(count, {gauze.pk: "8", str(suture.pk): "5"}, by=keeper)
    with pytest.raises(StockError, match="لسه ما اتعدّش"):
        stock.submit_count(count, by=keeper)
    stock.save_count(count, {drug.pk: "0"}, by=keeper)
    count = stock.submit_count(count, by=keeper)
    by_item = {line.item_id: line for line in count.lines.all()}
    assert (by_item[gauze.pk].expected, by_item[gauze.pk].difference) == (D(10), D(-2))
    assert (by_item[suture.pk].expected, by_item[suture.pk].difference, by_item[drug.pk].difference) == (
        D(4),
        D(1),
        D(0),
    )
    with pytest.raises(StockError, match="اتقفل"):
        stock.save_count(count, {gauze.pk: "9"}, by=keeper)

    # whoever counted cannot approve their own differences; the owner can
    assert not stock.can_approve(count, keeper_m) and stock.can_approve(count, world["owner_m"])
    with pytest.raises(StockError, match="حد تاني"):
        stock.approve_count(count, by=keeper, membership=keeper_m)
    count = stock.approve_count(count, by=owner, membership=world["owner_m"])
    assert count.status == CountStatus.APPROVED and count.doc.label == "ADJ-2026-00001"
    assert _lines(count.doc) == [
        ("inventory", D("0.00"), D("20.00"), ""),  # 2 boxes of gauze short, at 10
        ("inventory", D("25.00"), D("0.00"), ""),  # 1 suture over, at the shelf's average of 25
        ("stock_over_short", D("0.00"), D("25.00"), "admin"),
        ("stock_over_short", D("20.00"), D("0.00"), "admin"),
    ]
    assert _held(gauze, main) == (D(8), D("80.00")) and _held(suture, main) == (D(5), D("125.00"))
    assert _bal(world, "inventory") == stock.stock_value(org) == D("205.00")

    # a count whose stock moved after it was submitted cannot be approved: count again
    second = stock.start_count(org, warehouse=main, by=keeper, kind=CountKind.SPOT, items=[gauze])
    stock.save_count(second, {gauze.pk: "7"}, by=keeper)
    stock.submit_count(second, by=keeper)
    stock.issue(org, warehouse=main, lines=[(gauze, "1")], cost_center=world["theatre_cc"], by=owner)
    with pytest.raises(StockError, match="اتحرك بعد الجرد"):
        stock.approve_count(second, by=owner, membership=world["owner_m"])
    assert stock.cancel_count(second, by=owner).status == CountStatus.CANCELLED
    spot = stock.start_count(org, warehouse=main, by=keeper, kind=CountKind.SPOT, sample=1)
    assert spot.lines.count() == 1 and spot.lines.get().item in (gauze, suture)
    # an item found on an empty shelf comes in at the last price paid for it
    stock.cancel_count(spot, by=keeper)
    stock.issue(org, warehouse=main, lines=[(suture, "5")], cost_center=world["theatre_cc"], by=owner)
    found = stock.start_count(org, warehouse=main, by=keeper, kind=CountKind.SPOT, items=[suture])
    stock.save_count(found, {suture.pk: "2"}, by=keeper)
    stock.submit_count(found, by=keeper)
    stock.approve_count(found, by=owner, membership=world["owner_m"])
    assert _held(suture, main) == (D(2), D("50.00"))


def test_low_stock_alert(world):
    org, gauze = world["org"], world["gauze"]
    assert [i.name for i in stock.low_stock(org)] == ["شاش معقم"]  # nothing on the shelf, reorder level 5
    _open(world, gauze, "6", "10")
    assert stock.low_stock(org) == []
    stock.issue(org, warehouse=world["main"], lines=[(gauze, "1")], cost_center=world["theatre_cc"], by=world["owner"])
    (low,) = stock.low_stock(org)
    assert (low, low.held) == (gauze, D(5))


# ================================================================ operations


def _operation(world, price="3000"):
    org = world["org"]
    theatre.ensure_defaults(org)
    doctor = Doctor.objects.create(organization=org, name_ar="سارة", title_ar="د.")
    patient = patient_services.create_patient(
        org, Patient(full_name="مريضة العملية", phone="+201003300001", gender=Gender.FEMALE)
    )
    room = Room.objects.filter(organization=org, kind=RoomKind.OPERATING).first()
    procedure = ProcedureType.objects.get(organization=org, name_ar="كحت وتنظيف")
    start = datetime.combine(TODAY, time(13, 0), tzinfo=CAIRO)
    return theatre.book_operation(
        org=org, patient=patient, procedure=procedure, room=room, surgeon=doctor, start=start,
        by=world["doctor_user"], price=D(price), notify=False,
    )  # fmt: skip


def test_operation_supplies_come_out_of_the_store_and_go_on_the_patients_account(world):
    org, user, gauze, suture = world["org"], world["doctor_user"], world["gauze"], world["suture"]
    main, cabinet = world["main"], world["cabinet"]
    op = _operation(world)
    _open(world, gauze, "10", "10")  # in the main store only
    _open(world, suture, "6", "40", store="cabinet")
    with pytest.raises(TheatreError, match="مفيش رصيد كفاية"):
        theatre.add_consumable(op, item=suture, quantity="7", by=user)
    used = theatre.add_consumable(op, item=suture, quantity="2", by=user)  # from the cabinet, 120 each to the patient
    assert (used.name, used.unit, used.stock_move.warehouse, used.stock_move.value) == (
        "خيط جراحي",
        "قطعة",
        cabinet,
        D("80.00"),
    )
    assert (used.charge.quantity, used.charge.unit_price, used.charge.operation) == (2, D("120.00"), op)
    assert _held(suture, cabinet) == (D(4), D("160.00"))
    doc = used.stock_move.doc
    assert (doc.operation, doc.cost_center.system_key) == (op, "theatre") and _bal(
        world, "medical_supplies_expense"
    ) == D("80")
    from_main = theatre.add_consumable(op, item=gauze, quantity="1.5", by=user)  # the cabinet has none: the main store
    assert (from_main.stock_move.warehouse, from_main.charge) == (main, None)  # no sale price: inside the package
    free = theatre.add_consumable(op, name="لاصق طبي", quantity="1", by=user)
    assert (free.item, free.stock_move) == (None, None)
    assert [i.spec.description for i in invoicing.open_items(op.patient)] == ["كحت وتنظيف", "خيط جراحي"]

    # taken off again: back on the shelf at what it left with, and off the patient's account
    theatre.remove_consumable(used, by=user)
    used.refresh_from_db()
    assert used.is_removed and _held(suture, cabinet) == (D(6), D("240.00"))
    assert Charge.objects.get(pk=used.charge_id).is_cancelled and _bal(world, "medical_supplies_expense") == D("15")
    theatre.remove_consumable(free, by=user)
    assert list(op.consumables.filter(is_removed=False)) == [from_main]

    # once the patient was invoiced for it, it cannot be taken off
    billed = theatre.add_consumable(op, item=suture, quantity="1", by=user)
    for item in op.checklist.filter(done=False):
        theatre.toggle_checklist(item, by=user)
    for status in (
        OperationStatus.ADMITTED,
        OperationStatus.IN_THEATRE,
        OperationStatus.RECOVERY,
        OperationStatus.DONE,
    ):
        op = theatre.transition(op, status, by=user)
    invoice = invoicing.issue_account_invoice(
        op.patient, keys=[i.key for i in invoicing.open_items(op.patient)], by=world["owner"]
    )
    assert invoice.total == D("3120.00")
    with pytest.raises(TheatreError, match="اتفوتر"):
        theatre.remove_consumable(billed, by=user)
    assert _bal(world, "inventory") == stock.stock_value(org)


def test_a_procedures_usual_supplies_are_issued_in_one_step(world):
    user, gauze, suture = world["doctor_user"], world["gauze"], world["suture"]
    op = _operation(world)
    op.procedure.kit = [{"item": gauze.pk, "quantity": "2"}, {"item": suture.pk, "quantity": "1"}]
    op.procedure.save()
    _open(world, gauze, "10", "10", store="cabinet")
    with pytest.raises(TheatreError, match="مفيش رصيد كفاية"):
        theatre.issue_kit(op, by=user)  # no sutures anywhere: nothing of the kit is issued
    assert not op.consumables.exists() and _held(gauze, world["cabinet"])[0] == D(10)
    _open(world, suture, "3", "40", store="cabinet")
    issued = theatre.issue_kit(op, by=user)
    assert sorted((c.name, c.quantity) for c in issued) == [("خيط جراحي", D("1.00")), ("شاش معقم", D("2.00"))]
    with pytest.raises(TheatreError, match="مفيش مستلزمات معتادة ناقصة"):
        theatre.issue_kit(op, by=user)
    theatre.remove_consumable(issued[0], by=user)  # one was not needed: the next "issue kit" brings only that one
    assert len(theatre.issue_kit(op, by=user)) == 1


# ================================================================ screens and who may do what


def test_inventory_screens_and_permissions(client, world):
    org, owner, gauze, main = world["org"], world["owner"], world["gauze"], world["main"]
    _open(world, gauze, "12", "10.5")
    count = stock.start_count(org, warehouse=world["cabinet"], by=owner, kind=CountKind.SPOT, items=[gauze])
    urls = {
        "items": reverse("inventory:items"), "item": reverse("inventory:item", args=[gauze.pk]),
        "item_new": reverse("inventory:item_new"), "item_edit": reverse("inventory:item_edit", args=[gauze.pk]),
        "docs": reverse("inventory:docs"), "issue": reverse("inventory:issue_new"),
        "transfer": reverse("inventory:transfer_new"), "opening": reverse("inventory:opening_new"),
        "counts": reverse("inventory:counts"), "count": reverse("inventory:count", args=[count.pk]),
        "setup": reverse("inventory:setup"), "store": reverse("inventory:warehouse_edit", args=[main.pk]),
    }  # fmt: skip
    client.force_login(world["reception"])
    assert {client.get(u).status_code for u in urls.values()} == {403}
    client.force_login(world["other_owner"])
    for name in ("item", "item_edit", "count", "store"):
        assert client.get(urls[name]).status_code == 404, name
    client.force_login(owner)
    for name, url in urls.items():
        assert client.get(url).status_code == 200, name
    page = client.get(urls["items"]).content.decode()
    assert "شاش معقم" in page and "126.00" in page and "وصلت حد إعادة الطلب" not in page
    count_page = client.get(urls["count"]).content.decode()
    assert "الموجود فعلا" in count_page and "في الدفاتر" not in count_page  # the sheet is blind while it is open

    resp = client.post(
        urls["item_new"], {"name": "قطن طبي", "unit": "كيس", "reorder_level": "2", "sale_price": "0", "is_active": "on"}
    )
    cotton = StockItem.objects.get(name="قطن طبي")
    assert resp.status_code == 302
    assert (
        client.post(
            urls["item_new"], {"name": "قطن طبي", "unit": "كيس", "reorder_level": "0", "sale_price": "0"}
        ).status_code
        == 200
    )
    resp = client.post(urls["opening"], {"warehouse": main.pk, "item": [cotton.pk, ""], "quantity": ["20", ""],
                                         "unit_cost": ["3.25", ""], "expiry": ["", ""]})  # fmt: skip
    assert resp.status_code == 302 and _held(cotton, main) == (D(20), D("65.00"))
    resp = client.post(urls["issue"], {"warehouse": main.pk, "cost_center": world["theatre_cc"].pk,
                                       "item": [cotton.pk, gauze.pk], "quantity": ["5", "2"]})  # fmt: skip
    assert resp.status_code == 302 and _held(cotton, main)[0] == D(15) and _held(gauze, main)[0] == D(10)
    resp = client.post(urls["issue"], {"warehouse": main.pk, "cost_center": world["theatre_cc"].pk,
                                       "item": [cotton.pk], "quantity": ["500"]})  # fmt: skip
    assert resp.status_code == 200 and "مفيش في" in resp.content.decode()
    resp = client.post(urls["transfer"], {"warehouse": main.pk, "to_warehouse": world["cabinet"].pk,
                                          "item": [gauze.pk], "quantity": ["4"]})  # fmt: skip
    assert resp.status_code == 302 and _held(gauze, world["cabinet"]) == (D(4), D("42.00"))

    # the count from the screen: saved, submitted, approved (the owner counted it and may approve)
    resp = client.post(reverse("inventory:count_save", args=[count.pk]), {f"qty_{gauze.pk}": "3", "submit": "1"})
    count.refresh_from_db()
    assert resp.status_code == 302 and count.status == CountStatus.SUBMITTED
    assert "في الدفاتر" in client.get(urls["count"]).content.decode()
    client.post(reverse("inventory:count_approve", args=[count.pk]))
    count.refresh_from_db()
    assert count.status == CountStatus.APPROVED and _held(gauze, world["cabinet"]) == (D(3), D("31.50"))
    resp = client.post(reverse("inventory:count_start"), {"warehouse": main.pk, "kind": "spot", "sample": "2"})
    assert resp.status_code == 302 and StockCount.objects.filter(warehouse=main, status=CountStatus.OPEN).exists()

    xlsx = client.get(urls["items"], {"format": "xlsx"})
    values = [c for row in load_workbook(io.BytesIO(xlsx.content)).active.iter_rows(values_only=True) for c in row]
    assert "شاش معقم" in values and 94.5 in values  # 6 left in the main store + 3 in the cabinet, at 10.5
    assert _bal(world, "inventory") == stock.stock_value(org)


# ================================================================ any sequence of moves keeps shelves and books equal

step = st.one_of(
    st.tuples(st.just("open"), st.integers(0, 2), st.integers(1, 40), st.integers(1, 99999)),
    st.tuples(st.just("buy"), st.integers(0, 2), st.integers(1, 40), st.integers(1, 99999)),
    st.tuples(st.just("issue"), st.integers(0, 2), st.integers(1, 30), st.integers(0, 1)),
    st.tuples(st.just("unissue"), st.integers(0, 30), st.integers(0, 0), st.integers(0, 0)),
    st.tuples(st.just("transfer"), st.integers(0, 2), st.integers(1, 30), st.integers(0, 1)),
    st.tuples(st.just("return"), st.integers(0, 30), st.integers(1, 10), st.integers(0, 0)),
    st.tuples(st.just("count"), st.integers(0, 2), st.integers(0, 50), st.integers(0, 1)),
)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(steps=st.lists(step, min_size=1, max_size=16))
def test_any_sequence_of_stock_moves_keeps_shelves_and_books_equal(world, steps):
    """Random opening balances, purchases, issues, returns to the store, transfers, returns to the supplier and
    counts (quantities in thirds of a unit, prices in piasters). After every step: every entry balances, no shelf is
    negative, an empty shelf holds no value, the lots of a dated item add up to its balance, each move's "after"
    matches the next one's start, and the value on all shelves is the inventory account's balance."""
    org, owner, owner_m, cc = world["org"], world["owner"], world["owner_m"], world["theatre_cc"]
    items = [world["gauze"], world["suture"], world["drug"]]
    stores = [world["main"], world["cabinet"]]
    sid = transaction.savepoint()
    try:
        bills = []
        for kind, a, b, c in steps:
            try:
                if kind in ("open", "buy"):
                    item, qty, price = items[a], D(b) / 3, D(c) / 100
                    expiry = TODAY + timedelta(days=c % 400) if item.track_expiry else None
                    if kind == "open":
                        stock.opening(org, warehouse=stores[c % 2], lines=[(item, qty, price, expiry)], by=owner)
                    else:
                        bills.append(purchasing.create_invoice(
                            org, supplier=world["supplier"], date=TODAY, by=owner,
                            lines=[LineIn("", None, str(qty.quantize(D("0.001"))), str(price), item=item,
                                          warehouse=stores[c % 2], expiry_date=expiry,
                                          tax_code=world["taxes"]["vat14"] if c % 3 == 0 else None)],
                        ))  # fmt: skip
                elif kind == "issue":
                    stock.issue(org, warehouse=stores[c], lines=[(items[a], D(b) / 3)], cost_center=cc, by=owner)
                elif kind == "unissue":
                    issued = list(StockMove.objects.filter(organization=org, kind=MoveKind.ISSUE).order_by("id"))
                    if issued:
                        stock.return_issue(issued[a % len(issued)], by=owner)
                elif kind == "transfer":
                    stock.transfer(org, source=stores[c], target=stores[1 - c], lines=[(items[a], D(b) / 3)], by=owner)
                elif kind == "return" and bills:
                    bill = bills[a % len(bills)]
                    purchasing.create_return(bill, quantities={bill.lines.get().pk: D(b) / 3}, reason="x", by=owner)
                elif kind == "count":
                    count = stock.start_count(org, warehouse=stores[c], by=owner, kind=CountKind.SPOT, items=[items[a]])
                    stock.save_count(count, {items[a].pk: D(b) / 3}, by=owner)
                    stock.submit_count(count, by=owner)
                    stock.approve_count(count, by=owner, membership=owner_m)
            except (StockError, PurchaseError):
                StockCount.objects.filter(organization=org, status__in=["open", "submitted"]).update(status="cancelled")

            posted = JournalEntry.objects.filter(organization=org, status=EntryStatus.POSTED)
            for entry in posted.prefetch_related("lines"):
                lines = list(entry.lines.all())
                assert len(lines) >= 2 and sum(x.debit for x in lines) == sum(x.credit for x in lines) == entry.total
            assert reports.trial_balance(org, *YEAR).balanced
            assert _bal(world, "inventory") == stock.stock_value(org)
            for balance in StockBalance.objects.filter(organization=org).select_related("item"):
                assert balance.quantity >= 0 and balance.value >= 0
                assert balance.quantity > 0 or balance.value == 0
                if balance.item.track_expiry:
                    lots = StockBatch.objects.filter(item=balance.item, warehouse=balance.warehouse)
                    assert sum((lot.quantity for lot in lots), D(0)) == balance.quantity
                trail = StockMove.objects.filter(item=balance.item, warehouse=balance.warehouse).order_by("id")
                qty = value = D(0)
                for move in trail:
                    sign = 1 if move.is_in else -1
                    qty, value = qty + sign * move.quantity, value + sign * move.value
                    assert (move.quantity_after, move.value_after) == (qty, value)
                assert (qty, value) == (balance.quantity, balance.value)
    finally:
        transaction.savepoint_rollback(sid)
