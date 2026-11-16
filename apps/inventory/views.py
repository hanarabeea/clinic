"""Store screens: what is on the shelves, the moves behind it, issuing and transferring, counts. Thin views: every
quantity changes through inventory.services."""

from decimal import Decimal

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import require_perm
from apps.audit import services as audit
from apps.ledger import services as ledger
from apps.ledger.export import Table, respond
from apps.ledger.models import CostCenter
from apps.ledger.money import ZERO
from apps.ledger.views import _parse_date

from . import services
from .forms import InventorySettingsForm, StockItemForm, WarehouseForm
from .models import (
    CountKind,
    CountStatus,
    DocKind,
    InventorySettings,
    MoveKind,
    StockBalance,
    StockCount,
    StockDoc,
    StockItem,
    StockMove,
    Warehouse,
)


def _stores(org):
    services.ensure_defaults(org)
    return list(Warehouse.objects.for_org(org).filter(is_active=True))


def _soonest(batches) -> list:
    """One line per item for the expiry notice: its lot that expires first."""
    seen, out = set(), []
    for batch in batches:  # already soonest first
        if batch.item_id not in seen:
            seen.add(batch.item_id)
            out.append(batch)
    return out


def _item(request, pk):
    return get_object_or_404(StockItem.objects.for_org(request.organization), pk=pk)


# --- what is on the shelves --------------------------------------------------------------------------------------


@require_perm("stock.view")
def items(request):
    """Every item with how many each store holds and what they are worth, and what needs attention first: items
    at their reorder level and lots about to expire."""
    org = request.organization
    stores = _stores(org)
    q = (request.GET.get("q") or "").strip()
    rows = StockItem.objects.for_org(org).order_by("-is_active", "name")
    if q:
        rows = rows.filter(name__icontains=q)
    rows = list(rows)
    by_item = {}
    for b in StockBalance.objects.for_org(org).filter(item__in=rows):
        by_item.setdefault(b.item_id, {})[b.warehouse_id] = b
    for item in rows:
        held = by_item.get(item.pk, {})
        item.cells = [held.get(s.pk) for s in stores]
        item.total = sum((b.quantity for b in held.values()), Decimal(0))
        item.value = sum((b.value for b in held.values()), ZERO)
        item.low = item.is_active and item.reorder_level > 0 and item.total <= item.reorder_level
    if request.GET.get("format"):
        table = Table(
            title=_("أرصدة المخزن"),
            subtitle=f"{ledger.today():%d/%m/%Y}",
            headers=[_("الصنف"), _("الوحدة"), *[s.name for s in stores], _("الإجمالي"), _("القيمة")],
            rows=[
                [i.name, i.unit, *[(c.quantity if c else Decimal(0)) for c in i.cells], i.total, i.value] for i in rows
            ],
            totals=[_("الإجمالي"), "", *[None for _s in stores], None, sum((i.value for i in rows), ZERO)],
        )
        return respond(request, table, "stock")
    ctx = {
        "rows": rows, "stores": stores, "q": q, "value": services.stock_value(org),
        "low": services.low_stock(org), "expiring": _soonest(services.expiring(org)), "today": ledger.today(),
        "tab": "items",
    }  # fmt: skip
    return render(request, "inventory/items.html", ctx)


@require_perm("stock.move")
def item_edit(request, pk=None):
    org = request.organization
    item = _item(request, pk) if pk else None
    form = StockItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if StockItem.objects.for_org(org).filter(name__iexact=obj.name).exclude(pk=obj.pk).exists():
            form.add_error("name", _("فيه صنف بنفس الاسم."))
        else:
            obj.organization = org
            obj._history_user = request.user
            obj.save()
            audit.log("stock.item", request=request, target=obj, summary=obj.name)
            return redirect("inventory:item", pk=obj.pk)
    return render(request, "inventory/item_form.html", {"form": form, "item": item, "tab": "items"})


@require_perm("stock.view")
def item_detail(request, pk):
    """The item's card: its balances, its lots, and every move with the balance after it."""
    item = _item(request, pk)
    moves = (
        StockMove.objects.for_org(request.organization)
        .filter(item=item)
        .select_related("warehouse", "doc__fiscal_year", "purchase_line__invoice__fiscal_year", "created_by")
        .order_by("-id")[:200]
    )
    ctx = {
        "item": item,
        "balances": item.balances.select_related("warehouse").filter(quantity__gt=0),
        "batches": item.batches.select_related("warehouse").filter(quantity__gt=0),
        "moves": moves,
        "today": ledger.today(),
        "tab": "items",
    }
    return render(request, "inventory/item.html", ctx)


# --- documents ---------------------------------------------------------------------------------------------------


@require_perm("stock.view")
def docs(request):
    rows = list(
        StockDoc.objects.for_org(request.organization)
        .select_related("warehouse", "to_warehouse", "cost_center", "fiscal_year", "created_by", "operation")
        .prefetch_related("moves__item")[:200]
    )
    for doc in rows:  # a transfer is two moves per item: show the one that left
        doc.lines = [m for m in doc.moves.all() if m.kind != MoveKind.TRANSFER_IN]
    return render(request, "inventory/docs.html", {"rows": rows, "tab": "docs"})


def _typed_lines(request, items_by_pk):
    post = request.POST
    return [
        (items_by_pk.get(item_pk), quantity)
        for item_pk, quantity in zip(post.getlist("item"), post.getlist("quantity"), strict=False)
        if item_pk or quantity.strip()
    ]


def _doc_form(request, kind):
    """Issue, transfer and opening balance share one form: a store, rows of item + quantity, and what is special to
    each (a cost center, the other store, a unit cost and an expiry date)."""
    org = request.organization
    stores = _stores(org)
    all_items = list(StockItem.objects.for_org(org).filter(is_active=True))
    centers = list(CostCenter.objects.for_org(org).filter(is_active=True))
    post, error = request.POST, ""
    if request.method == "POST":
        pick = lambda rows, key: next((r for r in rows if str(r.pk) == post.get(key)), None)  # noqa: E731
        items_by_pk = {str(i.pk): i for i in all_items}
        try:
            if kind == DocKind.ISSUE:
                doc = services.issue(
                    org, warehouse=pick(stores, "warehouse"), lines=_typed_lines(request, items_by_pk),
                    cost_center=pick(centers, "cost_center"), by=request.user, notes=post.get("notes", ""),
                )  # fmt: skip
            elif kind == DocKind.TRANSFER:
                doc = services.transfer(
                    org, source=pick(stores, "warehouse"), target=pick(stores, "to_warehouse"),
                    lines=_typed_lines(request, items_by_pk), by=request.user, notes=post.get("notes", ""),
                )  # fmt: skip
            else:
                rows = zip(post.getlist("item"), post.getlist("quantity"), post.getlist("unit_cost"),
                           post.getlist("expiry"), strict=False)  # fmt: skip
                doc = services.opening(
                    org, warehouse=pick(stores, "warehouse"), by=request.user, notes=post.get("notes", ""),
                    lines=[(items_by_pk.get(i), q, c, _parse_date(e, None)) for i, q, c, e in rows if i],
                )  # fmt: skip
        except services.StockError as e:
            error = str(e)
        else:
            audit.log("stock.doc", request=request, target=doc, summary=doc.label)
            messages.success(request, _("%(d)s اتسجل.") % {"d": doc.label})
            return redirect("inventory:docs")
    typed = list(zip(post.getlist("item"), post.getlist("quantity"), post.getlist("unit_cost"), post.getlist("expiry"),
                     strict=False))  # fmt: skip
    rows = [{"item": i, "quantity": q, "unit_cost": c, "expiry": e} for i, q, c, e in typed] if typed else []
    if not rows:  # issue / transfer post no cost or date columns
        rows = [{"item": i, "quantity": q, "unit_cost": "", "expiry": ""}
                for i, q in zip(post.getlist("item"), post.getlist("quantity"), strict=False)]  # fmt: skip
    while len(rows) < 4:
        rows.append({"item": "", "quantity": "", "unit_cost": "", "expiry": ""})
    ctx = {"kind": kind, "stores": stores, "items": all_items, "centers": centers, "rows": rows, "post": post,
           "error": error, "tab": "docs"}  # fmt: skip
    return render(request, "inventory/doc_form.html", ctx)


@require_perm("stock.move")
def issue_new(request):
    return _doc_form(request, DocKind.ISSUE)


@require_perm("stock.move")
def transfer_new(request):
    return _doc_form(request, DocKind.TRANSFER)


@require_perm("stock.move")
def opening_new(request):
    return _doc_form(request, DocKind.OPENING)


# --- counts ------------------------------------------------------------------------------------------------------


def _count(request, pk):
    return get_object_or_404(
        StockCount.objects.for_org(request.organization).select_related(
            "warehouse", "counted_by", "approved_by", "doc"
        ),
        pk=pk,
    )


@require_perm("stock.view")
def counts(request):
    org = request.organization
    rows = StockCount.objects.for_org(org).select_related("warehouse", "counted_by", "approved_by")[:100]
    ctx = {"rows": rows, "stores": _stores(org), "kinds": CountKind.choices, "tab": "counts"}
    return render(request, "inventory/counts.html", ctx)


@require_POST
@require_perm("stock.count")
def count_start(request):
    org = request.organization
    store = get_object_or_404(Warehouse.objects.for_org(org), pk=request.POST.get("warehouse") or 0)
    kind = request.POST.get("kind", CountKind.FULL)
    try:
        count = services.start_count(
            org, warehouse=store, by=request.user, kind=kind if kind in CountKind.values else CountKind.FULL,
            sample=request.POST.get("sample") or 5,
        )  # fmt: skip
    except (services.StockError, ValueError) as e:
        messages.error(request, str(e) or _("اكتب عدد الأصناف رقم."))
        return redirect("inventory:counts")
    audit.log("stock.count_started", request=request, target=count, summary=store.name)
    return redirect("inventory:count", pk=count.pk)


@require_perm("stock.view")
def count_detail(request, pk):
    count = _count(request, pk)
    lines = list(count.lines.select_related("item"))
    for line in lines:
        diff = line.difference
        if diff and line.expected:
            line.diff_value = (line.expected_value * diff / line.expected).quantize(Decimal("0.01"))
        else:
            line.diff_value = None
    ctx = {
        "count": count,
        "lines": lines,
        "is_open": count.status == CountStatus.OPEN,
        "waiting": count.status == CountStatus.SUBMITTED,
        "can_approve": count.status == CountStatus.SUBMITTED and services.can_approve(count, request.membership),
        "differences": sum(1 for line in lines if line.difference),
        "tab": "counts",
    }
    return render(request, "inventory/count.html", ctx)


@require_POST
@require_perm("stock.count")
def count_save(request, pk):
    count = _count(request, pk)
    quantities = {key[4:]: value for key, value in request.POST.items() if key.startswith("qty_")}
    try:
        services.save_count(count, quantities, by=request.user)
        if request.POST.get("submit") == "1":
            services.submit_count(count, by=request.user)
            audit.log("stock.count_submitted", request=request, target=count, summary=count.warehouse.name)
            messages.success(request, _("الجرد اتبعت للاعتماد."))
        else:
            messages.success(request, _("اتحفظ."))
    except services.StockError as e:
        messages.error(request, str(e))
    return redirect("inventory:count", pk=count.pk)


@require_POST
@require_perm("stock.approve_count")
def count_approve(request, pk):
    count = _count(request, pk)
    try:
        services.approve_count(count, by=request.user, membership=request.membership)
    except services.StockError as e:
        messages.error(request, str(e))
    else:
        audit.log("stock.count_approved", request=request, target=count, summary=count.warehouse.name)
        messages.success(request, _("الجرد اتعتمد والفروق اتسوّت."))
    return redirect("inventory:count", pk=count.pk)


@require_POST
@require_perm("stock.count")
def count_cancel(request, pk):
    count = _count(request, pk)
    try:
        services.cancel_count(count, by=request.user)
    except services.StockError as e:
        messages.error(request, str(e))
    return redirect("inventory:counts")


# --- set-up ------------------------------------------------------------------------------------------------------


@require_perm("stock.move")
def setup(request):
    """Stores and the expiry warning. (Items are added from the items tab.)"""
    org = request.organization
    services.ensure_defaults(org)
    form = InventorySettingsForm(request.POST or None, instance=InventorySettings.for_org(org))
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj._history_user = request.user
        obj.save()
        messages.success(request, _("اتحفظ."))
        return redirect("inventory:setup")
    ctx = {"form": form, "stores": Warehouse.objects.for_org(org), "tab": "items"}
    return render(request, "inventory/setup.html", ctx)


@require_perm("stock.move")
def warehouse_edit(request, pk=None):
    org = request.organization
    store = get_object_or_404(Warehouse.objects.for_org(org), pk=pk) if pk else None
    form = WarehouseForm(request.POST or None, instance=store)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if Warehouse.objects.for_org(org).filter(name__iexact=obj.name).exclude(pk=obj.pk).exists():
            form.add_error("name", _("فيه مخزن بنفس الاسم."))
        elif store and not obj.is_active and store.balances.filter(quantity__gt=0).exists():
            form.add_error("is_active", _("المخزن ده فيه أصناف. حوّلها الأول."))
        else:
            obj.organization = org
            obj._history_user = request.user
            obj.save()
            return redirect("inventory:setup")
    return render(request, "inventory/warehouse_form.html", {"form": form, "store": store, "tab": "items"})
