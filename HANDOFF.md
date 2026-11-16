# HANDOFF: DigiClinic, phases 13 to 20

Last updated 2026-11-16. Phases 0-12 are done and committed on `main`. This file tells a new Claude Code session how
to carry the project to the end of phase 20 without the earlier conversation.

## 1. What this project is

DigiClinic is a clinic management system (Django 5.2, PostgreSQL, HTMX, Alpine, Tailwind v4, Arabic RTL). The owner
asked on 2026-11-09 for it to become a complete system for an **Obstetrics & Gynecology center**: pregnancy
follow-up, an operating / delivery room, emergency arrivals, a strong reception flow, and full double-entry accounting
(chart of accounts, suppliers, cheques, taxes, trial balance, stock, cash and stock counts), with reports and
permissions. Her words about the money side: "not a single mistake in maths". She says she knows little about finance
and wants Claude to decide and think deeply.

## 2. Read these first, in this order

1. `CLAUDE.md` (repo root): the working rules and one "notes" section per finished phase. It is long; the sections
   that matter most now are "Working rules", "Non-negotiable conventions", "Quality gates", "Design system (Phase 11)"
   and "Obstetrics notes (Phase 12)".
2. `docs/plan/14-obgyn-center.md`: **the plan for phases 11-20**. Section 14.11 is the roadmap and the open questions.
3. `docs/plan/README.md` for the locked decisions of the original system.

## 3. Where things stand

| Phase | Scope | State |
|---|---|---|
| 11 | Design system, work screen per role, sign-in box | done, commit `b0fe8ad` |
| 12 | Pregnancy file (`apps/obstetrics`): week / month / due date on every screen, obstetric history, antenatal sheet, milestones | done (see the last `phase-12` commit) |
| 13 | Center mode: several doctors in the UI, services catalog, emergency arrivals | next |
| 14 | Accounting core: chart of accounts, journal, periods, treasury, shift close, trial balance | |
| 15 | Operating room, deliveries, newborns, admissions | |
| 16 | Patient invoicing, deposits, doctor shares, payers | |
| 17 | Suppliers, purchasing, taxes, cheques | |
| 18 | Inventory, costing, stock counts | |
| 19 | Financial statements, management reports, books verification, year close | |
| 20 | Hardening: roles, permission matrix, crawl, performance, `seed_center`, user guide | |

Left over from phase 12, to fold into a later phase (listed at the end of "Obstetrics notes" in CLAUDE.md): pregnancy
message templates, a settings screen for the milestone list, structured ultrasound biometry, the pregnancy summary on
the patient export PDF. Phase 15 must link a delivery to `obstetrics.services.close_pregnancy` and add newborns.

## 4. How to work (the rules that are easy to break)

- **One phase at a time.** At the end of each: run the quality gates, add a "<App> notes" section to `CLAUDE.md`,
  commit as `phase-N: <summary>`, report what was done and how to check it by hand, then stop for the owner's go-ahead.
  If she says "continue" or "start", go to the next phase.
- **Quality gates:** `.venv/Scripts/ruff check .`, `.venv/Scripts/ruff format --check .`,
  `.venv/Scripts/python manage.py makemigrations --check --dry-run`, and the whole test suite green.
  The suite takes about 17 minutes: run the tests of the files you touched while working, and the whole suite once in
  the background before the commit (`.venv/Scripts/python -m pytest -p no:cacheprovider -q --tb=short`).
- **Every new screen:** add it to `apps/core/navigation.py` (`ITEMS`), never to a template. Mark it
  `work_screen=True` if a role could work in it alone (the owner wants staff to get one full-window screen each, like
  a separate program: cashier, accountant, storekeeper, theatre nurse).
- **Every new permission:** add it to the role sets, `PERMISSION_GROUPS` and `IMPLIES` in
  `apps/accounts/permissions.py`, gate the view with `@require_perm`, gate every button with `{% if can.x %}`, **and
  write a data migration that adds it to the existing `ClinicRole` rows** (roles are data now; see
  `organizations/migrations/0006_specialty.py` for the pattern). `tests/test_permission_matrix.py` fails otherwise.
- **Every new model:** `TenantScopedModel`, queried with `.for_org(request.organization)`, registered in the admin,
  `simple-history` on clinical and financial records, never hard-deleted. A cross-clinic test and a
  "reception gets 403" test for every new clinical or financial URL.
- **Business logic in `services.py`**, thin views, HTMX partials. Money is `Decimal`, times are stored in UTC and
  computed in `Africa/Cairo`.
- **Design:** load the `design-taste` skill before any interface work. Use the shared classes listed in CLAUDE.md
  ("Design system"), the `{% icon "name" %}` tag (Phosphor, copy new SVGs into `static/icons/`), no emoji as icons and
  no em dashes in interface text (`tests/test_design_system.py` enforces both). Rebuild `static/css/app.css` after
  adding classes; it is committed.
- **Do not ask the owner finance questions she cannot answer.** Pick the standard Egyptian practice, make it a
  setting, write the assumption in the plan and in the end-of-phase report, and say that tax defaults need the
  center's accountant before go-live.
- **Git:** commit locally at the end of each phase. `origin` is the original repo (Zeyad's); the owner's own copy is
  the `clinic` remote (github.com/hanarabeea/clinic) and `main` tracks it. Push only when she asks.

## 5. The accounting phases: what "no mistakes" has to mean (14, 16-19)

The design is in plan 14 §14.5 to §14.10. The parts that must not be skipped:

- One posting service, `ledger.services.post()`, is the only way to create journal entries. Every money-moving
  document (payment, voucher, invoice, supplier bill, cheque event, stock move, shift close) has a function that turns
  it into an entry, idempotent by its `source` (one document posts once).
- Three layers of protection: (1) the service checks; (2) **PostgreSQL**: `CHECK` that a line has exactly one of
  debit / credit above zero, a **deferred constraint trigger** that an entry's debits equal its credits at commit, and
  triggers that reject UPDATE / DELETE on posted entries and lines (corrections are reversal entries); (3)
  `manage.py verify_books` plus a nightly scheduler job (phase 19) that proves the control accounts equal their
  subledgers.
- `Decimal` only, rounded once per line with `ROUND_HALF_UP`; totals are sums of rounded lines; any split (discount,
  tax, shares) uses a largest-remainder allocation so the parts always add up to the whole.
- Gapless numbering per fiscal year under `select_for_update`, closed periods refuse postings.
- Tests: an exact expected entry for every posting function, **property-based tests** (add `hypothesis` as a dev
  dependency) that run random sequences of documents and assert the invariants after every step, a test that raw SQL
  cannot commit an unbalanced entry, and concurrency tests for numbering.
- Phase 14 must also post every existing `billing.Payment` so the ledger starts in agreement with the cashbox.
- Egyptian defaults (all data, not code): medical services VAT-exempt, VAT paid to suppliers treated as cost,
  withholding tax 1% goods / 3% services / 5% professional fees, a standard five-class chart of accounts.

## 6. Assumptions the plan rests on (the owner has not confirmed them)

Several doctors work in the center; there are a few inpatient rooms charged per night; stock of medical supplies is
tracked with expiry dates; the center is not VAT-registered. "Spot checks" was read as surprise cash counts and stock
counts. If she corrects one, update plan 14 §14.0 first, then the code.

## 7. Running it on this machine (Windows, no Docker)

- Everything: `run.bat`. Database only: `.venv/Scripts/python scripts/devdb.py start` (it stops when the PC
  restarts; start it again before tests or the server).
- Preview in Claude Code: `preview_start` with the name `digiclinic` (port 8010). It runs with `--noreload`, so
  **restart the preview server after editing a template** to see the change.
- Test logins for the local demo clinic are in `tmp/dev-login.txt` (not in git): the owner account and a
  receptionist. Demo data: `manage.py seed_demo`, `seed_pregnancies`, `simulate_day`.
- First time on a new machine: `.venv/Scripts/python -m playwright install chromium` (PDF and dictation tests).
- In the Bash tool, a Python heredoc that contains many quote characters fails to parse on this setup: write the
  script to a file in the scratchpad and run the file.
- The demo clinic is an OB/GYN clinic (`OrganizationSettings.specialty = "obgyn"`); the pregnancy layer is off for
  clinics with the general specialty.

## 8. Text to paste into a new Claude Code session

> Read `HANDOFF.md`, then `CLAUDE.md`, then `docs/plan/14-obgyn-center.md`. Continue the OB/GYN center plan from the
> next unfinished phase in the HANDOFF table and keep going one phase at a time until phase 20. For each phase: build
> it fully with tests, run the quality gates, check the new screens in the browser preview, add the phase's notes to
> `CLAUDE.md`, update the table in `HANDOFF.md`, commit as `phase-N: <summary>`, and give me a short report in plain
> language: what was built, what you checked, what is not done, and how I can try it. Then wait for me to say
> "continue". Do not ask me accounting or tax questions: choose the standard Egyptian practice, make it a setting,
> and write the assumption down. Load the design-taste skill before any interface work. Do not push unless I ask.
