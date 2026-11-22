"""Pregnancy, breastfeeding and drug-drug interaction rules for the prescription safety net (plan 15 §0.1-0.2).

Same spirit as `safety.DRUG_CLASSES`: a short, conservative table of well-established risks, matched on the drug's
generic name (the Egyptian database writes them in English, "+" between ingredients). It is a safety net, not a
clinical decision system: the doctor still decides, and every red / amber warning must be acknowledged before the
prescription is finalized. **A doctor of the clinic should review this table before go-live; extend it here rather
than adding checks elsewhere.**

Terms match at the start of a word of the generic name or the trade name ("cef" would match cefixime), so a term must
be specific enough not to hit an unrelated ingredient.

Levels: `avoid` (red), `caution` (amber) both need the doctor's "متأكد"; `info` (blue) is advice that needs no
acknowledgement (e.g. "leave two hours between iron and calcium").
"""

from dataclasses import dataclass, field

AVOID, CAUTION, INFO = "avoid", "caution", "info"
NEEDS_ACK = (AVOID, CAUTION)

# Forms that act on the spot (skin, eye, ear, nose): most systemic rules do not apply to them.
LOCAL_FORMS = {"كريم", "مرهم", "جل", "لوشن", "نقط عين", "نقط أذن", "بخاخ أنف", "للفم"}


@dataclass(frozen=True)
class Rule:
    key: str
    terms: tuple[str, ...]
    level: str
    reason: str
    # pregnancy only: from this week on the level becomes `late_level` (NSAIDs: caution early, avoid from 20 weeks)
    late_weeks: int | None = None
    late_level: str | None = None
    # until this week only (antithyroid drugs: the first trimester); None = the whole pregnancy
    until_weeks: int | None = None
    # level for creams / drops / gels; None = the rule does not apply to local forms
    local_level: str | None = None

    def level_at(self, weeks: int | None, local: bool) -> str | None:
        if local:
            return self.local_level
        if weeks is not None and self.until_weeks is not None and weeks >= self.until_weeks:
            return None
        if weeks is not None and self.late_weeks is not None and weeks >= self.late_weeks:
            return self.late_level
        return self.level


ACE_INHIBITORS = ("captopril", "enalapril", "lisinopril", "ramipril", "perindopril", "fosinopril", "quinapril",
                  "benazepril", "imidapril")  # fmt: skip
ARBS = ("losartan", "valsartan", "candesartan", "irbesartan", "telmisartan", "olmesartan", "azilsartan")
STATINS = ("atorvastatin", "rosuvastatin", "simvastatin", "pravastatin", "fluvastatin", "lovastatin", "pitavastatin")
TETRACYCLINES = ("doxycycline", "tetracycline", "oxytetracycline", "minocycline", "lymecycline")
QUINOLONES = ("ciprofloxacin", "levofloxacin", "moxifloxacin", "ofloxacin", "norfloxacin", "gemifloxacin")
NSAIDS = ("ibuprofen", "diclofenac", "naproxen", "ketoprofen", "dexketoprofen", "mefenamic", "celecoxib",
          "etoricoxib", "meloxicam", "piroxicam", "lornoxicam", "tenoxicam", "indomethacin", "ketorolac",
          "nimesulide", "aceclofenac", "flurbiprofen")  # fmt: skip
ASPIRIN = ("acetylsalicylic", "aspirin")
WARFARIN = ("warfarin", "acenocoumarol")
MACROLIDES_STRONG = ("clarithromycin", "erythromycin")  # the CYP3A4 inhibitors (azithromycin is not)
AZOLES = ("fluconazole", "itraconazole", "ketoconazole", "voriconazole", "miconazole")
SSRIS = ("fluoxetine", "sertraline", "paroxetine", "citalopram", "escitalopram", "fluvoxamine", "venlafaxine",
         "duloxetine", "desvenlafaxine")  # fmt: skip
NITRATES = ("nitroglycerin", "glyceryl trinitrate", "isosorbide")
PDE5 = ("sildenafil", "tadalafil", "vardenafil", "avanafil")
IRON = ("ferrous", "ferric", "iron", "carbonyl iron")
CALCIUM = ("calcium",)
ANTACID_METALS = ("magnesium", "aluminium", "aluminum", "zinc")
POTASSIUM_SPARING = ("spironolactone", "eplerenone", "amiloride", "potassium chloride", "potassium citrate")
ENZYME_INDUCERS = ("rifampicin", "rifampin", "carbamazepine", "phenytoin", "phenobarbital", "oxcarbazepine",
                   "topiramate", "st john")  # fmt: skip
ESTROGEN_CONTRACEPTIVES = ("ethinylestradiol", "ethinyl estradiol", "ethinyloestradiol", "estetrol")

PREGNANCY: tuple[Rule, ...] = (
    Rule("isotretinoin", ("isotretinoin", "acitretin", "alitretinoin"), AVOID,
         "بيسبب تشوهات خطيرة للجنين", local_level=CAUTION),
    Rule("tretinoin", ("tretinoin", "adapalene", "tazarotene"), CAUTION,
         "مشتقات فيتامين أ: الأفضل تتجنب في الحمل", local_level=CAUTION),
    Rule("warfarin", WARFARIN, AVOID, "بيسبب تشوهات ونزيف للجنين؛ البديل المعتاد هيبارين منخفض الوزن"),
    Rule("methotrexate", ("methotrexate",), AVOID, "بيسبب إجهاض وتشوهات"),
    Rule("misoprostol", ("misoprostol", "mifepristone"), AVOID,
         "بيعمل انقباضات وممكن إجهاض (إلا لو ده المقصود)"),
    Rule("ace_inhibitor", ACE_INHIBITORS, AVOID, "مثبطات ACE بتأذي كلى الجنين"),
    Rule("arb", ARBS, AVOID, "مضادات مستقبلات الأنجيوتنسين بتأذي كلى الجنين"),
    Rule("statin", STATINS, AVOID, "أدوية الكوليسترول ممنوعة في الحمل"),
    Rule("valproate", ("valpro", "divalproex"), AVOID, "بيسبب عيوب في الأنبوبة العصبية وتأخر في النمو"),
    Rule("antiepileptic", ("carbamazepine", "phenytoin", "topiramate", "phenobarbital"), CAUTION,
         "مرتبط بتشوهات؛ محتاج متابعة وحمض فوليك جرعة عالية"),
    Rule("lithium", ("lithium",), CAUTION, "مرتبط بعيوب في قلب الجنين"),
    Rule("tetracycline", TETRACYCLINES, AVOID, "بيأثر على أسنان وعضم الجنين", local_level=None),
    Rule("quinolone", QUINOLONES, CAUTION, "الأفضل تتجنب في الحمل لو فيه بديل"),
    Rule("aminoglycoside", ("gentamicin", "amikacin", "streptomycin", "tobramycin", "kanamycin"), CAUTION,
         "ممكن يأثر على سمع الجنين"),
    Rule("nsaid", NSAIDS, CAUTION, "مسكنات NSAIDs: الأفضل تتجنب في الحمل",
         late_weeks=20, late_level=AVOID),
    Rule("antithyroid", ("methimazole", "thiamazole", "carbimazole"), CAUTION,
         "في أول 3 شهور البروبيل ثيويوراسيل هو المفضل", until_weeks=16),
    Rule("amiodarone", ("amiodarone",), AVOID, "بيأثر على غدة الجنين الدرقية"),
    Rule("androgen", ("danazol", "testosterone"), AVOID, "بيسبب ذكورة للجنين الأنثى"),
    Rule("antiandrogen", ("finasteride", "dutasteride", "cyproterone", "spironolactone"), CAUTION,
         "مضاد للهرمونات الذكرية: ممكن يأثر على الجنين الذكر"),
    Rule("ovulation", ("clomiphene", "clomifene", "letrozole", "anastrozole"), AVOID,
         "منشطات التبويض / مثبطات الأروماتاز مش بتتاخد في الحمل"),
    Rule("immunosuppressant", ("mycophenolate", "leflunomide", "thalidomide", "lenalidomide"), AVOID,
         "بيسبب تشوهات"),
    Rule("ribavirin", ("ribavirin",), AVOID, "بيسبب تشوهات"),
    Rule("ergot", ("ergotamine", "dihydroergotamine", "methylergometrine", "methylergonovine", "ergometrine"), AVOID,
         "بيعمل انقباضات في الرحم قبل الولادة"),
    Rule("opioid", ("codeine", "tramadol"), CAUTION, "استخدام طويل قرب الولادة ممكن يعمل أعراض انسحاب للمولود"),
    Rule("fluconazole", ("fluconazole",), CAUTION, "الجرعات العالية أو الطويلة مرتبطة بتشوهات", local_level=None),
)  # fmt: skip

LACTATION: tuple[Rule, ...] = (
    Rule("methotrexate", ("methotrexate",), AVOID, "بيوصل للبن وبيأثر على الرضيع"),
    Rule("amiodarone", ("amiodarone",), AVOID, "بيتراكم في اللبن"),
    Rule("isotretinoin", ("isotretinoin", "acitretin"), AVOID, "مش آمن مع الرضاعة"),
    Rule("codeine", ("codeine",), AVOID, "ممكن يعمل هبوط في تنفس الرضيع"),
    Rule("tramadol", ("tramadol",), CAUTION, "ممكن يعمل نعاس للرضيع"),
    Rule("lithium", ("lithium",), CAUTION, "بيوصل للبن بنسبة عالية"),
    Rule("dopamine_agonist", ("cabergoline", "bromocriptine"), AVOID, "بيوقف اللبن (إلا لو ده المقصود)"),
    Rule("ergot", ("ergotamine",), AVOID, "مش آمن مع الرضاعة"),
    Rule("pseudoephedrine", ("pseudoephedrine",), CAUTION, "ممكن يقلل اللبن"),
    Rule("estrogen", ESTROGEN_CONTRACEPTIVES, CAUTION,
         "حبوب منع الحمل المركبة بتقلل اللبن؛ حبوب البروجستيرون بس هي الأنسب للمرضعة"),
)  # fmt: skip


@dataclass(frozen=True)
class Interaction:
    key: str
    a: tuple[str, ...]
    b: tuple[str, ...]
    level: str
    text: str  # "{a}" and "{b}" are the two medicines as written on the prescription
    local_counts: bool = False  # True: a cream / drop still interacts (rarely the case)
    exclude_b: tuple[str, ...] = field(default=())


INTERACTIONS: tuple[Interaction, ...] = (
    Interaction("warfarin_nsaid", WARFARIN, NSAIDS + ASPIRIN, AVOID, "{a} مع {b}: خطر نزيف"),
    Interaction("warfarin_inr", WARFARIN, MACROLIDES_STRONG + AZOLES + ("metronidazole", "sulfamethoxazole",
                "ciprofloxacin", "levofloxacin", "amiodarone"), CAUTION,
                "{b} بيرفع سيولة {a} (INR): محتاج متابعة"),
    Interaction("methotrexate_nsaid", ("methotrexate",), NSAIDS + ASPIRIN + ("trimethoprim", "sulfamethoxazole"),
                AVOID, "{b} بيزود سمية {a}"),
    Interaction("serotonin", SSRIS, ("tramadol", "sumatriptan", "rizatriptan", "zolmitriptan", "linezolid",
                "pethidine", "meperidine"), CAUTION, "{a} مع {b}: خطر متلازمة السيروتونين"),
    Interaction("nitrate_pde5", PDE5, NITRATES, AVOID, "{a} مع {b}: هبوط شديد في الضغط"),
    Interaction("statin_cyp3a4", ("simvastatin", "atorvastatin", "lovastatin"),
                (*MACROLIDES_STRONG, "itraconazole", "ketoconazole", "voriconazole"), AVOID,
                "{b} بيرفع مستوى {a}: خطر تكسير العضلات"),
    Interaction("raas_potassium", ACE_INHIBITORS + ARBS, POTASSIUM_SPARING, CAUTION,
                "{a} مع {b}: خطر ارتفاع البوتاسيوم"),
    Interaction("ace_arb", ACE_INHIBITORS, ARBS, AVOID, "{a} مع {b}: مش بيتاخدوا مع بعض"),
    Interaction("clopidogrel_ppi", ("clopidogrel",), ("omeprazole", "esomeprazole"), CAUTION,
                "{b} بيضعف مفعول {a}؛ البانتوبرازول أنسب"),
    Interaction("domperidone_qt", ("domperidone",), (*MACROLIDES_STRONG, "fluconazole", "ketoconazole",
                "itraconazole"), AVOID, "{a} مع {b}: خطر على كهربا القلب (QT)"),
    Interaction("contraceptive_inducer", (*ESTROGEN_CONTRACEPTIVES, "levonorgestrel", "desogestrel",
                "norethisterone", "drospirenone", "etonogestrel"), ENZYME_INDUCERS, CAUTION,
                "{b} بيضعف {a}: ممكن يحصل حمل، استخدمي وسيلة إضافية"),
    Interaction("digoxin", ("digoxin",), ("amiodarone", "clarithromycin", "verapamil"), CAUTION,
                "{b} بيرفع مستوى {a}"),
    Interaction("colchicine", ("colchicine",), MACROLIDES_STRONG, AVOID, "{b} بيزود سمية {a}"),
    Interaction("thyroxine_minerals", ("levothyroxine", "thyroxine"), IRON + CALCIUM + ANTACID_METALS, INFO,
                "{a} و{b}: سيبي بينهم 4 ساعات على الأقل"),
    Interaction("iron_calcium", IRON, CALCIUM, INFO, "{a} و{b}: سيبي بينهم ساعتين على الأقل"),
    Interaction("chelation", TETRACYCLINES + QUINOLONES, IRON + CALCIUM + ANTACID_METALS, INFO,
                "{a} و{b}: {a} قبلها بساعتين أو بعدها بـ4 ساعات"),
)  # fmt: skip
