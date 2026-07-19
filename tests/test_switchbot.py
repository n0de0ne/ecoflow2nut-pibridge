"""SwitchBot Bot control: config parsing + command path (with a fake BLE client)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ecoflow_nut import switchbot as sb
from ecoflow_nut.config import SwitchBotConfig, load_config
from ecoflow_nut.switchbot import _COMMANDS, SwitchBot, SwitchBotError

_BASE = """
ecoflow:
  mac: "AA:BB:CC:DD:EE:FF"
  serial: "P231TEST"
"""


def _write(tmp_path: Path, extra: str = "") -> str:
    path = tmp_path / "config.yaml"
    path.write_text(_BASE + extra)
    return str(path)


def test_switchbot_defaults_disabled(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path))
    assert config.switchbot.enabled is False
    assert config.switchbot.adapter == "hci0"


def test_switchbot_section_parses(tmp_path: Path) -> None:
    extra = """
switchbot:
  enabled: true
  mac: "C1:22:33:44:55:66"
  adapter: "hci1"
"""
    config = load_config(_write(tmp_path, extra))
    assert config.switchbot.enabled is True
    assert config.switchbot.mac == "C1:22:33:44:55:66"
    assert config.switchbot.adapter == "hci1"


def test_command_bytes() -> None:
    assert _COMMANDS["press"] == bytes([0x57, 0x01, 0x00])
    assert _COMMANDS["on"] == bytes([0x57, 0x01, 0x01])
    assert _COMMANDS["off"] == bytes([0x57, 0x01, 0x02])


def test_send_rejects_unknown_action() -> None:
    bot = SwitchBot(SwitchBotConfig(enabled=True, mac="x"))
    with pytest.raises(SwitchBotError, match="unknown action"):
        asyncio.run(bot.send("explode"))


def test_send_requires_mac() -> None:
    bot = SwitchBot(SwitchBotConfig(enabled=True, mac=""))
    with pytest.raises(SwitchBotError, match="switchbot.mac"):
        asyncio.run(bot.send("press"))


class _FakeClient:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []
        self.disconnected = False

    async def start_notify(self, char, cb) -> None:
        # Reply success (first byte 0x01) so the command path completes.
        cb(char, bytearray([0x01]))

    async def write_gatt_char(self, char, data, response=False) -> None:
        self.writes.append((char, bytes(data)))

    async def disconnect(self) -> None:
        self.disconnected = True


def test_send_writes_press_command(monkeypatch) -> None:
    client = _FakeClient()

    async def _find(adapter, mac, timeout):
        return type("Dev", (), {"address": "C1:22:33:44:55:66"})()  # stand-in BLEDevice

    async def _establish(cls, device, name, *a, **k):
        return client

    monkeypatch.setattr(sb, "_find_device", _find)
    # establish_connection is imported lazily inside send(); patch the source.
    import bleak_retry_connector

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", _establish)

    msg = asyncio.run(SwitchBot(SwitchBotConfig(enabled=True, mac="x")).send("press"))
    assert msg == "switchbot press"
    assert client.writes == [(sb._CMD_CHAR, bytes([0x57, 0x01, 0x00]))]
    assert client.disconnected is True


def test_send_device_not_found(monkeypatch) -> None:
    async def _find(adapter, mac, timeout):
        return None

    monkeypatch.setattr(sb, "_find_device", _find)
    with pytest.raises(SwitchBotError, match="not seen"):
        asyncio.run(SwitchBot(SwitchBotConfig(enabled=True, mac="x")).send("press"))
