"""Treasury screens: the clinic's cash and bank places with their balances, vouchers, movements, counts. Thin views:
every movement goes through treasury.services, which posts it."""

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import has_perm, require_perm
from apps.audit import services as audit
from apps.ledger import accounts as account_rules
from apps.ledger import chart, reports
from apps.ledger import services as ledger
from apps.ledger.export import Table, respond
from apps.ledger.models import Account, CostCenter
from apps.ledger.views import _parse_date, _period_label, _range

from . import cheques, services
from .forms import GROUP_CODE, CountForm, ExpenseTemplateForm, TreasuryForm, VoucherForm
from .models import (
    CHEQUE_OPEN,
    CashCount,
    Cheque,
    ChequeDirection,
    ChequeStatus,
    ExpenseTemplate,
    Shift,
    ShiftStatus,
    Treasury,
    TreasuryKind,
    VarianceStatus,
    Voucher,
    VoucherKind,
)


def _cheque_alert_days(org) -> int:
    from apps.purchasing.models import PurchaseSettings

    return PurchaseSettings.for_org(org).cheque_alert_days


def _treasury(request, pk):
    return get_object_or_404(Treasury.objects.for_org(request.organization).select_related("account"), pk=pk)


@require_perm("treasury.view")
def home(request):
    """Where the money is: each treasury with what the books say is in it, the open shifts, the cash differences
    waiting for approval and the latest vouchers."""
    org = request.organization
    treasuries = services.all_treasuries(org)
    open_shifts = {s.treasury_id: s for s in Shift.objects.for_org(org).filter(status=ShiftStatus.OPEN)}
    for t in treasuries:
        t.shift = open_shifts.get(t.pk)
    pending = list(
        CashCount.objects.for_org(org)
        .filter(variance_status=VarianceStatus.PENDING)
        .select_related("treasury", "counted_by")
    )
    for count in pending:
        count.can_approve = has_perm(request.membership, "treasury.count") and services.can_approve(
            count, request.membership
        )
    ctx = {
        "treasuries": treasuries,
        "total_cash": sum((t.balance for t in treasuries if t.holds_cash), 0),
        "total_other": sum((t.balance for t in treasuries if not t.holds_cash), 0),
        "pending": pending,
        "vouchers": Voucher.objects.for_org(org).select_related("treasury", "to_treasury", "counter_account")[:8],
        "soon": cheques.due_soon(org, _cheque_alert_days(org)),
        "tab": "home",
    }
    return render(request, "treasury/home.html", ctx)


# --- vouchers --------------------------------------------------------------------------------------------------


@require_perm("treasury.view")
def vouchers(request):
    org = request.organization
    start, end, presets = _range(request)
    kind = request.GET.get("kind", "")
    rows = Voucher.objects.for_org(org).filter(date__gte=start, date__lte=end)
    if kind in VoucherKind.values:
        rows = rows.filter(kind=kind)
    rows = rows.select_related("treasury", "to_treasury", "counter_account", "created_by")
    if request.GET.get("format"):
        table = Table(
            title=_("السندات"),
            subtitle=_period_label(start, end),
            headers=[
                _("النوع"),
                _("الرقم"),
                _("التاريخ"),
                _("الخزينة"),
                _("المقابل"),
                _("البيان"),
                _("المبلغ"),
                _("الحالة"),
            ],
            rows=[
                [
                    v.get_kind_display(),
                    v.number,
                    f"{v.date:%d/%m/%Y}",
                    v.treasury.name,
                    str(v.to_treasury or v.counter_account),
                    v.description,
                    v.amount,
                    v.get_status_display(),
                ]
                for v in rows
            ],
        )
        return respond(request, table, f"vouchers-{start}-{end}")
    ctx = {"vouchers": rows[:300], "start": start, "end": end, "presets": presets, "kind": kind,
           "kinds": VoucherKind.choices, "keep": f"&kind={kind}" if kind in VoucherKind.values else "",
           "tab": "vouchers"}  # fmt: skip
    return render(request, "treasury/vouchers.html", ctx)


@require_perm("treasury.operate")
def voucher_new(request):
    org = request.organization
    services.ensure(org)
    kind = request.POST.get("kind") or request.GET.get("kind") or VoucherKind.PAYMENT
    kind = kind if kind in VoucherKind.values else VoucherKind.PAYMENT
    initial = {"kind": kind, "date": ledger.today(), "treasury": request.GET.get("treasury")}
    form = VoucherForm(request.POST or None, org=org, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            voucher = services.create_voucher(
                org, kind=d["kind"], treasury=d["treasury"], amount=d["amount"], date=d["date"],
                description=d["description"], counter_account=d["counter_account"], to_treasury=d["to_treasury"],
                cost_center=d["cost_center"], party_name=d["party_name"], by=request.user,
            )  # fmt: skip
        except services.TreasuryError as e:
            form.add_error(None, str(e))
        else:
            audit.log("treasury.voucher", request=request, target=voucher, summary=f"{voucher}: {voucher.amount}")
            messages.success(request, _("%(v)s اتسجل واترحّل.") % {"v": voucher})
            return redirect("treasury:voucher", pk=voucher.pk)
    ctx = {"form": form, "kind": kind, "kinds": VoucherKind.choices, "tab": "vouchers"}
    return render(request, "treasury/voucher_new.html", ctx)


def _voucher(request, pk):
    return get_object_or_404(
        Voucher.objects.for_org(request.organization).select_related(
            "treasury", "to_treasury", "counter_account", "cost_center", "created_by", "cancelled_by"
        ),
        pk=pk,
    )


@require_perm("treasury.view")
def voucher_detail(request, pk):
    voucher = _voucher(request, pk)
    ctx = {"voucher": voucher, "entry": ledger.entry_for(voucher), "reversal": ledger.entry_for(voucher, "cancel"),
           "tab": "vouchers"}  # fmt: skip
    return render(request, "treasury/voucher.html", ctx)


@require_POST
@require_perm("treasury.operate")
def voucher_cancel(request, pk):
    voucher = _voucher(request, pk)
    try:
        services.cancel_voucher(voucher, by=request.user, reason=request.POST.get("reason", ""))
    except services.TreasuryError as e:
        messages.error(request, str(e))
    else:
        audit.log("treasury.voucher_cancelled", request=request, target=voucher, summary=str(voucher))
        messages.success(request, _("%(v)s اتلغى، وقيده اتعكس.") % {"v": voucher})
    return redirect("treasury:voucher", pk=voucher.pk)


# --- one treasury ----------------------------------------------------------------------------------------------


@require_perm("treasury.view")
def movement(request, pk):
    """حركة الخزينة: every posted line of the treasury's account in the period, with the running balance."""
    treasury = _treasury(request, pk)
    start, end, presets = _range(request)
    st = reports.account_statement(request.organization, treasury.account, start, end)
    if request.GET.get("format"):
        table = Table(
            title=f"{_('حركة')} {treasury.name}",
            subtitle=_period_label(start, end),
            headers=[_("التاريخ"), _("رقم القيد"), _("البيان"), _("داخل"), _("خارج"), _("الرصيد")],
            rows=[["", "", _("رصيد أول المدة"), None, None, st.opening]]
            + [
                [
                    f"{row.line.entry.date:%d/%m/%Y}",
                    row.line.entry.number,
                    row.line.entry.memo,
                    row.line.debit or None,
                    row.line.credit or None,
                    row.balance,
                ]
                for row in st.lines
            ],
            totals=["", "", _("الإجمالي"), st.debit, st.credit, st.closing],
        )
        return respond(request, table, f"treasury-{treasury.pk}-{start}-{end}")
    ctx = {"treasury": treasury, "st": st, "start": start, "end": end, "presets": presets, "keep": "",
           "tab": "home"}  # fmt: skip
    return render(request, "treasury/movement.html", ctx)


@require_perm("accounts.manage")
def treasury_edit(request, pk=None):
    """Add a treasury (a drawer, a safe, a bank account, an electronic collection account) with its own account in
    the chart, or rename one / change which way of paying lands in it / switch it off."""
    org = request.organization
    services.ensure(org)
    treasury = _treasury(request, pk) if pk else None
    form = TreasuryForm(request.POST or None, instance=treasury, org=org)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                obj = form.save(commit=False)
                if treasury is None:
                    group = Account.objects.get(organization=org, code=GROUP_CODE[obj.kind])
                    obj.organization = org
                    obj.account = account_rules.add_account(org, parent=group, name_ar=obj.name, by=request.user)
                elif not obj.is_active:
                    if services.balance(obj) != 0 or services.current_shift(obj) is not None:
                        raise services.TreasuryError(_("الخزينة فيها رصيد أو وردية مفتوحة. فضّيها واقفل الوردية الأول."))
                    if obj.system_key in ("main_safe", "reception_drawer"):
                        raise services.TreasuryError(_("الخزينة دي أساسية، مينفعش تتوقف."))
                obj.save()
                if treasury is not None and obj.account.name_ar != obj.name:
                    account_rules.rename(obj.account, name_ar=obj.name, name_en=obj.account.name_en, by=request.user)
        except (services.TreasuryError, ledger.LedgerError) as e:
            form.add_error(None, str(e))
        else:
            audit.log("treasury.changed", request=request, target=obj, summary=obj.name)
            messages.success(request, _("%(t)s اتحفظت.") % {"t": obj.name})
            return redirect("treasury:home")
    return render(request, "treasury/treasury_form.html", {"form": form, "treasury": treasury, "tab": "home"})


# --- counts ----------------------------------------------------------------------------------------------------


@require_perm("treasury.view")
def counts(request):
    rows = (
        CashCount.objects.for_org(request.organization)
        .select_related("treasury", "counted_by", "approved_by")
        .order_by("-counted_at")[:200]
    )
    rows = list(rows)
    for count in rows:
        count.can_approve = (
            count.variance_status == VarianceStatus.PENDING
            and has_perm(request.membership, "treasury.count")
            and services.can_approve(count, request.membership)
        )
    return render(request, "treasury/counts.html", {"counts": rows, "tab": "counts"})


@require_perm("treasury.count")
def surprise_count(request, pk):
    """جرد مفاجئ: count a treasury now, without closing anything."""
    treasury = _treasury(request, pk)
    form = CountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            count = services.surprise_count(
                treasury, denominations=form.denominations(), by=request.user, witness_name=d["witness_name"],
                notes=d["notes"],
            )  # fmt: skip
        except services.TreasuryError as e:
            form.add_error(None, str(e))
        else:
            audit.log("treasury.surprise_count", request=request, target=count,
                      summary=f"{treasury.name}: {count.difference}")  # fmt: skip
            if count.difference == 0:
                messages.success(request, _("الجرد مطابق."))
            else:
                messages.warning(request, _("الجرد اتسجل، وفيه فرق %(d)s.") % {"d": count.difference})
            return redirect("treasury:counts")
    ctx = {"treasury": treasury, "form": form, "surprise": True, "back": reverse("treasury:home"),
           "tab": "counts"}  # fmt: skip
    return render(request, "treasury/count_form.html", ctx)


@require_POST
@require_perm("treasury.count")
def approve_variance(request, pk):
    count = get_object_or_404(CashCount.objects.for_org(request.organization), pk=pk)
    try:
        services.approve_variance(count, by=request.user, membership=request.membership)
    except services.TreasuryError as e:
        messages.error(request, str(e))
    else:
        audit.log("treasury.variance_approved", request=request, target=count, summary=str(count.difference))
        messages.success(request, _("الفرق اتعتمد واترحّل على عجز وزيادة الخزينة."))
    return redirect("treasury:counts")


# --- cheques ---------------------------------------------------------------------------------------------------


def _cheque(request, pk):
    return get_object_or_404(
        Cheque.objects.for_org(request.organization).select_related("bank", "counter_account", "created_by"), pk=pk
    )


@require_perm("treasury.view")
def cheque_list(request):
    """Post-dated cheques: the ones still on their way first, the nearest due date on top."""
    from apps.purchasing.models import PurchaseSettings

    org = request.organization
    direction = request.GET.get("direction", ChequeDirection.ISSUED)
    direction = direction if direction in ChequeDirection.values else ChequeDirection.ISSUED
    show_all = request.GET.get("all") == "1"
    rows = Cheque.objects.for_org(org).filter(direction=direction).select_related("bank")
    if not show_all:
        rows = rows.filter(status__in=CHEQUE_OPEN[direction])
    rows = list(rows.order_by("due_date", "id")[:500])
    today = ledger.today()
    for c in rows:
        c.late = c.is_open and c.due_date < today
    ctx = {
        "rows": rows, "direction": direction, "show_all": show_all, "directions": ChequeDirection.choices,
        "open_total": cheques.open_total(org, direction),
        "soon": cheques.due_soon(org, PurchaseSettings.for_org(org).cheque_alert_days), "tab": "cheques",
    }  # fmt: skip
    return render(request, "treasury/cheques.html", ctx)


@require_perm("cheque.manage")
def cheque_new(request):
    from apps.invoicing.models import Payer

    org = request.organization
    services.ensure(org)
    direction = request.POST.get("direction") or request.GET.get("direction") or ChequeDirection.RECEIVED
    direction = direction if direction in ChequeDirection.values else ChequeDirection.RECEIVED
    banks = list(Treasury.objects.for_org(org).filter(is_active=True, kind=TreasuryKind.BANK))
    treasury_accounts = Treasury.objects.for_org(org).values("account_id")
    accounts = list(
        Account.objects.for_org(org)
        .filter(is_group=False, is_active=True)
        .exclude(pk__in=treasury_accounts)
        .exclude(system_key__in=chart.CONTROL_KEYS)
        .order_by("code")
    )
    centers = list(CostCenter.objects.for_org(org).filter(is_active=True))
    payers = list(Payer.objects.for_org(org).filter(is_active=True))
    post, error = request.POST, ""
    if request.method == "POST":
        pick = lambda rows, key: next((r for r in rows if str(r.pk) == post.get(key)), None)  # noqa: E731
        common = {
            "number": post.get("number", ""), "due_date": _parse_date(post.get("due_date"), None),
            "amount": post.get("amount") or 0, "by": request.user, "counter_account": pick(accounts, "account"),
            "cost_center": pick(centers, "cost_center"), "party_name": post.get("party_name", ""),
            "notes": post.get("notes", ""),
        }  # fmt: skip
        try:
            if direction == ChequeDirection.RECEIVED:
                cheque = cheques.receive_cheque(
                    org, bank_name=post.get("bank_name", ""), payer=pick(payers, "payer"), **common
                )
            else:
                cheque = cheques.issue_cheque(org, bank=pick(banks, "bank"), **common)
        except services.TreasuryError as e:
            error = str(e)
        else:
            audit.log("treasury.cheque", request=request, target=cheque, summary=f"{cheque}: {cheque.amount}")
            messages.success(request, _("الشيك اتسجل."))
            return redirect("treasury:cheque", pk=cheque.pk)
    ctx = {"direction": direction, "directions": ChequeDirection.choices, "banks": banks, "accounts": accounts,
           "centers": centers, "payers": payers, "post": post, "error": error, "tab": "cheques"}  # fmt: skip
    return render(request, "treasury/cheque_new.html", ctx)


STEP_LABELS = {
    ChequeStatus.CLEARED: _("اتصرف من البنك"),
    ChequeStatus.RETURNED: _("رجع من غير صرف"),
    ChequeStatus.CANCELLED: _("إلغاء الشيك"),
    ChequeStatus.DEPOSITED: _("اتودع في البنك"),
    ChequeStatus.COLLECTED: _("اتحصّل"),
    ChequeStatus.BOUNCED: _("ارتد"),
    ChequeStatus.ENDORSED: _("تظهير لمورد"),
}


@require_perm("treasury.view")
def cheque_detail(request, pk):
    from apps.purchasing.models import Supplier

    org = request.organization
    cheque = _cheque(request, pk)
    steps = [(s, STEP_LABELS[s]) for s in ChequeStatus.values if s in cheques.ALLOWED.get(cheque.status, set())]
    ctx = {
        "cheque": cheque,
        "events": cheque.events.select_related("by", "entry"),
        "steps": steps,
        "can_undo": cheque.status in cheques.UNDOABLE,
        "banks": Treasury.objects.for_org(org).filter(is_active=True, kind=TreasuryKind.BANK),
        "suppliers": Supplier.objects.for_org(org).filter(is_active=True) if cheque.status == "in_hand" else [],
        "payment": getattr(cheque, "supplier_payment", None),
        "tab": "cheques",
    }
    return render(request, "treasury/cheque.html", ctx)


@require_POST
@require_perm("cheque.manage")
def cheque_move(request, pk):
    from apps.purchasing.models import Supplier
    from apps.purchasing.services import PurchaseError

    org = request.organization
    cheque = _cheque(request, pk)
    to_status = request.POST.get("to", "")
    bank = Treasury.objects.for_org(org).filter(pk=request.POST.get("bank") or 0).first()
    supplier = Supplier.objects.for_org(org).filter(pk=request.POST.get("supplier") or 0).first()
    try:
        cheques.move(cheque, to_status, by=request.user, bank=bank, supplier=supplier,
                     notes=request.POST.get("notes", ""))  # fmt: skip
    except (services.TreasuryError, PurchaseError) as e:
        messages.error(request, str(e))
    else:
        audit.log("treasury.cheque_step", request=request, target=cheque, summary=f"{cheque}: {to_status}")
        messages.success(request, _("اتسجل."))
    return redirect("treasury:cheque", pk=cheque.pk)


@require_POST
@require_perm("cheque.manage")
def cheque_undo(request, pk):
    cheque = _cheque(request, pk)
    try:
        cheques.undo(cheque, by=request.user, notes=request.POST.get("notes", ""))
    except services.TreasuryError as e:
        messages.error(request, str(e))
    else:
        audit.log("treasury.cheque_undo", request=request, target=cheque, summary=str(cheque))
        messages.success(request, _("رجعنا خطوة، وقيدها اتعكس."))
    return redirect("treasury:cheque", pk=cheque.pk)


# --- expenses that come back every month ------------------------------------------------------------------------


@require_perm("treasury.view")
def expenses(request):
    org = request.organization
    today = ledger.today()
    rows = services.expense_templates(org, today)
    ctx = {"rows": rows, "today": today, "tab": "expenses"}
    return render(request, "treasury/expenses.html", ctx)


@require_perm("treasury.operate")
def expense_edit(request, pk=None):
    org = request.organization
    services.ensure(org)
    template = get_object_or_404(ExpenseTemplate.objects.for_org(org), pk=pk) if pk else None
    form = ExpenseTemplateForm(request.POST or None, instance=template, org=org)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if ExpenseTemplate.objects.for_org(org).filter(name__iexact=obj.name).exclude(pk=obj.pk).exists():
            form.add_error("name", _("فيه مصروف متكرر بنفس الاسم."))
        else:
            obj.organization = org
            obj._history_user = request.user
            obj.save()
            audit.log("treasury.expense_template", request=request, target=obj, summary=obj.name)
            return redirect("treasury:expenses")
    return render(request, "treasury/expense_form.html", {"form": form, "template": template, "tab": "expenses"})


@require_POST
@require_perm("treasury.operate")
def expense_record(request, pk):
    template = get_object_or_404(ExpenseTemplate.objects.for_org(request.organization), pk=pk)
    try:
        voucher = services.record_expense(template, by=request.user, amount=request.POST.get("amount") or None)
    except services.TreasuryError as e:
        messages.error(request, str(e))
        return redirect("treasury:expenses")
    audit.log("treasury.voucher", request=request, target=voucher, summary=f"{voucher}: {voucher.amount}")
    messages.success(request, _("%(v)s اتسجل واترحّل.") % {"v": voucher})
    return redirect("treasury:voucher", pk=voucher.pk)
