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
from ecoflow_nut.eve_outlet import EveError, EveOutlet, _is_on_char

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
def test_is_on_char_matches(value: str) -> None:
    assert _is_on_char(value) is True


@pytest.mark.parametrize("value", ["26", "name", ""])
def test_is_on_char_rejects(value: str) -> None:
    assert _is_on_char(value) is False


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


def _patch_connected(monkeypatch, controller) -> None:
    """Bypass the real bleak scan + seed; hand back a fake started controller."""

    async def _conn(adapter, device_id, timeout):
        controller.started = True
        return controller

    monkeypatch.setattr(eve_outlet, "_connected_controller", _conn)


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
    assert outlet._on_aid_iid == (1, 9)


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
