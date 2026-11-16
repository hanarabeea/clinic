# 14 - OB/GYN Center Expansion (v2)

Requested 2026-11-09: turn DigiClinic from a single-doctor clinic system into a full system for an
**Obstetrics & Gynecology center**: outpatient clinics, pregnancy follow-up, an operating/delivery room (غرفة العمليات),
emergency arrivals, and a complete, provably correct financial system (شجرة حسابات، موردين، شيكات، ضرائب، ميزان مراجعة،
مخزن، جرد), with a new visual identity, reports, permissions and built-in integrity checks.

This file is the source of truth for phases 11-20. It extends (does not replace) files 01-13.

## 14.0 Decisions

### Changed by this request (locked now)
| Was (README) | Now |
|---|---|
| الحسابات: بسيطة (سعر الزيارة، تسجيل الدفع، تقرير يومي) | Full double-entry accounting. The simple `billing.Payment` flow stays as the cashier front end and posts to the ledger. |
| ملف المريض: عام | General file + an OB/GYN layer (pregnancy record, obstetric history, antenatal visit sheet). The layer is switched on per clinic (`OrganizationSettings.specialty = "obgyn"`), so the product still sells to other specialties. |
| Look: sand/stone cards | New design system (14.1). |
| Every user sees the side menu with their allowed pages | A role can have one **work screen** (reception, doctor's desk, cashbox, later theatre / accounts): its people sign in straight onto it, full window, no side menu, like a program of its own (14.1). Asked for by the owner 2026-11-11. |
| Sign-in: full page, username or e-mail, e-mail password reset | A small sign-in box, username + password only. Staff have no e-mails; a forgotten password is rewritten by the clinic's admin from the users screen. |

### Assumed until the owner says otherwise (see 14.11 open questions)
- The center has **several doctors**. The UI becomes multi-doctor (doctor picker on the board, per-doctor queues). Data model already has `doctor` everywhere.
- There are **a few inpatient rooms** (admission after delivery, charged per night).
- Stock of medical supplies **is tracked** (quantities, cost, expiry), because the operating room consumes them and "جرد" needs it.
- Currency EGP only. Fiscal year = calendar year (configurable start month).
- Medical services are **VAT-exempt** in Egypt, so the center does not charge VAT to patients and treats VAT paid to suppliers as cost. Both are settings (a taxable service or recoverable VAT can be switched on per tax code). Withholding tax (خصم تحت حساب الضريبة) on supplier payments is supported and off by default per supplier. **Every tax default must be confirmed by the center's accountant before go-live**; the system is built so they are data, not code.

### Unchanged
Stack, multi-tenancy, permissions via `@require_perm`, services-only business logic, outbox for messages, UTC storage,
`Decimal` money, no hard deletes of medical or financial records.

---

## 14.1 Visual identity (Phase 11)

Design read: an operational tool used 10 hours a day by reception, doctors and an accountant in a bright women's health
center. Trust-first, data-dense, calm. Not pink-and-babies, not teal-medical, not sand-and-cards.

Concept: **the clinical chart and the ledger**. Crisp paper surfaces, hairline rules, ink typography, tabular figures,
one accent. Identity comes from structure and type, so it survives any brand colour the clinic picks.

- Type: IBM Plex Sans Arabic (Arabic + Latin + digits in one family, 400/500/600/700), tabular numerals in tables and money.
- Colour: neutrals are cool and take a slight tint from the clinic's brand colour at runtime. Default brand: pomegranate
  (رمّاني) on an aubergine ink. Semantic colours (ok / wait / late / danger / info) fixed, never the brand colour.
- Shape: 6-8px radii, 1px rules, no decorative shadows. Status as squared tags. Real tables for lists.
- Shell: ink sidebar grouped by area (العيادة / المركز / الحسابات / الإدارة), top bar with page title, global patient
  search, date, user menu.
- Icons: one vendored icon family (Phosphor, MIT) through `{% icon "name" %}`; no emoji as icons.
- Motion: 120-200ms ease-out on overlays and toasts only; `prefers-reduced-motion` respected.
- Work screens: `ClinicRole.start_screen`; the screens are defined once in `apps/core/navigation.py` and every later
  phase adds its screens there (theatre board, cashier, accounts, stock), each usable as a role's work screen.
  Layout only: what a person may open is still decided by permissions, per view.
- Schema: only `ClinicRole.start_screen` and the new default colours. Every existing test stays green.

---

## 14.2 OB/GYN medical layer (Phase 12)

New app `apps/obstetrics` (all models `TenantScopedModel`, simple-history, admin, never hard-deleted).

- `ObstetricProfile` (one per patient): blood group + Rh, husband's name/blood group (optional), marital status,
  menarche age, cycle length/regularity, contraception method, G/P/A/L **derived** from the history rows plus the
  current pregnancy (never typed twice), risk flags (previous CS count, Rh negative, GDM, hypertension, twins...).
- `PregnancyHistory` (previous pregnancies): year, outcome (ولادة طبيعية / قيصرية / إجهاض / خارج الرحم / ولادة ميتة),
  gestational weeks, baby sex/weight, place, complications.
- `Pregnancy` (the follow-up record; at most one `active` per patient, enforced by a partial unique constraint):
  - dating: `lmp`, `cycle_days`, optional ultrasound dating (`us_date`, `us_weeks`, `us_days`), `edd_method`
    (lmp / ultrasound / ivf), `edd` stored once agreed.
  - **Gestational age is always computed, never stored**: `ga(on_date) -> (weeks, days)`; trimester; Egyptian month
    ("الشهر السادس"); days to EDD; term status (preterm < 37w, term 37-41+6, post-term ≥ 42w).
  - number of fetuses, risk level (low/high + reasons), status `active / delivered / miscarried / ectopic / terminated /
    transferred`, outcome link to the delivery (Phase 15).
- `AntenatalVisit` (OneToOne on `clinical.Visit` when the patient has an active pregnancy): GA at visit (snapshot),
  fundal height, fetal heart rate, fetal movement, presentation, edema, urine albumin/sugar, Hb, ultrasound summary
  (BPD/FL/AC/EFW/AFI/placenta) as optional fields, next-visit suggestion (every 4 weeks to 28w, 2 weeks to 36w, weekly after).
- `PregnancyMilestone` schedule (data, per clinic, seeded): booking labs, NT scan 11-13w+6, anomaly scan 18-22w,
  GTT 24-28w, anti-D 28w for Rh-negative, GBS 35-37w, etc. The doctor's workspace shows what is due / overdue.

Where the gestational age shows (the owner's explicit requirement): reception board row, doctor queue, visit header,
patient page, booking panel, operation list. Format: **"حامل 24 أسبوع + 3 أيام · الشهر السادس · الولادة المتوقعة 12/02"**.

Rules worth pinning in tests: Naegele (LMP + 280 days, adjusted by `cycle_days - 28`), ultrasound dating replaces LMP
when chosen, GA never negative or beyond 45 weeks without a warning, month mapping table, DST-independence (dates only).

Messages: new templates (antenatal visit reminder with the week, milestone due, post-partum check) through the
existing outbox, with consent.

Built in Phase 12 (2026-11-16), with these differences from the sketch above: the profile keeps blood groups, cycle
and contraception (marital status and menarche age left to the clinic's custom patient fields); risk flags are computed
from recorded facts instead of stored; the milestone model is `Milestone` + `MilestoneCheck`; the ultrasound part of
the antenatal sheet is EFW, AFI, placenta and free notes. Still open from this section: the message templates, a
settings screen for the milestone list, structured biometry, and the pregnancy summary on the export PDF.

Permissions: read/write follow `clinical.view` / `clinical.edit`; reception sees **only** the GA badge and EDD
(new `pregnancy.view_basic`), never the obstetric history.

---

## 14.3 Center mode: doctors, services, emergency (Phase 13)

- Multi-doctor UI: doctor picker on the reception board (all / one), per-doctor queue and schedule screens, doctors
  screen in settings (add/deactivate), each doctor's own visit types. `views._get_doctor(org)` call sites move to an
  explicit current-doctor selection.
- `Service` catalog (خدمات المركز): ultrasound, 4D, CTG, cautery, IUD insertion, lab collection, injection...
  with price, revenue account, optional doctor share. Services are added to a visit as `ServiceOrder` lines
  (by doctor or reception) and billed with it.
- Emergency (طوارئ): one button on the reception board. Fast registration (name + phone are enough), triage level
  (حرج / عاجل / عادي), reason (ولادة، نزيف، ألم...), jumps the queue, shows red on the doctor's desk, timestamps
  (arrival, seen, disposition: خرجت / دخول عمليات / حجز غرفة / تحويل). Emergency fee from settings. Implemented as an
  `Appointment(source=emergency)` + `EmergencyCase` so the visit, prescription, billing and audit paths are reused.

---

## 14.4 Operating room and admissions (Phase 15)

New app `apps/theatre`.

- `Room` (kind: operating / delivery / recovery / inpatient; nightly rate for inpatient).
- `ProcedureType` (ولادة طبيعية، قيصرية، كحت D&C، ربط عنق الرحم، منظار رحمي/بطني، استئصال رحم...): default duration,
  package price, default team roles and fees, default consumables kit, revenue account.
- `Operation`: patient, pregnancy (optional), procedure, room, planned start/end, emergency flag, status
  `scheduled → admitted → in_theatre → recovery → done` (+ `cancelled`, `postponed`), state machine in `services.py`
  like appointments. **Room conflicts are prevented under a `select_for_update` room-day ledger** (same pattern as
  `DayLedger`). Reschedule keeps history.
- `OperationTeamMember` (surgeon, assistant, anaesthetist, paediatrician, nurse; internal `Doctor` or external name; fee).
- Pre-op checklist (consent signed, fasting, labs, blood group, anaesthesia review) from a per-procedure template;
  the operation cannot move to `in_theatre` with required items open unless marked emergency (then it is logged).
- `OperationNote` (findings, procedure, blood loss, complications, post-op orders), immutable once signed (revision like Rx).
- `Newborn` (per operation, 1..n): sex, weight, Apgar 1/5, time of birth, status, notes. Closing a delivery closes the
  `Pregnancy` (status delivered) and writes the `PregnancyHistory` row automatically.
- `Admission`: room/bed, admit and discharge times, nights computed, discharge summary PDF.
- `OperationConsumable`: items taken from stock for the case (Phase 18 links cost).
- Screens: theatre calendar (day/week per room), operation page (tabs: plan, checklist, team, note, newborn,
  consumables, account), "today in the center" board (who is admitted, in theatre, in recovery).
- Messages: operation booked / reminder with fasting instructions / changed, via the outbox.

---

## 14.5 Accounting core (Phase 14)

New app `apps/ledger`. This is the part that must never be wrong; the rules below are enforced in **three layers**:
service code, database constraints/triggers, and a verification job.

### Chart of accounts (شجرة الحسابات)
- `Account`: code (hierarchical, e.g. `1`, `11`, `1101`, `110101`), `name_ar`, `name_en`, parent, `type`
  (asset / liability / equity / revenue / expense), `normal_balance` (derived from type), `is_group` (only leaf
  accounts accept postings), `is_active`, `system_key` (for accounts the code posts to, e.g. `cash_main`,
  `patients_receivable`, `suppliers_payable`, `cheques_receivable`, `cheques_payable`, `vat_input`, `wht_payable`,
  `inventory`, `cash_over_short`, `retained_earnings`, `opening_balances`).
- Seeded with a standard Egyptian tree for a medical center (5 classes: 1 الأصول، 2 الالتزامات، 3 حقوق الملكية،
  4 الإيرادات، 5 المصروفات), editable: add/rename/move/deactivate; an account with postings can never be deleted or
  turned into a group; type cannot change once it has postings.
- `CostCenter` (العيادات الخارجية / العمليات / الطوارئ / الإقامة / الإدارة) on every revenue and expense line.

### Journal
- `FiscalYear`, `FiscalPeriod` (monthly; status open / closed). No posting into a closed period. Closing the year
  generates the closing entry to retained earnings; reopening needs `ledger.close_period` and is audited.
- `JournalEntry`: number (gapless per fiscal year, assigned under `select_for_update` at posting), date, memo,
  `status` draft / posted / reversed, `source` (generic link to the document that produced it, **unique**: one
  document posts once), `reverses` (FK), created/posted by.
- `JournalLine`: account (leaf), `debit`, `credit` (`NUMERIC(16,2)`), cost center, party (patient / supplier / doctor /
  employee, nullable), memo.
- Invariants:
  1. A line has exactly one of debit/credit > 0 (CHECK constraint).
  2. An entry's debits equal its credits and it has ≥ 2 lines (**deferred constraint trigger in PostgreSQL**, checked
     at commit; plus the service asserts it before saving).
  3. Posted entries and their lines are immutable (trigger rejects UPDATE/DELETE); corrections are reversal entries.
  4. Posting requires an open period, active leaf accounts, and the same organization on every row.
  5. All arithmetic is `Decimal`, rounded once with `ROUND_HALF_UP` to 2 places at the line level; totals are sums of
     rounded lines; any allocation (discount, tax, shares) uses a largest-remainder split so parts always sum to the whole.
- `ledger.services.post(entry_spec)` is the only way to create entries. Every money-moving document in the system
  (payment, voucher, invoice, bill, cheque event, stock move, shift close) owns a `posting.py` function that turns it
  into an entry spec; idempotent by `source`.

### Treasury
- `Treasury` (خزنة / حساب بنكي / محفظة إلكترونية), each tied to one ledger account. Reception cash drawer = a treasury.
- `Voucher`: سند قبض / سند صرف / تحويل بين الخزن, numbered per kind per year, with counter-account, party, cost center.
- The existing `billing.Payment` becomes a receipt into a treasury (cash → the user's drawer; card/InstaPay/wallet →
  their clearing accounts) and posts automatically.
- **Shift close (تقفيل وردية)**: expected (from the ledger) vs counted by denomination; the difference posts to
  عجز وزيادة الخزينة; the handover moves the cash to the main safe. A **surprise cash count (جرد مفاجئ)** is the same
  screen without closing the shift: it records the count and the variance and who witnessed it.

### Reports in this phase
Trial balance (ميزان المراجعة: opening / period movement / closing, by level, with or without zero accounts), general
ledger and account statement with running balance, journal (دفتر اليومية), treasury movement. Every report prints
(PDF) and exports to Excel. Numbers on a report always come from posted lines only.

### Existing data
A data migration builds the tree and posts every historical `Payment` so the ledger starts in agreement with the cashbox.

---

## 14.6 Patient invoicing (Phase 16)

- `Invoice` (فاتورة مريض) with lines: visit fee, services (14.3), operation package, team fees, room nights,
  consumables, discounts. One open **patient account (ملف حساب)** per stay/operation collects lines until discharge.
- Deposits (مقدم/عربون للعملية) are liabilities until the invoice is issued, then applied. Refunds by voucher.
- Discounts need `invoice.discount` and a reason; above a settings limit they need the owner.
- Doctor shares / external team fees post to أتعاب أطباء مستحقة and are paid by payment voucher; statement per doctor.
- Outpatient visits keep the fast flow (pay at the desk); behind it each paid visit is an auto-issued invoice.
- Corporate / insurance payers (جهات التعاقد): optional payer on the invoice with its own receivable account and statement.
- Receipt and invoice PDFs (Arabic, numbered, with tax registration data when applicable).

## 14.7 Suppliers, purchasing, taxes, cheques (Phase 17)

- `Supplier`: name, tax registration number, tax file, commercial register, phones, payment terms, default withholding
  rate, opening balance. Statement (كشف حساب مورد) and aging.
- `TaxCode`: kind (VAT / withholding / stamp / other), rate, account, recoverable flag, inclusive/exclusive.
  Seeded: ض.ق.م 14% (non-recoverable by default), exempt, zero; خصم تحت حساب الضريبة 1% (توريدات), 3% (خدمات),
  5% (أتعاب مهنية/عمولات). Tax is computed per line and rounded per line; the invoice total is the sum of lines.
- `PurchaseInvoice` (فاتورة مشتريات): lines to stock items or directly to expense accounts / fixed assets; discounts;
  taxes; attachments (scan of the paper invoice). `PurchaseReturn`.
- `SupplierPayment`: cash / bank transfer / cheque, allocated to invoices (partial allowed), withholding deducted at
  payment and posted to ضرائب خصم مستحقة with a certificate line for the quarterly form 41.
- `Cheque`: direction issued (أوراق دفع) / received (أوراق قبض); number, bank, due date, amount, party.
  Issued: `issued → cleared | cancelled | returned`. Received: `in_hand → deposited → collected | bounced`
  (+ `endorsed`). Each transition posts its entry and is undoable only by the reverse transition. Due-date calendar
  and scheduler alerts (cheques due in N days, balance check against the bank account).
- Expense vouchers for non-supplier spending (rent, salaries, utilities) with recurring templates.
- Tax reports: VAT paid by period, withholding by supplier and quarter, taxable sales if any.

## 14.8 Inventory and counts (Phase 18)

- `StockItem` (unit, category, reorder level, expiry-tracked?), `Warehouse` (main store, theatre cabinet),
  `StockBatch` (expiry, cost), `StockMove` (receipt from purchase, issue to operation/department, transfer,
  adjustment, return) - quantities change only through moves, under a per-item lock.
- Costing: weighted average per item per warehouse, recomputed on receipt; issues post Dr consumption / Cr inventory
  at that cost; rounding differences go to a dedicated account, never lost.
- **Stock count (جرد)**: full or spot (a random or chosen sample of items). Count sheet → variances → approval →
  adjustment moves + entry. Spot counts are logged with who counted and who approved.
- Alerts: below reorder level, expiring within N days (dashboard + daily digest).
- Operation kits: a procedure's default consumables are issued in one step and adjusted to actual use.

## 14.9 Financial statements, management reports, verification (Phase 19)

- Income statement (قائمة الدخل) by period and by cost center, balance sheet (المركز المالي), cash movement,
  receivables and payables aging, revenue by doctor / service / procedure, operation profitability (package price
  less team fees, consumables and room), daily cashier report, doctor share statements, tax summaries.
- Comparative columns (previous period / previous year), drill-down from any number to the entries behind it.
- **Books verification (فحص سلامة الدفاتر)**, `manage.py verify_books` + nightly scheduler job + screen:
  1. every posted entry balances; the whole ledger balances per organization;
  2. no posted entry without a source document and no posted document without its entry;
  3. each control account equals its subledger (patients, suppliers, payers, each treasury, cheques by status,
     inventory value, deposits, doctor payables, withholding);
  4. numbering has no gaps or duplicates; nothing dated in a closed period after its close;
  5. stock quantities never negative; batch quantities sum to item quantities.
  Any failure raises an owner alert (same channel as `check_health`) and blocks period close.
- Year-end close with a checklist.

## 14.10 Permissions, roles, quality (all phases, finished in Phase 20)

New permissions, added to `PERMISSION_GROUPS` as each phase lands (the matrix test already enforces that every URL and
button is covered):
`pregnancy.view_basic`, `emergency.register`, `service.order`, `theatre.view`, `theatre.schedule`, `theatre.operate`,
`admission.manage`, `ledger.view`, `ledger.post`, `ledger.close_period`, `accounts.manage`, `treasury.operate`,
`treasury.count`, `invoice.issue`, `invoice.discount`, `invoice.refund`, `supplier.manage`, `purchase.manage`,
`supplier.pay`, `cheque.manage`, `tax.manage`, `stock.view`, `stock.move`, `stock.count`, `stock.approve_count`,
`report.statements`, `books.verify`.

New default roles: محاسب (accountant), أمين خزنة/كاشير, أمين مخزن, تمريض عمليات. Separation of duties defaults: the
person who counts cash or stock cannot approve their own variance; discounts and refunds above the limit need a second person.

Testing standard for money: unit tests per posting function with exact expected entries; **property-based tests**
(hypothesis) that run random sequences of documents and assert the 14.9 invariants after every step; concurrency tests
for numbering and stock; `verify_books` run at the end of the crawl test. `seed_center` builds a realistic month of
activity (visits, deliveries, purchases, cheques, counts) through the real services for demos and tests.

---

## 14.11 Roadmap

| Phase | Scope | Size |
|---|---|---|
| 11 | Visual identity and design system (14.1) | L |
| 12 | OB/GYN medical layer: pregnancy, GA everywhere, obstetric history, antenatal sheet, milestones (14.2) | L |
| 13 | Center mode: multi-doctor UI, services catalog, emergency (14.3) | L |
| 14 | Accounting core: chart of accounts, journal, periods, treasury, shift close, trial balance (14.5) | L |
| 15 | Operating room, deliveries, newborns, admissions (14.4) | L |
| 16 | Patient invoicing, deposits, doctor shares, payers (14.6) | L |
| 17 | Suppliers, purchasing, taxes, cheques (14.7) | L |
| 18 | Inventory, costing, stock counts (14.8) | M |
| 19 | Financial statements, management reports, books verification, year close (14.9) | L |
| 20 | Hardening: roles, full permission matrix, crawl, performance, `seed_center`, user guide (14.10) | M |

Each phase follows the CLAUDE.md rules: quality gates, Definition of Done reported, commit `phase-N: ...`, stop for approval.

### Definition of Done highlights
- 12: opening any pregnant patient anywhere shows week + day, month and EDD; changing the LMP or choosing ultrasound
  dating updates every screen; reception never sees obstetric history (test).
- 14: an unbalanced entry cannot be committed even with raw SQL (test); posted entries cannot be edited (test); trial
  balance totals equal for any random sequence of documents (property test); cashbox total = ledger cash accounts.
- 15: two operations cannot overlap in one room (threaded test); closing a delivery closes the pregnancy and creates
  the newborn and history rows.
- 17: a supplier bill with mixed tax lines, a partial payment by post-dated cheque with withholding, the cheque clearing,
  then a return: supplier statement, tax accounts and bank all agree to the piaster.
- 19: `verify_books` green after `seed_center`; every statement number drills down to entries.

### Open questions for the owner
1. How many doctors work in the center, and do visiting surgeons operate there?
2. Are there inpatient rooms, and how many?
3. Is the center registered for VAT? Who is the accountant who will confirm the tax defaults and the chart of accounts?
4. Do doctors get a percentage of visits/operations, or fixed fees?
5. Are there contracts with companies or insurers?
6. What exactly should "spot checks" cover beyond surprise cash and stock counts?
