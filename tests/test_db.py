"""Unit tests for the Postgres telemetry store's pure logic.

These do not touch a real database -- they cover identifier validation and the
history bucket-width arithmetic, which are the parts worth pinning down without
an integration harness.
"""

from __future__ import annotations

import time

import pytest

from ecoflow_nut.config import PostgresConfig
from ecoflow_nut.db import (
    TelemetryStore,
    _ident,
    bucket_seconds,
    resolve_window,
)


def test_ident_accepts_safe_names() -> None:
    assert _ident("ecoflow_samples") == "ecoflow_samples"
    assert _ident("samples2") == "samples2"


@pytest.mark.parametrize("bad", ["drop;table", "a b", "tbl-1", "x'y", ""])
def test_ident_rejects_unsafe_names(bad: str) -> None:
    with pytest.raises(ValueError):
        _ident(bad)


def test_unsafe_table_name_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        TelemetryStore(PostgresConfig(table="bad; drop"))


async def test_record_and_history_noop_without_pool() -> None:
    """With no connection pool the store is inert, never raising."""
    store = TelemetryStore(PostgresConfig(table="ecoflow_samples"))
    assert store.connected is False
    assert await store.history("ecoflow", 60) == []
    assert await store.history("ecoflow", since=1, until=2) == []
    # record/prune must be safe no-ops when not connected.
    await store.prune("ecoflow")


def test_bucket_seconds_bounds_the_point_count() -> None:
    # An hour into 240 points is a 15 s bucket.
    assert bucket_seconds(3600, 240) == 15
    # Rounds up, so the cap is never exceeded.
    assert bucket_seconds(3601, 240) == 16
    assert 3600 / bucket_seconds(3600, 7) <= 7


def test_bucket_seconds_floors_at_one_second() -> None:
    """More requested points than seconds must not collapse to a zero width."""
    assert bucket_seconds(60, 240) == 1
    assert bucket_seconds(0, 240) == 1


def test_bucket_seconds_survives_degenerate_input() -> None:
    for points in (0, -1):
        assert bucket_seconds(3600, points) >= 1
    assert bucket_seconds(-10, 240) >= 1


def test_resolve_window_defaults_to_the_last_n_minutes() -> None:
    before = time.time()
    since, until = resolve_window(60, None, None)
    assert until >= before
    assert until - since == pytest.approx(3600, abs=1)


def test_resolve_window_prefers_an_explicit_window() -> None:
    assert resolve_window(60, 100.0, 700.0) == (100.0, 700.0)
    # A lone "until" still anchors the span to minutes.
    since, until = resolve_window(10, None, 1000.0)
    assert (since, until) == (400.0, 1000.0)
