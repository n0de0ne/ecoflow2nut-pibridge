"""Static checks on the dashboard's stylesheet.

CSS fails silently in a way Python does not: a token that is never defined makes
its property compute to nothing, and the browser paints on regardless. Nobody
sees an error -- the surface is just transparent, or the text is the wrong
colour, on whichever theme happened not to be looked at by eye.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "ecoflow_nut" / "static"
HTML = (STATIC / "index.html").read_text()

# Comments are stripped first, or a token named in prose ("heavier than
# --glass-edge: ...") reads as a declaration and swallows the real one after it.
CSS = re.sub(r"/\*.*?\*/", "", (STATIC / "app.css").read_text(), flags=re.S)


def _block(selector: str) -> str:
    """The body of the first rule whose selector matches, braces balanced."""
    start = CSS.index(selector)
    open_at = CSS.index("{", start)
    depth, i = 0, open_at
    while True:
        if CSS[i] == "{":
            depth += 1
        elif CSS[i] == "}":
            depth -= 1
            if depth == 0:
                return CSS[open_at + 1 : i]
        i += 1


def _tokens(block: str) -> dict[str, str]:
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)
    }


DARK = _tokens(_block(":root {"))
LIGHT_MEDIA = _tokens(_block(':root:not([data-theme="dark"])'))
LIGHT_PINNED = _tokens(_block(':root[data-theme="light"]'))
USED = set(re.findall(r"var\((--[\w-]+)", CSS))


def test_the_two_light_palettes_are_identical() -> None:
    """The light palette is written twice because an attribute selector cannot
    re-enter a media query. Nothing but this keeps the copies in step, and a
    token added to one and not the other silently falls back to the dark value
    for whichever half of the users came into light mode by the other route.
    """
    assert LIGHT_MEDIA == LIGHT_PINNED


def test_every_light_token_overrides_a_dark_one() -> None:
    """A token defined only in light mode has no dark value to fall back to."""
    missing = sorted(set(LIGHT_PINNED) - set(DARK))
    assert not missing, f"light-only tokens with no dark default: {missing}"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_token_the_stylesheet_reads_is_defined(theme: str) -> None:
    """`var(--typo)` computes to nothing and the browser paints on regardless."""
    defined = DARK if theme == "dark" else {**DARK, **LIGHT_PINNED}
    missing = sorted(USED - set(defined))
    assert not missing, f"{theme}: undefined tokens {missing}"


def test_no_token_is_defined_and_never_read() -> None:
    """Dead tokens are how a palette drifts out of step with what it paints."""
    # chart.js draws the canvas itself and reads its colours off the element,
    # and views.js names a token per series -- neither goes through var().
    from_js = set(
        re.findall(r'getPropertyValue\("(--[\w-]+)"\)', (STATIC / "chart.js").read_text())
    ) | set(re.findall(r'color:\s*"(--[\w-]+)"', (STATIC / "views.js").read_text()))
    unread = sorted(set(DARK) - USED - from_js)
    assert not unread, f"defined but never read: {unread}"


GLASS_SURFACES = ("appbar", "nav", "card", "save-bar", "toast", "chart-tip")


@pytest.mark.parametrize("surface", GLASS_SURFACES)
def test_every_glass_surface_has_an_opaque_fallback(surface: str) -> None:
    """Without backdrop-filter the tint alone is far too thin to read text on.

    Firefox ships the effect but lets users switch it off, and the page must not
    degrade to grey-on-grey when they do.
    """
    assert f".{surface}" in _block("@supports not ((backdrop-filter")


def test_the_glass_blur_is_prefixed_for_safari() -> None:
    """iOS Safari carried the effect for years under -webkit- only, and an
    iPhone is the device this layout is built for."""
    assert CSS.count("-webkit-backdrop-filter") >= CSS.count("\n  backdrop-filter")


def test_the_reserve_marker_is_clipped_to_the_battery_orb() -> None:
    """Its ends are the orb's full diameter, so at any level but half it would
    otherwise stick out either side of the circle."""
    clipped = re.search(r'<g clip-path="url\(#orbClip\)">(.*?)</g>', HTML, re.S)
    assert clipped is not None
    assert 'id="flowReserve"' in clipped.group(1)
