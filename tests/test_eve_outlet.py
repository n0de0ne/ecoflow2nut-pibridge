"""HomeKit-over-BLE outlet integration: config parsing + control logic.

aiohomekit is an optional dependency and is NOT installed in the test env; the
EveOutlet talks to a fake controller injected via ``_build_controller`` so the
logic is exercised without any real BLE / aiohomekit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecoflow_nut import eve_outlet
from ecoflow_nut.config import EveOutletConfig, load_config
from ecoflow_nut.eve_outlet import (
    _ON_TYPES,
    _WATT_TYPES,
    EveError,
    EveOutlet,
    _find_char,
    _norm_char_type,
)

_BASE = """
ecoflow:
  mac: "AA:BB:CC:DD:EE:FF"
  serial: "P231TEST"
"""


def _write(tmp_path: Path, extra: str = "") -> str:
    path = tmp_path / "config.yaml"
    path.write_text(_BASE + extra)
    return str(path)


# --- config parsing -------------------------------------------------------- #


def test_eve_defaults_disabled(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path))
    assert config.eve.enabled is False
    assert config.eve.adapter == "hci1"
    assert config.auto_shutdown.cut_eve is False


def test_eve_section_parses(tmp_path: Path) -> None:
    extra = """
eve:
  enabled: true
  device_id: "AA:BB:CC:11:22:33"
  adapter: "hci2"
  setup_code: "123-45-678"
auto_shutdown:
  cut_eve: true
"""
    config = load_config(_write(tmp_path, extra))
    assert config.eve.enabled is True
    assert config.eve.device_id == "AA:BB:CC:11:22:33"
    assert config.eve.adapter == "hci2"
    assert config.eve.setup_code == "123-45-678"
    assert config.auto_shutdown.cut_eve is True


# --- On-characteristic matching ------------------------------------------- #


@pytest.mark.parametrize("value", ["25", "00000025-0000-1000-8000-0026BB765291"])
def test_on_type_matches(value: str) -> None:
    assert _norm_char_type(value) in _ON_TYPES


@pytest.mark.parametrize("value", ["26", "name", ""])
def test_on_type_rejects(value: str) -> None:
    assert _norm_char_type(value) not in _ON_TYPES


def test_watt_type_matches_eve_vendor_uuid() -> None:
    assert _norm_char_type("E863F10D-079E-48FF-8F27-9C2605A29F52") in _WATT_TYPES


def test_find_char_returns_none_when_absent() -> None:
    """A non-metering outlet must read as "no watts", not raise."""
    accessories = [
        {"aid": 1, "services": [{"characteristics": [{"type": "25", "iid": 9}]}]}
    ]
    assert _find_char(accessories, _ON_TYPES) == (1, 9)
    assert _find_char(accessories, _WATT_TYPES) is None


# --- control via a fake aiohomekit controller ----------------------------- #


class _FakePairing:
    def __init__(self, accessories: list[dict]) -> None:
        self._accessories = accessories
        self.written: list[tuple] = []
        self.values: dict[tuple, dict] = {}

    async def list_accessories_and_characteristics(self) -> list[dict]:
        return self._accessories

    async def put_characteristics(self, chars):
        self.written.extend(chars)
        return {}  # empty == success

    async def get_characteristics(self, chars):
        return {c: {"value": self.values.get(c, {}).get("value")} for c in chars}


class _FakeController:
    def __init__(self, pairing: _FakePairing) -> None:
        self._pairing = pairing
        self.started = False
        self.stopped = False
        self.loaded: tuple | None = None

    async def async_start(self) -> None:
        self.started = True

    async def async_stop(self) -> None:
        self.stopped = True

    def load_pairing(self, alias, data):
        self.loaded = (alias, data)
        return self._pairing


def _patch_connected(monkeypatch, controller) -> list[tuple]:
    """Bypass the real bleak scan + seed; hand back a fake started controller.

    Returns the list of connect calls, so tests can assert how many cold
    connections a sequence of operations actually cost.
    """
    calls: list[tuple] = []

    async def _conn(adapter, device_id, timeout):
        calls.append((adapter, device_id, timeout))
        controller.started = True
        return controller

    monkeypatch.setattr(eve_outlet, "_connected_controller", _conn)
    return calls


def _outlet_accessories() -> list[dict]:
    return [
        {
            "aid": 1,
            "services": [
                {
                    "characteristics": [
                        {"iid": 8, "type": "23"},  # Name
                        {"iid": 9, "type": "25"},  # On
                    ]
                }
            ],
        }
    ]


@pytest.fixture()
def paired(tmp_path: Path) -> EveOutletConfig:
    pairing_file = tmp_path / "eve.json"
    pairing_file.write_text(json.dumps({"dev1": {"AccessoryAddress": "x"}}))
    return EveOutletConfig(enabled=True, device_id="dev1", pairing_file=str(pairing_file))


def test_set_writes_on_characteristic(monkeypatch, paired: EveOutletConfig) -> None:
    pairing = _FakePairing(_outlet_accessories())
    controller = _FakeController(pairing)
    _patch_connected(monkeypatch, controller)

    import asyncio

    asyncio.run(EveOutlet(paired).set(True))

    assert pairing.written == [(1, 9, True)]
    assert controller.loaded == ("dev1", {"AccessoryAddress": "x"})
    assert controller.started and controller.stopped


def test_select_pairing_is_case_insensitive(monkeypatch, tmp_path: Path) -> None:
    # aiohomekit keys discoveries/pairings by the lowercase device id; a config
    # value in upper-case must still resolve.
    pairing_file = tmp_path / "eve.json"
    pairing_file.write_text(json.dumps({"6e:e0:a7:97:ea:d2": {"x": 1}}))
    cfg = EveOutletConfig(
        enabled=True, device_id="6E:E0:A7:97:EA:D2", pairing_file=str(pairing_file)
    )
    pairing = _FakePairing(_outlet_accessories())
    _patch_connected(monkeypatch, _FakeController(pairing))
    import asyncio

    asyncio.run(EveOutlet(cfg).set(True))
    assert pairing.written == [(1, 9, True)]


def test_set_caches_aid_iid(monkeypatch, paired: EveOutletConfig) -> None:
    pairing = _FakePairing(_outlet_accessories())
    _patch_connected(monkeypatch, _FakeController(pairing))
    import asyncio

    outlet = EveOutlet(paired)
    asyncio.run(outlet.set(False))
    # One walk resolves every characteristic we know about, so a later watts
    # read costs no extra traversal -- and a cached None is remembered, so an
    # outlet without metering is not re-searched on every poll.
    assert outlet._char_cache["on"] == (1, 9)
    assert outlet._char_cache["watts"] is None


def _metering_accessories() -> list[dict]:
    """An Eve Energy: the On switch plus the vendor instantaneous-watts char."""
    return [
        {
            "aid": 1,
            "services": [
                {
                    "characteristics": [
                        {"iid": 8, "type": "23", "perms": ["pr"]},  # Name
                        {"iid": 9, "type": "25", "perms": ["pr", "pw"]},  # On
                        {
                            "iid": 21,
                            "type": "E863F10D-079E-48FF-8F27-9C2605A29F52",
                            "perms": ["pr"],
                            "unit": "W",
                        },
                    ]
                }
            ],
        }
    ]


def test_read_returns_watts_when_the_outlet_meters(
    monkeypatch, paired: EveOutletConfig
) -> None:
    pairing = _FakePairing(_metering_accessories())
    pairing.values = {(1, 9): {"value": True}, (1, 21): {"value": 84.5}}
    _patch_connected(monkeypatch, _FakeController(pairing))
    import asyncio

    reading = asyncio.run(EveOutlet(paired).read())
    assert reading.on is True
    assert reading.watts == pytest.approx(84.5)


def test_read_without_metering_reports_none_watts(
    monkeypatch, paired: EveOutletConfig
) -> None:
    """A plain outlet still reports on/off; watts is simply unavailable."""
    pairing = _FakePairing(_outlet_accessories())
    pairing.values = {(1, 9): {"value": False}}
    _patch_connected(monkeypatch, _FakeController(pairing))
    import asyncio

    reading = asyncio.run(EveOutlet(paired).read())
    assert reading.on is False
    assert reading.watts is None


def test_session_reads_repeatedly_on_one_connection(
    monkeypatch, paired: EveOutletConfig
) -> None:
    """The whole point of session(): sample without paying a cold connect each time."""
    pairing = _FakePairing(_metering_accessories())
    controller = _FakeController(pairing)
    calls = _patch_connected(monkeypatch, controller)
    import asyncio

    async def _drive() -> list[float | None]:
        readings = []
        async with EveOutlet(paired).session() as eve:
            for watts in (120.0, 60.0, 1.2):
                pairing.values = {(1, 9): {"value": True}, (1, 21): {"value": watts}}
                readings.append((await eve.read()).watts)
            await eve.set(False)
        return readings

    assert asyncio.run(_drive()) == [120.0, 60.0, 1.2]
    assert len(calls) == 1, "expected a single connect for the whole session"
    assert pairing.written == [(1, 9, False)]
    assert controller.stopped is True


def test_characteristics_dump_includes_values(
    monkeypatch, paired: EveOutletConfig
) -> None:
    """The diagnostic must surface the type and value of every readable char."""
    pairing = _FakePairing(_metering_accessories())
    pairing.values = {(1, 8): {"value": "Eve"}, (1, 21): {"value": 42.0}}
    _patch_connected(monkeypatch, _FakeController(pairing))
    import asyncio

    rows = asyncio.run(EveOutlet(paired).characteristics())
    by_iid = {r["iid"]: r for r in rows}
    assert by_iid[21]["type"].lower().startswith("e863f10d")
    assert by_iid[21]["value"] == pytest.approx(42.0)
    assert by_iid[8]["value"] == "Eve"


def test_missing_pairing_file_raises(tmp_path: Path) -> None:
    cfg = EveOutletConfig(enabled=True, pairing_file=str(tmp_path / "absent.json"))
    import asyncio

    with pytest.raises(EveError, match="no pairing data"):
        asyncio.run(EveOutlet(cfg).set(True))


def test_no_on_characteristic_raises(monkeypatch, paired: EveOutletConfig) -> None:
    pairing = _FakePairing([{"aid": 1, "services": [{"characteristics": []}]}])
    _patch_connected(monkeypatch, _FakeController(pairing))
    import asyncio

    with pytest.raises(EveError, match="no On characteristic"):
        asyncio.run(EveOutlet(paired).set(True))
