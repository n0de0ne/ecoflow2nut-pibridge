"""Optional local SQLite telemetry logging for the EcoFlow NUT bridge.

A 100%-local store built on the Python standard library (``sqlite3``) -- no extra
dependency and no separate database server, so the whole bridge can run
self-contained on a Raspberry Pi. It mirrors :class:`ecoflow_nut.db.TelemetryStore`
(same method signatures) so the daemon can use either backend interchangeably.

``sqlite3`` is blocking, so every call runs in a worker thread via
``asyncio.to_thread`` and an :class:`asyncio.Lock` serialises access to the single
connection (kept ``check_same_thread=False`` for that reason).

Timestamps are stored in SQLite's own ``datetime('now')`` UTC text format
(``YYYY-MM-DD HH:MM:SS``) so range filters are plain lexicographic comparisons,
and history buckets are computed from ``strftime('%s', ts)`` (works on the older
SQLite shipped by Raspberry Pi OS, which lacks ``unixepoch()``).
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from .config import SqliteConfig
from .db import _METRIC_COLUMNS, _ident, bucket_seconds, resolve_window
from .state import DeviceState

log = structlog.get_logger(__name__)


def _window_bounds(since: float, until: float) -> tuple[int, int]:
    """Whole-second bounds for a window, widened so nothing is truncated inward.

    ``datetime(<int>, 'unixepoch')`` renders the same ``YYYY-MM-DD HH:MM:SS`` UTC
    shape as the column's ``datetime('now')`` default, so the comparison stays the
    plain lexicographic one this module relies on. (The ``unixepoch()`` *function*
    is deliberately avoided -- it needs SQLite 3.38, newer than Raspberry Pi OS
    ships; the modifier form used here is ancient.)
    """
    return math.floor(since), math.ceil(until)


# Per-sample columns with their SQLite affinity. Booleans are stored as 0/1.
_COLUMN_TYPES = {
    "soc_percent": "REAL",
    "ac_input_watts": "REAL",
    "ac_output_watts": "REAL",
    "usb_output_watts": "REAL",
    "usbc_output_watts": "REAL",
    "input_watts": "REAL",
    "output_watts": "REAL",
    "runtime_seconds": "INTEGER",
    "status": "TEXT",
    "ac_input_present": "INTEGER",
    "ac_output_on": "INTEGER",
    "remain_charge_min": "INTEGER",
    "remain_discharge_min": "INTEGER",
    "error_code": "INTEGER",
    # Appended deliberately: ALTER TABLE ADD COLUMN puts new columns last,
    # so appending here keeps a migrated database and a fresh one identical.
    "solar_input_watts": "REAL",
    # Needed to close the energy balance over a window: what the pack took or
    # gave, and the 12V draw. Rows written before these columns existed hold
    # NULL, which is what lets the Energy page say "not recorded for this
    # window" instead of silently costing the gap as conversion loss.
    "battery_watts": "REAL",
    "dc_output_watts": "REAL",
}


class SqliteTelemetryStore:
    """Persists telemetry samples to a local SQLite file and serves history."""

    def __init__(self, config: SqliteConfig) -> None:
        self._config = config
        self._table = _ident(config.table)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._last_write_monotonic = 0.0

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        """Open (and create) the database file and ensure the schema exists."""
        await asyncio.to_thread(self._connect_sync)
        log.info("db.connected", backend="sqlite", path=self._config.path)

    def _connect_sync(self) -> None:
        path = Path(self._config.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._config.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL + NORMAL: durable enough for telemetry, far less SD-card wear.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        columns = ",\n                ".join(
            f"{name} {affinity}" for name, affinity in _COLUMN_TYPES.items()
        )
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                ts     TEXT NOT NULL DEFAULT (datetime('now')),
                device TEXT NOT NULL,
                {columns}
            );
            CREATE INDEX IF NOT EXISTS {self._table}_device_ts_idx
                ON {self._table} (device, ts DESC);
            """)
        self._add_missing_columns(conn)
        conn.commit()
        self._conn = conn

    def _add_missing_columns(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after this database was created.

        CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so a
        database from an older version would silently keep failing every insert
        once a metric is added.
        """
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({self._table})")
        }
        for name, affinity in _COLUMN_TYPES.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {self._table} ADD COLUMN {name} {affinity}")
                log.info("db.column_added", column=name, backend="sqlite")

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            await asyncio.to_thread(conn.close)

    async def record(
        self,
        device: str,
        state: DeviceState,
        status: str,
        runtime_seconds: int,
    ) -> None:
        """Insert one sample, honouring the min-interval throttle."""
        if self._conn is None:
            return
        now = asyncio.get_running_loop().time()
        if (
            self._config.min_interval_seconds
            and self._last_write_monotonic
            and now - self._last_write_monotonic < self._config.min_interval_seconds
        ):
            return
        self._last_write_monotonic = now
        values = (
            device,
            state.soc_percent,
            state.ac_input_watts,
            state.ac_output_watts,
            state.usb_output_watts,
            state.usbc_output_watts,
            state.input_watts,
            state.output_watts,
            runtime_seconds,
            status,
            state.ac_input_present,
            state.ac_output_on,
            state.remain_charge_minutes,
            state.remain_discharge_minutes,
            state.error_code,
            state.solar_input_watts,
            state.battery_watts,
            state.dc_output_watts,
        )
        try:
            async with self._lock:
                await asyncio.to_thread(self._insert_sync, values)
        except Exception as exc:  # noqa: BLE001 - logging must never crash the poll
            log.warning("db.record_failed", error=str(exc))

    def _insert_sync(self, values: tuple[Any, ...]) -> None:
        assert self._conn is not None
        placeholders = ", ".join(["?"] * len(values))
        columns = "device, " + ", ".join(_COLUMN_TYPES)
        self._conn.execute(
            f"INSERT INTO {self._table} ({columns}) VALUES ({placeholders})", values
        )
        self._conn.commit()

    async def history(
        self,
        device: str,
        minutes: int = 60,
        max_points: int = 240,
        *,
        since: float | None = None,
        until: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return down-sampled averages over a time window for a device.

        The window is either explicit (``since``/``until`` as epoch seconds, used
        by the chart's zoom and pan) or the last ``minutes``. Buckets are anchored
        to the Unix epoch rather than to the window start, so panning does not
        resample onto shifting boundaries (which would make the line shimmer).
        """
        if self._conn is None:
            return []
        start, end = resolve_window(minutes, since, until)
        bucket = bucket_seconds(end - start, max_points)
        async with self._lock:
            rows = await asyncio.to_thread(self._history_sync, device, start, end, bucket)
        return rows

    def _history_sync(
        self, device: str, since: float, until: float, bucket: int
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        averages = ", ".join(f"avg({col}) AS {col}" for col in _METRIC_COLUMNS)
        sql = (
            "SELECT (CAST(strftime('%s', ts) AS INTEGER) / ?) * ? AS bucket, "
            f"{averages} FROM {self._table} "
            "WHERE device = ? "
            "  AND ts >= datetime(?, 'unixepoch') "
            "  AND ts <  datetime(?, 'unixepoch') "
            "GROUP BY bucket ORDER BY bucket ASC"
        )
        rows = self._conn.execute(
            sql, (bucket, bucket, device, *_window_bounds(since, until))
        ).fetchall()
        return [
            {
                "ts": datetime.fromtimestamp(int(r["bucket"]), tz=UTC).isoformat(),
                **{c: r[c] for c in _METRIC_COLUMNS},
            }
            for r in rows
        ]

    async def energy_series(
        self,
        device: str,
        minutes: int,
        bucket_width: int,
        *,
        since: float | None = None,
        until: float | None = None,
    ) -> list[dict[str, Any]]:
        """Average AC in/out watts per fixed-width bucket, for energy costing."""
        if self._conn is None:
            return []
        start, end = resolve_window(minutes, since, until)
        bucket_width = max(1, int(bucket_width))
        async with self._lock:
            return await asyncio.to_thread(
                self._energy_series_sync, device, start, end, bucket_width
            )

    def _energy_series_sync(
        self, device: str, since: float, until: float, bucket: int
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        sql = (
            "SELECT (CAST(strftime('%s', ts) AS INTEGER) / ?) * ? AS bucket, "
            "avg(ac_input_watts) AS in_w, avg(solar_input_watts) AS solar_w, "
            # The full draw, not just AC: the balance has to account for every
            # port or the residual absorbs whatever was left out.
            "avg(ac_output_watts) + coalesce(avg(usb_output_watts), 0) "
            "  + coalesce(avg(usbc_output_watts), 0) "
            "  + coalesce(avg(dc_output_watts), 0) AS out_w, "
            "avg(battery_watts) AS bat_w "
            f"FROM {self._table} "
            "WHERE device = ? "
            "  AND ts >= datetime(?, 'unixepoch') "
            "  AND ts <  datetime(?, 'unixepoch') "
            "GROUP BY bucket ORDER BY bucket ASC"
        )
        rows = self._conn.execute(
            sql, (bucket, bucket, device, *_window_bounds(since, until))
        ).fetchall()
        return [
            {
                "ts": datetime.fromtimestamp(int(r["bucket"]), tz=UTC).isoformat(),
                "in_w": r["in_w"],
                "out_w": r["out_w"],
                "solar_w": r["solar_w"],
                "bat_w": r["bat_w"],
            }
            for r in rows
        ]

    async def prune(self, device: str) -> None:
        """Delete rows older than the configured retention window (if any)."""
        if self._conn is None or not self._config.retention_days:
            return
        try:
            async with self._lock:
                await asyncio.to_thread(self._prune_sync, device)
        except Exception as exc:  # noqa: BLE001
            log.warning("db.prune_failed", error=str(exc))

    def _prune_sync(self, device: str) -> None:
        assert self._conn is not None
        self._conn.execute(
            f"DELETE FROM {self._table} " "WHERE device = ? AND ts < datetime('now', ?)",
            (device, f"-{self._config.retention_days} days"),
        )
        self._conn.commit()
