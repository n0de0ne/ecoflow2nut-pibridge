"""Tests for the daemon's background loops."""

from __future__ import annotations

import asyncio
import contextlib
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
