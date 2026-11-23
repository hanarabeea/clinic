# 15 — Enhancements review (2026-11-22)

A full pass over DigiClinic after phases 1-20, compared with what clinic systems elsewhere offer (Cliniko, Jane,
Doctolib, athenaOne, eClinicalWorks / DrChrono / ModMed OB-GYN, Phreesia, Zocdoc, Vezeeta, WhatsApp booking bots).
Goal: **less time per patient for the doctor and the front desk, fewer mistakes, fewer empty slots.**

Nothing here is built yet. Each item says what exists today, why it matters, and a rough size
(S = a day or two, M = about a week, L = a phase). The owner picks; then each group becomes a phase with a
Definition of Done like phases 11-20.

---

## 0. Fix first (found during the review)

| # | Finding | Why it matters | Size |
|---|---|---|---|
| 0.1 | **No pregnancy / breastfeeding check on prescriptions.** `prescriptions/safety.py` checks allergies (+ drug classes) and duplicate lines only. A patient with an active `Pregnancy` can be prescribed isotretinoin, warfarin, an ACE inhibitor, a statin, methotrexate, misoprostol, tetracyclines... with no warning. | Patient safety, in an OB/GYN center. | S-M |
| 0.2 | **No drug-drug interaction check** (planned in 10-future-features P3, never built). | Safety; doctors expect it. A small curated table of the major interactions (warfarin + NSAIDs / macrolides, methotrexate + NSAIDs, SSRIs + tramadol, nitrates + sildenafil...) covers most real risk, same pattern as `DRUG_CLASSES`. | M |
| 0.3 | **The emergency alert covers the patient header** on the visit page (fixed red bar sits on top of the sticky header at 1366 px and below), hiding the name, visit number and the allergy line. | The allergy line must never be hidden (04 §4.3). | S |
| 0.4 | Dates are written four ways on clinical screens (`4/10/2026`, `04/10/2026`, `2026-10-04`, `2026/10/04`). | A doctor reading history, the due date and the last visit side by side can swap day and month. Use "4 أكتوبر 2026" on clinical screens. | S |

Proposal for 0.1: `PREGNANCY_RISK` table (generic name / class → level `avoid` / `caution` + one-line Arabic reason,
plus `lactation` flag), checked when the patient has an active pregnancy or delivered in the last N months (setting);
same "acknowledge to finalize" mechanism as allergy warnings. Shown also as a red tag next to the drug in the search
results so the doctor sees it before adding.

---

## 1. Biggest daily time savers (front desk)

| # | Feature | Today | What others do | Size |
|---|---|---|---|---|
| 1.1 | **Reply to the reminder: 1 = confirm, 2 = cancel** (any other text → the reception inbox). Confirm sets `confirmed`; cancel frees the slot and alerts reception. | Inbound WhatsApp messages are stored (`InboundMessage`) and shown on the patient page only. Nothing acts on them. Already P1 in 10-future-features. | Every system with reminders offers one-tap confirm / reschedule; it is the main no-show lever after reminders themselves. | M |
| 1.2 | **Reception inbox**: one list of unread patient messages (matched to a patient, link to her bookings), mark as handled. | Hidden on each patient page. | Standard (two-way texting). | S |
| 1.3 | **Waiting list + "a slot opened"**: patient wants an earlier date / a full day; when someone cancels, the first matching person gets a WhatsApp offer, first to reply "1" gets it. | None. | Phreesia / Zocdoc / Doctolib fill cancellations automatically. | M |
| 1.4 | **Egyptian national ID (14 digits)** on the patient: fills date of birth and gender automatically, validates the check digit, searchable. Plus **husband's name** (standard on OB/GYN files in Egypt). | Typed by hand; no husband field (only `guardian_name`). | Local systems all have it. | S |
| 1.5 | **Waiting-room TV screen**: full-screen page with the number / short name being called, sound on call. Driven by "دخّل" / the doctor's "call next". | None. Already P1 in 10-future-features. | Common in Egyptian clinics. | S |
| 1.6 | **No-show risk tag** on the board (patient's own history: `no_show_count`, last-minute bookings) → extra reminder, or ask for a deposit. | `no_show_count` exists, not used for anything. | Phreesia / dental tools flag risky bookings. | S |
| 1.7 | **Family on one phone**: when a number has several files, the booking panel shows them as cards (03 §3.2 describes this). Check it is really there; if not, build it. | Search returns them as a list. | | S |

## 2. Biggest daily time savers (doctor)

| # | Feature | Today | Why | Size |
|---|---|---|---|---|
| 2.1 | **Visit templates ("order sets")**: one click fills complaint / exam / plan, the prescription, the investigations and the follow-up days. E.g. "ANC routine 2nd trimester", "Post-op check", "PCOS first visit". Built from a finished visit ("save as template"). | Quick phrases per field, Rx templates separately, requests separately. | The single most recommended EHR efficiency tool (AAFP, athena "encounter plans"). | M |
| 2.2 | **Close the results loop**: requests (labs / imaging) become "waiting for result"; reception or the doctor enters the result (number for common labs: Hb, FBS, HbA1c, TSH, ferritin, vitamin D, urine...; or attach the PDF); abnormal values flagged red; trends on the snapshot; list "results waiting / abnormal not reviewed" on the desk. | Requests print on the prescription, then nothing. Only `hemoglobin` is structured (antenatal sheet). | Lab results inbox is core to every EHR; abnormal results are where things get missed. | L |
| 2.3 | **One-click medical documents** from templates with the patient's data: sick-leave certificate, medical report, referral letter, pregnancy / fitness certificate, ultrasound report, visit summary. Same PDF engine and numbering as prescriptions, logged in the file. | None (only the prescription, discharge summary and invoice PDFs). | Daily paperwork in Egyptian clinics. | M |
| 2.4 | **Visit and prescription side by side**: since the phase 11 redesign they are two tabs at every screen size, so the doctor switches back and forth. Offer "side by side" on screens from 1280 px, remembered per computer, keeping the tabs as the default. | Tabs only. | Fewer clicks per visit. | S |
| 2.5 | **Current medications list** on the patient (what she takes regularly, from any doctor), carried from visit to visit and checked by 0.1 / 0.2. | Chronic conditions exist; medications only inside past prescriptions. | Needed for interaction checks and handovers. | S-M |
| 2.6 | **Structured past history**: surgical history, family history, social (smoking) as quick lists, not free text. | `history` is one text field per visit. | Snapshot and risk flags can then use them. | S |
| 2.7 | **Pre-visit summary**: before the doctor opens the visit, a short "what changed since last time" (new results, missed milestones, open problems, current meds). Most of it can be rule-based from existing data; an AI summary is optional later. | `workspace.changes_since` covers vitals only. | Doctolib / athena chart-prep. | M |
| 2.8 | **Ambient scribe (optional, later)**: the conversation becomes a draft note (complaint / exam / diagnosis / plan) the doctor confirms, building on the existing dictation pipeline. Evidence: about 10% less note time in trials (Nabla RCT, JAMA multi-site); bigger for frequent users. Needs a cloud model or a GPU, and a legal review under Law 151/2020 before audio leaves the clinic. | Dictation per field + conversational prescription extraction. | | L |

## 3. OB/GYN depth (the center's specialty)

| # | Feature | Today | Size |
|---|---|---|---|
| 3.1 | **Fetal growth charts**: EFW and BPD / HC / AC / FL plotted against gestational age with reference percentiles (Hadlock or INTERGROWTH-21st, a setting), auto-flag below 10th / above 90th, and the EFW percentile written on the sheet. | Biometry stored, not charted or graded. | M |
| 3.2 | **Mother's charts**: weight gain vs the recommended range for her pre-pregnancy BMI; BP trend with a pre-eclampsia flag (≥ 140/90 after 20 weeks, or protein in urine); fundal height vs weeks. | Weight / BP sparklines only. | S-M |
| 3.3 | **Antenatal labs by schedule** tied to 2.2: blood group / Rh, CBC, OGTT at 24-28 weeks, urine, hepatitis, toxoplasma... each milestone shows its result, and Rh-negative mothers get the anti-D reminder at 28 weeks / after delivery. | Milestones are checkboxes. | M |
| 3.4 | **Gynecology (non-pregnant) file**: menstrual history (LMP, cycle, flow), contraception with an end date (IUD / implant / injection → reminder when due), Pap / HPV screening with a recall date, menopause. | Only cycle length and contraception text on `ObstetricProfile`. | M |
| 3.5 | **Infertility follow-up**: folliculometry per cycle day (follicle sizes per ovary, endometrium thickness) as a small table + chart, trigger / IUI / transfer dates feeding IVF dating. | IVF transfer date is a dating method only. | M |
| 3.6 | **Labour record**: partograph (dilation vs time, alert / action lines), CTG attachment, delivery summary feeding the newborn rows. | Delivery = an operation + newborn rows. | L |
| 3.7 | **Newborn → own patient file** (with the mother linked) for the first pediatric visit and vaccinations, if the center sees babies. | Newborn rows only. | S |

## 4. Patients outside the clinic (growth and fewer phone calls)

| # | Feature | Today | Size |
|---|---|---|---|
| 4.1 | **Online booking link** (clinic page: visit type → free slot → name + phone → WhatsApp code), same booking engine. | None (10-future-features P2). | M |
| 4.2 | **WhatsApp booking bot**: "احجز" → visit type → next free times → confirm. Uses the same engine as 4.1. | None. | M-L |
| 4.3 | **Patient link** (no app): a signed link in WhatsApp to see her upcoming bookings, prescriptions, results, invoices, and cancel / reschedule within the rules. | None. | M |
| 4.4 | **Recall campaigns**: overdue follow-ups, annual Pap, 6-week postpartum, contraception due, "you haven't visited in a year"; preview the list, then send through the outbox (rate-limited, consent-checked). | `followup_due`, `milestone_due`, `postpartum` exist individually. | M |
| 4.5 | **Online payment** for deposits / no-show-risk bookings (InstaPay link, Paymob / Fawry). | Cash / card recorded by hand. | M |
| 4.6 | **Official WhatsApp Cloud API** provider (approved templates), SMS fallback. whatsapp-web.js risks the number being banned as volume grows. | 10-future-features P2. | M |

## 5. Owner, security, compliance

| # | Feature | Today | Size |
|---|---|---|---|
| 5.1 | **Two-step sign-in** (TOTP app) for owner / doctors / finance roles; new-device e-mail. | Password + axes lockout. | S-M |
| 5.2 | **ETA e-receipt integration** for patient receipts (Egypt widened e-invoicing to B2C receipts; whether medical services are in scope must be confirmed with the center's accountant first). | None. | L |
| 5.3 | **Insurance / UHIS claims**: payer contracts exist (phase 16); add claim batches, approvals (pre-authorization numbers), rejections. | Payer share on invoices only. | L |
| 5.4 | **Doctor's phone view** (installable web app): today's list, open a file, approve results, at home or in theatre. | Responsive pages, not tuned for phone. | M |
| 5.5 | **Daily owner digest** (WhatsApp / e-mail at night): visits, revenue, cash differences, no-shows, abnormal results waiting, cheques due. | Dashboard only. | S |
| 5.6 | **Time-per-step measurement**: arrival → called → finished, waiting time per doctor and per hour, shown on reports, so changes can be measured. | Timestamps exist; not reported. | S |

---

## Suggested order (as phases)

| Phase | Contents | Why first |
|---|---|---|
| 21 | 0.1 – 0.4, 2.4, 2.5 | Safety + the doctor's screen at the clinic's real screen size. |
| 22 | 1.1, 1.2, 1.3, 1.6 | No-shows and empty slots are lost money every day. |
| 23 | 1.4, 1.5, 1.7, 2.1, 2.3, 2.6 | Seconds saved on every single patient. |
| 24 | 2.2, 3.3 | Results loop (largest risk of something being missed). |
| 25 | 3.1, 3.2, 3.4, 3.5 | OB/GYN depth. |
| 26 | 4.1 – 4.4 | Patients self-serve. |
| later | 2.7, 2.8, 3.6, 3.7, 4.5, 4.6, 5.x | Each needs an outside decision (provider, legal, accountant) or is large. |

### Phase 21: Definition of Done

- A draft for a patient with a followed pregnancy warns (red / amber, by week) for the drugs in
  `prescriptions/drug_rules.PREGNANCY`, also in the search results; after a live birth the `LACTATION` rules apply for
  `lactation_months`; neither applies to men or to women without those records.
- Interactions between lines, and between a line and a current medication, warn once per pair; spacing advice (`info`)
  shows without blocking; red / amber warnings block finalize until ticked.
- Current medications can be added (catalog-linked by exact name, or from the patient's past prescriptions) and stopped
  (kept with the date) from the patient page and the visit page; the visit header lists them; merge moves them;
  reception gets 403; another clinic gets 404.
- The emergency alert never covers the visit header; clinical dates read "4 أكتوبر 2026"; "جنب بعض" shows both panes
  from 1280 px.
- Quality gates green; `tests/test_rx_safety.py`.

### Phase 22: Definition of Done

- A WhatsApp "1" / "2" answering a confirmation or reminder (within the reply window) confirms / cancels the booking;
  anything else, and every cancellation, waits in the inbox until marked handled; a message delivered twice acts once.
- A cancelled or moved booking offers its place to the first matching patient on the waiting list (dates, time of day,
  consent, still free), one at a time; "1" books it, "2" or silence passes it on; nobody is offered the same place
  twice; a booking inside the wanted days closes the request; past requests expire.
- Patients who often miss show a tag and get one more reminder.
- Settings for all of it; screens gated (reception 200, viewer 403, another clinic 404); quality gates green;
  `tests/test_replies_waitlist.py`.

### Phase 23: Definition of Done

- A valid national ID fills date of birth and gender, an impossible one is refused, a second active file with the same
  ID is refused; husband's name on the file; search by ID.
- Booking search shows age, last visit and "نفس الرقم" for files sharing a phone.
- Past history lines (surgical / family / social / other) added and removed (hidden) from the patient page and the
  visit drawer.
- A visit saved as a template fills another visit (fields, prescription lines, requests, follow-up) without removing
  or doubling anything.
- Six kinds of medical documents drafted from the clinic's wording, edited, issued with a yearly number, printed as a
  PDF, voided with a reason; the wording is editable in Settings.
- The waiting-room screen shows who is inside and next per doctor, chimes on a call, and can hide names.
- Reception 403 / other clinic 404 on the new clinical URLs; quality gates green; `tests/test_phase23.py`.

## Open questions for the owner

1. Which clinics / centers will use it first: only this OB/GYN center, or general clinics too? (Changes the weight of
   section 3 vs the rest.)
2. Is sending patient data (audio, text) to a cloud AI acceptable, or must everything stay on the clinic's machine?
   (Decides 2.7 / 2.8.)
3. Does the center want patients to book and pay online (4.1, 4.5), or keep all booking through reception?
4. Reference for fetal growth: Hadlock or INTERGROWTH-21st? (Default proposal: INTERGROWTH-21st, a setting.)

## Sources used

- Competitor overviews: Pabau's Cliniko alternatives (pabau.com/blog/cliniko-alternatives), Capterra Cliniko
  alternatives, Doctolib Pro review (lonasante.com), heise on Doctolib's practice software.
- OB/GYN EHR features: eClinicalWorks OB/GYN, DrChrono OB/GYN, OmniMD, Meditab IMS blog, ModMed OB/GYN.
- No-shows and waitlists: Zocdoc waitlist article, Phreesia products, DoctorConnect, DentistryIQ.
- WhatsApp in MENA: Wati, SleekFlow, LetsBot (Egypt), Tanla / Dr. Sulaiman Al Habib case study.
- EHR efficiency: AAFP "Techniques to reduce EHR usability burden", athenahealth chart-prep case study.
- AI scribes: JAMA Network Open multi-site study, Nabla / DAX randomized trial (PubMed 41497288), JMIR Med Inform 2026.
- Egypt: Avalara e-invoicing guide (B2C receipts), Ahram Online on the UHIS rollout.
