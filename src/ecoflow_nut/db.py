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
    "battery_watts",
    "dc_output_watts",
    "runtime_seconds",
)


def bucket_seconds(span_seconds: float, max_points: int) -> int:
    """Bucket width so a window of ``span_seconds`` yields <= ``max_points``.

    Shared by both backends (and by the web layer, which reports it to the
    browser so the chart can label its resolution) to keep the arithmetic in one
    place. Rounds up, so the point count is never exceeded.
    """
    span = max(1, int(span_seconds))
    points = max(1, int(max_points))
    return max(1, (span + points - 1) // points)


def resolve_window(
    minutes: int,
    since: float | None,
    until: float | None,
) -> tuple[float, float]:
    """Normalise a history request to an absolute ``(since, until)`` window.

    Callers may pass an explicit window (zoom/pan) or fall back to the legacy
    "last N minutes". ``until`` defaults to now, and ``since`` to ``minutes``
    before it.
    """
    resolved_until = float(until) if until is not None else time.time()
    resolved_since = (
        float(since) if since is not None else resolved_until - max(1, int(minutes)) * 60
    )
    return resolved_since, resolved_until


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
                solar_input_watts  real,
                battery_watts      real,
                dc_output_watts    real
            );
            CREATE INDEX IF NOT EXISTS {self._table}_device_ts_idx
                ON {self._table} (device, ts DESC);
            """)
        # CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so a
        # database from an older version needs metrics added after the fact.
        for column in ("solar_input_watts", "battery_watts", "dc_output_watts"):
            await self._pool.execute(
                f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS {column} real"
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
                    solar_input_watts, battery_watts, dc_output_watts
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                    $15, $16, $17, $18
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
                state.battery_watts,
                state.dc_output_watts,
            )
        except Exception as exc:  # noqa: BLE001 - logging must never crash the poll
            log.warning("db.record_failed", error=str(exc))

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
        by the chart's zoom and pan) or the last ``minutes``. Rows are bucketed so
        the result has at most ~``max_points`` points; each bucket reports the
        average of its metrics. Buckets are returned oldest first, which is what
        the dashboard chart expects.

        Buckets are anchored to the Unix epoch rather than to the window start, so
        panning does not resample onto shifting boundaries (which would make the
        line visibly shimmer as you drag).
        """
        if self._pool is None:
            return []
        start, end = resolve_window(minutes, since, until)
        bucket = bucket_seconds(end - start, max_points)
        averages = ", ".join(f"avg({col})::real AS {col}" for col in _METRIC_COLUMNS)
        rows = await self._pool.fetch(
            f"""
            SELECT
                date_bin(make_interval(secs => $2), ts, 'epoch') AS bucket,
                {averages}
            FROM {self._table}
            WHERE device = $1 AND ts >= to_timestamp($3) AND ts < to_timestamp($4)
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            device,
            bucket,
            start,
            end,
        )
        return [
            {"ts": r["bucket"].isoformat(), **{c: r[c] for c in _METRIC_COLUMNS}}
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
        if self._pool is None:
            return []
        start, end = resolve_window(minutes, since, until)
        bucket_width = max(1, int(bucket_width))
        rows = await self._pool.fetch(
            f"""
            SELECT
                date_bin(make_interval(secs => $2), ts, 'epoch') AS bucket,
                avg(ac_input_watts)::real AS in_w,
                avg(solar_input_watts)::real AS solar_w,
                -- The full draw, not just AC: the balance has to account for
                -- every port or the residual absorbs whatever was left out.
                (avg(ac_output_watts) + coalesce(avg(usb_output_watts), 0)
                 + coalesce(avg(usbc_output_watts), 0)
                 + coalesce(avg(dc_output_watts), 0))::real AS out_w,
                avg(battery_watts)::real AS bat_w
            FROM {self._table}
            WHERE device = $1 AND ts >= to_timestamp($3) AND ts < to_timestamp($4)
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            device,
            bucket_width,
            start,
            end,
        )
        return [
            {
                "ts": r["bucket"].isoformat(),
                "in_w": r["in_w"],
                "out_w": r["out_w"],
                "solar_w": r["solar_w"],
                "bat_w": r["bat_w"],
            }
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
