"""Tests for the local SQLite telemetry store (real on-disk database)."""

from __future__ import annotations

import asyncio
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


def test_missing_columns_are_added_to_an_existing_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS leaves an old table alone.

    Without a migration, a database created before a metric existed would fail
    every insert from then on.
    """
    import sqlite3

    path = tmp_path / "telemetry.db"
    # A table as an older version would have created it: no solar column.
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ecoflow_samples ("
        "ts TEXT NOT NULL DEFAULT (datetime('now')), device TEXT NOT NULL, "
        "soc_percent REAL)"
    )
    conn.commit()
    conn.close()

    store = SqliteTelemetryStore(SqliteConfig(enabled=True, path=str(path)))
    asyncio.run(store.connect())
    try:
        columns = {
            row[1]
            for row in store._conn.execute("PRAGMA table_info(ecoflow_samples)")  # type: ignore[union-attr]
        }
        assert "solar_input_watts" in columns
        # Every other later-added metric must be back too, not just the newest.
        assert {"ac_input_watts", "status", "error_code"} <= columns
    finally:
        asyncio.run(store.close())
