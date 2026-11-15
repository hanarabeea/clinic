"""Design-system guards (docs/plan/14-obgyn-center.md §14.1): one icon family, no emoji as icons, no em dashes in the
interface. These read the template sources, so a slip shows up here and not on a screen nobody opened."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.template import Context, Template

TEMPLATES = settings.BASE_DIR / "templates"
ICONS = settings.BASE_DIR / "static" / "icons"
SCREENS = [
    p
    for p in sorted(TEMPLATES.rglob("*.html"))
    # printed documents and e-mails keep their own typography and may carry symbols patients read
    if "pdf" not in p.relative_to(TEMPLATES).parts and "emails" not in p.relative_to(TEMPLATES).parts
]
ICON_TAG = re.compile(r"""\{%\s*icon\s+["']([a-z0-9-]+)["']""")
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿⭐＋]")
COMMENTS = re.compile(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{#.*?#\}|^\s*//.*$", re.S | re.M)


def _visible(path: Path) -> str:
    return COMMENTS.sub("", path.read_text(encoding="utf-8"))


def test_every_icon_a_template_asks_for_is_vendored():
    missing = {}
    for path in SCREENS:
        for name in ICON_TAG.findall(path.read_text(encoding="utf-8")):
            if not (ICONS / f"{name}.svg").is_file():
                missing.setdefault(name, []).append(path.relative_to(TEMPLATES).as_posix())
    assert not missing, f"copy these from @phosphor-icons/core into static/icons/: {missing}"


def test_icon_tag_renders_inline_svg_and_refuses_unknown_names():
    html = Template('{% icon "check" "size-4 text-green-700" %}').render(Context())
    assert (
        html.startswith('<svg class="icon size-4 text-green-700"') and 'aria-hidden="true"' in html and "<path" in html
    )
    assert 'aria-label="تم"' in Template('{% icon "check" label="تم" %}').render(Context())
    with pytest.raises(Exception, match="not in static/icons"):
        Template('{% icon "no-such-icon" %}').render(Context())


@pytest.mark.parametrize("path", SCREENS, ids=lambda p: p.relative_to(TEMPLATES).as_posix())
def test_screens_use_icons_not_emoji_and_no_em_dashes(path):
    text = _visible(path)
    assert not EMOJI.search(text), f"emoji / symbol used as an icon: {sorted(set(EMOJI.findall(text)))}"
    assert "—" not in text, "em dash in interface copy: use a comma, a colon or two sentences"


def test_the_stylesheet_is_built_from_the_current_source():
    """static/css/app.css is committed: rebuilding it must not be forgotten after touching static/src/app.css."""
    built = (settings.BASE_DIR / "static" / "css" / "app.css").read_text(encoding="utf-8")
    for token in ("--color-ink-panel", ".stat-strip", ".pane-tab", ".data-table", ".overlay", ".segmented"):
        assert token in built, f"{token} missing from static/css/app.css: run the Tailwind build"
