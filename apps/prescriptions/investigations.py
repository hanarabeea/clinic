"""Common lab tests and imaging requested in Egyptian clinics — suggestions for the prescription's "الطلبات" section
(the doctor can always type anything else). Names are the way they're written on a request (English, as labs read
them); `kind_for` guesses lab / imaging for a typed name."""

LABS = [
    "CBC", "ESR", "CRP", "FBS (سكر صايم)", "PPBS (سكر فاطر)", "RBS (سكر عشوائي)", "HbA1c (السكر التراكمي)",
    "Lipid profile", "Liver function tests (ALT, AST, Albumin, Bilirubin)", "Kidney function tests (Urea, Creatinine)",
    "Serum uric acid", "TSH", "Free T3, Free T4", "Serum electrolytes (Na, K)", "Serum calcium", "Vitamin D (25-OH)",
    "Vitamin B12", "Serum ferritin", "Serum iron & TIBC", "PT, PTT, INR", "Urine analysis", "Stool analysis",
    "Urine culture", "Throat swab culture", "ASO titre", "RF (Rheumatoid factor)", "Anti-CCP", "ANA",
    "H. pylori Ag in stool",
    "HCV Ab", "HBsAg", "HIV Ab", "Pregnancy test (β-hCG)", "PSA", "Semen analysis", "Prolactin", "FSH, LH",
    "Serum amylase & lipase", "Troponin", "D-dimer", "Blood group & Rh", "CA-125", "CEA", "AFP",
]  # fmt: skip

IMAGING = [
    "Chest X-ray (PA view)", "X-ray (منطقة: ...)", "Abdominal & pelvic ultrasound", "Pelvic ultrasound",
    "Transvaginal ultrasound", "Obstetric ultrasound", "Thyroid ultrasound", "Breast ultrasound", "Scrotal ultrasound",
    "Doppler ultrasound (...)", "Echocardiography", "ECG", "CT brain", "CT chest", "CT abdomen & pelvis",
    "MRI brain", "MRI spine (...)", "MRI knee", "Mammography", "DEXA scan", "Upper GI endoscopy", "Colonoscopy",
    "Spirometry (PFT)", "Audiometry", "EEG", "Nerve conduction study (NCS/EMG)",
]  # fmt: skip

_IMAGING_WORDS = (
    "x-ray", "xray", "ultrasound", "mri ", "ct ", "doppler ", "sonar", "doppler", "echo", "ecg", "ct-", "mri",
    "mammo", "dexa", "endoscopy", "colonoscopy", "spirometry", "audiometry", "eeg", "emg", "ncs",
    "اشعه", "أشعة", "اشعة", "سونار", "رسم قلب", "رنين", "مقطعيه", "مقطعية", "منظار", "ايكو", "دوبلر",
)  # fmt: skip


def kind_for(name: str) -> str:
    low = f"{name.lower()} "
    if any(w in low for w in _IMAGING_WORDS):
        return "imaging"
    lab_words = ("test", "analysis", "culture", "serum", "تحليل", "صورة دم", "سكر", "وظائف")
    if name in LABS or any(w in low for w in lab_words):
        return "lab"
    return "lab" if name.isupper() else "other"


# --- voice: what a doctor says → the request written on the prescription ---------------------------------------
# (spoken Egyptian phrase, canonical request). Matched fuzzily inside free speech; longer phrases win.
SPOKEN = [
    ("صوره دم", "CBC"), ("صوره دم كامله", "CBC"), ("cbc", "CBC"), ("سي بي سي", "CBC"),
    ("سرعه ترسيب", "ESR"), ("ترسيب", "ESR"), ("esr", "ESR"), ("crp", "CRP"), ("سي ار بي", "CRP"),
    ("سكر صايم", "FBS (سكر صايم)"), ("سكر فاطر", "PPBS (سكر فاطر)"), ("سكر بعد الاكل", "PPBS (سكر فاطر)"),
    ("سكر عشوائي", "RBS (سكر عشوائي)"), ("تراكمي", "HbA1c (السكر التراكمي)"), ("سكر تراكمي", "HbA1c (السكر التراكمي)"),
    ("دهون", "Lipid profile"), ("دهون الدم", "Lipid profile"), ("كوليسترول", "Lipid profile"),
    ("وظائف كبد", "Liver function tests (ALT, AST, Albumin, Bilirubin)"),
    ("كبد", "Liver function tests (ALT, AST, Albumin, Bilirubin)"),
    ("انزيمات كبد", "Liver function tests (ALT, AST, Albumin, Bilirubin)"),
    ("وظائف كلى", "Kidney function tests (Urea, Creatinine)"), ("كلى", "Kidney function tests (Urea, Creatinine)"),
    ("كرياتينين", "Kidney function tests (Urea, Creatinine)"),
    ("حمض يوريك", "Serum uric acid"), ("يوريك اسيد", "Serum uric acid"), ("نقرس", "Serum uric acid"),
    ("غده", "TSH"), ("غده درقيه", "TSH"), ("tsh", "TSH"), ("هرمون الغده", "TSH"),
    ("فيتامين د", "Vitamin D (25-OH)"), ("فيتامين دي", "Vitamin D (25-OH)"), ("فيتامين ب 12", "Vitamin B12"),
    ("فيتامين بي 12", "Vitamin B12"), ("مخزون الحديد", "Serum ferritin"), ("فيريتين", "Serum ferritin"),
    ("حديد", "Serum iron & TIBC"), ("سيولة", "PT, PTT, INR"), ("سيوله", "PT, PTT, INR"), ("inr", "PT, PTT, INR"),
    ("صوره بول", "Urine analysis"), ("تحليل بول", "Urine analysis"), ("مزرعه بول", "Urine culture"),
    ("تحليل براز", "Stool analysis"), ("براز", "Stool analysis"), ("مزرعه زور", "Throat swab culture"),
    ("مسحه زور", "Throat swab culture"), ("روماتيزم", "RF (Rheumatoid factor)"), ("اي اس او", "ASO titre"),
    ("جرثومه المعده", "H. pylori Ag in stool"), ("جرثومه", "H. pylori Ag in stool"), ("فيروس سي", "HCV Ab"),
    ("فيروس بي", "HBsAg"), ("تحليل حمل", "Pregnancy test (β-hCG)"), ("حمل", "Pregnancy test (β-hCG)"),
    ("بروستاتا", "PSA"), ("تحليل سائل منوي", "Semen analysis"), ("سائل منوي", "Semen analysis"),
    ("هرمون الحليب", "Prolactin"), ("برولاكتين", "Prolactin"), ("اميليز", "Serum amylase & lipase"),
    ("تروبونين", "Troponin"), ("فصيله الدم", "Blood group & Rh"), ("فصيله", "Blood group & Rh"),
    ("اشعه على الصدر", "Chest X-ray (PA view)"), ("اشعه صدر", "Chest X-ray (PA view)"),
    ("سونار على البطن", "Abdominal & pelvic ultrasound"), ("سونار بطن", "Abdominal & pelvic ultrasound"),
    ("سونار بطن وحوض", "Abdominal & pelvic ultrasound"), ("سونار حوض", "Pelvic ultrasound"),
    ("سونار مهبلي", "Transvaginal ultrasound"), ("سونار حمل", "Obstetric ultrasound"),
    ("سونار على الغده", "Thyroid ultrasound"), ("سونار غده", "Thyroid ultrasound"), ("سونار ثدي", "Breast ultrasound"),
    ("رسم قلب", "ECG"), ("ايكو", "Echocardiography"), ("ايكو على القلب", "Echocardiography"),
    ("رنين على المخ", "MRI brain"), ("مقطعيه على المخ", "CT brain"), ("مقطعيه على الصدر", "CT chest"),
    ("ماموجرام", "Mammography"), ("قياس كثافه العظام", "DEXA scan"), ("كثافه العظام", "DEXA scan"),
    ("منظار معده", "Upper GI endoscopy"), ("منظار قولون", "Colonoscopy"), ("وظائف تنفس", "Spirometry (PFT)"),
    ("قياس سمع", "Audiometry"), ("رسم مخ", "EEG"), ("رسم عصب", "Nerve conduction study (NCS/EMG)"),
]  # fmt: skip

# "<scan> على <body part>" → the scan with the part as said (when no exact phrase above matched)
_REGION = [("اشعه", "X-ray"), ("سونار", "Ultrasound"), ("رنين", "MRI"), ("مقطعيه", "CT"), ("دوبلر", "Doppler")]
_REGION_STOP = {"و", "او", "كمان", "وكمان", "مع", "بعد", "قبل", "لمده", "في", "عشان", "علشان"}


def _norm(text):
    from apps.core.arabic import normalize_arabic

    return normalize_arabic((text or "").lower())


def _bare(word, phrase_word):
    """Drop a و / ال stuck to a spoken word ("وغدة", "والدهون") unless the phrase itself starts with it."""
    for p in ("وال", "و", "ال"):
        if word.startswith(p) and not phrase_word.startswith(p) and len(word) - len(p) >= 3:
            return word[len(p) :]
    return word


def _pretty_part(word):
    return word[:-1] + "ة" if len(word) > 3 and word.endswith("ه") else word


# Said in a prescription these are medicines unless a request word comes just before ("تحليل / اعمل / مستوى ...")
AMBIGUOUS = {"Vitamin D (25-OH)", "Vitamin B12", "Serum iron & TIBC", "Serum ferritin", "Serum calcium", "Prolactin"}
_CUES = {"تحليل", "تحاليل", "اعمل", "اعملي", "تعمل", "تعملي", "نعمل", "هنعمل", "مستوي", "مستوى", "نسبه", "قياس", "نشوف"}


def _cue_before(words, i, reach=3):
    return any(_bare(w, "x") in _CUES or w in _CUES for w in words[max(0, i - reach) : i])


def split_requests(text: str):
    """For the prescription mic: (requests said, the rest of the speech) — the rest goes on to the medicine extraction
    without the request phrases ("سونار على البطن" must not become a drug)."""
    found, _u, words, taken = _find(text, require_cue=True)
    rest = " ".join(w for i, w in enumerate(words) if i not in taken)
    return found, rest


def find_requests(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    found, unknown, _w, _t = _find(text, require_cue=False)
    return found, unknown


def _find(text: str, *, require_cue: bool):
    """Requests said in free speech → ([(name, kind)], unrecognised scan phrases). Fuzzy (speech recognition is noisy),
    longest phrase wins, spoken order kept, each request once."""
    import re

    from rapidfuzz import fuzz

    words = re.sub(r"[^\w\s]", " ", _norm(text)).split()
    phrases = sorted(((_norm(p).split(), name) for p, name in SPOKEN), key=lambda x: -len(" ".join(x[0])))
    taken, found = set(), []  # found: (position, name)
    for ph, name in phrases:
        n = len(ph)
        target = " ".join(ph)
        for i in range(len(words) - n + 1):
            span = set(range(i, i + n))
            if span & taken:
                continue
            window = " ".join(_bare(w, ph[0]) for w in words[i : i + n])
            short = len(target) <= 3
            if (window == target) if short else fuzz.ratio(window, target) >= 88:
                if name in AMBIGUOUS and require_cue and not _cue_before(words, i):
                    continue  # "فيتامين د" alone is a medicine; "تحليل فيتامين د" is a test
                taken |= span
                found.append((i, name))
    # "<scan> على <part>" not covered above
    for i, w in enumerate(words):
        if i in taken:
            continue
        base = w[1:] if w.startswith("و") and len(w) > 4 else w
        for spoken, label in _REGION:
            if fuzz.ratio(base, spoken) >= 85 and i + 2 < len(words) and words[i + 1] in ("على", "علي", "ع"):
                part = []
                for k, x in enumerate(words[i + 2 : i + 4], start=i + 2):
                    if x in _REGION_STOP or k in taken or (x.startswith("و") and len(x) > 2):
                        break  # the next request starts ("وفيتامين د")
                    part.append(_pretty_part(x))
                if part:
                    taken |= set(range(i, i + 2 + len(part)))
                    found.append((i, f"{label} {' '.join(part)}"))
                break
    out, seen = [], set()
    for _i, name in sorted(found):
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append((name, "imaging" if name in IMAGING or kind_for(name) == "imaging" else "lab"))
    return out, [], words, taken
