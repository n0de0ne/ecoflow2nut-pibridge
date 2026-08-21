"""Optional Postgres telemetry logging for the EcoFlow NUT bridge.

A thin async wrapper around ``asyncpg`` that persists one sample row per poll and
serves down-sampled history to the web UI. ``asyncpg`` is an optional dependency
(``pip install ecoflow-nut-bridge[postgres]``); this module is imported lazily by
the daemon so the bridge still runs without it.

The schema is a single append-only table. History queries bucket rows with
Postgres 14+ ``date_bin`` and average each bucket, so a wide time range returns a
bounded number of points regardless of how many samples were stored.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from .config import PostgresConfig
from .state import DeviceState

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg

log = structlog.get_logger(__name__)

# Columns persisted per sample, in insert order. Kept as a module constant so the
# schema, the INSERT statement and the history SELECT stay in lock-step.
_METRIC_COLUMNS = (
    "soc_percent",
    "ac_input_watts",
    "ac_output_watts",
    "usb_output_watts",
    "usbc_output_watts",
    "input_watts",
    "output_watts",
    "solar_input_watts",
    "runtime_seconds",
)


def _ident(name: str) -> str:
    """Validate a SQL identifier (table name) we interpolate into DDL/queries.

    asyncpg parameters cannot stand in for identifiers, so the configurable table
    name is interpolated -- restrict it to a safe character set to keep that safe.
    """
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


class TelemetryStore:
    """Persists telemetry samples and serves history. Wraps an asyncpg pool."""

    def __init__(self, config: PostgresConfig) -> None:
        self._config = config
        self._table = _ident(config.table)
        self._pool: asyncpg.Pool | None = None
        self._last_write_monotonic = 0.0

    @property
    def connected(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        """Open the connection pool and ensure the schema exists."""
        import asyncpg  # lazy: optional dependency

        self._pool = await asyncpg.create_pool(self._config.dsn, min_size=1, max_size=4)
        await self._ensure_schema()
        log.info("db.connected", table=self._table)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ensure_schema(self) -> None:
        assert self._pool is not None
        await self._pool.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                ts                 timestamptz NOT NULL DEFAULT now(),
                device             text        NOT NULL,
                soc_percent        real,
                ac_input_watts     real,
                ac_output_watts    real,
                usb_output_watts   real,
                usbc_output_watts  real,
                input_watts        real,
                output_watts       real,
                runtime_seconds    integer,
                status             text,
                ac_input_present   boolean,
                ac_output_on       boolean,
                remain_charge_min  integer,
                remain_discharge_min integer,
                error_code         integer,
                solar_input_watts  real
            );
            CREATE INDEX IF NOT EXISTS {self._table}_device_ts_idx
                ON {self._table} (device, ts DESC);
            """)
        # CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so a
        # database from an older version needs metrics added after the fact.
        await self._pool.execute(
            f"ALTER TABLE {self._table} "
            "ADD COLUMN IF NOT EXISTS solar_input_watts real"
        )

    async def record(
        self,
        device: str,
        state: DeviceState,
        status: str,
        runtime_seconds: int,
    ) -> None:
        """Insert one sample, honouring the min-interval throttle."""
        if self._pool is None:
            return
        now = time.monotonic()
        if (
            self._config.min_interval_seconds
            and self._last_write_monotonic
            and now - self._last_write_monotonic < self._config.min_interval_seconds
        ):
            return
        self._last_write_monotonic = now
        try:
            await self._pool.execute(
                f"""
                INSERT INTO {self._table} (
                    device, soc_percent, ac_input_watts, ac_output_watts,
                    usb_output_watts, usbc_output_watts, input_watts, output_watts,
                    runtime_seconds, status, ac_input_present, ac_output_on,
                    remain_charge_min, remain_discharge_min, error_code,
                    solar_input_watts
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                    $15, $16
                )
                """,
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
            )
        except Exception as exc:  # noqa: BLE001 - logging must never crash the poll
            log.warning("db.record_failed", error=str(exc))

    async def history(
        self, device: str, minutes: int, max_points: int = 240
    ) -> list[dict[str, Any]]:
        """Return down-sampled averages over the last ``minutes`` for a device.

        Rows are bucketed so the result has at most ~``max_points`` points; each
        bucket reports the average of its metrics. Buckets are returned oldest
        first, which is what the dashboard chart expects.
        """
        if self._pool is None:
            return []
        minutes = max(1, int(minutes))
        max_points = max(1, int(max_points))
        # Bucket width in seconds, rounded up so we never exceed max_points.
        bucket_seconds = max(1, (minutes * 60 + max_points - 1) // max_points)
        averages = ", ".join(f"avg({col})::real AS {col}" for col in _METRIC_COLUMNS)
        rows = await self._pool.fetch(
            f"""
            SELECT
                date_bin(make_interval(secs => $2), ts, 'epoch') AS bucket,
                {averages}
            FROM {self._table}
            WHERE device = $1 AND ts >= now() - make_interval(mins => $3)
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            device,
            bucket_seconds,
            minutes,
        )
        return [
            {"ts": r["bucket"].isoformat(), **{c: r[c] for c in _METRIC_COLUMNS}}
            for r in rows
        ]

    async def energy_series(
        self, device: str, minutes: int, bucket_seconds: int
    ) -> list[dict[str, Any]]:
        """Average AC in/out watts per fixed-width bucket, for energy costing."""
        if self._pool is None:
            return []
        minutes = max(1, int(minutes))
        bucket_seconds = max(1, int(bucket_seconds))
        rows = await self._pool.fetch(
            f"""
            SELECT
                date_bin(make_interval(secs => $2), ts, 'epoch') AS bucket,
                avg(ac_input_watts)::real AS in_w,
                avg(ac_output_watts)::real AS out_w
            FROM {self._table}
            WHERE device = $1 AND ts >= now() - make_interval(mins => $3)
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            device,
            bucket_seconds,
            minutes,
        )
        return [
            {"ts": r["bucket"].isoformat(), "in_w": r["in_w"], "out_w": r["out_w"]}
            for r in rows
        ]

    async def prune(self, device: str) -> None:
        """Delete rows older than the configured retention window (if any)."""
        if self._pool is None or not self._config.retention_days:
            return
        try:
            await self._pool.execute(
                f"DELETE FROM {self._table} "
                "WHERE device = $1 AND ts < now() - make_interval(days => $2)",
                device,
                self._config.retention_days,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("db.prune_failed", error=str(exc))
