"""The shipped example config, which is what every install starts from.

`load_config` filters each block down to the keys its dataclass actually has, so
that an unrelated or stale key in someone's file does not stop the bridge
booting. The cost of that tolerance is that a typo in the file we *ship* is
silent: the key is dropped, the default stands, and the setting the user thought
they had set simply never applies. Nothing surfaces it -- not at load, not in
the log, not on the dashboard.

So the example is checked here against the same dataclasses, key by key.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from ecoflow_nut.config import (
    AutoShutdownConfig,
    BleConfig,
    Config,
    EcoflowConfig,
    EveOutletConfig,
    LoggingConfig,
    MqttConfig,
    NutConfig,
    NutStaticValues,
    NutThresholds,
    PostgresConfig,
    PricingConfig,
    SqliteConfig,
    SwitchBotConfig,
    WebConfig,
    load_config,
)

EXAMPLES = sorted(
    (Path(__file__).resolve().parents[1] / "config").glob("config*.yaml")
)
# config.unknown-device.yaml is a diagnostic config -- 'model: raw' connects and
# dumps frames rather than bridging -- so it has no NUT device and nothing to
# publish. Anything about running a bridge is asked of the others only.
BRIDGE_EXAMPLES = [
    p for p in EXAMPLES
    if "raw" not in (yaml.safe_load(p.read_text()) or {}).get("ecoflow", {}).get(
        "model", ""
    )
]

# Every top-level block the loader maps onto a dataclass. `nut` carries two
# nested blocks of its own, handled below.
BLOCKS: dict[str, type] = {
    "ecoflow": EcoflowConfig,
    "ble": BleConfig,
    "nut": NutConfig,
    "logging": LoggingConfig,
    "auto_shutdown": AutoShutdownConfig,
    "eve": EveOutletConfig,
    "switchbot": SwitchBotConfig,
    "web": WebConfig,
    "postgres": PostgresConfig,
    "sqlite": SqliteConfig,
    "pricing": PricingConfig,
    "mqtt": MqttConfig,
}


def _names(cls: type) -> set[str]:
    return {f.name for f in fields(cls)}


def test_there_is_an_example_to_copy() -> None:
    assert EXAMPLES, "config/config*.yaml is what the README tells people to copy"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_every_key_in_the_example_is_a_key_the_loader_reads(path: Path) -> None:
    raw = yaml.safe_load(path.read_text()) or {}

    unknown_blocks = sorted(
        set(raw) - set(BLOCKS) - _names(Config) - {"control_socket_path"}
    )
    assert not unknown_blocks, f"{path.name} has blocks nothing reads: {unknown_blocks}"

    for block, cls in BLOCKS.items():
        section = raw.get(block)
        if section is None:
            continue
        allowed = _names(cls)
        if block == "nut":
            # These two are lifted out and parsed against their own dataclasses.
            allowed |= {"thresholds", "static_values"}
        stray = sorted(set(section) - allowed)
        assert not stray, f"{path.name}: {block} sets keys nothing reads: {stray}"

    nut = raw.get("nut") or {}
    nested_blocks = (("thresholds", NutThresholds), ("static_values", NutStaticValues))
    for nested, cls in nested_blocks:
        stray = sorted(set(nut.get(nested) or {}) - _names(cls))
        assert not stray, f"{path.name}: nut.{nested} sets keys nothing reads: {stray}"


def _as_filled_in(path: Path, tmp_path: Path) -> Path:
    """The example with its placeholders replaced, the way a user edits it.

    The loader rejects the shipped placeholders on purpose -- a config left
    unedited must fail at the cause rather than as "device not found during
    scan" -- so the file cannot be loaded verbatim. Substituting instead of
    disabling that check keeps the real validator in the path.
    """
    text = (
        path.read_text()
        .replace("AA:BB:CC:DD:EE:FF", "DC:06:75:A8:3E:29")
        .replace("REPLACE-WITH-FULL-SERIAL", "E201ZE1APH560861")
        .replace("REPLACE-WITH-USER-ID", "1234567890")
        .replace("E201XXXXXXXXXXXX", "E201ZE1APH560861")
        .replace("R351XXXXXXXXXXXX", "R351ZE1APH560861")
    )
    out = tmp_path / path.name
    out.write_text(text)
    return out


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_the_example_loads_once_it_is_filled_in(path: Path, tmp_path: Path) -> None:
    assert load_config(_as_filled_in(path, tmp_path)).nut.device_name


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_the_example_is_rejected_while_still_a_placeholder(path: Path) -> None:
    """Left unedited it must fail loudly, naming the field it wants."""
    with pytest.raises(ValueError, match="placeholder|XXXX"):
        load_config(path)


@pytest.mark.parametrize("path", BRIDGE_EXAMPLES, ids=lambda p: p.name)
def test_the_example_offers_home_assistant(path: Path, tmp_path: Path) -> None:
    """A block you have to learn about from the README is a block nobody enables.

    And off by default: the bridge must not try to reach a broker nobody
    configured just because the example mentions one.
    """
    config = load_config(_as_filled_in(path, tmp_path))
    assert "mqtt:" in path.read_text()
    assert config.mqtt.enabled is False
    # Entities in HA are sensor.<device_id>_<field>, so this default is what
    # everyone's entity ids are actually named.
    assert config.mqtt.device_id == config.nut.device_name
