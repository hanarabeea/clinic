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
    "x-ray", "xray", "ultrasound", "sonar", "doppler", "echo", "ecg", "ct ", "ct-", "mri", "mammo", "dexa", "endoscopy",
    "colonoscopy", "spirometry", "audiometry", "eeg", "emg", "ncs", "اشعه", "أشعة", "اشعة", "سونار", "رسم قلب", "رنين",
    "مقطعيه", "مقطعية", "منظار", "ايكو", "دوبلر",
)  # fmt: skip


def kind_for(name: str) -> str:
    low = f"{name.lower()} "
    if any(w in low for w in _IMAGING_WORDS):
        return "imaging"
    lab_words = ("test", "analysis", "culture", "serum", "تحليل", "صورة دم", "سكر", "وظائف")
    if name in LABS or any(w in low for w in lab_words):
        return "lab"
    return "lab" if name.isupper() else "other"
