"""The watt axis's arithmetic, run in a real JS engine.

Everything else about the dashboard is checked by reading its source as text,
which is enough for "does this id exist" but not for "does this number come out
right". The watt scale is arithmetic with edge cases -- an axis that has to open
a negative half for the battery series while keeping zero on a gridline -- so it
is executed rather than read.

Skipped where node is not installed: the bridge itself never runs node, and a Pi
does not have to have it just to run the test suite.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "ecoflow_nut" / "static"
CHART = STATIC / "chart.js"
DIVISIONS = 4  # must match the constant in chart.js


def _scale(cases: list[tuple[float, float]]) -> list[dict[str, float]]:
    """Run wattScale(trough, peak) for each case in node, and return the results."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        f"import {{ wattScale }} from {json.dumps(CHART.as_uri())};\n"
        f"const cases = {json.dumps(cases)};\n"
        "console.log(JSON.stringify(cases.map(([t, p]) => wattScale(t, p))));"
    )
    out = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)


def test_an_all_positive_window_keeps_the_axis_on_the_baseline() -> None:
    """Nothing negative in view means no negative half, and no wasted plot."""
    for scale in _scale([(0, 187), (0, 251), (0, 9), (0, 1800)]):
        assert scale["min"] == 0
        assert scale["max"] > 0


def test_a_negative_reading_opens_the_axis_below_zero() -> None:
    """A discharging pack reads -80 W; clipped to the baseline it reads 0 W.

    That is not a cosmetic difference: flat-at-zero is what the series looks
    like when the pack is idle, so clipping turns "carrying the whole load" into
    "doing nothing".
    """
    (scale,) = _scale([(-80, 251)])
    assert scale["min"] <= -80
    assert scale["max"] >= 251


def test_every_reading_in_view_fits_inside_the_scale() -> None:
    cases = [
        (0, 10), (0, 400), (-150, 400), (-350, 50), (-2000, 2400),
        (-1, 1), (0, 0), (-0.5, 0.5), (0, 12345),
    ]
    for (trough, peak), scale in zip(cases, _scale(cases), strict=True):
        assert scale["min"] <= min(trough, 0), (trough, peak, scale)
        assert scale["max"] >= max(peak, 0), (trough, peak, scale)


def test_zero_always_lands_on_a_gridline() -> None:
    """The gridlines are the only thing marking which side of zero a line is on.

    They are drawn at fixed fractions of the plot (they double as the percentage
    axis), so zero has to fall on one of them or the sign of the battery series
    is guesswork.
    """
    cases = [(-80, 251), (-150, 400), (-350, 50), (-7, 33), (-2000, 2400)]
    for scale in _scale(cases):
        step = (scale["max"] - scale["min"]) / DIVISIONS
        assert scale["min"] % step == 0, scale


def test_the_labels_are_round_numbers() -> None:
    """Every gridline is labelled, and `Math.round` on 2.5 W steps prints 3 W."""
    cases = [(0, 10), (0, 187), (-80, 251), (-150, 400), (0, 7)]
    for scale in _scale(cases):
        step = (scale["max"] - scale["min"]) / DIVISIONS
        assert step == int(step), scale
