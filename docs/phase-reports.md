# Phase reports (OB/GYN center plan, phases 13 to 20)

One short report per phase, in plain language: what was built, what was checked, what is not done, and how to try it.
Written while the owner was away on 2026-11-16. The eight phases were committed together as one commit that day.

## Phase 13: center mode (several doctors, services, emergency)

**What was built**

- **Several doctors.** Settings has a new tab "الدكاترة": add a doctor, give them their own working hours and visit
  types, link them to their own login, or switch them off (never deleted, their old visits stay). The reception board
  has a doctor picker: "الكل" shows every doctor's day one under the other, or pick one doctor to see the full
  schedule with free slots. The booking box asks which doctor, and each doctor keeps their own prices and free
  times. A doctor who signs in with their own login lands on their own patients.
- **Services.** Settings has a new tab "الخدمات والطوارئ" with the center's services (ultrasound, 4D, CTG, IUD,
  injections and so on) with a price and an optional share for the doctor. Reception adds a service to a visit from
  the row's "more" menu, or the doctor adds it inside the visit. The amount due on the visit goes up by itself, and
  the payment box shows the new total. A service that was already paid for cannot be removed until the money is
  refunded.
- **Emergency.** A "طوارئ" button on the reception board. A name and a phone number are enough (typing looks up
  existing patients). Reception picks how urgent (حرج / عاجل / عادي), the reason and the doctor. The patient is
  placed ahead of everyone on that doctor's queue, shows on top of every reception board, and a red banner appears on
  the doctor's screen until they call her in. The system records when she arrived, when the doctor took her in, and
  what happened next (went home, operating room, room, transferred). "الطوارئ" in the side menu is the daily log.

**What was checked**

- 25 new automated tests for this phase, and the whole test suite passes (lint, formatting and database migration
  checks included).
- Looked at every new screen in the browser: doctors settings, services settings, the board for all doctors and for
  one, the week view, the emergency dialog, the services box, the doctor's visit page with an emergency, the
  emergency log, and the board at phone width. One real bug was found and fixed this way (the "what happened next"
  button on the visit page broke its own dialog).
- After the owner asked for plainer screens, the reception board was simplified: no coloured row backgrounds, one
  line per emergency case with the extra actions behind a "more" menu, two buttons in the header instead of three,
  and the services list in the visit page stays folded until needed.

**Decisions made without asking (change them in Settings or tell me)**

- The emergency fee is one fixed amount per case, set in Settings (it starts at 600 in the demo data, 0 for a new
  clinic).
- A doctor's share of a service is a percentage of its price, credited to the doctor of that visit. It is only
  recorded for now; paying it out comes with invoicing in phase 16.
- An emergency case does not take a slot away from booked patients and never gets a "missed appointment" message.
- Staff who can only view the schedule see that there is an emergency case and who it is, but not the reason.

**Not done in this phase**

- Reports by doctor and by service (phase 19).
- "Operating room" and "room" as outcomes of an emergency are only recorded; they will open an operation or an
  admission when phase 15 exists.

**How to try it**

1. Start the system with `run.bat` (it adds the starting services by itself).
2. Settings, tab "الدكاترة": add a second doctor, then give her working hours.
3. Open "الاستقبال والمواعيد": use the doctor picker, book for each doctor.
4. Press "طوارئ", type a name and a phone, choose "حرج", save. Then open "شاشة الدكتور" for that doctor and watch
   the red banner.
5. On any booking's "more" menu choose "خدمات على الزيارة" and add an ultrasound; open "تسجيل دفع" to see the new
   total.

## Phase 14: the accounting core (the books and the treasury)

**What was built**

- **The books.** A full chart of accounts (a standard Egyptian tree in five classes: assets, liabilities, equity,
  revenue, expenses) that you can add to and rename, a journal where every movement of money is an entry, months that
  can be closed so nothing in them changes again, and three reports: the trial balance (ميزان المراجعة), the journal
  (دفتر اليومية) and an account statement (كشف حساب). Each report prints and exports to Excel.
- **The treasury.** The reception drawer, the main safe, a bank account and the card / InstaPay / wallet accounts,
  each with a balance that comes straight from the books. Receipt, payment and transfer vouchers (سند قبض، سند صرف،
  تحويل). A surprise cash count.
- **The cashier's shift.** The shift opens by itself with the first cash of the day. To close it the cashier counts
  the drawer note by note without seeing the expected amount; the cash goes to the main safe; if the count does not
  match, the difference waits for a second person to approve it and is then recorded as عجز or زيادة.
- **Every patient payment now writes its own journal entry**, and the old payments were posted too, so the cashbox
  screen and the books are the same numbers.

**How "no mistakes" is enforced**

- One piece of code writes entries, and it refuses anything that does not balance to the piaster.
- The database itself refuses an unbalanced entry and refuses any change or deletion of a posted entry, even if
  someone tries with a direct database command. A mistake is fixed by a reversing entry, so both stay visible.
- Entry numbers have no gaps. A closed month accepts nothing. Cash in a drawer or safe can never go below zero.
- All amounts are exact decimals rounded once; any split of an amount is done so the parts always add up.

**What was checked**

- 28 tests for the books and 19 for the treasury, including: direct database attempts to break an entry (all
  refused), eight entries posted at the same instant (numbers 1 to 8, no gap), and two randomized tests that run
  hundreds of random sequences of payments, refunds, vouchers, cancellations and shift closes and check after every
  single step that everything still balances and that the cashbox equals the books.
- The whole test suite, lint, formatting and migration checks.
- Every new screen in the browser: trial balance, journal, a new manual entry, the accounts tree, months, the
  treasury screen, a new voucher, the cashbox.

**Decisions made without asking (standard practice, all changeable)**

- A payment is credited to the patient's own account; income is recorded when the visit is invoiced, which comes in
  phase 16. Until then the patients' account shows what they paid as a credit. This is the normal way and avoids
  redoing the books later.
- The fiscal year is the calendar year.
- Only the owner can see the books and the treasury for now. Accountant and cashier roles come in phase 20; you can
  already make such a role yourself in Users, Roles.
- The chart of accounts and the tax accounts in it are a standard starting point. **Your accountant should review
  the chart before real use.**

**Not done in this phase**

- Income and expense reports, the balance sheet and the year-end close (phase 19).
- Invoices, deposits and doctor shares (phase 16); suppliers, taxes and cheques (phase 17); stock (phase 18).

**How to try it**

1. Start with `run.bat`. Sign in as the owner.
2. Take a payment at reception, then open "الدفاتر": the entry is in دفتر اليومية and the trial balance balances.
3. Open "الخزن والسندات": press "سند جديد", record a receipt into the main safe (for example capital), then a
   payment for rent.
4. On "الخزنة" press "تقفيل الوردية", count less than what was taken, and see the difference waiting for approval on
   "الخزن والسندات".
5. In "الدفاتر", tab "الشهور", close last month and try to date a voucher in it: it is refused.

---

## Phase 15: operating room, deliveries, newborns, stays (2026-11-16)

**What was built**

- A new screen "العمليات والإقامة". The first tab shows today's operations, one column per operating or delivery
  room (or the whole week), and who is staying in the center right now. Each operation shows one button: its next step (المريضة وصلت،
  دخلت العمليات، خرجت للإفاقة، خلصت).
- Booking an operation: pick the patient, the operation type, the room, the doctor and the time. The program refuses
  a time that overlaps another operation in the same room, even if two people book at the same second.
- Each operation has its own page: the checklist before the operation, the team and their fees, the operation
  report, the newborn (for deliveries), the supplies used, and a log of every step with who did it and when.
- The patient cannot enter the theatre while a required checklist item is open. For an emergency she can, and the
  program writes down what was skipped.
- The operation report is written by the doctor and then signed. A signed report cannot be changed; a correction is a
  new version and the old one stays.
- Deliveries: the newborn is recorded (sex, weight, Apgar, time). When the delivery is finished the pregnancy file is
  closed by itself and the mother's history (G/P/A/L) is updated by itself.
- Stays: admit a patient to a room with a free bed, see every room and who is in it, discharge her with a summary and
  instructions, and print the discharge summary. Nights are counted by the program.
- Messages to the patient: operation booked, a reminder the day before with the fasting hours, and a message when the
  time changes. Cancelling or postponing stops the reminder.
- Settings, tab "العمليات والغرف": the rooms (with beds and price per night) and the operation types (duration, price,
  checklist, fasting hours). The program starts with 5 rooms and 7 common OB/GYN operations that you can change.
- From an emergency case, "دخول عمليات" and "حجز غرفة" now open the booking with the patient already filled in. The
  doctor's visit page has "حجز عملية". The patient page lists her operations.

**What was checked**

- Tests for every rule above: double booking (including two bookings at the same instant), the step order, the
  checklist block and the emergency exception, the signed report, closing a delivery into the pregnancy file, twins,
  stillbirth, beds, nights, messages, permissions and that another clinic can never see or touch any of it.
- The whole test suite, lint, formatting and migration checks.
- In the browser: the board, an operation page and all its tabs, the new operation form, the stays screen, the admit
  form and the settings tab.

**Decisions made without asking (all changeable)**

- A night is counted each time midnight passes, and a stay is one night at least.
- The price of the operation and the price of the room per night are fixed at the moment of booking / admission, so
  changing the price list later does not change an old case.
- The reminder is sent 24 hours before the operation. Fasting is 8 hours unless the operation type says otherwise.
- Reception can book and move operations and admit / discharge; only doctors record the steps, the team, the report
  and the newborn. You can change this per role in Users, Roles.

**Not done in this phase**

- The money of an operation or a stay (invoice, deposit, the doctor's and the team's fees): phase 16.
- Supplies are a name and a quantity for now. Taking them from stock with their cost is phase 18.

**How to try it**

1. Start with `run.bat`, sign in, and open "العمليات والإقامة".
2. Press "عملية جديدة", pick a patient who has a pregnancy file, choose "ولادة قيصرية", a room and a time today.
3. Try to book a second operation in the same room at the same time: it is refused.
4. Open the operation, tick the checklist, press the steps one by one, add the newborn, then finish. Open the
   patient's pregnancy file: it is closed and her history has the new birth.
5. Open the tab "الإقامة", press "دخول مريضة", admit her to a room, then discharge her and print the summary.

---

## Phase 16: invoices, deposits, doctor shares, contracted companies (2026-11-16)

**What was built**

- A new screen "الفواتير" with four tabs: the invoices, patients' accounts, what the center owes its doctors, and
  contracted companies / insurers.
- Reception works exactly as before. When a patient who is in the center pays, or when her visit is finished, the
  program makes the invoice by itself. If something changes afterwards (a service is added or removed, a discount,
  going back a step, a cancellation) the program corrects the invoice by itself with a credit note and a new invoice.
- Operations and stays: each patient has an account page. It shows what is waiting for an invoice (the operation,
  the nights of the stay, extra items you add), what she owes, and the deposit you hold for her. One button issues
  the invoice, and the deposit is deducted from it automatically.
- Deposits before an operation, payments on the account, and giving money back, all from the same page. Each one
  can be printed as a receipt. Invoices print as a numbered Arabic PDF.
- Discounts need a reason. A discount on the visit is in reception's "more" menu. Above 10% (you can change this) it
  needs you, the owner.
- Doctor shares: set a percentage of the visit fee for each doctor, a percentage for each service, and fees for each
  member of an operation's team. The program records what each doctor earned on every invoice, shows each doctor's
  statement, and paying them is one button that takes the money from a safe or the bank.
- Contracted companies and insurers: add the company, put the patient on it with the percentage it covers, and
  every invoice is split between her and the company. Reception is asked only for her part. Each company has a
  statement and a "collect" button.
- An invoice can never be edited or deleted. A mistake is reversed with a credit note and both stay on record.

**What was checked**

- 19 tests for invoicing: the exact accounting entry of every document, that nothing can be invoiced twice (also
  refused by the database itself), the discount limits, deposits, shares, companies, credit notes, permissions, the
  printed PDFs and the Excel export.
- A randomized test that runs hundreds of random sequences of visits, payments, services, discounts, refunds and
  cancellations and checks after every step that the invoices, each patient's balance, each doctor's balance and
  the books all say the same thing.
- The whole test suite, lint, formatting and migration checks.
- In the browser: the invoices list, patients' accounts, a patient's account (recorded a demo deposit), an invoice
  and a credit note, doctors, a new company, the settings tab and reception's discount box.

**Decisions made without asking (standard practice, all changeable)**

- Income is recorded when the patient is in the center and has paid, or when the visit is finished. Money taken
  earlier (at booking) waits on her account until then.
- A doctor's share is recorded when the invoice is made, not when the patient pays.
- The price of an operation includes the team's fees: the team is paid out of it, the patient is not charged extra.
- Medical services have no VAT, so patient invoices have no tax lines.
- Only you can issue credit notes, pay doctors and manage companies. Reception can issue invoices and take money.
  Doctors can give discounts up to the limit.

**Not done in this phase**

- Supplies used in an operation are not priced on the invoice yet (phase 18, with the stock).
- Overdue balances by age and income reports (phase 19).

**How to try it**

1. Start with `run.bat`. At reception, mark a patient as arrived and take her payment. Open "الفواتير": her invoice
   is there.
2. In reception's "more" menu on a visit choose "خصم على الكشف", give a discount with a reason, and see the invoice
   corrected.
3. Open "العمليات والإقامة", book an operation with a price, and open "الحساب" from the operation page. Record a
   deposit with "تسجيل فلوس". Finish the operation, then press "إصدار الفاتورة": the deposit is deducted.
4. In Settings, الدكاترة, give a doctor a percentage of the visit. After the next paid visit open "الفواتير", tab
   "مستحقات الدكاترة", and pay the doctor.
5. In "الفواتير", tab "جهات التعاقد", add a company. On a patient's account open "جهة تعاقد المريضة" and put her on
   it at 80%. Her next visit asks her for 20% only.

---

## Phase 17: suppliers, purchases, taxes, cheques (2026-11-16)

**What was built**

- A new screen "المشتريات والموردين" with four tabs: purchase invoices, suppliers, payments and taxes.
- Suppliers with their tax numbers, payment period and what you owed them before starting. Each supplier has a
  statement and a list of invoices still unpaid, and there is a report of supplier debts by how late they are.
- Purchase invoices: type the supplier's invoice line by line, each line on its expense account, with VAT per line.
  The program works out the tax and the total, refuses the same supplier invoice entered twice, and keeps a scan of
  the paper invoice. Part of an invoice can be returned later.
- Paying a supplier: cash, bank transfer or cheque, for one invoice or several, in part or in full. The tax withheld
  from the supplier (1%, 3% or 5%) is calculated, deducted from what he receives and kept as owed to the tax
  authority.
- Cheques, both ways. A cheque you write waits until the bank pays it, then the bank balance goes down. A cheque you
  receive waits until it is collected, or bounces, or is passed on to a supplier. Each step is one button and can be
  undone. The treasury screen warns about cheques coming due and tells you in red if the bank balance will not cover
  them.
- Recurring expenses (rent, salaries, bills): the program prepares the voucher every month and shows which ones are
  not paid yet. You press one button to record it.
- Tax reports for a quarter: what was withheld from each supplier (the lines of form 41) and the VAT paid on
  purchases. Both export to Excel.

**What was checked**

- 14 tests, including the full story the plan asks for: an invoice with different taxes on its lines, part of it
  paid by a post-dated cheque with tax withheld, the cheque clearing, then a return. The supplier's statement, the
  tax accounts and the bank agree to the piaster.
- A randomized test with hundreds of random sequences of invoices, payments, returns, cancellations and cheque
  steps, checking after every step that each account equals its documents.
- The whole test suite, lint, formatting and migration checks.
- In the browser: the invoices list, a new invoice, an invoice page, a new payment, cheques, a cheque page,
  recurring expenses, the tax report, a supplier's statement and the debts-by-age report.

**Decisions made without asking (standard Egyptian practice, all changeable in the tax settings)**

- The center is treated as not registered for VAT, so VAT paid on purchases is added to the cost. If you are
  registered, tick "تتخصم" on the tax and it is tracked separately.
- Tax is withheld on the value without VAT, and not from a payment of 300 pounds or less.
- Only you can use these screens for now. An accountant role comes in phase 20; you can already make one in Users,
  Roles.
- **Your accountant should confirm the tax rates and these rules before real use.**

**Not done in this phase**

- Buying stock items into the store (phase 18).
- Depreciation of equipment. E-mail alerts for cheques (the warning is on the screen).

**How to try it**

1. Open "المشتريات والموردين", tab "الموردين", add a supplier.
2. Tab "فواتير المشتريات", press "فاتورة مشتريات", type two lines (one with ض.ق.م 14%) and save.
3. Open the invoice and press "سداد". Choose "شيك", a cheque number and a due date, and a withholding rate. Save.
4. Open "الخزن والسندات", tab "الشيكات": the cheque is there. Press "اتصرف من البنك" and see the bank balance go down.
5. Back on the invoice press "مرتجع", type a quantity and a reason. Open the supplier: the statement shows it all.
6. Tab "الضرائب" shows what was withheld this quarter.

---

## Phase 18: the store (2026-11-16)

**What was built**

- A new screen "المخزن" with three tabs: items, issuing and transfers, and counts.
- Items with their unit, the level at which you want to be warned, whether they have an expiry date, and an
  optional price to charge the patient. The list shows how many each store holds and what they are worth, and on top
  it warns about items running low and items about to expire.
- Two stores to start with: the main store and the operating theatre cabinet. You can add more.
- Stock comes in from the purchase invoice: choose the item on the invoice line and it goes on the shelf at what it
  cost. Sending goods back to the supplier takes them off the shelf.
- Issuing to a department, and moving stock between stores.
- In an operation, supplies are picked from the store and come off the shelf at once. An item with a patient price
  goes on her account by itself. One button issues the usual supplies of that operation type. Taking a line off puts
  it back on the shelf.
- Items with an expiry date are tracked by batch, and the batch that expires first is used first.
- Stock counts: full, or a surprise count of a few random items. The person counting does not see what the program
  expects. The differences wait for a second person to approve, then the shelves and the books are corrected.
- Each item has a card showing every movement and the balance after it.

**What was checked**

- 12 tests: the cost of everything issued to the piaster, purchases and returns, expiry batches, transfers, counts
  and who may approve them, operations taking from the store and charging the patient, permissions and exports.
- A randomized test with hundreds of random sequences of purchases, issues, returns, transfers and counts, checking
  after every step that the value on the shelves equals the stock account in the books.
- The whole test suite, lint, formatting and migration checks.
- In the browser: the items list with its warnings, an item card, issuing, counts, the documents list, supplies on
  an operation, and the purchase invoice with stock items.

**Decisions made without asking (standard practice, all changeable)**

- Cost is the weighted average per store.
- Count differences and price differences on returns go to one account, "عجز وزيادة المخزون".
- Supplies without a patient price are considered part of the operation's price.
- Only you can use the store screens for now. A storekeeper role comes in phase 20.

**Not done in this phase**

- More than one unit per item (box and piece). Purchase orders before the invoice.

**How to try it**

1. Open "المخزن", press "صنف جديد" and add two items, one with an expiry date.
2. Tab "الصرف والتحويل", press "رصيد افتتاحي" and enter what you have and its cost.
3. Enter a purchase invoice and choose a stock item on a line: the quantity on the items list goes up.
4. Open an operation, tab "المستلزمات", pick an item: it comes off the shelf. Remove the line: it goes back.
5. Tab "الجرد", start a count, type what is really there, send it for approval, then approve it.

---

## Phase 19: financial statements, reports, books check, year close (2026-11-16)

**What was built**

- **Income statement** (قائمة الدخل): what was earned and spent in any period and what is left, for the whole center
  or one department, with a comparison to the previous period or the same period last year.
- **Balance sheet** (المركز المالي): what the center owns and owes on any date.
- Any number on these statements can be pressed to see the entries behind it.
- **Books check** (فحص الدفاتر): the program proves that the books are sound. It checks that every entry balances,
  that every payment, voucher and invoice has its entry, that the patients', suppliers', doctors', cheques' and stock
  accounts each equal their details, that numbering has no gaps, and that no safe or shelf is negative. It runs by
  itself every night and you can run it any time. If it finds something it e-mails you, and a month cannot be closed
  until it is solved.
- **Year close**: a page with a checklist of what must be done first. Closing moves the year's profit or loss to
  retained earnings and starts income and expenses from zero. A closed year can be reopened, corrected and closed
  again.
- Four new tabs on the reports page: income by doctor and by service, the profit of each operation (price less team
  fees and supplies), what patients owe by age, and cash movement for every safe and bank.
- Everything exports to Excel and prints.

**What was checked**

- Tests that build real documents and compare the statements to them, a randomized test that the balance sheet
  always balances, a test that breaks the books in five different ways and checks that the books check names each
  one, and a test that closes a year, reopens it, corrects it and closes it again.
- The books check on the demo clinic: sound.
- The whole test suite, lint, formatting and migration checks.
- In the browser: both statements (with comparison and by department), the books check, the year close page and the
  new report tabs.

**Decisions made without asking (all changeable)**

- The statements and the books check are for you only for now (an accountant role comes in phase 20). The four
  management reports follow the existing reports permission (doctors and you).
- The books check e-mails the owner at most once a day.

**Not done in this phase**

- A cash-flow statement split by activity, budgets, and depreciation of equipment.

**How to try it**

1. Open "الدفاتر", tab "قائمة الدخل". Choose "الشهر ده", then a department, then a comparison.
2. Press any amount: the account's entries open.
3. Tab "المركز المالي": the two sides are equal.
4. Tab "فحص الدفاتر": press "افحص دلوقتي".
5. Tab "الشهور والسنين": press "إقفال السنة" to see the checklist.
6. Open "التقارير" and try the new tabs.

---

## Phase 20: finishing and hardening (2026-11-16)

**What was built**

- **Four new ready-made roles**, each opening straight onto its own screen: accountant (the books), cashier (the
  cash box), storekeeper (the store), operating-theatre nurse (operations). You can change what each may do, or make
  your own, from Users, Roles.
- **A month of demo activity**: one command fills the demo clinic with a realistic month (visits, payments,
  purchases, cheques, operations, stays, salaries and rent, a contracted company, a stock count). I ran it on your
  demo clinic, so every screen has real-looking content now.
- **Merging two files of the same patient** now moves everything: bookings, visits, prescriptions, attachments, the
  pregnancy file, operations, invoices and payments. It refuses if the duplicate file still has money owed or a
  deposit, and tells you to settle it first.
- **The pregnancy items that were left from phase 12**: ultrasound measurements (BPD, HC, AC, FL) on the pregnancy
  visit; a settings tab "متابعة الحمل" to edit the follow-up schedule; a message to the patient when a follow-up step
  is due and a message after delivery; the pregnancy week can be used in any appointment message; the pregnancy file
  is included when a patient's record is exported.
- **A staff guide in Arabic**: `docs/user-guide.md`, one section per screen with the steps.

**What was checked**

- A test that runs twelve days of the demo month from nothing and then checks that the books verify, the balance
  sheet balances, the stock value equals the books, and every money screen opens.
- A speed test on 23 money screens: none of them gets slower as the data grows.
- The permission tests walk every page as every role and as someone with only one permission, for all the new
  screens: nobody sees a button they cannot use.
- The whole test suite, lint, formatting and migration checks.
- In the browser, with the seeded month: the income statement, operation profit, the roles list, the pregnancy
  schedule settings and the home page.

**Decisions made without asking (all changeable from Users, Roles)**

- The accountant can do everything in the books, the treasury, purchases and the store, but cannot give discounts.
- The cashier takes money and issues invoices but does not see the books.
- The storekeeper counts stock but someone else approves the difference.
- The message after delivery is sent 33 days after the birth (a week before the six-week check).

**What you should do before real use**

1. Ask your accountant to review the chart of accounts and the tax settings.
2. Tell me the answers to the open questions (how many doctors and rooms, VAT registration, how doctors are paid,
   contracts with companies) so the settings match your center.
3. Decide when to push: phases 13 to 20 are committed on your computer and not pushed.

**How to try it**

1. Start with `run.bat` and sign in as the owner. The demo clinic has a month of activity.
2. Open "الدفاتر", tab "قائمة الدخل", choose "الشهر اللي فات".
3. Open "التقارير", tab "ربحية العمليات".
4. Open "المستخدمين", tab "الأدوار": add a user as "كاشير" and sign in as them to see their screen.
5. Open "الإعدادات", tab "متابعة الحمل".
6. Read `docs/user-guide.md`.
