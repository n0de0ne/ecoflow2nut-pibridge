"""Daemon: poll the power station over BLE and keep the NUT dummy-ups file fresh."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import statistics
import sys
import time
from collections import deque
from pathlib import Path

import structlog

# ``db`` is safe to import eagerly: asyncpg is only referenced under TYPE_CHECKING,
# so this pulls in the shared bucket arithmetic without the optional dependency.
from . import db, devices, pricing, settings_store
from .autoshutdown import AutoShutdownController, EveIdleConfirmer, ShutdownAction
from .ble_client import EcoFlowBLE
from .config import Config, load_config
from .devices import OUTPUT_KINDS, DeviceDriver
from .eve_outlet import EveOutlet
from .nut_writer import NutWriter, derive_status
from .settings_store import SettingsStore
from .state import DeviceState
from .switchbot import SwitchBot

log = structlog.get_logger(__name__)

# How long telemetry may go quiet before the watchdog intervenes.
WATCHDOG_TIMEOUT_SECONDS = 120
# How often the watchdog looks.
WATCHDOG_TICK_SECONDS = 5
# Consecutive in-process reconnects to try before handing the problem to the
# supervisor (systemd / Docker) for a restart from a clean state.
WATCHDOG_MAX_RECOVERIES = 3
# Ticks to wait after forcing a reconnect before judging whether it worked. A
# cold reconnect is a scan plus a handshake -- tens of seconds -- and tearing
# that down half-way would cause the very failure it is meant to repair.
WATCHDOG_RECOVERY_HOLD_TICKS = 18
# How often the retention window is applied. Deleting rows is cheap next to the
# cost of scanning for them, so there is no reason to do it often.
PRUNE_INTERVAL_SECONDS = 6 * 3600
# Samples the conversion-loss figure is taken over. It is a residual --
# a difference of larger numbers -- and the subsystems reporting those
# numbers do not tick together: the inverter sends about a third as often
# as the MPPT, so a load change momentarily pairs a fresh output reading
# with a stale input one and the raw residual jumps. A median over roughly
# ten seconds discards those without lagging a real change.
CONVERSION_SAMPLES = 21


def seed_state() -> DeviceState:
    """Placeholder telemetry used before the first BLE read (reads as ``OL``)."""
    return DeviceState(
        soc_percent=100, ac_input_present=True, ac_input_watts=100, ac_output_watts=0
    )


def parse_control_command(line: str) -> tuple[str, bool]:
    """Parse a control line like ``ac off`` into ``("ac", False)``.

    Raises ValueError on anything that is not ``<ac|usb|dc> <on|off>``.
    """
    parts = line.strip().lower().split()
    if len(parts) != 2 or parts[0] not in OUTPUT_KINDS or parts[1] not in ("on", "off"):
        raise ValueError("usage: <ac|usb|dc> <on|off>")
    return parts[0], parts[1] == "on"


def configure_logging(level: str, fmt: str) -> None:
    """Configure structlog for JSON or console output."""
    renderer = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )


class Daemon:
    """Owns the BLE connection lifecycle, the poll loop and the watchdog."""

    def __init__(self, config: Config) -> None:
        self._config = config
        # Overlay any persisted live-edited settings before building subsystems.
        self._settings = SettingsStore(config.settings_file)
        self._settings.load_into(config)
        # Protocol driver for the configured model; raises on an unknown model.
        self._driver: DeviceDriver = devices.get_driver(config.ecoflow.model)
        self._writer = NutWriter(config.nut)
        self._stop = asyncio.Event()
        self._last_write_monotonic = time.monotonic()
        self._autoshutdown = AutoShutdownController(config.auto_shutdown)
        self._active_client: EcoFlowBLE | None = None
        # Optional downstream HomeKit-over-BLE outlet (independent cut target).
        self._eve: EveOutlet | None = (
            EveOutlet(config.eve) if config.eve.enabled else None
        )
        # The Eve outlet's on/off state and draw. Seeded from what we commanded
        # and then corrected by _eve_poll, because the outlet has its own button
        # and its own app: a bridge that only remembers its own commands is
        # blank after a restart and stale the moment anyone else touches it.
        self._eve_state: bool | None = None
        self._eve_watts: float | None = None
        self._eve_poll_task: asyncio.Task[None] | None = None
        # Set while a cut is holding for the Eve's load to go idle, so the UI can
        # say so rather than claiming "CUT sent" when nothing has been cut yet.
        self._eve_confirm: EveIdleConfirmer | None = None
        self._eve_confirm_task: asyncio.Task[None] | None = None
        # Optional SwitchBot Bot (manual server power-button presser).
        self._switchbot: SwitchBot | None = (
            SwitchBot(config.switchbot) if config.switchbot.enabled else None
        )
        # One-shot startup reconciliation of the auto-shutdown cut state.
        self._reconciled = False
        # Latest published telemetry, surfaced to the optional web UI / DB logger.
        self._latest_state: DeviceState | None = None
        self._latest_status: str = "OB"
        self._latest_runtime: int = 0
        self._latest_update_monotonic: float = 0.0
        # What the BLE link is doing right now, for the dashboard. Without this
        # a four-second reconnect and a device that is genuinely gone both
        # render as "awaiting device", which is no way to tell them apart.
        self._link_state = "starting"
        self._link_error: str | None = None
        # Recent conversion-loss residuals, smoothed before publishing.
        self._conversion: deque[float] = deque(maxlen=CONVERSION_SAMPLES)
        # Optional subsystems (web UI / Postgres / MQTT), created in run().
        self._web: object | None = None
        self._store: object | None = None
        self._mqtt: object | None = None
        self._bg_tasks: set[asyncio.Task[None]] = set()

    def request_stop(self, *_: object) -> None:
        log.info("daemon.stop_requested")
        self._stop.set()

    async def run(self) -> int:
        # Seed the NUT file immediately so clients have something to read while
        # we establish the first BLE connection. Default optimistically to
        # "online" so clients do not briefly see "on battery" at startup.
        self._writer.write(seed_state())

        await self._start_store()
        await self._start_web()
        await self._start_mqtt()
        control = await self._start_control_server()
        watchdog = asyncio.create_task(self._watchdog())
        pruner = asyncio.create_task(self._prune_loop())
        if self._eve is not None and self._config.eve.poll_interval_seconds > 0:
            self._eve_poll_task = asyncio.create_task(self._eve_poll())
        try:
            backoff = 1.0
            while not self._stop.is_set():
                client = EcoFlowBLE(
                    self._config.ecoflow,
                    self._config.ble,
                    on_state=self._on_state,
                )
                try:
                    self._link_state = "connecting"
                    await client.connect()
                    self._link_state = "authenticating"
                    if not await client.wait_authenticated(timeout=30):
                        raise TimeoutError("authentication/first-read timed out")
                    log.info("daemon.connected")
                    self._link_state = "connected"
                    self._link_error = None
                    backoff = 1.0
                    self._active_client = client
                    await self._poll_loop(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._link_state = "error"
                    self._link_error = str(exc)
                    log.error(
                        "daemon.connection_error",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        error_repr=repr(exc),
                    )
                finally:
                    self._active_client = None
                    await client.disconnect()

                if self._stop.is_set():
                    break
                backoff = min(backoff * 2, self._config.ble.reconnect_backoff_max_seconds)
                if self._link_state != "error":
                    self._link_state = "reconnecting"
                log.info("daemon.reconnect_wait", seconds=round(backoff, 1))
                await self._sleep_or_stop(backoff)
        finally:
            watchdog.cancel()
            pruner.cancel()
            if self._eve_poll_task is not None:
                self._eve_poll_task.cancel()
            if control is not None:
                control.close()
            self._remove_control_socket()
            await self._stop_mqtt()
            await self._stop_web()
            await self._stop_store()
        return 0

    # -- optional subsystems: web UI + telemetry store ---------------------- #
    async def _start_store(self) -> None:
        """Bring up the telemetry store. Postgres wins if both are enabled."""
        pg = self._config.postgres
        lite = self._config.sqlite
        try:
            if pg.enabled:
                if not pg.dsn:
                    log.warning("db.disabled", reason="postgres.enabled but no dsn")
                    return
                if lite.enabled:
                    log.warning("db.both_enabled", note="using postgres, ignoring sqlite")
                from .db import TelemetryStore

                store: object = TelemetryStore(pg)
            elif lite.enabled:
                from .db_sqlite import SqliteTelemetryStore

                store = SqliteTelemetryStore(lite)
            else:
                return
            await store.connect()  # type: ignore[attr-defined]
            self._store = store
        except Exception as exc:  # noqa: BLE001 - DB must not prevent the bridge
            log.error("db.start_failed", error=str(exc), error_type=type(exc).__name__)
            self._store = None

    async def _start_mqtt(self) -> None:
        """Bring up the MQTT publisher. Never fatal: the broker is a consumer,
        not a dependency, so a bad host must not stop NUT being served."""
        cfg = self._config.mqtt
        if not cfg.enabled:
            return
        try:
            from .mqtt import MqttPublisher

            publisher = MqttPublisher(
                cfg,
                device_model=self._config.nut.static_values.model,
                device_serial=self._config.nut.static_values.serial,
                control=self.control_output,
            )
            await publisher.start()
            self._mqtt = publisher
        except Exception as exc:  # noqa: BLE001
            log.error("mqtt.start_failed", error=str(exc), error_type=type(exc).__name__)
            self._mqtt = None

    async def _stop_mqtt(self) -> None:
        if self._mqtt is not None:
            try:
                await self._mqtt.stop()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                log.warning("mqtt.stop_failed", error=str(exc))
            self._mqtt = None

    async def _stop_store(self) -> None:
        if self._store is not None:
            try:
                await self._store.close()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                log.debug("db.close_failed", error=str(exc))
            self._store = None

    async def _start_web(self) -> None:
        if not self._config.web.enabled:
            return
        try:
            from .webapp import WebServer

            web = WebServer(
                self._config.web,
                state_provider=self._web_state,
                control=self.control_output,
                history=self._web_history,
                autoshutdown_status=self._autoshutdown_status,
                get_settings=self._get_settings,
                update_settings=self._update_settings,
                energy=self._web_energy,
                history_enabled=self._store is not None,
                eve_control=self.control_eve if self._eve is not None else None,
                switchbot_press=(
                    self.control_switchbot if self._switchbot is not None else None
                ),
            )
            await web.start()
            self._web = web
        except Exception as exc:  # noqa: BLE001 - web UI must not prevent the bridge
            log.error("web.start_failed", error=str(exc), error_type=type(exc).__name__)
            self._web = None

    async def _stop_web(self) -> None:
        if self._web is not None:
            try:
                await self._web.stop()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                log.debug("web.stop_failed", error=str(exc))
            self._web = None

    def _web_state(self) -> dict[str, object]:
        """Current telemetry snapshot for the web UI's ``/api/state``."""
        s = self._latest_state
        age = (
            round(time.monotonic() - self._latest_update_monotonic, 1)
            if self._latest_update_monotonic
            else None
        )
        eve = (
            {
                "eve_enabled": True,
                "eve_on": self._eve_state,
                "eve_watts": self._eve_watts,
            }
            if self._eve is not None
            else {}
        )
        if self._switchbot is not None:
            eve = {**eve, "switchbot_enabled": True}
        # The browser plots server timestamps, so it needs this host's clock to
        # place "now" -- a phone's clock can be minutes off. It also paces its
        # polling off the device's cadence rather than guessing.
        common = {
            "server_time": time.time(),
            # How often a new reading actually appears. This is the number the
            # UI must pace itself against: poll_interval_seconds below is the
            # BLE *link watchdog* tick and says nothing about telemetry rate, so
            # pacing off it showed 2-second-old data on a 5-second refresh --
            # and would have looked outright dead to anyone who set the
            # watchdog to the minute-scale value its name invites.
            "publish_interval_seconds": self._config.nut.min_write_interval_seconds,
            "poll_interval_seconds": self._config.ecoflow.poll_interval_seconds,
            # The bridge drives several models, so the UI names the configured
            # one rather than hardcoding a device the user may not own.
            "device_model": self._config.nut.static_values.model,
            # Full scale for the charge/discharge meter. Without it the UI has
            # to guess, and a guess that suits a 2400 W station makes a 300 W
            # one look permanently idle.
            "rated_watts": self._config.nut.realpower_nominal,
            # So the UI can say what it is waiting for rather than just that it
            # is waiting.
            "link_state": self._link_state,
            "link_error": self._link_error,
        }
        if s is None:
            return {
                "status": self._latest_status,
                "updated_seconds_ago": age,
                **common,
                **eve,
            }
        return {
            **common,
            **eve,
            "soc_percent": s.soc_percent,
            "ac_input_watts": s.ac_input_watts,
            "ac_input_voltage": s.ac_input_voltage,
            "solar_input_watts": s.solar_input_watts,
            "battery_watts": s.battery_watts,
            "ac_output_watts": s.ac_output_watts,
            "usb_output_watts": s.usb_output_watts,
            "usbc_output_watts": s.usbc_output_watts,
            "input_watts": s.input_watts,
            "output_watts": s.output_watts,
            "dc_output_watts": s.dc_output_watts,
            "ac_input_present": s.ac_input_present,
            "ac_output_on": s.ac_output_on,
            "usb_output_on": s.usb_output_on,
            "dc_output_on": s.dc_output_on,
            "remain_charge_minutes": s.remain_charge_minutes,
            "remain_discharge_minutes": s.remain_discharge_minutes,
            "error_code": s.error_code,
            # Pack health and the limits the station enforces on itself. All
            # None on a model whose BMS stays quiet about them.
            "battery_temp_c": s.battery_temp_c,
            "cell_temp_max_c": s.cell_temp_max_c,
            "cell_temp_min_c": s.cell_temp_min_c,
            "cell_mv_max": s.cell_mv_max,
            "cell_mv_min": s.cell_mv_min,
            # The difference is the number worth watching -- it widens long
            # before capacity or SoH move -- so derive it once here rather than
            # in each of the dashboard, MQTT and anything else downstream.
            "cell_mv_spread": (
                s.cell_mv_max - s.cell_mv_min
                if s.cell_mv_max is not None and s.cell_mv_min is not None
                else None
            ),
            "battery_cycles": s.battery_cycles,
            "battery_soh_percent": s.battery_soh_percent,
            "battery_full_mah": s.battery_full_mah,
            "battery_design_mah": s.battery_design_mah,
            "inverter_temp_c": s.inverter_temp_c,
            "fan_on": s.fan_on,
            "charge_limit_percent": s.charge_limit_percent,
            "discharge_limit_percent": s.discharge_limit_percent,
            "ac_charge_watts": s.ac_charge_watts,
            "backup_reserve_percent": s.backup_reserve_percent,
            # Where the watts are going, including the ones nothing meters.
            **(s.power_balance() or {}),
            "conversion_watts": (
                round(statistics.median(self._conversion), 1)
                if self._conversion
                else None
            ),
            # Lifetime Wh odometers, straight from the station. These are what
            # Home Assistant's Energy dashboard consumes.
            "solar_energy_wh": s.solar_energy_wh,
            "grid_energy_wh": s.grid_energy_wh,
            "ac_output_energy_wh": s.ac_output_energy_wh,
            "dc_output_energy_wh": s.dc_output_energy_wh,
            "status": self._latest_status,
            "runtime_seconds": self._latest_runtime,
            "updated_seconds_ago": age,
        }

    async def _web_history(
        self, *, since: float, until: float, max_points: int
    ) -> dict[str, object]:
        """Down-sampled history over an absolute window, for the UI's chart."""
        if self._store is None:
            return {"points": [], "bucket_seconds": 0}
        points = await self._store.history(  # type: ignore[attr-defined]
            self._config.nut.device_name,
            max_points=max_points,
            since=since,
            until=until,
        )
        # Report the bucket width so the chart can label its resolution without
        # re-deriving the arithmetic in JS (which would drift).
        return {
            "points": points,
            "bucket_seconds": db.bucket_seconds(until - since, max_points),
        }

    async def _web_energy(self, *, since: float, until: float) -> dict[str, object]:
        """Energy + HC/HP cost summary over a window, for the UI."""
        if self._store is None:
            return {"enabled": False}
        span = max(1.0, until - since)
        # ~one bucket per minute, capped so very long ranges stay cheap. Keep the
        # 60 s floor: compute_energy classifies buckets by local time-of-day, so
        # sub-minute buckets add cost without adding accuracy.
        bucket_seconds = max(60, int(span // 2000) or 60)
        series = await self._store.energy_series(  # type: ignore[attr-defined]
            self._config.nut.device_name,
            0,
            bucket_seconds,
            since=since,
            until=until,
        )
        summary = pricing.compute_energy(series, bucket_seconds, self._config.pricing)
        summary["enabled"] = True
        summary["minutes"] = round(span / 60)
        summary["since"] = since
        summary["until"] = until
        summary["bucket_seconds"] = bucket_seconds
        return summary

    def _get_settings(self) -> dict[str, object]:
        """Editable-settings schema + current values for the UI form."""
        return {
            "fields": settings_store.schema(),
            "values": settings_store.current_values(self._config),
        }

    async def _update_settings(self, updates: dict[str, object]) -> dict[str, object]:
        """Validate + apply edited settings live, persist, and echo new values."""
        changed = settings_store.apply_updates(self._config, updates)  # raises ValueError
        if any(k.startswith("auto_shutdown.") for k in changed):
            # Rebuild the controller so the new policy (and its reset state) applies.
            self._autoshutdown = AutoShutdownController(self._config.auto_shutdown)
        if changed:
            self._settings.save(self._config)
            log.info("settings.updated", keys=changed)
        return {"values": settings_store.current_values(self._config), "changed": changed}

    def _autoshutdown_status(self) -> dict[str, object]:
        cfg = self._config.auto_shutdown
        return {
            "enabled": cfg.enabled,
            "armed": self._autoshutdown.armed,
            "triggered": self._autoshutdown.triggered,
            "seconds_until_cut": self._autoshutdown.seconds_until_cut(time.monotonic()),
            "trigger_soc_percent": cfg.trigger_soc_percent,
            "recover_soc_percent": cfg.recover_soc_percent,
            "grace_period_seconds": cfg.grace_period_seconds,
            "cut_outputs": self._cut_outputs(),
            # The controller latches `triggered` before any I/O, so while we are
            # holding for the outlet to go idle it already reads as cut. Say what
            # is actually happening instead.
            "awaiting_eve_idle": self._eve_confirm is not None,
            "eve_watts": (
                self._eve_confirm.last_watts if self._eve_confirm is not None else None
            ),
            "eve_confirm_idle_watts": cfg.eve_confirm_idle_watts,
        }

    # DeviceState flag each output kind reports, for the optimistic update below.
    _OUTPUT_FLAGS = {
        "ac": "ac_output_on",
        "usb": "usb_output_on",
        "dc": "dc_output_on",
    }

    async def control_output(self, kind: str, enabled: bool) -> str:
        """Send an output toggle over the live BLE connection. Raises on failure."""
        if kind not in OUTPUT_KINDS:
            raise ValueError(f"unknown output: {kind}")
        client = self._active_client
        if client is None or not client.is_connected:
            raise RuntimeError("not connected to device")
        await client.send_command_packet(self._driver.output_packet(kind, enabled))
        # Record what we just commanded. AC and 12V both report a switch flag,
        # so the next heartbeat overwrites this within a second or two and it
        # only buys the UI an instant response. USB is the case that needs it:
        # the DELTA 2 generation has no USB flag, the driver can only infer ON
        # from a live draw, and without this a bank switched off here would go
        # on reading ON until something else contradicted it -- which, at zero
        # watts, nothing ever would.
        if (flag := self._OUTPUT_FLAGS.get(kind)) and self._latest_state is not None:
            setattr(self._latest_state, flag, enabled)
        log.info("control.command", output=kind, enabled=enabled)
        return f"{kind} {'on' if enabled else 'off'}"

    async def control_eve(self, enabled: bool) -> str:
        """Toggle the downstream HomeKit outlet. Raises on failure.

        Cancels any auto-shutdown confirmation still waiting for the load to go
        idle: an explicit command from the CLI or the dashboard is the operator
        overruling the wait, and it must not queue behind it on the BLE lock.
        """
        if self._eve is None:
            raise RuntimeError("eve outlet is not enabled")
        await self._cancel_eve_confirm()
        await self._eve.set(enabled)
        self._eve_state = enabled
        # Whatever it was drawing is no longer true of the state we just moved
        # it into; the next poll fills it back in.
        self._eve_watts = None
        log.info("control.eve", enabled=enabled)
        return f"eve {'on' if enabled else 'off'}"

    async def _eve_poll(self) -> None:
        """Keep the Eve tile honest by reading the outlet, not our own memory.

        One connect/read/disconnect per pass, on the interval from config. Slow
        by default: the outlet is a convenience, not telemetry, and on a shared
        adapter every read costs EcoFlow airtime.

        Two things it deliberately does not do. It does not poll while an
        auto-shutdown confirmation is running -- that loop holds the outlet's
        BLE lock and may hold it indefinitely, so a queued read would achieve
        nothing but a stuck task. And a failed read leaves the last known state
        alone rather than blanking it: a missed poll means we did not look, not
        that the outlet stopped having a state.
        """
        interval = self._config.eve.poll_interval_seconds
        if self._eve is None or interval <= 0:
            return
        while not self._stop.is_set():
            if self._eve_confirm is None:
                try:
                    reading = await self._eve.read()
                    self._eve_state = reading.on
                    self._eve_watts = reading.watts
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the outlet is optional
                    log.warning("eve.poll_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    async def control_switchbot(self, action: str = "press") -> str:
        """Send a one-shot command to the SwitchBot Bot. Raises on failure."""
        if self._switchbot is None:
            raise RuntimeError("switchbot is not enabled")
        message = await self._switchbot.send(action)
        log.info("control.switchbot", action=action)
        return message

    def _record_sample(self, state: DeviceState, status: str, runtime: int) -> None:
        """Fire-and-forget a Postgres write (errors are swallowed in the store)."""
        store = self._store
        if store is None:
            return

        async def _write() -> None:
            await store.record(  # type: ignore[attr-defined]
                self._config.nut.device_name, state, status, runtime
            )

        task = asyncio.create_task(_write())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # -- control socket ----------------------------------------------------- #
    async def _start_control_server(self) -> asyncio.AbstractServer | None:
        path = self._config.control_socket_path
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._remove_control_socket()
            server = await asyncio.start_unix_server(self._handle_control, path=path)
            os.chmod(path, 0o660)
            log.info("control.listening", socket=path)
            return server
        except Exception as exc:  # noqa: BLE001
            log.warning("control.unavailable", socket=path, error=str(exc))
            return None

    def _remove_control_socket(self) -> None:
        try:
            os.unlink(self._config.control_socket_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.debug("control.unlink_failed", error=str(exc))

    async def _handle_control(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            response = await self._exec_control(raw.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            response = f"error: {exc}"
        try:
            writer.write((response + "\n").encode())
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def _exec_control(self, line: str) -> str:
        kind, enabled = parse_control_command(line)  # raises ValueError -> error
        try:
            return "ok: " + await self.control_output(kind, enabled)
        except RuntimeError as exc:
            return f"error: {exc}"

    async def _poll_loop(self, client: EcoFlowBLE) -> None:
        while not self._stop.is_set():
            if not client.is_connected:
                log.warning("daemon.poll_lost_connection")
                return
            # Re-read each cycle so a live poll-interval edit takes effect.
            await self._sleep_or_stop(self._config.ecoflow.poll_interval_seconds)

    def _on_state(self, state: DeviceState) -> None:
        if not state.is_complete:
            return
        now = time.monotonic()
        # Always keep the dashboard's snapshot live: it is just a reference.
        self._latest_state = state
        if (balance := state.power_balance()) is not None:
            self._conversion.append(balance["conversion_watts"])

        # Rewriting the NUT file on every frame pegged a Pi Zero near 80% CPU
        # against a DELTA 2 generation device, which streams several subsystem
        # heartbeats per second where the DELTA 3 sends one periodic frame --
        # dozens of atomic file rewrites a second, for data NUT re-reads every
        # two. (poll_interval_seconds is the BLE link watchdog, not this.)
        status = derive_status(state, self._config.nut)
        interval = max(0.0, self._config.nut.min_write_interval_seconds)
        due = now - self._last_write_monotonic >= interval
        # A status change is never delayed: an outage must reach NUT clients at
        # once, not up to an interval later.
        if not due and status == self._latest_status:
            return

        variables = self._writer.write(state)
        self._last_write_monotonic = now
        status = variables.get("ups.status", status)
        # Snapshot the latest values for the web UI and Postgres logger.
        self._latest_status = status
        self._latest_runtime = int(variables.get("battery.runtime", "0"))
        self._latest_update_monotonic = now
        # Push to any open dashboard before touching the store: the browser is
        # waiting on this, and a slow disk should not delay it. publish() never
        # blocks -- a client that cannot keep up drops frames instead.
        if self._web is not None and self._web.has_listeners:  # type: ignore[attr-defined]
            self._web.publish(self._web_state())  # type: ignore[attr-defined]
        if self._mqtt is not None:
            # Records the snapshot only; the publisher's own task does the
            # writing, so a slow broker never reaches the decode path.
            self._mqtt.publish(self._web_state())  # type: ignore[attr-defined]
        if self._store is not None:
            self._record_sample(state, status, self._latest_runtime)
        log.info(
            "state.updated",
            soc=state.soc_percent,
            ac_in=state.ac_input_watts,
            ac_out=state.ac_output_watts,
            status=status,
        )

        on_battery = status != "OL"
        if not self._reconciled:
            self._reconcile_shutdown_state(state.soc_percent, on_battery)
        action = self._autoshutdown.evaluate(
            time.monotonic(),
            state.soc_percent,
            on_battery,
            state.ac_output_watts,
        )
        if action is not ShutdownAction.NONE:
            self._handle_shutdown_action(action)

    def _reconcile_shutdown_state(self, soc: float | None, on_battery: bool) -> None:
        """One-shot startup check for a deep-discharge reboot.

        Auto-shutdown state lives in memory, so a full battery drain that
        power-cycles the host loses the fact that we had cut output. If we boot
        on line power but still below ``recover_soc_percent``, assume we are
        recovering from such a drain: hold the cut outputs off until SoC climbs
        back to the recover level (so protected gear isn't re-powered without a
        battery buffer to shut down again).
        """
        self._reconciled = True
        cfg = self._config.auto_shutdown
        if (
            cfg.enabled
            and cfg.restore_on_recovery
            and self._cut_outputs()
            and not on_battery
            and soc is not None
            and soc < cfg.recover_soc_percent
        ):
            log.warning(
                "auto_shutdown.hold_until_recovery",
                soc=soc,
                recover_soc=cfg.recover_soc_percent,
                outputs=self._cut_outputs(),
            )
            self._autoshutdown.force_triggered()
            self._send_outputs(enabled=False)

    def _handle_shutdown_action(self, action: ShutdownAction) -> None:
        cfg = self._config.auto_shutdown
        if action is ShutdownAction.ARMED:
            log.warning(
                "auto_shutdown.armed",
                trigger_soc=cfg.trigger_soc_percent,
                grace_seconds=cfg.grace_period_seconds,
            )
        elif action is ShutdownAction.DISARMED:
            log.info("auto_shutdown.disarmed")
        elif action is ShutdownAction.CUT:
            log.critical("auto_shutdown.cut", outputs=self._cut_outputs())
            self._send_outputs(enabled=False)
        elif action is ShutdownAction.RESTORE:
            log.warning("auto_shutdown.restore", outputs=self._cut_outputs())
            self._send_outputs(enabled=True)

    def _cut_outputs(self) -> list[str]:
        cfg = self._config.auto_shutdown
        names = []
        if cfg.cut_ac:
            names.append("ac")
        if cfg.cut_usb:
            names.append("usb")
        if cfg.cut_dc:
            names.append("dc")
        if cfg.cut_eve and self._eve is not None:
            names.append("eve")
        return names

    def _send_outputs(self, enabled: bool) -> None:
        cfg = self._config.auto_shutdown
        client = self._active_client
        packets = []
        if cfg.cut_ac:
            packets.append(self._driver.output_packet("ac", enabled))
        if cfg.cut_usb:
            packets.append(self._driver.output_packet("usb", enabled))
        if cfg.cut_dc:
            packets.append(self._driver.output_packet("dc", enabled))
        if packets and client is None:
            log.error("auto_shutdown.no_client", note="cannot send EcoFlow command")

        cut_eve = cfg.cut_eve and self._eve is not None
        # Only a cut waits: restoring power to an idle outlet needs no proof.
        confirm_first = not enabled and cut_eve and cfg.eve_confirm_idle_watts is not None

        async def _send_ecoflow() -> None:
            if client is None:
                return
            for packet in packets:
                try:
                    await client.send_command_packet(packet)
                except Exception as exc:  # noqa: BLE001
                    log.error("auto_shutdown.send_failed", error=str(exc))
                await asyncio.sleep(0.3)

        async def _send_eve() -> None:
            # The HomeKit outlet is an independent cut target on its own radio,
            # so drive it regardless of the EcoFlow link state.
            try:
                await self.control_eve(enabled)
            except Exception as exc:  # noqa: BLE001
                log.error("auto_shutdown.eve_failed", error=str(exc))

        async def _send() -> None:
            if confirm_first:
                # Cut the outlet FIRST when confirming. The Eve hangs off a
                # DELTA 3 socket, so cutting the AC bank first would kill it --
                # and the load behind it -- before the confirmation could mean
                # anything.
                await self._await_eve_idle()
                await _send_eve()
                await _send_ecoflow()
                return
            await _send_ecoflow()
            if cut_eve:
                await _send_eve()

        # Retained: a confirmation wait can run for a long time, and a task with
        # no live reference is garbage-collectable mid-flight.
        task = asyncio.create_task(_send())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        if confirm_first:
            self._eve_confirm_task = task

    async def _await_eve_idle(self) -> None:
        """Hold until the Eve outlet's own draw shows the load has powered down.

        Holds ONE connection for the whole wait: a cold call costs a scan, a BLE
        connect, a pair-verify and a full GATT read, which would be seconds of
        airtime per sample. On a read failure the session is dropped and reopened
        on the next pass, so a flaky link recovers instead of aborting.

        With no timeout configured this waits indefinitely -- deliberately, since
        cutting a server that is still writing is the thing we are avoiding. The
        dashboard's Eve Off button cancels this and cuts immediately, and
        ``eve_confirm_timeout_seconds`` bounds it for the unattended case.
        """
        cfg = self._config.auto_shutdown
        if self._eve is None or cfg.eve_confirm_idle_watts is None:
            return
        confirmer = EveIdleConfirmer(cfg, time.monotonic())
        self._eve_confirm = confirmer
        # Floored rather than allowed to be 0: a real BLE read takes longer than
        # this anyway, so the floor only stops a misconfigured 0 spinning hot.
        poll = max(0.05, float(cfg.eve_confirm_poll_seconds))
        log.warning(
            "auto_shutdown.eve_confirm_wait",
            idle_watts=cfg.eve_confirm_idle_watts,
            samples=cfg.eve_confirm_samples,
            timeout=cfg.eve_confirm_timeout_seconds,
        )
        try:
            while True:
                try:
                    async with self._eve.session() as eve:
                        while True:
                            reading = await eve.read()
                            if confirmer.observe(reading.watts):
                                log.critical(
                                    "auto_shutdown.eve_confirmed_idle",
                                    watts=reading.watts,
                                )
                                return
                            if confirmer.expired(time.monotonic()):
                                log.critical(
                                    "auto_shutdown.eve_confirm_timeout",
                                    watts=reading.watts,
                                    note="cutting anyway",
                                )
                                return
                            log.info(
                                "auto_shutdown.eve_still_drawing",
                                watts=reading.watts,
                                consecutive_idle=confirmer.consecutive,
                            )
                            await asyncio.sleep(poll)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # Unreadable is not idle: keep holding, and retry the link.
                    confirmer.observe(None)
                    log.error("auto_shutdown.eve_confirm_read_failed", error=str(exc))
                    if confirmer.expired(time.monotonic()):
                        log.critical(
                            "auto_shutdown.eve_confirm_timeout",
                            note="outlet unreadable; cutting anyway",
                        )
                        return
                    await asyncio.sleep(poll)
        finally:
            self._eve_confirm = None

    async def _cancel_eve_confirm(self) -> None:
        """Stop any in-flight confirmation so a manual command takes over."""
        task = self._eve_confirm_task
        self._eve_confirm_task = None
        if task is None or task.done():
            return
        log.warning("auto_shutdown.eve_confirm_cancelled", reason="manual override")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _prune_loop(self) -> None:
        """Apply the store's retention window at startup, then periodically.

        Prunes before sleeping so a restart is enough to reclaim space, and so a
        retention change made in the UI is not deferred by up to a full interval.
        Both stores no-op when ``retention_days`` is 0 and swallow their own
        errors, so this can never interrupt the NUT path.
        """
        while not self._stop.is_set():
            if self._store is not None:
                await self._store.prune(  # type: ignore[attr-defined]
                    self._config.nut.device_name
                )
            await self._sleep_or_stop(PRUNE_INTERVAL_SECONDS)

    async def _watchdog(self) -> None:
        """Recover a link that has gone quiet -- in-process first.

        A stalled BLE stream used to exit the process and let the supervisor
        start us over. That works, but it is the most expensive repair
        available: ten seconds of restart backoff, a cold scan, a fresh
        handshake, and every accumulated reading discarded -- to fix a fault
        that dropping and re-establishing the link fixes on its own. Worse, the
        restart races the device: a station that has just lost its one client
        needs a moment to advertise again, and a process that exits and
        immediately rescans tends to miss that window and report the device
        missing entirely.

        So reconnect first, and keep the hard exit for what reconnecting cannot
        fix -- a wedged adapter, where only a clean process (or a clean host)
        will do.
        """
        recoveries = 0
        hold = 0
        while not self._stop.is_set():
            await asyncio.sleep(WATCHDOG_TICK_SECONDS)
            stale = time.monotonic() - self._last_write_monotonic
            if stale <= WATCHDOG_TIMEOUT_SECONDS:
                # Telemetry is flowing; whatever we last did, it worked.
                recoveries = 0
                hold = 0
                continue
            if hold > 0:
                # A reconnect is in flight. Give it room.
                hold -= 1
                continue
            recoveries += 1
            if recoveries > WATCHDOG_MAX_RECOVERIES:
                log.critical(
                    "daemon.watchdog_timeout",
                    stale_seconds=round(stale),
                    recoveries=recoveries - 1,
                )
                self._stop.set()
                await self._release_link()
                # Hard-exit so the supervisor restarts us.
                sys.exit(70)
            log.warning(
                "daemon.watchdog_reconnect",
                stale_seconds=round(stale),
                attempt=recoveries,
                of=WATCHDOG_MAX_RECOVERIES,
            )
            # Dropping the link is the whole intervention: the poll loop sees
            # the connection go, returns, and the run loop reconnects.
            await self._release_link()
            hold = WATCHDOG_RECOVERY_HOLD_TICKS

    async def _release_link(self) -> None:
        """Tear the BLE link down, whether we are retrying or exiting.

        Dropping it without this -- and in particular exiting without it --
        leaves BlueZ holding the connection. The device, which accepts one
        client at a time, then believes it still has one, so it stops
        advertising and becomes invisible to every scanner on the host, not
        just to us. Nothing can reach it again until it is power-cycled, which
        is a miserable failure mode for a watchdog whose whole job is to
        recover unattended.
        """
        client = self._active_client
        if client is None:
            return
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
            log.info("daemon.link_released")
        except Exception as exc:  # noqa: BLE001 - recovering regardless
            log.warning("daemon.link_release_failed", error=str(exc))

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass


async def _amain(config: Config) -> int:
    daemon = Daemon(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:  # pragma: no cover - non-unix
            signal.signal(sig, daemon.request_stop)
    return await daemon.run()


def run_daemon(config_path: str) -> int:
    """Synchronous entry point used by the CLI ``run`` command."""
    config = load_config(config_path)
    configure_logging(config.logging.level, config.logging.format)
    log.info("daemon.starting", mac=config.ecoflow.mac, model=config.ecoflow.model)
    return asyncio.run(_amain(config))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_daemon(sys.argv[1] if len(sys.argv) > 1 else "config.yaml"))
