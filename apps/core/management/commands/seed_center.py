"""A realistic month in the life of the center, made through the real services (plan 14 §14.10):

    python manage.py seed_center [--org demo-clinic] [--days 30] [--seed 7]

Every day: visits (paid, unpaid, with services, a discount now and then), the cashier's shift closed into the safe.
Through the month: stock bought and paid for (cash, transfer, post-dated cheques with tax withheld, cheques clearing),
operations with a team, supplies from the store and a stay, deposits and invoices, rent and salaries, a contracted
company, doctors paid, a stock count. At the end the books are verified.

For a demo or a test database only: nothing it makes can be removed (money documents never are). It moves the clock
back with freezegun (a development dependency), cancels every message its bookings queued (nothing is sent), and
refuses to run twice.
"""

import random
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.timeutils import CAIRO
from apps.organizations.models import Membership, Organization, Role

D = Decimal
MARK = "seed_center"
ITEMS = [  # name, unit, reorder level, has expiry, patient price, cost
    ("شاش معقم", "علبة", 10, False, 0, 35),
    ("قفازات معقمة", "زوج", 40, False, 0, 6),
    ("خيط جراحي فيكريل", "قطعة", 8, False, 150, 95),
    ("أوكسيتوسين 10 وحدات", "أمبول", 15, True, 0, 18),
    ("محلول ملح 500 مل", "زجاجة", 20, True, 0, 22),
    ("كانيولا", "قطعة", 25, False, 0, 9),
]
PRICES = {"ولادة طبيعية": 9000, "ولادة قيصرية": 16000, "كحت وتنظيف": 5000, "منظار رحمي": 7000}


class Command(BaseCommand):
    help = "Seed a realistic month of activity through the real services (demo / test databases only)."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="demo-clinic")
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--seed", type=int, default=7)

    def handle(self, *args, **o):
        try:
            from freezegun import freeze_time
        except ImportError as e:
            raise CommandError("seed_center needs freezegun (a development dependency).") from e
        try:
            org = Organization.objects.get(slug=o["org"])
        except Organization.DoesNotExist as e:
            raise CommandError(f"No organization with slug {o['org']!r}") from e
        from apps.purchasing.models import Supplier

        if Supplier.objects.filter(organization=org, notes=MARK).exists():
            raise CommandError("This clinic was already seeded with seed_center.")
        owner = Membership.objects.filter(organization=org, role=Role.OWNER, is_active=True).first()
        if owner is None:
            raise CommandError("The clinic needs an owner first (seed_org / create_user).")
        self.org, self.owner_m, self.user = org, owner, owner.user
        self.rng = random.Random(o["seed"])  # noqa: S311 (demo data, repeatable on purpose)
        self.freeze = freeze_time
        from apps.notifications.models import ScheduledMessage

        last_message = ScheduledMessage.objects.order_by("-pk").values_list("pk", flat=True).first() or 0

        from apps.patients.management.commands.seed_demo import MARKER
        from apps.patients.models import Patient

        if Patient.objects.filter(organization=org, notes=MARKER).count() < 10:  # seed_demo adds 30 every time
            call_command("seed_demo", org=org.slug)
        call_command("seed_pregnancies", org=org.slug)
        call_command("seed_services", org=org.slug)
        today = timezone.localtime(timezone.now(), CAIRO).date()
        days = [today - timedelta(days=n) for n in range(o["days"], 0, -1)]
        with self.freeze(self._at(days[0], 8), tick=True):
            self._setup()
        for n, day in enumerate(days):
            if day.weekday() == 4:  # Friday
                continue
            with self.freeze(self._at(day, 10), tick=True):
                self._visits(day)
                if n % 7 == 1:
                    self._buy(day)
                if n % 7 == 3:
                    self._pay_suppliers(day)
                if n % 4 == 2:
                    self._operation(day)
                self._discharge(day)
                self._monthly(day)
            with self.freeze(self._at(day, 17), tick=True):
                self._clear_cheques(day)
                self._close_shift()
        with self.freeze(self._at(days[-1], 18), tick=True):
            self._month_end()

        cancelled = ScheduledMessage.objects.filter(pk__gt=last_message, status="pending").update(
            status="cancelled", status_reason=MARK
        )
        from apps.ledger import verify

        problems = verify.verify(org)
        for problem in problems:
            self.stdout.write(self.style.ERROR(f"  [{problem.code}] {problem.message}"))
        if problems:
            raise CommandError("The seeded books do not verify.")
        self.stdout.write(self.style.SUCCESS(
            f"{org.slug}: {len(days)} days seeded, books verified, {cancelled} queued message(s) cancelled."
        ))  # fmt: skip

    # ------------------------------------------------------------------ helpers

    def _at(self, day, hour):
        return datetime.combine(day, time(hour, 0), tzinfo=CAIRO)

    def _acc(self, key):
        from apps.ledger import chart

        return chart.account(self.org, key)

    def _cc(self, key):
        from apps.ledger import chart

        return chart.cost_center(self.org, key)

    # ------------------------------------------------------------------ the first morning

    def _setup(self):
        from apps.doctors.models import Doctor, VisitType
        from apps.inventory import services as stock
        from apps.inventory.models import StockItem
        from apps.invoicing import services as invoicing
        from apps.ledger import chart
        from apps.patients.management.commands.seed_demo import MARKER
        from apps.patients.models import Patient
        from apps.purchasing import services as purchasing
        from apps.purchasing.models import TaxCode
        from apps.theatre import services as theatre
        from apps.theatre.models import ProcedureType, Room, RoomKind
        from apps.treasury import services as treasury
        from apps.treasury.models import ExpenseTemplate, VoucherKind

        org, user = self.org, self.user
        chart.ensure(org)
        treasury.ensure(org)
        purchasing.ensure_defaults(org)
        stock.ensure_defaults(org)
        theatre.ensure_defaults(org)
        self.bank = treasury.treasury(org, "bank_main")
        self.safe = treasury.treasury(org, "main_safe")
        self.drawer = treasury.treasury(org, "reception_drawer")
        self.main, self.cabinet = stock.warehouse(org, "main"), stock.warehouse(org, "theatre")
        self.taxes = {t.system_key: t for t in TaxCode.objects.filter(organization=org)}
        self.doctors = list(Doctor.objects.filter(organization=org, is_active=True)[:3])
        if not self.doctors:
            raise CommandError("The clinic has no doctor (run seed_org).")
        for i, doctor in enumerate(self.doctors[1:], start=1):  # the first doctor is the owner: no share
            if not doctor.visit_share_percent:
                doctor.visit_share_percent = D("30") if i == 1 else D("40")
                doctor.save(update_fields=["visit_share_percent", "updated_at"])
        self.visit_types = {
            d.pk: list(VisitType.objects.filter(organization=org, doctor=d, is_active=True, is_system=False))
            for d in self.doctors
        }
        self.doctors = [d for d in self.doctors if self.visit_types[d.pk]]
        self.patients = list(Patient.objects.filter(organization=org, notes=MARKER, is_active=True))
        self.women = [p for p in self.patients if p.gender == "female"]
        from apps.center.models import Service

        self.services = list(Service.objects.filter(organization=org, is_active=True)[:6])

        # money to start with
        today = timezone.localdate()
        treasury.create_voucher(
            org, kind=VoucherKind.RECEIPT, treasury=self.bank, amount=D("400000"), date=today, by=user,
            description="رأس المال", counter_account=self._acc("capital"), party_name="صاحب المركز",
        )  # fmt: skip
        treasury.create_voucher(
            org, kind=VoucherKind.TRANSFER, treasury=self.bank, to_treasury=self.safe, amount=D("30000"), date=today,
            by=user, description="سحب للخزينة الرئيسية",
        )  # fmt: skip

        # the store
        self.items = {}
        opening = []
        for name, unit, level, expiry, price, cost in ITEMS:
            item, _made = StockItem.objects.get_or_create(
                organization=org, name=name,
                defaults={"unit": unit, "reorder_level": D(level), "track_expiry": expiry, "sale_price": D(price)},
            )  # fmt: skip
            self.items[name] = (item, D(cost))
            opening.append(
                (item, str(level * 3), str(cost), today + timedelta(days=300) if item.track_expiry else None)
            )
        stock.opening(org, warehouse=self.main, lines=opening, by=user)
        stock.transfer(
            org, source=self.main, target=self.cabinet, by=user,
            lines=[(item, str(int(item.reorder_level))) for item, _cost in self.items.values()],
        )  # fmt: skip

        # suppliers, a contracted company, recurring expenses
        self.suppliers = [
            purchasing.save_supplier(
                org,
                by=user,
                name=name,
                notes=MARK,
                tax_registration_number=tax,
                payment_terms_days=terms,
                default_withholding=self.taxes[wht],
                phone=phone,
            )
            for name, tax, terms, wht, phone in [
                ("النيل للمستلزمات الطبية", "412-556-901", 30, "wht1", "0223456789"),
                ("الدلتا للأدوية", "305-118-774", 15, "wht1", "0227654321"),
                ("الصفا للصيانة والخدمات", "220-904-316", 0, "wht3", "01001234567"),
            ]
        ]
        self.payer = invoicing.save_payer(org, name="شركة مصر للتأمين الطبي", by=user, kind="insurance",
                                          default_percent=D("80"))  # fmt: skip
        for patient in self.patients[:3]:
            invoicing.set_coverage(
                patient, payer=self.payer, percent=D("80"), member_no=f"M-{patient.file_number}", by=user
            )
        for name, key, amount, day in [("الإيجار", "rent", 25000, 3), ("المرتبات", "salaries", 60000, 28),
                                       ("الكهربا والمياه", "utilities", 4500, 12)]:  # fmt: skip
            account = self._acc(key) if self._has(key) else self._acc("sundry_expenses")
            ExpenseTemplate.objects.get_or_create(
                organization=org, name=name,
                defaults={"treasury": self.bank, "counter_account": account, "cost_center": self._cc("admin"),
                          "amount": D(amount), "day_of_month": day},
            )  # fmt: skip

        # prices for the theatre and the rooms
        for procedure in ProcedureType.objects.filter(organization=org):
            if not procedure.package_price and procedure.name_ar in PRICES:
                procedure.package_price = D(PRICES[procedure.name_ar])
                if not procedure.kit:
                    procedure.kit = [
                        {"item": self.items["شاش معقم"][0].pk, "quantity": "2"},
                        {"item": self.items["قفازات معقمة"][0].pk, "quantity": "4"},
                        {"item": self.items["خيط جراحي فيكريل"][0].pk, "quantity": "1"},
                    ]
                procedure.save()
        Room.objects.filter(organization=org, kind=RoomKind.INPATIENT, nightly_rate=0).update(nightly_rate=D("1200"))
        self.procedures = list(ProcedureType.objects.filter(organization=org, package_price__gt=0, delivery=""))
        self.theatre_room = Room.objects.filter(organization=org, kind=RoomKind.OPERATING, is_active=True).first()
        self.ward = Room.objects.filter(organization=org, kind=RoomKind.INPATIENT, is_active=True).first()
        self.stays = []

    def _has(self, key) -> bool:
        from apps.ledger.models import Account

        return Account.objects.filter(organization=self.org, system_key=key).exists()

    # ------------------------------------------------------------------ every day

    def _visits(self, day):
        from apps.billing import services as billing
        from apps.center import services as center
        from apps.invoicing import services as invoicing
        from apps.scheduling import services as booking
        from apps.scheduling.models import Appointment, AppointmentStatus

        rng, user = self.rng, self.user
        busy = set(Appointment.objects.filter(organization=self.org, date=day).values_list("patient_id", flat=True))
        for doctor in self.doctors:
            pool = [p for p in self.patients if p.pk not in busy]
            for patient in rng.sample(pool, min(rng.randint(4, 7), len(pool))):
                busy.add(patient.pk)
                try:
                    appt = booking.walk_in(
                        patient=patient, doctor=doctor, visit_type=rng.choice(self.visit_types[doctor.pk]), by=user,
                        overbook=True,
                    )  # fmt: skip
                except booking.BookingError:
                    continue  # the doctor does not work today
                if self.services and rng.random() < 0.3:
                    center.order_service(appt, rng.choice(self.services), by=user)
                if appt.price and rng.random() < 0.06:
                    try:
                        invoicing.set_visit_discount(
                            appt, amount=D(int(appt.price * D("0.2"))), reason="حالة خاصة", by=user,
                            membership=self.owner_m,
                        )  # fmt: skip
                    except invoicing.InvoiceError:
                        pass
                due = billing.balance(appt).due
                roll = rng.random()
                if due > 0 and roll < 0.9:
                    paid = due if roll < 0.82 else D(int(due / 2))
                    method = rng.choices(["cash", "card", "instapay"], weights=[7, 2, 1])[0]
                    if paid > 0:
                        billing.record_payment(appt, amount=paid, method=method, by=user)
                for status in (AppointmentStatus.IN_CONSULTATION, AppointmentStatus.COMPLETED):
                    appt = booking.transition(appt, status, by=user)

    def _close_shift(self):
        from apps.treasury import services as treasury

        shift = treasury.current_shift(self.drawer)
        if shift is None:
            return
        cash = treasury.cash_on_hand(self.drawer)
        short = D(10) if self.rng.random() < 0.08 and cash > 50 else D(0)  # once in a while the drawer is short
        count = treasury.close_shift(shift, denominations={"1": int(cash - short)}, by=self.user)
        if count.variance_status == "pending":
            treasury.approve_variance(count, by=self.user, membership=self.owner_m)

    # ------------------------------------------------------------------ through the month

    def _buy(self, day):
        from apps.purchasing import services as purchasing
        from apps.purchasing.services import LineIn

        rng = self.rng
        supplier = rng.choice(self.suppliers[:2])
        lines = []
        for name in rng.sample(list(self.items), 3):
            item, cost = self.items[name]
            lines.append(LineIn(
                "", None, str(rng.randint(10, 40)), str(cost), item=item, warehouse=self.main,
                tax_code=self.taxes["vat14"],
                expiry_date=day + timedelta(days=rng.randint(200, 500)) if item.track_expiry else None,
            ))  # fmt: skip
        purchasing.create_invoice(self.org, supplier=supplier, date=day, lines=lines, by=self.user,
                                  supplier_ref=f"S{day:%m%d}-{rng.randint(100, 999)}")  # fmt: skip
        if rng.random() < 0.6:  # a service invoice: maintenance, straight to an expense
            purchasing.create_invoice(
                self.org, supplier=self.suppliers[2], date=day, by=self.user,
                supplier_ref=f"M{day:%m%d}-{rng.randint(100, 999)}",
                lines=[LineIn("صيانة أجهزة", self._acc("sundry_expenses"), "1", str(rng.randint(8, 30) * 100),
                              self._cc(rng.choice(["outpatient", "theatre"])))],
            )  # fmt: skip

    def _pay_suppliers(self, day):
        from apps.purchasing import reports
        from apps.purchasing import services as purchasing
        from apps.purchasing.models import PayMethod

        for supplier in self.suppliers:
            open_rows = reports.open_invoices(self.org, day, supplier=supplier)
            if not open_rows:
                continue
            row = open_rows[0]
            method = self.rng.choice([PayMethod.CHEQUE, PayMethod.CHEQUE, PayMethod.TRANSFER])
            _base, withheld = purchasing.suggest_withholding(self.org, supplier, [(row.invoice, row.open)], row.open)
            try:
                purchasing.pay_supplier(
                    self.org, supplier=supplier, amount=row.open, method=method, treasury_obj=self.bank, by=self.user,
                    date=day, allocations=[(row.invoice, row.open)],
                    wht_code=supplier.default_withholding if withheld else None,
                    cheque_number=f"{day:%m%d}{supplier.pk:03d}",
                    cheque_due=day + timedelta(days=self.rng.randint(5, 20)),
                )  # fmt: skip
            except purchasing.PurchaseError:
                continue

    def _clear_cheques(self, day):
        from apps.treasury import cheques
        from apps.treasury.models import Cheque, ChequeStatus

        due = Cheque.objects.filter(
            organization=self.org, direction="issued", status=ChequeStatus.ISSUED, due_date__lte=day
        )
        for cheque in due:
            cheques.move(cheque, ChequeStatus.CLEARED, by=self.user, date=day)
        for cheque in Cheque.objects.filter(organization=self.org, direction="received", due_date__lte=day):
            if cheque.status == ChequeStatus.IN_HAND:
                cheque = cheques.move(cheque, ChequeStatus.DEPOSITED, by=self.user, bank=self.bank)
            if cheque.status == ChequeStatus.DEPOSITED:
                cheques.move(cheque, ChequeStatus.COLLECTED, by=self.user, date=day)

    def _operation(self, day):
        from apps.billing import services as billing
        from apps.billing.models import PaymentKind
        from apps.theatre import services as theatre
        from apps.theatre.models import Admission, AdmissionStatus, Operation, OperationStatus, TeamRole

        if not (self.procedures and self.theatre_room and self.women):
            return
        rng, user, org = self.rng, self.user, self.org
        taken = set(Operation.objects.filter(organization=org).values_list("patient_id", flat=True))
        in_ward = set(
            Admission.objects.filter(organization=org, status=AdmissionStatus.ADMITTED).values_list(
                "patient_id", flat=True
            )
        )
        free = [p for p in self.women if p.pk not in taken and p.pk not in in_ward]
        if not free:
            return
        patient, procedure, surgeon = rng.choice(free), rng.choice(self.procedures), rng.choice(self.doctors)
        try:
            op = theatre.book_operation(
                org=org, patient=patient, procedure=procedure, room=self.theatre_room, surgeon=surgeon,
                start=timezone.now() + timedelta(hours=1), by=user, notes=MARK, notify=False,
            )  # fmt: skip
        except theatre.TheatreError:
            return
        team = {m.role: m for m in op.team.all()}
        price = op.price
        if TeamRole.SURGEON in team:
            theatre.save_team_member(op, role=TeamRole.SURGEON, by=user, member=team[TeamRole.SURGEON], doctor=surgeon,
                                     fee=int(price * D("0.35")))  # fmt: skip
        if TeamRole.ANAESTHETIST in team:
            theatre.save_team_member(op, role=TeamRole.ANAESTHETIST, by=user, member=team[TeamRole.ANAESTHETIST],
                                     name="د. تخدير زائر", fee=int(price * D("0.1")))  # fmt: skip
        billing.record_account_payment(patient, amount=D(int(price / 2)), by=user, kind=PaymentKind.DEPOSIT)
        for item in op.checklist.filter(done=False):
            theatre.toggle_checklist(item, by=user)
        try:
            theatre.issue_kit(op, by=user)
        except theatre.TheatreError:
            pass  # the cabinet ran out: the case goes on with what was written by hand
        for status in (
            OperationStatus.ADMITTED,
            OperationStatus.IN_THEATRE,
            OperationStatus.RECOVERY,
            OperationStatus.DONE,
        ):
            op = theatre.transition(op, status, by=user)
        if self.ward and theatre.beds_free(self.ward) > 0:
            try:
                self.stays.append(theatre.admit(org=org, patient=patient, room=self.ward, by=user, operation=op,
                                                doctor=surgeon, reason=MARK))  # fmt: skip
                return
            except theatre.TheatreError:
                pass
        self._invoice(patient)

    def _discharge(self, day):
        from apps.theatre import services as theatre

        for stay in list(self.stays):
            if timezone.localtime(stay.admitted_at, CAIRO).date() < day:
                theatre.discharge(stay, by=self.user, summary="خرجت بحالة جيدة", instructions="متابعة بعد أسبوع")
                self.stays.remove(stay)
                self._invoice(stay.patient)

    def _invoice(self, patient):
        from apps.billing import services as billing
        from apps.invoicing import services as invoicing

        keys = [i.key for i in invoicing.open_items(patient) if i.ready]
        if not keys:
            return
        invoicing.issue_account_invoice(patient, keys=keys, by=self.user, membership=self.owner_m)
        owed = invoicing.patient_balance(patient)
        if owed > 0 and self.rng.random() < 0.85:
            billing.record_account_payment(patient, amount=owed, by=self.user, method=self.rng.choice(["cash", "card"]))

    def _monthly(self, day):
        from apps.treasury import services as treasury

        for template in treasury.expense_templates(self.org, day):
            if template.is_active and template.paid_voucher is None and day.day >= template.day_of_month:
                try:
                    treasury.record_expense(template, by=self.user, date=day)
                except treasury.TreasuryError:
                    pass

    # ------------------------------------------------------------------ the last evening

    def _month_end(self):
        from apps.inventory import services as stock
        from apps.inventory.models import CountKind
        from apps.invoicing import services as invoicing
        from apps.treasury import cheques

        org, user = self.org, self.user
        for doctor in self.doctors:
            owed = invoicing.doctor_balance(doctor)
            if owed > 100:
                invoicing.pay_doctor(doctor, amount=D(int(owed * D("0.8"))), treasury_obj=self.bank, by=user)
        owed = invoicing.payer_balance(self.payer)
        if owed > 200:  # the company pays part of what it owes with a post-dated cheque
            cheques.receive_cheque(
                org, number="778201", bank_name="CIB", due_date=timezone.localdate() + timedelta(days=12),
                amount=D(int(owed * D("0.6"))), by=user, payer=self.payer,
            )  # fmt: skip
        # a surprise count of three items: one is found short
        held = [item for item, _cost in self.items.values() if stock.on_hand(item, self.main) > 2][:3]
        if held:
            count = stock.start_count(org, warehouse=self.main, by=user, kind=CountKind.SPOT, items=held)
            quantities = {item.pk: stock.on_hand(item, self.main) for item in held}
            quantities[held[0].pk] -= 1
            stock.save_count(count, quantities, by=user)
            stock.submit_count(count, by=user)
            stock.approve_count(count, by=user, membership=self.owner_m)
