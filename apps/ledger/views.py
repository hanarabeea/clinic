"""The accounting screens: trial balance, journal, chart of accounts, account statement, periods. Thin views: the
rules live in ledger.services / ledger.accounts, the numbers in ledger.reports."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import require_perm
from apps.audit import services as audit

from . import accounts as account_rules
from . import chart, closing, reports, services, statements, verify
from .export import Table, respond
from .models import Account, BooksCheck, CostCenter, FiscalPeriod, FiscalYear, JournalEntry, PeriodStatus
from .money import ZERO

# accounts that only their own documents move (each line there names a patient, a supplier, a cheque...): a manual
# entry on them would break the agreement between the account and its subledger
CONTROL_KEYS = chart.CONTROL_KEYS
MAX_LINES = 40


def _parse_date(raw, default):
    try:
        return date.fromisoformat(raw or "")
    except ValueError:
        return default


def _range(request):
    """The period a report covers: ?from=&to=, else this month so far."""
    today = services.today()
    start = _parse_date(request.GET.get("from"), today.replace(day=1))
    end = _parse_date(request.GET.get("to"), today)
    if end < start:
        start, end = end, start
    year_start = services.fiscal_year_for(request.organization, today).start_date
    last_month_end = today.replace(day=1) - timedelta(days=1)
    presets = [
        (_("الشهر ده"), today.replace(day=1), today),
        (_("الشهر اللي فات"), last_month_end.replace(day=1), last_month_end),
        (_("من أول السنة"), year_start, today),
    ]
    return (
        start,
        end,
        [{"label": label, "from": s, "to": e, "active": (s, e) == (start, end)} for label, s, e in presets],
    )


def _period_label(start, end) -> str:
    return f"{start:%d/%m/%Y} - {end:%d/%m/%Y}"


# --- trial balance ---------------------------------------------------------------------------------------------


@require_perm("ledger.view")
def trial_balance(request):
    org = request.organization
    chart.ensure(org)
    start, end, presets = _range(request)
    depth = request.GET.get("depth", "")
    depth = int(depth) if depth in ("1", "2", "3") else None
    show_zero = request.GET.get("zero") == "1"
    tb = reports.trial_balance(org, start, end, max_depth=depth, hide_zero=not show_zero)
    if request.GET.get("format"):
        t = tb.totals
        table = Table(
            title=_("ميزان المراجعة"),
            subtitle=_period_label(start, end),
            headers=[
                _("الكود"),
                _("الحساب"),
                _("رصيد أول مدين"),
                _("رصيد أول دائن"),
                _("حركة مدين"),
                _("حركة دائن"),
                _("رصيد آخر مدين"),
                _("رصيد آخر دائن"),
            ],
            rows=[
                [
                    r.account.code,
                    r.account.name_ar,
                    r.opening_debit,
                    r.opening_credit,
                    r.debit,
                    r.credit,
                    r.closing_debit,
                    r.closing_credit,
                ]
                for r in tb.rows
            ],
            totals=[
                "",
                _("الإجمالي"),
                t.opening_debit,
                t.opening_credit,
                t.debit,
                t.credit,
                t.closing_debit,
                t.closing_credit,
            ],
        )
        return respond(request, table, f"trial-balance-{start}-{end}")
    keep = (f"&depth={depth}" if depth else "") + ("&zero=1" if show_zero else "")
    ctx = {"tb": tb, "start": start, "end": end, "presets": presets, "depth": depth, "show_zero": show_zero,
           "keep": keep, "tab": "trial"}  # fmt: skip
    return render(request, "ledger/trial_balance.html", ctx)


# --- journal ---------------------------------------------------------------------------------------------------


@require_perm("ledger.view")
def journal(request):
    org = request.organization
    start, end, presets = _range(request)
    q = request.GET.get("q", "").strip()
    entries = reports.journal(org, start, end, q=q)
    if request.GET.get("format"):
        rows = []
        for e in entries:
            for line in e.lines.all():
                rows.append([e.number, f"{e.date:%d/%m/%Y}", e.memo, line.account.code, line.account.name_ar,
                             line.debit or None, line.credit or None])  # fmt: skip
        table = Table(
            title=_("دفتر اليومية"),
            subtitle=_period_label(start, end),
            headers=[_("رقم القيد"), _("التاريخ"), _("البيان"), _("الكود"), _("الحساب"), _("مدين"), _("دائن")],
            rows=rows,
        )
        return respond(request, table, f"journal-{start}-{end}")
    ctx = {"entries": entries[:300], "start": start, "end": end, "presets": presets, "q": q,
           "keep": f"&q={q}" if q else "", "tab": "journal"}  # fmt: skip
    return render(request, "ledger/journal.html", ctx)


def _entry(request, pk):
    return get_object_or_404(
        JournalEntry.objects.for_org(request.organization).select_related("created_by", "reverses", "period"), pk=pk
    )


@require_perm("ledger.view")
def entry_detail(request, pk):
    entry = _entry(request, pk)
    lines = entry.lines.select_related("account", "cost_center").order_by("line_no")
    reversal = JournalEntry.objects.filter(reverses=entry).first()
    ctx = {
        "entry": entry,
        "lines": lines,
        "reversal": reversal,
        "can_reverse": entry.is_posted and not entry.source_type and not entry.reverses_id and reversal is None,
        "tab": "journal",
    }
    return render(request, "ledger/entry.html", ctx)


def _amount(raw):
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return ZERO
    try:
        return Decimal(raw)
    except InvalidOperation as e:
        raise services.LedgerError(_("فيه مبلغ مش مكتوب صح: %(v)s") % {"v": raw}) from e


def _manual_lines(request, accounts_by_pk, centers_by_pk):
    """The rows of the manual-entry form → ledger lines (blank rows are skipped)."""
    post = request.POST
    lines = []
    rows = zip(post.getlist("account"), post.getlist("debit"), post.getlist("credit"), post.getlist("cost_center"),
               post.getlist("line_memo"), strict=False)  # fmt: skip
    for account_pk, debit, credit, center_pk, memo in list(rows)[:MAX_LINES]:
        debit, credit = _amount(debit), _amount(credit)
        if not account_pk and not debit and not credit:
            continue
        account = accounts_by_pk.get(account_pk)
        if account is None:
            raise services.LedgerError(_("اختار الحساب في كل سطر."))
        if account.system_key in CONTROL_KEYS:
            raise services.LedgerError(
                _("%(a)s بيتحرك من مستنداته بس (تحصيل، فاتورة، سند...)، مش بقيد يدوي.") % {"a": account}
            )
        lines.append(
            services.Line(
                account=account, debit=debit, credit=credit, cost_center=centers_by_pk.get(center_pk), memo=memo
            )
        )
    return lines


@require_perm("ledger.post")
def entry_new(request):
    """A manual journal entry (opening balances, corrections, accruals). Saved = posted."""
    org = request.organization
    chart.ensure(org)
    leaf_accounts = list(Account.objects.for_org(org).filter(is_group=False, is_active=True).order_by("code"))
    centers = list(CostCenter.objects.for_org(org).filter(is_active=True))
    error = ""
    if request.method == "POST":
        try:
            lines = _manual_lines(request, {str(a.pk): a for a in leaf_accounts}, {str(c.pk): c for c in centers})
            entry = services.post(
                org,
                date=_parse_date(request.POST.get("date"), services.today()),
                memo=request.POST.get("memo", "").strip() or _("قيد يدوي"),
                lines=lines,
                by=request.user,
            )
        except services.LedgerError as e:
            error = str(e)
        else:
            audit.log("ledger.entry_posted", request=request, target=entry, summary=f"{entry.number}: {entry.memo}")
            messages.success(request, _("القيد رقم %(n)s اترحّل.") % {"n": entry.number})
            return redirect("ledger:entry", pk=entry.pk)
    post = request.POST
    typed = list(zip(post.getlist("account"), post.getlist("debit"), post.getlist("credit"),
                     post.getlist("cost_center"), post.getlist("line_memo"), strict=False))  # fmt: skip
    rows = [{"account": a, "debit": d, "credit": c, "cost_center": cc, "memo": m} for a, d, c, cc, m in typed]
    while len(rows) < 4:  # a fresh form starts with a few empty lines
        rows.append({"account": "", "debit": "", "credit": "", "cost_center": "", "memo": ""})
    ctx = {
        "accounts": [a for a in leaf_accounts if a.system_key not in CONTROL_KEYS],
        "centers": centers,
        "error": error,
        "date": _parse_date(post.get("date"), services.today()),
        "memo": post.get("memo", ""),
        "rows": rows,
        "tab": "journal",
    }
    return render(request, "ledger/entry_new.html", ctx)


@require_POST
@require_perm("ledger.post")
def entry_reverse(request, pk):
    entry = _entry(request, pk)
    if entry.source_type:
        messages.error(request, _("القيد ده جاي من مستند. الغي المستند نفسه (السند، الدفعة...) والقيد هيتعكس معاه."))
        return redirect("ledger:entry", pk=entry.pk)
    try:
        mirror = services.reverse(entry, by=request.user, memo=request.POST.get("memo", "").strip())
    except services.LedgerError as e:
        messages.error(request, str(e))
        return redirect("ledger:entry", pk=entry.pk)
    audit.log("ledger.entry_reversed", request=request, target=entry, summary=f"{entry.number} -> {mirror.number}")
    messages.success(request, _("القيد اتعكس بالقيد رقم %(n)s.") % {"n": mirror.number})
    return redirect("ledger:entry", pk=mirror.pk)


# --- chart of accounts -----------------------------------------------------------------------------------------


@require_perm("ledger.view")
def accounts(request):
    """شجرة الحسابات with each account's balance today."""
    org = request.organization
    chart.ensure(org)
    today = services.today()
    tb = reports.trial_balance(org, today, today, hide_zero=False)
    rows = []
    for r in tb.rows:
        net = r.closing_debit - r.closing_credit
        rows.append({"account": r.account, "depth": r.depth, "balance": net if r.account.debit_normal else -net})
    return render(request, "ledger/accounts.html", {"rows": rows, "tab": "accounts"})


def _account(request, pk):
    return get_object_or_404(Account.objects.for_org(request.organization), pk=pk)


@require_perm("ledger.view")
def account_statement(request, pk):
    account = _account(request, pk)
    if account.is_group:
        messages.info(request, _("ده حساب رئيسي. افتح حساب فرعي عشان تشوف حركته."))
        return redirect("ledger:accounts")
    start, end, presets = _range(request)
    st = reports.account_statement(request.organization, account, start, end)
    if request.GET.get("format"):
        table = Table(
            title=f"{_('كشف حساب')}: {account.code} {account.name_ar}",
            subtitle=_period_label(start, end),
            headers=[_("التاريخ"), _("رقم القيد"), _("البيان"), _("مدين"), _("دائن"), _("الرصيد")],
            rows=[["", "", _("رصيد أول المدة"), None, None, st.opening]]
            + [
                [
                    f"{row.line.entry.date:%d/%m/%Y}",
                    row.line.entry.number,
                    row.line.memo or row.line.entry.memo,
                    row.line.debit or None,
                    row.line.credit or None,
                    row.balance,
                ]
                for row in st.lines
            ],
            totals=["", "", _("الإجمالي"), st.debit, st.credit, st.closing],
        )
        return respond(request, table, f"account-{account.code}-{start}-{end}")
    ctx = {"account": account, "st": st, "start": start, "end": end, "presets": presets, "keep": "",
           "tab": "accounts"}  # fmt: skip
    return render(request, "ledger/statement.html", ctx)


@require_perm("accounts.manage")
def account_add(request):
    org = request.organization
    groups = list(Account.objects.for_org(org).filter(is_group=True, is_active=True).order_by("code"))
    parent = next((g for g in groups if str(g.pk) == (request.POST.get("parent") or request.GET.get("parent"))), None)
    error = ""
    if request.method == "POST":
        try:
            account = account_rules.add_account(
                org,
                parent=parent,
                name_ar=request.POST.get("name_ar", ""),
                name_en=request.POST.get("name_en", ""),
                code=request.POST.get("code", ""),
                is_group=request.POST.get("is_group") == "1",
                by=request.user,
            )
        except services.LedgerError as e:
            error = str(e)
        else:
            audit.log("ledger.account_added", request=request, target=account, summary=str(account))
            messages.success(request, _("%(a)s اتضاف.") % {"a": account})
            return redirect("ledger:accounts")
    ctx = {
        "groups": groups,
        "parent": parent,
        "suggested": account_rules.suggest_code(parent) if parent else "",
        "error": error,
        "values": request.POST,
        "tab": "accounts",
    }
    return render(request, "ledger/account_form.html", ctx)


@require_perm("accounts.manage")
def account_edit(request, pk):
    """Rename, move under another group, switch off / on, or remove an account nothing was posted to."""
    account = _account(request, pk)
    org = request.organization
    groups = list(
        Account.objects.for_org(org).filter(is_group=True, type=account.type).exclude(pk=account.pk).order_by("code")
    )
    error = ""
    if request.method == "POST":
        action = request.POST.get("action", "save")
        try:
            if action == "delete":
                name = str(account)
                account_rules.delete_account(account)
                audit.log("ledger.account_deleted", request=request, summary=name)
                messages.success(request, _("%(a)s اتمسح.") % {"a": name})
                return redirect("ledger:accounts")
            if action == "toggle":
                account_rules.set_active(account, not account.is_active, by=request.user)
            else:
                account_rules.rename(
                    account, name_ar=request.POST.get("name_ar", ""), name_en=request.POST.get("name_en", ""),
                    by=request.user,
                )  # fmt: skip
                new_parent = next((g for g in groups if str(g.pk) == request.POST.get("parent")), None)
                if new_parent is not None and new_parent.pk != account.parent_id:
                    account_rules.move(account, parent=new_parent, by=request.user)
        except services.LedgerError as e:
            error = str(e)
        else:
            audit.log("ledger.account_changed", request=request, target=account, summary=f"{action}: {account}")
            messages.success(request, _("%(a)s اتحفظ.") % {"a": account})
            return redirect("ledger:accounts")
    ctx = {
        "account": account,
        "groups": groups,
        "error": error,
        "used": account_rules.has_postings(account),
        "locked": bool(account.system_key) or account_rules.is_treasury_account(account),
        "tab": "accounts",
    }
    return render(request, "ledger/account_form.html", ctx)


# --- periods ---------------------------------------------------------------------------------------------------


@require_perm("ledger.view")
def periods(request):
    org = request.organization
    services.fiscal_year_for(org, services.today())  # the current year always shows
    rows = list(FiscalPeriod.objects.for_org(org).select_related("fiscal_year", "closed_by").order_by("-start_date"))
    open_ones = [p for p in rows if p.is_open]
    closed_ones = [p for p in rows if not p.is_open]
    ctx = {
        "periods": rows,
        "years": FiscalYear.objects.for_org(org).order_by("-start_date"),
        "next_to_close": min(open_ones, key=lambda p: p.start_date) if open_ones else None,
        "last_closed": max(closed_ones, key=lambda p: p.start_date) if closed_ones else None,
        "today": services.today(),
        "tab": "periods",
    }
    return render(request, "ledger/periods.html", ctx)


@require_POST
@require_perm("ledger.close_period")
def period_toggle(request, pk):
    period = get_object_or_404(FiscalPeriod.objects.for_org(request.organization), pk=pk)
    try:
        if period.is_open:
            services.close_period(period, by=request.user)
            audit.log("ledger.period_closed", request=request, target=period, summary=str(period))
            messages.success(request, _("شهر %(p)s اتقفل.") % {"p": f"{period.start_date:%m/%Y}"})
        else:
            services.reopen_period(period, by=request.user)
            audit.log("ledger.period_reopened", request=request, target=period, summary=str(period))
            messages.success(request, _("شهر %(p)s اتفتح تاني.") % {"p": f"{period.start_date:%m/%Y}"})
    except services.LedgerError as e:
        messages.error(request, str(e))
    return redirect("ledger:periods")


# --- financial statements --------------------------------------------------------------------------------------

COMPARE = {"": _("من غير مقارنة"), "previous": _("الفترة اللي قبلها"), "year": _("نفس الفترة السنة اللي فاتت")}


@require_perm("report.statements")
def income_statement(request):
    """قائمة الدخل. Every amount links to its account's statement for the same dates."""
    org = request.organization
    chart.ensure(org)
    start, end, presets = _range(request)
    compare = request.GET.get("compare", "")
    compare = compare if compare in COMPARE else ""
    centers = list(CostCenter.objects.for_org(org).filter(is_active=True))
    center = next((c for c in centers if str(c.pk) == request.GET.get("center")), None)
    st = statements.income_statement(org, start, end, cost_center=center, compare=compare)
    if request.GET.get("format"):
        compared = bool(st.compare_start)
        rows = []
        for section in (st.revenue, st.expenses):
            rows.append([section.title, "", section.total, *([section.compare] if compared else [])])
            rows += [[r.account.code, r.account.name_ar, r.amount, *([r.compare] if compared else [])]
                     for r in section.rows]  # fmt: skip
        table = Table(
            title=_("قائمة الدخل") + (f": {center.name_ar}" if center else ""),
            subtitle=_period_label(start, end),
            headers=[_("الكود"), _("البند"), _("الفترة"), *([_("المقارنة")] if compared else [])],
            rows=rows,
            totals=["", _("صافي الربح"), st.net, *([st.compare_net] if compared else [])],
        )
        return respond(request, table, f"income-statement-{start}-{end}")
    keep = (f"&compare={compare}" if compare else "") + (f"&center={center.pk}" if center else "")
    ctx = {
        "st": st, "start": start, "end": end, "presets": presets, "compare": compare,
        "compare_choices": COMPARE.items(),
        "centers": centers, "center": center, "keep": keep,
        "by_center": statements.by_cost_center(org, start, end) if center is None else [], "tab": "income",
        "start_s": f"{start:%Y-%m-%d}", "end_s": f"{end:%Y-%m-%d}",
    }  # fmt: skip
    return render(request, "ledger/income_statement.html", ctx)


@require_perm("report.statements")
def balance_sheet(request):
    org = request.organization
    chart.ensure(org)
    today = services.today()
    as_of = _parse_date(request.GET.get("date"), today)
    year_start = services.fiscal_year_for(org, as_of).start_date
    compare_as_of = year_start - timedelta(days=1) if request.GET.get("compare") == "1" else None
    bs = statements.balance_sheet(org, as_of, compare_as_of=compare_as_of)
    if request.GET.get("format"):
        compared = compare_as_of is not None
        rows = []
        for section in (bs.assets, bs.liabilities, bs.equity):
            rows.append([section.title, "", section.total, *([section.compare] if compared else [])])
            rows += [[r.account.code, r.account.name_ar, r.amount, *([r.compare] if compared else [])]
                     for r in section.rows]  # fmt: skip
        rows.append(["", _("نتيجة الفترة (لسه ما اترحّلتش)"), bs.result, *([bs.compare_result] if compared else [])])
        table = Table(
            title=_("المركز المالي"),
            subtitle=f"{as_of:%d/%m/%Y}",
            headers=[_("الكود"), _("البند"), _("الرصيد"), *([f"{compare_as_of:%d/%m/%Y}"] if compared else [])],
            rows=rows,
            totals=["", _("الالتزامات وحقوق الملكية"), bs.liabilities_and_equity, *([None] if compared else [])],
        )
        return respond(request, table, f"balance-sheet-{as_of}")
    ctx = {"bs": bs, "as_of": as_of, "compare_as_of": compare_as_of, "year_start": year_start, "today": today,
           "year_start_s": f"{year_start:%Y-%m-%d}", "as_of_s": f"{as_of:%Y-%m-%d}", "tab": "balance"}  # fmt: skip
    return render(request, "ledger/balance_sheet.html", ctx)


# --- books verification ----------------------------------------------------------------------------------------


@require_perm("books.verify")
def verify_books(request):
    """فحص سلامة الدفاتر: the last result, and a button to run it now."""
    org = request.organization
    if request.method == "POST":
        result = verify.run(org, by=request.user)
        audit.log("ledger.verified", request=request, target=result, summary="ok" if result.ok else "problems")
        if result.ok:
            messages.success(request, _("الدفاتر سليمة."))
        else:
            messages.error(request, _("الفحص لقى %(n)s مشكلة.") % {"n": len(result.problems)})
        return redirect("ledger:verify")
    ctx = {
        "last": verify.last(org),
        "history": BooksCheck.objects.for_org(org).order_by("-ran_at")[:10],
        "tab": "verify",
    }
    return render(request, "ledger/verify.html", ctx)


# --- closing the year ------------------------------------------------------------------------------------------


@require_perm("ledger.close_period")
def year_close(request, pk):
    year = get_object_or_404(FiscalYear.objects.for_org(request.organization), pk=pk)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "reopen":
                closing.reopen_year(year, by=request.user)
                audit.log("ledger.year_reopened", request=request, target=year, summary=year.name)
                messages.success(request, _("السنة %(y)s اتفتحت تاني.") % {"y": year.name})
            else:
                closing.close_year(year, by=request.user)
                audit.log("ledger.year_closed", request=request, target=year, summary=year.name)
                messages.success(request, _("السنة %(y)s اتقفلت، ونتيجتها اترحّلت للأرباح المرحّلة.") % {"y": year.name})
        except services.LedgerError as e:
            messages.error(request, str(e))
        return redirect("ledger:year_close", pk=year.pk)
    is_closed = year.status == PeriodStatus.CLOSED
    checks = [] if is_closed else closing.checklist(year)
    ctx = {
        "year": year, "is_closed": is_closed, "checks": checks, "ready": all(c.ok for c in checks),
        "result": closing.result_of(year), "entry": closing.closing_entry(year), "tab": "periods",
    }  # fmt: skip
    return render(request, "ledger/year_close.html", ctx)
