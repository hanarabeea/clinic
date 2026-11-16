"""Invoicing screens: the invoices, each patient's account, what the center owes its doctors and what contracted
companies owe the center. Thin views: every document is made by invoicing.services / billing.services."""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import has_perm, require_perm
from apps.audit import services as audit
from apps.billing import services as billing
from apps.billing.models import Payment, PaymentKind, PaymentMethod
from apps.doctors.models import Doctor
from apps.ledger import chart, reports
from apps.ledger import services as ledger
from apps.ledger.export import Table, respond
from apps.ledger.money import ZERO
from apps.ledger.views import _period_label, _range
from apps.patients.models import Patient
from apps.theatre.models import Admission, Operation
from apps.treasury import services as treasury
from apps.treasury.models import Treasury

from . import services
from .forms import InvoiceSettingsForm, PayerForm
from .models import Charge, ChargeKind, Invoice, InvoiceKind, InvoiceSettings, PatientCoverage, Payer


def _patient(request, pk):
    return get_object_or_404(Patient.objects.for_org(request.organization), pk=pk)


def _invoice(request, pk):
    return get_object_or_404(
        Invoice.objects.for_org(request.organization).select_related(
            "patient", "payer", "fiscal_year", "credit_of__fiscal_year", "issued_by", "appointment"
        ),
        pk=pk,
    )


# --- the invoices ------------------------------------------------------------------------------------------------


@require_perm("invoice.view")
def invoice_list(request):
    org = request.organization
    start, end, presets = _range(request)
    rows = (
        Invoice.objects.for_org(org)
        .filter(date__gte=start, date__lte=end)
        .select_related("patient", "payer", "fiscal_year", "credit_of__fiscal_year")
    )
    q = (request.GET.get("q") or "").strip()
    if q:
        rows = rows.filter(patient__in=Patient.objects.for_org(org).search(q))
    rows = list(rows[:500])
    total = sum((-r.total if r.is_credit else r.total for r in rows), ZERO)
    if request.GET.get("format"):
        table = Table(
            title=_("الفواتير"),
            subtitle=_period_label(start, end),
            headers=[_("الرقم"), _("التاريخ"), _("المريضة"), _("الإجمالي"), _("على جهة التعاقد"), _("على المريضة")],
            rows=[
                [r.label, f"{r.date:%d/%m/%Y}", r.patient.full_name, *(-v if r.is_credit else v for v in
                                                                         (r.total, r.payer_share, r.patient_share))]
                for r in rows
            ],
            totals=[_("الإجمالي"), "", "", total, None, None],
        )  # fmt: skip
        return respond(request, table, "invoices")
    ctx = {"rows": rows, "total": total, "start": start, "end": end, "presets": presets, "q": q, "tab": "invoices"}
    return render(request, "invoicing/list.html", ctx)


@require_perm("invoice.view")
def invoice_detail(request, pk):
    invoice = _invoice(request, pk)
    lines = list(
        invoice.lines.select_related("doctor", "reversed_by__invoice__fiscal_year").prefetch_related("shares__doctor")
    )
    state = None
    if not invoice.is_credit:
        states, _left = services.account_state(invoice.patient)
        state = next((s for s in states if s.invoice.pk == invoice.pk), None)
    ctx = {
        "invoice": invoice,
        "lines": lines,
        "state": state,
        "credit_notes": invoice.credit_notes.select_related("fiscal_year") if not invoice.is_credit else [],
        "can_credit": (
            not invoice.is_credit and not invoice.appointment_id and any(line.is_live for line in lines)
            and has_perm(request.membership, "invoice.refund")
        ),
        "show_shares": has_perm(request.membership, "doctor.payout"),
        "entry": ledger.entry_for(invoice) if has_perm(request.membership, "ledger.view") else None,
        "tab": "invoices",
    }  # fmt: skip
    return render(request, "invoicing/invoice.html", ctx)


def _pdf(request, template, ctx, filename):
    from apps.documents.pdf import font_css, logo_uri, render_pdf

    org_settings = getattr(request.organization, "settings", None)
    ctx = {
        **ctx, "org": request.organization, "settings": org_settings, "font_css": font_css(),
        "logo_uri": logo_uri(org_settings), "inv_settings": InvoiceSettings.for_org(request.organization),
    }  # fmt: skip
    html = render_to_string(template, ctx)
    resp = HttpResponse(render_pdf(html, format="A4", print_background=True), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{filename}.pdf"'
    return resp


@require_perm("invoice.view")
def invoice_pdf(request, pk):
    invoice = _invoice(request, pk)
    state = None
    if not invoice.is_credit:
        states, _left = services.account_state(invoice.patient)
        state = next((s for s in states if s.invoice.pk == invoice.pk), None)
    ctx = {"invoice": invoice, "lines": list(invoice.lines.all()), "state": state}
    return _pdf(request, "pdf/invoice.html", ctx, invoice.label)


@require_perm("invoice.view")
def receipt_pdf(request, pk):
    payment = get_object_or_404(
        Payment.objects.for_org(request.organization).select_related(
            "patient", "received_by", "appointment__visit_type"
        ),
        pk=pk,
    )
    return _pdf(request, "pdf/receipt.html", {"payment": payment}, f"receipt-{payment.pk}")


@require_POST
@require_perm("invoice.refund")
def invoice_credit(request, pk):
    invoice = _invoice(request, pk)
    line_ids = [int(v) for v in request.POST.getlist("line") if v.isdigit()]
    try:
        note = services.credit_invoice(
            invoice, line_ids=line_ids or None, reason=request.POST.get("reason", ""), by=request.user
        )
    except services.InvoiceError as e:
        messages.error(request, str(e))
        return redirect("invoicing:invoice", pk=invoice.pk)
    audit.log("invoice.credited", request=request, target=invoice, summary=f"{note.label}: {note.total}")
    messages.success(request, _("اتعمل %(n)s. البنود رجعت لحساب المريضة.") % {"n": note.label})
    return redirect("invoicing:invoice", pk=note.pk)


# --- patients' accounts ------------------------------------------------------------------------------------------


@require_perm("invoice.view")
def accounts(request):
    """Who has something open: a balance she owes, money of hers the center holds, or work not invoiced yet."""
    org = request.organization
    owed = {k: v for k, v in services.party_balances(org, "patients_receivable", "patient").items() if v}
    deposits = {k: v for k, v in services.party_balances(org, "patient_deposits", "patient").items() if v}
    waiting = set(
        Operation.objects.for_org(org)
        .exclude(status__in=services.OPERATION_OFF)
        .exclude(invoice_lines__is_live=True)
        .filter(price__gt=0)
        .values_list("patient_id", flat=True)
    )
    waiting |= set(
        Admission.objects.for_org(org)
        .exclude(status="cancelled")
        .exclude(invoice_lines__is_live=True)
        .filter(nightly_rate__gt=0)
        .values_list("patient_id", flat=True)
    )
    waiting |= set(
        Charge.objects.for_org(org)
        .filter(is_cancelled=False)
        .exclude(invoice_lines__is_live=True)
        .values_list("patient_id", flat=True)
    )
    ids = set(owed) | set(deposits) | waiting
    patients = list(Patient.objects.for_org(org).filter(pk__in=ids).order_by("full_name"))
    for p in patients:
        p.owed, p.deposit, p.waiting = owed.get(p.pk, ZERO), deposits.get(p.pk, ZERO), p.pk in waiting
    patients.sort(key=lambda p: (not p.waiting, -p.owed))
    ctx = {
        "patients": patients,
        "total_owed": sum((p.owed for p in patients if p.owed > 0), ZERO),
        "total_deposits": sum((p.deposit for p in patients), ZERO),
        "tab": "accounts",
    }
    return render(request, "invoicing/accounts.html", ctx)


def _account_ctx(request, patient):
    org = request.organization
    items = services.open_items(patient)
    states, _left = services.account_state(patient)
    coverage = PatientCoverage.objects.filter(patient=patient, is_active=True).select_related("payer").first()
    ready_total = sum((i.amount for i in items if i.ready), ZERO)
    return {
        "patient": patient,
        "items": items,
        "ready_total": ready_total,
        "any_ready": any(i.ready for i in items),
        "states": list(reversed(states)),
        "credit_notes": Invoice.objects.for_org(org)
        .filter(patient=patient, kind=InvoiceKind.CREDIT)
        .select_related("fiscal_year", "credit_of__fiscal_year"),
        "payments": Payment.objects.for_org(org)
        .filter(patient=patient)
        .select_related("appointment__visit_type", "received_by")
        .order_by("-received_at", "-id")[:40],
        "owed": services.patient_balance(patient),
        "deposit": services.deposit_balance(patient),
        "coverage": coverage,
        "payers": Payer.objects.for_org(org).filter(is_active=True),
        "methods": PaymentMethod.choices,
        "charge_kinds": ChargeKind.choices,
        "doctors": Doctor.objects.for_org(org).filter(is_active=True),
        "limit": InvoiceSettings.for_org(org).discount_limit_percent,
        "tab": "accounts",
    }


@require_perm("invoice.view")
def account(request, pk):
    return render(request, "invoicing/account.html", _account_ctx(request, _patient(request, pk)))


@require_POST
@require_perm("invoice.issue")
def account_issue(request, pk):
    patient = _patient(request, pk)
    payer, percent = None, None
    if request.POST.get("payer"):
        payer = get_object_or_404(Payer.objects.for_org(request.organization), pk=request.POST["payer"])
        percent = request.POST.get("percent") or payer.default_percent
    elif "payer" in request.POST:
        percent = 0  # "on the patient herself" chosen on the form: no payer even if she has a coverage
    try:
        invoice = services.issue_account_invoice(
            patient, keys=request.POST.getlist("item"), by=request.user, membership=request.membership,
            discount=request.POST.get("discount") or 0, reason=request.POST.get("reason", ""), payer=payer,
            percent=percent,
        )  # fmt: skip
    except services.InvoiceError as e:
        messages.error(request, str(e))
        return redirect("invoicing:account", pk=patient.pk)
    audit.log("invoice.issued", request=request, target=invoice, summary=f"{invoice.label}: {invoice.total}")
    messages.success(request, _("الفاتورة %(n)s اتعملت واترحّلت.") % {"n": invoice.label})
    return redirect("invoicing:invoice", pk=invoice.pk)


MOVES = {  # the form's "what is this money": (kind, is_refund)
    "pay": (PaymentKind.PAYMENT, False),
    "deposit": (PaymentKind.DEPOSIT, False),
    "refund": (PaymentKind.PAYMENT, True),
    "deposit_refund": (PaymentKind.DEPOSIT, True),
}


@require_POST
@require_perm("payment.record")
def account_pay(request, pk):
    patient = _patient(request, pk)
    move = request.POST.get("move", "pay")
    if move not in MOVES:
        return HttpResponse(status=400)
    kind, is_refund = MOVES[move]
    method = request.POST.get("method", PaymentMethod.CASH)
    if method not in PaymentMethod.values:
        return HttpResponse(status=400)
    try:
        payment = billing.record_account_payment(
            patient, amount=request.POST.get("amount") or 0, method=method, note=request.POST.get("note", ""),
            is_refund=is_refund, kind=kind, by=request.user,
        )  # fmt: skip
    except billing.PaymentError as e:
        messages.error(request, str(e))
    else:
        audit.log(
            "payment.recorded", request=request, target=patient, summary=f"{payment.get_kind_display()} {payment}"
        )
        messages.success(request, _("اتسجل %(a)s") % {"a": payment})
    return redirect("invoicing:account", pk=patient.pk)


@require_POST
@require_perm("invoice.issue")
def charge_add(request, pk):
    patient = _patient(request, pk)
    org = request.organization
    doctor = None
    if request.POST.get("doctor"):
        doctor = get_object_or_404(Doctor.objects.for_org(org), pk=request.POST["doctor"])
    try:
        charge = services.add_charge(
            patient, description=request.POST.get("description", ""), unit_price=request.POST.get("unit_price") or 0,
            quantity=request.POST.get("quantity") or 1, kind=request.POST.get("kind", ChargeKind.SERVICE),
            doctor=doctor, share_percent=request.POST.get("share_percent") or 0, by=request.user,
        )  # fmt: skip
    except services.InvoiceError as e:
        messages.error(request, str(e))
    else:
        audit.log("invoice.charge_added", request=request, target=patient, summary=str(charge))
        messages.success(request, _("البند اتضاف على الحساب."))
    return redirect("invoicing:account", pk=patient.pk)


@require_POST
@require_perm("invoice.issue")
def charge_cancel(request, pk):
    charge = get_object_or_404(Charge.objects.for_org(request.organization), pk=pk)
    try:
        services.cancel_charge(charge, by=request.user)
    except services.InvoiceError as e:
        messages.error(request, str(e))
    else:
        audit.log("invoice.charge_cancelled", request=request, target=charge.patient, summary=str(charge))
    return redirect("invoicing:account", pk=charge.patient_id)


@require_POST
@require_perm("invoice.issue")
def coverage(request, pk):
    patient = _patient(request, pk)
    payer = None
    if request.POST.get("payer"):
        payer = get_object_or_404(Payer.objects.for_org(request.organization), pk=request.POST["payer"])
    try:
        services.set_coverage(
            patient, payer=payer, percent=request.POST.get("percent") or (payer.default_percent if payer else 0),
            member_no=request.POST.get("member_no", ""), by=request.user,
        )  # fmt: skip
    except services.InvoiceError as e:
        messages.error(request, str(e))
    else:
        audit.log("invoice.coverage", request=request, target=patient, summary=str(payer or "none"))
        messages.success(request, _("اتحفظ.") if payer else _("المريضة بقت من غير جهة تعاقد."))
    return redirect("invoicing:account", pk=patient.pk)


# --- doctors: what the center owes them ------------------------------------------------------------------------------


def _cash_places(org):
    treasury.ensure(org)
    return Treasury.objects.for_org(org).filter(is_active=True).select_related("account")


@require_perm("doctor.payout")
def doctors(request):
    org = request.organization
    balances = services.party_balances(org, "doctor_payables", "doctor")
    rows = list(Doctor.objects.for_org(org).order_by("-is_active", "id"))
    for d in rows:
        d.owed = balances.get(d.pk, ZERO)
    outside = ledger.balance_of(chart.account(org, "external_fees_payable"))
    ctx = {"doctors": rows, "total": sum((d.owed for d in rows), ZERO), "outside": outside, "tab": "doctors"}
    return render(request, "invoicing/doctors.html", ctx)


def _statement(request, account_key, party, title, filename):
    org = request.organization
    start, end, presets = _range(request)
    statement = reports.account_statement(org, chart.account(org, account_key), start, end, party=party)
    if request.GET.get("format"):
        table = Table(
            title=title,
            subtitle=_period_label(start, end),
            headers=[_("التاريخ"), _("القيد"), _("البيان"), _("مدين"), _("دائن"), _("الرصيد")],
            rows=[[_("رصيد أول المدة"), "", "", None, None, statement.opening]]
            + [[f"{r.line.entry.date:%d/%m/%Y}", r.line.entry.number, r.line.memo or r.line.entry.memo,
                r.line.debit or None, r.line.credit or None, r.balance] for r in statement.lines],
            totals=[_("الإجمالي"), "", "", statement.debit, statement.credit, statement.closing],
        )  # fmt: skip
        return None, respond(request, table, filename)
    return {"st": statement, "start": start, "end": end, "presets": presets}, None


@require_perm("doctor.payout")
def doctor(request, pk):
    org = request.organization
    doc = get_object_or_404(Doctor.objects.for_org(org), pk=pk)
    ctx, export = _statement(request, "doctor_payables", doc, _("كشف حساب %(d)s") % {"d": doc}, f"doctor-{doc.pk}")
    if export is not None:
        return export
    ctx.update({"doctor": doc, "owed": services.doctor_balance(doc), "treasuries": _cash_places(org),
                "today": ledger.today(), "tab": "doctors"})  # fmt: skip
    return render(request, "invoicing/doctor.html", ctx)


def _amount(request):
    try:
        return Decimal(str(request.POST.get("amount") or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)


@require_POST
@require_perm("doctor.payout")
def doctor_pay(request, pk):
    org = request.organization
    doc = get_object_or_404(Doctor.objects.for_org(org), pk=pk)
    place = get_object_or_404(_cash_places(org), pk=request.POST.get("treasury"))
    try:
        voucher = services.pay_doctor(
            doc, amount=_amount(request), treasury_obj=place, by=request.user, note=request.POST.get("note", "")
        )
    except (services.InvoiceError, ValueError, TypeError) as e:
        messages.error(request, str(e) or _("اكتب المبلغ بالأرقام."))
    else:
        audit.log("doctor.paid", request=request, target=doc, summary=f"{voucher}: {voucher.amount}")
        messages.success(request, _("اتصرف %(a)s بـ%(v)s.") % {"a": voucher.amount, "v": voucher})
    return redirect("invoicing:doctor", pk=doc.pk)


# --- payers: contracted companies and insurers ------------------------------------------------------------------------


@require_perm("payer.manage")
def payers(request):
    org = request.organization
    balances = services.party_balances(org, "payers_receivable", "payer")
    rows = list(Payer.objects.for_org(org).order_by("-is_active", "name"))
    for p in rows:
        p.owed = balances.get(p.pk, ZERO)
    ctx = {"payers": rows, "total": sum((p.owed for p in rows), ZERO), "tab": "payers"}
    return render(request, "invoicing/payers.html", ctx)


@require_perm("payer.manage")
def payer_edit(request, pk=None):
    org = request.organization
    payer = get_object_or_404(Payer.objects.for_org(org), pk=pk) if pk else None
    form = PayerForm(request.POST or None, instance=payer)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            saved = services.save_payer(org, payer=payer, by=request.user, **d)
        except services.InvoiceError as e:
            form.add_error("name", str(e))
        else:
            audit.log("payer.saved", request=request, target=saved, summary=saved.name)
            return redirect("invoicing:payer", pk=saved.pk)
    return render(request, "invoicing/payer_form.html", {"form": form, "payer": payer, "tab": "payers"})


@require_perm("payer.manage")
def payer(request, pk):
    org = request.organization
    obj = get_object_or_404(Payer.objects.for_org(org), pk=pk)
    ctx, export = _statement(
        request, "payers_receivable", obj, _("كشف حساب %(p)s") % {"p": obj.name}, f"payer-{obj.pk}"
    )
    if export is not None:
        return export
    ctx.update({
        "payer": obj, "owed": services.payer_balance(obj), "treasuries": _cash_places(org),
        "covered": PatientCoverage.objects.for_org(org).filter(payer=obj, is_active=True).count(), "tab": "payers",
    })  # fmt: skip
    return render(request, "invoicing/payer.html", ctx)


@require_POST
@require_perm("payer.manage")
def payer_receive(request, pk):
    org = request.organization
    obj = get_object_or_404(Payer.objects.for_org(org), pk=pk)
    place = get_object_or_404(_cash_places(org), pk=request.POST.get("treasury"))
    try:
        voucher = services.receive_from_payer(
            obj, amount=_amount(request), treasury_obj=place, by=request.user, note=request.POST.get("note", "")
        )
    except (services.InvoiceError, ValueError, TypeError) as e:
        messages.error(request, str(e) or _("اكتب المبلغ بالأرقام."))
    else:
        audit.log("payer.received", request=request, target=obj, summary=f"{voucher}: {voucher.amount}")
        messages.success(request, _("اتحصّل %(a)s بـ%(v)s.") % {"a": voucher.amount, "v": voucher})
    return redirect("invoicing:payer", pk=obj.pk)


# --- settings (Settings → الفواتير) -----------------------------------------------------------------------------------


@require_perm("settings.manage")
def settings(request):
    org = request.organization
    instance = InvoiceSettings.for_org(org)
    form = InvoiceSettingsForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj._history_user = request.user
        obj.save()
        audit.log("invoice.settings", request=request, target=obj, summary="")
        messages.success(request, _("اتحفظ."))
        return redirect("invoicing:settings")
    return render(request, "invoicing/settings.html", {"form": form, "tab": "invoicing"})


# --- management reports (التقارير) -----------------------------------------------------------------------------------


@require_perm("report.finance")
def report_revenue(request):
    from . import reports as invoice_reports

    org = request.organization
    start, end, presets = _range(request)
    doctors_rows = invoice_reports.by_doctor(org, start, end)
    items = invoice_reports.by_item(org, start, end)
    total = sum((r.revenue for r in items), ZERO)
    if request.GET.get("format"):
        table = Table(
            title=_("الإيراد حسب الخدمة"),
            subtitle=_period_label(start, end),
            headers=[_("البند"), _("النوع"), _("العدد"), _("الإيراد")],
            rows=[[r.name, r.kind_label, r.count, r.revenue] for r in items],
            totals=[_("الإجمالي"), "", None, total],
        )
        return respond(request, table, "revenue")
    ctx = {"doctors": doctors_rows, "items": items, "total": total, "start": start, "end": end, "presets": presets,
           "tab": "revenue"}  # fmt: skip
    return render(request, "invoicing/report_revenue.html", ctx)


@require_perm("report.finance")
def report_operations(request):
    from . import reports as invoice_reports

    org = request.organization
    start, end, presets = _range(request)
    rows = invoice_reports.operations_profit(org, start, end)
    totals = {k: sum((getattr(r, k) for r in rows), ZERO) for k in ("price", "team", "supplies", "left")}
    if request.GET.get("format"):
        table = Table(
            title=_("ربحية العمليات"),
            subtitle=_period_label(start, end),
            headers=[_("التاريخ"), _("العملية"), _("المريضة"), _("السعر"), _("بنود إضافية"), _("أتعاب الفريق"),
                     _("المستلزمات"), _("الباقي")],
            rows=[[f"{r.invoice.date:%d/%m/%Y}", r.operation.procedure_name, r.operation.patient.full_name, r.price,
                   r.extra, r.team, r.supplies, r.left] for r in rows],
            totals=[_("الإجمالي"), "", "", totals["price"], None, totals["team"], totals["supplies"], totals["left"]],
        )  # fmt: skip
        return respond(request, table, "operations-profit")
    ctx = {"rows": rows, "totals": totals, "start": start, "end": end, "presets": presets, "tab": "operations"}
    return render(request, "invoicing/report_operations.html", ctx)


@require_perm("report.finance")
def report_receivables(request):
    from . import reports as invoice_reports

    org = request.organization
    as_of = ledger.today()
    rows = invoice_reports.receivables_aging(org, as_of)
    labels = [label for _limit, label in invoice_reports.BUCKETS]
    totals = [sum((r.buckets[i] for r in rows), ZERO) for i in range(len(labels))]
    if request.GET.get("format"):
        table = Table(
            title=_("مديونيات المرضى"),
            subtitle=f"{as_of:%d/%m/%Y}",
            headers=[_("المريضة"), *labels, _("الإجمالي")],
            rows=[[r.patient.full_name, *r.buckets, r.total] for r in rows],
            totals=[_("الإجمالي"), *totals, sum(totals, ZERO)],
        )
        return respond(request, table, "patients-aging")
    balances = services.party_balances(org, "payers_receivable", "payer")
    payers_rows = [p for p in Payer.objects.for_org(org) if balances.get(p.pk)]
    for p in payers_rows:
        p.owed = balances[p.pk]
    ctx = {"rows": rows, "labels": labels, "totals": totals, "grand": sum(totals, ZERO), "as_of": as_of,
           "payers": payers_rows, "tab": "receivables"}  # fmt: skip
    return render(request, "invoicing/report_receivables.html", ctx)


@require_perm("report.finance")
def report_cash(request):
    from apps.ledger import statements

    org = request.organization
    treasury.ensure(org)
    start, end, presets = _range(request)
    rows = statements.cash_movement(org, start, end)
    totals = {k: sum((r[k] for r in rows), ZERO) for k in ("opening", "came", "went", "closing")}
    if request.GET.get("format"):
        table = Table(
            title=_("حركة النقدية"),
            subtitle=_period_label(start, end),
            headers=[_("الخزينة"), _("أول المدة"), _("داخل"), _("خارج"), _("آخر المدة")],
            rows=[[r["treasury"].name, r["opening"], r["came"], r["went"], r["closing"]] for r in rows],
            totals=[_("الإجمالي"), totals["opening"], totals["came"], totals["went"], totals["closing"]],
        )
        return respond(request, table, "cash-movement")
    ctx = {"rows": rows, "totals": totals, "start": start, "end": end, "presets": presets, "tab": "cash"}
    return render(request, "invoicing/report_cash.html", ctx)
