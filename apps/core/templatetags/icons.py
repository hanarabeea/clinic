"""`{% icon "name" %}`: inline SVG from the vendored Phosphor set in `static/icons/` (MIT, regular weight).

One icon family for the whole system; never hand-draw a path or use an emoji as an icon. To add an icon, copy its
SVG from @phosphor-icons/core `assets/regular/<name>.svg` (or `assets/fill/<name>-fill.svg`) into `static/icons/`.
`tests/test_design_system.py` fails when a template asks for an icon that isn't there.
"""

import re
from functools import lru_cache

from django import template
from django.conf import settings
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

ICON_DIR = settings.BASE_DIR / "static" / "icons"
_INNER = re.compile(r"<svg[^>]*>(.*)</svg>", re.S)


@lru_cache(maxsize=512)
def _inner(name):
    path = ICON_DIR / f"{name}.svg"
    if not path.is_file():
        return None
    m = _INNER.search(path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


@register.simple_tag
def icon(name, cls="size-5", label=""):
    inner = _inner(name)
    if inner is None:
        if settings.DEBUG or getattr(settings, "TESTING", False):
            raise template.TemplateSyntaxError(f"icon '{name}' is not in static/icons/")
        return ""
    a11y = format_html('role="img" aria-label="{}"', label) if label else mark_safe('aria-hidden="true"')
    return format_html(
        '<svg class="icon {}" viewBox="0 0 256 256" fill="currentColor" {}>{}</svg>',
        cls,
        a11y,
        mark_safe(inner),  # noqa: S308 - vendored file, not user input
    )
