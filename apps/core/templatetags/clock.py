from datetime import datetime

from django import template

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
