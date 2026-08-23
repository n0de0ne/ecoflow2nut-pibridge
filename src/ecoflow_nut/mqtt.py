"""Publish telemetry to MQTT with Home Assistant discovery.

The bridge announces itself once as a single HA *device* carrying every sensor
and switch, then publishes one retained JSON message per update. Every entity
reads that same topic through a ``value_template``, so a reading costs one
message rather than one per entity, and HA sees current values the instant it
subscribes rather than waiting for the next frame.

``aiomqtt`` is an optional dependency (``pip install ecoflow-nut-bridge[mqtt]``)
imported lazily, so the bridge runs without it unless MQTT is switched on.

Availability is a retained last-will on ``<base>/<device>/status``: the broker
publishes "offline" on our behalf if the bridge dies, so HA marks the entities
unavailable instead of showing a frozen reading forever.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog

from .config import MqttConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

log = structlog.get_logger(__name__)

# Seconds between reconnect attempts. The broker is not on the critical path --
# NUT and the dashboard carry on regardless -- so this only has to be patient.
RECONNECT_SECONDS = 10


def _template(key: str, *, lower: bool = False) -> str:
    """Jinja that yields the field, or nothing where the model omits it.

    Without the `is defined` guard HA renders a missing field as the literal
    string "None" and a numeric sensor goes unavailable with a parse error;
    with it, the entity is simply unknown, which is the truth.

    Concatenated rather than formatted: every brace here is Jinja's, so an
    f-string would need each one doubled and the result would be unreadable.
    """
    value = "value_json." + key
    rendered = value + " | lower" if lower else value
    return (
        "{{ " + rendered + " if " + value + " is defined and "
        + value + " is not none else none }}"
    )


def _sensor(
    key: str,
    name: str,
    *,
    unit: str | None = None,
    device_class: str | None = None,
    state_class: str | None = "measurement",
    icon: str | None = None,
) -> dict[str, Any]:
    entity: dict[str, Any] = {"key": key, "name": name, "component": "sensor"}
    if unit:
        entity["unit_of_measurement"] = unit
    if device_class:
        entity["device_class"] = device_class
    if state_class:
        entity["state_class"] = state_class
    if icon:
        entity["icon"] = icon
    return entity


# What the bridge exposes to HA. `key` is the field in the state payload; a
# power/energy device_class is what lets HA's own statistics and the Energy
# dashboard pick these up rather than treating them as anonymous numbers.
SENSORS: tuple[dict[str, Any], ...] = (
    _sensor("soc_percent", "State of charge", unit="%", device_class="battery"),
    _sensor("ac_input_watts", "Grid input", unit="W", device_class="power"),
    _sensor("solar_input_watts", "Solar input", unit="W", device_class="power"),
    _sensor("ac_output_watts", "AC output", unit="W", device_class="power"),
    _sensor("dc_output_watts", "12V DC output", unit="W", device_class="power"),
    _sensor("usb_output_watts", "USB-A output", unit="W", device_class="power"),
    _sensor("usbc_output_watts", "USB-C output", unit="W", device_class="power"),
    _sensor("input_watts", "Total input", unit="W", device_class="power"),
    _sensor("output_watts", "Total output", unit="W", device_class="power"),
    # Signed, so HA charts it either side of zero: positive charging.
    _sensor("battery_watts", "Battery power", unit="W", device_class="power"),
    _sensor("ac_input_voltage", "Grid voltage", unit="V", device_class="voltage"),
    _sensor(
        "runtime_seconds", "Runtime remaining", unit="s", device_class="duration"
    ),
    _sensor(
        "remain_charge_minutes",
        "Time to full",
        unit="min",
        device_class="duration",
        icon="mdi:battery-clock",
    ),
    _sensor(
        "remain_discharge_minutes",
        "Time to empty",
        unit="min",
        device_class="duration",
        icon="mdi:battery-clock-outline",
    ),
    # Not a measurement: a string that changes state, so no state_class.
    _sensor("status", "UPS status", state_class=None, icon="mdi:power-plug"),
    # Pack health. Temperature first: it is the one HA users set automations on.
    _sensor(
        "battery_temp_c", "Battery temperature", unit="°C", device_class="temperature"
    ),
    _sensor(
        "inverter_temp_c", "Inverter temperature", unit="°C", device_class="temperature"
    ),
    # Cycles only ever climbs, so total_increasing lets HA chart it as a
    # lifetime counter instead of a noisy measurement.
    _sensor(
        "battery_cycles",
        "Battery cycles",
        state_class="total_increasing",
        icon="mdi:battery-sync",
    ),
    _sensor(
        "battery_soh_percent",
        "Battery health",
        unit="%",
        icon="mdi:battery-heart-variant",
    ),
    _sensor(
        "cell_mv_spread",
        "Cell voltage spread",
        unit="mV",
        device_class="voltage",
        icon="mdi:battery-alert-variant-outline",
    ),
    # Lifetime energy, for HA's Energy dashboard. device_class "energy" plus
    # state_class "total_increasing" is the exact pair the dashboard requires:
    # it wants a monotonic total it can difference, not a power reading it has
    # to integrate -- an integration would only count the hours HA was up and
    # listening, and would restart from zero whenever the helper was recreated.
    # These odometers live on the station, so a bridge restart costs nothing.
    _sensor(
        "solar_energy_wh",
        "Solar energy",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _sensor(
        "grid_energy_wh",
        "Grid energy in",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _sensor(
        "ac_output_energy_wh",
        "AC output energy",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _sensor(
        "dc_output_energy_wh",
        "12V DC output energy",
        unit="Wh",
        device_class="energy",
        state_class="total_increasing",
    ),
)

BINARY_SENSORS: tuple[dict[str, Any], ...] = (
    {
        "key": "ac_input_present",
        "name": "Grid present",
        "component": "binary_sensor",
        "device_class": "plug",
    },
    {
        "key": "ac_output_on",
        "name": "AC output active",
        "component": "binary_sensor",
        "device_class": "power",
    },
    {
        "key": "dc_output_on",
        "name": "12V DC active",
        "component": "binary_sensor",
        "device_class": "power",
    },
    {
        "key": "fan_on",
        "name": "Fan running",
        "component": "binary_sensor",
        "device_class": "running",
    },
)

# Switches carry a command topic. `output` is what the daemon's control callback
# receives, matching the CLI's own vocabulary.
SWITCHES: tuple[dict[str, Any], ...] = (
    {"key": "ac_output_on", "output": "ac", "name": "AC output"},
    {"key": "usb_output_on", "output": "usb", "name": "USB output"},
    {"key": "dc_output_on", "output": "dc", "name": "12V DC output"},
)


class MqttPublisher:
    """Owns the broker connection, the discovery announcement and state pushes."""

    def __init__(
        self,
        config: MqttConfig,
        *,
        device_model: str,
        device_serial: str,
        control: Callable[[str, bool], Awaitable[str]] | None = None,
    ) -> None:
        self._config = config
        self._model = device_model
        self._serial = device_serial
        self._control = control
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None
        self._latest: dict[str, Any] | None = None
        self._connected = asyncio.Event()

    # -- topics ------------------------------------------------------------- #
    @property
    def _root(self) -> str:
        return f"{self._config.base_topic}/{self._config.device_id}"

    @property
    def state_topic(self) -> str:
        return f"{self._root}/state"

    @property
    def availability_topic(self) -> str:
        return f"{self._root}/status"

    def command_topic(self, output: str) -> str:
        return f"{self._root}/set/{output}"

    def _device_block(self) -> dict[str, Any]:
        return {
            "identifiers": [self._config.device_id],
            "name": self._model or self._config.device_id,
            "manufacturer": "EcoFlow",
            "model": self._model,
            "serial_number": self._serial,
            "via_device": self._config.device_id,
        }

    def discovery_payloads(self) -> list[tuple[str, dict[str, Any]]]:
        """(topic, config) for every entity, as HA's discovery schema wants it."""
        out: list[tuple[str, dict[str, Any]]] = []
        device = self._device_block()
        prefix = self._config.discovery_prefix
        dev_id = self._config.device_id

        def base(unique: str, name: str) -> dict[str, Any]:
            return {
                "name": name,
                "unique_id": f"{dev_id}_{unique}",
                "object_id": f"{dev_id}_{unique}",
                "state_topic": self.state_topic,
                "availability_topic": self.availability_topic,
                "device": device,
            }

        for spec in SENSORS:
            key = spec["key"]
            cfg = base(key, spec["name"])
            cfg["value_template"] = _template(key)
            for extra in (
                "unit_of_measurement",
                "device_class",
                "state_class",
                "icon",
            ):
                if extra in spec:
                    cfg[extra] = spec[extra]
            out.append((f"{prefix}/sensor/{dev_id}/{key}/config", cfg))

        for spec in BINARY_SENSORS:
            key = spec["key"]
            cfg = base(key, spec["name"])
            cfg["device_class"] = spec["device_class"]
            cfg["payload_on"] = "true"
            cfg["payload_off"] = "false"
            cfg["value_template"] = _template(key, lower=True)
            out.append((f"{prefix}/binary_sensor/{dev_id}/{key}/config", cfg))

        for spec in SWITCHES:
            key, output = spec["key"], spec["output"]
            cfg = base(f"switch_{output}", spec["name"])
            cfg["command_topic"] = self.command_topic(output)
            cfg["payload_on"] = "ON"
            cfg["payload_off"] = "OFF"
            cfg["state_on"] = "true"
            cfg["state_off"] = "false"
            cfg["optimistic"] = False
            cfg["value_template"] = _template(key, lower=True)
            out.append((f"{prefix}/switch/{dev_id}/{output}/config", cfg))

        return out

    # -- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def publish(self, payload: dict[str, Any]) -> None:
        """Hand the latest reading over. Never blocks the caller.

        Called from the decode path, so it only records the snapshot; the
        connection task does the writing. A broker that is slow, wedged or
        missing therefore cannot hold up telemetry.
        """
        self._latest = payload

    async def _run(self) -> None:
        """Connect, announce, then publish until cancelled -- retrying forever."""
        try:
            import aiomqtt
        except ImportError:
            log.error(
                "mqtt.unavailable",
                note="install the optional extra: pip install ecoflow-nut-bridge[mqtt]",
            )
            return

        cfg = self._config
        while True:
            try:
                will = aiomqtt.Will(
                    topic=self.availability_topic,
                    payload=b"offline",
                    qos=1,
                    retain=True,
                )
                async with aiomqtt.Client(
                    hostname=cfg.host,
                    port=cfg.port,
                    username=cfg.username or None,
                    password=cfg.password or None,
                    identifier=cfg.client_id,
                    keepalive=cfg.keepalive_seconds,
                    tls_params=aiomqtt.TLSParameters() if cfg.tls else None,
                    will=will,
                ) as client:
                    self._client = client
                    log.info("mqtt.connected", host=cfg.host, port=cfg.port)
                    await self._announce(client)
                    await asyncio.gather(
                        self._publish_loop(client),
                        self._command_loop(client),
                    )
            except asyncio.CancelledError:
                await self._say_goodbye()
                raise
            except Exception as exc:  # noqa: BLE001 - the broker is optional
                log.warning(
                    "mqtt.disconnected",
                    error=str(exc),
                    retry_seconds=RECONNECT_SECONDS,
                )
            finally:
                self._client = None
                self._connected.clear()
            await asyncio.sleep(RECONNECT_SECONDS)

    async def _announce(self, client: Any) -> None:
        """Publish discovery configs, then go online.

        Retained, so HA rebuilds the device from the broker after a restart
        without waiting for the bridge to reconnect.
        """
        for topic, payload in self.discovery_payloads():
            await client.publish(topic, json.dumps(payload), qos=1, retain=True)
        await client.publish(self.availability_topic, "online", qos=1, retain=True)
        for spec in SWITCHES:
            await client.subscribe(self.command_topic(spec["output"]), qos=1)
        self._connected.set()
        log.info("mqtt.announced", entities=len(self.discovery_payloads()))

    async def _publish_loop(self, client: Any) -> None:
        """Push the newest snapshot on a fixed cadence.

        Deliberately not on every decoded frame: HA coalesces nothing, and a
        station streaming five heartbeats a second would fill its recorder with
        near-identical rows.
        """
        last: str | None = None
        while True:
            if self._latest is not None:
                body = json.dumps(self._latest, default=str)
                if body != last:
                    await client.publish(self.state_topic, body, qos=0, retain=True)
                    last = body
            await asyncio.sleep(2)

    async def _command_loop(self, client: Any) -> None:
        """Act on switch commands from HA."""
        async for message in client.messages:
            topic = str(message.topic)
            output = topic.rsplit("/", 1)[-1]
            wanted = message.payload.decode(errors="replace").strip().upper() == "ON"
            log.info("mqtt.command", output=output, on=wanted)
            if self._control is None:
                continue
            try:
                await self._control(output, wanted)
            except Exception as exc:  # noqa: BLE001 - a bad command is not fatal
                log.error("mqtt.command_failed", output=output, error=str(exc))

    async def _say_goodbye(self) -> None:
        """Mark ourselves offline on a clean shutdown, rather than leaving it to
        the broker's will -- HA then updates immediately instead of after the
        keepalive expires."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await asyncio.wait_for(
                client.publish(self.availability_topic, "offline", qos=1, retain=True),
                timeout=2,
            )
        except Exception:  # noqa: BLE001 - we are shutting down regardless
            pass
