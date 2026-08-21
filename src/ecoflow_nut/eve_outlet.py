"""Drive a HomeKit-over-BLE smart outlet (e.g. an Eve Energy, BLE/non-Thread).

The DELTA 3's AC output is a single, all-or-nothing bank: the ``ConfigWrite``
toggle cuts every AC socket at once. To shed *one* downstream load (say, an
Unraid server) on critical battery while keeping the other sockets live (a
router / fibre ONT), the bridge needs a switch *downstream* of the EcoFlow. A
HomeKit-over-BLE outlet is exactly that switch.

This module makes the bridge the outlet's HomeKit controller, speaking HAP over
BLE via the optional :mod:`aiohomekit` dependency. A HAP accessory pairs with a
single controller, so the outlet must be reset and removed from Apple Home
first, then paired once with ``ecoflow-nut eve pair``.

Design notes:

* **aiohomekit is optional.** It is imported lazily so the bridge runs without
  it unless the Eve integration is actually used.
* **On-demand connections.** Each action starts a controller, connects, writes,
  and stops again. The DELTA 3 link is a persistent, latency-sensitive BLE
  session; touching the radio only briefly (and ideally on a *separate* adapter)
  keeps it from stalling telemetry. Callers that need several reads (polling the
  outlet's draw until a load goes idle) should hold one :meth:`EveOutlet.session`
  instead of paying a cold connect per sample.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from .config import EveOutletConfig

log = structlog.get_logger(__name__)

# HomeKit "On" characteristic -- public.hap.characteristic.on. aiohomekit may
# report the short ("25") or full UUID form depending on the accessory.
_ON_TYPES = frozenset({"25", "000000250000100080000026bb765291"})
# Eve's vendor characteristic for instantaneous power. Not a standard HAP type,
# so it only ever appears in full form. Present on the metering Eve Energy;
# absent on non-metering outlets, which is why reads of it are optional.
_WATT_TYPES = frozenset({"e863f10d079e48ff8f279c2605a29f52"})


class EveError(RuntimeError):
    """Any failure talking to the HomeKit outlet (surfaced to the CLI/daemon)."""


@dataclass(frozen=True, slots=True)
class EveReading:
    """One batched read of the outlet's state."""

    on: bool | None
    watts: float | None


def _norm_id(device_id: str) -> str:
    """Normalise a HomeKit device id. aiohomekit keys discoveries by the
    lowercase ``xx:xx:..`` form, so a config value in any case still matches."""
    return device_id.strip().lower()


def _norm_char_type(type_str: Any) -> str:
    """Characteristic types come back in mixed case, with or without dashes."""
    return str(type_str).lower().replace("-", "")


def _find_char(accessories: Any, accepted: frozenset[str]) -> tuple[int, int] | None:
    """Locate a characteristic by type across the accessory database.

    Returns its ``(aid, iid)``, or None when the accessory does not expose it --
    the caller decides whether that is fatal (the On switch) or simply a feature
    this hardware lacks (power metering).
    """
    for accessory in accessories:
        aid = accessory["aid"]
        for service in accessory.get("services", []):
            for char in service.get("characteristics", []):
                if _norm_char_type(char.get("type", "")) in accepted:
                    return (aid, char["iid"])
    return None


def _make_scanner(adapter: str) -> Any:
    """A BleakScanner that still exposes ``register_detection_callback``.

    aiohomekit's BLE backend drives the scanner with the legacy
    ``register_detection_callback`` API (that's what Home Assistant's bluetooth
    wrapper provides), but bleak >= 0.22 removed it in favour of a constructor
    ``detection_callback``. This subclass bridges the two: it installs a stable
    dispatcher at construction time and lets aiohomekit (re)point it later.
    """
    from bleak import BleakScanner

    class _CompatScanner(BleakScanner):  # type: ignore[misc]
        def __init__(self, **kwargs: Any) -> None:
            self._ahk_callback: Any = None
            super().__init__(detection_callback=self._dispatch, **kwargs)

        def _dispatch(self, device: Any, advertisement_data: Any) -> None:
            if self._ahk_callback is not None:
                # aiohomekit's per-advert bookkeeping can raise (e.g. before a
                # pairing's accessory DB is populated); swallow so it never spams
                # the bleak/dbus message handler or aborts a scan.
                with contextlib.suppress(Exception):
                    self._ahk_callback(device, advertisement_data)

        def register_detection_callback(self, callback: Any) -> None:
            self._ahk_callback = callback

    return _CompatScanner(adapter=adapter)


def _build_controller(adapter: str) -> Any:
    """Construct a BLE-only aiohomekit Controller bound to ``adapter``.

    Imports are local so aiohomekit/bleak are only required when the Eve
    integration is exercised.
    """
    try:
        from aiohomekit.controller import Controller
        from aiohomekit.controller import controller as _controller_mod
    except ImportError as exc:  # pragma: no cover - exercised via CLI/runtime
        raise EveError(
            "aiohomekit is not installed; install the optional extra with "
            "'pip install ecoflow-nut-bridge[eve]'"
        ) from exc

    # aiohomekit's async_start otherwise also tries to bring up the IP (zeroconf)
    # and CoAP transports. With no AsyncZeroconf instance supplied that raises
    # ("AttributeError: 'NoneType' object has no attribute 'zeroconf'") before the
    # BLE backend is even registered. We only ever want BLE here, so switch the
    # other transports off (they are gated on these module-level flags).
    for _flag in ("IP_TRANSPORT_SUPPORTED", "COAP_TRANSPORT_SUPPORTED"):
        if hasattr(_controller_mod, _flag):
            setattr(_controller_mod, _flag, False)

    return Controller(bleak_scanner_instance=_make_scanner(adapter))


async def _scan_for_device(adapter: str, device_id: str, timeout: int) -> tuple[Any, Any]:
    """Find a specific HomeKit accessory with a plain bleak scan.

    Returns ``(BLEDevice, AdvertisementData)`` for the accessory whose HomeKit
    advert id matches ``device_id``, or ``(None, None)`` if not seen in time.
    """
    from bleak import BleakScanner

    target = _norm_id(device_id)
    found: dict[str, Any] = {}
    done = asyncio.Event()

    def _cb(device: Any, adv: Any) -> None:
        apple = (adv.manufacturer_data or {}).get(0x004C)
        homekit = parse_homekit_advert(apple) if apple else None
        if homekit is not None and homekit["device_id"] == target:
            found["device"], found["adv"] = device, adv
            done.set()

    scanner = BleakScanner(detection_callback=_cb, adapter=adapter)
    await scanner.start()
    try:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(done.wait(), timeout)
    finally:
        await scanner.stop()
    return found.get("device"), found.get("adv")


def _ble_transport(controller: Any) -> Any:
    for transport in controller.transports.values():
        if hasattr(transport, "_device_detected"):
            return transport
    raise EveError("aiohomekit BLE transport is unavailable")


async def _connected_controller(adapter: str, device_id: str, timeout: int) -> Any:
    """Build a started Controller with ``device_id`` seeded into its discovery cache.

    aiohomekit's own BLE scan pipeline does not reliably populate discoveries
    when driven standalone (it is built around Home Assistant's always-on
    bluetooth stack), so we locate the accessory with our own bleak scan and feed
    the result straight into aiohomekit. The caller owns the returned controller
    and must ``async_stop`` it.
    """
    device, _adv = await _scan_for_device(adapter, device_id, timeout)
    if device is None:
        raise EveError(
            f"accessory {_norm_id(device_id)} not seen on {adapter}; "
            "is it in range and advertising? (try 'eve scan')"
        )
    controller = _build_controller(adapter)
    await controller.async_start()
    try:
        transport = _ble_transport(controller)
        transport._device_detected(device, _adv)
        # aiohomekit's live scan keeps running after async_start. Left on, it (a)
        # invokes per-advert bookkeeping on the loaded pairing before its
        # accessory DB exists -- which raises repeatedly -- and (b) scanning while
        # connecting on a single radio drives BlueZ into "Client is already
        # connected" failures. We've already fed aiohomekit the device (and seeded
        # its discovery cache), so silence and stop the scan before connecting;
        # establish_connection works from the BLEDevice without an active scan.
        scanner = getattr(transport, "_scanner", None)
        if scanner is not None:
            with contextlib.suppress(Exception):
                scanner.register_detection_callback(None)
            with contextlib.suppress(Exception):
                await scanner.stop()
    except Exception:
        await controller.async_stop()
        raise
    return controller


class EveSession:
    """An open connection to the outlet: read and write without reconnecting.

    A cold call costs a bleak scan, a BLE connect, a HAP pair-verify and a full
    GATT database read -- seconds of airtime. Anything that samples the outlet
    repeatedly (waiting for a load to go idle) should do it through one of these
    rather than paying that per sample.
    """

    def __init__(self, outlet: EveOutlet, pairing: Any, alias: str) -> None:
        self._outlet = outlet
        self._pairing = pairing
        self._alias = alias

    async def characteristics(self) -> list[dict[str, Any]]:
        """Every characteristic the accessory exposes, flattened, with values.

        Diagnostic only: this is how you find out whether a given unit reports
        power at all, and under which type.
        """
        accessories = await self._pairing.list_accessories_and_characteristics()
        rows: list[dict[str, Any]] = []
        wanted: list[tuple[int, int]] = []
        for accessory in accessories:
            aid = accessory["aid"]
            for service in accessory.get("services", []):
                for char in service.get("characteristics", []):
                    row = {"aid": aid, "service": service.get("type", ""), **char}
                    rows.append(row)
                    if "pr" in (char.get("perms") or []):
                        wanted.append((aid, char["iid"]))
        if wanted:
            # One batched read for every readable characteristic.
            with contextlib.suppress(Exception):
                values = await self._pairing.get_characteristics(wanted)
                for row in rows:
                    entry = values.get((row["aid"], row["iid"]))
                    if entry is not None and "value" in entry:
                        row["value"] = entry["value"]
        return rows

    async def _resolve(
        self, name: str, accepted: frozenset[str]
    ) -> tuple[int, int] | None:
        cache = self._outlet._char_cache
        if name in cache:
            return cache[name]
        accessories = await self._pairing.list_accessories_and_characteristics()
        # Resolve everything we know about in one walk, so a watts lookup never
        # costs a second traversal.
        cache["on"] = _find_char(accessories, _ON_TYPES)
        cache["watts"] = _find_char(accessories, _WATT_TYPES)
        return cache[name]

    async def _on_aid_iid(self) -> tuple[int, int]:
        found = await self._resolve("on", _ON_TYPES)
        if found is None:
            raise EveError("no On characteristic found on the paired accessory")
        return found

    async def read(self) -> EveReading:
        """Batched read of on/off and (when supported) instantaneous watts."""
        on_id = await self._on_aid_iid()
        watt_id = await self._resolve("watts", _WATT_TYPES)
        wanted = [on_id] + ([watt_id] if watt_id is not None else [])
        values = await self._pairing.get_characteristics(wanted)

        raw_on = values.get(on_id, {}).get("value")
        watts: float | None = None
        if watt_id is not None:
            raw_watts = values.get(watt_id, {}).get("value")
            if raw_watts is not None:
                with contextlib.suppress(TypeError, ValueError):
                    watts = float(raw_watts)
        return EveReading(on=None if raw_on is None else bool(raw_on), watts=watts)

    async def set(self, on: bool) -> None:
        aid, iid = await self._on_aid_iid()
        result = await self._pairing.put_characteristics([(aid, iid, on)])
        # put_characteristics returns a (possibly empty) mapping of failures
        # keyed by (aid, iid); a non-empty result means the write was rejected.
        if result:
            raise EveError(f"outlet rejected the write: {result}")
        log.info("eve.set", device_id=self._alias, on=on, aid=aid, iid=iid)


class EveOutlet:
    """Turn a paired HomeKit-over-BLE outlet on or off on demand."""

    def __init__(self, config: EveOutletConfig) -> None:
        self._config = config
        # Cache resolved (aid, iid) pairs between calls so we do not re-walk the
        # accessory database on every toggle. A cached None means "this hardware
        # does not expose it", so absent characteristics are not re-searched.
        self._char_cache: dict[str, tuple[int, int] | None] = {}
        # One radio, one conversation at a time: a web-UI toggle racing an
        # auto-shutdown cut would otherwise start two scanners on the adapter.
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[EveSession]:
        """Open one connection to the outlet for the duration of the block."""
        alias, pairing_data = self._select_pairing(self._pairing_store())
        async with self._lock:
            controller = await _connected_controller(
                self._config.adapter, alias, self._config.connect_timeout_seconds
            )
            try:
                pairing = controller.load_pairing(alias, pairing_data)
                yield EveSession(self, pairing, alias)
            finally:
                # Best-effort: a cancelled session (manual override taking over)
                # must not have its teardown mask the cancellation.
                with contextlib.suppress(Exception):
                    await controller.async_stop()

    def _pairing_store(self) -> dict[str, Any]:
        path = Path(self._config.pairing_file)
        if not path.exists():
            raise EveError(f"no pairing data at {path}; run 'ecoflow-nut eve pair' first")
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise EveError(f"cannot read pairing data {path}: {exc}") from exc
        if not isinstance(data, dict) or not data:
            raise EveError(f"pairing data {path} is empty or malformed")
        return data

    def _select_pairing(self, store: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Pick the configured accessory from the persisted pairing store."""
        alias = (
            _norm_id(self._config.device_id)
            if self._config.device_id
            else next(iter(store))
        )
        if alias not in store:
            raise EveError(
                f"device_id '{alias}' not found in pairing data; " f"have {sorted(store)}"
            )
        return alias, store[alias]

    async def set(self, on: bool) -> None:
        """Connect, flip the outlet's On characteristic, and disconnect."""
        async with self.session() as eve:
            await eve.set(on)

    async def status(self) -> bool | None:
        """Return the outlet's current On value, or None if unavailable."""
        async with self.session() as eve:
            return (await eve.read()).on

    async def read(self) -> EveReading:
        """One connection, returning on/off plus watts when the outlet meters."""
        async with self.session() as eve:
            return await eve.read()

    async def characteristics(self) -> list[dict[str, Any]]:
        """Diagnostic dump of everything the paired accessory exposes."""
        async with self.session() as eve:
            return await eve.characteristics()


async def discover(adapter: str, timeout: int = 10) -> list[dict[str, Any]]:
    """Scan for HomeKit-over-BLE accessories and return brief descriptions."""
    controller = _build_controller(adapter)
    await controller.async_start()
    found: list[dict[str, Any]] = []
    try:
        async for discovery in controller.async_discover(timeout):
            desc = discovery.description
            found.append(
                {
                    "device_id": getattr(desc, "id", None),
                    "name": getattr(desc, "name", None),
                    "category": getattr(desc, "category", None),
                }
            )
    finally:
        await controller.async_stop()
    return found


# HomeKit accessory categories (HAP spec, partial) for human-readable scan output.
_HK_CATEGORIES = {1: "Other", 2: "Bridge", 7: "Outlet", 8: "Switch", 10: "Sensor"}


def parse_homekit_advert(apple_mfr_data: bytes) -> dict[str, Any] | None:
    """Decode an Apple manufacturer-data blob as a HomeKit (HAP-BLE) advert.

    Apple manufacturer data carries several types; HomeKit uses type ``0x06``.
    Layout: type(1) | subtype+len(1) | status-flags(1) | device-id(6) |
    category(2, LE) | global-state(2) | config(1) | compat(1). The low bit of
    the status flags is set while the accessory is *unpaired* (i.e. pairable).
    Returns ``None`` if the blob is not a HomeKit advert.
    """
    if len(apple_mfr_data) < 11 or apple_mfr_data[0] != 0x06:
        return None
    status = apple_mfr_data[2]
    device_id = ":".join(f"{b:02X}" for b in apple_mfr_data[3:9])
    category = int.from_bytes(apple_mfr_data[9:11], "little")
    return {
        "device_id": device_id.lower(),
        "category": category,
        "category_name": _HK_CATEGORIES.get(category, f"#{category}"),
        "status_flags": status,
        # HAP "Status Flag" bit 0: 1 == not paired (discoverable/pairable).
        "paired": not bool(status & 0x01),
    }


async def raw_scan(adapter: str, timeout: int = 10) -> list[dict[str, Any]]:
    """Low-level BLE scan that decodes HomeKit adverts itself (a diagnostic).

    Unlike :func:`discover` this does not go through aiohomekit's filtering, so
    it surfaces every device the radio sees and, for HomeKit accessories, their
    ``device_id`` and paired state -- exactly what's needed to tell "not
    advertising" apart from "still paired to Apple Home".
    """
    from bleak import BleakScanner

    seen: dict[str, dict[str, Any]] = {}

    def _cb(device: Any, adv: Any) -> None:
        apple = (adv.manufacturer_data or {}).get(0x004C)
        homekit = parse_homekit_advert(apple) if apple else None
        entry = seen.setdefault(device.address, {"address": device.address})
        entry["name"] = adv.local_name or entry.get("name")
        entry["rssi"] = adv.rssi
        if homekit is not None:
            entry["homekit"] = homekit

    scanner = BleakScanner(detection_callback=_cb, adapter=adapter)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()
    # HomeKit accessories first, then the rest, for readable output.
    return sorted(seen.values(), key=lambda e: "homekit" not in e)


async def pair(config: EveOutletConfig) -> str:
    """Pair with the configured accessory and persist its pairing data.

    Requires ``device_id`` (from :func:`discover`) and ``setup_code`` (the
    8-digit HomeKit code, e.g. ``123-45-678``). The outlet must not already be
    paired to another controller (reset it / remove from Apple Home first).
    """
    if not config.device_id:
        raise EveError("set eve.device_id (see 'ecoflow-nut eve discover') first")
    if not config.setup_code:
        raise EveError("set eve.setup_code (8-digit HomeKit code) to pair")

    device_id = _norm_id(config.device_id)
    controller = await _connected_controller(
        config.adapter, device_id, config.connect_timeout_seconds
    )
    try:
        transport = _ble_transport(controller)
        discovery = transport.discoveries.get(device_id)
        if discovery is None:
            discovery = await controller.async_find(device_id, timeout=10)
        finish_pairing = await discovery.async_start_pairing(device_id)
        pairing = await finish_pairing(config.setup_code)
        path = Path(config.pairing_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({device_id: pairing.pairing_data}, indent=2))
        log.info("eve.paired", device_id=device_id, pairing_file=str(path))
        return device_id
    finally:
        await controller.async_stop()
