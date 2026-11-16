"""Operating room and admissions logic. Views stay thin: every change to an operation goes through here.

Room conflicts are prevented in one place: a booking or a move runs under `select_for_update()` on the `RoomDay` rows
it touches, then checks that no other operation still holding the room overlaps it.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.timeutils import CAIRO
from apps.obstetrics import services as obstetrics
from apps.obstetrics.models import Outcome

from .models import (
    ALLOWED_TRANSITIONS,
    HOLDS_ROOM,
    OPEN_STATUSES,
    Admission,
    AdmissionStatus,
    ChecklistItem,
    DeliveryMode,
    Newborn,
    NewbornStatus,
    NoteStatus,
    Operation,
    OperationConsumable,
    OperationEvent,
    OperationNote,
    OperationStatus,
    OperationTeamMember,
    ProcedureType,
    Room,
    RoomDay,
    RoomKind,
    TeamRole,
)

DEFAULT_CHECKLIST = [
    {"label": "إقرار العملية موقّع", "required": True},
    {"label": "المريضة صايمة", "required": True},
    {"label": "التحاليل اتراجعت", "required": True},
    {"label": "فصيلة الدم معروفة", "required": True},
    {"label": "كشف التخدير", "required": True},
    {"label": "المجوهرات وطلاء الأظافر اتشالوا", "required": False},
]
NOTE_FIELDS = ("anaesthesia", "findings", "procedure_details", "blood_loss_ml", "complications", "post_op_orders")
BED_KINDS = (RoomKind.INPATIENT, RoomKind.RECOVERY)


class TheatreError(Exception):
    """The change was refused. The message is written for the person at the screen."""


def today():
    return timezone.localtime(timezone.now(), CAIRO).date()


def _day(dt):
    return timezone.localtime(dt, CAIRO).date()


# --- booking and moving ----------------------------------------------------------------------------------------


def _lock_days(room, start, end) -> None:
    """Lock the room's day rows the operation touches (in date order, so two bookings can never deadlock)."""
    days = sorted({_day(start), _day(end - timedelta(seconds=1))})
    for day in days:
        RoomDay.objects.get_or_create(organization=room.organization, room=room, date=day)
    list(RoomDay.objects.select_for_update().filter(room=room, date__in=days).order_by("date"))


def _clash(room, start, end, exclude=None):
    """Another operation that still needs this room during [start, end)."""
    qs = Operation.objects.filter(room=room, status__in=HOLDS_ROOM, planned_start__lt=end, planned_end__gt=start)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs.select_related("patient").first()


def _check_window(room, start, end, exclude=None) -> None:
    other = _clash(room, start, end, exclude)
    if other is not None:
        when = timezone.localtime(other.planned_start, CAIRO)
        raise TheatreError(
            _("%(r)s مشغولة في الوقت ده: %(p)s (%(n)s) الساعة %(t)s.")
            % {"r": room.name, "p": other.procedure_name, "n": other.patient.full_name, "t": f"{when:%H:%M}"}
        )


def _duration(minutes, procedure) -> timedelta:
    try:
        minutes = int(minutes or procedure.duration_minutes)
    except (TypeError, ValueError) as e:
        raise TheatreError(_("اكتب المدة بالدقايق.")) from e
    if not 10 <= minutes <= 720:
        raise TheatreError(_("مدة العملية لازم تكون بين 10 دقايق و12 ساعة."))
    return timedelta(minutes=minutes)


@transaction.atomic
def book_operation(
    *, org, patient, procedure, room, surgeon, start, by, duration_minutes=None, is_emergency=False, pregnancy=None,
    notes="", emergency_case=None, price=None, notify=True,
) -> Operation:  # fmt: skip
    """Book an operation in a room. The procedure's price, checklist and usual team are copied onto it."""
    for obj in (patient, procedure, room, surgeon):
        if obj.organization_id != org.pk:
            raise TheatreError(_("فيه حاجة في الحجز مش بتاعة العيادة دي."))
    if not room.is_active or not room.is_theatre:
        raise TheatreError(_("اختار غرفة عمليات أو غرفة ولادة شغالة."))
    if not procedure.is_active:
        raise TheatreError(_("نوع العملية ده متوقف."))
    if not surgeon.is_active:
        raise TheatreError(_("الدكتور ده متوقف."))
    if start is None or timezone.is_naive(start):
        raise TheatreError(_("اختار ميعاد العملية."))
    if not is_emergency and start < timezone.now() - timedelta(minutes=5):
        raise TheatreError(_("الميعاد ده فات. لو الحالة طارئة علّم عليها طوارئ."))
    end = start + _duration(duration_minutes, procedure)
    _lock_days(room, start, end)
    _check_window(room, start, end)
    busy = Operation.objects.filter(
        patient=patient, status__in=OPEN_STATUSES, planned_start__lt=end, planned_end__gt=start
    )
    if busy.exists():
        raise TheatreError(_("المريضة عندها عملية تانية في نفس الوقت."))
    if pregnancy is None and procedure.delivery:
        pregnancy = obstetrics.active_pregnancy(patient)  # a delivery closes the pregnancy that is being followed
    if pregnancy is not None and pregnancy.patient_id != patient.pk:
        raise TheatreError(_("ملف الحمل ده مش بتاع المريضة دي."))

    op = Operation.objects.create(
        organization=org,
        patient=patient,
        pregnancy=pregnancy,
        procedure=procedure,
        procedure_name=procedure.name_ar,
        room=room,
        surgeon=surgeon,
        date=_day(start),
        planned_start=start,
        planned_end=end,
        is_emergency=is_emergency,
        price=procedure.package_price if price is None else price,
        fasting_hours=procedure.fasting_hours,
        notes=(notes or "").strip()[:300],
        emergency_case=emergency_case,
        created_by=by,
    )
    OperationEvent.objects.create(organization=org, operation=op, to_status=op.status, by=by)
    ChecklistItem.objects.bulk_create(
        ChecklistItem(
            organization=org, operation=op, label=str(item.get("label", ""))[:150],
            required=bool(item.get("required", True)), order=i,
        )
        for i, item in enumerate(procedure.checklist or DEFAULT_CHECKLIST)
        if item.get("label")
    )  # fmt: skip
    usual = {
        m.get("role"): m.get("fee") or 0 for m in (procedure.default_team or []) if m.get("role") in TeamRole.values
    }
    usual.setdefault(TeamRole.SURGEON, 0)
    for role, fee in usual.items():
        OperationTeamMember.objects.create(
            organization=org, operation=op, role=role, fee=Decimal(str(fee)),
            doctor=surgeon if role == TeamRole.SURGEON else None,
        )  # fmt: skip
    if notify:
        from . import notifications

        transaction.on_commit(lambda: notifications.on_booked(op, by=by))
    return op


@transaction.atomic
def move_operation(op, *, by, start=None, room=None, duration_minutes=None, reason="") -> Operation:
    """Change an operation's time, room or length. Only before it starts; the change is kept in its history."""
    op = Operation.objects.select_for_update(of=("self",)).select_related("room", "procedure", "patient").get(pk=op.pk)
    if op.status not in (OperationStatus.SCHEDULED, OperationStatus.ADMITTED):
        raise TheatreError(_("العملية دي مينفعش يتغير ميعادها دلوقتي."))
    new_room = room or op.room
    if new_room.organization_id != op.organization_id or not new_room.is_active or not new_room.is_theatre:
        raise TheatreError(_("اختار غرفة عمليات أو غرفة ولادة شغالة."))
    new_start = start or op.planned_start
    if timezone.is_naive(new_start):
        raise TheatreError(_("اختار ميعاد العملية."))
    length = _duration(duration_minutes, op.procedure) if duration_minutes else op.planned_end - op.planned_start
    new_end = new_start + length
    if (new_room.pk, new_start, new_end) == (op.room_id, op.planned_start, op.planned_end):
        return op
    if start is not None and not op.is_emergency and new_start < timezone.now() - timedelta(minutes=5):
        raise TheatreError(_("الميعاد ده فات."))
    # lock the old and the new room-days in one fixed order
    for locked_room, s, e in sorted(
        [(op.room, op.planned_start, op.planned_end), (new_room, new_start, new_end)], key=lambda x: (x[0].pk, x[1])
    ):
        _lock_days(locked_room, s, e)
    _check_window(new_room, new_start, new_end, exclude=op)
    old = timezone.localtime(op.planned_start, CAIRO)
    op.room, op.planned_start, op.planned_end, op.date = new_room, new_start, new_end, _day(new_start)
    op._history_user = by
    op.save(update_fields=["room", "planned_start", "planned_end", "date", "updated_at"])
    note = _("اتنقلت من %(d)s") % {"d": f"{old:%d/%m %H:%M}"}
    OperationEvent.objects.create(
        organization=op.organization, operation=op, from_status=op.status, to_status=op.status, by=by,
        notes=f"{note}. {reason}".strip()[:300],
    )  # fmt: skip
    from . import notifications

    transaction.on_commit(lambda: notifications.on_changed(op, by=by))
    return op


# --- the day of the operation ----------------------------------------------------------------------------------

_ORDER = {
    OperationStatus.SCHEDULED: 1,
    OperationStatus.ADMITTED: 2,
    OperationStatus.IN_THEATRE: 3,
    OperationStatus.RECOVERY: 4,
    OperationStatus.DONE: 5,
}
_STAMP = {
    OperationStatus.ADMITTED: "admitted_at",
    OperationStatus.IN_THEATRE: "started_at",
    OperationStatus.RECOVERY: "ended_at",
    OperationStatus.DONE: "done_at",
}


def open_required_items(op) -> list[ChecklistItem]:
    return list(op.checklist.filter(required=True, done=False))


@transaction.atomic
def transition(op, to_status, *, by, notes="", now=None) -> Operation:
    """Move an operation one step (or back one step). Into the theatre, the required checklist items must be done,
    unless the operation is an emergency: then it goes in and what was skipped is written on the event. Finishing a
    delivery writes the birth to the pregnancy file."""
    op = Operation.objects.select_for_update(of=("self",)).select_related("procedure", "patient").get(pk=op.pk)
    if to_status not in ALLOWED_TRANSITIONS.get(op.status, set()):
        raise TheatreError(
            _("مينفعش العملية تتنقل من «%(a)s» لـ«%(b)s».")
            % {"a": op.get_status_display(), "b": dict(OperationStatus.choices).get(to_status, to_status)}
        )
    now = now or timezone.now()
    old = op.status
    backwards = _ORDER.get(to_status, 9) < _ORDER.get(old, 0)
    notes = (notes or "").strip()
    if to_status == OperationStatus.IN_THEATRE and not backwards:
        missing = open_required_items(op)
        if missing and not op.is_emergency:
            raise TheatreError(
                _("لسه في قايمة ما قبل العملية: %(l)s.") % {"l": "، ".join(item.label for item in missing)}
            )
        if missing:
            skipped = _("طوارئ، دخلت من غير: %(l)s") % {"l": "، ".join(item.label for item in missing)}
            notes = f"{skipped}. {notes}".strip()
    if to_status == OperationStatus.DONE and op.is_delivery and not op.delivery_closed:
        close_delivery(op, by=by)
        op.delivery_closed = True
    if to_status in (OperationStatus.CANCELLED, OperationStatus.POSTPONED):
        op.cancelled_at, op.cancel_reason = now, notes[:200]
    if backwards:
        setattr(op, _STAMP[old], None)  # forget the time of the step being undone
    elif to_status in _STAMP:
        setattr(op, _STAMP[to_status], now)
    op.status = to_status
    op._history_user = by
    op.save()
    OperationEvent.objects.create(
        organization=op.organization, operation=op, from_status=old, to_status=to_status, by=by, notes=notes[:300]
    )
    if to_status in (OperationStatus.CANCELLED, OperationStatus.POSTPONED):
        from . import notifications

        transaction.on_commit(lambda: notifications.on_cancelled(op))
    return op


@transaction.atomic
def rebook_postponed(op, *, start, by, room=None, duration_minutes=None) -> Operation:
    """A postponed operation gets its new date: a new operation, linked from the old one (which stays as it was)."""
    op = (
        Operation.objects.select_for_update(of=("self",))
        .select_related("procedure", "room", "patient", "surgeon")
        .get(pk=op.pk)
    )
    if op.status != OperationStatus.POSTPONED or op.postponed_to_id:
        raise TheatreError(_("العملية دي مش متأجلة، أو اتحجزلها ميعاد جديد قبل كده."))
    length = duration_minutes or int((op.planned_end - op.planned_start).total_seconds() // 60)
    new = book_operation(
        org=op.organization, patient=op.patient, procedure=op.procedure, room=room or op.room, surgeon=op.surgeon,
        start=start, by=by, duration_minutes=length, pregnancy=op.pregnancy, notes=op.notes, price=op.price,
        emergency_case=op.emergency_case,
    )  # fmt: skip
    op.postponed_to = new
    op.save(update_fields=["postponed_to", "updated_at"])
    return new


# --- deliveries and newborns -----------------------------------------------------------------------------------


def living_newborns(op) -> list[Newborn]:
    return list(op.newborns.filter(is_removed=False).order_by("order", "id"))


@transaction.atomic
def add_newborn(
    op, *, sex, by, weight_g=None, apgar_1=None, apgar_5=None, born_at=None, status=NewbornStatus.WELL, notes=""
) -> Newborn:
    op = Operation.objects.select_for_update(of=("self",)).select_related("procedure").get(pk=op.pk)
    if not op.is_delivery:
        raise TheatreError(_("العملية دي مش ولادة."))
    if op.delivery_closed or op.status in (OperationStatus.CANCELLED, OperationStatus.POSTPONED):
        raise TheatreError(_("الولادة دي اتقفلت، مينفعش يتضاف مولود."))
    baby = Newborn(
        organization=op.organization, operation=op, order=op.newborns.filter(is_removed=False).count() + 1, sex=sex,
        weight_g=weight_g, apgar_1=apgar_1, apgar_5=apgar_5, born_at=born_at or timezone.now(), status=status,
        notes=(notes or "").strip()[:300],
    )  # fmt: skip
    baby._history_user = by
    baby.full_clean(exclude=["organization", "operation"])
    baby.save()
    return baby


@transaction.atomic
def remove_newborn(baby, *, by) -> None:
    """Recorded by mistake: kept, and out of every count. Not once the birth is in the pregnancy file."""
    if Operation.objects.filter(pk=baby.operation_id, delivery_closed=True).exists():  # asked fresh, not from memory
        raise TheatreError(_("الولادة دي اتقفلت."))
    baby.is_removed = True
    baby._history_user = by
    baby.save(update_fields=["is_removed", "updated_at"])


def close_delivery(op, *, by) -> None:
    """Write the birth to the mother's pregnancy file: the followed pregnancy is closed with how it ended, and her
    obstetric history gets its row (so G/P/A/L move by themselves). Called when a delivery operation is finished."""
    babies = living_newborns(op)
    if not babies:
        raise TheatreError(_("سجّل المولود الأول، وبعدين اقفل العملية."))
    pregnancy = op.pregnancy
    if pregnancy is None or not pregnancy.is_active:
        return  # nothing is being followed here (or the file was closed by hand): the newborns are the record
    alive = [b for b in babies if b.is_alive]
    if not alive:
        outcome = Outcome.STILLBIRTH
    else:
        outcome = Outcome.CESAREAN if op.procedure.delivery == DeliveryMode.CESAREAN else Outcome.VAGINAL
    first = babies[0]
    clinic = op.organization.settings.clinic_name_ar or op.organization.name_ar
    try:
        obstetrics.close_pregnancy(
            pregnancy, outcome=outcome, ended_on=_day(first.born_at), by=by, living_children=len(alive),
            baby_sex=first.sex, baby_weight_g=first.weight_g, place=clinic,
        )  # fmt: skip
    except obstetrics.ObstetricError as e:
        raise TheatreError(str(e)) from e


# --- checklist, team, consumables ------------------------------------------------------------------------------


def _editable(op) -> None:
    """The operation as it is in the database now (the caller's copy may be older than a cancellation)."""
    status = Operation.objects.values_list("status", flat=True).get(pk=op.pk)
    if status in (OperationStatus.CANCELLED, OperationStatus.POSTPONED):
        raise TheatreError(_("العملية دي ملغية أو متأجلة."))


@transaction.atomic
def toggle_checklist(item, *, by) -> ChecklistItem:
    _editable(item.operation)
    item.done = not item.done
    item.done_by, item.done_at = (by, timezone.now()) if item.done else (None, None)
    item.save(update_fields=["done", "done_by", "done_at", "updated_at"])
    return item


@transaction.atomic
def save_team_member(op, *, role, by, member=None, doctor=None, name="", fee=0) -> OperationTeamMember:
    _editable(op)
    if role not in TeamRole.values:
        raise TheatreError(_("اختار الدور."))
    if doctor is not None and doctor.organization_id != op.organization_id:
        raise TheatreError(_("الدكتور ده مش في العيادة دي."))
    try:
        fee = Decimal(str(fee or 0))
    except Exception as e:
        raise TheatreError(_("اكتب الأتعاب رقم.")) from e
    if fee < 0:
        raise TheatreError(_("الأتعاب مينفعش تكون بالسالب."))
    member = member or OperationTeamMember(organization=op.organization, operation=op)
    member.role, member.doctor, member.name, member.fee = role, doctor, (name or "").strip()[:120], fee
    member._history_user = by
    member.save()
    return member


@transaction.atomic
def add_consumable(op, *, by, name="", quantity=1, unit="", item=None) -> OperationConsumable:
    """Write down something used in the case. A store `item` leaves the store now (the theatre cabinet first, the
    main store if the cabinet does not hold enough) and, when it has a sale price, goes on the patient's account."""
    _editable(op)
    name = " ".join((name or "").split())
    if item is None and not name:
        raise TheatreError(_("اكتب اسم الصنف."))
    try:
        quantity = Decimal(str(1 if quantity in (None, "") else quantity)).quantize(Decimal("0.01"))
    except Exception as e:
        raise TheatreError(_("اكتب الكمية رقم.")) from e
    if quantity <= 0:
        raise TheatreError(_("الكمية لازم تكون أكبر من صفر."))
    if item is None:
        return OperationConsumable.objects.create(
            organization=op.organization, operation=op, name=name[:120], quantity=quantity,
            unit=(unit or "").strip()[:20], added_by=by,
        )  # fmt: skip
    return _use_stock_item(op, item, quantity, by)


def _use_stock_item(op, item, quantity, by) -> OperationConsumable:
    from apps.inventory import services as stock
    from apps.invoicing import services as invoicing
    from apps.invoicing.models import ChargeKind
    from apps.ledger import chart
    from apps.ledger.money import money

    org = op.organization
    if item.organization_id != org.pk or not item.is_active:
        raise TheatreError(_("الصنف ده مش متاح."))
    stores = [stock.warehouse(org, "theatre"), stock.warehouse(org, "main")]
    store = next((s for s in stores if s.is_active and stock.on_hand(item, s) >= quantity), None)
    if store is None:
        raise TheatreError(_("مفيش رصيد كفاية من «%(i)s» في دولاب العمليات ولا المخزن الرئيسي.") % {"i": item.name})
    with transaction.atomic():
        try:
            doc = stock.issue(
                org, warehouse=store, lines=[(item, quantity)], cost_center=chart.cost_center(org, "theatre"), by=by,
                operation=op, notes=op.procedure_name,
            )  # fmt: skip
        except stock.StockError as e:
            raise TheatreError(str(e)) from e
        charge = None
        if item.sale_price > 0:
            whole = quantity == quantity.to_integral_value() and quantity <= 999
            try:
                charge = invoicing.add_charge(
                    op.patient, by=by, kind=ChargeKind.SUPPLY, operation=op,
                    description=item.name if whole else f"{item.name} x {quantity.normalize():f}",
                    quantity=int(quantity) if whole else 1,
                    unit_price=item.sale_price if whole else money(item.sale_price * quantity),
                )  # fmt: skip
            except invoicing.InvoiceError as e:
                raise TheatreError(str(e)) from e
        return OperationConsumable.objects.create(
            organization=org, operation=op, name=item.name[:120], quantity=quantity, unit=item.unit[:20], item=item,
            stock_move=doc.moves.get(), charge=charge, added_by=by,
        )  # fmt: skip


@transaction.atomic
def remove_consumable(consumable, *, by) -> None:
    """Take a line off. A store item goes back on the shelf at the value it left with and its charge is cancelled
    (not once the patient was invoiced for it); the line stays on record. Free text is simply removed."""
    from apps.inventory import services as stock
    from apps.invoicing import services as invoicing

    consumable = OperationConsumable.objects.select_for_update(of=("self",)).get(pk=consumable.pk)
    _editable(consumable.operation)
    if consumable.is_removed:
        return
    if consumable.item_id is None:
        consumable.delete()
        return
    try:
        if consumable.charge_id:
            invoicing.cancel_charge(consumable.charge, by=by)
        if consumable.stock_move_id:
            stock.return_issue(consumable.stock_move, by=by)
    except (invoicing.InvoiceError, stock.StockError) as e:
        raise TheatreError(str(e)) from e
    consumable.is_removed = True
    consumable._history_user = by
    consumable.save(update_fields=["is_removed", "updated_at"])


def kit_rows(procedure) -> list:
    """The procedure's usual supplies as [(StockItem, Decimal quantity)] (items that were switched off are skipped)."""
    from apps.inventory.models import StockItem

    wanted = {}
    for row in procedure.kit or []:
        try:
            wanted[int(row.get("item"))] = Decimal(str(row.get("quantity") or 1))
        except (TypeError, ValueError, ArithmeticError):
            continue
    items = StockItem.objects.filter(organization=procedure.organization, pk__in=wanted, is_active=True)
    return [(item, wanted[item.pk]) for item in items.order_by("name")]


@transaction.atomic
def issue_kit(op, *, by) -> list[OperationConsumable]:
    """Issue the procedure's usual supplies in one step: every kit item the operation does not already have. Then the
    lines are corrected to what was really used (take one off, add another)."""
    have = set(op.consumables.filter(is_removed=False, item__isnull=False).values_list("item_id", flat=True))
    todo = [(item, quantity) for item, quantity in kit_rows(op.procedure) if item.pk not in have]
    if not todo:
        raise TheatreError(_("مفيش مستلزمات معتادة ناقصة للعملية دي."))
    return [add_consumable(op, item=item, quantity=quantity, by=by) for item, quantity in todo]


# --- the surgeon's note ----------------------------------------------------------------------------------------


def latest_note(op) -> OperationNote | None:
    return op.op_notes.order_by("-revision").first()


@transaction.atomic
def draft_note(op, *, by) -> OperationNote:
    """The note being written: the open draft, or a new one (a new revision that starts from the signed text)."""
    _editable(op)
    last = latest_note(op)
    if last is not None and not last.is_signed:
        return last
    note = OperationNote(organization=op.organization, operation=op, revision=(last.revision + 1) if last else 0)
    if last is not None:
        for field in NOTE_FIELDS:
            setattr(note, field, getattr(last, field))
    note._history_user = by
    note.save()
    return note


@transaction.atomic
def save_note(note, values: dict, *, by) -> OperationNote:
    note = OperationNote.objects.select_for_update().get(pk=note.pk)
    if note.is_signed:
        raise TheatreError(_("التقرير اتوقّع، مينفعش يتعدل. اعمل نسخة معدّلة."))
    for field in NOTE_FIELDS:
        if field in values:
            value = values[field]
            if field == "blood_loss_ml":
                value = str(value or "").strip()
                if value and (not value.isdigit() or int(value) > 20000):
                    raise TheatreError(_("اكتب الدم المفقود بالملّي، رقم."))
                value = int(value) if value else None
            else:
                value = (value or "").strip()[: 120 if field == "anaesthesia" else 20000]
            setattr(note, field, value)
    note._history_user = by
    note.save()
    return note


@transaction.atomic
def sign_note(note, *, by) -> OperationNote:
    """Sign the note: from now on it never changes (a correction is a new revision)."""
    note = OperationNote.objects.select_for_update().get(pk=note.pk)
    if note.is_signed:
        return note
    if not (note.procedure_details or note.findings):
        raise TheatreError(_("اكتب اللي اتعمل أو المشاهدات قبل التوقيع."))
    note.status, note.signed_by, note.signed_at = NoteStatus.SIGNED, by, timezone.now()
    note._history_user = by
    note.save()
    return note


# --- admissions ------------------------------------------------------------------------------------------------


def beds_free(room) -> int:
    used = Admission.objects.filter(room=room, status=AdmissionStatus.ADMITTED).count()
    return max(room.beds - used, 0)


@transaction.atomic
def admit(
    *, org, patient, room, by, operation=None, emergency_case=None, doctor=None, reason="", now=None
) -> Admission:
    """Put a patient in a room. One open stay per patient, and never more patients than beds (the room row is
    locked while its beds are counted)."""
    if patient.organization_id != org.pk or room.organization_id != org.pk:
        raise TheatreError(_("المريضة أو الغرفة مش في العيادة دي."))
    room = Room.objects.select_for_update().get(pk=room.pk)
    if not room.is_active or room.kind not in BED_KINDS:
        raise TheatreError(_("اختار غرفة إقامة أو إفاقة شغالة."))
    if Admission.objects.filter(patient=patient, status=AdmissionStatus.ADMITTED).exists():
        raise TheatreError(_("المريضة مقيمة في غرفة دلوقتي."))
    if beds_free(room) < 1:
        raise TheatreError(_("%(r)s مفيهاش سرير فاضي.") % {"r": room.name})
    return Admission.objects.create(
        organization=org,
        patient=patient,
        room=room,
        operation=operation,
        emergency_case=emergency_case,
        doctor=doctor or (operation.surgeon if operation else None),
        admitted_at=now or timezone.now(),
        nightly_rate=room.nightly_rate,
        reason=(reason or "").strip()[:200],
        admitted_by=by,
    )


@transaction.atomic
def discharge(admission, *, by, summary="", instructions="", now=None) -> Admission:
    admission = Admission.objects.select_for_update().get(pk=admission.pk)
    if admission.status != AdmissionStatus.ADMITTED:
        raise TheatreError(_("الإقامة دي مقفولة."))
    now = now or timezone.now()
    if now < admission.admitted_at:
        raise TheatreError(_("وقت الخروج قبل وقت الدخول."))
    admission.status, admission.discharged_at, admission.discharged_by = AdmissionStatus.DISCHARGED, now, by
    admission.discharge_summary, admission.discharge_instructions = (
        (summary or "").strip(),
        (instructions or "").strip(),
    )
    admission._history_user = by
    admission.save()
    return admission


@transaction.atomic
def cancel_admission(admission, *, by) -> Admission:
    """Entered by mistake: the stay is cancelled (kept on record, charged nothing)."""
    admission = Admission.objects.select_for_update().get(pk=admission.pk)
    if admission.status != AdmissionStatus.ADMITTED:
        raise TheatreError(_("الإقامة دي مقفولة."))
    admission.status, admission.discharged_at, admission.discharged_by = AdmissionStatus.CANCELLED, timezone.now(), by
    admission._history_user = by
    admission.save()
    return admission


# --- what the screens read -------------------------------------------------------------------------------------


def operations_on(org, day, rooms=None) -> list[Operation]:
    qs = Operation.objects.for_org(org).filter(date=day).exclude(status=OperationStatus.POSTPONED)
    if rooms is not None:
        qs = qs.filter(room__in=rooms)
    return list(qs.select_related("patient", "room", "surgeon", "procedure").order_by("planned_start", "id"))


def waiting_for_a_date(org) -> list[Operation]:
    """Postponed operations that still have no new date."""
    qs = Operation.objects.for_org(org).filter(status=OperationStatus.POSTPONED, postponed_to__isnull=True)
    return list(qs.select_related("patient", "room", "surgeon", "procedure").order_by("-cancelled_at")[:50])


def admitted_now(org) -> list[Admission]:
    qs = Admission.objects.for_org(org).filter(status=AdmissionStatus.ADMITTED)
    return list(qs.select_related("patient", "room", "doctor", "operation").order_by("room__order", "admitted_at"))


def ensure_defaults(org) -> None:
    """A clinic that has no rooms and no procedures yet gets a small starting set (it edits them in Settings)."""
    if not Room.objects.filter(organization=org).exists():
        Room.objects.bulk_create(
            Room(organization=org, name=name, kind=kind, nightly_rate=rate, order=i)
            for i, (name, kind, rate) in enumerate(DEFAULT_ROOMS)
        )
    if not ProcedureType.objects.filter(organization=org).exists():
        ProcedureType.objects.bulk_create(
            ProcedureType(
                organization=org,
                name_ar=name,
                name_en=en,
                delivery=delivery,
                duration_minutes=minutes,
                package_price=price,
                checklist=DEFAULT_CHECKLIST,
                order=i,
                default_team=[{"role": role, "fee": "0"} for role in team],
            )
            for i, (name, en, delivery, minutes, price, team) in enumerate(DEFAULT_PROCEDURES)
        )


DEFAULT_ROOMS = [
    ("غرفة العمليات", RoomKind.OPERATING, 0),
    ("غرفة الولادة", RoomKind.DELIVERY, 0),
    ("الإفاقة", RoomKind.RECOVERY, 0),
    ("غرفة 1", RoomKind.INPATIENT, 0),
    ("غرفة 2", RoomKind.INPATIENT, 0),
]
_FULL = (TeamRole.SURGEON, TeamRole.ASSISTANT, TeamRole.ANAESTHETIST, TeamRole.NURSE)
_BIRTH = (*_FULL, TeamRole.PAEDIATRICIAN)
DEFAULT_PROCEDURES = [
    (
        "ولادة طبيعية",
        "Vaginal delivery",
        DeliveryMode.VAGINAL,
        120,
        0,
        (TeamRole.SURGEON, TeamRole.NURSE, TeamRole.PAEDIATRICIAN),
    ),
    ("ولادة قيصرية", "Cesarean section", DeliveryMode.CESAREAN, 60, 0, _BIRTH),
    ("كحت وتنظيف", "D&C", "", 30, 0, _FULL),
    ("ربط عنق الرحم", "Cervical cerclage", "", 30, 0, _FULL),
    ("منظار رحمي", "Hysteroscopy", "", 45, 0, _FULL),
    ("منظار بطني", "Laparoscopy", "", 90, 0, _FULL),
    ("استئصال رحم", "Hysterectomy", "", 150, 0, _FULL),
]
