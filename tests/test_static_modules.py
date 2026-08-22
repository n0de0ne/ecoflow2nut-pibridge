"""Static checks on the dashboard's ES modules.

The browser is the only thing that runs this code, so a broken import graph
ships silently: the module throws on load, the router never runs, and the page
renders as an empty shell with no error anywhere the bridge can see. That has
happened twice -- once from an import that did not exist, once from a call to a
function that was never imported -- so both are pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "ecoflow_nut" / "static"
MODULES = sorted(STATIC.glob("*.js"))


def _exports(text: str) -> set[str]:
    declaration = r"^export\s+(?:async\s+)?(?:const|let|var|function|class)\s+(\w+)"
    names = set(re.findall(declaration, text, re.M))
    for block in re.findall(r"^export\s*\{([^}]*)\}", text, re.M):
        names.update(
            part.split(" as ")[-1].strip() for part in block.split(",") if part.strip()
        )
    return names


def _imports(text: str) -> list[tuple[str, str]]:
    """(imported name, source module) for every named import."""
    out: list[tuple[str, str]] = []
    for block, target in re.findall(
        r"import\s*\{([^}]*)\}\s*from\s*[\"']\./([\w.]+)[\"']", text
    ):
        for part in block.split(","):
            name = part.strip().split(" as ")[0].strip()
            if name:
                out.append((name, target))
    return out


def _strip_noise(text: str) -> str:
    """Drop comments and string literals so identifier scans see only code."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", " ", text, flags=re.M)
    text = re.sub(r"`(?:[^`\\]|\\.)*`", '""', text, flags=re.S)
    text = re.sub(r"'(?:[^'\\\n]|\\.)*'", '""', text)
    return re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', text)


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_every_import_resolves(module: Path) -> None:
    """An import of a name the target does not export kills the whole module."""
    for name, target in _imports(module.read_text()):
        source = STATIC / target
        assert source.exists(), f"{module.name} imports from missing {target}"
        assert name in _exports(source.read_text()), (
            f"{module.name} imports '{name}' but {target} does not export it"
        )


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_nothing_uses_a_sibling_export_it_forgot_to_import(module: Path) -> None:
    """Calling an un-imported helper is a ReferenceError, not a missing feature.

    `openEventStream` called `getToken()` while importing only `setToken`. It
    threw on module load, so the router never ran and the dashboard rendered as
    an empty shell -- with the bridge perfectly healthy behind it.
    """
    text = module.read_text()
    imported = {name for name, _ in _imports(text)}
    declared = set(re.findall(r"(?:function|const|let|var|class)\s+(\w+)", text))
    used = set(re.findall(r"\b(\w+)\s*\(", _strip_noise(text)))

    # Anything another module exports, that this one calls but neither imports
    # nor declares itself.
    siblings: set[str] = set()
    for other in MODULES:
        if other != module:
            siblings |= _exports(other.read_text())

    missing = sorted((used & siblings) - imported - declared)
    assert not missing, f"{module.name} calls {missing} without importing them"


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_every_element_lookup_has_a_matching_id(module: Path) -> None:
    """`el("#thing")` on an id that does not exist returns null, silently.

    Nothing throws until something dereferences it, so a renamed or mistyped id
    shows up as one dead tile on a page that otherwise works -- or, when the
    caller does dereference it, as a TypeError that takes the whole render with
    it. Neither is visible from the bridge.
    """
    ids = set(re.findall(r'id="([\w-]+)"', (STATIC / "index.html").read_text()))
    referenced = set(re.findall(r'el\("#([\w-]+)"\)', module.read_text()))
    missing = sorted(referenced - ids)
    assert not missing, (
        f"{module.name} looks up ids that index.html does not define: {missing}"
    )


def test_a_dead_bms_field_does_not_pin_the_meter_to_zero() -> None:
    """The E2000 sends the pack watt fields as a constant zero.

    Trusting "present" rather than "non-zero" made the meter read 0 W while a
    105 W solar surplus was charging the battery and the station's own estimate
    said "full in 7h 41m" -- the one reading the meter exists to get right.
    """
    source = (STATIC / "views.js").read_text()
    assert "if (s.battery_watts)" in source, (
        "battery_watts must be trusted only when non-zero; `!= null` accepts a "
        "field the firmware never fills in"
    )
