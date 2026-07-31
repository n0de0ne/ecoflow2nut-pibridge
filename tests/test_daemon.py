"""Tests for the daemon's background loops."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ecoflow_nut.config import Config, EcoflowConfig, NutConfig
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
