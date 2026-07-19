"""Daemon: poll the DELTA 3 over BLE and keep the NUT dummy-ups file fresh."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

import structlog

from . import delta3, pricing, settings_store
from .autoshutdown import AutoShutdownController, ShutdownAction
from .ble_client import EcoFlowBLE
from .config import Config, load_config
from .delta3 import DeviceState
from .eve_outlet import EveOutlet
from .nut_writer import NutWriter
from .settings_store import SettingsStore
from .switchbot import SwitchBot

log = structlog.get_logger(__name__)

# If no successful BLE read happens within this window, exit so the supervisor
# (systemd / Docker) restarts us from a clean state.
WATCHDOG_TIMEOUT_SECONDS = 120


def seed_state() -> DeviceState:
    """Placeholder telemetry used before the first BLE read (reads as ``OL``)."""
    return DeviceState(
        soc_percent=100, ac_input_present=True, ac_input_watts=100, ac_output_watts=0
    )


# Output command builders shared by the control socket and the auto-shutdown path.
OUTPUT_BUILDERS = {
    "ac": delta3.set_ac_enabled_packet,
    "usb": delta3.set_usb_enabled_packet,
    "dc": delta3.set_dc_enabled_packet,
}


def parse_control_command(line: str) -> tuple[str, bool]:
    """Parse a control line like ``ac off`` into ``("ac", False)``.

    Raises ValueError on anything that is not ``<ac|usb|dc> <on|off>``.
    """
    parts = line.strip().lower().split()
    if (
        len(parts) != 2
        or parts[0] not in OUTPUT_BUILDERS
        or parts[1] not in ("on", "off")
    ):
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
        self._writer = NutWriter(config.nut)
        self._stop = asyncio.Event()
        self._last_write_monotonic = time.monotonic()
        self._autoshutdown = AutoShutdownController(config.auto_shutdown)
        self._active_client: EcoFlowBLE | None = None
        # Optional downstream HomeKit-over-BLE outlet (independent cut target).
        self._eve: EveOutlet | None = (
            EveOutlet(config.eve) if config.eve.enabled else None
        )
        # Last on/off state we commanded the Eve outlet into (the outlet is not
        # polled to avoid extra BLE traffic; None == unknown until first command).
        self._eve_state: bool | None = None
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
        # Optional subsystems (web UI / Postgres), created in run() when enabled.
        self._web: object | None = None
        self._store: object | None = None
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
        control = await self._start_control_server()
        watchdog = asyncio.create_task(self._watchdog())
        try:
            backoff = 1.0
            while not self._stop.is_set():
                client = EcoFlowBLE(
                    self._config.ecoflow,
                    self._config.ble,
                    on_state=self._on_state,
                )
                try:
                    await client.connect()
                    if not await client.wait_authenticated(timeout=30):
                        raise TimeoutError("authentication/first-read timed out")
                    log.info("daemon.connected")
                    backoff = 1.0
                    self._active_client = client
                    await self._poll_loop(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
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
                log.info("daemon.reconnect_wait", seconds=round(backoff, 1))
                await self._sleep_or_stop(backoff)
        finally:
            watchdog.cancel()
            if control is not None:
                control.close()
            self._remove_control_socket()
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
            {"eve_enabled": True, "eve_on": self._eve_state}
            if self._eve is not None
            else {}
        )
        if self._switchbot is not None:
            eve = {**eve, "switchbot_enabled": True}
        if s is None:
            return {
                "status": self._latest_status,
                "updated_seconds_ago": age,
                **eve,
            }
        return {
            **eve,
            "soc_percent": s.soc_percent,
            "ac_input_watts": s.ac_input_watts,
            "ac_output_watts": s.ac_output_watts,
            "usb_output_watts": s.usb_output_watts,
            "usbc_output_watts": s.usbc_output_watts,
            "input_watts": s.input_watts,
            "output_watts": s.output_watts,
            "ac_input_present": s.ac_input_present,
            "ac_output_on": s.ac_output_on,
            "remain_charge_minutes": s.remain_charge_minutes,
            "remain_discharge_minutes": s.remain_discharge_minutes,
            "error_code": s.error_code,
            "status": self._latest_status,
            "runtime_seconds": self._latest_runtime,
            "updated_seconds_ago": age,
        }

    async def _web_history(self, minutes: int) -> list[dict[str, object]]:
        if self._store is None:
            return []
        return await self._store.history(  # type: ignore[attr-defined]
            self._config.nut.device_name, minutes
        )

    async def _web_energy(self, minutes: int) -> dict[str, object]:
        """Energy + HC/HP cost summary over the last ``minutes`` for the UI."""
        if self._store is None:
            return {"enabled": False}
        minutes = max(1, int(minutes))
        # ~one bucket per minute, capped so very long ranges stay cheap.
        bucket_seconds = max(60, (minutes * 60) // 2000 * 1 or 60)
        series = await self._store.energy_series(  # type: ignore[attr-defined]
            self._config.nut.device_name, minutes, bucket_seconds
        )
        summary = pricing.compute_energy(series, bucket_seconds, self._config.pricing)
        summary["enabled"] = True
        summary["minutes"] = minutes
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
        }

    async def control_output(self, kind: str, enabled: bool) -> str:
        """Send an output toggle over the live BLE connection. Raises on failure."""
        if kind not in OUTPUT_BUILDERS:
            raise ValueError(f"unknown output: {kind}")
        client = self._active_client
        if client is None or not client.is_connected:
            raise RuntimeError("not connected to device")
        await client.send_command_packet(OUTPUT_BUILDERS[kind](enabled))
        log.info("control.command", output=kind, enabled=enabled)
        return f"{kind} {'on' if enabled else 'off'}"

    async def control_eve(self, enabled: bool) -> str:
        """Toggle the downstream HomeKit outlet. Raises on failure."""
        if self._eve is None:
            raise RuntimeError("eve outlet is not enabled")
        await self._eve.set(enabled)
        self._eve_state = enabled
        log.info("control.eve", enabled=enabled)
        return f"eve {'on' if enabled else 'off'}"

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
        variables = self._writer.write(state)
        now = time.monotonic()
        self._last_write_monotonic = now
        status = variables.get("ups.status", "OB")
        # Snapshot the latest values for the web UI and Postgres logger.
        self._latest_state = state
        self._latest_status = status
        self._latest_runtime = int(variables.get("battery.runtime", "0"))
        self._latest_update_monotonic = now
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
            packets.append(delta3.set_ac_enabled_packet(enabled))
        if cfg.cut_usb:
            packets.append(delta3.set_usb_enabled_packet(enabled))
        if cfg.cut_dc:
            packets.append(delta3.set_dc_enabled_packet(enabled))
        if packets and client is None:
            log.error("auto_shutdown.no_client", note="cannot send EcoFlow command")

        async def _send() -> None:
            if client is not None:
                for packet in packets:
                    try:
                        await client.send_command_packet(packet)
                    except Exception as exc:  # noqa: BLE001
                        log.error("auto_shutdown.send_failed", error=str(exc))
                    await asyncio.sleep(0.3)
            # The HomeKit outlet is an independent cut target on its own radio,
            # so drive it regardless of the EcoFlow link state.
            if cfg.cut_eve and self._eve is not None:
                try:
                    await self.control_eve(enabled)
                except Exception as exc:  # noqa: BLE001
                    log.error("auto_shutdown.eve_failed", error=str(exc))

        asyncio.create_task(_send())

    async def _watchdog(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(5)
            stale = time.monotonic() - self._last_write_monotonic
            if stale > WATCHDOG_TIMEOUT_SECONDS:
                log.critical("daemon.watchdog_timeout", stale_seconds=round(stale))
                self._stop.set()
                # Hard-exit so the supervisor restarts us.
                sys.exit(70)

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
