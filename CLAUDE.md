# CLAUDE.md — Execution Instructions

You are implementing **DigiClinic**, a clinic management system by Digitiva, forked from Marsool Al-Barq
(a quotation system). The plan in `docs/plan/` is the source of truth. Read `docs/plan/README.md` first, then
the files relevant to the current phase.

## Working rules
- Work **one phase at a time** from `docs/plan/08-roadmap.md` (phases 11-20: `docs/plan/14-obgyn-center.md`). At the end of each phase: run the quality gates,
  report against its Definition of Done, list how the user can verify it manually, commit `phase-N: <summary>`,
  then STOP and wait for approval.
- If a decision is not covered by the plan, **ask**. Do not silently change locked decisions in `docs/plan/README.md`.
  If you think one is wrong, say so and propose.
- **Development is local on Windows WITHOUT Docker.** `run.bat` does everything (uv sync → embedded PostgreSQL via
  `pgserver` → Tailwind → migrate/seed → WhatsApp gateway → `run_scheduler` → runserver).
- **DigiClinic must coexist with Marsool Al-Barq on the same machine**: data in `%LOCALAPPDATA%\digiclinic`,
  Postgres port 54339, DB `digiclinic`, Django 8010, WhatsApp gateway 3320. Never reuse the Al-Barq paths/ports.
- Production (Phase 10) depends on a hosting decision that is still open (see `07-operations.md` §7.6).

## Stack (locked)
Python 3.12 (uv) · Django 5.2 LTS · PostgreSQL 16 · HTMX 2 · Alpine.js 3 · Tailwind v4 standalone ·
Chromium/Playwright for PDFs · WhatsApp via local Node gateway `whatsapp-gateway/` (whatsapp-web.js) · SMTP ·
`rapidfuzz` for drug matching. **No Celery/Redis**: one `manage.py run_scheduler` process (local and server).
No Node build step for the Django app; HTMX/Alpine/fonts vendored in `static/`.

## Domain rules
- One clinic = one `Organization`. UI shows one doctor, but **every scheduling/clinical model has a `doctor` FK**.
- Booking mode per doctor: `slots` (exact time) or `queue` (number + estimated time). Both must work.
- Appointment changes go through `apps/scheduling/services.py` only (`book`, `reschedule`, `cancel`, `transition`,
  `mark_no_shows`). Every write for a doctor/day happens under `select_for_update()` on `DayLedger`.
  Reschedule = old appointment `rescheduled` + new one. Every time change bumps `Appointment.version`.
- **Every outgoing message is a `ScheduledMessage` row** (outbox). Nothing sends directly. Before sending, the
  dispatcher re-checks freshness (`appointment_version`, status, `min_lead_minutes`) and renders text at send time.
  `dedupe_key` is unique. Quiet hours apply. No messages without `patient.messaging_consent`.
- No-show policy, reminder offsets, templates, quiet hours, prescription sending: all from settings, never hardcoded.
  Auto-rebook happens at most once per chain (`auto_rebooked_from`).
- Prescriptions: drafts editable; `final` is immutable (edits create revision `-R1`). PDF in two print modes
  (`full` / `preprinted` with margins in mm); the copy sent to patients is always `full`. Allergy warnings must be
  acknowledged. Mixed Arabic/English text uses isolated bidi spans.
- Voice dictation: browser Web Speech API behind a JS provider interface. Dictated lines must be confirmed by the
  doctor before a prescription can be finalized.
- Times stored UTC, computed in `Africa/Cairo` with `zoneinfo` (DST-safe). Money `Decimal`. Phones E.164 (`apps/core/phones.py`).

## Non-negotiable conventions (inherited from Al-Barq)
- Every operational model inherits `TenantScopedModel`; views query via `.for_org(request.organization)`.
  Never `Model.objects.all()` in views. Cross-tenant test for every new view.
- Permissions via `@require_perm(...)` in views (`apps/accounts/permissions.py`), not only in templates.
  Reception must get 403 on clinical URLs (test it).
- Business logic in `services.py`; views stay thin. HTMX partials for interactive screens.
- Secrets only from `.env`; DB-stored credentials Fernet-encrypted (`apps/core/crypto.py`).
- Private files (logos, prescriptions, attachments) served via permission-checked views only.
- Medical records are never hard-deleted; `simple-history` on clinical models; `audit.log` for clinical views.
- No medical text or full phone numbers in logs.
- UI Arabic RTL, strings wrapped for i18n. Arabic search normalizes أ/إ/آ→ا, ة→ه, ى→ي, strips tatweel/diacritics.

## Quality gates per phase
- `.venv/Scripts/ruff check .` + `ruff format --check .` clean, `.venv/Scripts/python -m pytest` green,
  migrations committed, no TODOs in phase scope, new models registered in Django admin.
- Time-dependent logic is tested with a frozen/injected clock (no sleeping tests).

## Update this file
At the end of each phase, add a short "<App> notes" section here (like Al-Barq's CLAUDE.md had) with the commands,
gotchas and conventions introduced in that phase.

## Doctors notes (Phase 1)
- `apps/doctors`: `Doctor` (single row per org in v1 — `views._get_doctor(org)` gets-or-creates it on first visit
  to the settings screen), `WorkingPeriod`, `ScheduleException` (`closed`/`custom`/`extra`), `VisitType`.
  `simple-history` on `Doctor`/`WorkingPeriod`/`VisitType`.
- `apps/core/timeutils.py`: `CAIRO` zoneinfo, `weekday_egypt()` (Python Monday=0..Sunday=6 → Egyptian
  Saturday=0..Friday=6: `(python_weekday + 2) % 7`), `local_dt()` builds a DST-correct aware datetime directly
  from a `date` + `time` — never do manual UTC offset math for clinic hours.
- `apps/scheduling/availability.periods_for(doctor, day)` is the only place that resolves a day's actual working
  periods (weekly schedule + exceptions). `scheduling` has no models yet — `Appointment`/`DayLedger` land in Phase 3.
- Settings screens live under `org/settings/doctor/` (profile + weekly grid + exceptions, HTMX partials swapped
  in place) and `org/settings/visit-types/`. Both gated by the `schedule.manage` permission (doctor/admin/owner).
- `seed_org` now also seeds the doctor/working periods/visit types from `clinic.json`'s `doctor`/`working_periods`/
  `visit_types` keys (same create-or-skip-unless-`--force` rule as the org settings).

## Patients notes (Phase 2)
- `apps/patients`: `Patient`, `Allergy`, `ChronicCondition`, `PatientFieldDefinition` (model only — wired into
  forms in Phase 6), `PatientSequence` (one row per org; `services.next_file_number()` assigns `file_number`
  under `select_for_update()`, same pattern as Al-Barq's old `QuoteSequence`). `simple-history` on `Patient`.
- `services.create_patient(org, instance, by=...)` is the only way to create a patient (wraps file-number
  assignment in `transaction.atomic()`); build the unsaved instance via `PatientForm.save(commit=False)` first.
- Phone is intentionally **not unique** (families share one number) — `services.possible_duplicates()` only
  warns, never blocks. Search (`PatientQuerySet.search()`) matches partial phone digits (any of
  `010.../+2010.../2010...`), `normalize_ar`-matched name, or file number.
- Permissions: `patient.view_basic`/`edit_basic` (reception+) cover demographics, contact info and **allergies**
  (shown as a safety banner per 04§4.3). `patient.view_medical`/`edit_medical` (doctor/admin/owner) gate
  `ChronicCondition` — reception's patient detail page has no "الأمراض المزمنة" section at all. `patient.merge`
  (doctor/admin/owner) via `services.merge_patients()` (moves allergies/chronic conditions, sets `merged_into`,
  deactivates the duplicate — appointments/visits/attachments move too once those apps exist).
- `import_patients <xlsx> --org <slug>` (columns: الاسم، الموبايل، السن، النوع، ملاحظات — idempotent, matched by
  name+phone) and `seed_demo [--org] [--remove]` (30 fake patients, 3 sharing one phone number).

## Scheduling notes (Phase 3)
- `apps/scheduling`: `Appointment`, `AppointmentEvent`, `DayLedger` (one row per doctor+day; every write for that
  day happens under `select_for_update()` on it — this is the only place conflicts/races are prevented).
  `availability.Period.period_id` (`wp-<pk>`/`exc-<pk>`) is stored on `Appointment.period_key` instead of a
  `WorkingPeriod` FK, since queue periods can come from a `ScheduleException` (custom/extra) that has no
  `WorkingPeriod` row — the schema note in `02-database-schema.md` assumed every period is a `WorkingPeriod`; this
  is the one locked-doc deviation so far, flagged here per CLAUDE.md's own rule.
- `services.py` is the only way to touch an appointment: `free_slots`/`queue_availability` (read-only),
  `book`/`reschedule`/`cancel`/`transition` (all `@transaction.atomic`, lock the `DayLedger` first).
  `ALLOWED_TRANSITIONS` in `models.py` is the full state machine (12-booking-engine §6); anything else raises
  `InvalidTransition`. Queue numbers are **never reused** — `DayLedger.queue_numbers` is `{period_id: last number}`,
  only ever incremented, even when an appointment in that period is cancelled.
- Free follow-up pricing (`_price_for`): a follow-up `VisitType` is priced 0 and gets `followup_of` set when the
  patient has a `completed` appointment with the same doctor within `visit_type.free_followup_days`.
- Closing a day (`doctors:exception_add` with kind `closed`/`custom`) checks `affected_by_closing()` for the date
  being closed; if any active appointment no longer fits, the exception still saves but the response carries
  `HX-Redirect` to `scheduling:affected` instead of swapping the exceptions partial — staff reschedule each
  affected appointment to a freshly-computed suggestion (`reschedule_to_suggestion`) one at a time.
- Booking screen (`/booking/`) is a single doctor-scoped flow: search (reuses `Patient.search`) → quick-add if not
  found → visit type + day picker → `/booking/<patient>/slots/` (HTMX partial, branches on `doctor.booking_mode`)
  → `POST /booking/<patient>/confirm/` → redirects to a cleared `/booking/` with a toast (matches the "search
  screen returns empty, ready for the next patient" UX in `03-modules-and-ux.md` §3.2). The actual "ابعت رسالة
  تأكيد" / scheduled-reminders part of that screen's mockup is Phase 4 (`notifications` doesn't exist yet) —
  `book()` has a commented-out `transaction.on_commit` hook marking where it plugs in.
- Permissions added: `appointment.view` (all roles), `appointment.book`/`reschedule`/`cancel` (reception/doctor/
  admin/owner), `appointment.overbook` (doctor/owner only — reception's "حجز استثنائي" button is hidden, not just
  disabled, when the period is full and they lack this permission).
- Tests needing a frozen clock use `freezegun` (new dev dependency) — Egypt's DST (2026: Apr 25 → Oct 30) matters
  for same-day "is this slot still in the future" checks, not just for `periods_for` from Phase 1.

## Notifications notes (Phase 4)
- `apps/notifications`: `NotificationSettings` (one per org, `for_org()` get-or-creates), `NotificationTemplate`,
  `ScheduledMessage` (the outbox), `SchedulerHeartbeat`, `InboundMessage`. `simple-history` on settings/templates.
  `messaging.Delivery` now has a `scheduled_message` FK (one `Delivery` per send attempt); `messaging.WhatsAppNumber`
  caches the gateway's `isRegisteredUser` result for 30 days.
- `services.py` only **enqueues** (`schedule_for`, `on_rescheduled`, `on_cancelled`, `enqueue`, `cancel_pending`);
  `dispatcher.Dispatcher.run_once()` is the only code that sends. `scheduling.services.book/reschedule/cancel`
  call the hooks via `transaction.on_commit` — tests must use `django_capture_on_commit_callbacks(execute=True)`
  to see rows. `book(notify=False)` is used inside `reschedule()` (the "rescheduled" message replaces a fresh
  confirmation). Phase 5 adds `on_no_show`/`on_queue_progress` on top of `enqueue()`.
- `rescheduled` messages hang off the **new** appointment with `context={"old_date","old_time"}`; `cancelled`
  messages hang off the cancelled one. Freshness guard (`Dispatcher.freshness`) per event: version mismatch →
  `cancelled`, wrong appointment status → `cancelled`, less than `template.min_lead_minutes` left → `skipped`,
  quiet hours re-applied at send time (a scheduler restarting at night defers instead of sending).
- Quiet hours wrap midnight. `NotificationSettings.urgent_until` (default 23:00) is the plan's "rescheduled/cancelled
  for today/tomorrow still go out in quiet hours before 11pm" knob — added as a field since 06 §6.5 says it's a setting.
- Retries: `MESSAGING_RETRY_DELAYS` (60/300/900 s). WhatsApp `not_ready`/5xx are temporary; after the last retry →
  `failed` + an email fallback row (`<dedupe_key>:fallback`) if `email_fallback` and the patient has an email.
  Rows stuck in `sending` after a crash are marked `failed` on scheduler start (`recover_interrupted`) — never
  auto-resent, to avoid duplicates.
- `manage.py run_scheduler [--once]`: `pg_try_advisory_lock` (a second copy refuses to start), heartbeat every
  tick (`SCHEDULER_TICK_SECONDS`=20), `JOBS` list (dispatch every tick, cleanup daily — Phase 5 adds
  `mark_no_shows` there). Screens warn when the heartbeat is > 2 minutes old. `run.bat` starts it minimized.
- WhatsApp anti-ban gap: the dispatcher sleeps `wa_min_gap_seconds` + 0–10 s jitter between sends in-process;
  disabled in tests via `NOTIFY_SLEEP_BETWEEN_WA=False` (gap tests inject `sleep`/`monotonic`/`jitter`).
- Templates screen: `org/settings/messages/` (settings.manage) — variable buttons, live HTMX preview with sample
  data, "ابعت تجربة" (an `event=test` outbox row sent synchronously via `dispatcher.send_now`). Unknown `{vars}`
  are rejected at save time (`templating.validate_body`). Message log: `/messages/` (`messages.view` /
  `messages.retry`: reception/doctor/admin/owner, not viewer), filterable by status/event/day/appointment.
- Gateway: new `GET /sessions/:id/check/:number` and inbound 1:1 messages forwarded as `event: "message"` →
  `InboundMessage` (stored only, matched to a patient by phone).
- `seed_notifications [--org] [--force]` creates the 9 default templates (06 §6.3) + settings from `clinic.json`;
  run by `run.bat` after `seed_org`, idempotent.
- Local setup gotcha: on this machine pip under Python 3.10 fails TLS verification (intercepting proxy/AV);
  uv was installed with `C:\Python313\python.exe -m pip install --user uv` and `run.bat` now sets `UV_NATIVE_TLS=1`.

## Reception notes (Phase 5)
- New apps: `reception` (screen only, no models), `billing` (`Payment`; `services.balance()` computes
  due/paid/status — pass a prefetched payments list to avoid N+1; `record_payment()`; `cashbox(org, day)`), and
  `clinical` with `Visit` + `Vitals` **created early** because vitals are recorded at check-in (Vitals is a
  OneToOne on Visit per the schema). `Visit.for_appointment(appt)` get-or-creates the open visit. Visit's own
  `history` TextField (medical history) clashes with simple-history's default name, so its HistoricalRecords
  manager is `Visit.records`. Phase 6 builds the doctor desk on these models.
- `scheduling.services` additions: `walk_in()` (same-day book + immediately `arrived`, source=walk_in → no
  confirmation), `mark_no_show()` (applies `no_show_policy`; auto-rebook via `find_rebook_slot` = closest time of
  day, `auto_rebook_after_days` later, within `auto_rebook_window_days`; never for an appointment that is itself
  `auto_rebooked_from` something), `undo_no_show()` (cancels the auto-rebooking **silently** — `cancel(notify=False)`),
  `due_no_shows()/mark_no_shows()` (scheduler job every 2 min: slots → `start + grace`; queue → after the period
  ends, or never when `no_show_queue_mark_at=manual`). `transition()` backwards ("رجوع خطوة") clears the
  undone step's timestamp.
- `notifications.on_no_show(appt, rebooked)` and `on_queue_progress(doctor, day)` (fired on_commit by
  `transition()` to in_consultation/completed for queue appointments). "Ahead" counts every earlier number not yet
  completed/no-show/cancelled — the patient currently with the doctor counts. `near_turn` is never skipped for
  "time passed" (queue estimates run late), only if the patient already arrived.
- Reception screen `/reception/` (`reception.operate`: reception/doctor/owner — **admin does not get it**, per the
  matrix). Polls `/reception/rows/?sig=` every 5 s; the server returns 204 when the row signature is unchanged, and
  polling pauses while a modal is open. Actions return the fresh rows partial + an `HX-Trigger: toast` event;
  invalid modal forms come back with `HX-Retarget: #modal`. "خلص" is on the reception screen for now (the doctor
  desk takes over in Phase 6). Cashbox `/cashbox/` (`payment.record`).
- New permissions: `reception.operate`, `vitals.record`, `payment.record` (reception/doctor/owner),
  `report.finance` (doctor/owner, used by Phase 9 reports).
- `simulate_day [--remove]` books 10 seed_demo patients today through the real services (3 done+paid, 1 with the
  doctor, 2 waiting with vitals, 1 no-show, rest booked) and cancels every outbox row it caused — nothing is sent.

## Clinical notes (Phase 6)
- `clinical`: `Visit`, `Vitals` (from Phase 5) + `Attachment` (private storage under
  `org_<id>/patients/<file_number>/<random>.<ext>`, type checked by **content** — `%PDF-` header or Pillow
  JPEG/PNG — max 15 MB, served only via `clinical:attachment_file`; never deleted, `is_archived` instead).
  `Visit.followup_handled` drives reception's "إعادات محتاجة حجز" task (`services.followups_due()`; auto-handled
  once the patient has a booking created after the visit finished, or dismissed by reception).
- `clinical.services`: `start_visit` (call in / reopen; always re-reads the appointment), `call_next`,
  `save_field` (autosave whitelist: text fields, `followup_after_days`, `cf_<key>` visit custom fields;
  `diagnosis` also fills `diagnosis_tags`, split on `، , ; newline`), `finish_visit` (→ appointment `completed`;
  **Phase 7 plugs prescription sending/the draft check in here**), `diagnosis_suggestions`, `add_attachment`,
  `log_clinical_view` (one audit event per user×patient×hour).
- Doctor desk `/desk/` (`clinical.view`; edits `clinical.edit`): queue sidebar (polls every 10 s), visit page with
  tabs (current visit / history with weight & BP sparklines / attachments / data). `/desk/` auto-opens the patient
  currently `in_consultation` (creating the Visit if reception called them in). Any patient's file:
  `/desk/patients/<pk>/` (search box in the sidebar, or "الملف الطبي" on the patient page).
- Autosave: per field, 1 s debounce + on change/blur, `navigator.sendBeacon` flush on `pagehide`/hidden, **plus a
  localStorage draft per field** re-applied on load until the server confirms — closing the tab mid-sentence loses
  nothing (an immediate reload could otherwise race the beacon).
- Idle lock (04 §4.1, changed from 15 min to **6 h** at the clinic's request; `DESK_IDLE_LOCK_MINUTES`): JS posts `/desk/lock/` after that many idle minutes; the `desk_unlocked` decorator then serves the
  lock page (HTTP 423) for every desk view until `/desk/unlock/` gets the user's password. Session age is now 8 h.
- Permissions: `clinical.view`/`clinical.edit` (doctor/owner), `attachment.upload` (reception/doctor/owner —
  reception uploads from the patient page but can't list/view attachments). `test_reception_gets_403_on_every_
  clinical_url` walks `apps/clinical/urls.py` automatically, so new clinical URLs are covered by default.
- Custom fields (`PatientFieldDefinition`): settings tab "ملف المريض" (`/org/settings/patient-fields/`,
  settings.manage; fields are switched off, never deleted). `apps/patients/custom_fields.py` builds form fields and
  JSON values; `PatientForm(org=...)` adds scope=patient fields (quick-add in booking omits them on purpose).
- Reception's "خلص" button from Phase 5 is still there (useful when the doctor doesn't use the desk).

## Prescriptions notes (Phase 7)
- `apps/prescriptions`: `Drug`, `DosePhrase`, `PrescriptionSettings` (one per org: page size, print mode, pre-printed
  margins, voice on/off, `reception_can_reprint`), `PrescriptionSequence` (org+year, `RX-2026-00042`), `Prescription`
  (+ `PrescriptionItem`, simple-history), `PrescriptionTemplate(+Item)`. "Send after visit" + channel live on
  `NotificationSettings` (already there from Phase 4) but are edited on the Rx settings tab.
- Lifecycle (`services.py`): draft (editable) → `finalize()` (number, patient/doctor **snapshots**, immutable; every
  warning key must be in `acknowledged`; any `needs_review` line blocks — Phase 8 sets it for dictated lines) →
  `revise()` = new draft `-R1` sharing the number (original untouched) → `void()` with reason. Never deleted.
  `finish_visit()` refuses while a draft with lines exists, and (if enabled) calls `notifications.send_prescription`.
- Safety (`safety.py`): allergy text vs trade/generic/Arabic aliases + a small drug-class table (`DRUG_CLASSES`:
  Penicillin/بنسلين → amoxicillin, flucloxacillin…; sulfa, NSAIDs, macrolides, quinolones) + duplicate lines. It's a
  safety net — extend `DRUG_CLASSES` rather than adding ad-hoc checks.
- PDF: `apps/documents/pdf.render_pdf()` keeps **one warm Chromium on a dedicated thread** (Playwright's sync API is
  thread-bound; Django serves from many threads). `warm_up()` is called when the Rx tab opens (skipped when
  `settings.TESTING`). `full` PDF is rendered once at finalize (`ensure_pdf`, on_commit, never raises) into
  `pdf_full`; `preprinted` is rendered on the fly for printing; patients always get `full`. Template:
  `templates/pdf/prescription.html` — every mixed-direction run is an isolated `dir` span (05 §5.3).
  `render_rx_samples` writes `tmp/rx-sample-{full,preprinted}.pdf` + `tmp/rx-calibration.pdf`.
- Builder (desk → "الروشتة" tab): search (Arabic aliases too) — **Enter adds the best catalog match** (server-side,
  so it works even before the dropdown arrives); "+ إضافة" (`free=1`) adds free text; cells autosave; dose phrases
  insert into the last focused cell; finalize returns the builder + `printRx()` (hidden iframe → print dialog).
- Sending: `ScheduledMessage.prescription` FK; the dispatcher attaches the stored PDF (`send_document` on WhatsApp,
  email attachment) when the template has `attach_pdf`. Dedupe `rx:<pk>:<channel>`.
- Permissions: `prescription.write`/`print`, `drug.manage`, `rx_template.manage` (doctor/owner). Reception can
  reprint (numbers/dates only, no drug names) only with `reception_can_reprint`.
- `import_drugs [csv] [--update]` (run by `run.bat`): create-only unless `--update`; seeds dose phrases once.
- Local env gotcha found here: `.env` had `MEDIA_ROOT=C:/Users/zeyad/...` from another machine, so no upload/PDF could
  be stored. `MEDIA_ROOT` now defaults to `%LOCALAPPDATA%\digiclinic\media`; leave it empty in `.env`.

## Dictation notes (Phase 8)
- `static/src/dictation.js` (served as `src/dictation.js`, no build step): any field with `data-dictate` gets a 🎤 +
  ع/EN toggle (language remembered in localStorage). Click / Ctrl+Space toggles; 3 s silence stops; interim text grey
  under the field; finals inserted **at the cursor** and fire `input` (so the field's autosave runs). Commands:
  "سطر جديد" → `\n`, "نقطة" → `.`. Provider interface (`WebSpeechProvider` now; a Whisper provider can be dropped in).
  Unsupported browser (Firefox/Safari, or non-secure origin) → no mic, and `[data-dictation-unsupported]` is shown.
- Loaded only on desk pages and only when `PrescriptionSettings.voice_dictation_enabled` (`voice_enabled` in the
  desk context). HTMX-loaded content is scanned on `htmx:afterSettle`.
- Prescription mode (`data-dictate="rx"` on the drug search box): finals are collected (each pause = a line) and POSTed
  to `prescriptions:dictate` → `matching.match_text()` → top-3 suggestions per line in `#rx-dictation` (outside
  `#rx-builder`, so picking one line keeps the rest). Picking → `dictate_add` adds the line with **`needs_review=True`**
  (finalize stays blocked until "✓ تأكيد") and `learn_alias()` stores the spoken form on the drug.
- `apps/prescriptions/matching.py`: normalize (Arabic + digits) → best of the first 1–3 words vs name/generic/aliases
  with rapidfuzz `WRatio` (threshold 80), ties → longer match, then `usage_count`. The rest of the line becomes the
  instructions with spoken numbers → digits; a spoken strength ("واحد جرام", or a bare number equal to the drug's own
  strength) is dropped from the instructions.
- JS is tested in real headless Chromium from pytest (`tests/test_dictation.py`) with a fake `SpeechRecognition`. Note:
  newer Chromium has an unprefixed `SpeechRecognition` too, and Playwright must run in a worker thread (its event loop
  trips Django's async-safety check otherwise).

## Polish notes (Phase 9)
- Dashboard (`apps/dashboard/services.py`): `day_counts`, `report(org, start, end)` (visits, new patients, no-show
  rate, revenue by method net of refunds, top diagnosis tags, reminder delivered/read rates from `Delivery` acks,
  per-day table) — aggregate queries only. Home is role-based; `/reports/` needs `report.finance` (doctor/owner).
- Backups: `manage.py backup` / `restore <file> --database NEW --media-root EMPTY [--create-db] [--check]`
  (`apps/core/backup.py`): pg_dump -Fc + private media → tar.gz → Fernet (`BACKUP_KEY`, default
  `FIELD_ENCRYPTION_KEY` — losing that key loses every backup). Restore **only into an empty DB and an empty media
  folder**. `run_scheduler` makes one backup per day (hourly check, keeps 14; `AUTO_BACKUP=False` turns it off).
  Postgres client binaries: `PG_BIN` → pgserver's bundled ones → PATH.
- Security fixes from the review (`docs/security-review.md`): CSP + Referrer/Permissions-Policy middleware,
  `X_FRAME_OPTIONS = "SAMEORIGIN"` (DENY breaks the print iframe), per-user rate limit on the 4 patient-search
  endpoints (`apps/core/ratelimit.py`, 120/min; decorated views carry `ratelimit_key`), `Patient.consent_recorded_by`,
  patient export PDF (`clinical:export`, audited).
- Patient page now shows bookings, payments (payment.record) and sent + inbound messages (messages.view).
- N+1 guard: `tests/test_performance.py` compares warm query counts at small vs large data — keep it green when
  touching the reception/desk screens.
- Week calendar (03 §3.3, added after Phase 9): `/appointments/week/?date=` (Saturday→Friday, `views.week_start`),
  day/week toggle on both views. Each day shows its working periods (`periods_for`) or "مفيش شغل", exceptions with
  reason, bookings colored by `VisitType.color` + status icon. Drag a booked/confirmed card onto a future day (native
  HTML5 DnD) or press "تأجيل" (phones) → `reschedule_options` dialog with that day's free slots / queue periods →
  `reschedule_confirm` → `services.reschedule()` (so the patient gets the "rescheduled" message and old reminders are
  cancelled). "قفل" per day (`close_day`, schedule.manage) → closed exception → affected-bookings screen if needed.

## Deployment notes (Phase 10 — VPS, prepared; not yet run on the server)
- `deploy/`: `Dockerfile` (web + scheduler image: Python 3.12, uv, Postgres 16 client for backups, Playwright
  Chromium for PDFs, non-root, gunicorn gthread 2×4), `whatsapp.Dockerfile` (Node 20 + Debian chromium),
  `compose.yml` (db/web/scheduler/whatsapp; only web published, on 127.0.0.1:${WEB_PORT}), `env.production.example`,
  `nginx/digiclinic.conf` (new site file only, certbot `certonly --webroot`), `cron` (health alerts every 5 min +
  off-site copy), `offsite-backup.sh` (rclone; files are already encrypted), `README.md` (the runbook — review first).
- App changes for it: `/healthz/` (db + scheduler, no data), `manage.py check_health [--alert]` (owner e-mail, once per
  problem per 6 h via `ops.alert` audit events; run from host cron so it works when the scheduler is down),
  `SECURE_REDIRECT_EXEMPT` for the internal webhook + healthz, `EMAIL_URL`, `SITE_URL`, gateway `WA_GATEWAY_HOST`,
  gunicorn dependency. Upload images are shrunk to 2000 px / JPEG 85 (07 §7.2) in `clinical.services.shrink_image`.
- Docker isn't available on the dev machine: the images have not been built here. First build happens on the server.

## Drug categories (added after Phase 10 prep)
- `apps/prescriptions/specialties.py`: the clinic's 29 specialties + 24 sub-specialty presets (category order + level,
  as given 2026-10-05). `DrugCategory` = a category name shared in the org (drugs are tagged with it, M2M
  `Drug.categories`); `DoctorCategory` = one doctor's buttons (order/level). `Doctor.drug_specialty` = the preset key.
  Logic in `apps/prescriptions/categories.py` (`apply_specialty` replaces the doctor's list; `add` reuses an existing
  name case-insensitively; `move`). Settings → "تصنيفات الأدوية" (`prescriptions:categories`, drug.manage).
- Builder: chip row above the search (`#rx-cats`); the chosen category is kept per doctor in localStorage and sent as
  `category` with search / add / ＋📚 (`#rx-category`). With a category, `search_drugs` = that category's drugs whose
  trade name, generic name or an Arabic alias **starts with** the text (empty text lists the category); without, the
  old "contains" search with prefix matches first. All levels are shown, in order (level only colours a dot in
  Settings). Category names stay English.
- `import_drugs` reads an optional `categories` column ("A|B"); the 32 seed drugs are tagged. The clinic will send a
  full drug file later — names must match the preset names (new names are created).
- HTMX gotcha fixed here: elements inside a form with `hx-swap="outerHTML"` inherit it — give inner hx-targets an
  explicit `hx-swap="innerHTML"` (the drug search box lost its results container after the first keystroke).

## Egyptian drug database (added after the categories feature)
- `apps/prescriptions/data/egyptian-drugs.csv` (+ README): 25,070 products registered in Egypt, June 2026, **CC0**
  (github.com/karem505/egyptian-drug-database). `manage.py import_egyptian_drugs [--update] [--recategorize]`
  (run by run.bat; idempotent; bulk): `source="eg-db"`, manufacturer, form guessed from the product name then the
  route, the Arabic transliteration as an alias (Arabic search + voice), categories from `eg_classes.py`.
  Drugs the clinic added (`source=""`) are never touched. Prices are not imported.
- `eg_classes.py`: keyword rules on the source `drug_class` (+ route refinements, + a few ingredient rules);
  `"!WORD"` excludes. Audited by hand: the source labels are messy (apixaban filed under "ANTIPLATLET", antidotes
  named after what they reverse, "SOMATOSTATIN" contains "STATIN"...) — `test_clinically_sensitive_mappings` pins the
  fixes; after changing rules run `--recategorize`. ~17k of 25k products get a category; the rest are mostly
  cosmetics/personal care (searchable under "الكل", ranked after medicines).
- Voice matching at this scale (`matching.py`): a per-org cached index of the first 1–3 words of every name,
  scored with rapidfuzz `cdist(..., fuzz.ratio, workers=-1)` (numpy dependency). ~0.2 s per dictated line on 25k
  drugs; the index rebuilds when the catalog's count/max(updated_at) changes (~1.5 s).
- Category search puts trade names starting with the text first, then generic/alias prefix matches.

## Conversational dictation notes
- `apps/prescriptions/extraction.extract(org, transcript)` pulls medicines out of free conversation: 1–3 word
  windows (stopwords/units/modifiers/numbers can't start a name, و/ال clitics stripped) scored with `cdist` against
  the voice index; weak matches need a regimen nearby. A number/unit/modifier after the name is the strength (picks
  the matching variant, never ends up in instructions). Regimen = whole-word `PATTERNS` (dose/freq/timing/duration)
  up to the next mention; everything else is ignored. Same drug said twice → one line.
- `dictate` view uses it first and falls back to line-by-line `match_text` only when no medicine is recognised.
  `dictate_add` now accepts `duration`.
- Robustness rules in `extraction.py` (each pinned by a test in `tests/test_dictation.py`): prefix variants (و/ال/ب/ف/ك/ل
  tried, never forced), sound-alike letters (`_SOUNDS`, ر/ل…), names heard split in two ("اوج منتين"), spoken English
  (`SPOKEN_EN`, "اي 1" → A1), Arabic vs English-only names by consonant skeleton (weak: needs a dose), glued numbers
  ("كونكور5"), compound numbers ("ميتين وخمسين"), number ≥ 20 after a name = strength, number + مل/نقط = dose,
  `NEGATIONS` before a name drop it ("بلاش البنادول"), `CORRECTIONS` after it drop it ("لا قصدي"), `TYPES` near the
  name pick the form, `VARIANT_WORDS` (PLUS/XR…) only when said, repeated phrases written once.

## Doctor workspace (visit page redesign)
- `/desk/visits/<pk>/` is one workspace: sticky compact header (allergies always red), `snapshot.html` (last visit,
  last prescription, latest readings, "اتغير من الزيارة اللي فاتت"), the visit form and the prescription builder side by
  side (builder loads with `hx-trigger="load"`), a fixed action bar, and a drawer (`openDrawer(url, title)`) for history/
  prescriptions/attachments/data/a previous visit. Nothing navigates away while a visit is open.
- `apps/clinical/workspace.py` (read-only summaries + `reuse`): `previous_visit`, `snapshot`, `changes_since` (only values
  recorded in both visits; never inferred), `quick_phrases` (doctor's own ≥2-use segments, topped up with neutral
  `DEFAULT_PHRASES`), `reuse(visit, source, fields)` (empty field → filled, written field → old text appended, prescription
  → `rx_services.repeat_from` which skips drugs already there). No schema changes.
- New URLs: `clinical:visit_compare` (drawer: previous vs current + reuse checkboxes), `clinical:visit_reuse` (POST →
  `HX-Refresh`), `clinical:poll` (5 s "مريض جديد جاهز" after finishing). `visit_finish` with `HX-Request` finishes in place
  (renders `finished.html`) so the print iframe survives; without HTMX it still redirects as before.
- Finish flow (`finishVisit()` in `visit.html`): flush autosave → ✓/⚠ summary (never blocks) → if the draft prescription
  has lines, submit `#rx-finalize` and continue only when `#rx-builder[data-status]` turns `final` (warnings/voice lines
  still need the doctor) → finish. Shortcuts: `/` drug search, Ctrl+Enter finish, Esc closes the drawer.
- Prescription: `rx_services.current_for_visit` (moved from the view), `recent_for_patient` ("أدوية المريض" chips carry the
  patient's previous line), `favorites_for_doctor` (≥2 uses, latest dose), `last_usage` (picking a drug fills the doctor's
  own last dose/duration before the catalog default). Dose/frequency stay one "الجرعة" field (`instructions`) — splitting
  them would need a migration and every PDF/template/voice path changed.
- Diagnosis autocomplete = the doctor's own diagnoses, most used first (`diagnosis_suggestions`), free text kept.
- Tablet: `pointer-coarse:` variants enlarge chips/checkboxes; below `lg` the panes stack and the action bar gets
  "الكشف / الروشتة" jump links.
- Look (redesign 2026-10-06): shared classes in `static/src/app.css` — `ws-panel` (the two white panes + snapshot),
  `ws-title` (section title with a brand bar), `ws-label`, `ws-section` (hairline-separated block), `chip`
  (a Tailwind `@utility`, so other classes can `@apply` it) / `chip-soft`, `cat-tile` (drug categories as an equal
  grid of big tiles), `quiet-input` (prescription cells that look like text until hovered/focused). The voice mic sits
  **inside** fields: wrap an input in `.dictate-inline` or a textarea in `.dictate-corner`. Below `lg` the action bar's
  ("الكشف" | "الروشتة") are two full-width tabs at the top (`showPane()`, remembered per visit in sessionStorage; `/` and a blocked finish switch to the prescription); the drawer has its own tabs (`[data-drawer]`).
- Voice robustness (2026-10-15): `tests/test_voice_robustness.py` runs against the real 25k catalog (imported once per
  module, ~3 min): 400 everyday words + a dose must never become a drug (`everyday_words.py` is the vocabulary that can
  never start a name), real names found ≥93% also with ال/و prefixes and ر↔ل, and every dose/duration phrasing in
  `CASES`. Rules added: rough matches on short words (< `WEAK_MIN_CHARS`) are refused, prefix-stripped spellings score
  `GUESS_PENALTY` lower, dashes/slashes in names are indexed as spaces and glued, "4 و20" → 24, "لما يحتاج" → "عند
  اللزوم", a number after a unit is not a count when "مرات/ساعات" follows. Add a case there for every new misreading.
- Release checks (2026-10-15): `tests/test_crawl.py` seeds a full clinic (seed_org, drugs, seed_demo, simulate_day) and
  every role follows every href/hx-get it can reach (~400 pages for owner/doctor): no 500s, and reception/viewer/admin
  never get a 200 on `/desk/`. WhatsApp gateway client never loads TLS certificates for the local http:// gateway and
  treats `OSError` as "offline" (Avast's aswMonFltProxy blocked cert loading and crashed the home page). Grid cards
  holding long text need `min-w-0` (the dashboard's top-diagnoses card pushed the phone layout sideways).
- Voice box = confirmation (2026-10-24): the voice results show الجرعة/المدة as editable inputs (empty dose yellow, each
  with its own mic); "أضف"/"أضف الكل" send those values and **that press is the 13 §13.4 confirmation** — `dictate_add`
  now saves `needs_review=False` (lines added any other way are unaffected; `confirm_item` stays for old drafts). A number
  before "مرات/ساعات/ايام" is never the strength ("انتينال 5 مرات"), but before "مرة" it is ("كونكور 5 مرة الصبح").
- Form words & context (2026-10-24): a form word right next to a name (`TYPES`: قطره/شراب/كريم/مرهم/حقنه/لبوس/بخاخ/كيس…)
  adds `CONTEXT_BONUS` (so "قطره بلنك" finds BLINK), lets an exact 3-letter name count (`SHORT_NAME_CHARS`, "لوشن
  ماش"), and the said form ranks that product form first (`FORM_NAMES`/`FORM_PREF`, "شراب سيتال" → the syrup). New
  regimen kinds: `route` (في كل عين، على المكان، في العضل، تحت اللسان، شرجي…) and `time` (الصبح، بالليل…). A dose word said
  before the first name is kept in front ("حباية جلوكوفاج الصبح" → "حباية الصبح"). With no drug recognised the
  line-by-line fallback only shows matches ≥ `FALLBACK_MIN_SCORE` (90), else "مالقيتش اسم دوا". Measured on the full
  catalog: exact names next to a form word 99%, ر↔ل 98.5%, a dropped ي/ا ~90%, form word + everyday word 0/960.
  `test_voice_robustness.py` removes a leftover "voice-robustness" org first (the test DB is reused). When a form is said
  with an ingredient name and no match comes in that form ("كيس فوار باراسيتامول"), `_same_ingredient_in_form` adds
  products with the same `generic_name` in that form, ranked by how many said forms they fit (كيس+فوار → EFF. SACHETS).
- Mic stays on (2026-10-24): recording never stops on silence — only a second press (or another mic, or a real error).
  The browser ends a recognition session by itself after quiet; `dictation.js` starts a new one at once (`listen()`),
  keeping what was collected (after `RESTART_DELAY_MS`). Safety net is time-based: `MAX_QUIET_MS` (5 min) with nothing
  heard stops it; 8 sessions ending within a second each in 10 s → a visible "المايك وقف لوحده" message. (A restart
  *counter* used to run out within a second when Chrome refused sessions, silently cutting the doctor off.)
- الطلبات / Investigations (2026-10-24): `PrescriptionRequest` (prescription FK `requests`, kind lab/imaging/other, name,
  notes, order; simple-history; admin inline). Same lifecycle as the lines: `add_request` (same name twice → kept once,
  kind guessed by `investigations.kind_for`), `update_request`, `remove_request` need a draft; `revise` copies them,
  `repeat_last`/`repeat_from` bring them along. Suggestions: `investigations.LABS`/`IMAGING` (datalist) +
  `frequent_requests(doctor)` chips. Printed in `pdf/prescription.html` under the medicines (before the advice).
  URLs: `prescriptions:request_add` / `request_update` / `request_remove` (prescription.write).
- Requests by voice / split (2026-10-24): one box (typed or 🎤, `data-dictate-url` = `prescriptions:request_dictate`,
  `data-dictate-swap="outerHTML"` on `#rx-builder`) → `investigations.find_requests(text)` (Egyptian spoken names in
  `SPOKEN`, fuzzy, longest phrase first, و/ال stripped; "<اشعه|سونار|رنين|مقطعيه> على <part>" → "X-ray الركبة") → each
  added with its kind. Typed text with nothing recognised is added as written; speech with nothing recognised only shows
  a message. Screen and print show two groups: تحاليل (Lab tests) | أشعة (Imaging). `dictation.js` collect-and-post mode
  now applies to any field with `data-dictate-url` (was `data-dictate="rx"` only); the "analysing" banner stays rx-only.
- Requests from the prescription mic (2026-10-24): `dictate` first runs `investigations.split_requests(text)` — requests
  found are shown in the voice box ("الطلبات اللي اتقالت", added via `prescriptions:request_add_many`, included in
  "أضف الكل") and removed from the text before the medicine extraction. `AMBIGUOUS` names (Vitamin D, B12, iron,
  ferritin, calcium, prolactin) count as a test only after a cue word (`_CUES`: تحليل/اعمل/مستوى…) — "فيتامين د نقط" stays
  a medicine.
- Stop keeps the last words (2026-10-24): Chrome finalises only at a pause, so on the stop press `dictation.js` calls
  `stop()`, waits for `onend` (≤ `STOP_GRACE_MS`, mic amber meanwhile) and then flushes the still-interim text
  (`pending`) as a final. A session the browser closes by itself also flushes `pending` before restarting. Before this,
  talking straight through and pressing stop sent only what was finalised (often just the first drug).
- Split names & vowels (2026-10-24): two heard words that together match a one-word name ("اريستا بيكس" → ERASTAPEX)
  get `JOIN_BONUS` and win over a half matching something else (never gluing a form/unit/modifier word); a vowel-less
  comparison (`_novowel`: long vowels ا/و/ي dropped, not on words still carrying "ال") finds "اريستابكس" ≈ "اراستابيكس".
  The voice box shows the full heard text ("🗣 الكلام كله اللي اتسمع") open, so a cut-off recording is visible.

## Per-employee permissions (2026-10-25)
- `Membership.granted_permissions` / `revoked_permissions` (JSON lists). `permissions.effective_permissions(m)` =
  `with_implied((role's ∪ granted) − revoked)`; owner always has everything, inactive has nothing. `has_perm` and the
  template `can.*` both use it — **never check `membership.role` for access**, always a permission. `IMPLIES` = edit →
  view (clinical.edit → clinical.view, prescription.write → clinical.view + prescription.print, appointment.* →
  appointment.view, …); every role set is already closed under it (tested). `PERMISSION_GROUPS` holds the Arabic label +
  help of every permission (a new permission must be added there — tested).
- Screen: Users → "🔑 الصلاحيات" (`accounts:membership_permissions`, user.manage): grouped checkboxes, "من الدور" dot,
  زيادة/متشالة badges, JS mirrors IMPLIES, "رجّع صلاحيات الدور". Saving stores only the difference from the role; an
  editor can only change permissions they have; owner + yourself not editable; changing the role resets the lists;
  audited as `user.permissions`. Someone who isn't owner/admin but was given user.manage can only manage members whose
  permissions they all have, and only assign roles that don't exceed theirs (`_can_edit`, `_allowed_roles`).
- `require_perm` / `require_membership` wrappers carry `required_perm` (kept through `functools.wraps`).
  `tests/test_permission_matrix.py`: every URL needs its permission (403 without / through with only it); a full clinic
  is crawled as every role, "only <perm>" for every permission and adjusted roles — every URL in every page they reach
  (href, hx-*, form action, data-*, JS strings) must resolve to something they're allowed, and no followed link may 403.
  **A new button needs `{% if can.<perm> %}` matching its view's decorator, or this test fails.**
- Settings menu shows for settings.manage / schedule.manage / drug.manage and opens the first allowed tab; each tab is
  gated by its view's permission. Home's "today" numbers need appointment.view. The medical file's prescriptions list
  is open to clinical.view (print buttons only with print/reprint). The visit page without clinical.edit is read-only
  (no autosave URL, no finish/reuse forms).

## Clinic-defined roles (2026-10-25)
- Roles are the clinic's own: `organizations.ClinicRole` (org, name unique per org, description, `permissions` list,
  `key` = built-in role it started from or blank, order). `Membership.clinic_role` / `Invitation.clinic_role`; such
  members have `role="member"` (new `Role.MEMBER`). Only `Role.OWNER` stays fixed (everything, always). Display a
  role with `.role_name` (never `get_role_display`). `permissions.base_permissions(m)` = clinic role's perms (closed
  over IMPLIES), falling back to `ROLE_PERMISSIONS[m.role]` for a member still on a built-in role (tests create those
  with `make_member(org, "doctor")`; the screens convert them via `roles.ensure_defaults`).
- `apps/accounts/roles.py`: `DEFAULT_ROLES` (مدير النظام/دكتور/استقبال/مشاهد), `ensure_defaults(org)` (creates them only
  when the org has no roles + moves built-in-role members onto them; called by seed_org, create_user and the users/
  roles screens), `choices_for(editor, org)` (role-select values: `"owner"` for the owner only, else ClinicRole pk —
  only roles within the editor's permissions), `resolve`, `save_role` (drops members' now-redundant adjustments),
  `in_use` (delete refused while members or pending invites use it). Data migration `accounts/0004_clinic_roles_data`
  froze the defaults as of today and moved every existing member/invitation.
- Screens: المستخدمين | الأدوار tabs. `/accounts/roles/` (cards: permission count, members, تعديل/نسخة),
  `roles/new/[?from=<pk>]`, `roles/<pk>/`, `roles/<pk>/delete/` — all user.manage; the checkbox grid is the shared
  `accounts/partials/perm_groups.html`. A non-owner can't open/edit/delete a role holding permissions they don't have,
  and can only manage members whose permissions they all have (so the default مدير النظام can't manage doctors or
  reception unless the owner gives him those permissions). Audit: `role.created/updated/deleted`.
- Tests: `tests/test_roles.py`; `role_choice(org, key)` fixture gives the select value of a default role;
  `test_permission_matrix._member(..., clinic_perms=...)` puts a crawl profile on a custom role.

## Users without invitations (2026-10-25)
- Invitations are gone (model deleted in `accounts/0005_username_optional_email`, no invite links/e-mails). Users →
  "إضافة مستخدم" (`accounts:member_create`, user.manage): name, **username** (required, `USERNAME_RE` = letters incl.
  Arabic, digits, `. _ -`, never "@"), password ×2 (validators), optional email, role → they sign in right away.
  `accounts:member_edit` changes name/username/email and sets a new password (the only reset for users without an
  email; the e-mail reset page still works for those with one). Same `_can_edit` rules as the rest of the screen.
  Audit: `user.created`, `user.edited`.
- `User.USERNAME_FIELD = "username"`; email nullable + unique (stored lowercase, "" saved as NULL). Existing users got
  their email as username (it contains "@", so the edit form accepts an unchanged username that doesn't match the
  regex). `UserManager.create_user(username=None, email=None, password=None, ...)` uses the email as username when none
  is given (tests' `make_member(..., email=...)` rely on that). Login = `accounts.backends.UsernameOrEmailBackend`
  (username or email, case-insensitive; axes still first). `create_user --username [--email]` (one of them).

## Branded sign-in pages (2026-10-25)
- `organizations.branding.public_brand()` = the only active organization's settings + first doctor, or None when the
  install has 0 or 2+ clinics (a shared server never reveals its clinics). Context: `public_brand` (always a dict with
  org/settings/doctor, values None when unbranded — filter args and `{% with %}` fail on missing keys) and
  `brand_settings` (signed in: the org's settings, else the public brand's) — `base_page.html` takes colours + tab icon
  from `brand_settings`. `organizations:public_logo` (`/org/brand-logo/`, no login) serves the logo only in that
  single-clinic case — a deliberate exception to "logos are private media" (the clinic logo is public on its sign and
  prescriptions anyway). `base_auth.html`: the system's light background (`bg-sand`) with faint brand-coloured circles;
  logo, clinic name and doctor above a centred form card (brand top border) — same on every screen size.

## Offline voice (2026-10-26)
- Why: the browser recogniser (Web Speech) needs internet and doesn't work inside an exe / Electron / WebView2; Chrome's
  on-device mode has no Arabic. Researched + benchmarked (tools/asr_bench: synthetic ar-EG prescriptions through the real
  pipeline): lemura-arabic-asr (NeMo FastConformer-CTC, 115M, CC-BY-4.0) + beam 8 + the catalog's drug names as a
  boosting tree (α 1.3) = 75% drugs found at ~0.1× real time on a laptop CPU; Whisper large-v3-turbo 77% but 3.6× real
  time (too slow without a GPU); Whisper small/medium worse and slow; picking among n-best readings didn't help.
  Re-run the bench on real recordings before changing the model or the numbers (`tools/asr_bench/README.md`).
- `speech/server.py`: separate process + its own env (`%LOCALAPPDATA%\digiclinic\speech\.venv`, torch CPU + NeMo,
  ~2.5 GB; `speech\setup.bat` once, needs internet; model cached in `...\speech\models`). 127.0.0.1:3321, `X-Api-Key`
  (SPEECH_KEY, default WA_GATEWAY_KEY, read from .env like the gateway). `GET /health`, `POST /transcribe` (16-bit WAV ≤
  60 s, any rate → 16 kHz). Never logs words, only durations. Re-reads `boost.txt` when its mtime changes. No torch/NeMo
  at import time, so `tests/test_speech.py` runs its HTTP handler with a stand-in recogniser. run.bat starts it if the
  env exists (after `build_speech_boost`).
- Django: `apps/prescriptions/speech.py` (`status()`, `engine(rx_settings)`, `transcribe()`,
  `boost_phrases()`/`write_boost()` = every active drug alias's first word + first two words, normalised like the model
  writes: ة→ه, أإآ→ا, no marks). `PrescriptionSettings.voice_engine` auto/local/browser (Rx settings → "الإملاء الصوتي",
  shows the service's state). **auto = the browser's recogniser while online, the offline one without internet** (the
  clinic's PCs have no GPU, and on a real voice through a laptop mic lemura misheard most drug names where the browser's
  didn't — real-voice test 2026-10-26); "local" = audio never leaves the computer (privacy). `prescriptions:transcribe` (POST WAV; member with clinical.edit or prescription.write;
  503 JSON when the service is down). `status()`: 3 s timeout, "ready" cached 60 s, failures 5 s (a busy moment must not
  flip a page to the online recogniser). Scheduler job `speech_boost` every 6 h (file rewritten only on change). Test
  settings point SPEECH_DATA_DIR at tmp/ and SPEECH_URL at a closed port; mock with `settings.SPEECH_TRANSPORT`.
- `dictation.js` `LocalSpeechProvider`: getUserMedia → ScriptProcessor (2048-sample blocks) → energy VAD with
  hysteresis (start above max(0.01, 3× noise floor), keep going above half of it — the browser's noise suppression dips
  inside words and once cut a sentence in two, the second half then misheard) → cut after 800 ms quiet (1.5 s for the
  prescription mic: collect-and-post, nothing shown live, longer utterances = more context), max 15 s, < 0.3 s of *voiced*
  time is dropped (a cough + the quiet after it is not a sentence), 4-block pre-roll → 16 kHz 16-bit WAV → POST one
  utterance at a time (texts arrive in order) → `final`. Through Chrome's mic processing the model sometimes writes
  أ/ة/periods (it doesn't on clean files) — matching normalises them, nothing to do. `pickProvider()` reads `#voice-config` (desk_base: data-engine, data-local-ready):
  local → LocalSpeechProvider; auto → the browser's unless `navigator.onLine` is false / the browser's recogniser
  reported "network" (then local for the rest of the page — mid-recording the session just restarts on the local one);
  English always the browser's. Raw mic (echoCancellation/noiseSuppression/autoGainControl off: they muffled the voice,
  ~0.2% energy above 3 kHz) and the button stays amber until the mic really records (the first syllable was lost).
  `SPEECH_KEEP_SAMPLES=1` (env, testing only) keeps each utterance's WAV + text in `<speech data>/samples`. Status icon (🎙 / ⏳) via `onStatus`; `graceMs` 15 s lets the last utterance
  come back after the stop press. Browser tests drive it with synthetic frames (`_openMic` replaced).
- Extraction fixes found by the bench (help every engine): a dose/form word glued to the name is split off (`_unglue`,
  "بلينكنقطتين"), a name's last "ال" moved onto the next form/dose word is tried back ("انتين الكبسوله" → انتينال),
  dose nouns (اللزوم/قرصه/الكبسوله/حبايه...) can never start or sit inside a name (everyday_words), "كبسوله" is a form.
- Real-voice bench (2026-10-26, the owner's voice, laptop mic, 18 utterances, `%LOCALAPPDATA%\digiclinic\asr-bench\real2`):
  drugs found — lemura 20%, + whole-catalog boost 40% (many wrong extras), **+ a 38-drug "doctor's list" boost α1.5 50%**
  (0.08× real time); Whisper small 35% / + prompt with the doctor's drugs 50% (1.4×); medium 55% / + prompt 75% (4.4×);
  turbo 65% (4–6×); Arabic-dialect Whisper fine-tunes (oddadmix small/medium, whisper-small-eg) no better. No GPU in the
  clinics → the offline engine stays lemura, boosted with the clinic's own drugs (`speech.DOCTOR_LIST_MIN/MAX`, by
  `Drug.usage_count` + clinic-added drugs; the catalog until 30 drugs were prescribed; the service picks α 1.5 for a
  list ≤ 3000 phrases, 1.3 for the catalog). Online, the browser's recogniser is still clearly better → "auto".
  A headset / USB mic should help any engine (the laptop mic gave ~0.3% energy above 3 kHz).

## Reception board (2026-10-28)
- One screen for reception: `reception:today` (`/reception/`, **appointment.view**) = today's reception work + the day
  and week schedule + new bookings. `?view=day|week&date=` (the view is remembered in the session, the date resets to
  today; `_session_state` lets actions re-render the board the user is on). Old screens redirect there:
  `appointments_day`/`appointments_week` → `?view=…&date=`, `booking_home` → `?book=1`, `booking_patient` → `?book=<pk>`
  (the board opens the booking panel on load). Sidebar: one item "الاستقبال والمواعيد".
- `apps/reception/board.py` (read-only): `day_board` = every slot of the doctor's periods (`Slot`: entries / covered by
  a longer booking / outside the periods / bookable / past) or, in queue mode, each period's numbers; `week_board` =
  hour rows × Saturday→Friday cells (open = inside a period, bookable = open + after min notice + within the horizon).
  Templates: `reception/partials/board.html` (polled, `#rows-sig` includes view+date) → `board_day.html` /
  `board_week.html`; one booking row = `board_row.html` + `appt_actions.html` (today: وصل/دخّل/خلص/لم يحضر; any day: ⋯
  payment, vitals, back a step, confirm, reschedule, cancel, messages — each behind its permission). Past free slots are
  hidden; "إخفاء المواعيد الفاضية" toggle; on phones الكشف/الحساب columns hide.
- Booking panel (`scheduling:booking_panel`, appointment.book) in `#modal`: patient search (`booking_search` partial
  buttons swap the panel) / add new (`booking_new_patient` with HTMX → the panel for that patient, errors in place) →
  visit type chips, day, free times (AM/PM radio grid; `at` from a free day-cell or `hour` from a week-cell pre-picks)
  → `booking_confirm` (HTMX: empty body + `HX-Trigger` toast + `board-refresh`; errors re-render the panel) or, today,
  "سجّل حضوره دلوقتي" → `reception:walk_in` (HTMX too). The board listens for `board-refresh` (hx-trigger from:body).
- Times on reception screens: `{% load clock %}{{ dt|ampm }}` → "2:30 PM" (Latin AM/PM, Cairo time); patient messages
  keep `fmt_time` (ص/م). Week drag-and-drop to reschedule now lives on the board (delegated listeners on `#reception`).

## Design system (Phase 11, 2026-11-15) — supersedes the older "Look" notes above
- Plan: `docs/plan/14-obgyn-center.md` (the OB/GYN center expansion, phases 11-20). §14.1 is this design system.
- Concept "the chart and the ledger": paper surfaces, hairline rules, squared tags, tabular digits, one accent. Type: IBM Plex
  Sans Arabic for Arabic + Latin + digits (`static/fonts/plex-*.woff2`, family name `Plex`; `font-mono` = the same family
  with tabular digits). PDFs keep Cairo/Poppins (calibrated layouts, untouched).
- Colours (`static/src/app.css` `@theme`): the clinic's three settings come in through `base_page.html` as
  `--color-brand` (primary), `--color-ink-panel` (**dark colour = the side panel / dialog heads**, clamped dark enough for
  white text) and `--color-sand` (neutral, held to a pale page tint). Everything else is derived with `color-mix`:
  `brand-dark`, `brand-soft`, `ink`, and the whole **`stone-*` scale (cool greys tinted by the brand; 400/500 darker than
  Tailwind's for contrast)**. Radii are remapped small (`rounded-lg` 6px, `xl` 8px), `shadow-sm` is nothing; shadows only
  on things that float. Defaults: pomegranate `#A21E48` on aubergine `#24131E` (existing clinics keep their colours).
- Components (use these, don't restyle by hand): `btn-primary/secondary/ghost/danger`, `btn-icon`, `card`, `badge`
  (squared tag, never a pill), `link`, `page-head` (+ `h1`, `page-sub`), `section-title`, `stat-strip`/`stat`
  (figures in one ruled row, not a grid of cards), `data-table`, `menu`/`menu-item`, `overlay` + `dialog` (modals),
  `toast`, `notice notice-danger|warn|info`, `segmented` (`.is-active`), `tabs`/`tab` (`.is-active`), `input`,
  `pane-tab` (`aria-selected`), plus the workspace classes (`ws-*`, `chip`, `cat-tile`, `rx-*`).
- Icons: `{% icon "name" "size-4 extra-classes" label="" %}` (a template builtin, no `{% load %}`) inlines the vendored
  Phosphor SVG from `static/icons/` (MIT). To add one, copy `assets/regular/<name>.svg` from `@phosphor-icons/core`.
  **No emoji or symbol characters as icons, and no em dashes in interface copy** (templates or Python strings):
  `tests/test_design_system.py` checks every screen template and that every icon asked for exists. RTL: "back" is
  `arrow-right`/`caret-right`, "next" is `caret-left`.
- `static/css/app.css` is committed: rebuild after touching `static/src/app.css` or adding classes
  (`.venv/Scripts/tailwindcss -i static/src/app.css -o static/css/app.css --minify`, `TAILWINDCSS_VERSION=v4.3.3`).
- Shell (`base.html`): the screens are defined once in **`apps/core/navigation.py` (`ITEMS`)**; a new screen is added
  there (label, icon, url, permissions, which view names light it), never in the template. The context processor gives
  `nav_groups` (side panel, `partials/side_panel.html`) or `work`. Top bar blocks: `topbar_search` (the desk swaps in
  "open a patient's file") and `topbar_actions`. The top bar is not sticky; `--side-w` is the side panel's width for
  fixed bars (`lg:start-(--side-w)` on the visit action bar).
- **Work screen (asked for by the owner 2026-11-11)**: `ClinicRole.start_screen` (a key of `ITEMS` with
  `work_screen=True`: reception / desk / cashbox; blank = the whole system). People on that role sign in straight onto
  that screen with **no side panel** (it fills the window like its own program); whatever else they may open shows as
  tools in the top bar, with a "back to my screen" button on tool pages; `/` redirects to the screen. Set in the role
  editor ("شاشة العمل"); a screen the role's permissions don't open is cleared on save. The default reception role
  gets `reception` (migration `organizations/0005` for existing clinics). **Layout only: access is still permissions**
  (`navigation.work_screen(membership)`; the owner and members not on a clinic role always get the whole system).
- Sign-in (`base_auth.html`): one small box in the middle of the window (dark head with logo + clinic name, form under
  it), asking for **username + password only**. Staff are created without e-mails: the e-mail field is off the
  "add user" box (still on the edit page: the owner's e-mail receives health alerts) and the login no longer links to
  the e-mail reset (the pages still exist). An e-mail still signs in for accounts that have one.
- Logo content type comes from `organizations.branding.logo_content_type()` (by extension), never `mimetypes`: on
  Windows that reads the registry and another program had remapped `.png`, so the logo didn't draw.
- Tests on a new machine need Chromium once: `.venv/Scripts/python -m playwright install chromium` (PDF + dictation tests).
- Local preview gotcha: `.claude/launch.json` runs `runserver --noreload`, and Django caches templates in that mode:
  restart the preview server to see a template edit.

## Obstetrics notes (Phase 12, 2026-11-16)
- `apps/obstetrics` = the OB/GYN layer (plan 14 §14.2). On only for clinics with **`OrganizationSettings.specialty =
  "obgyn"`** (Settings → العيادة; `services.enabled(org)`, template flag `obgyn`) and only for female patients
  (`services.applies_to`). With the layer off its URLs answer 404 and nothing of it renders.
- `calc.py` is pure date arithmetic. **The EDD is the one stored anchor** (`Pregnancy.edd`); the three dating methods
  (LMP with cycle length, ultrasound, IVF transfer) only differ in how it is found (`services.Dating.edd()`), and
  **gestational age is never stored**: `pregnancy.ga(on)` → `calc.GestationalAge` (weeks/days, trimester, the month
  "the way patients count it" = completed calendar months + 1 capped at 9, days left, term status, the Arabic labels).
  Dates only, Cairo calendar day (`services.today()`). `AntenatalVisit.ga_days` is the one exception: the age written
  on that visit's day, kept so old visits read right after a re-dating.
- Models: `ObstetricProfile` (blood group, cycle, contraception; one per patient), `Pregnancy` (one `active` per
  patient, **partial unique constraint**), `PregnancyHistory` (typed rows + the row written when a followed pregnancy
  is closed; wrong rows get `is_removed`, never deleted), `AntenatalVisit` (OneToOne on `clinical.Visit`, created on
  the first field written), `Milestone` (the clinic's schedule, seeded by `ensure_milestones`) + `MilestoneCheck`.
  **G/P/A/L are counted, never typed** (`services.gpal`): G = history rows + the followed pregnancy, P = births
  (vaginal / cesarean / stillbirth), A = the rest, L = sum of `living_children`.
- `services.py` is the only way in: `start_pregnancy`, `set_dating`, `update_details`, `close_pregnancy` (writes the
  history row; `DELIVERED` for births, `LOST` otherwise), `transfer_pregnancy`, `reopen_pregnancy` (takes the auto row
  out of the count), `add_history` / `remove_history`, `risk_flags` (only from recorded facts), `milestones_for`
  (done / due / overdue / upcoming), `toggle_milestone`, `save_antenatal_field` (autosave whitelist, like
  `clinical.save_field`), and for lists **`badges_for(org, patient_ids)` = one query** / `badge_for(patient)`.
- Where the week shows: doctor's sticky header (`header_tag.html`), the band under it (`band.html`: week, month,
  trimester, EDD, G/P/A/L, risk flags, what is due), the antenatal sheet inside the visit form (`antenatal.html`), the
  pregnancy file (`panel.html`: drawer on the visit page, tab on the medical file); and for reception the small
  `ga_badge.html` tag on the board (as of each booking's day, attached in `reception.board.appointments`), the
  patients list, the patient page and the booking panel. `apps/obstetrics/context.py` holds what clinical pages need
  (`overview`, `antenatal_ctx`) so `clinical.views` can import it without a circle.
- Every change in the panel answers with the fresh panel + **`HX-Trigger: ob-changed`**; the header tag, the band and
  the antenatal sheet carry `hx-trigger="ob-changed from:body"` and re-read themselves, so a re-dating moves every
  open screen. The band's button calls `window.openObPanel()` (defined by `visit.html` and `record.html`).
- Permissions: the file itself is `clinical.view` / `clinical.edit` (reception gets 403 on every obstetrics URL, tested).
  New **`pregnancy.view_basic`** (reception / doctor / admin / owner; implied by `clinical.view`) only shows the tag
  and the expected date, never the history, blood group or risk. Migration `organizations/0006` adds it to existing
  clinic roles that started as reception / doctor / admin.
- Demo: `clinic.json` is now an OB/GYN clinic; `manage.py seed_pregnancies [--org]` gives the `seed_demo` women 8
  pregnancies from 7 to 40 weeks with history and blood groups (run once; the crawl tests use it).
- The four items left out of this phase (pregnancy messages, the milestone settings screen, ultrasound biometry,
  the pregnancy file on the export PDF) were built in phase 20: see "Hardening notes".

## Center mode notes (Phase 13, 2026-11-16)
- Plan 14 §14.3: several doctors in the interface, the services catalog, emergency arrivals. New app `apps/center`
  (`CenterSettings`, `Service`, `ServiceOrder`, `EmergencyCase`; all with history, nothing hard-deleted).
- **Which doctor** (`apps/doctors/services.py`): `current_doctor(request)` = `doctor` in the request (GET or POST,
  remembered in the session) → the one picked last → the doctor record linked to the signed-in user
  (`Doctor.user`, unique per clinic, set in Settings) → the first active doctor. `board_doctor(request)` is the
  reception board's choice and may be None = every doctor. `get_doctor(org)` is now only "the first active doctor"
  for commands and seeds: **views never call it**. `active_doctors(org)` never returns an empty list.
  Wherever a visit type is posted (booking, walk-in, free slots) **the visit type decides the doctor**
  (`scheduling.views._visit_type`): a stale session can never book for the wrong doctor.
- Settings: "الدكاترة" tab (`doctors:list`, add with a copy of the first doctor's visit types, `doctors:toggle`).
  A doctor is switched off, never deleted; refused while they have bookings ahead or are the last active one
  (`services.set_active`). The schedule / visit-type / drug-category pages show `doctors/partials/doctor_switcher.html`
  and every form posts to `?doctor=<pk>`; delete views take the doctor from the row itself. A visit type that was
  booked is switched off instead of deleted.
- Reception board: `_board_ctx` builds **one board per doctor shown** (`boards`; `board` = the first, kept for old
  callers). With several doctors the toolbar has الكل / each doctor (`?doctor=all|<pk>`, session `board_doctor`); in
  "all" the free slots start hidden. Booking links carry `&doctor=`. `close_day` takes `doctor` (`all` = the whole
  center); the affected-bookings screen takes `?doctor=`.
- Services: catalog in Settings → "الخدمات والطوارئ" (`center:services`, settings.manage). `center.services.
  order_service / cancel_order`; an order copies name, price and doctor share when made. **`billing.balance(appt)` =
  visit price less discount plus active orders** (`Balance.services` is that part); pass prefetched `orders` to avoid
  a query per row. Cancelling an order that was already paid for is refused until the money is refunded. The same box
  (`center/partials/orders.html`, swaps `#orders-box`) is a dialog on the board ("خدمات على الزيارة") and inline in the
  doctor's visit form. `Service.revenue_account_key` is reserved for the ledger (phase 14).
- Emergency: `scheduling.services.emergency_arrival` creates an appointment for right now, already `arrived`,
  `source=emergency`, no queue number, no period, no messages, under the day's ledger lock. `center.services.
  register_emergency` adds the `EmergencyCase` (triage, reason, arrived / seen / disposition times) and prices the
  visit with `CenterSettings.emergency_fee`. Its visit type is a hidden per-doctor one (`VisitType.is_system`).
  `URGENT_FIRST` (scheduling.models) orders emergencies ahead in the doctor's queue and the queue-mode board.
  `mark_seen` is called by `clinical.start_visit` and by reception's "دخّل" (written once). "رجوع خطوة" is refused for
  an arrived emergency (it would become a booking the no-show job chases): use `cancel_emergency` (only before she is
  seen). One open case per patient.
- Where an emergency shows: the red button on the board (`center:emergency_register`: name + phone are enough, typing
  looks existing patients up), the strip on top of every board (`center/partials/emergency_strip.html`, all doctors,
  part of the poll), a red tag on her row, a fixed red banner on every desk page (`clinical:emergency_alert`, polled
  every 10 s, for the desk's doctor), the band under the visit header with the disposition dialog, and the log
  `center:emergencies` (side menu "الطوارئ").
- Permissions: `emergency.register`, `service.order` (reception / doctor / owner; both bring `appointment.view`).
  Migration `center/0002` adds them to existing clinic roles that started as reception or doctor. The reason of an
  emergency is shown only with emergency.register or clinical.view.
- HTMX gotcha met again: a button inside an element that re-reads itself with `hx-swap="outerHTML"` inherits it; the
  disposition button replaced the `#modal` container until it got its own `hx-swap="innerHTML"` (pinned by a test).
  Also: `ms-auto` on an element with `dir="ltr"` pushes the wrong way in an RTL row; put `dir` on an inner span.
- Seeds: `clinic.json` has `extra_doctors`, `center.emergency_fee` and `services`; `seed_services [--org] [--force]`
  (run by run.bat, idempotent). `simulate_day` adds a service and one emergency, refuses to simulate a day twice, and
  `--remove` keeps (and un-marks) simulated visits that became medical records instead of failing.
- Local preview: a stale `runserver --noreload` on 8010 from another session serves old code. `.claude/launch.json`
  has a second configuration `digiclinic-check` on port 8012 for that case.
- Not in this phase: revenue by doctor / service reports (phase 19), the doctor-share payout (phase 16), linking the
  dispositions "دخول عمليات" / "حجز غرفة" to an operation or an admission (phase 15).

## Accounting core notes (Phase 14, 2026-11-16)
- Plan 14 §14.5. Two apps: `apps/ledger` (chart of accounts, fiscal periods, the journal, reports) and
  `apps/treasury` (treasuries, vouchers, cashier shifts, cash counts). **The rule above all others: only
  `ledger.services.post()` creates journal entries.** Every money-moving document has a small `posting.py` next to
  it (`billing/posting.py`, `treasury/posting.py`) that builds the lines and calls `post(org, date=, memo=, lines=,
  source=<the document>, event="")`; with a source it is idempotent (one entry per document per event).
- `post()` checks: two lines or more, each line exactly one of debit / credit above zero, debits = credits, active
  leaf accounts of this clinic, **a cost center on every revenue / expense line**, an open period. It then takes the
  next number of the fiscal year (`EntrySequence`, `select_for_update`, taken last so a refused entry burns none)
  and runs the database's deferred check at once (`SET CONSTRAINTS ... IMMEDIATE`). Errors are `LedgerError` /
  `PeriodClosed` with an Arabic message for the screen. `Line.account` and `cost_center` take a model or a system key
  (`"cash_main"`, `"admin"`); helpers `debit(...)`, `credit(...)`. `party=` (patient / supplier / doctor / user)
  writes `party_type` + `party_id`: that is the subledger of a control account.
- **The database enforces it too** (`ledger/migrations/0002_integrity.py`, PostgreSQL triggers, SQLSTATE 23514 →
  `IntegrityError`): a posted entry balances and has two lines or more (deferred constraint triggers, checked at
  COMMIT); posted entries and their lines cannot be updated, deleted or added to; an entry becomes posted only with a
  number, in an open period of its clinic that contains its date; a line's entry, account and cost center are one
  clinic's, the account an active leaf; an account with lines cannot become a group or change type. Consequence:
  **nothing that carries posted entries can be deleted** (a clinic, a user who posted). Corrections are
  `services.reverse(entry)` (one reversal per entry; a reversal is not reversed).
- Money: `ledger/money.py`. `money(x)` = Decimal, two places, ROUND_HALF_UP, **floats refused**; `allocate(total,
  weights)` = largest-remainder split whose parts always sum to the total (use it for every discount / tax / share
  split in later phases); `percent_of`.
- Chart: `ledger/chart.py` (`CHART`, a standard Egyptian five-class tree; `ensure(org)` completes it and never
  overwrites; `account(org, key)` / `cost_center(org, key)` find the program's accounts and create the tree on first
  use). Codes are hierarchical (a child's code starts with its parent's), so ordering by code is the tree. Editing
  rules in `ledger/accounts.py` (add under a group, rename, move a leaf, switch off with a zero balance, delete only
  if never posted to; system and treasury accounts stay).
- Periods: `fiscal_year_for(org, day)` creates the year and its 12 months on demand
  (`LedgerSettings.fiscal_year_start_month`, default January). `close_period` (in order) / `reopen_period` (last
  closed first), audited. Year close is phase 19.
- Reports (`ledger/reports.py`, posted lines only): `trial_balance` (opening / movement / closing, groups roll up,
  `.balanced`), `account_statement` (running balance, signed the account's normal way), `journal`. Every report takes
  `?format=xlsx|pdf` through `ledger/export.py` (`Table` → openpyxl with numeric cells, or `pdf/report.html`).
  Template filter `{% load money %}{{ x|money }}` (blank for zero) / `|money0`.
- Treasury: a `Treasury` is tied to one leaf account; **its balance is never stored, it is that account's posted
  balance** (`treasury.services.balance`). Defaults per clinic (`ensure`): reception drawer (receives cash), main
  safe, bank, card / InstaPay / wallet collection accounts (`receives_method` says which payment method lands
  where). Vouchers: receipt / payment / transfer, numbered per kind per fiscal year, posted on save, cancelled by a
  reversal (`event="cancel"`). **Cash treasuries can never go below zero** (banks may).
- Payments: `billing.record_payment` now sets `Payment.treasury`, opens the drawer's shift if none
  (`ensure_shift`), and posts **Dr treasury / Cr patients receivable (party = the patient)** in the same transaction
  (refund = the reverse; a cash refund needs the cash to be in the drawer). Revenue is not touched here: invoices
  (phase 16) debit the same patient account. A payment into a closed month is refused.
- Shifts: closing (`billing:shift_close`, payment.record, from the cashbox screen) is a **blind count** by
  denomination; the counted cash less the float left in the drawer goes to the main safe by a transfer voucher; a
  difference stays on the drawer's account as `CashCount.variance_status=pending` until `approve_variance` posts it
  to "عجز وزيادة الخزينة". Separation of duties: the counter cannot approve their own difference unless they are the
  owner (`can_approve`). A surprise count (`treasury:surprise_count`, treasury.count) only records.
  **`treasury.cash_on_hand(t)` = the books' balance plus the differences still waiting for approval: use it (never
  `balance`) wherever the question is "how much cash is really there"** (the expected amount of a count, whether a
  refund or a payment voucher can be covered, the cashbox). Otherwise a pending shortage is counted again next shift.
- Existing data: `manage.py ledger_bootstrap` (run by run.bat and the deploy steps after `migrate`; idempotent)
  builds the chart and treasuries and posts every payment that has no entry, oldest first. This replaces the "data
  migration" of the plan: service code inside a migration would break on any later schema change.
- Screens: "الدفاتر" (`ledger:*` under `/books/`: ميزان المراجعة, دفتر اليومية, قيد جديد, شجرة الحسابات, كشف حساب,
  الشهور) and "الخزن والسندات" (`treasury:*`). Manual entries cannot touch the control accounts in
  `ledger.views.CONTROL_KEYS` (they move only through their documents), and an entry that came from a document is
  reversed by cancelling the document, not from the journal.
- Permissions (`FINANCE` set, owner only by default; accountant / cashier roles come in phase 20): `ledger.view`,
  `ledger.post`, `ledger.close_period`, `accounts.manage`, `treasury.view`, `treasury.operate`, `treasury.count`.
- Tests: `tests/test_ledger.py` (exact entries, every rule, raw-SQL attempts against the triggers, a commit that
  must fail, threaded numbering, a hypothesis test over random entries) and `tests/test_treasury.py` (payments,
  vouchers, shifts, bootstrap, screens, exports, a hypothesis test over random documents that checks the invariants
  after every step). `hypothesis` is a dev dependency. **Never run two pytest processes at once: they share the test
  database.**
- Gotchas (phase 14): `select_for_update()` with `select_related` on a nullable FK fails in PostgreSQL ("nullable side of an
  outer join"): use `select_for_update(of=("self",))`. `simulate_day --remove` now refunds and cancels paid demo
  bookings instead of deleting them (payments are never deleted).

## Theatre notes (Phase 15, 2026-11-16)
- Plan 14 §14.4. New app `apps/theatre`: `Room` (operating / delivery / recovery / inpatient; `beds`, `nightly_rate`),
  `ProcedureType` (duration, package price, `delivery` = vaginal / cesarean / blank, fasting hours, `checklist` and
  `default_team` as JSON lists), `RoomDay` (the lock row), `Operation` (+ `OperationEvent`), `OperationTeamMember`,
  `ChecklistItem`, `OperationNote`, `Newborn`, `OperationConsumable`, `Admission`. All with history, nothing deleted.
- `services.py` is the only way in. **A room is never double-booked**: `book_operation` / `move_operation` lock the
  `RoomDay` rows the operation touches (`select_for_update`, created on demand, an operation over midnight locks both
  days) and then look for an overlapping operation still holding the room (`HOLDS_ROOM` = scheduled / admitted /
  in theatre; recovery and done have left it). The same patient cannot have two overlapping operations either.
  A booking copies the procedure's name, price, fasting hours, checklist and usual team onto the operation, so
  changing the catalog later never rewrites an old operation.
- State machine `ALLOWED_TRANSITIONS` (models.py): scheduled → admitted → in_theatre → recovery → done, each one step
  back, `cancelled` / `postponed` from the first two. `transition()` stamps the time of each step and clears it on
  the way back. **Into the theatre the required checklist items must be done, unless `is_emergency`**: then she goes
  in and what was skipped is written on the `OperationEvent`. A postponed operation stays as it was;
  `rebook_postponed` books a new one and links it (`postponed_to`). `waiting_for_a_date(org)` = postponed, not rebooked.
- Deliveries: `Newborn` rows (sex, weight, Apgar 1 / 5, time, status; a wrong one gets `is_removed`). Finishing a
  delivery operation calls `close_delivery`: it needs one newborn at least, and closes the followed pregnancy through
  `obstetrics.close_pregnancy` (cesarean / vaginal from the procedure, stillbirth when no baby is alive, living
  children counted), so **G/P/A/L move by themselves**. `Operation.delivery_closed` says it was written; after that
  newborns are fixed. A delivery with no followed pregnancy just keeps its newborns.
- Operation note: one draft at a time (`draft_note` / `save_note`), `sign_note` makes it immutable, a change after
  that is a new revision (`revision` + 1, the signed one stays). Only `clinical.view` sees it, `theatre.operate` writes.
- `_editable(op)` (checklist, team, consumables) **re-reads the status from the database**: an operation object held
  in memory by a view can be stale. Same for `remove_newborn`. Keep that habit in any new theatre service.
- Admissions: `admit` (a bed must be free: `beds_free(room)`, one open stay per patient by a partial unique
  constraint, the room's rate is copied), `discharge` (summary + instructions, PDF `theatre:discharge_pdf`,
  `templates/pdf/discharge.html`), `cancel_admission`. `Admission.nights()` = midnights between in and out in Cairo,
  one at least; `room_charge()` = nights × the copied rate. Nothing is posted to the books here: the invoice of
  phase 16 reads `Operation.price`, the team fees and `room_charge()`.
- Messages (`theatre/notifications.py`, all through the outbox, keys `op:<pk>:<event>:<start stamp>`): booked (at
  once), reminder with the fasting hours 24 h before (`REMIND_BEFORE`), changed (on a move: the old reminder is
  withdrawn and a new one queued); cancel / postpone withdraws what is pending. These rows have no appointment:
  `notifications.services.enqueue_for_patient(org, patient, event, send_at, context, key, by)`, rendered by the
  dispatcher from `templating.patient_ctx`. New events `operation_booked / operation_reminder / operation_changed`
  (`seed_notifications` adds missing default templates to an existing clinic without touching edited ones).
- Screens: `theatre:board` (`/theatre/`, `?view=day|week&date=`: one column per theatre room for the day or one per day of
  the week, the one next step as the button,
  "مقيمين دلوقتي", postponed operations waiting for a date), `theatre:operation` (header + tabs: قبل العملية / الفريق /
  تقرير العملية / المولود / المستلزمات / السجل; tabs are Alpine, HTMX partials swap inside), `theatre:operation_new`,
  `operation_move`, `theatre:admissions` (a card per room with its beds), `admit`, `discharge`, and Settings →
  "العمليات والغرف" (`theatre:setup`, settings.manage: rooms and procedure types, switched off, never deleted).
  Side menu item "العمليات والإقامة" (also a work screen for a role). Links in: the emergency band and log
  (dispositions "دخول عمليات" / "حجز غرفة" open the booking / admission with the patient and the case filled in), the
  visit action bar ("حجز عملية"), the patient page (her operations).
- Permissions: `theatre.view` (board, operation page without the note), `theatre.schedule` (book / move / cancel /
  postpone), `theatre.operate` (steps, checklist, team, consumables, note, newborns), `admission.manage`. Reception:
  view + schedule + admission. Doctor: all four. Migration `theatre/0002` adds them to existing clinic roles.
- `ensure_defaults(org)` seeds 5 rooms and 7 procedure types the first time the screens open (never again once the
  clinic has any). `simulate_day` books one operation and one admission.
- Not in this phase: the money side (phase 16), consumables taken from stock with their cost (phase 18: today a
  consumable is a name + quantity).

## Invoicing notes (Phase 16, 2026-11-16)
- Plan 14 §14.6. New app `apps/invoicing`: `Invoice` (+ `InvoiceLine`, `InvoiceShare`, `InvoiceSequence`), `Charge`,
  `Payer`, `PatientCoverage`, `InvoiceSettings`. An invoice is numbered per fiscal year (`INV-2026-00001`, credit
  notes `CN-...`, no gaps), never edited, and posted in the transaction that makes it (`invoicing/posting.py`).
  CHECK constraints keep `gross - discount = total = payer_share + patient_share` on invoices and lines.
- **Two kinds of invoice.** (1) A visit's invoice is made by the program: **`services.sync_appointment(appt)`**
  compares what the visit should bill now (`appointment_specs`: the fee and the active service orders, once the
  patient is in the center and either paid something or the visit is finished) with the live lines already invoiced,
  takes changed lines back by a credit note and bills what is missing. It is idempotent; call it after anything that
  changes what a visit is worth. It is already called (through `sync_quietly`) by `billing.record_payment`,
  `scheduling.transition` / `cancel`, `center.order_service` / `cancel_order` and `set_visit_discount`.
  (2) An **account invoice** is issued by a person (`issue_account_invoice(patient, keys=...)`) for what
  `open_items(patient)` lists: operations in recovery / done, discharged stays, extra `Charge` rows.
- `sync_quietly` never lets an accounting refusal stop a visit: it logs and leaves the visit for the next sync (the
  scheduler job `invoice_sync` hourly → `sync_recent`). **Under tests `INVOICING_STRICT=True` raises instead**, so a
  bug in the sync fails the test that caused it.
- A line names what it bills (`appointment` = the visit fee, `service_order`, `operation`, `admission`, `charge`).
  **Nothing is billed twice**: partial unique constraints allow one live line per source. A credit note takes whole
  lines back (`reverses`, the original gets `is_live=False`), then the source can be billed again, corrected.
  Visit invoices are corrected only through the visit (`credit_invoice` refuses them).
- Entries: the invoice (Dr patients receivable [patient] + Dr payers receivable [payer] + Dr discounts allowed per
  cost center / Cr revenue per account and cost center, at the full price), `event="shares"` (Dr doctor fees expense /
  Cr doctor fees payable [doctor], Cr `external_fees_payable` for outside team members), `event="deposit"` (Dr patient
  deposits / Cr patients receivable). A credit note is the mirror. Cost centers: outpatient / emergency / theatre /
  inpatient by what the line is.
- Shares (accrued when invoiced, `InvoiceShare`): `Doctor.visit_share_percent` of the visit fee after discount
  (Settings → the doctor's page; 0 = none), the service order's copied percent, and for an operation each team
  member's fee (the team is paid out of the package price, it is not an extra line for the patient).
- Payments: `billing.Payment` now has `patient` (always), `appointment` (nullable) and `kind` (payment / deposit).
  `billing.record_account_payment(patient, amount=, kind=, is_refund=)` is money on the account: a payment cannot be
  more than she owes (more = a deposit), a refund not more than her credit, a deposit refund not more than the deposit
  held. A deposit sits in "دفعات مقدمة من المرضى" until `issue_account_invoice` applies it. A reschedule moves the
  visit's payments to the new appointment.
- Balances are the books' own: `patient_balance`, `deposit_balance`, `doctor_balance`, `payer_balance`
  (`ledger.balance_of(account, party=...)`), lists use `party_balances(org, key, kind)` (one query).
  `account_state(patient)` says which invoice her money paid (a visit's payments pay that visit first, an applied
  deposit its own invoice, the rest oldest first): display only, nothing stored.
- Payers: `PatientCoverage` (payer + percent) applies to **every** invoice issued while it is on; the payer's part is
  split over the lines with `allocate`. **`billing.balance(appt, ..., coverage=)` now returns the patient's own part**
  (`Balance.covered` is the payer's); lists pass `billing.coverage_map(patient_ids)` to avoid a query per row.
- Discounts: on a visit (`set_visit_discount`, reception → ⋯ → "خصم على الكشف", stores `Appointment.discount_reason`)
  or on an account invoice (spread over the lines by price). `invoice.discount` up to
  `InvoiceSettings.discount_limit_percent` (10), `invoice.discount_any` above it; a reason is required.
- Vouchers carry a party now (`Voucher.party_type/party_id`, on the journal line too). **Control accounts
  (`ledger.chart.CONTROL_KEYS`) move only through their documents**: a free voucher on one is refused and the voucher
  form does not offer them. `pay_doctor` / `receive_from_payer` are vouchers with the party, never above the balance.
- Screens (`/invoices/`, side menu "الفواتير"): الفواتير (list, Excel / print), حسابات المرضى, the patient's account
  (`invoicing:account`: what waits, issue, take money, extra charge, coverage), the invoice page (print, credit note),
  مستحقات الدكاترة (statement + pay), جهات التعاقد (statement + receive), Settings → الفواتير. PDFs:
  `pdf/invoice.html`, `pdf/receipt.html`. Links in from the patient page, the operation page and the board's ⋯ menu.
- Permissions: `invoice.view`, `invoice.issue` (reception), `invoice.discount` (doctor), and owner-only by default
  `invoice.discount_any`, `invoice.refund` (credit notes), `doctor.payout`, `payer.manage`. Migration
  `invoicing/0002` gives the first ones to existing reception / doctor roles.
- Existing data: `ledger_bootstrap` now also runs `invoice_visits(org)`: visits from before this phase get their
  invoice dated the day of the visit (today when that month is closed).
- Tests: `tests/test_invoicing.py` (exact entries of every document, the DB constraints, and a hypothesis test over
  random visit events that checks after every step that a fresh sync has nothing to do and that each control account
  equals its documents). Copy it for phase 17's documents.
- Assumptions written down (standard practice, all changeable): revenue is recognised when the patient is in the
  center and paid, or when the visit is finished; shares are accrued at invoice, not at collection; the operation's
  package price includes the team's fees; medical services carry no VAT (no tax lines on patient invoices).
- Not in this phase: priced consumables from stock on the invoice (phase 18), aging and revenue reports (phase 19).

## Purchasing notes (Phase 17, 2026-11-16)
- Plan 14 §14.7. New app `apps/purchasing` (`Supplier`, `TaxCode`, `PurchaseInvoice` + `PurchaseLine`,
  `PurchaseReturn` + lines, `SupplierPayment` + `PaymentAllocation`, `PurchaseSettings`); cheques and recurring
  expenses live in `apps/treasury` (`Cheque`, `ChequeEvent`, `ExpenseTemplate`, `treasury/cheques.py`). Every
  document is numbered per fiscal year (`PINV-`, `PRET-`, `SPAY-`), posted in the transaction that makes it
  (`purchasing/posting.py`), never edited; a mistake is cancelled (entry reversed, event `"cancel"`) or returned.
- Taxes are data (`TaxCode`, seeded by `services.ensure_defaults`): ض.ق.م 14%, معفاة/صفر, خصم 1% / 3% / 5%.
  **VAT starts "not recoverable"**: it is added to the line's cost (`PurchaseLine.cost` = net + VAT). A VAT-registered
  center ticks "تتخصم" on the code and the VAT goes to `vat_input` instead. Tax is worked out per line and rounded per
  line (`price_line`); with prices that include VAT the tax is what is left after the net, so net + VAT is exactly what
  was typed. Totals are sums of lines (CHECK constraints on every document).
- Purchase invoice: Dr each line's account (cost center required for expenses; control and treasury accounts refused)
  + Dr `vat_input` / Cr `suppliers_payable` [supplier]. The same paper invoice of a supplier cannot be entered twice
  (partial unique on `supplier_ref`). The scan is a private file checked by content (`purchasing:invoice_scan`).
  `open_amount(invoice)` = total less returns less allocations of posted payments. `cancel_invoice` only while nothing
  was paid or returned.
- Return (`create_return(invoice, quantities={line pk: qty})`): each line's amounts go back in proportion and **the
  last piece of a line takes whatever is left of it**, so a line returned in steps comes to exactly its own amounts.
  More returned than was still unpaid leaves a debit on the supplier's account (they owe the clinic).
- Paying (`pay_supplier`): cash from a drawer / safe (never below zero), bank transfer, or cheque. `allocations` say
  which invoices it settles; the rest stays on the supplier's account. **Withholding** (`wht_code`): `wht_base`
  defaults to the taxable value being settled (each allocation less its share of the invoice's VAT,
  `withholding_base`); the supplier gets `amount - wht_amount`, their account drops by `amount`, and the withheld part
  is owed in `wht_payable` (paid to the tax authority by an ordinary payment voucher). `suggest_withholding` applies
  the supplier's usual code only above `PurchaseSettings.wht_threshold` (300). The form 41 lines are the payments
  with `wht_amount > 0` (`reports.withheld`).
- Cheques (`treasury/cheques.py`): issued ones sit in `cheques_payable` until `cleared` (then the bank is credited);
  received ones in `cheques_receivable` until `collected`, `bounced` or `endorsed` to a supplier; depositing moves no
  money. **A supplier payment by cheque and its cheque never disagree**: the payment is cancelled only through the
  cheque (`move(..., CANCELLED | RETURNED)` → `cancel_payment(from_cheque=True)`), and not once the bank paid it.
  `undo(cheque)` reverses the last step's entry (each step posts as `event="step:<n>"`, so a redone step posts again).
  `cheques.open_total(org, direction)` must equal the control account: the tests check it after every step.
  `due_soon(org, days)` feeds the notice on the treasury screens (a bank that cannot pay the cheques drawn on it is
  shown in red). `receive_cheque(payer=...)` settles a contracted company's balance.
- Recurring expenses (`ExpenseTemplate`, treasury tab "مصروفات متكررة"): the program fills the payment voucher and
  shows which ones this month still has not paid; **a person presses the button** (`record_expense`, once a month,
  `Voucher.template`). Nothing leaves a treasury by itself.
- Reports (`purchasing/reports.py`): `open_invoices`, `aging` (buckets by due date), `vat_paid` (recoverable vs in
  cost, net of returns), `withheld`, `quarter_of`. Supplier statement = `ledger.reports.account_statement(...,
  party=supplier)`. All export to Excel.
- Screens: side menu "المشتريات والموردين" (`/purchases/`: فواتير المشتريات, الموردين + أعمار الديون, السداد, الضرائب) and
  two new treasury tabs (الشيكات, مصروفات متكررة). Permissions, owner only by default: `purchase.view`,
  `supplier.manage`, `purchase.manage`, `supplier.pay`, `cheque.manage`, `tax.manage`.
- Tests: `tests/test_purchasing.py` (the Definition of Done scenario to the piaster, every refusal, cheques both ways,
  screens, and a hypothesis test: suppliers' account, cheques on their way, tax withheld and recoverable VAT each
  equal their documents after every random step).
- Assumptions written down: the clinic is not VAT-registered (VAT paid is cost); withholding is computed on the value
  without VAT and not at or under 300 EGP; stamp duty and other taxes are not modelled (enter them as a line).
- Not in this phase: stock items on purchase lines (phase 18 adds `item` / `warehouse` to `PurchaseLine`), fixed
  assets register and depreciation, automatic e-mail alerts for cheques (the notice is on screen).

## Inventory notes (Phase 18, 2026-11-16)
- Plan 14 §14.8. New app `apps/inventory`: `Warehouse` (defaults "main" المخزن الرئيسي and "theatre" دولاب العمليات),
  `StockItem` (unit, reorder level, `track_expiry`, `sale_price`), `StockBalance` (item + store: quantity **and
  value**), `StockBatch` (expiry lots), `StockMove`, `StockDoc` (issue / transfer / adjustment / opening, numbered
  `ISS-` `TRF-` `ADJ-` `OPN-`), `StockCount` + lines, `InventorySettings`.
- **Quantities and values change only in `services._move`**, under `select_for_update` on that item's balance row.
  Costing is weighted average per item per store: a balance keeps its total value, what goes out takes
  `cost_out` = its share of it, and **the last unit out takes all that is left**, so no rounding remainder is ever
  stranded (CHECK constraints: nothing negative, and an empty shelf has no value). A move stores the balance after it
  (`quantity_after`, `value_after`): the item card, and the check that a balance equals the replay of its moves.
  **`stock_value(org)` must equal the inventory account**: the tests and `verify_books` check it.
- Entries: an issue posts Dr supplies used [cost center] / Cr inventory; an issue returned to the store the reverse
  (at the value it left with, `event="return:<move pk>"`); a count's shortage / overage goes to
  `stock_over_short` [admin]; an opening balance credits `opening_balances`; **a transfer posts nothing** (the value
  only changes shelf). A purchase receipt posts nothing either: the purchase invoice itself debits inventory.
- Purchases: `PurchaseLine.item` / `warehouse` / `expiry_date` / `lot` (`LineIn(item=..., warehouse=...)`). A stock
  line's account is the inventory account and its cost (net + VAT that is not recoverable) is what goes on the shelf.
  An invoice with stock lines cannot be cancelled, only returned. **A purchase return takes the goods out at today's
  average** (`PurchaseReturnLine.stock_value`); the difference from what the supplier credits goes to
  `stock_over_short`, so inventory never drifts from the shelves. A return of goods no longer there is refused.
- Expiry: lots are created by receipts of `track_expiry` items (a date is required) and consumed **first to expire,
  first out** (`_batches_out`); a transfer carries the earliest lot's date. `expiring(org)` and `low_stock(org)` feed
  the two notices on top of the items screen (`InventorySettings.expiry_alert_days`, default 60).
- Counts (جرد): `start_count` (full, or spot = chosen items or a random sample; one running count per store) →
  `save_count` (the sheet shows **no** expected quantities) → `submit_count` (the books' figures are written next to
  the counted ones) → `approve_count` (needs `stock.approve_count`; **not the person who counted, unless the owner**;
  refused if the stock moved after the count was submitted) → adjustment moves + entry. Something found on an empty
  shelf comes in at the last price paid for it.
- Operations: `theatre.add_consumable(op, item=...)` issues the item from the theatre cabinet (the main store if the
  cabinet lacks it) to cost center theatre, and puts an `invoicing.Charge` on the patient's account when the item has
  a `sale_price`; `remove_consumable` returns it to the shelf and cancels the charge (refused once invoiced; the line
  stays with `is_removed`). `ProcedureType.kit` (typed on the procedure form as "name = quantity" lines) is issued
  in one step by `issue_kit(op)` and then corrected to what was really used. Free-text consumables still work.
- Screens: side menu "المخزن" (`/stock/`: الأصناف + item card, الصرف والتحويل, الجرد, and المخازن set-up). The purchase
  form shows a stock-item column only when the clinic has stock items. Permissions (owner only by default):
  `stock.view`, `stock.move`, `stock.count`, `stock.approve_count`.
- Tests: `tests/test_inventory.py` (exact costs and entries, FEFO, counts, operations, screens, and a hypothesis test:
  after every random move the shelves' value equals the inventory account, lots add up, and each balance equals the
  replay of its moves).
- Assumptions written down: weighted average cost per store; price differences on returns and count differences go to
  one account (عجز وزيادة المخزون); supplies with no sale price are inside the operation's package price.
- Not in this phase: several units per item (box / piece), purchase orders, and stock for the outpatient clinics as a
  separate store (add a store from المخازن).

## Statements and verification notes (Phase 19, 2026-11-16)
- Plan 14 §14.9. `ledger/statements.py` (read-only, posted lines only): `income_statement(org, start, end,
  cost_center=, compare="previous"|"year")`, `by_cost_center`, `balance_sheet(org, as_of, compare_as_of=)`,
  `cash_movement`. Amounts carry the natural sign of their section. A group row is shown only when it adds up two
  lines or more (a chain of groups repeating one number is noise). Every leaf amount on screen links to that account's
  statement for the same dates (the drill-down). `BalanceSheet.result` = revenue less expenses not yet closed into
  retained earnings; `.balanced` must hold (a hypothesis test checks it for any entries).
- **Books verification** (`ledger/verify.py`): `verify(org)` → list of `Problem(code, message)`; empty = sound.
  Checks: entries balance; every document has its entry and every entry's document exists; control accounts have no
  line without a party, and suppliers / cheques both ways / inventory / patient deposits equal their documents;
  numbering has no gaps (entries, invoices, vouchers, purchase and stock documents); nothing posted into a month
  after its close; no negative cash treasury or shelf; lots add up; each stock balance equals the replay of its moves;
  the visits of the last 30 days carry exactly the invoice lines they should. A check that cannot run is itself a
  problem (`check.error`), never a silent pass. **When a later phase adds a money document, add it to
  `verify._documents` and, if it has a number, to `check_numbering`.**
- `verify.run(org, by=)` stores a `BooksCheck`; screen "فحص الدفاتر" (`ledger:verify`, `books.verify`);
  `manage.py verify_books [--org] [--alert]` (exit 1 on problems); scheduler job `verify_books` daily → owners are
  e-mailed once a day at most (`ops.alert` audit event, like `check_health`). **`close_period` refuses while the
  books do not verify** (`verified=True` skips the re-check when the caller has just verified).
- Year close (`ledger/closing.py`): `checklist(year)` (earlier years closed, months 1-11 closed, books verify, no
  cash difference or stock count waiting) → `close_year` posts one entry on the year's last day that empties every
  revenue and expense account **per cost center** into `retained_earnings` (source = the fiscal year, event
  `close:<n>`), closes the last month and the year. `reopen_year` reverses it (event `reopen:<n>`), so a year can be
  corrected and closed again. **Statements leave out every entry whose source is a fiscal year** (`statements.CLOSING`),
  so a closed year still shows what it earned; the balance sheet keeps them.
- Management reports (`invoicing/reports.py`, screens under `/reports/`, tabs on the existing "التقارير" page,
  `report.finance`): revenue by doctor and by item (invoice lines less credit-note lines dated in the period),
  operation profitability (price after discount + extra charges - team shares - supplies at cost), patients'
  receivables by age (from `account_state`), cash movement per treasury.
- Permissions: `report.statements` (income statement, balance sheet) and `books.verify`, owner only by default. The
  "الدفاتر" menu item opens on the first of trial balance / income statement / verification the person may see, and
  each tab is hidden without its permission.
- Tests: `tests/test_statements.py` (statements against real documents, the balance sheet for any entries, the
  reports, every verification problem named by its code, the year closed / reopened / closed again, screens).
- Not in this phase: a statement of cash flows by activity (the cash movement per treasury is there), budgets,
  depreciation.

## Hardening notes (Phase 20, 2026-11-16)
- Plan 14 §14.10. **Default roles**: a new clinic gets eight (`accounts/roles.DEFAULT_ROLES`): the clinic's four plus
  محاسب (`accountant`, opens on الدفاتر), كاشير (`cashier`, الخزنة), أمين مخزن (`storekeeper`, المخزن), تمريض عمليات
  (`theatre_nurse`, العمليات). Their permission sets are `permissions.EXTRA_ROLE_PERMISSIONS`
  (`roles.default_permissions(key)`); the accountant keeps the books but gives no discounts. Existing clinics got them
  by `organizations/0007_center_roles` (frozen lists; a test checks they equal today's defaults: change both
  together, or write a new migration).
- **`manage.py seed_center [--org] [--days 30] [--seed 7]`** (`apps/core/management/commands`): a month of a center's
  life through the real services (visits, services, discounts, shift closes, purchases, cheques with withholding,
  operations with a kit and a stay, deposits, invoices, recurring expenses, a payer, doctor payouts, a stock count),
  then `verify`. Demo / test databases only: nothing it makes can be removed, it needs freezegun, it cancels every
  message it queued and it refuses to run twice. `tests/test_seed_center.py` runs 12 days of it and opens every
  money screen: **the integration test of phases 13-19**.
- **Merging two patient files** (`patients.services.merge_patients`) now moves everything (`MERGE_MOVES`: bookings,
  visits, attachments, prescriptions, the pregnancy file, emergencies, operations, stays, charges, invoices,
  payments). It is refused while the duplicate still owes money or has a deposit (her account in the books carries
  her own party id: settle it first), when both have a followed pregnancy, or both are in a room. **A new model with
  a `patient` FK must be added to `MERGE_MOVES`.**
- Pregnancy leftovers of phase 12, done: ultrasound biometry on the antenatal sheet (`bpd_mm`, `hc_mm`, `ac_mm`,
  `fl_mm`); Settings → "متابعة الحمل" (`obstetrics:milestones`, OB/GYN clinics only; a row is switched off, not
  deleted); messages through the outbox: `milestone_due` (scheduler job `pregnancy_milestones` →
  `obstetrics.notify_milestones`, once per pregnancy and milestone) and `postpartum` (queued by `close_pregnancy`
  after a live birth, `POSTPARTUM_AFTER_DAYS`); `{pregnancy_week}` / `{pregnancy_month}` are available to every
  appointment template; the pregnancy file is on the patient export PDF (`obstetrics.context.export_ctx`). The two
  pregnancy templates are seeded for OB/GYN clinics only (`notifications.defaults.templates_for(org)`).
- Guards added: `test_performance.test_money_screens_are_constant_in_queries` (23 money screens, small vs large
  data), `test_crawl.test_the_books_verify_after_a_seeded_and_simulated_day`. The permission matrix and the crawl
  already cover every URL of phases 13-19.
- `docs/user-guide.md`: the staff guide in Arabic, one section per screen.
- The whole suite takes about 20 minutes now. Phases 13 to 20 were committed together as one commit (see HANDOFF.md).

## Safety and doctor's screen notes (Phase 21, 2026-11-22)
- Plan: `docs/plan/15-enhancements.md` (the review after phase 20; phases 21-26). Phase 21 = §0.1-0.4, §2.4, §2.5.
- **Prescription safety** (`prescriptions/safety.py` + the tables in `prescriptions/drug_rules.py`): besides allergies and
  duplicates, every draft is checked for **pregnancy** (a followed `Pregnancy`, by its week: NSAIDs amber before 20
  weeks and red from 20, antithyroid drugs only before 16, low-dose aspirin is never flagged), **breastfeeding** (a live
  birth with its date in the last `PrescriptionSettings.lactation_months`, default 6, 0 = off; Settings → الروشتة →
  تحذيرات الأمان) and **interactions** between lines and between a line and the patient's current medications (two
  current medications alone never warn). Rules match the **generic name** at the start of a word
  (`(?<![a-z])term`), so terms must be specific; `LOCAL_FORMS` (creams, eye/ear drops...) use `local_level`
  (None = the rule does not apply). Levels: `avoid` red, `caution` amber (both need "متأكد" before finalize),
  `info` blue (spacing advice, no tick). A warning is `{key, kind, level, text, needs_ack}`; `safety.pending()` is
  what blocks `finalize`. The tables were written from standard references, not by a clinic doctor: **have a doctor
  review `drug_rules.py` before go-live, and extend the tables there rather than adding checks elsewhere.**
- The drug search shows a red / amber "ممنوع / حذر في الحمل (الرضاعة)" tag per result (`safety.drug_flags`), and the
  builder says when the checks are on (`#rx-state`).
- **Current medications** (`patients.CurrentMedication`, history, in `MERGE_MOVES`): what the patient takes regularly,
  whoever prescribed it. `patients.services.add_medication` (links the catalog drug by exact name, refuses the same
  name twice) / `stop_medication` (sets `stopped_on`, never deletes). Screens: patient page (medical section),
  drawer tab + action-bar button "الأدوية المستمرة" on the visit page, "بتاخد:" line in the visit header
  (`#header-meds`). Any change answers with `HX-Trigger: meds-changed`; the header re-reads itself and the visit page
  reloads the prescription builder (warnings recomputed). Permissions: `patient.view_medical` / `edit_medical`.
- **Dates**: `{{ d|day }}` → "4 أكتوبر 2026" (`|day:"short"` without the year), wrapped in an RTL isolate so it reads
  right inside `dir="ltr"` spans. The `clock` filters (`day`, `ampm`) are template builtins now. Clinical screens use it;
  `d/m/Y` stays in the money tables, `Y-m-d` stays only in attributes / exports.
- **Emergency alert** on the desk sits at the bottom (above the visit page's action bar, `--action-bar-h`), never over
  the patient header. **"جنب بعض"** on the visit page (≥ 1280 px): the visit and the prescription side by side,
  remembered per computer (`localStorage visit-split`); the tabs stay the default.
- Not done here (found during the review, small): allergies and chronic conditions are still hard-deleted by their
  "مسح" buttons (`patients.views.allergy_delete` / `condition_delete`), against "medical records are never deleted".
