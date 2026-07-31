"""Tests for the local SQLite telemetry store (real on-disk database)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoflow_nut.config import SqliteConfig
from ecoflow_nut.db_sqlite import SqliteTelemetryStore
from ecoflow_nut.delta3 import DeviceState


def _state(soc: float, ac_out: float) -> DeviceState:
    return DeviceState(
        soc_percent=soc,
        ac_input_watts=0.0,
        ac_output_watts=ac_out,
        usb_output_watts=2.0,
        usbc_output_watts=0.0,
        input_watts=0.0,
        output_watts=ac_out,
        ac_input_present=False,
        ac_output_on=True,
        remain_charge_minutes=0,
        remain_discharge_minutes=120,
        error_code=0,
    )


async def _store(tmp_path: Path, **kw: object) -> SqliteTelemetryStore:
    config = SqliteConfig(
        enabled=True, path=str(tmp_path / "telemetry.db"), **kw  # type: ignore[arg-type]
    )
    store = SqliteTelemetryStore(config)
    await store.connect()
    return store


async def test_connect_creates_file_and_parent(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "telemetry.db"
    store = SqliteTelemetryStore(SqliteConfig(enabled=True, path=str(db_path)))
    await store.connect()
    try:
        assert store.connected is True
        assert db_path.exists()
    finally:
        await store.close()
    assert store.connected is False


async def test_record_then_history_returns_points(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        await store.record("ecoflow", _state(80, 100), "OB", 3600)
        await store.record("ecoflow", _state(60, 140), "OB LB", 1800)
        points = await store.history("ecoflow", minutes=60)
        assert points, "expected at least one bucket"
        # Averages cover the metric columns and a timestamp is present.
        assert "ts" in points[0]
        assert "soc_percent" in points[0]
        socs = [p["soc_percent"] for p in points if p["soc_percent"] is not None]
        assert socs and all(60 <= v <= 80 for v in socs)
    finally:
        await store.close()


def _insert_at(store: SqliteTelemetryStore, device: str, at: float, soc: float) -> None:
    """Insert a row with an explicit timestamp.

    ``record()`` relies on the column's ``datetime('now')`` default, so every row
    it writes lands in the same second and cannot exercise bucketing or windowing.
    """
    when = datetime.fromtimestamp(at, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    store._conn.execute(  # type: ignore[union-attr]
        "INSERT INTO ecoflow_samples (ts, device, soc_percent) VALUES (?, ?, ?)",
        (when, device, soc),
    )
    store._conn.commit()  # type: ignore[union-attr]


async def test_history_window_selects_only_rows_inside(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        base = 1_700_000_000
        for i in range(10):
            _insert_at(store, "ecoflow", base + i * 60, 50 + i)
        points = await store.history(
            "ecoflow", since=base + 120, until=base + 300, max_points=100
        )
        socs = [p["soc_percent"] for p in points]
        # Rows at +120, +180, +240 are inside; +300 is the exclusive upper bound.
        assert socs == [52, 53, 54]
    finally:
        await store.close()


async def test_history_window_is_half_open(tmp_path: Path) -> None:
    """A sample exactly at ``until`` belongs to the next window, not this one."""
    store = await _store(tmp_path)
    try:
        base = 1_700_000_000
        _insert_at(store, "ecoflow", base, 10)
        _insert_at(store, "ecoflow", base + 60, 20)
        points = await store.history(
            "ecoflow", since=base, until=base + 60, max_points=100
        )
        assert [p["soc_percent"] for p in points] == [10]
    finally:
        await store.close()


async def test_history_buckets_are_epoch_aligned(tmp_path: Path) -> None:
    """Bucket boundaries must not move with the window, or panning shimmers."""
    store = await _store(tmp_path)
    try:
        base = 1_700_000_000
        for i in range(20):
            _insert_at(store, "ecoflow", base + i * 30, 50 + i)
        # Two windows offset from one another must agree on shared boundaries.
        a = await store.history("ecoflow", since=base, until=base + 600, max_points=10)
        b = await store.history(
            "ecoflow", since=base + 137, until=base + 737, max_points=10
        )
        bucket = 60
        for points in (a, b):
            for p in points:
                epoch = datetime.fromisoformat(p["ts"]).timestamp()
                assert epoch % bucket == 0
        # Interior buckets must agree exactly. (The first and last bucket of any
        # window are clipped by its edges, so they legitimately hold fewer
        # samples -- only whole buckets are comparable.)
        interior = {p["ts"]: p["soc_percent"] for p in a[1:-1]}
        overlapping = [p for p in b[1:-1] if p["ts"] in interior]
        assert overlapping, "expected the two windows to share interior buckets"
        for p in overlapping:
            assert p["soc_percent"] == interior[p["ts"]]
    finally:
        await store.close()


async def test_history_averages_within_a_bucket(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        base = 1_700_000_000
        _insert_at(store, "ecoflow", base + 1, 40)
        _insert_at(store, "ecoflow", base + 2, 60)
        points = await store.history("ecoflow", since=base, until=base + 60, max_points=1)
        assert [p["soc_percent"] for p in points] == [50]
    finally:
        await store.close()


async def test_history_empty_window_returns_no_points(tmp_path: Path) -> None:
    """Live mode keeps the right edge just ahead of now; that is not an error."""
    store = await _store(tmp_path)
    try:
        await store.record("ecoflow", _state(50, 100), "OB", 3600)
        future = time.time() + 3600
        assert await store.history("ecoflow", since=future, until=future + 60) == []
    finally:
        await store.close()


async def test_history_max_points_clamped(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        base = 1_700_000_000
        _insert_at(store, "ecoflow", base, 50)
        for bad in (0, -5):
            points = await store.history(
                "ecoflow", since=base, until=base + 600, max_points=bad
            )
            assert len(points) >= 1
    finally:
        await store.close()


async def test_energy_series_window(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        base = 1_700_000_000
        store._conn.executemany(  # type: ignore[union-attr]
            "INSERT INTO ecoflow_samples (ts, device, ac_input_watts) "
            "VALUES (?, 'ecoflow', ?)",
            [
                (
                    datetime.fromtimestamp(base + i * 60, tz=UTC).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    100.0 * i,
                )
                for i in range(5)
            ],
        )
        store._conn.commit()  # type: ignore[union-attr]
        rows = await store.energy_series("ecoflow", 0, 60, since=base, until=base + 180)
        assert [r["in_w"] for r in rows] == [0.0, 100.0, 200.0]
    finally:
        await store.close()


async def test_history_filters_by_device(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        await store.record("ecoflow", _state(50, 100), "OB", 3600)
        assert await store.history("other-device", minutes=60) == []
    finally:
        await store.close()


async def test_min_interval_throttles_writes(tmp_path: Path) -> None:
    store = await _store(tmp_path, min_interval_seconds=3600)
    try:
        await store.record("ecoflow", _state(80, 100), "OB", 3600)
        await store.record("ecoflow", _state(70, 100), "OB", 3600)  # throttled
        count = store._conn.execute(  # type: ignore[union-attr]
            "SELECT count(*) AS n FROM ecoflow_samples"
        ).fetchone()["n"]
        assert count == 1
    finally:
        await store.close()


async def test_noop_before_connect() -> None:
    store = SqliteTelemetryStore(SqliteConfig(enabled=True, path=":memory:"))
    assert store.connected is False
    assert await store.history("ecoflow", 60) == []
    await store.record("ecoflow", _state(50, 100), "OB", 1)  # must not raise
    await store.prune("ecoflow")


@pytest.mark.parametrize("bad", ["bad; drop", "a b"])
def test_unsafe_table_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        SqliteTelemetryStore(SqliteConfig(table=bad))
