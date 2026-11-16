"""Purchasing screens: suppliers, their invoices and returns, paying them, and the taxes on all of it. Thin views:
every document is made and posted by purchasing.services."""

from datetime import date as date_cls

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import has_perm, require_perm
from apps.audit import services as audit
from apps.inventory.models import StockItem, Warehouse
from apps.ledger import chart
from apps.ledger import reports as ledger_reports
from apps.ledger import services as ledger
from apps.ledger.export import Table, respond
from apps.ledger.models import Account, CostCenter
from apps.ledger.money import ZERO
from apps.ledger.views import _parse_date, _period_label, _range
from apps.treasury import services as treasury
from apps.treasury.models import Treasury

from . import reports, services
from .forms import PurchaseSettingsForm, SupplierForm, TaxCodeForm
from .models import (
    DocStatus,
    PayMethod,
    PurchaseInvoice,
    PurchaseSettings,
    Supplier,
    SupplierPayment,
    TaxCode,
    TaxKind,
)


def _supplier(request, pk):
    return get_object_or_404(Supplier.objects.for_org(request.organization), pk=pk)


def _invoice(request, pk):
    return get_object_or_404(
        PurchaseInvoice.objects.for_org(request.organization).select_related("supplier", "fiscal_year", "created_by"),
        pk=pk,
    )


def _balances(org) -> dict:
    from apps.invoicing.services import party_balances

    return party_balances(org, "suppliers_payable", "supplier")


# --- purchase invoices -------------------------------------------------------------------------------------------


@require_perm("purchase.view")
def invoices(request):
    org = request.organization
    start, end, presets = _range(request)
    rows = (
        PurchaseInvoice.objects.for_org(org)
        .filter(date__gte=start, date__lte=end)
        .select_related("supplier", "fiscal_year")
    )
    supplier = None
    if request.GET.get("supplier", "").isdigit():
        supplier = _supplier(request, request.GET["supplier"])
        rows = rows.filter(supplier=supplier)
    rows = list(rows[:500])
    live = [r for r in rows if not r.is_cancelled]
    totals = {k: sum((getattr(r, k) for r in live), ZERO) for k in ("subtotal", "vat_total", "total")}
    if request.GET.get("format"):
        table = Table(
            title=_("فواتير المشتريات"),
            subtitle=_period_label(start, end),
            headers=[_("الرقم"), _("التاريخ"), _("المورد"), _("رقم فاتورة المورد"), _("قبل الضريبة"), _("الضريبة"),
                     _("الإجمالي")],
            rows=[[r.label, f"{r.date:%d/%m/%Y}", r.supplier.name, r.supplier_ref, r.subtotal, r.vat_total, r.total]
                  for r in live],
            totals=[_("الإجمالي"), "", "", "", totals["subtotal"], totals["vat_total"], totals["total"]],
        )  # fmt: skip
        return respond(request, table, "purchases")
    ctx = {"rows": rows, "totals": totals, "start": start, "end": end, "presets": presets, "supplier": supplier,
           "keep": f"&supplier={supplier.pk}" if supplier else "", "tab": "invoices"}  # fmt: skip
    return render(request, "purchasing/invoices.html", ctx)


def _typed_rows(post) -> list[dict]:
    names = ("description", "account", "cost_center", "quantity", "unit_price", "discount", "tax_code")
    columns = [post.getlist(n) for n in names]
    rows = [dict(zip(names, values, strict=True)) for values in zip(*columns, strict=False)]
    # the stock columns are on the form only when the clinic has stock items
    for row, item, expiry in zip(rows, post.getlist("item"), post.getlist("expiry"), strict=False):
        row["item"], row["expiry"] = item, expiry
    return rows


@require_perm("purchase.manage")
def invoice_new(request):
    org = request.organization
    services.ensure_defaults(org)
    chart.ensure(org)
    treasury_accounts = Treasury.objects.for_org(org).values("account_id")
    accounts = list(
        Account.objects.for_org(org)
        .filter(is_group=False, is_active=True)
        .exclude(pk__in=treasury_accounts)
        .exclude(system_key__in=chart.CONTROL_KEYS)
        .order_by("code")
    )
    centers = list(CostCenter.objects.for_org(org).filter(is_active=True))
    taxes = list(TaxCode.objects.for_org(org).filter(kind=TaxKind.VAT, is_active=True))
    suppliers = Supplier.objects.for_org(org).filter(is_active=True)
    stock_items = list(StockItem.objects.for_org(org).filter(is_active=True))
    stores = list(Warehouse.objects.for_org(org).filter(is_active=True)) if stock_items else []
    post, error = request.POST, ""
    if request.method == "POST":
        by_pk = lambda rows: {str(r.pk): r for r in rows}  # noqa: E731
        accounts_by, centers_by, taxes_by, items_by = by_pk(accounts), by_pk(centers), by_pk(taxes), by_pk(stock_items)
        store = by_pk(stores).get(post.get("warehouse", ""))
        lines = [
            services.LineIn(
                description=row["description"], account=accounts_by.get(row["account"]),
                cost_center=centers_by.get(row["cost_center"]), quantity=row["quantity"] or "1",
                unit_price=row["unit_price"], discount=row["discount"], tax_code=taxes_by.get(row["tax_code"]),
                item=items_by.get(row.get("item", "")), warehouse=store,
                expiry_date=_parse_date(row.get("expiry", ""), None),
            )
            for row in _typed_rows(post)
            if row["description"].strip() or row["unit_price"].strip() or row.get("item")
        ]  # fmt: skip
        try:
            supplier = suppliers.filter(pk=post.get("supplier") or 0).first()
            with transaction.atomic():
                invoice = services.create_invoice(
                    org, supplier=supplier, date=_parse_date(post.get("date"), None), lines=lines, by=request.user,
                    supplier_ref=post.get("supplier_ref", ""), due_date=_parse_date(post.get("due_date"), None),
                    prices_include_vat=post.get("prices_include_vat") == "on", notes=post.get("notes", ""),
                    scan=request.FILES.get("scan"),
                )  # fmt: skip
        except services.PurchaseError as e:
            error = str(e)
        except IntegrityError:
            error = _("فاتورة المورد دي متسجلة قبل كده.")
        else:
            audit.log("purchase.invoice", request=request, target=invoice, summary=f"{invoice.label}: {invoice.total}")
            messages.success(request, _("%(i)s اتسجلت واترحّلت.") % {"i": invoice.label})
            return redirect("purchasing:invoice", pk=invoice.pk)
    rows = _typed_rows(post)
    blank = dict.fromkeys(
        ("description", "account", "cost_center", "unit_price", "discount", "tax_code", "item", "expiry"), ""
    )
    while len(rows) < 3:
        rows.append({**blank, "quantity": "1"})
    ctx = {
        "accounts": accounts, "centers": centers, "taxes": taxes, "suppliers": suppliers, "rows": rows,
        "stock_items": stock_items, "stores": stores,
        "error": error, "post": post, "today": ledger.today(),
        "chosen": post.get("supplier") or request.GET.get("supplier", ""),
        "tab": "invoices",
    }  # fmt: skip
    return render(request, "purchasing/invoice_new.html", ctx)


@require_perm("purchase.view")
def invoice_detail(request, pk):
    invoice = _invoice(request, pk)
    lines = list(invoice.lines.select_related("account", "cost_center", "tax_code", "warehouse"))
    done = {}
    for ret in invoice.returns.prefetch_related("lines"):
        for r in ret.lines.all():
            done[r.line_id] = done.get(r.line_id, 0) + r.quantity
    for line in lines:
        line.returned_qty = done.get(line.pk, 0)
        line.left_qty = line.quantity - line.returned_qty
    ctx = {
        "invoice": invoice,
        "lines": lines,
        "returns": invoice.returns.select_related("fiscal_year"),
        "allocations": invoice.allocations.filter(payment__status=DocStatus.POSTED).select_related(
            "payment__fiscal_year"
        ),
        "open": services.open_amount(invoice),
        "can_return": not invoice.is_cancelled and any(line.left_qty > 0 for line in lines),
        "entry": ledger.entry_for(invoice) if has_perm(request.membership, "ledger.view") else None,
        "tab": "invoices",
    }
    return render(request, "purchasing/invoice.html", ctx)


@require_perm("purchase.view")
def invoice_scan(request, pk):
    invoice = _invoice(request, pk)
    if not invoice.scan:
        raise Http404
    audit.log("purchase.scan_viewed", request=request, target=invoice, summary=invoice.label)
    return FileResponse(invoice.scan.open("rb"), content_type=invoice.scan_content_type or "application/octet-stream")


@require_POST
@require_perm("purchase.manage")
def invoice_cancel(request, pk):
    invoice = _invoice(request, pk)
    try:
        services.cancel_invoice(invoice, by=request.user, reason=request.POST.get("reason", ""))
    except services.PurchaseError as e:
        messages.error(request, str(e))
    else:
        audit.log("purchase.invoice_cancelled", request=request, target=invoice, summary=invoice.label)
        messages.success(request, _("%(i)s اتلغت، وقيدها اتعكس.") % {"i": invoice.label})
    return redirect("purchasing:invoice", pk=invoice.pk)


@require_POST
@require_perm("purchase.manage")
def return_new(request, pk):
    invoice = _invoice(request, pk)
    quantities = {key[4:]: value for key, value in request.POST.items() if key.startswith("qty_") and value.strip()}
    try:
        ret = services.create_return(
            invoice, quantities=quantities, reason=request.POST.get("reason", ""), by=request.user
        )
    except services.PurchaseError as e:
        messages.error(request, str(e))
    else:
        audit.log("purchase.return", request=request, target=invoice, summary=f"{ret.label}: {ret.total}")
        messages.success(request, _("المرتجع %(r)s اتسجل.") % {"r": ret.label})
    return redirect("purchasing:invoice", pk=invoice.pk)


# --- suppliers ---------------------------------------------------------------------------------------------------


@require_perm("purchase.view")
def suppliers(request):
    org = request.organization
    balances = _balances(org)
    rows = list(Supplier.objects.for_org(org).order_by("-is_active", "name"))
    for s in rows:
        s.owed = balances.get(s.pk, ZERO)
    ctx = {"suppliers": rows, "total": sum((s.owed for s in rows), ZERO), "tab": "suppliers"}
    return render(request, "purchasing/suppliers.html", ctx)


@require_perm("supplier.manage")
def supplier_edit(request, pk=None):
    org = request.organization
    services.ensure_defaults(org)
    supplier = _supplier(request, pk) if pk else None
    form = SupplierForm(request.POST or None, instance=supplier, org=org)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        opening = data.pop("opening_balance", None)
        try:
            saved = services.save_supplier(org, by=request.user, supplier=supplier, opening_balance=opening, **data)
        except services.PurchaseError as e:
            form.add_error("name", str(e))
        else:
            audit.log("supplier.saved", request=request, target=saved, summary=saved.name)
            return redirect("purchasing:supplier", pk=saved.pk)
    return render(request, "purchasing/supplier_form.html", {"form": form, "supplier": supplier, "tab": "suppliers"})


@require_perm("purchase.view")
def supplier_detail(request, pk):
    org = request.organization
    supplier = _supplier(request, pk)
    start, end, presets = _range(request)
    account = chart.account(org, "suppliers_payable")
    statement = ledger_reports.account_statement(org, account, start, end, party=supplier)
    if request.GET.get("format"):
        table = Table(
            title=_("كشف حساب مورد: %(s)s") % {"s": supplier.name},
            subtitle=_period_label(start, end),
            headers=[_("التاريخ"), _("القيد"), _("البيان"), _("مدين"), _("دائن"), _("الرصيد")],
            rows=[[_("رصيد أول المدة"), "", "", None, None, statement.opening]]
            + [[f"{r.line.entry.date:%d/%m/%Y}", r.line.entry.number, r.line.memo or r.line.entry.memo,
                r.line.debit or None, r.line.credit or None, r.balance] for r in statement.lines],
            totals=[_("الإجمالي"), "", "", statement.debit, statement.credit, statement.closing],
        )  # fmt: skip
        return respond(request, table, f"supplier-{supplier.pk}")
    ctx = {
        "supplier": supplier, "st": statement, "start": start, "end": end, "presets": presets,
        "owed": services.supplier_balance(supplier),
        "open_invoices": reports.open_invoices(org, ledger.today(), supplier=supplier), "tab": "suppliers",
    }  # fmt: skip
    return render(request, "purchasing/supplier.html", ctx)


@require_perm("purchase.view")
def aging(request):
    org = request.organization
    as_of = _parse_date(request.GET.get("date"), ledger.today())
    rows = reports.aging(org, as_of)
    totals = [sum((r.buckets[i] for r in rows), ZERO) for i in range(len(reports.BUCKETS))]
    labels = [label for _limit, label in reports.BUCKETS]
    if request.GET.get("format"):
        table = Table(
            title=_("أعمار ديون الموردين"),
            subtitle=f"{as_of:%d/%m/%Y}",
            headers=[_("المورد"), *labels, _("الإجمالي")],
            rows=[[r.supplier.name, *r.buckets, r.total] for r in rows],
            totals=[_("الإجمالي"), *totals, sum(totals, ZERO)],
        )
        return respond(request, table, "suppliers-aging")
    ctx = {"rows": rows, "labels": labels, "totals": totals, "grand": sum(totals, ZERO), "as_of": as_of,
           "tab": "suppliers"}  # fmt: skip
    return render(request, "purchasing/aging.html", ctx)


# --- payments ----------------------------------------------------------------------------------------------------


@require_perm("purchase.view")
def payments(request):
    org = request.organization
    start, end, presets = _range(request)
    rows = list(
        SupplierPayment.objects.for_org(org)
        .filter(date__gte=start, date__lte=end)
        .select_related("supplier", "fiscal_year", "treasury")[:500]
    )
    live = [r for r in rows if not r.is_cancelled]
    totals = {k: sum((getattr(r, k) for r in live), ZERO) for k in ("amount", "wht_amount", "net_paid")}
    if request.GET.get("format"):
        table = Table(
            title=_("مدفوعات الموردين"),
            subtitle=_period_label(start, end),
            headers=[_("الرقم"), _("التاريخ"), _("المورد"), _("الطريقة"), _("المبلغ"), _("المخصوم"), _("المدفوع")],
            rows=[[r.label, f"{r.date:%d/%m/%Y}", r.supplier.name, r.get_method_display(), r.amount, r.wht_amount,
                   r.net_paid] for r in live],
            totals=[_("الإجمالي"), "", "", "", totals["amount"], totals["wht_amount"], totals["net_paid"]],
        )  # fmt: skip
        return respond(request, table, "supplier-payments")
    ctx = {"rows": rows, "totals": totals, "start": start, "end": end, "presets": presets, "tab": "payments"}
    return render(request, "purchasing/payments.html", ctx)


@require_perm("supplier.pay")
def payment_new(request):
    org = request.organization
    services.ensure_defaults(org)
    treasury.ensure(org)
    suppliers_qs = Supplier.objects.for_org(org).filter(is_active=True)
    chosen = request.POST.get("supplier") or request.GET.get("supplier") or ""
    supplier = suppliers_qs.filter(pk=chosen).first() if chosen.isdigit() else None
    open_rows = reports.open_invoices(org, date_cls.max, supplier=supplier) if supplier else []
    places = list(Treasury.objects.for_org(org).filter(is_active=True).select_related("account"))
    codes = list(TaxCode.objects.for_org(org).filter(kind=TaxKind.WITHHOLDING, is_active=True))
    post, error = request.POST, ""
    if request.method == "POST" and post.get("go") == "1":
        allocations = [
            (row.invoice, post.get(f"alloc_{row.invoice.pk}", ""))
            for row in open_rows
            if post.get(f"alloc_{row.invoice.pk}", "").strip()
        ]  # fmt: skip
        try:
            payment = services.pay_supplier(
                org, supplier=supplier, amount=post.get("amount"), method=post.get("method", PayMethod.CASH),
                treasury_obj=next((p for p in places if str(p.pk) == post.get("treasury")), None), by=request.user,
                date=_parse_date(post.get("date"), ledger.today()), allocations=allocations,
                wht_code=next((c for c in codes if str(c.pk) == post.get("wht_code")), None),
                wht_base=post.get("wht_base"), cheque_number=post.get("cheque_number", ""),
                cheque_due=_parse_date(post.get("cheque_due"), None), notes=post.get("notes", ""),
            )  # fmt: skip
        except services.PurchaseError as e:
            error = str(e)
        else:
            audit.log("supplier.paid", request=request, target=payment, summary=f"{payment.label}: {payment.amount}")
            messages.success(request, _("%(p)s اتسجل واترحّل.") % {"p": payment.label})
            return redirect("purchasing:payment", pk=payment.pk)
    for row in open_rows:
        row.typed = post.get(f"alloc_{row.invoice.pk}", "")
    ctx = {
        "suppliers": suppliers_qs, "supplier": supplier, "open_rows": open_rows, "places": places, "codes": codes,
        "methods": PayMethod.choices, "error": error, "post": post, "today": ledger.today(),
        "owed": services.supplier_balance(supplier) if supplier else None,
        "threshold": PurchaseSettings.for_org(org).wht_threshold, "tab": "payments",
    }  # fmt: skip
    return render(request, "purchasing/payment_new.html", ctx)


def _payment(request, pk):
    return get_object_or_404(
        SupplierPayment.objects.for_org(request.organization).select_related(
            "supplier", "fiscal_year", "treasury", "cheque", "wht_code", "created_by"
        ),
        pk=pk,
    )


@require_perm("purchase.view")
def payment_detail(request, pk):
    payment = _payment(request, pk)
    ctx = {
        "payment": payment,
        "allocations": payment.allocations.select_related("invoice__fiscal_year"),
        "entry": ledger.entry_for(payment) if has_perm(request.membership, "ledger.view") else None,
        "tab": "payments",
    }
    return render(request, "purchasing/payment.html", ctx)


@require_POST
@require_perm("supplier.pay")
def payment_cancel(request, pk):
    payment = _payment(request, pk)
    try:
        services.cancel_payment(payment, by=request.user, reason=request.POST.get("reason", ""))
    except services.PurchaseError as e:
        messages.error(request, str(e))
    else:
        audit.log("supplier.payment_cancelled", request=request, target=payment, summary=payment.label)
        messages.success(request, _("%(p)s اتلغى، وقيده اتعكس.") % {"p": payment.label})
    return redirect("purchasing:payment", pk=payment.pk)


# --- taxes -------------------------------------------------------------------------------------------------------


@require_perm("purchase.view")
def taxes(request):
    """The VAT paid on purchases and what was withheld from suppliers, for the period (this quarter by default)."""
    org = request.organization
    services.ensure_defaults(org)
    q_start, q_end = reports.quarter_of(ledger.today())
    start = _parse_date(request.GET.get("from"), q_start)
    end = _parse_date(request.GET.get("to"), q_end)
    prev_start, prev_end = reports.quarter_of(date_cls.fromordinal(q_start.toordinal() - 1))
    presets = [
        {"label": _("ربع السنة ده"), "from": q_start, "to": q_end, "active": (start, end) == (q_start, q_end)},
        {"label": _("الربع اللي فات"), "from": prev_start, "to": prev_end,
         "active": (start, end) == (prev_start, prev_end)},
    ]  # fmt: skip
    vat = reports.vat_paid(org, start, end)
    withheld = list(reports.withheld(org, start, end))
    report = request.GET.get("report", "wht")
    if request.GET.get("format"):
        if report == "vat":
            table = Table(
                title=_("ضريبة القيمة المضافة على المشتريات"),
                subtitle=_period_label(start, end),
                headers=[_("الضريبة"), _("النسبة %"), _("قيمة المشتريات"), _("ضريبة تتخصم"), _("ضريبة على التكلفة")],
                rows=[[r.name, r.rate, r.net, r.recoverable, r.in_cost] for r in vat],
                totals=[_("الإجمالي"), None, sum((r.net for r in vat), ZERO),
                        sum((r.recoverable for r in vat), ZERO), sum((r.in_cost for r in vat), ZERO)],
            )  # fmt: skip
            return respond(request, table, "vat-on-purchases")
        table = Table(
            title=_("الخصم تحت حساب الضريبة (نموذج 41)"),
            subtitle=_period_label(start, end),
            headers=[_("التاريخ"), _("المورد"), _("رقم التسجيل الضريبي"), _("رقم الملف"), _("السداد"),
                     _("القيمة الخاضعة"), _("النسبة %"), _("المخصوم")],
            rows=[[f"{p.date:%d/%m/%Y}", p.supplier.name, p.supplier.tax_registration_number,
                   p.supplier.tax_file_number, p.label, p.wht_base, p.wht_rate, p.wht_amount] for p in withheld],
            totals=[_("الإجمالي"), "", "", "", "", sum((p.wht_base for p in withheld), ZERO), None,
                    sum((p.wht_amount for p in withheld), ZERO)],
        )  # fmt: skip
        return respond(request, table, "withholding-41")
    ctx = {
        "vat": vat, "withheld": withheld, "start": start, "end": end, "presets": presets,
        "wht_total": sum((p.wht_amount for p in withheld), ZERO),
        "wht_due": ledger.balance_of(chart.account(org, "wht_payable")),
        "codes": TaxCode.objects.for_org(org), "tab": "taxes",
    }  # fmt: skip
    return render(request, "purchasing/taxes.html", ctx)


@require_perm("tax.manage")
def tax_edit(request, pk=None):
    org = request.organization
    code = get_object_or_404(TaxCode.objects.for_org(org), pk=pk) if pk else None
    form = TaxCodeForm(request.POST or None, instance=code)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        clash = TaxCode.objects.for_org(org).filter(name__iexact=obj.name).exclude(pk=obj.pk)
        if clash.exists():
            form.add_error("name", _("فيه ضريبة بنفس الاسم."))
        else:
            obj.organization = org
            if obj.kind != TaxKind.VAT:
                obj.recoverable = False
            obj._history_user = request.user
            obj.save()
            audit.log("tax.saved", request=request, target=obj, summary=f"{obj.name} {obj.rate}%")
            return redirect("purchasing:taxes")
    return render(request, "purchasing/tax_form.html", {"form": form, "code": code, "tab": "taxes"})


@require_perm("tax.manage")
def settings(request):
    org = request.organization
    form = PurchaseSettingsForm(request.POST or None, instance=PurchaseSettings.for_org(org))
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj._history_user = request.user
        obj.save()
        audit.log("purchase.settings", request=request, target=obj, summary="")
        messages.success(request, _("اتحفظ."))
        return redirect("purchasing:taxes")
    return render(request, "purchasing/settings.html", {"form": form, "tab": "taxes"})
