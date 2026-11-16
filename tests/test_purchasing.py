"""Suppliers, purchasing, taxes and cheques (plan 14 §14.7): the exact entry of every document, the Definition of
Done scenario (a bill with mixed tax lines, a partial payment by post-dated cheque with withholding, the cheque
clearing, then a return: the supplier's statement, the tax accounts and the bank agree to the piaster), and the
proof that any sequence of documents keeps each control account equal to its documents."""

import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.urls import reverse
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from openpyxl import load_workbook

from apps.ledger import chart, reports
from apps.ledger import services as ledger
from apps.ledger.models import EntryStatus, JournalEntry
from apps.ledger.services import credit, debit
from apps.purchasing import reports as purchase_reports
from apps.purchasing import services as purchasing
from apps.purchasing.models import (
    DocStatus,
    PayMethod,
    PurchaseInvoice,
    PurchaseLine,
    PurchaseReturnLine,
    PurchaseSettings,
    SupplierPayment,
    TaxCode,
    TaxKind,
)
from apps.purchasing.services import LineIn, PurchaseError
from apps.treasury import cheques
from apps.treasury import services as treasury
from apps.treasury.models import Cheque, ChequeDirection, ChequeStatus, ExpenseTemplate, VoucherKind

D = Decimal
C = ChequeStatus
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
    owner = make_member(org_a, "owner")
    taxes = {t.system_key: t for t in TaxCode.objects.filter(organization=org_a)}
    taxes["vat14r"] = TaxCode.objects.create(
        organization=org_a, name="ض.ق.م 14% تتخصم", kind=TaxKind.VAT, rate=D("14"), recoverable=True
    )
    supplier = purchasing.save_supplier(
        org_a, by=owner, name="شركة المستلزمات", tax_registration_number="123-456-789", payment_terms_days=30,
        default_withholding=taxes["wht1"],
    )  # fmt: skip
    return {
        "org": org_a,
        "owner": owner,
        "reception": make_member(org_a, "reception"),
        "other_owner": make_member(org_b, "owner"),
        "taxes": taxes,
        "supplier": supplier,
        "bank": treasury.treasury(org_a, "bank_main"),
        "safe": treasury.treasury(org_a, "main_safe"),
        "drawer": treasury.treasury(org_a, "reception_drawer"),
        "n": 0,
    }


def _acc(world, key):
    return chart.account(world["org"], key)


def _cc(world, key):
    return chart.cost_center(world["org"], key)


def _fund(world, where, amount):
    return treasury.create_voucher(
        world["org"], kind=VoucherKind.RECEIPT, treasury=world[where], amount=D(amount), date=TODAY,
        description="تمويل", by=world["owner"], counter_account=_acc(world, "capital"),
    )  # fmt: skip


def _bill(world, **kw):
    """The Definition of Done bill: supplies with VAT that is part of the cost, an exempt line, and a line whose
    VAT is recoverable."""
    t = world["taxes"]
    world["n"] += 1
    lines = [
        LineIn("مستلزمات عمليات", _acc(world, "medical_supplies_expense"), "10", "150", _cc(world, "theatre"),
               discount="100", tax_code=t["vat14"]),
        LineIn("إيجار مخزن", _acc(world, "rent"), "1", "500", _cc(world, "admin"), tax_code=t["vat0"]),
        LineIn("صيانة", _acc(world, "sundry_expenses"), "2", "250", _cc(world, "admin"), tax_code=t["vat14r"]),
    ]  # fmt: skip
    defaults = {"supplier": world["supplier"], "date": TODAY, "lines": lines, "by": world["owner"],
                "supplier_ref": f"A-{world['n']}"}  # fmt: skip
    return purchasing.create_invoice(world["org"], **{**defaults, **kw})


def _lines(source, event=""):
    entry = ledger.entry_for(source, event)
    assert entry is not None, f"no entry for {source} / {event!r}"
    return sorted(
        (
            line.account.system_key, line.debit, line.credit,
            line.cost_center.system_key if line.cost_center_id else "", line.party_type, line.party_id,
        )
        for line in entry.lines.select_related("account", "cost_center")
    )  # fmt: skip


def _bal(world, key):
    return ledger.balance_of(_acc(world, key))


# ================================================================ taxes, line by line


def test_default_tax_codes_and_line_rounding(world):
    org = world["org"]
    codes = {t.system_key: (t.kind, t.rate, t.recoverable) for t in TaxCode.objects.filter(organization=org)}
    assert codes["vat14"] == ("vat", D("14.00"), False) and codes["wht3"] == ("withholding", D("3.00"), False)
    assert {"vat14", "vat0", "wht1", "wht3", "wht5"} <= set(codes)
    purchasing.ensure_defaults(org)  # asking again adds nothing
    assert TaxCode.objects.filter(organization=org).count() == 6
    price = purchasing.price_line
    assert price(D("3"), D("33.3333"), D("0"), D("14"), False) == (D("100.00"), D("14.00"))
    assert price(D("1"), D("114"), D("0"), D("14"), True) == (D("100.00"), D("14.00"))
    assert price(D("1"), D("100"), D("0"), D("14"), True) == (D("87.72"), D("12.28"))  # net + vat = what was typed
    assert price(D("2.5"), D("19.99"), D("4.98"), D("14"), False) == (D("45.00"), D("6.30"))
    assert price(D("1"), D("50"), D("0"), D("0"), True) == (D("50.00"), D("0.00"))
    with pytest.raises(PurchaseError, match="الخصم أكبر"):
        price(D("1"), D("50"), D("60"), D("14"), False)


def test_a_purchase_invoice_with_mixed_tax_lines_posts_exactly(world):
    supplier = world["supplier"]
    invoice = _bill(world)
    assert (invoice.label, invoice.subtotal, invoice.vat_total, invoice.total) == (
        "PINV-2026-00001", D("2400.00"), D("266.00"), D("2666.00"),
    )  # fmt: skip
    assert invoice.due_date == TODAY + timedelta(days=30)
    by_name = {line.description: line for line in invoice.lines.all()}
    supplies = by_name["مستلزمات عمليات"]
    assert (supplies.net, supplies.vat_amount, supplies.vat_recoverable, supplies.cost) == (
        D("1400.00"), D("196.00"), False, D("1596.00"),
    )  # fmt: skip
    assert (by_name["صيانة"].vat_amount, by_name["صيانة"].vat_recoverable, by_name["صيانة"].cost) == (
        D("70.00"), True, D("500.00"),
    )  # fmt: skip
    assert _lines(invoice) == [
        ("medical_supplies_expense", D("1596.00"), D("0.00"), "theatre", "", None),
        ("rent", D("500.00"), D("0.00"), "admin", "", None),
        ("sundry_expenses", D("500.00"), D("0.00"), "admin", "", None),
        ("suppliers_payable", D("0.00"), D("2666.00"), "", "supplier", supplier.pk),
        ("vat_input", D("70.00"), D("0.00"), "", "", None),
    ]
    assert purchasing.supplier_balance(supplier) == purchasing.open_amount(invoice) == D("2666.00")


def test_what_an_invoice_refuses(world):
    org, owner, supplier = world["org"], world["owner"], world["supplier"]
    first = _bill(world, supplier_ref="X-1")
    with pytest.raises(PurchaseError, match="متسجلة قبل كده"):
        _bill(world, supplier_ref="X-1")
    line = lambda **kw: [LineIn(**{"description": "بند", "account": _acc(world, "rent"), "quantity": "1",  # noqa: E731
                                   "unit_price": "100", "cost_center": _cc(world, "admin"), **kw})]  # fmt: skip
    cases = [
        ({"lines": []}, "سطر واحد"),
        ({"lines": line(cost_center=None)}, "مركز التكلفة"),
        ({"lines": line(account=_acc(world, "suppliers_payable"), cost_center=None)}, "مينفعش يتحمّل"),
        ({"lines": line(account=world["bank"].account, cost_center=None)}, "مينفعش يتحمّل"),
        ({"lines": line(quantity="0")}, "الكمية"),
        ({"lines": line(unit_price="abc")}, "بالأرقام"),
        ({"lines": line(unit_price="0")}, "صفر"),
        ({"lines": line(tax_code=world["taxes"]["wht1"])}, "قيمة مضافة"),
        ({"date": None}, "تاريخ"),
    ]
    for kw, message in cases:
        with pytest.raises(PurchaseError, match=message):
            _bill(world, **kw)
    assert PurchaseInvoice.objects.filter(organization=org).count() == 1  # and none of them burned a number
    second = _bill(world)
    assert (first.number, second.number) == (1, 2)
    purchasing.cancel_invoice(first, by=owner, reason="اتسجلت غلط")
    assert _bill(world, supplier_ref="X-1").number == 3  # a cancelled paper invoice can be entered again, right
    assert purchasing.supplier_balance(supplier) == D("5332.00")
    with pytest.raises(PurchaseError, match="سبب"):
        purchasing.cancel_invoice(second, by=owner, reason=" ")


# ================================================================ the Definition of Done scenario


def test_bill_then_cheque_with_withholding_then_clearing_then_return_all_agree(world):
    org, owner, supplier, bank = world["org"], world["owner"], world["supplier"], world["bank"]
    _fund(world, "bank", "10000")
    invoice = _bill(world)

    # a partial payment by a post-dated cheque, 1% withheld on the part of it that is not VAT
    payment = purchasing.pay_supplier(
        org, supplier=supplier, amount="2000", method=PayMethod.CHEQUE, treasury_obj=bank, by=owner,
        allocations=[(invoice, "2000")], wht_code=world["taxes"]["wht1"], cheque_number="000451",
        cheque_due=TODAY + timedelta(days=20),
    )  # fmt: skip
    assert (payment.label, payment.wht_base, payment.wht_rate, payment.wht_amount, payment.net_paid) == (
        "SPAY-2026-00001", D("1800.45"), D("1.00"), D("18.00"), D("1982.00"),
    )  # fmt: skip
    assert _lines(payment) == [
        ("cheques_payable", D("0.00"), D("1982.00"), "", "", None),
        ("suppliers_payable", D("2000.00"), D("0.00"), "", "supplier", supplier.pk),
        ("wht_payable", D("0.00"), D("18.00"), "", "supplier", supplier.pk),
    ]
    cheque = payment.cheque
    assert (cheque.direction, cheque.status, cheque.amount, cheque.bank) == ("issued", C.ISSUED, D("1982.00"), bank)
    assert treasury.balance(bank) == D("10000.00")  # nothing left the bank yet
    assert _bal(world, "cheques_payable") == cheques.open_total(org, ChequeDirection.ISSUED) == D("1982.00")
    assert purchasing.open_amount(invoice) == D("666.00") and purchasing.supplier_balance(supplier) == D("666.00")

    # the cheque clears
    cheque = cheques.move(cheque, C.CLEARED, by=owner)
    last = cheque.events.order_by("-id").first()
    assert sorted((x.account.system_key, x.debit, x.credit) for x in last.entry.lines.select_related("account")) == [
        ("bank_main", D("0.00"), D("1982.00")),
        ("cheques_payable", D("1982.00"), D("0.00")),
    ]
    assert treasury.balance(bank) == D("8018.00") and _bal(world, "cheques_payable") == 0
    with pytest.raises(PurchaseError, match="الشيك بتاع السداد"):
        purchasing.cancel_payment(payment, by=owner, reason="غلط")  # the bank paid it: too late

    # then part of the supplies goes back
    supplies = invoice.lines.get(description="مستلزمات عمليات")
    ret = purchasing.create_return(invoice, quantities={supplies.pk: "4"}, reason="تالف", by=owner)
    assert (ret.label, ret.subtotal, ret.vat_total, ret.total) == (
        "PRET-2026-00001",
        D("560.00"),
        D("78.40"),
        D("638.40"),
    )
    assert _lines(ret) == [
        ("medical_supplies_expense", D("0.00"), D("638.40"), "theatre", "", None),
        ("suppliers_payable", D("638.40"), D("0.00"), "", "supplier", supplier.pk),
    ]

    # everything agrees to the piaster
    assert purchasing.supplier_balance(supplier) == purchasing.open_amount(invoice) == D("27.60")
    statement = reports.account_statement(org, _acc(world, "suppliers_payable"), *YEAR, party=supplier)
    assert (statement.credit, statement.debit, statement.closing) == (D("2666.00"), D("2638.40"), D("27.60"))
    assert _bal(world, "suppliers_payable") == D("27.60")
    assert _bal(world, "wht_payable") == D("18.00") and _bal(world, "vat_input") == D("70.00")
    assert _bal(world, "medical_supplies_expense") == D("957.60")  # 1596 less what went back
    vat = {row.name: row for row in purchase_reports.vat_paid(org, *YEAR)}
    assert (vat["ض.ق.م 14%"].net, vat["ض.ق.م 14%"].in_cost, vat["ض.ق.م 14%"].recoverable) == (D(840), D("117.60"), D(0))
    assert (vat["ض.ق.م 14% تتخصم"].net, vat["ض.ق.م 14% تتخصم"].recoverable) == (D(500), D("70.00"))
    assert [(p.supplier, p.wht_amount) for p in purchase_reports.withheld(org, *YEAR)] == [(supplier, D("18.00"))]
    assert reports.trial_balance(org, *YEAR).balanced

    # the rest of the line goes back in two more steps and comes to exactly the line's own amounts
    purchasing.create_return(invoice, quantities={supplies.pk: "1"}, reason="تالف", by=owner)
    purchasing.create_return(invoice, quantities={str(supplies.pk): "5"}, reason="تالف", by=owner)
    back = PurchaseReturnLine.objects.filter(line=supplies)
    assert sum(r.net for r in back) == supplies.net and sum(r.vat_amount for r in back) == supplies.vat_amount
    with pytest.raises(PurchaseError, match="أقصى كمية"):
        purchasing.create_return(invoice, quantities={supplies.pk: "1"}, reason="تالف", by=owner)
    assert _bal(world, "medical_supplies_expense") == 0
    # more came back than was still unpaid: the invoice is settled and the supplier owes the difference
    assert purchasing.open_amount(invoice) == 0 and purchasing.supplier_balance(supplier) == D("-930.00")
    with pytest.raises(PurchaseError, match="فيه سداد"):
        purchasing.cancel_invoice(invoice, by=owner, reason="x")


# ================================================================ paying suppliers


def test_cash_and_transfer_payments_and_their_rules(world):
    org, owner, supplier = world["org"], world["owner"], world["supplier"]
    invoice = _bill(world)
    pay = lambda **kw: purchasing.pay_supplier(  # noqa: E731
        org, **{"supplier": supplier, "amount": "500", "method": PayMethod.CASH, "treasury_obj": world["safe"],
                "by": owner, **kw}
    )  # fmt: skip
    with pytest.raises(PurchaseError, match="مفيش في"):
        pay()  # the safe is empty: cash never goes below zero
    _fund(world, "safe", "1000")
    cases = [
        ({"amount": "0"}, "أكبر من صفر"),
        ({"method": PayMethod.TRANSFER}, "حساب بنكي"),
        ({"method": PayMethod.CASH, "treasury_obj": world["bank"]}, "خزينة أو درج"),
        ({"allocations": [(invoice, "600")]}, "أكبر من المبلغ"),
        ({"amount": "3000", "treasury_obj": world["bank"], "method": PayMethod.TRANSFER,
          "allocations": [(invoice, "2700")]}, "أكبر من الباقي"),
        ({"wht_code": world["taxes"]["vat14"]}, "خصم تحت حساب الضريبة"),
        ({"wht_code": world["taxes"]["wht1"], "wht_base": "501"}, "وعاء الخصم"),
        ({"method": PayMethod.CHEQUE, "treasury_obj": world["bank"]}, "رقم الشيك"),
    ]  # fmt: skip
    for kw, message in cases:
        with pytest.raises(PurchaseError, match=message):
            pay(**kw)
    assert not SupplierPayment.objects.filter(organization=org).exists() and not Cheque.objects.exists()

    cash = pay(allocations=[(invoice, "500")])
    assert _lines(cash) == [
        ("cash_main", D("0.00"), D("500.00"), "", "", None),
        ("suppliers_payable", D("500.00"), D("0.00"), "", "supplier", supplier.pk),
    ]
    assert treasury.balance(world["safe"]) == D("500.00") and purchasing.open_amount(invoice) == D("2166.00")
    # a transfer may overdraw the bank; what is not allocated stays on the supplier's account as money paid ahead
    transfer = pay(
        amount="3000", method=PayMethod.TRANSFER, treasury_obj=world["bank"], allocations=[(invoice, "2166")]
    )
    assert treasury.balance(world["bank"]) == D("-3000.00") and purchasing.open_amount(invoice) == 0
    assert purchasing.supplier_balance(supplier) == D("-834.00")
    with pytest.raises(PurchaseError, match="فيه سداد"):
        purchasing.cancel_invoice(invoice, by=owner, reason="x")

    # cancelling a payment reverses it and opens its invoices again
    purchasing.cancel_payment(transfer, by=owner, reason="اتحول لمورد تاني")
    transfer.refresh_from_db()
    assert transfer.is_cancelled and treasury.balance(world["bank"]) == 0
    assert purchasing.open_amount(invoice) == D("2166.00") and purchasing.supplier_balance(supplier) == D("2166.00")
    assert purchasing.cancel_payment(transfer, by=owner, reason="تاني").is_cancelled  # asking twice changes nothing
    assert reports.trial_balance(org, *YEAR).balanced
    assert [p.number for p in SupplierPayment.objects.filter(organization=org).order_by("number")] == [1, 2]


def test_withholding_follows_the_supplier_and_the_threshold(world):
    org, owner, supplier = world["org"], world["owner"], world["supplier"]
    invoice = _bill(world)  # 2400 before VAT, 2666 with it
    assert PurchaseSettings.for_org(org).wht_threshold == D("300")
    assert purchasing.withholding_base([(invoice, D("2666"))]) == D("2400.00")
    assert purchasing.suggest_withholding(org, supplier, [(invoice, D("2666"))], D("2666")) == (
        D("2400.00"),
        D("24.00"),
    )
    assert purchasing.suggest_withholding(org, supplier, [(invoice, D("300"))], D("300")) == (D("270.07"), D("0.00"))
    supplier.default_withholding = None
    assert purchasing.suggest_withholding(org, supplier, [(invoice, D("2666"))], D("2666")) == (D(0), D(0))
    # a payment in full by cheque, 3% of a base the accountant typed
    _fund(world, "bank", "5000")
    payment = purchasing.pay_supplier(
        org, supplier=supplier, amount="2666", method=PayMethod.TRANSFER, treasury_obj=world["bank"], by=owner,
        allocations=[(invoice, "2666")], wht_code=world["taxes"]["wht3"], wht_base="1000",
    )  # fmt: skip
    assert (payment.wht_amount, payment.net_paid) == (D("30.00"), D("2636.00"))
    assert treasury.balance(world["bank"]) == D("2364.00") and _bal(world, "wht_payable") == D("30.00")
    assert purchasing.supplier_balance(supplier) == 0


def test_opening_balance_and_supplier_rules(world):
    org, owner = world["org"], world["owner"]
    old = purchasing.save_supplier(org, by=owner, name="مورد قديم", opening_balance="1500.50")
    assert _lines(old, "opening") == [
        ("opening_balances", D("1500.50"), D("0.00"), "", "", None),
        ("suppliers_payable", D("0.00"), D("1500.50"), "", "supplier", old.pk),
    ]
    assert purchasing.supplier_balance(old) == D("1500.50")
    purchasing.save_supplier(org, by=owner, supplier=old, name="مورد قديم جدا", opening_balance="99")  # only when new
    assert purchasing.supplier_balance(old) == D("1500.50")
    with pytest.raises(PurchaseError, match="نفس الاسم"):
        purchasing.save_supplier(org, by=owner, name="  شركة   المستلزمات ")
    with pytest.raises(PurchaseError, match="خصم تحت حساب"):
        purchasing.save_supplier(org, by=owner, name="تاني", default_withholding=world["taxes"]["vat14"])


# ================================================================ cheques


def test_issued_cheque_life_cancel_and_undo(world):
    org, owner, supplier, bank = world["org"], world["owner"], world["supplier"], world["bank"]
    invoice = _bill(world)
    pay = lambda number: purchasing.pay_supplier(  # noqa: E731
        org, supplier=supplier, amount="1000", method=PayMethod.CHEQUE, treasury_obj=bank, by=owner,
        allocations=[(invoice, "1000")], cheque_number=number, cheque_due=TODAY + timedelta(days=5),
    )  # fmt: skip
    payment = pay("700")
    with pytest.raises(PurchaseError, match="نفس الرقم"):
        pay("700")
    assert SupplierPayment.objects.filter(organization=org).count() == 1
    cheque = payment.cheque
    # cleared by mistake, then undone: the bank is as it was and the cheque is on its way again
    cheque = cheques.move(cheque, C.CLEARED, by=owner)
    assert treasury.balance(bank) == D("-1000.00")
    cheque = cheques.undo(cheque, by=owner)
    assert (cheque.status, treasury.balance(bank), _bal(world, "cheques_payable")) == (C.ISSUED, D(0), D(1000))
    with pytest.raises(treasury.TreasuryError, match="مينفعش يترجع"):
        cheques.undo(cheque, by=owner)
    with pytest.raises(treasury.TreasuryError, match="مينفعش يبقى"):
        cheques.move(cheque, C.COLLECTED, by=owner)
    # the supplier gives it back: the payment is cancelled with it and the invoice is open again
    cheque = cheques.move(cheque, C.RETURNED, by=owner, notes="رجّعه المورد")
    payment.refresh_from_db()
    assert payment.is_cancelled and payment.cancel_reason == "رجّعه المورد"
    assert _bal(world, "cheques_payable") == 0 and purchasing.open_amount(invoice) == D("2666.00")
    assert pay("700").cheque.status == C.ISSUED  # the number is free again

    # a cheque that is not a supplier payment (rent by cheque)
    rent = cheques.issue_cheque(
        org, bank=bank, number="900", due_date=TODAY + timedelta(days=40), amount="5000", by=owner,
        counter_account=_acc(world, "rent"), cost_center=_cc(world, "admin"), party_name="المالك",
    )  # fmt: skip
    assert _bal(world, "rent") == D("5500") and _bal(world, "cheques_payable") == D("6000")  # the bill had 500 of rent
    cheques.move(rent, C.CANCELLED, by=owner)
    assert _bal(world, "rent") == D("500")
    assert _bal(world, "cheques_payable") == cheques.open_total(org, "issued") == D("1000")
    for key in ("suppliers_payable", "cash_main"):
        with pytest.raises(treasury.TreasuryError, match="مينفعش يتحرك بشيك حر"):
            cheques.issue_cheque(org, bank=bank, number="901", due_date=TODAY, amount="1", by=owner,
                                 counter_account=_acc(world, key))  # fmt: skip
    with pytest.raises(treasury.TreasuryError, match="حساب بنكي"):
        cheques.issue_cheque(org, bank=world["safe"], number="902", due_date=TODAY, amount="1", by=owner,
                             counter_account=_acc(world, "rent"), cost_center=_cc(world, "admin"))  # fmt: skip


def test_received_cheque_life(world):
    from apps.invoicing import services as invoicing

    org, owner, supplier, bank = world["org"], world["owner"], world["supplier"], world["bank"]
    payer = invoicing.save_payer(org, name="شركة النيل", by=owner)
    ledger.post(org, date=TODAY, memo="مطالبة", by=owner, lines=[
        debit("payers_receivable", D("3000"), party=payer), credit("revenue_other", D("3000"), cost_center="admin"),
    ])  # fmt: skip
    with pytest.raises(treasury.TreasuryError, match="أكبر من المستحق"):
        cheques.receive_cheque(org, number="1", due_date=TODAY, amount="3001", by=owner, payer=payer)
    cheque = cheques.receive_cheque(
        org, number="55", due_date=TODAY + timedelta(days=3), amount="2000", by=owner, payer=payer, bank_name="CIB"
    )
    assert (cheque.status, cheque.party_type, cheque.party_id, cheque.party_name) == (
        C.IN_HAND,
        "payer",
        payer.pk,
        "شركة النيل",
    )
    assert invoicing.payer_balance(payer) == D("1000") and _bal(world, "cheques_receivable") == D("2000")
    with pytest.raises(treasury.TreasuryError, match="الحساب البنكي"):
        cheques.move(cheque, C.DEPOSITED, by=owner)
    cheque = cheques.move(cheque, C.DEPOSITED, by=owner, bank=bank)  # moves no money
    assert (treasury.balance(bank), _bal(world, "cheques_receivable")) == (D(0), D(2000))
    cheque = cheques.move(cheque, C.COLLECTED, by=owner)
    assert (treasury.balance(bank), _bal(world, "cheques_receivable")) == (D(2000), D(0))
    cheque = cheques.undo(cheque, by=owner)  # the bank had not collected it after all
    assert (cheque.status, treasury.balance(bank), _bal(world, "cheques_receivable")) == (C.DEPOSITED, D(0), D(2000))
    cheque = cheques.move(cheque, C.BOUNCED, by=owner)
    assert invoicing.payer_balance(payer) == D("3000") and _bal(world, "cheques_receivable") == 0  # they owe it again

    # a second cheque is endorsed to a supplier instead of going to the bank
    invoice = _bill(world)
    other = cheques.receive_cheque(org, number="56", due_date=TODAY, amount="1000", by=owner, payer=payer)
    other = cheques.move(other, C.ENDORSED, by=owner, supplier=supplier)
    assert purchasing.supplier_balance(supplier) == invoice.total - D("1000") and other.endorsed_to_id == supplier.pk
    other = cheques.undo(other, by=owner)
    assert (other.status, other.endorsed_to_id, purchasing.supplier_balance(supplier)) == (
        C.IN_HAND,
        None,
        invoice.total,
    )
    assert _bal(world, "cheques_receivable") == cheques.open_total(org, "received") == D("1000")
    # not from a payer: it credits the account given
    capital = cheques.receive_cheque(org, number="57", due_date=TODAY, amount="700", by=owner,
                                     counter_account=_acc(world, "capital"), party_name="الشريك")  # fmt: skip
    assert _bal(world, "capital") == D("700") and capital.party_type == ""
    assert reports.trial_balance(org, *YEAR).balanced


def test_cheques_due_soon_and_a_bank_that_cannot_pay(world):
    org, owner, bank = world["org"], world["owner"], world["bank"]
    kw = {"bank": bank, "by": owner, "counter_account": _acc(world, "rent"), "cost_center": _cc(world, "admin")}
    cheques.issue_cheque(org, number="1", due_date=TODAY + timedelta(days=3), amount="4000", **kw)
    cheques.issue_cheque(org, number="2", due_date=TODAY - timedelta(days=1), amount="1000", **kw)
    cheques.issue_cheque(org, number="3", due_date=TODAY + timedelta(days=30), amount="9000", **kw)
    soon = cheques.due_soon(org, 7)
    assert [c.number for c in soon["issued"]] == ["2", "1"]  # the late one first, the far one not at all
    assert [(row["bank"], row["due"], row["balance"]) for row in soon["short"]] == [(bank, D("5000.00"), D("0.00"))]
    _fund(world, "bank", "5000")
    assert cheques.due_soon(org, 7)["short"] == []


# ================================================================ recurring expenses, aging


def test_recurring_expense_is_recorded_once_a_month(world):
    org, owner = world["org"], world["owner"]
    _fund(world, "safe", "20000")
    rent = ExpenseTemplate.objects.create(
        organization=org, name="الإيجار", treasury=world["safe"], counter_account=_acc(world, "rent"),
        cost_center=_cc(world, "admin"), amount=D("5000"), day_of_month=5, party_name="المالك",
    )  # fmt: skip
    (row,) = treasury.expense_templates(org, TODAY)
    assert row.paid_voucher is None and row.overdue  # the 10th: its day (the 5th) has passed
    voucher = treasury.record_expense(rent, by=owner, amount="5200")
    assert (voucher.kind, voucher.amount, voucher.template, voucher.description) == (
        "payment",
        D("5200.00"),
        rent,
        "الإيجار 03/2026",
    )
    assert _bal(world, "rent") == D("5200") and treasury.balance(world["safe"]) == D("14800")
    with pytest.raises(treasury.TreasuryError, match="اتسجل الشهر ده"):
        treasury.record_expense(rent, by=owner)
    (row,) = treasury.expense_templates(org, TODAY)
    assert row.paid_voucher == voucher and not row.overdue
    treasury.cancel_voucher(voucher, by=owner)  # a cancelled voucher does not count as this month's
    assert treasury.expense_templates(org, TODAY)[0].paid_voucher is None
    with freeze_time("2026-04-02 10:00:00+02:00"):
        (row,) = treasury.expense_templates(org, date(2026, 4, 2))
        assert row.paid_voucher is None and not row.overdue
        assert treasury.record_expense(rent, by=owner).amount == D("5000.00")


def test_supplier_aging_by_due_date(world):
    org, owner = world["org"], world["owner"]
    line = [LineIn("بند", _acc(world, "rent"), "1", "1000", _cc(world, "admin"))]
    make = lambda days_ago, ref: purchasing.create_invoice(  # noqa: E731
        org, supplier=world["supplier"], date=TODAY - timedelta(days=days_ago),
        due_date=TODAY - timedelta(days=days_ago),
        lines=line, by=owner, supplier_ref=ref,
    )  # fmt: skip
    current, late20, late100 = make(0, "a"), make(20, "b"), make(100, "c")
    _fund(world, "bank", "5000")
    purchasing.pay_supplier(org, supplier=world["supplier"], amount="400", method=PayMethod.TRANSFER,
                            treasury_obj=world["bank"], by=owner, allocations=[(late20, "400")])  # fmt: skip
    (row,) = purchase_reports.aging(org, TODAY)
    assert row.buckets == [D("1000.00"), D("600.00"), D(0), D(0), D("1000.00")] and row.total == D("2600.00")
    assert [i.invoice for i in purchase_reports.open_invoices(org, TODAY)] == [late100, late20, current]
    assert purchase_reports.quarter_of(date(2026, 5, 17)) == (date(2026, 4, 1), date(2026, 6, 30))
    assert purchase_reports.quarter_of(date(2026, 12, 31)) == (date(2026, 10, 1), date(2026, 12, 31))


# ================================================================ screens and who may do what


def test_purchasing_screens_and_permissions(client, world):
    org, owner, supplier = world["org"], world["owner"], world["supplier"]
    _fund(world, "bank", "10000")
    invoice = _bill(world)
    payment = purchasing.pay_supplier(
        org, supplier=supplier, amount="1000", method=PayMethod.CHEQUE, treasury_obj=world["bank"], by=owner,
        allocations=[(invoice, "1000")], wht_code=world["taxes"]["wht1"], cheque_number="12",
        cheque_due=TODAY + timedelta(days=2),
    )  # fmt: skip
    tax = world["taxes"]["vat14"]
    urls = {
        "invoices": reverse("purchasing:invoices"), "invoice_new": reverse("purchasing:invoice_new"),
        "invoice": reverse("purchasing:invoice", args=[invoice.pk]), "suppliers": reverse("purchasing:suppliers"),
        "supplier_new": reverse("purchasing:supplier_new"), "aging": reverse("purchasing:aging"),
        "supplier": reverse("purchasing:supplier", args=[supplier.pk]), "payments": reverse("purchasing:payments"),
        "payment_new": reverse("purchasing:payment_new") + f"?supplier={supplier.pk}",
        "payment": reverse("purchasing:payment", args=[payment.pk]), "taxes": reverse("purchasing:taxes"),
        "tax_edit": reverse("purchasing:tax_edit", args=[tax.pk]), "settings": reverse("purchasing:settings"),
        "cheques": reverse("treasury:cheques"), "cheque": reverse("treasury:cheque", args=[payment.cheque.pk]),
        "cheque_new": reverse("treasury:cheque_new"), "expenses": reverse("treasury:expenses"),
        "expense_new": reverse("treasury:expense_new"), "treasury": reverse("treasury:home"),
    }  # fmt: skip
    client.force_login(world["reception"])
    assert {client.get(u).status_code for u in urls.values()} == {403}
    client.force_login(world["other_owner"])
    for name in ("invoice", "supplier", "payment", "tax_edit", "cheque"):
        assert client.get(urls[name]).status_code == 404, name

    client.force_login(owner)
    for name, url in urls.items():
        assert client.get(url).status_code == 200, name
    assert "شيك صادر يستحق" in client.get(urls["treasury"]).content.decode()  # the due-soon notice

    # a purchase invoice typed on the form, with a scan
    scan = SimpleUploadedFile("invoice.pdf", b"%PDF-1.4 a scanned invoice", content_type="application/pdf")
    resp = client.post(urls["invoice_new"], {
        "supplier": supplier.pk, "supplier_ref": "F-9", "date": "2026-03-10", "scan": scan,
        "description": ["قفازات", "", "مطهر"],
        "account": [_acc(world, "medical_supplies_expense").pk, "", _acc(world, "medical_supplies_expense").pk],
        "cost_center": [_cc(world, "outpatient").pk, "", _cc(world, "outpatient").pk], "quantity": ["4", "1", "2"],
        "unit_price": ["25", "", "617.28"], "discount": ["", "", ""], "tax_code": [tax.pk, "", ""],
    })  # fmt: skip
    typed = PurchaseInvoice.objects.get(supplier_ref="F-9")
    assert resp.status_code == 302 and (typed.subtotal, typed.vat_total, typed.total) == (
        D("1334.56"),
        D("14.00"),
        D("1348.56"),
    )
    assert client.get(reverse("purchasing:invoice_scan", args=[typed.pk])).status_code == 200
    resp = client.post(urls["invoice_new"], {"supplier": supplier.pk, "date": "2026-03-10", "description": ["x"],
                                             "account": [""], "cost_center": [""], "quantity": ["1"],
                                             "unit_price": ["5"], "discount": [""], "tax_code": [""]})  # fmt: skip
    assert resp.status_code == 200 and "اختار الحساب" in resp.content.decode()

    # a return and a payment from their forms
    gloves = typed.lines.get(description="قفازات")
    resp = client.post(
        reverse("purchasing:return_new", args=[typed.pk]), {f"qty_{gloves.pk}": "1", "reason": "مقاس غلط"}
    )
    assert resp.status_code == 302 and purchasing.returned_total(typed) == D("28.50")
    resp = client.post(reverse("purchasing:payment_new"), {
        "go": "1", "supplier": supplier.pk, "amount": "1320.06", "method": "transfer", "treasury": world["bank"].pk,
        "date": "2026-03-10", f"alloc_{typed.pk}": "1320.06", "wht_code": "",
    })  # fmt: skip
    assert resp.status_code == 302 and purchasing.open_amount(typed) == 0

    # the reports export with numbers as numbers
    for url, params, expected in [
        (urls["invoices"], {"from": "2026-03-01", "to": "2026-03-31"}, 2666.0),
        (urls["aging"], {}, 1666.0),
        (urls["taxes"], {"report": "wht", "from": "2026-01-01", "to": "2026-03-31"}, 9.0),
        (urls["supplier"], {"from": "2026-03-01", "to": "2026-03-31"}, 2666.0),
        (urls["payments"], {"from": "2026-03-01", "to": "2026-03-31"}, 1320.06),
    ]:
        xlsx = client.get(url.split("?")[0], {**params, "format": "xlsx"})
        values = [c for row in load_workbook(io.BytesIO(xlsx.content)).active.iter_rows(values_only=True) for c in row]
        assert expected in values, url

    # cheques and recurring expenses from the screens
    cheque = payment.cheque
    resp = client.post(reverse("treasury:cheque_move", args=[cheque.pk]), {"to": "cleared"})
    cheque.refresh_from_db()
    assert resp.status_code == 302 and cheque.status == C.CLEARED
    client.post(reverse("treasury:cheque_undo", args=[cheque.pk]))
    cheque.refresh_from_db()
    assert cheque.status == C.ISSUED
    resp = client.post(urls["cheque_new"], {
        "direction": "received", "number": "88", "amount": "450", "due_date": "2026-04-01", "bank_name": "NBE",
        "account": _acc(world, "capital").pk, "party_name": "شريك",
    })  # fmt: skip
    assert resp.status_code == 302 and Cheque.objects.get(number="88").status == C.IN_HAND
    resp = client.post(urls["expense_new"], {
        "name": "الكهربا", "amount": "800", "treasury": world["bank"].pk, "counter_account": _acc(world, "rent").pk,
        "cost_center": _cc(world, "admin").pk, "day_of_month": "15", "is_active": "on",
    })  # fmt: skip
    template = ExpenseTemplate.objects.get(name="الكهربا")
    assert resp.status_code == 302
    resp = client.post(reverse("treasury:expense_record", args=[template.pk]), {"amount": "810"})
    assert resp.status_code == 302 and template.vouchers.get().amount == D("810.00")
    resp = client.post(
        urls["tax_edit"], {"name": tax.name, "kind": "vat", "rate": "14", "recoverable": "on", "is_active": "on"}
    )
    tax.refresh_from_db()
    assert resp.status_code == 302 and tax.recoverable
    resp = client.post(urls["settings"], {"wht_threshold": "500", "cheque_alert_days": "10"})
    assert resp.status_code == 302 and PurchaseSettings.for_org(org).wht_threshold == D("500")


# ================================================================ any sequence of documents keeps the books right

doc = st.one_of(
    st.tuples(st.just("bill"), st.sampled_from(["vat14", "vat14r", "vat0", ""]), st.integers(1, 900000)),
    st.tuples(st.just("pay"), st.sampled_from(["cash", "transfer", "cheque"]), st.integers(1, 900000)),
    st.tuples(st.just("pay_wht"), st.sampled_from(["transfer", "cheque"]), st.integers(1, 900000)),
    st.tuples(st.just("return"), st.just(""), st.integers(1, 9)),
    st.tuples(st.just("cancel_pay"), st.just(""), st.integers(0, 20)),
    st.tuples(st.just("cancel_bill"), st.just(""), st.integers(0, 20)),
    st.tuples(st.just("clear"), st.just(""), st.integers(0, 20)),
    st.tuples(st.just("give_back"), st.just(""), st.integers(0, 20)),
    st.tuples(st.just("undo"), st.just(""), st.integers(0, 20)),
)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(docs=st.lists(doc, min_size=1, max_size=16))
def test_any_sequence_of_supplier_documents_keeps_the_books_right(world, docs):
    """Random bills, payments (cash, transfer, cheque, with and without withholding), returns, cancellations and
    cheque steps, amounts in piasters. After every step: every entry balances, and the supplier's account, the
    cheques on their way, the tax withheld and the recoverable VAT each equal what their documents say."""
    org, owner, supplier, taxes = world["org"], world["owner"], world["supplier"], world["taxes"]
    sid = transaction.savepoint()
    try:
        _fund(world, "safe", "3000")
        bills, pays, n = [], [], 0
        for kind, how, raw in docs:
            amount = D(raw) / 100
            try:
                if kind == "bill":
                    n += 1
                    bills.append(purchasing.create_invoice(
                        org, supplier=supplier, date=TODAY, by=owner, supplier_ref=f"H-{n}",
                        lines=[LineIn("بند", _acc(world, "medical_supplies_expense"), "3", str(amount),
                                      _cc(world, "theatre"), tax_code=taxes.get(how))],
                    ))  # fmt: skip
                elif kind in ("pay", "pay_wht"):
                    n += 1
                    target = next((b for b in bills if purchasing.open_amount(b) > 0), None)
                    alloc = [(target, min(amount, purchasing.open_amount(target)))] if target else []
                    pays.append(purchasing.pay_supplier(
                        org, supplier=supplier, amount=amount, method=how, by=owner, allocations=alloc,
                        treasury_obj=world["safe"] if how == "cash" else world["bank"],
                        wht_code=taxes["wht3"] if kind == "pay_wht" else None, cheque_number=f"N{n}",
                        cheque_due=TODAY + timedelta(days=raw % 40),
                    ))  # fmt: skip
                elif kind == "return" and bills:
                    bill = bills[raw % len(bills)]
                    purchasing.create_return(bill, quantities={bill.lines.first().pk: "1"}, reason="x", by=owner)
                elif kind == "cancel_pay" and pays:
                    purchasing.cancel_payment(pays[raw % len(pays)], by=owner, reason="x")
                elif kind == "cancel_bill" and bills:
                    purchasing.cancel_invoice(bills[raw % len(bills)], by=owner, reason="x")
                elif kind in ("clear", "give_back", "undo"):
                    all_cheques = list(Cheque.objects.filter(organization=org).order_by("id"))
                    if all_cheques:
                        cheque = all_cheques[raw % len(all_cheques)]
                        if kind == "undo":
                            cheques.undo(cheque, by=owner)
                        else:
                            cheques.move(cheque, C.CLEARED if kind == "clear" else C.RETURNED, by=owner, notes="x")
            except (PurchaseError, treasury.TreasuryError):
                pass  # a refused document must leave nothing behind: the checks below prove it

            posted = JournalEntry.objects.filter(organization=org, status=EntryStatus.POSTED)
            for entry in posted.prefetch_related("lines"):
                lines = list(entry.lines.all())
                assert len(lines) >= 2 and sum(x.debit for x in lines) == sum(x.credit for x in lines) == entry.total
            assert reports.trial_balance(org, *YEAR).balanced
            live_bills = PurchaseInvoice.objects.filter(organization=org, status=DocStatus.POSTED)
            live_pays = SupplierPayment.objects.filter(organization=org, status=DocStatus.POSTED)
            billed = sum((b.total for b in live_bills), D(0))
            returned = sum((r.total for r in PurchaseReturnLine.objects.filter(organization=org)), D(0))
            paid = sum((p.amount for p in live_pays), D(0))
            assert _bal(world, "suppliers_payable") == purchasing.supplier_balance(supplier) == billed - returned - paid
            assert _bal(world, "cheques_payable") == cheques.open_total(org, ChequeDirection.ISSUED)
            assert _bal(world, "wht_payable") == sum((p.wht_amount for p in live_pays), D(0))
            vat_in = sum(
                (x.vat_amount for x in PurchaseLine.objects.filter(invoice__in=live_bills, vat_recoverable=True)), D(0)
            )
            vat_back = sum(
                (r.vat_amount for r in PurchaseReturnLine.objects.filter(organization=org, line__vat_recoverable=True)),
                D(0),
            )
            assert _bal(world, "vat_input") == vat_in - vat_back
            assert treasury.balance(world["safe"]) >= 0
            for bill in live_bills:
                assert 0 <= purchasing.open_amount(bill) <= bill.total
                assert purchasing.allocated_total(bill) + purchasing.returned_total(bill) <= bill.total or (
                    purchasing.open_amount(bill) == 0
                )
            for k, model in (("number", PurchaseInvoice), ("number", SupplierPayment)):
                numbers = list(model.objects.filter(organization=org).order_by(k).values_list(k, flat=True))
                assert numbers == list(range(1, len(numbers) + 1))
            for payment in SupplierPayment.objects.filter(organization=org, cheque__isnull=False).select_related(
                "cheque"
            ):
                # a payment and its cheque never disagree
                assert payment.is_cancelled == (payment.cheque.status in (C.CANCELLED, C.RETURNED))
    finally:
        transaction.savepoint_rollback(sid)
