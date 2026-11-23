"""The Egyptian national ID (الرقم القومي, 14 digits), plan 15 §1.4.

    C YY MM DD GG SSS G K
    │ └──┬───┘ └┤ └┬┘ │ └ check digit (its formula is not published: not checked here)
    │    │      │  │  └── gender: odd = male, even = female
    │    │      │  └───── sequence
    │    │      └──────── governorate of birth registration (88 = born abroad)
    │    └─────────────── date of birth
    └──────────────────── century: 2 = 1900s, 3 = 2000s

Read from it: the date of birth, the gender and the governorate, so reception types one number instead of three
fields. A number whose date or governorate cannot exist is refused.
"""

from dataclasses import dataclass
from datetime import date

GOVERNORATES = {
    "01": "القاهرة", "02": "الإسكندرية", "03": "بورسعيد", "04": "السويس", "11": "دمياط", "12": "الدقهلية",
    "13": "الشرقية", "14": "القليوبية", "15": "كفر الشيخ", "16": "الغربية", "17": "المنوفية", "18": "البحيرة",
    "19": "الإسماعيلية", "21": "الجيزة", "22": "بني سويف", "23": "الفيوم", "24": "المنيا", "25": "أسيوط",
    "26": "سوهاج", "27": "قنا", "28": "أسوان", "29": "الأقصر", "31": "البحر الأحمر", "32": "الوادي الجديد",
    "33": "مطروح", "34": "شمال سيناء", "35": "جنوب سيناء", "88": "مواليد الخارج",
}  # fmt: skip
CENTURIES = {"2": 1900, "3": 2000}
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


class InvalidNationalId(ValueError):
    pass


@dataclass(frozen=True)
class NationalId:
    number: str
    birth_date: date
    gender: str  # "male" / "female" (patients.Gender values)
    governorate: str


def clean(raw: str) -> str:
    """Digits only (Arabic digits accepted, spaces and dashes dropped)."""
    return "".join(ch for ch in (raw or "").translate(_DIGITS) if ch.isdigit())


def parse(raw: str, *, today: date | None = None) -> NationalId:
    number = clean(raw)
    if len(number) != 14:
        raise InvalidNationalId("الرقم القومي لازم يكون 14 رقم.")
    century = CENTURIES.get(number[0])
    if century is None:
        raise InvalidNationalId("أول رقم في الرقم القومي لازم يكون 2 أو 3.")
    try:
        born = date(century + int(number[1:3]), int(number[3:5]), int(number[5:7]))
    except ValueError as e:
        raise InvalidNationalId("تاريخ الميلاد اللي في الرقم القومي مش صحيح.") from e
    if born > (today or date.today()):
        raise InvalidNationalId("تاريخ الميلاد اللي في الرقم القومي في المستقبل.")
    governorate = GOVERNORATES.get(number[7:9])
    if governorate is None:
        raise InvalidNationalId("كود المحافظة في الرقم القومي مش معروف.")
    gender = "male" if int(number[12]) % 2 else "female"
    return NationalId(number=number, birth_date=born, gender=gender, governorate=governorate)
