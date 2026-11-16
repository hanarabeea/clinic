"""Pregnancy dating: pure date arithmetic, no database (docs/plan/14-obgyn-center.md §14.2).

One stored anchor, the expected date of delivery (EDD). Everything else is derived from it for any day:
gestational age = 280 days minus the days left to the EDD. So the three dating methods only differ in how the EDD is
found, and choosing another method moves every screen at once.

Dates only (no times, no time zones): a pregnancy is one day older when the calendar day changes in Cairo.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

TERM_DAYS = 280  # 40 weeks from the first day of the last period
CONCEPTION_TO_TERM_DAYS = 266  # 38 weeks from fertilisation (IVF dating)
STANDARD_CYCLE = 28
MAX_REASONABLE_DAYS = 44 * 7  # older than this: the dates are wrong or the pregnancy has ended

MONTH_LABELS = [
    gettext_lazy("الشهر الأول"),
    gettext_lazy("الشهر الثاني"),
    gettext_lazy("الشهر الثالث"),
    gettext_lazy("الشهر الرابع"),
    gettext_lazy("الشهر الخامس"),
    gettext_lazy("الشهر السادس"),
    gettext_lazy("الشهر السابع"),
    gettext_lazy("الشهر الثامن"),
    gettext_lazy("الشهر التاسع"),
]
TRIMESTER_LABELS = {
    1: gettext_lazy("الثلث الأول"),
    2: gettext_lazy("الثلث الثاني"),
    3: gettext_lazy("الثلث الثالث"),
}


def edd_from_lmp(lmp: date, cycle_days: int = STANDARD_CYCLE) -> date:
    """Naegele's rule, adjusted for a cycle that isn't 28 days (ovulation moves by the difference)."""
    return lmp + timedelta(days=TERM_DAYS + (cycle_days - STANDARD_CYCLE))


def edd_from_ultrasound(scan_date: date, weeks: int, days: int = 0) -> date:
    """The scan said `weeks`+`days` on `scan_date`: term is the rest of the 280 days after it."""
    return scan_date + timedelta(days=TERM_DAYS - (weeks * 7 + days))


def edd_from_transfer(transfer_date: date, embryo_age_days: int = 5) -> date:
    """IVF: 266 days from fertilisation; the embryo was already `embryo_age_days` old on the transfer day."""
    return transfer_date + timedelta(days=CONCEPTION_TO_TERM_DAYS - embryo_age_days)


def ga_days(edd: date, on: date) -> int:
    """Gestational age in days on `on` (can be negative before the last period, or past 280 after the EDD)."""
    return TERM_DAYS - (edd - on).days


def trimester(days: int) -> int:
    """1: up to 13 weeks + 6 days, 2: 14 to 27+6, 3: from 28 weeks."""
    if days < 14 * 7:
        return 1
    return 2 if days < 28 * 7 else 3


def month(days: int) -> int:
    """The month the pregnancy is *in* (1-9), the way patients count it: completed calendar months + 1, and the
    ninth month lasts until delivery. 24 weeks + 3 days is "the sixth month"."""
    return max(1, min(9, int(days // 30.4375) + 1))


def term_status(days: int) -> str:
    """preterm before 37 weeks, term 37 to 41+6, post-term from 42."""
    if days < 37 * 7:
        return "preterm"
    return "term" if days < 42 * 7 else "post_term"


def next_visit_weeks(days: int) -> int:
    """The usual antenatal rhythm: every 4 weeks to 28 weeks, every 2 weeks to 36, then weekly."""
    if days < 28 * 7:
        return 4
    return 2 if days < 36 * 7 else 1


def _days_ar(n: int) -> str:
    """Arabic counts: one and two have their own words, 3-10 take the plural, 11+ the singular."""
    if n == 1:
        return _("يوم")
    if n == 2:
        return _("يومين")
    if n <= 10:
        return _("%(n)s أيام") % {"n": n}
    return _("%(n)s يوم") % {"n": n}


@dataclass(frozen=True)
class GestationalAge:
    """Where a pregnancy stands on a given day, with the wording the screens use."""

    edd: date
    on: date
    days: int

    @property
    def weeks(self) -> int:
        return max(self.days, 0) // 7

    @property
    def extra_days(self) -> int:
        return max(self.days, 0) % 7

    @property
    def trimester(self) -> int:
        return trimester(self.days)

    @property
    def month(self) -> int:
        return month(self.days)

    @property
    def days_left(self) -> int:
        return (self.edd - self.on).days

    @property
    def term(self) -> str:
        return term_status(self.days)

    @property
    def overdue(self) -> bool:
        return self.days_left < 0

    @property
    def implausible(self) -> bool:
        """Before the last period or far past term: the dates need a second look."""
        return self.days < 0 or self.days > MAX_REASONABLE_DAYS

    @property
    def weeks_label(self) -> str:
        """ "24 أسبوع و3 أيام" / "24 أسبوع"."""
        if self.extra_days:
            return _("%(w)s أسبوع و%(d)s") % {"w": self.weeks, "d": _days_ar(self.extra_days)}
        return _("%(w)s أسبوع") % {"w": self.weeks}

    @property
    def short_label(self) -> str:
        """Compact, for a tag in a table row: "24+3"."""
        return f"{self.weeks}+{self.extra_days}"

    @property
    def month_label(self) -> str:
        return str(MONTH_LABELS[self.month - 1])

    @property
    def trimester_label(self) -> str:
        return str(TRIMESTER_LABELS[self.trimester])

    @property
    def left_label(self) -> str:
        if self.days_left > 0:
            return _("باقي %(d)s") % {"d": _days_ar(self.days_left)}
        if self.days_left == 0:
            return _("النهارده")
        return _("عدّى بـ%(d)s") % {"d": _days_ar(-self.days_left)}

    @property
    def next_visit_weeks(self) -> int:
        return next_visit_weeks(self.days)


def gestational_age(edd: date, on: date) -> GestationalAge:
    return GestationalAge(edd=edd, on=on, days=ga_days(edd, on))
