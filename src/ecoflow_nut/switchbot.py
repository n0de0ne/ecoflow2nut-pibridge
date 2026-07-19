"""Control a SwitchBot Bot (mechanical button pusher) over BLE.

Unlike the HomeKit outlet, a SwitchBot Bot speaks plain BLE GATT -- no pairing,
no handshake. We scan for it, connect on demand (via bleak-retry-connector,
already a dependency), write a one-shot command characteristic and disconnect.
A convenience to physically press a server's power button from the CLI or web
dashboard.

Deliberately *manual only* -- it is not wired into auto-shutdown, because a
power button is a toggle and an automated press could power off a server that
already auto-booted. Password-protected Bots are not supported (the simple
unencrypted command path only).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog

from .config import SwitchBotConfig

log = structlog.get_logger(__name__)

# SwitchBot Bot GATT (community-documented, stable across Bot firmware).
_CMD_CHAR = "cba20002-224d-11e6-9fb8-0002a5d5c51b"
_NOTIFY_CHAR = "cba20003-224d-11e6-9fb8-0002a5d5c51b"
# 0x57 = magic, 0x01 = device-command group, then the action byte.
_COMMANDS = {
    "press": bytes([0x57, 0x01, 0x00]),
    "on": bytes([0x57, 0x01, 0x01]),
    "off": bytes([0x57, 0x01, 0x02]),
}
# SwitchBot BLE company id (manufacturer data key) and service-data UUID.
_COMPANY_ID = 0x0969
_SERVICE_DATA_UUID = "0000fd3d-0000-1000-8000-00805f9b34fb"


class SwitchBotError(RuntimeError):
    """Any failure talking to the SwitchBot (surfaced to the CLI/daemon)."""


def _looks_like_switchbot(adv: Any) -> bool:
    if _COMPANY_ID in (adv.manufacturer_data or {}):
        return True
    return any(str(u).lower() == _SERVICE_DATA_UUID for u in (adv.service_data or {}))


async def scan(adapter: str, timeout: int = 10) -> list[dict[str, Any]]:
    """List nearby SwitchBot devices (to find a Bot's MAC)."""
    from bleak import BleakScanner

    seen: dict[str, dict[str, Any]] = {}

    def _cb(device: Any, adv: Any) -> None:
        if _looks_like_switchbot(adv):
            seen[device.address] = {
                "address": device.address,
                "name": adv.local_name or device.name,
                "rssi": adv.rssi,
            }

    scanner = BleakScanner(detection_callback=_cb, adapter=adapter)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()
    return list(seen.values())


async def _find_device(adapter: str, mac: str, timeout: int) -> Any:
    from bleak import BleakScanner

    target = mac.strip().lower()
    found: dict[str, Any] = {}
    done = asyncio.Event()

    def _cb(device: Any, _adv: Any) -> None:
        if device.address.lower() == target:
            found["device"] = device
            done.set()

    scanner = BleakScanner(detection_callback=_cb, adapter=adapter)
    await scanner.start()
    try:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(done.wait(), timeout)
    finally:
        await scanner.stop()
    return found.get("device")


class SwitchBot:
    """Send a one-shot command to a SwitchBot Bot over BLE."""

    def __init__(self, config: SwitchBotConfig) -> None:
        self._config = config

    async def send(self, action: str = "press") -> str:
        """Connect, send ``action`` (press/on/off), and disconnect."""
        if action not in _COMMANDS:
            raise SwitchBotError(f"unknown action: {action} (press|on|off)")
        if not self._config.mac:
            raise SwitchBotError("set switchbot.mac (see 'switchbot scan') first")

        device = await _find_device(
            self._config.adapter, self._config.mac, self._config.connect_timeout_seconds
        )
        if device is None:
            raise SwitchBotError(
                f"SwitchBot {self._config.mac} not seen on {self._config.adapter}; "
                "is it in range? (try 'switchbot scan')"
            )

        from bleak import BleakClient
        from bleak_retry_connector import establish_connection

        client = await establish_connection(BleakClient, device, device.address)
        try:
            notified = asyncio.Event()
            response: dict[str, bytes] = {}

            def _on_notify(_char: Any, data: bytearray) -> None:
                response["data"] = bytes(data)
                notified.set()

            with contextlib.suppress(Exception):
                await client.start_notify(_NOTIFY_CHAR, _on_notify)
            await client.write_gatt_char(_CMD_CHAR, _COMMANDS[action], response=False)
            # The Bot replies on the notify char (first byte 0x01 == success);
            # not all firmware notifies, so a timeout is not treated as failure.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(notified.wait(), 5)
            data = response.get("data")
            if data and data[0] not in (0x01, 0x05):
                raise SwitchBotError(
                    f"SwitchBot rejected the command (0x{data.hex()}); "
                    "a password may be set, which is unsupported"
                )
            log.info(
                "switchbot.command",
                mac=self._config.mac,
                action=action,
                response=data.hex() if data else None,
            )
            return f"switchbot {action}"
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
