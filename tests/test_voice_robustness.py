"""Voice robustness against the real Egyptian catalog (25k products): everyday speech must never become a drug,
real names must be found even when misheard, and the common ways of saying a dose / duration must be understood.
Add a line here whenever a new misreading is reported."""

import pytest
from django.core.management import call_command

from apps.prescriptions import extraction
from apps.prescriptions.models import Drug

EVERYDAY_SAMPLE = (
    """
ازيك عامل ايه الحمد لله كويس تمام ماشي طيب حلو جميل معلش اسف شكرا متشكر عفوا اتفضل اقعد استريح
النهارده امبارح بكره بعدين دلوقتي لسه خلاص كده برضه كمان يعني مثلا طبعا اكيد ممكن لازم مفروض
البيت الشغل المدرسه الجامعه الشارع العربيه المكتب السوق المطعم المستشفى الصيدليه المعمل
الاكل الشرب المايه العصير الشاي القهوه اللبن العيش الرز الفراخ اللحمه السمك الخضار الفاكهه الحلويات السكريات
راسي بطني ضهري رجلي ايدي عيني ودني سناني صدري معدتي كتفي ركبتي رقبتي زوري مناخيري جلدي شعري
وجع الم صداع دوخه كحه زكام برد سخونيه حراره ترجيع اسهال امساك حموضه حرقان تعب ارهاق قلق توتر
كشف تحليل اشعه سونار رسم قلب ضغط سكر كوليسترول وزن طول نبض
ابني بنتي جوزي مراتي امي ابويا اخويا اختي جدتي جدي عمي خالي الولد البنت العيال
صباح مساء الصبح الضهر العصر المغرب العشا بالليل الفجر نص الليل
الاسبوع الشهر السنه يوم يومين ساعه دقيقه
تاني تالت اول اخر قبل بعد فوق تحت جنب قدام ورا
قوي خفيف شديد بسيط كتير قليل زياده ناقص احسن اوحش
روح تعالى خلي سيب شوف اسمع قول اكتب اقرا امسك حط شيل افتح اقفل
الحمام النوم الصحيان الرياضه المشي الجري السباحه
التلاجه الفريزر الدرج الشنطه الموبايل التليفون الساعه النضاره
موعد معاد حجز استشاره متابعه زياره روشته علاج دوا دوايا ادويه حبوب برشام
مرض حساسيه التهاب عدوى فيروس بكتيريا ميكروب جرثومه
ممتاز اوكي ياريت انشالله ربنا يخليك ربنا يشفيك سلامتك الف سلامه
بنزين عربيه مواصلات ميكروباص اتوبيس مترو تاكسي اوبر
فلوس حساب فاتوره كاش فيزا تأمين
""".split()
    + """
مستعجل مشغول تعبان زهقان مبسوط خايف متضايق نايم صاحي جعان عطشان شبعان
المطبخ الصاله الاوضه السرير الكنبه البلكونه السلم الاسانسير العماره الشقه
الكورنيش النادي المول الجامع الكنيسه البنك البوسطه المحكمه القسم
الامتحانات المذاكره الدروس الواجب المدرس الطلبه الفصل
الفطار الغدا العشاء السحور الفطور الساندوتش البيتزا الشوربه السلطه الكشري المحشي
البرتقال التفاح الموز العنب المانجه الفراوله البطيخ الجوافه الليمون
الطماطم الخيار البصل التوم الجزر الكوسه البامية الملوخيه السبانخ
الحر البرد الشتا الصيف المطر الهوا التراب الشمس
عرقان مرهق مصدع مدوخ مزكوم مكحكح سخن بارد
الضرس اللثه اللسان الحلق المريء القولون الكبد الكلى المثانه الرحم المبيض البروستاتا
الرئه الشعب الهوائيه الجيوب الانفيه الغده الدرقيه البنكرياس الطحال المرارة
الكورتيزون المضاد الحيوي المسكن الخافض المنوم المهدئ الفيتامين الحديد الكالسيوم
مستر مدام استاذه باشمهندس حاج حاجه شيخ
واحد اتنين تلاته اربعه خمسه سته سبعه تمانيه تسعه عشره
الوصفه الجرعه الكميه النوع الشكل اللون الطعم الريحه
المرتب الايجار الكهربا الغاز النت الشبكه الرصيد
فيسبوك واتساب تيك توك يوتيوب انستجرام
المسلسل الفيلم الماتش الكوره الاهلي الزمالك
""".split()
)
STILL_MEDICINES = {"الكورتيزون", "الفيتامين", "الكالسيوم"}  # said like this they ARE medicines

CASES = [  # (said after "بنادول", must-be-in-dose, must-be-in-duration)
    ("قرص كل 8 ساعات", "كل 8 ساعات", ""),
    ("قرص كل تمن ساعات", "كل 8 ساعات", ""),
    ("قرص كل ست ساعات", "كل 6 ساعات", ""),
    ("قرص كل اتناشر ساعه", "كل 12 ساعة", ""),
    ("قرص كل اربعه وعشرين ساعه", "كل 24 ساعة", ""),
    ("قرص مرتين في اليوم", "مرتين في اليوم", ""),
    ("قرص مرتين يوميا", "مرتين يوميًا", ""),
    ("قرص تلات مرات في اليوم", "3 مرات في اليوم", ""),
    ("قرص تلت مرات يوميا", "3 مرات يوميًا", ""),
    ("قرص مره واحده في اليوم", "مرة واحدة في اليوم", ""),
    ("قرص مره في اليوم", "مرة في اليوم", ""),
    ("قرص الصبح وبالليل", "الصبح وبالليل", ""),
    ("قرص الصبح والليل", "الصبح والليل", ""),
    ("قرص قبل النوم", "قبل النوم", ""),
    ("قرص عند اللزوم", "عند اللزوم", ""),
    ("قرص لما يحتاج", "عند اللزوم", ""),
    ("قرص لو فيه وجع", "عند اللزوم", ""),
    ("قرص بعد الاكل", "بعد الأكل", ""),
    ("قرص قبل الاكل بنص ساعه", "قبل الأكل بنص ساعة", ""),
    ("قرص على الريق", "على الريق", ""),
    ("قرص مع الاكل", "مع الأكل", ""),
    ("قرص بعد الفطار", "بعد الفطار", ""),
    ("قرص بعد الغدا", "بعد الغدا", ""),
    ("قرص بعد العشا", "بعد العشا", ""),
    ("نص قرص مرتين في اليوم", "½ قرص", ""),
    ("ربع قرص بالليل", "¼ قرص", ""),
    ("قرصين مرتين في اليوم", "قرصين", ""),
    ("2 قرص مرتين في اليوم", "2 قرص", ""),
    ("معلقه صغيره تلات مرات", "معلقة", ""),
    ("5 مل كل 8 ساعات", "5 مل", ""),
    ("خمسه سم كل تمن ساعات", "5 سم", ""),
    ("عشر نقط مرتين في اليوم", "10 نقط", ""),
    ("بخه في كل مناخير مرتين", "بخة", ""),
    ("كيس فوار مره في اليوم", "كيس", ""),
    ("لبوسه بالليل", "لبوسة", ""),
    ("حقنه في العضل مره واحده", "حقنة", ""),
    ("دهان طبقه رقيقه مرتين", "دهان", ""),
    ("قرص يوم بعد يوم", "يوم بعد يوم", ""),
    ("قرص مره في الاسبوع", "مرة في الاسبوع", ""),
    ("قرص كل يوم الصبح", "كل يوم الصبح", ""),
    ("قرص مرتين في اليوم لمده اسبوع", "مرتين", "أسبوع"),
    ("قرص مرتين لمده اسبوعين", "مرتين", "اسبوعين"),
    ("قرص مرتين لمده شهر", "مرتين", "شهر"),
    ("قرص مرتين لمده تلات شهور", "مرتين", "3 شهور"),
    ("قرص مرتين لمده عشر ايام", "مرتين", "10 أيام"),
    ("قرص مرتين لمده خمس ايام", "مرتين", "5 أيام"),
    ("قرص مرتين خمس ايام", "مرتين", "5 أيام"),
    ("قرص مرتين لمده يومين", "مرتين", "يومين"),
    ("قرص مرتين لمده تلات اسابيع", "مرتين", "3 اسابيع"),
    ("قرص مرتين لحد ما العلبه تخلص", "مرتين", "لحد ما"),
    ("قرص مرتين على طول", "مرتين", "على طول"),
    ("قرص مرتين باستمرار", "مرتين", "باستمرار"),
    ("قرص مرتين اسبوع كامل", "مرتين", "أسبوع كامل"),
    ("قرص مرتين لمده ٧ ايام", "مرتين", "7 أيام"),
    ("قرص مرتين لمدة 10 أيام", "مرتين", "10 أيام"),
]


def _drop_org(slug):
    from apps.organizations.models import Organization, OrganizationSettings
    from apps.prescriptions.models import DrugCategory

    for org in Organization.objects.filter(slug=slug):
        Drug.categories.through.objects.filter(drug__organization=org).delete()
        Drug.objects.filter(organization=org).delete()
        DrugCategory.objects.filter(organization=org).delete()
        OrganizationSettings.objects.filter(organization=org).delete()
        org.delete()


@pytest.fixture(scope="module")
def catalog(django_db_setup, django_db_blocker):
    """The full catalog, imported once for this file (committed, removed at the end — and any leftover from an
    interrupted earlier run is removed first, since the test DB is reused)."""
    import io

    from apps.organizations.models import Organization, OrganizationSettings

    with django_db_blocker.unblock():
        _drop_org("voice-robustness")
        org = Organization.objects.create(slug="voice-robustness", name_ar="فحص الصوت")
        OrganizationSettings.objects.create(organization=org)
        call_command("import_egyptian_drugs", "--org", org.slug, stdout=io.StringIO())
        Drug.objects.create(organization=org, name="Panadol 500", aliases_ar=["بنادول"])
        extraction._index(org)  # build the voice index once
    yield org
    with django_db_blocker.unblock():
        _drop_org("voice-robustness")


def test_everyday_words_never_become_drugs(catalog, db):
    dose = " مرتين في اليوم بعد الاكل لمده اسبوع"
    bad = [w for w in EVERYDAY_SAMPLE if w not in STILL_MEDICINES and extraction.extract(catalog, w + dose)]
    assert bad == []


def test_real_names_found_even_misheard(catalog, db):
    picks = list(
        Drug.objects.filter(organization=catalog, source="eg-db", categories__isnull=False)
        .exclude(aliases_ar=[])
        .order_by("pk")
        .distinct()
        .values_list("name", "aliases_ar")[:400:4]
    )
    swap = str.maketrans({"ر": "ل", "ل": "ر"})
    total = hits = 0
    for name, aliases in picks:
        alias = aliases[0].replace("-", " ").split()[0]
        if len(alias) < 4:
            continue
        brand = name.split()[0].split("-")[0].upper()
        for said in (alias, "ال" + alias, "و" + alias, alias.translate(swap)):
            res = extraction.extract(catalog, f"تمام هكتبلك {said} قرص مرتين في اليوم لمده اسبوع")
            total += 1
            hits += bool(res) and any(c.drug.name.upper().startswith(brand) for c in res[0].candidates)
    assert hits / total >= 0.93, f"{hits}/{total}"


@pytest.mark.parametrize(("said", "dose", "duration"), CASES)
def test_dose_and_duration_phrasings(catalog, db, said, dose, duration):
    [item] = extraction.extract(catalog, "خدي بنادول " + said)
    assert dose in item.instructions
    assert duration in item.duration


FORM_WORDS = {
    "DROPS": "قطره", "SYRUP": "شراب", "CREAM": "كريم", "OINT": "مرهم", "AMP": "حقنه", "SUPP": "لبوس",
    "SPRAY": "بخاخ", "SACHET": "كيس", "LOTION": "لوشن", "SHAMPOO": "شامبو", "GEL": "جل", "CAPS": "كبسوله",
}  # fmt: skip


def test_form_word_next_to_the_name(catalog, db):
    """'قطره بلنك' / 'لوشن ماش': a form word next to a (short or slightly misheard) name finds it."""
    total = hits = 0
    for key, word in FORM_WORDS.items():
        picks = (
            Drug.objects.filter(organization=catalog, source="eg-db", name__icontains=key)
            .exclude(aliases_ar=[])
            .order_by("pk")
            .values_list("name", "aliases_ar")[:15]
        )
        for name, aliases in picks:
            said = aliases[0].replace("-", " ").split()[0]
            brand = name.replace("-", " ").replace(".", " ").split()[0].upper()
            for text in (f"بص خدي {word} {said}", f"خدي {said} {word} مرتين في اليوم"):
                res = extraction.extract(catalog, text)
                total += 1
                hits += bool(res) and any(c.drug.name.upper().startswith(brand) for c in res[0].candidates)
    assert hits / total >= 0.95, f"{hits}/{total}"


def test_form_word_with_everyday_word_is_not_a_drug(catalog, db):
    bad = [
        f"{fw} {w}"
        for fw in FORM_WORDS.values()
        for w in EVERYDAY_SAMPLE[::6]
        if w not in STILL_MEDICINES and extraction.extract(catalog, f"{fw} {w}")
    ]
    assert bad == []


@pytest.mark.parametrize(
    ("said", "brand", "in_dose"),
    [
        ("قطره بلنك نقطتين في كل عين 3 مرات", "BLINK", "في كل عين"),
        ("كريم فيوسيدين على المكان مرتين في اليوم", "FUCIDIN", "المكان"),
        ("حقنه ديكلاك في العضل مره واحده", "DICLAC", "في العضل"),
        ("لبوس فولتارين شرجي بالليل", "VOLTAREN", "شرجي بالليل"),
        ("بخاخ اوتريفين في كل فتحه مرتين", "OTRIVIN", "في كل فتحه"),
        ("نقط اوتال في الودن 3 مرات", "OTAL", "في الودن"),
        ("شراب سيتال معلقه كل 6 ساعات", "CETAL", "معلقة"),
        ("خدي panadol اكسترا مرتين", "PANADOL EXTRA", "مرتين"),
    ],
)  # fmt: skip
def test_route_time_and_form(catalog, db, said, brand, in_dose):
    [item] = extraction.extract(catalog, said)
    assert item.candidates[0].drug.name.upper().startswith(brand), item.candidates[0].drug.name
    assert in_dose in item.instructions


def test_said_form_picks_the_product_form(catalog, db):
    [item] = extraction.extract(catalog, "شراب سيتال معلقه كل 6 ساعات")
    assert any(f in item.candidates[0].drug.name.upper() for f in ("SYRUP", "SUSP"))


def test_dose_word_before_the_name_is_kept(catalog, db):
    [item] = extraction.extract(catalog, "حبايه جلوكوفاج الصبح")
    assert item.instructions == "حباية الصبح"


@pytest.mark.parametrize(
    ("said", "must_have"),
    [
        ("كيس فوار باراسيتامول لما يحتاج", ("EFF", "SACHET")),
        ("فوار باراسيتامول", ("EFF",)),
        ("كيس باراسيتامول مرتين", ("SACHET",)),
        ("شراب باراسيتامول كل 6 ساعات", ("SYRUP", "SUSP")),
        ("لبوس باراسيتامول بالليل", ("SUPP",)),
        ("كيس فوار سيفامول", ("EFF", "SACHET")),
        ("اقراص فوار سيفامول", ("EFF", "TAB")),
    ],
)  # fmt: skip
def test_said_form_with_an_ingredient_name(catalog, db, said, must_have):
    """No product named like the ingredient comes in the said form → same active ingredient in that form first."""
    [item] = extraction.extract(catalog, said)
    top = item.candidates[0].drug.name.upper()
    assert all(f in top for f in must_have) or any(f in top for f in must_have[1:] or must_have), top


@pytest.mark.parametrize("said", ["اريستابكس اربعين قرص الصبح", "اريستا بيكس قرص الصبح", "ارستابكس 40 الصبح"])
def test_name_heard_with_other_vowels_or_split(catalog, db, said):
    """ERASTAPEX ("اراستابيكس") said "اريستابكس", or split by the recogniser ("اريستا بيكس")."""
    [item] = extraction.extract(catalog, said)
    assert item.candidates[0].drug.name.startswith("ERASTAPEX"), item.candidates[0].drug.name
