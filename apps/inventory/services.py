"""Stock logic. Every change of a quantity goes through `_move`, under a lock on that item's balance in that store.

Costing is weighted average per item per store. A balance keeps its quantity and its total value; what goes out
takes its share of the value (`cost_out`), and the last unit out takes all that is left, so no rounding remainder is
ever stranded on an empty shelf. Entries:

    receipt from a purchase     posted by the purchase invoice (Dr inventory / Cr the supplier)
    return to a supplier        posted by the purchase return (Cr inventory at the value that left the store)
    issue                       Dr supplies used (cost center)     Cr inventory
    issue returned to the store the reverse
    transfer between stores     no entry: the value only changes shelf
    count shortage / overage    Dr / Cr stock over and short       Cr / Dr inventory
    opening balance             Dr inventory                       Cr opening balances
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import has_perm
from apps.ledger import services as ledger
from apps.ledger.money import ZERO, money
from apps.ledger.services import credit, debit
from apps.organizations.models import Role

from .models import (
    MOVES_IN,
    CountKind,
    CountStatus,
    DocKind,
    InventorySettings,
    MoveKind,
    StockBalance,
    StockBatch,
    StockCount,
    StockCountLine,
    StockDoc,
    StockItem,
    StockMove,
    StockSequence,
    Warehouse,
)

Q3 = Decimal("0.001")
DEFAULT_WAREHOUSES = [("main", "المخزن الرئيسي"), ("theatre", "دولاب العمليات")]


class StockError(Exception):
    """Refused. The message is written for the person at the screen."""


def ensure_defaults(org) -> None:
    have = set(Warehouse.objects.filter(organization=org).exclude(system_key="").values_list("system_key", flat=True))
    for key, name in DEFAULT_WAREHOUSES:
        if key not in have and not Warehouse.objects.filter(organization=org, name=name).exists():
            Warehouse.objects.create(organization=org, name=name, system_key=key)
    InventorySettings.for_org(org)


def warehouse(org, key: str) -> Warehouse:
    found = Warehouse.objects.filter(organization=org, system_key=key).first()
    if found is None:
        ensure_defaults(org)
        found = Warehouse.objects.get(organization=org, system_key=key)
    return found


def _qty(value, what=None) -> Decimal:
    try:
        value = Decimal(str(value if value not in (None, "") else "0").replace(",", "")).quantize(Q3)
    except (ArithmeticError, ValueError) as e:
        raise StockError(_("%(w)s لازم تتكتب بالأرقام.") % {"w": what or _("الكمية")}) from e
    if not value.is_finite():
        raise StockError(_("%(w)s لازم تتكتب بالأرقام.") % {"w": what or _("الكمية")})
    return value


def _next_number(org, fiscal_year, kind) -> int:
    seq, _created = StockSequence.objects.get_or_create(organization=org, fiscal_year=fiscal_year, kind=kind)
    seq = StockSequence.objects.select_for_update().get(pk=seq.pk)
    seq.last_number += 1
    seq.save(update_fields=["last_number", "updated_at"])
    return seq.last_number


def _doc(org, kind, *, warehouse, by, date=None, **fields) -> StockDoc:
    date = date or ledger.today()
    fiscal_year = ledger.fiscal_year_for(org, date)
    return StockDoc.objects.create(
        organization=org, kind=kind, fiscal_year=fiscal_year, number=_next_number(org, fiscal_year, kind), date=date,
        warehouse=warehouse, created_by=by, **fields,
    )  # fmt: skip


def _locked_balance(item, store) -> StockBalance:
    StockBalance.objects.get_or_create(organization=item.organization, item=item, warehouse=store)
    return StockBalance.objects.select_for_update().get(item=item, warehouse=store)


def cost_out(balance, quantity) -> Decimal:
    """The value `quantity` units take with them at the weighted average. All of them take all of it."""
    if quantity >= balance.quantity:
        return balance.value
    return money(balance.value * quantity / balance.quantity)


def _check(org, item, store) -> None:
    if item is None or item.organization_id != org.pk:
        raise StockError(_("اختار الصنف."))
    if store is None or store.organization_id != org.pk:
        raise StockError(_("اختار المخزن."))


def _batches_in(item, store, quantity, expiry, lot) -> None:
    lot = (lot or "").strip()[:40]
    batch = (
        StockBatch.objects.select_for_update().filter(item=item, warehouse=store, expiry_date=expiry, lot=lot).first()
    )
    if batch is None:
        batch = StockBatch(organization=item.organization, item=item, warehouse=store, expiry_date=expiry, lot=lot)
    batch.quantity += quantity
    batch.save()


def _batches_out(item, store, quantity) -> None:
    """First to expire, first out. Lots without a date go last."""
    left = quantity
    batches = StockBatch.objects.select_for_update().filter(item=item, warehouse=store, quantity__gt=0)
    for batch in sorted(batches, key=lambda b: (b.expiry_date is None, b.expiry_date or timezone.now().date(), b.pk)):
        take = min(batch.quantity, left)
        batch.quantity -= take
        batch.save(update_fields=["quantity", "updated_at"])
        left -= take
        if left <= 0:
            return
    if left > 0:  # the lots hold less than the balance: never silently
        raise StockError(_("تشغيلات «%(i)s» أقل من رصيده. راجع الجرد.") % {"i": item.name})


def _move(
    kind, item, store, quantity, *, by, value=None, date=None, doc=None, expiry=None, lot="", **links
) -> StockMove:
    """The one place a balance changes. In: `value` is what came with the goods. Out: the value is worked out here
    from the weighted average unless the caller names it (a move that undoes another takes back exactly its value)."""
    balance = _locked_balance(item, store)
    incoming = kind in MOVES_IN
    if quantity <= 0:
        raise StockError(_("الكمية لازم تكون أكبر من صفر."))
    if incoming:
        value = money(value or 0)
        if value < 0:
            raise StockError(_("القيمة مينفعش تكون بالسالب."))
        balance.quantity += quantity
        balance.value += value
        if item.track_expiry:
            _batches_in(item, store, quantity, expiry, lot)
    else:
        if quantity > balance.quantity:
            raise StockError(
                _("مفيش في %(w)s من «%(i)s» غير %(q)s.")
                % {"w": store.name, "i": item.name, "q": f"{balance.quantity.normalize():f}"}
            )
        value = cost_out(balance, quantity) if value is None else min(money(value), balance.value)
        if quantity == balance.quantity:
            value = balance.value  # nothing is left on the shelf, so nothing is left of its value
        balance.quantity -= quantity
        balance.value -= value
        if item.track_expiry:
            _batches_out(item, store, quantity)
    balance.save(update_fields=["quantity", "value", "updated_at"])
    return StockMove.objects.create(
        organization=item.organization, kind=kind, item=item, warehouse=store, date=date or ledger.today(),
        quantity=quantity, value=value, expiry_date=expiry if incoming else None,
        lot=(lot or "")[:40] if incoming else "",
        doc=doc, quantity_after=balance.quantity, value_after=balance.value, created_by=by, **links,
    )  # fmt: skip


# --- purchases (called by apps/purchasing) -------------------------------------------------------------------------


def receive_purchase_line(line, *, by) -> StockMove:
    """A purchase line of a stock item puts it on the shelf at what it cost (its net, plus VAT that is not
    recoverable). The purchase invoice's own entry debits inventory by the same amount."""
    if line.item.track_expiry and line.expiry_date is None:
        raise StockError(_("اكتب تاريخ صلاحية «%(i)s».") % {"i": line.item.name})
    return _move(
        MoveKind.RECEIPT, line.item, line.warehouse, line.quantity, value=line.cost, by=by, date=line.invoice.date,
        expiry=line.expiry_date, lot=line.lot, purchase_line=line,
    )  # fmt: skip


def return_purchase_line(return_line, *, by) -> Decimal:
    """Goods going back to the supplier leave the store they were received into, at today's average. Returns the
    value that left (the purchase return credits inventory by exactly that)."""
    line = return_line.line
    move = _move(
        MoveKind.PURCHASE_RETURN, line.item, line.warehouse, return_line.quantity, by=by, date=return_line.ret.date,
        return_line=return_line,
    )  # fmt: skip
    return move.value


# --- issues ------------------------------------------------------------------------------------------------------


def _clean_lines(org, lines) -> list:
    out, seen = [], set()
    for item, quantity in lines:
        quantity = _qty(quantity)
        if quantity == 0:
            continue
        if item is None or item.organization_id != org.pk:
            raise StockError(_("اختار الصنف."))
        if quantity < 0:
            raise StockError(_("الكمية لازم تكون أكبر من صفر."))
        if item.pk in seen:
            raise StockError(_("«%(i)s» متكرر. اجمع الكمية في سطر واحد.") % {"i": item.name})
        seen.add(item.pk)
        out.append((item, quantity))
    if not out:
        raise StockError(_("اكتب كمية صنف واحد على الأقل."))
    return out


@transaction.atomic
def issue(org, *, warehouse, lines, cost_center, by, operation=None, notes="", date=None) -> StockDoc:
    """Take supplies out of a store for a department or an operation. They become an expense of that cost center at
    the store's weighted average cost."""
    if warehouse is None or warehouse.organization_id != org.pk or not warehouse.is_active:
        raise StockError(_("اختار مخزن شغال."))
    if cost_center is None or cost_center.organization_id != org.pk:
        raise StockError(_("اختار مركز التكلفة اللي الصرف عليه."))
    lines = _clean_lines(org, lines)
    doc = _doc(org, DocKind.ISSUE, warehouse=warehouse, by=by, date=date, cost_center=cost_center,
               operation=operation, notes=(notes or "")[:300])  # fmt: skip
    total = ZERO
    for item, quantity in sorted(lines, key=lambda row: row[0].pk):  # one lock order for everyone
        total += _move(MoveKind.ISSUE, item, warehouse, quantity, by=by, date=doc.date, doc=doc).value
    if total > 0:
        _post(doc, [debit("medical_supplies_expense", total, cost_center=cost_center), credit("inventory", total)],
              _("صرف من %(w)s") % {"w": warehouse.name}, by=by)  # fmt: skip
    return doc


@transaction.atomic
def return_issue(move, *, by) -> StockMove:
    """Supplies that were issued and not used go back on the shelf at the value they left with."""
    move = (
        StockMove.objects.select_for_update(of=("self",))
        .select_related("doc__cost_center", "item", "warehouse")
        .get(pk=move.pk)
    )
    if move.kind != MoveKind.ISSUE:
        raise StockError(_("دي مش حركة صرف."))
    if hasattr(move, "reversed_by"):
        raise StockError(_("الصنف ده رجع المخزن قبل كده."))
    back = _move(
        MoveKind.ISSUE_RETURN, move.item, move.warehouse, move.quantity, value=move.value, by=by, doc=move.doc,
        reverses=move,
    )  # fmt: skip
    if move.value > 0:
        doc = move.doc
        lines = [
            debit("inventory", move.value),
            credit("medical_supplies_expense", move.value, cost_center=doc.cost_center),
        ]
        _post(doc, lines,
              _("رجوع للمخزن من %(d)s") % {"d": doc.label}, by=by, event=f"return:{move.pk}")  # fmt: skip
    return back


def _post(doc, lines, memo, *, by, event=""):
    try:
        day = doc.date if not event else ledger.today()
        return ledger.post(doc.organization, date=day, memo=f"{doc.label}: {memo}",
                           lines=lines, source=doc, event=event, by=by)  # fmt: skip
    except ledger.LedgerError as e:
        raise StockError(str(e)) from e


@transaction.atomic
def transfer(org, *, source, target, lines, by, notes="", date=None) -> StockDoc:
    """Move stock from one store to another. Its value goes with it; the books do not change."""
    for store in (source, target):
        if store is None or store.organization_id != org.pk or not store.is_active:
            raise StockError(_("اختار مخزن شغال."))
    if source.pk == target.pk:
        raise StockError(_("اختار مخزنين مختلفين."))
    lines = _clean_lines(org, lines)
    doc = _doc(
        org, DocKind.TRANSFER, warehouse=source, by=by, date=date, to_warehouse=target, notes=(notes or "")[:300]
    )
    for item, quantity in sorted(lines, key=lambda row: row[0].pk):
        expiry = None
        if item.track_expiry:  # the lot's date travels with it (the earliest one, which is what leaves first)
            first = (
                StockBatch.objects.filter(item=item, warehouse=source, quantity__gt=0).order_by("expiry_date").first()
            )
            expiry = first.expiry_date if first else None
        out = _move(MoveKind.TRANSFER_OUT, item, source, quantity, by=by, date=doc.date, doc=doc)
        _move(
            MoveKind.TRANSFER_IN, item, target, quantity, value=out.value, by=by, date=doc.date, doc=doc, expiry=expiry
        )
    return doc


@transaction.atomic
def opening(org, *, warehouse, lines, by, date=None, notes="") -> StockDoc:
    """What was already on the shelves when the clinic started using the program. `lines` = [(item, quantity,
    unit cost, expiry or None)]."""
    if warehouse is None or warehouse.organization_id != org.pk:
        raise StockError(_("اختار المخزن."))
    doc = _doc(org, DocKind.OPENING, warehouse=warehouse, by=by, date=date, notes=(notes or "")[:300])
    total, any_line = ZERO, False
    for item, quantity, unit_cost, expiry in lines:
        quantity = _qty(quantity)
        if quantity == 0:
            continue
        _check(org, item, warehouse)
        try:
            value = money(quantity * Decimal(str(unit_cost or 0)))
        except (ArithmeticError, ValueError, TypeError) as e:
            raise StockError(_("اكتب تكلفة الوحدة بالأرقام.")) from e
        if quantity < 0 or value < 0:
            raise StockError(_("الكمية والتكلفة مش بالسالب."))
        if item.track_expiry and expiry is None:
            raise StockError(_("اكتب تاريخ صلاحية «%(i)s».") % {"i": item.name})
        total += _move(
            MoveKind.OPENING, item, warehouse, quantity, value=value, by=by, date=doc.date, doc=doc, expiry=expiry
        ).value
        any_line = True
    if not any_line:
        raise StockError(_("اكتب كمية صنف واحد على الأقل."))
    if total > 0:
        _post(doc, [debit("inventory", total), credit("opening_balances", total)], _("رصيد افتتاحي للمخزن"), by=by)
    return doc


# --- counts (جرد) ------------------------------------------------------------------------------------------------


@transaction.atomic
def start_count(org, *, warehouse, by, kind=CountKind.FULL, items=None, sample=0, notes="") -> StockCount:
    """Open a count sheet. Full: every active item (and anything else the store holds). Spot: the items chosen, or
    `sample` items picked at random from what the store holds. The sheet shows no expected quantities."""
    if warehouse is None or warehouse.organization_id != org.pk:
        raise StockError(_("اختار المخزن."))
    if StockCount.objects.filter(warehouse=warehouse, status__in=(CountStatus.OPEN, CountStatus.SUBMITTED)).exists():
        raise StockError(_("فيه جرد مفتوح للمخزن ده. خلّصه أو الغيه الأول."))
    held = StockItem.objects.filter(balances__warehouse=warehouse, balances__quantity__gt=0)
    if kind == CountKind.FULL:
        chosen = list((StockItem.objects.filter(organization=org, is_active=True) | held).distinct())
    elif items:
        chosen = [i for i in items if i.organization_id == org.pk]
    else:
        pool = list(held.distinct())
        chosen = random.sample(pool, min(max(int(sample or 0), 1), len(pool))) if pool else []
    if not chosen:
        raise StockError(_("مفيش أصناف تتجرد."))
    count = StockCount(organization=org, warehouse=warehouse, kind=kind, counted_by=by, notes=(notes or "")[:300])
    count._history_user = by
    count.save()
    StockCountLine.objects.bulk_create(StockCountLine(organization=org, count=count, item=item) for item in chosen)
    return count


@transaction.atomic
def save_count(count, quantities: dict, *, by) -> StockCount:
    """Write down what was counted so far ({item pk: quantity}; blank = not counted yet)."""
    count = StockCount.objects.select_for_update(of=("self",)).get(pk=count.pk)
    if count.status != CountStatus.OPEN:
        raise StockError(_("الجرد ده اتقفل."))
    for line in count.lines.all():
        raw = quantities.get(line.item_id, quantities.get(str(line.item_id)))
        if raw is None:
            continue
        line.counted = None if str(raw).strip() == "" else _qty(raw)
        if line.counted is not None and line.counted < 0:
            raise StockError(_("الكمية مش بالسالب."))
        line.save(update_fields=["counted", "updated_at"])
    return count


@transaction.atomic
def submit_count(count, *, by) -> StockCount:
    """The counting is done: what the books hold right now is written next to what was counted."""
    count = StockCount.objects.select_for_update(of=("self",)).select_related("warehouse").get(pk=count.pk)
    if count.status != CountStatus.OPEN:
        raise StockError(_("الجرد ده اتقفل."))
    lines = list(count.lines.select_related("item"))
    missing = [line.item.name for line in lines if line.counted is None]
    if missing:
        raise StockError(_("لسه ما اتعدّش: %(l)s.") % {"l": "، ".join(missing[:6])})
    balances = {b.item_id: b for b in StockBalance.objects.filter(warehouse=count.warehouse)}
    for line in lines:
        balance = balances.get(line.item_id)
        line.expected = balance.quantity if balance else Decimal(0)
        line.expected_value = balance.value if balance else ZERO
        line.save(update_fields=["expected", "expected_value", "updated_at"])
    count.status, count.submitted_at = CountStatus.SUBMITTED, timezone.now()
    count._history_user = by
    count.save(update_fields=["status", "submitted_at", "updated_at"])
    return count


def can_approve(count, membership) -> bool:
    """Whoever counted does not approve their own differences (the owner may)."""
    if membership is None or not has_perm(membership, "stock.approve_count"):
        return False
    return membership.role == Role.OWNER or membership.user_id != count.counted_by_id


def _last_unit_cost(item, store) -> Decimal:
    move = (
        StockMove.objects.filter(item=item, kind__in=(MoveKind.RECEIPT, MoveKind.OPENING), value__gt=0)
        .order_by("-id")
        .first()
    )
    return move.unit_cost if move else Decimal(0)


@transaction.atomic
def approve_count(count, *, by, membership) -> StockCount:
    """Approve the differences: each becomes an adjustment move, and their value goes to "عجز وزيادة المخزون"."""
    count = (
        StockCount.objects.select_for_update(of=("self",)).select_related("warehouse", "organization").get(pk=count.pk)
    )
    if count.status != CountStatus.SUBMITTED:
        raise StockError(_("الجرد ده مش مستني اعتماد."))
    if not can_approve(count, membership):
        raise StockError(_("اللي عدّ مينفعش يعتمد فروق جرده. لازم حد تاني."))
    org, store = count.organization, count.warehouse
    lines = [line for line in count.lines.select_related("item") if line.difference]
    short = over = ZERO
    doc = None
    if lines:
        doc = _doc(org, DocKind.ADJUSTMENT, warehouse=store, by=by, notes=_("تسوية جرد"))
    for line in sorted(lines, key=lambda row: row.item_id):
        balance = _locked_balance(line.item, store)
        if balance.quantity != line.expected:
            raise StockError(_("رصيد «%(i)s» اتحرك بعد الجرد. الغي الجرد ده واعمله تاني.") % {"i": line.item.name})
        diff = line.difference
        if diff < 0:
            short += _move(MoveKind.ADJUST_OUT, line.item, store, -diff, by=by, doc=doc).value
        else:
            unit = balance.average_cost if balance.quantity else _last_unit_cost(line.item, store)
            over += _move(MoveKind.ADJUST_IN, line.item, store, diff, value=money(unit * diff), by=by, doc=doc).value
    entry_lines = []
    if short > 0:
        entry_lines += [debit("stock_over_short", short, cost_center="admin"), credit("inventory", short)]
    if over > 0:
        entry_lines += [debit("inventory", over), credit("stock_over_short", over, cost_center="admin")]
    if entry_lines:
        _post(doc, entry_lines, _("تسوية جرد %(w)s") % {"w": store.name}, by=by)
    count.status, count.approved_by, count.approved_at, count.doc = CountStatus.APPROVED, by, timezone.now(), doc
    count._history_user = by
    count.save(update_fields=["status", "approved_by", "approved_at", "doc", "updated_at"])
    return count


@transaction.atomic
def cancel_count(count, *, by) -> StockCount:
    count = StockCount.objects.select_for_update(of=("self",)).get(pk=count.pk)
    if count.status not in (CountStatus.OPEN, CountStatus.SUBMITTED):
        raise StockError(_("الجرد ده اتقفل."))
    count.status = CountStatus.CANCELLED
    count._history_user = by
    count.save(update_fields=["status", "updated_at"])
    return count


# --- what the stores hold ----------------------------------------------------------------------------------------


def stock_value(org) -> Decimal:
    """The value of everything on the shelves: what the inventory account must hold."""
    return StockBalance.objects.filter(organization=org).aggregate(s=Sum("value"))["s"] or ZERO


def on_hand(item, store=None) -> Decimal:
    rows = StockBalance.objects.filter(item=item)
    if store is not None:
        rows = rows.filter(warehouse=store)
    return rows.aggregate(s=Sum("quantity"))["s"] or Decimal(0)


def low_stock(org) -> list:
    """Active items at or under their reorder level (all stores together)."""
    items = (
        StockItem.objects.filter(organization=org, is_active=True, reorder_level__gt=0)
        .annotate(held=Sum("balances__quantity"))
        .order_by("name")
    )
    out = []
    for item in items:
        item.held = item.held or Decimal(0)
        if item.held <= item.reorder_level:
            out.append(item)
    return out


def expiring(org, days=None, today=None) -> list:
    """Lots that expire within `days` (or already did) and still have stock, soonest first."""
    today = today or ledger.today()
    days = InventorySettings.for_org(org).expiry_alert_days if days is None else days
    return list(
        StockBatch.objects.filter(
            organization=org, quantity__gt=0, expiry_date__isnull=False, expiry_date__lte=today + timedelta(days=days)
        )
        .select_related("item", "warehouse")
        .order_by("expiry_date", "id")
    )
