from datetime import datetime

from django import template
from django.utils import dateformat
from django.utils.html import format_html

from apps.core.timeutils import CAIRO

register = template.Library()


@register.filter
def ampm(value) -> str:
    """`2:30 PM` — the clinic's local time with AM / PM in Latin letters (what the reception screens show; patient
    messages keep `fmt_time`'s ص / م)."""
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        value = (value.astimezone(CAIRO) if value.tzinfo else value).time()
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


@register.filter
def day(value, arg="") -> str:
    """`4 أكتوبر 2026`: a day written with the month's name, so day and month can never be swapped (plan 15 §0.4).
    Wrapped in an RTL isolate, so it reads right even inside a `dir="ltr"` span. `|day:"short"` drops the year."""
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        value = value.astimezone(CAIRO) if value.tzinfo else value
    text = dateformat.format(value, "j F" if arg == "short" else "j F Y")
    return format_html('<bdi dir="rtl">{}</bdi>', text)
