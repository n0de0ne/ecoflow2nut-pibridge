"""Tests for the daemon's background loops."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import pytest

from ecoflow_nut.config import AutoShutdownConfig, Config, EcoflowConfig, NutConfig
from ecoflow_nut.eve_outlet import EveReading
from ecoflow_nut.main import Daemon


class _StubStore:
    """Records prune calls so the test can assert the retention window runs."""

    def __init__(self) -> None:
        self.pruned: list[str] = []

    async def prune(self, device: str) -> None:
        self.pruned.append(device)


def _daemon(tmp_path: Path) -> Daemon:
    return Daemon(
        Config(
            ecoflow=EcoflowConfig(mac="AA:BB:CC:DD:EE:FF", serial="P231"),
            nut=NutConfig(dev_file_path=str(tmp_path / "ecoflow.dev")),
            settings_file=str(tmp_path / "settings.json"),
        )
    )


async def test_prune_loop_prunes_immediately(tmp_path: Path) -> None:
    """Retention must be applied at startup, not one interval later.

    prune() existed on both stores but was never called by anything, so
    retention_days was silently inert -- this pins the wiring.
    """
    daemon = _daemon(tmp_path)
    store = _StubStore()
    daemon._store = store

    task = asyncio.create_task(daemon._prune_loop())
    await asyncio.sleep(0)  # let it reach the first await
    try:
        assert store.pruned == ["ecoflow"]
    finally:
        task.cancel()


async def test_prune_loop_without_a_store_is_inert(tmp_path: Path) -> None:
    """History logging is optional; the loop must not care."""
    daemon = _daemon(tmp_path)
    assert daemon._store is None

    task = asyncio.create_task(daemon._prune_loop())
    await asyncio.sleep(0)
    task.cancel()
    # Cancelling an un-started sleep is the only outcome; nothing raised.
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_prune_loop_stops_with_the_daemon(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    daemon._store = _StubStore()
    daemon._stop.set()
    await asyncio.wait_for(daemon._prune_loop(), timeout=1)


# --- Eve idle confirmation before a cut ------------------------------------ #


class _FakeEve:
    """Stands in for EveOutlet: yields scripted readings from a held session."""

    def __init__(self, readings: list[object]) -> None:
        self._readings = list(readings)
        self.sessions = 0
        self.reads = 0
        self.commanded: list[bool] = []

    @contextlib.asynccontextmanager
    async def session(self):
        self.sessions += 1
        outer = self

        class _Session:
            async def read(self) -> EveReading:
                outer.reads += 1
                if not outer._readings:
                    return EveReading(on=True, watts=0.0)
                nxt = outer._readings.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return EveReading(on=True, watts=nxt)

            async def set(self, on: bool) -> None:
                outer.commanded.append(on)

        yield _Session()

    async def set(self, on: bool) -> None:
        self.commanded.append(on)


def _confirm_daemon(tmp_path: Path, eve: _FakeEve, **kw: object) -> Daemon:
    policy = dict(
        enabled=True,
        cut_eve=True,
        eve_confirm_idle_watts=5.0,
        eve_confirm_samples=2,
        eve_confirm_poll_seconds=0.01,  # floored to 0.05s; keeps tests quick
    )
    policy.update(kw)
    daemon = Daemon(
        Config(
            ecoflow=EcoflowConfig(mac="AA:BB:CC:DD:EE:FF", serial="P231"),
            nut=NutConfig(dev_file_path=str(tmp_path / "ecoflow.dev")),
            settings_file=str(tmp_path / "settings.json"),
            auto_shutdown=AutoShutdownConfig(**policy),  # type: ignore[arg-type]
        )
    )
    daemon._eve = eve  # type: ignore[assignment]
    return daemon


async def test_waits_until_the_outlet_reports_idle(tmp_path: Path) -> None:
    """The point of the feature: don't cut while the server is still drawing."""
    eve = _FakeEve([180.0, 90.0, 40.0, 2.0, 1.5])
    daemon = _confirm_daemon(tmp_path, eve)
    await asyncio.wait_for(daemon._await_eve_idle(), timeout=5)
    # Five readings: three above the threshold, then the two consecutive idle
    # ones that satisfy eve_confirm_samples.
    assert eve.reads == 5
    assert eve.sessions == 1, "expected one held connection, not one per sample"


async def test_never_cuts_while_the_load_keeps_drawing(tmp_path: Path) -> None:
    eve = _FakeEve([200.0] * 50)
    daemon = _confirm_daemon(tmp_path, eve)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(daemon._await_eve_idle(), timeout=0.2)
    assert eve.commanded == [], "must not have cut anything"


async def test_a_failed_read_reopens_the_session(tmp_path: Path) -> None:
    """A flaky link should recover, not abort the wait and cut blindly."""
    eve = _FakeEve([1.0, RuntimeError("ble dropped"), 1.0, 1.0])
    daemon = _confirm_daemon(tmp_path, eve)
    await asyncio.wait_for(daemon._await_eve_idle(), timeout=5)
    assert eve.sessions == 2, "expected the session to be reopened after the error"


async def test_timeout_cuts_anyway_when_configured(tmp_path: Path) -> None:
    """The opt-in bound on 'never cut without confirmation'."""
    eve = _FakeEve([300.0] * 100)
    daemon = _confirm_daemon(tmp_path, eve, eve_confirm_timeout_seconds=0)
    await asyncio.wait_for(daemon._await_eve_idle(), timeout=5)


async def test_no_threshold_means_no_wait(tmp_path: Path) -> None:
    """Unset, the historical immediate-cut behaviour is unchanged."""
    eve = _FakeEve([500.0] * 10)
    daemon = _confirm_daemon(tmp_path, eve, eve_confirm_idle_watts=None)
    await asyncio.wait_for(daemon._await_eve_idle(), timeout=5)
    assert eve.reads == 0


async def test_status_reports_the_wait_instead_of_claiming_cut(
    tmp_path: Path,
) -> None:
    """`triggered` latches before any I/O, so the UI needs the real phase."""
    eve = _FakeEve([200.0] * 50)
    daemon = _confirm_daemon(tmp_path, eve)
    task = asyncio.create_task(daemon._await_eve_idle())
    await asyncio.sleep(0.05)
    try:
        status = daemon._autoshutdown_status()
        assert status["awaiting_eve_idle"] is True
        assert status["eve_watts"] == pytest.approx(200.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert daemon._autoshutdown_status()["awaiting_eve_idle"] is False


async def test_manual_override_cancels_the_wait(tmp_path: Path) -> None:
    """The operator must always be able to cut now."""
    eve = _FakeEve([200.0] * 100)
    daemon = _confirm_daemon(tmp_path, eve)
    task = asyncio.create_task(daemon._await_eve_idle())
    daemon._eve_confirm_task = task  # type: ignore[assignment]
    await asyncio.sleep(0.05)

    await asyncio.wait_for(daemon.control_eve(False), timeout=5)
    assert task.cancelled() or task.done()
    assert eve.commanded == [False]
    assert daemon._eve_state is False


def _watchdog_daemon(tmp_path: Path) -> Daemon:
    config = Config(
        ecoflow=EcoflowConfig(
            mac="DC:06:75:A8:3E:29", serial="E201ZE1APH560861", model="e2000"
        )
    )
    config.nut.dev_file_path = str(tmp_path / "ecoflow.dev")
    config.settings_file = str(tmp_path / "settings.json")
    return Daemon(config)


def _hurry_the_watchdog(monkeypatch):
    """Collapse the watchdog's tick, without starving the event loop.

    Bind the real sleep first: the patch target is the shared ``asyncio``
    module, so a replacement that called ``asyncio.sleep`` would recurse -- and
    so a test that wants to wait for real needs the returned original, not the
    patched name.
    """
    real_sleep = asyncio.sleep

    async def _tick(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("ecoflow_nut.main.asyncio.sleep", _tick)
    return real_sleep


async def test_watchdog_reconnects_before_it_restarts_the_process(tmp_path, monkeypatch):
    """A quiet link is worth a reconnect before it is worth a restart.

    Exiting costs the supervisor's backoff, a cold scan and a fresh handshake,
    and races the device: a station that has just lost its only client needs a
    moment before it advertises again. Reconnecting in-process is both cheaper
    and likelier to work, so it has to be tried first.
    """
    from ecoflow_nut import main as main_mod

    daemon = _watchdog_daemon(tmp_path)
    disconnects: list[bool] = []

    class _Client:
        async def disconnect(self) -> None:
            disconnects.append(True)

    daemon._active_client = _Client()  # type: ignore[assignment]
    # Make the link look stale enough to trip the watchdog immediately, and
    # keep it stale: nothing here ever brings the telemetry back.
    daemon._last_write_monotonic = time.monotonic() - 10_000
    _hurry_the_watchdog(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        await daemon._watchdog()

    assert exc.value.code == 70, "gives up eventually so the supervisor restarts us"
    assert len(disconnects) == main_mod.WATCHDOG_MAX_RECOVERIES + 1, (
        "one release per reconnect attempt, plus one before exiting"
    )


async def test_watchdog_stands_down_once_the_link_recovers(tmp_path, monkeypatch):
    """A reconnect that works must not still cost a restart."""
    daemon = _watchdog_daemon(tmp_path)
    disconnects: list[bool] = []

    class _Client:
        async def disconnect(self) -> None:
            disconnects.append(True)
            # Stand in for the run loop reconnecting and telemetry resuming.
            daemon._last_write_monotonic = time.monotonic()

    daemon._active_client = _Client()  # type: ignore[assignment]
    daemon._last_write_monotonic = time.monotonic() - 10_000
    real_sleep = _hurry_the_watchdog(monkeypatch)

    task = asyncio.create_task(daemon._watchdog())
    await real_sleep(0.2)  # thousands of ticks at the patched rate
    assert not task.done(), "a recovered link must not be escalated to a restart"
    assert disconnects == [True], "and must not be dropped again"
    daemon._stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_watchdog_still_exits_if_the_disconnect_fails(tmp_path, monkeypatch):
    """A wedged link must not stop the restart -- exiting is the recovery."""
    daemon = _watchdog_daemon(tmp_path)

    class _StuckClient:
        async def disconnect(self) -> None:
            raise OSError("transport already gone")

    daemon._active_client = _StuckClient()  # type: ignore[assignment]
    daemon._last_write_monotonic = time.monotonic() - 10_000
    _hurry_the_watchdog(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        await daemon._watchdog()
    assert exc.value.code == 70


async def test_watchdog_escalates_when_there_is_nothing_to_disconnect(
    tmp_path, monkeypatch
):
    """Stuck mid-reconnect with no client is exactly when the restart is due."""
    daemon = _watchdog_daemon(tmp_path)
    daemon._last_write_monotonic = time.monotonic() - 10_000
    _hurry_the_watchdog(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        await daemon._watchdog()
    assert exc.value.code == 70


# ---------------------------------------------------------------------- #
# Port state after a command
# ---------------------------------------------------------------------- #


class _SendingClient:
    """Accepts command packets and records them."""

    is_connected = True

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_command_packet(self, packet: object) -> None:
        self.sent.append(packet)


async def test_switching_usb_off_clears_the_inferred_on_state(tmp_path: Path) -> None:
    """USB has no switch flag on this generation, so nothing else would.

    The driver reads USB as ON from a live draw. Switch the bank off and the
    draw goes to zero -- which is exactly the reading that proves nothing, so
    the ON would stand forever. The command is the missing evidence.
    """
    from ecoflow_nut.state import DeviceState

    daemon = _daemon(tmp_path)
    daemon._active_client = _SendingClient()  # type: ignore[assignment]
    daemon._latest_state = DeviceState(usb_output_on=True, usb_output_watts=1.0)

    await daemon.control_output("usb", False)

    assert daemon._latest_state.usb_output_on is False


async def test_a_command_with_no_telemetry_yet_does_not_crash(tmp_path: Path) -> None:
    """Controls are reachable before the first heartbeat lands."""
    daemon = _daemon(tmp_path)
    daemon._active_client = _SendingClient()  # type: ignore[assignment]
    assert daemon._latest_state is None

    await daemon.control_output("ac", True)  # must not raise


async def test_the_eve_tile_reflects_a_read_not_the_last_command(
    tmp_path: Path,
) -> None:
    """The outlet has its own button and its own app.

    A bridge that only remembers what it commanded shows "?" after every
    restart, and shows the wrong thing the moment anyone else touches it.
    """
    daemon = _daemon(tmp_path)
    daemon._config.eve.poll_interval_seconds = 3600

    class _Outlet:
        async def read(self) -> EveReading:
            return EveReading(on=True, watts=121.7)

    daemon._eve = _Outlet()  # type: ignore[assignment]
    assert daemon._web_state()["eve_on"] is None

    task = asyncio.create_task(daemon._eve_poll())
    for _ in range(100):
        await asyncio.sleep(0)
        if daemon._eve_state is not None:
            break
    daemon._stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert daemon._web_state()["eve_on"] is True
    assert daemon._web_state()["eve_watts"] == pytest.approx(121.7)


async def test_an_unreachable_outlet_keeps_the_last_known_state(
    tmp_path: Path,
) -> None:
    """A missed poll means we did not look, not that the outlet has no state.

    Blanking the tile on a single failed read would make a flaky link look
    like a flaky outlet, and would throw away the answer we already had.
    """
    daemon = _daemon(tmp_path)
    daemon._config.eve.poll_interval_seconds = 3600
    daemon._eve_state = True
    daemon._eve_watts = 121.7

    class _DeadOutlet:
        async def read(self) -> EveReading:
            raise OSError("le-connection-abort-by-local")

    daemon._eve = _DeadOutlet()  # type: ignore[assignment]

    task = asyncio.create_task(daemon._eve_poll())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    daemon._stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert daemon._eve_state is True
    assert daemon._eve_watts == pytest.approx(121.7)


async def test_the_poll_stands_aside_for_an_auto_shutdown_confirmation(
    tmp_path: Path,
) -> None:
    """That loop holds the outlet's BLE lock, possibly indefinitely.

    Queueing a poll behind it achieves nothing and parks a task on a lock with
    no bounded wait.
    """
    daemon = _daemon(tmp_path)
    daemon._config.eve.poll_interval_seconds = 3600
    reads = 0

    class _Outlet:
        async def read(self) -> EveReading:
            nonlocal reads
            reads += 1
            return EveReading(on=True, watts=5.0)

    daemon._eve = _Outlet()  # type: ignore[assignment]
    daemon._eve_confirm = object()  # type: ignore[assignment]

    task = asyncio.create_task(daemon._eve_poll())
    for _ in range(20):
        await asyncio.sleep(0)
    daemon._stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert reads == 0


# --- Solar by calendar day ------------------------------------------------- #


class _SeriesStore:
    """Records the windows and bucket widths energy_series was asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, float, float]] = []

    async def energy_series(
        self, device: str, minutes: int, bucket: int, *, since: float, until: float
    ) -> list[dict[str, object]]:
        self.calls.append((bucket, since, until))
        return []


async def test_solar_asks_for_whole_local_days(tmp_path: Path) -> None:
    """The window has to start at a local midnight, not N*86400 seconds ago.

    Cut on elapsed seconds, the oldest "day" is a fragment starting at whatever
    time of day it happens to be -- so the first bar is always short and the
    daily average is always wrong.
    """
    from datetime import datetime, timedelta

    from ecoflow_nut import solar

    daemon = _daemon(tmp_path)
    store = _SeriesStore()
    daemon._store = store

    await daemon._web_solar(days=30)

    buckets = [c[0] for c in store.calls]
    assert buckets == [3600, 900], "daily totals hourly; the pace comparison finer"

    today = datetime.now().astimezone().date()

    def midnight(days_back: int) -> float:
        return solar.local_midnight(today - timedelta(days=days_back)).timestamp()

    assert store.calls[0][1] == midnight(29)
    # The pace query only ever spans yesterday and today, whatever the range.
    assert store.calls[1][1] == midnight(1)


async def test_solar_without_a_store_is_disabled(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    assert await daemon._web_solar(days=30) == {"enabled": False}
