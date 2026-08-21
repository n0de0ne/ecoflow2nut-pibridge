"""Republish throttling in the daemon's state callback.

The DELTA 2 generation streams several subsystem heartbeats per second rather
than one periodic frame. Rewriting the NUT file on every one -- an atomic
mkstemp/write/chmod/rename each time -- pegged a Pi Zero near 80% CPU and wrote
to the SD card dozens of times a second.
"""

from __future__ import annotations

import pytest

from ecoflow_nut.config import Config, EcoflowConfig
from ecoflow_nut.main import Daemon
from ecoflow_nut.state import DeviceState


@pytest.fixture
def daemon(tmp_path, monkeypatch) -> Daemon:
    config = Config(
        ecoflow=EcoflowConfig(
            mac="DC:06:75:A8:3E:29",
            serial="E201ZE1APH560861",
            model="e2000",
            poll_interval_seconds=5,
        )
    )
    config.nut.dev_file_path = str(tmp_path / "ecoflow.dev")
    config.settings_file = str(tmp_path / "settings.json")
    return Daemon(config)


def _writes(daemon: Daemon) -> int:
    return daemon._writer.path.read_text().count("ups.status") if _exists(daemon) else 0


def _exists(daemon: Daemon) -> bool:
    return daemon._writer.path.exists()


def _on_mains(soc: float = 90.0) -> DeviceState:
    return DeviceState(soc_percent=soc, ac_input_voltage=230.0, ac_output_watts=100)


def _outage(soc: float = 90.0) -> DeviceState:
    return DeviceState(soc_percent=soc, ac_input_voltage=0.0, ac_output_watts=100)


def test_a_burst_of_frames_produces_one_write(daemon, monkeypatch):
    written: list[DeviceState] = []
    monkeypatch.setattr(
        daemon._writer, "write", lambda s: (written.append(s), {"ups.status": "OL"})[1]
    )
    now = [1000.0]
    monkeypatch.setattr("ecoflow_nut.main.time.monotonic", lambda: now[0])
    daemon._last_write_monotonic = now[0]

    # 30 heartbeats inside one poll interval -- roughly one real second.
    for _ in range(30):
        now[0] += 0.03
        daemon._on_state(_on_mains())
    assert len(written) == 1, "status changed once (OB->OL), so exactly one write"

    # Still inside the interval: nothing further.
    before = len(written)
    for _ in range(30):
        now[0] += 0.03
        daemon._on_state(_on_mains())
    assert len(written) == before

    # Past the interval: one more.
    now[0] += 5.0
    daemon._on_state(_on_mains())
    assert len(written) == before + 1


def test_an_outage_is_published_immediately(daemon, monkeypatch):
    """Throttling must never delay a status change reaching NUT clients."""
    statuses: list[str] = []

    def _fake_write(state: DeviceState) -> dict[str, str]:
        from ecoflow_nut.nut_writer import build_variables

        variables = build_variables(state, daemon._config.nut)
        statuses.append(variables["ups.status"])
        return variables

    monkeypatch.setattr(daemon._writer, "write", _fake_write)
    now = [1000.0]
    monkeypatch.setattr("ecoflow_nut.main.time.monotonic", lambda: now[0])

    now[0] += 0.1
    daemon._on_state(_on_mains())
    assert statuses == ["OL"]

    # The grid drops a fraction of a second later, far inside the interval.
    now[0] += 0.05
    daemon._on_state(_outage())
    assert statuses == ["OL", "OB"], "an outage must not wait for the next tick"

    # And low battery, likewise, is not held back.
    now[0] += 0.05
    daemon._on_state(_outage(soc=10.0))
    assert statuses[-1] == "OB LB"


def test_the_dashboard_snapshot_stays_live_between_writes(daemon, monkeypatch):
    """Throttling the file must not freeze the web UI's view."""
    monkeypatch.setattr(daemon._writer, "write", lambda s: {"ups.status": "OL"})
    now = [1000.0]
    monkeypatch.setattr("ecoflow_nut.main.time.monotonic", lambda: now[0])
    daemon._last_write_monotonic = now[0]
    daemon._latest_status = "OL"

    now[0] += 0.03
    daemon._on_state(_on_mains(soc=88.0))
    assert daemon._latest_state is not None
    assert daemon._latest_state.soc_percent == 88.0
