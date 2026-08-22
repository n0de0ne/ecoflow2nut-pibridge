"""Home Assistant discovery payloads.

These are a contract with software we cannot run here: HA reads them once, at
its own startup, and a malformed one fails quietly -- an entity simply never
appears, or appears permanently unavailable, with the error in HA's log rather
than the bridge's. So the shape is pinned.
"""

from __future__ import annotations

import json

import pytest

from ecoflow_nut.config import MqttConfig
from ecoflow_nut.mqtt import BINARY_SENSORS, SENSORS, SWITCHES, MqttPublisher


def _publisher(**kw: object) -> MqttPublisher:
    config = MqttConfig(enabled=True, device_id="ecoflow", **kw)  # type: ignore[arg-type]
    return MqttPublisher(config, device_model="E2000", device_serial="E201ZE1APH560861")


def test_every_entity_is_announced() -> None:
    payloads = _publisher().discovery_payloads()
    assert len(payloads) == len(SENSORS) + len(BINARY_SENSORS) + len(SWITCHES)


def test_discovery_topics_follow_ha_s_schema() -> None:
    """<prefix>/<component>/<node>/<object>/config, or HA ignores them outright."""
    for topic, _ in _publisher().discovery_payloads():
        parts = topic.split("/")
        assert parts[0] == "homeassistant"
        assert parts[1] in {"sensor", "binary_sensor", "switch"}
        assert parts[-1] == "config"
        assert len(parts) == 5


def test_a_custom_discovery_prefix_is_honoured() -> None:
    payloads = _publisher(discovery_prefix="ha").discovery_payloads()
    assert all(topic.startswith("ha/") for topic, _ in payloads)


def test_every_entity_carries_a_unique_id_and_one_device() -> None:
    """Without unique_id HA refuses to let you rename or group the entity, and a
    differing device block splits one station into several devices."""
    payloads = _publisher().discovery_payloads()
    ids = [cfg["unique_id"] for _, cfg in payloads]
    assert len(set(ids)) == len(ids), "duplicate unique_id merges two entities into one"

    devices = {json.dumps(cfg["device"], sort_keys=True) for _, cfg in payloads}
    assert len(devices) == 1, "every entity must hang off the same device block"


def test_entities_share_one_state_topic_and_an_availability_topic() -> None:
    pub = _publisher()
    for _, cfg in pub.discovery_payloads():
        assert cfg["state_topic"] == pub.state_topic
        assert cfg["availability_topic"] == pub.availability_topic


def test_a_missing_field_renders_as_unknown_rather_than_the_string_none() -> None:
    """Models differ in what they report; solar is absent on a station without PV.

    Without the guard HA receives the literal "None", which fails a numeric
    sensor's parse and leaves it unavailable with the reason buried in HA's log.
    """
    for _, cfg in _publisher().discovery_payloads():
        template = cfg["value_template"]
        assert "is defined" in template and "is not none" in template


def test_switches_command_on_their_own_topic() -> None:
    pub = _publisher()
    switches = [cfg for topic, cfg in pub.discovery_payloads() if "/switch/" in topic]
    assert {c["command_topic"] for c in switches} == {
        pub.command_topic(spec["output"]) for spec in SWITCHES
    }
    for cfg in switches:
        # Reflect the device's real state rather than assuming the command took.
        assert cfg["optimistic"] is False
        assert cfg["state_on"] == "true" and cfg["state_off"] == "false"


def test_binary_sensors_read_json_booleans() -> None:
    """JSON gives `true`; HA compares against payload_on as text, so lower-case."""
    pub = _publisher()
    for topic, cfg in pub.discovery_payloads():
        if "/binary_sensor/" in topic:
            assert cfg["payload_on"] == "true" and cfg["payload_off"] == "false"
            assert "| lower" in cfg["value_template"]


def test_power_sensors_are_typed_for_the_energy_dashboard() -> None:
    """device_class + state_class is what lets HA integrate watts into kWh."""
    by_key = {s["key"]: s for s in SENSORS}
    for key in ("ac_input_watts", "solar_input_watts", "battery_watts"):
        assert by_key[key]["device_class"] == "power"
        assert by_key[key]["state_class"] == "measurement"
        assert by_key[key]["unit_of_measurement"] == "W"


def test_the_status_string_is_not_a_measurement() -> None:
    """A state_class on a text sensor makes HA's recorder reject every sample."""
    status = next(s for s in SENSORS if s["key"] == "status")
    assert "state_class" not in status
    assert "unit_of_measurement" not in status


def test_topics_are_namespaced_by_device() -> None:
    """Two stations on one broker must not overwrite each other."""
    a = MqttPublisher(
        MqttConfig(device_id="garage"), device_model="E2000", device_serial="x"
    )
    b = MqttPublisher(
        MqttConfig(device_id="office"), device_model="E2000", device_serial="y"
    )
    assert a.state_topic != b.state_topic
    assert a.availability_topic != b.availability_topic
    assert a.command_topic("ac") != b.command_topic("ac")


def test_publish_never_blocks_the_caller() -> None:
    """It runs on the decode path; the connection task does the writing."""
    pub = _publisher()
    pub.publish({"soc_percent": 93})
    assert pub._latest == {"soc_percent": 93}


ANNOUNCED_KEYS = sorted(
    {s["key"] for s in SENSORS}
    | {s["key"] for s in BINARY_SENSORS}
    | {s["key"] for s in SWITCHES}
)


@pytest.mark.parametrize("key", ANNOUNCED_KEYS)
def test_every_announced_key_exists_in_the_state_payload(key: str, tmp_path) -> None:
    """An entity pointing at a field the bridge never sends is silently unknown.

    Nothing errors: HA creates the entity, the template finds no such field, and
    it sits at "unknown" forever with no clue why. Cheaper to catch here.
    """
    from ecoflow_nut.config import Config, EcoflowConfig
    from ecoflow_nut.main import Daemon
    from ecoflow_nut.state import DeviceState

    config = Config(
        ecoflow=EcoflowConfig(
            mac="DC:06:75:A8:3E:29", serial="E201ZE1APH560861", model="e2000"
        )
    )
    config.nut.dev_file_path = str(tmp_path / "ecoflow.dev")
    config.settings_file = str(tmp_path / "settings.json")
    daemon = Daemon(config)
    daemon._latest_state = DeviceState(soc_percent=50)
    assert key in daemon._web_state()
