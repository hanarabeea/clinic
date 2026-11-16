"""Purchasing logic. Views stay thin: every supplier document is made here and posted in the same transaction."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.inventory import services as stock
from apps.ledger import chart
from apps.ledger import services as ledger
from apps.ledger.models import AccountType
from apps.ledger.money import ZERO, money, percent_of
from apps.treasury import services as treasury
from apps.treasury.models import Treasury, TreasuryKind

from . import posting
from .models import (
    DocKind,
    DocStatus,
    PaymentAllocation,
    PayMethod,
    PurchaseInvoice,
    PurchaseLine,
    PurchaseReturn,
    PurchaseReturnLine,
    PurchaseSequence,
    PurchaseSettings,
    Supplier,
    SupplierPayment,
    TaxCode,
    TaxKind,
)

DEFAULT_TAXES = [
    ("vat14", "ض.ق.م 14%", TaxKind.VAT, "14"),
    ("vat0", "معفاة / صفر", TaxKind.VAT, "0"),
    ("wht1", "خصم 1% توريدات", TaxKind.WITHHOLDING, "1"),
    ("wht3", "خصم 3% خدمات", TaxKind.WITHHOLDING, "3"),
    ("wht5", "خصم 5% أتعاب مهنية وعمولات", TaxKind.WITHHOLDING, "5"),
]
MAX_LINES = 60


class PurchaseError(Exception):
    """Refused. The message is written for the person at the screen."""


def ensure_defaults(org) -> None:
    """The usual Egyptian tax codes, once. VAT starts as "not recoverable" (a clinic's services are VAT-exempt, so
    the VAT it pays is part of the cost); a VAT-registered center switches that on in the tax settings."""
    have = set(TaxCode.objects.filter(organization=org).exclude(system_key="").values_list("system_key", flat=True))
    for key, name, kind, rate in DEFAULT_TAXES:
        if key not in have and not TaxCode.objects.filter(organization=org, name=name).exists():
            TaxCode.objects.create(organization=org, name=name, kind=kind, rate=Decimal(rate), system_key=key)
    PurchaseSettings.for_org(org)


def _next_number(org, fiscal_year, kind) -> int:
    seq, _created = PurchaseSequence.objects.get_or_create(organization=org, fiscal_year=fiscal_year, kind=kind)
    seq = PurchaseSequence.objects.select_for_update().get(pk=seq.pk)
    seq.last_number += 1
    seq.save(update_fields=["last_number", "updated_at"])
    return seq.last_number


def _dec(value, what) -> Decimal:
    try:
        value = Decimal(str(value if value not in (None, "") else "0").replace(",", ""))
    except (ArithmeticError, ValueError) as e:
        raise PurchaseError(_("%(w)s لازم يتكتب بالأرقام.") % {"w": what}) from e
    if not value.is_finite():
        raise PurchaseError(_("%(w)s لازم يتكتب بالأرقام.") % {"w": what})
    return value


# --- suppliers ---------------------------------------------------------------------------------------------------


@transaction.atomic
def save_supplier(org, *, by, supplier=None, opening_balance=None, opening_date=None, **fields) -> Supplier:
    name = " ".join((fields.get("name") or "").split())
    if not name:
        raise PurchaseError(_("اكتب اسم المورد."))
    clash = Supplier.objects.filter(organization=org, name__iexact=name)
    if supplier is not None:
        clash = clash.exclude(pk=supplier.pk)
    if clash.exists():
        raise PurchaseError(_("فيه مورد بنفس الاسم."))
    wht = fields.get("default_withholding")
    if wht is not None and (wht.organization_id != org.pk or wht.kind != TaxKind.WITHHOLDING):
        raise PurchaseError(_("اختار نسبة خصم تحت حساب الضريبة."))
    new = supplier is None
    supplier = supplier or Supplier(organization=org)
    fields["name"] = name
    for key, value in fields.items():
        setattr(supplier, key, value)
    supplier._history_user = by
    supplier.full_clean(exclude=["organization"])
    supplier.save()
    if new and opening_balance not in (None, ""):
        amount = money(_dec(opening_balance, _("الرصيد الافتتاحي")))
        if amount != 0:
            try:
                posting.post_opening(supplier, amount, opening_date or ledger.today(), by=by)
            except ledger.LedgerError as e:
                raise PurchaseError(str(e)) from e
    return supplier


def supplier_balance(supplier) -> Decimal:
    """What the clinic owes the supplier by the books (negative: the supplier owes the clinic)."""
    return ledger.balance_of(chart.account(supplier.organization, "suppliers_payable"), party=supplier)


# --- purchase invoices -------------------------------------------------------------------------------------------


@dataclass
class LineIn:
    """A line as typed."""

    description: str
    account: object
    quantity: object
    unit_price: object
    cost_center: object = None
    discount: object = 0
    tax_code: object = None
    item: object = None  # a stock item: `account` is ignored (it is the inventory account) and it needs `warehouse`
    warehouse: object = None
    expiry_date: object = None
    lot: str = ""


def price_line(quantity, unit_price, discount, rate, inclusive: bool) -> tuple[Decimal, Decimal]:
    """(net, vat) of one line, each rounded once. With prices that include VAT the tax is what is left after the
    net is taken out, so net + vat is exactly what was typed."""
    gross = money(quantity * unit_price) - discount
    if gross < 0:
        raise PurchaseError(_("الخصم أكبر من قيمة السطر."))
    if inclusive and rate:
        net = money(gross * 100 / (100 + rate))
        return net, gross - net
    return gross, percent_of(gross, rate) if rate else ZERO


def _check_line_account(org, account, center, what) -> None:
    if account is None or account.organization_id != org.pk:
        raise PurchaseError(_("اختار الحساب في سطر «%(w)s».") % {"w": what})
    if account.is_group or not account.is_active:
        raise PurchaseError(_("حساب «%(w)s» لازم يكون حساب فرعي مفعّل.") % {"w": what})
    if account.system_key in chart.CONTROL_KEYS or Treasury.objects.filter(account=account).exists():
        raise PurchaseError(_("%(a)s مينفعش يتحمّل عليه مشتريات مباشرة.") % {"a": account})
    if center is not None and center.organization_id != org.pk:
        raise PurchaseError(_("مركز التكلفة ده مش في العيادة دي."))
    if account.type in (AccountType.EXPENSE, AccountType.REVENUE) and center is None:
        raise PurchaseError(_("اختار مركز التكلفة في سطر «%(w)s» (مطلوب لحسابات المصروفات).") % {"w": what})


@transaction.atomic
def create_invoice(
    org, *, supplier, date, lines, by, supplier_ref="", due_date=None, prices_include_vat=False, notes="", scan=None
) -> PurchaseInvoice:
    """Enter a supplier's invoice. Tax is worked out line by line and rounded per line; the totals are sums."""
    if supplier is None or supplier.organization_id != org.pk or not supplier.is_active:
        raise PurchaseError(_("اختار مورد شغال."))
    if date is None:
        raise PurchaseError(_("اكتب تاريخ الفاتورة."))
    lines = list(lines)
    if not lines:
        raise PurchaseError(_("الفاتورة محتاجة سطر واحد على الأقل."))
    if len(lines) > MAX_LINES:
        raise PurchaseError(_("الفاتورة أكبر من %(n)s سطر. قسّمها.") % {"n": MAX_LINES})
    supplier_ref = " ".join((supplier_ref or "").split())[:60]
    if (
        supplier_ref
        and PurchaseInvoice.objects.filter(
            supplier=supplier, supplier_ref=supplier_ref, status=DocStatus.POSTED
        ).exists()
    ):
        raise PurchaseError(_("فاتورة المورد رقم %(r)s متسجلة قبل كده.") % {"r": supplier_ref})
    priced = []
    for raw in lines:
        what = " ".join((raw.description or "").split())
        if raw.item is not None:
            what = what or raw.item.name
            if raw.item.organization_id != org.pk or not raw.item.is_active:
                raise PurchaseError(_("الصنف «%(w)s» مش متاح.") % {"w": what})
            if raw.warehouse is None or raw.warehouse.organization_id != org.pk or not raw.warehouse.is_active:
                raise PurchaseError(_("اختار المخزن اللي «%(w)s» هيدخله.") % {"w": what})
            if raw.item.track_expiry and raw.expiry_date is None:
                raise PurchaseError(_("اكتب تاريخ صلاحية «%(w)s».") % {"w": what})
            raw.account, raw.cost_center = chart.account(org, "inventory"), None
        elif not what:
            raise PurchaseError(_("اكتب البيان في كل سطر."))
        else:
            _check_line_account(org, raw.account, raw.cost_center, what)
        quantity = _dec(raw.quantity, _("الكمية")).quantize(Decimal("0.001"))
        unit_price = _dec(raw.unit_price, _("السعر")).quantize(Decimal("0.0001"))
        discount = money(_dec(raw.discount, _("الخصم")))
        if quantity <= 0 or unit_price < 0 or discount < 0:
            raise PurchaseError(
                _("الكمية لازم تكون أكبر من صفر، والسعر والخصم مش بالسالب (سطر «%(w)s»).") % {"w": what}
            )
        tax = raw.tax_code
        if tax is not None and (tax.organization_id != org.pk or tax.kind != TaxKind.VAT or not tax.is_active):
            raise PurchaseError(_("اختار ضريبة قيمة مضافة شغالة في سطر «%(w)s».") % {"w": what})
        rate = tax.rate if tax else Decimal(0)
        net, vat = price_line(quantity, unit_price, discount, rate, prices_include_vat)
        priced.append((what, raw, quantity, unit_price, discount, net, vat, tax, rate))
    subtotal = sum((p[5] for p in priced), ZERO)
    vat_total = sum((p[6] for p in priced), ZERO)
    if subtotal + vat_total <= 0:
        raise PurchaseError(_("إجمالي الفاتورة صفر."))
    fiscal_year = ledger.fiscal_year_for(org, date)
    invoice = PurchaseInvoice(
        organization=org, supplier=supplier, fiscal_year=fiscal_year,
        number=_next_number(org, fiscal_year, DocKind.INVOICE), supplier_ref=supplier_ref, date=date,
        due_date=due_date or date + timedelta(days=supplier.payment_terms_days),
        prices_include_vat=prices_include_vat, subtotal=subtotal, vat_total=vat_total, total=subtotal + vat_total,
        notes=(notes or "")[:300], created_by=by,
    )  # fmt: skip
    if scan is not None:
        _attach_scan(invoice, scan)
    invoice.save()
    made = PurchaseLine.objects.bulk_create(
        PurchaseLine(
            organization=org, invoice=invoice, order=i, description=what[:200], account=raw.account,
            cost_center=raw.cost_center, quantity=quantity, unit_price=unit_price, discount=discount, net=net,
            tax_code=tax, vat_rate=rate, vat_amount=vat, vat_recoverable=bool(tax and tax.recoverable and vat),
            total=net + vat, item=raw.item, warehouse=raw.warehouse if raw.item else None,
            expiry_date=raw.expiry_date if raw.item else None, lot=(raw.lot or "")[:40] if raw.item else "",
        )
        for i, (what, raw, quantity, unit_price, discount, net, vat, tax, rate) in enumerate(priced, start=1)
    )  # fmt: skip
    try:
        for line in made:
            if line.item_id:  # the goods go on the shelf at what the line cost
                line.invoice = invoice
                stock.receive_purchase_line(line, by=by)
        posting.post_invoice(invoice, by=by)
    except (ledger.LedgerError, stock.StockError) as e:
        raise PurchaseError(str(e)) from e
    return invoice


def _attach_scan(invoice, f) -> None:
    """The paper invoice's scan: a PDF or a picture, checked by its content, shrunk like every upload."""
    from apps.clinical.services import shrink_image, sniff_upload

    try:
        content_type = sniff_upload(f)
    except ValidationError as e:
        raise PurchaseError(" ".join(e.messages)) from e
    f = shrink_image(f, content_type)
    if getattr(f, "content_type", "") == "image/jpeg":
        content_type = "image/jpeg"
    f.name = "scan" + {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}[content_type]
    invoice.scan, invoice.scan_content_type = f, content_type


def returned_total(invoice) -> Decimal:
    return invoice.returns.aggregate(s=Sum("total"))["s"] or ZERO


def allocated_total(invoice) -> Decimal:
    rows = PaymentAllocation.objects.filter(invoice=invoice, payment__status=DocStatus.POSTED)
    return rows.aggregate(s=Sum("amount"))["s"] or ZERO


def open_amount(invoice) -> Decimal:
    """What is still to be paid on this invoice (never below zero: a return after full payment leaves a credit on
    the supplier's account, not a negative invoice)."""
    if invoice.is_cancelled:
        return ZERO
    return max(invoice.total - returned_total(invoice) - allocated_total(invoice), ZERO)


@transaction.atomic
def cancel_invoice(invoice, *, by, reason) -> PurchaseInvoice:
    """Entered by mistake: its entry is reversed. Not once something was paid or returned against it."""
    invoice = PurchaseInvoice.objects.select_for_update(of=("self",)).select_related("supplier").get(pk=invoice.pk)
    if invoice.is_cancelled:
        return invoice
    if not (reason or "").strip():
        raise PurchaseError(_("اكتب سبب الإلغاء."))
    if allocated_total(invoice) > 0:
        raise PurchaseError(_("فيه سداد متسجل على الفاتورة دي. الغي السداد الأول."))
    if invoice.returns.exists():
        raise PurchaseError(_("الفاتورة دي عليها مرتجع، مينفعش تتلغي."))
    if _has_stock(invoice):
        raise PurchaseError(_("الفاتورة دي دخّلت أصناف المخزن. اعمل مرتجع مشتريات بدل الإلغاء."))
    try:
        posting.reverse_document(invoice, by=by)
    except ledger.LedgerError as e:
        raise PurchaseError(str(e)) from e
    invoice.status = DocStatus.CANCELLED
    invoice.cancelled_by, invoice.cancelled_at, invoice.cancel_reason = by, timezone.now(), reason.strip()[:200]
    invoice.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancel_reason", "updated_at"])
    return invoice


def _has_stock(invoice) -> bool:
    return invoice.lines.filter(item__isnull=False).exists()


@transaction.atomic
def create_return(invoice, *, quantities: dict, reason, by, date=None) -> PurchaseReturn:
    """Send back part (or all) of an invoice. `quantities` = {purchase line pk: quantity going back}. Each line's
    amounts go back in proportion, and the last piece of a line takes whatever is left of it, so a line returned in
    several steps comes to exactly its own amounts."""
    invoice = PurchaseInvoice.objects.select_for_update(of=("self",)).select_related("supplier").get(pk=invoice.pk)
    org = invoice.organization
    if invoice.is_cancelled:
        raise PurchaseError(_("الفاتورة دي ملغية."))
    if not (reason or "").strip():
        raise PurchaseError(_("اكتب سبب المرتجع."))
    date = date or ledger.today()
    if date < invoice.date:
        raise PurchaseError(_("تاريخ المرتجع قبل تاريخ الفاتورة."))
    rows = []
    for line in invoice.lines.select_related("item", "warehouse"):
        qty = _dec(quantities.get(line.pk) or quantities.get(str(line.pk)) or 0, _("الكمية")).quantize(Decimal("0.001"))
        if qty == 0:
            continue
        before = line.returned.aggregate(q=Sum("quantity"), n=Sum("net"), v=Sum("vat_amount"))
        done_q, done_n, done_v = before["q"] or Decimal(0), before["n"] or ZERO, before["v"] or ZERO
        if qty < 0 or qty > line.quantity - done_q:
            raise PurchaseError(
                _("أقصى كمية ترجع من «%(d)s» هي %(q)s.") % {"d": line.description, "q": f"{line.quantity - done_q:g}"}
            )
        if qty == line.quantity - done_q:
            net, vat = line.net - done_n, line.vat_amount - done_v
        else:
            net, vat = money(line.net * qty / line.quantity), money(line.vat_amount * qty / line.quantity)
        rows.append((line, qty, net, vat))
    if not rows:
        raise PurchaseError(_("اكتب الكمية اللي راجعة في سطر واحد على الأقل."))
    fiscal_year = ledger.fiscal_year_for(org, date)
    subtotal, vat_total = sum((r[2] for r in rows), ZERO), sum((r[3] for r in rows), ZERO)
    ret = PurchaseReturn.objects.create(
        organization=org, invoice=invoice, fiscal_year=fiscal_year,
        number=_next_number(org, fiscal_year, DocKind.RETURN), date=date, reason=reason.strip()[:200],
        subtotal=subtotal, vat_total=vat_total, total=subtotal + vat_total, created_by=by,
    )  # fmt: skip
    made = PurchaseReturnLine.objects.bulk_create(
        PurchaseReturnLine(organization=org, ret=ret, line=line, quantity=qty, net=net, vat_amount=vat, total=net + vat)
        for line, qty, net, vat in rows
    )
    try:
        for back in made:
            if back.line.item_id:  # the goods leave the store they were received into
                back.ret = ret
                back.stock_value = stock.return_purchase_line(back, by=by)
                back.save(update_fields=["stock_value", "updated_at"])
        posting.post_return(ret, by=by)
    except (ledger.LedgerError, stock.StockError) as e:
        raise PurchaseError(str(e)) from e
    return ret


# --- paying suppliers --------------------------------------------------------------------------------------------


def withholding_base(allocations) -> Decimal:
    """The taxable value a payment settles: each allocated amount less its share of the invoice's VAT."""
    base = ZERO
    for invoice, amount in allocations:
        base += money(amount * invoice.subtotal / invoice.total) if invoice.total else amount
    return base


def suggest_withholding(org, supplier, allocations, amount) -> tuple[Decimal, Decimal]:
    """(base, amount) of the withholding the supplier's usual code would take from this payment: nothing when the
    supplier has no code or the taxable value is not above the clinic's threshold."""
    code = supplier.default_withholding
    if code is None or not code.is_active:
        return ZERO, ZERO
    base = withholding_base(allocations) if allocations else money(amount)
    if base <= PurchaseSettings.for_org(org).wht_threshold:
        return base, ZERO
    return base, percent_of(base, code.rate)


@transaction.atomic
def pay_supplier(
    org, *, supplier, amount, method, treasury_obj, by, date=None, allocations=None, wht_code=None, wht_base=None,
    cheque_number="", cheque_due=None, notes="",
) -> SupplierPayment:  # fmt: skip
    """Pay a supplier. `allocations` = [(invoice, amount)] says which invoices it settles (the rest stays on the
    supplier's account as money paid ahead). With `wht_code`, that share of `wht_base` (default: the taxable value
    being settled) is withheld: the supplier gets `amount` less the withholding, and their account drops by all of
    `amount`. By cheque nothing leaves the bank now: the cheque sits in "أوراق الدفع" until it clears."""
    from apps.treasury import cheques

    if supplier is None or supplier.organization_id != org.pk:
        raise PurchaseError(_("اختار المورد."))
    amount = money(_dec(amount, _("المبلغ")))
    if amount <= 0:
        raise PurchaseError(_("المبلغ لازم يكون أكبر من صفر."))
    if method not in PayMethod.values:
        raise PurchaseError(_("اختار طريقة السداد."))
    if treasury_obj is None or treasury_obj.organization_id != org.pk or not treasury_obj.is_active:
        raise PurchaseError(_("اختار الخزينة أو الحساب البنكي."))
    if method != PayMethod.CASH and treasury_obj.kind != TreasuryKind.BANK:
        raise PurchaseError(_("التحويل والشيك لازم يكونوا من حساب بنكي."))
    if method == PayMethod.CASH and not treasury_obj.holds_cash:
        raise PurchaseError(_("السداد النقدي لازم يكون من خزينة أو درج."))
    date = date or ledger.today()

    clean = []
    for invoice, part in allocations or []:
        invoice = PurchaseInvoice.objects.select_for_update(of=("self",)).get(pk=invoice.pk)
        part = money(_dec(part, _("المبلغ")))
        if part == 0:
            continue
        if invoice.supplier_id != supplier.pk or invoice.is_cancelled:
            raise PurchaseError(_("فيه فاتورة مش بتاعة المورد ده، أو ملغية."))
        if part < 0 or part > open_amount(invoice):
            raise PurchaseError(
                _("المبلغ على %(i)s أكبر من الباقي عليها (%(o)s).") % {"i": invoice.label, "o": open_amount(invoice)}
            )
        clean.append((invoice, part))
    if len({inv.pk for inv, _part in clean}) != len(clean):
        raise PurchaseError(_("الفاتورة متكررة في التوزيع."))
    if sum((part for _inv, part in clean), ZERO) > amount:
        raise PurchaseError(_("المتوزّع على الفواتير أكبر من المبلغ."))

    wht_rate = wht_amount = base = ZERO
    if wht_code is not None:
        if wht_code.organization_id != org.pk or wht_code.kind != TaxKind.WITHHOLDING or not wht_code.is_active:
            raise PurchaseError(_("اختار نسبة خصم تحت حساب الضريبة شغالة."))
        base = money(_dec(wht_base, _("وعاء الخصم"))) if wht_base not in (None, "") else (
            withholding_base(clean) if clean else amount
        )  # fmt: skip
        if base < 0 or base > amount:
            raise PurchaseError(_("وعاء الخصم لازم يكون بين صفر والمبلغ."))
        wht_rate, wht_amount = wht_code.rate, percent_of(base, wht_code.rate)
    net_paid = amount - wht_amount

    fiscal_year = ledger.fiscal_year_for(org, date)
    payment = SupplierPayment(
        organization=org, supplier=supplier, fiscal_year=fiscal_year,
        number=_next_number(org, fiscal_year, DocKind.PAYMENT), date=date, method=method, treasury=treasury_obj,
        amount=amount, wht_code=wht_code if wht_amount else None, wht_base=base if wht_amount else ZERO,
        wht_rate=wht_rate if wht_amount else ZERO, wht_amount=wht_amount, net_paid=net_paid,
        notes=(notes or "")[:300], created_by=by,
    )  # fmt: skip
    try:
        if method == PayMethod.CHEQUE:
            if net_paid <= 0:
                raise PurchaseError(_("مفيش مبلغ يتكتب بيه الشيك بعد الخصم."))
            payment.cheque = cheques.issue_cheque(
                org, bank=treasury_obj, number=cheque_number, due_date=cheque_due, amount=net_paid, by=by,
                party=supplier, party_name=supplier.name, post=False,
            )  # fmt: skip
        elif treasury_obj.holds_cash and net_paid > treasury.cash_on_hand(treasury_obj):
            raise PurchaseError(
                _("مفيش في %(t)s غير %(b)s.") % {"t": treasury_obj.name, "b": treasury.cash_on_hand(treasury_obj)}
            )
        payment.save()
        PaymentAllocation.objects.bulk_create(
            PaymentAllocation(organization=org, payment=payment, invoice=invoice, amount=part)
            for invoice, part in clean
        )
        posting.post_payment(payment, by=by)
    except (ledger.LedgerError, treasury.TreasuryError) as e:
        raise PurchaseError(str(e)) from e
    return payment


@transaction.atomic
def cancel_payment(payment, *, by, reason, from_cheque=False) -> SupplierPayment:
    """A payment recorded by mistake, or whose cheque will never be paid: its entry is reversed and the invoices it
    settled are open again. A payment by a cheque is cancelled from the cheque (so the two never disagree), and
    never once the bank paid it."""
    from apps.treasury.models import ChequeStatus

    payment = (
        SupplierPayment.objects.select_for_update(of=("self",))
        .select_related("supplier", "treasury__account", "cheque")
        .get(pk=payment.pk)
    )
    if payment.is_cancelled:
        return payment
    if not (reason or "").strip():
        raise PurchaseError(_("اكتب سبب الإلغاء."))
    if payment.cheque_id and not from_cheque:
        if payment.cheque.status != ChequeStatus.ISSUED:
            raise PurchaseError(_("الشيك بتاع السداد ده «%(s)s».") % {"s": payment.cheque.get_status_display()})
        from apps.treasury import cheques

        try:
            cheques.move(payment.cheque, ChequeStatus.CANCELLED, by=by, notes=reason)
        except treasury.TreasuryError as e:
            raise PurchaseError(str(e)) from e
        payment.refresh_from_db()
        return payment
    try:
        posting.reverse_document(payment, by=by)
    except ledger.LedgerError as e:
        raise PurchaseError(str(e)) from e
    payment.status = DocStatus.CANCELLED
    payment.cancelled_by, payment.cancelled_at, payment.cancel_reason = by, timezone.now(), reason.strip()[:200]
    payment.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancel_reason", "updated_at"])
    return payment
