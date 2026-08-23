"""Tests for the local SQLite telemetry store (real on-disk database)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoflow_nut.config import SqliteConfig
from ecoflow_nut.db_sqlite import SqliteTelemetryStore
from ecoflow_nut.state import DeviceState


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


async def test_a_store_with_no_solar_column_data_reports_no_solar(
    tmp_path: Path,
) -> None:
    """End to end for the solar/grid split's unknown case.

    These rows carry AC input and nothing else, as a model that does not report
    PV would write them. avg() over the all-NULL column must arrive at the
    pricing code as None -- coerce it to 0.0 anywhere on the way and the UI
    claims "100% grid" for a station that simply cannot see its own panels.
    """
    from ecoflow_nut.config import PricingConfig
    from ecoflow_nut.pricing import compute_energy

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
                    500.0,
                )
                for i in range(5)
            ],
        )
        store._conn.commit()  # type: ignore[union-attr]
        rows = await store.energy_series("ecoflow", 0, 60, since=base, until=base + 300)

        assert all(r["solar_w"] is None for r in rows), "NULL must survive the query"
        money = compute_energy(rows, 60, PricingConfig(enabled=True))
        assert money["grid_kwh"] > 0, "the grid draw is real and still counted"
        assert money["solar_reported"] is False
        assert money["solar_share"] is None
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


async def test_min_interval_change_takes_effect_without_restart(tmp_path: Path) -> None:
    """The Settings page edits the live config object; the store must honour the
    new value on its next write rather than the one it was constructed with."""
    store = await _store(tmp_path, min_interval_seconds=3600)
    try:
        await store.record("ecoflow", _state(80, 100), "OB", 3600)
        await store.record("ecoflow", _state(70, 100), "OB", 3600)  # throttled
        store._config.min_interval_seconds = 0  # what a live edit does
        await store.record("ecoflow", _state(60, 100), "OB", 3600)
        count = store._conn.execute(  # type: ignore[union-attr]
            "SELECT count(*) AS n FROM ecoflow_samples"
        ).fetchone()["n"]
        assert count == 2
    finally:
        await store.close()


async def test_prune_deletes_only_rows_past_retention(tmp_path: Path) -> None:
    store = await _store(tmp_path, retention_days=7)
    try:
        now = time.time()
        _insert_at(store, "ecoflow", now - 30 * 86400, 10)  # older than retention
        _insert_at(store, "ecoflow", now - 1 * 86400, 20)  # inside retention
        await store.prune("ecoflow")
        rows = store._conn.execute(  # type: ignore[union-attr]
            "SELECT soc_percent FROM ecoflow_samples"
        ).fetchall()
        assert [r["soc_percent"] for r in rows] == [20]
    finally:
        await store.close()


async def test_prune_keeps_everything_when_retention_is_zero(tmp_path: Path) -> None:
    store = await _store(tmp_path, retention_days=0)
    try:
        _insert_at(store, "ecoflow", time.time() - 3650 * 86400, 10)
        await store.prune("ecoflow")
        count = store._conn.execute(  # type: ignore[union-attr]
            "SELECT count(*) AS n FROM ecoflow_samples"
        ).fetchone()["n"]
        assert count == 1
    finally:
        await store.close()


async def test_prune_leaves_other_devices_alone(tmp_path: Path) -> None:
    store = await _store(tmp_path, retention_days=7)
    try:
        _insert_at(store, "other-device", time.time() - 30 * 86400, 10)
        await store.prune("ecoflow")
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


async def test_solar_is_never_billed_as_grid_energy(tmp_path: Path) -> None:
    """Cost is metered on AC input alone, because the sun does not invoice.

    ``input_watts`` is the device's *total* intake -- mains plus PV plus the car
    port -- so costing against it would charge for every free watt harvested.
    Nothing pinned that choice before: it was correct only by construction, and
    a refactor reaching for the more obvious-sounding "input" column would have
    silently inflated every figure on the Energy page.
    """
    from ecoflow_nut.config import PricingConfig
    from ecoflow_nut.pricing import compute_energy

    store = await _store(tmp_path)
    try:
        # A sunny hour: 900 W of PV, nothing at all drawn from the wall.
        solar = DeviceState(
            soc_percent=80.0,
            ac_input_watts=0.0,
            solar_input_watts=900.0,
            input_watts=900.0,  # total intake, as the PD heartbeat reports it
            ac_output_watts=200.0,
            output_watts=200.0,
        )
        base = time.time() - 3600
        for i in range(6):
            await store.record("ecoflow", solar, "OL", 3600)
            store._conn.execute(  # type: ignore[union-attr]
                "UPDATE ecoflow_samples SET ts = datetime(?, 'unixepoch') "
                "WHERE rowid = (SELECT max(rowid) FROM ecoflow_samples)",
                (base + i * 600,),
            )
        store._conn.commit()  # type: ignore[union-attr]

        series = await store.energy_series("ecoflow", minutes=120, bucket_width=600)
        assert series, "the samples are there"
        assert all((row["in_w"] or 0.0) == 0.0 for row in series), (
            "the grid series must see none of the solar"
        )

        money = compute_energy(series, 600, PricingConfig(enabled=True, price_hp=0.25))
        assert money["grid_kwh"] == 0.0
        assert money["total_cost"] == 0.0
        assert money["peak_grid_watts"] == 0.0
    finally:
        await store.close()


async def test_mains_draw_is_still_billed(tmp_path: Path) -> None:
    """The other half: AC input must actually reach the bill."""
    from ecoflow_nut.config import PricingConfig
    from ecoflow_nut.pricing import compute_energy

    store = await _store(tmp_path)
    try:
        grid = DeviceState(
            soc_percent=50.0,
            ac_input_watts=600.0,
            solar_input_watts=0.0,
            input_watts=600.0,
            ac_output_watts=100.0,
        )
        base = time.time() - 3600
        for i in range(6):
            await store.record("ecoflow", grid, "OL", 3600)
            store._conn.execute(  # type: ignore[union-attr]
                "UPDATE ecoflow_samples SET ts = datetime(?, 'unixepoch') "
                "WHERE rowid = (SELECT max(rowid) FROM ecoflow_samples)",
                (base + i * 600,),
            )
        store._conn.commit()  # type: ignore[union-attr]

        series = await store.energy_series("ecoflow", minutes=120, bucket_width=600)
        # Both windows priced: the samples are stamped off the wall clock, so a
        # run between 22:00 and 06:00 puts every bucket in HC -- and with the
        # default 0/kWh there the bill was zero and this failed, nightly.
        pricing = PricingConfig(enabled=True, price_hc=0.15, price_hp=0.25)
        money = compute_energy(series, 600, pricing)
        assert money["grid_kwh"] == pytest.approx(0.6, abs=0.01), "600 W for an hour"
        assert money["total_cost"] > 0
    finally:
        await store.close()


async def test_solar_reaches_the_savings_figure(tmp_path: Path) -> None:
    """Harvest has to survive the round trip through the store to be worth anything.

    The costing query gained a solar column; without this, a future edit could
    drop it and the Energy page would quietly report zero saved forever, which
    reads as "solar is not helping" rather than as a bug.
    """
    from ecoflow_nut.config import PricingConfig
    from ecoflow_nut.pricing import compute_energy

    store = await _store(tmp_path)
    try:
        state = DeviceState(
            soc_percent=70.0,
            ac_input_watts=0.0,
            solar_input_watts=500.0,
            ac_output_watts=500.0,
        )
        base = time.time() - 3600
        for i in range(6):
            await store.record("ecoflow", state, "OL", 3600)
            store._conn.execute(  # type: ignore[union-attr]
                "UPDATE ecoflow_samples SET ts = datetime(?, 'unixepoch') "
                "WHERE rowid = (SELECT max(rowid) FROM ecoflow_samples)",
                (base + i * 600,),
            )
        store._conn.commit()  # type: ignore[union-attr]

        series = await store.energy_series("ecoflow", minutes=120, bucket_width=600)
        assert all(row["solar_w"] == pytest.approx(500.0) for row in series)

        # Priced in both windows, or an overnight run values the harvest at the
        # default 0/kWh and "the sun did the work" reads as zero.
        pricing = PricingConfig(enabled=True, price_hc=0.12, price_hp=0.20)
        money = compute_energy(series, 600, pricing)
        assert money["total_cost"] == 0.0, "nothing was bought"
        assert money["solar_savings"] > 0, "but the sun did the work"
        assert money["net_saving"] == pytest.approx(money["load_cost"])
    finally:
        await store.close()
