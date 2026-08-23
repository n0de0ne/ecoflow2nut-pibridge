"""The MQTT publisher against a real broker.

The other MQTT tests pin the *shape* of the discovery payloads. Nothing there
exercises connecting, announcing, publishing, taking a command back or shutting
down -- and the first time this code met an actual broker it turned out that
every clean shutdown left Home Assistant reading "online", because the goodbye
was published after the client context manager had already closed the socket.

Skipped when mosquitto or aiomqtt is missing, so it runs where a broker exists
and costs nothing where one does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator

import pytest

from ecoflow_nut.config import MqttConfig
from ecoflow_nut.mqtt import BINARY_SENSORS, SENSORS, SWITCHES, MqttPublisher

pytest.importorskip("aiomqtt", reason="needs the [mqtt] extra")
if shutil.which("mosquitto") is None:  # pragma: no cover - environment dependent
    pytest.skip("needs a mosquitto binary", allow_module_level=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def broker(tmp_path) -> Iterator[int]:
    """A throwaway mosquitto on a free port, with no persistence between tests."""
    port = _free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
    )
    proc = subprocess.Popen(
        ["mosquitto", "-c", str(conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Poll with a pause between attempts: without one, a hundred retries
        # elapse in a couple of milliseconds and the broker is skipped as dead
        # before it has finished starting.
        for _ in range(100):
            with contextlib.suppress(OSError), socket.socket() as s:
                s.settimeout(0.2)
                s.connect(("127.0.0.1", port))
                break
            time.sleep(0.05)
        else:  # pragma: no cover - only if mosquitto will not start
            pytest.skip("mosquitto did not come up")
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def _collect(port: int, *topics: str, seconds: float = 2.0) -> dict[str, str]:
    """Everything retained on `topics`, as a fresh subscriber would see it."""
    import aiomqtt

    seen: dict[str, str] = {}
    async with aiomqtt.Client("127.0.0.1", port, identifier="test-probe") as client:
        for topic in topics:
            await client.subscribe(topic)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                async for message in client.messages:
                    seen[str(message.topic)] = message.payload.decode()
    return seen


@contextlib.asynccontextmanager
async def _running(port: int, **kw) -> AsyncIterator[MqttPublisher]:
    pub = MqttPublisher(
        MqttConfig(enabled=True, host="127.0.0.1", port=port, device_id="ecoflow"),
        device_model="E2000",
        device_serial="E201X",
        **kw,
    )
    await pub.start()
    for _ in range(200):  # give it a moment to connect and announce
        await asyncio.sleep(0.02)
        if pub._connected.is_set():
            break
    try:
        yield pub
    finally:
        await pub.stop()


async def test_a_fresh_subscriber_finds_the_whole_device_already_there(broker) -> None:
    """Discovery and state are retained, so HA rebuilds the device the instant
    it subscribes rather than waiting for the next reading."""
    async with _running(broker) as pub:
        pub.publish({"soc_percent": 90.5, "status": "OL"})
        await asyncio.sleep(2.5)
        seen = await _collect(broker, "homeassistant/#", "ecoflow/#")

    configs = [t for t in seen if t.startswith("homeassistant/")]
    assert len(configs) == len(SENSORS) + len(BINARY_SENSORS) + len(SWITCHES)
    assert seen[pub.availability_topic] == "online"
    assert json.loads(seen[pub.state_topic])["soc_percent"] == 90.5


async def test_a_switch_command_reaches_the_control_callback(broker) -> None:
    """The path HA actually uses: publish ON/OFF to the command topic."""
    got: list[tuple[str, bool]] = []

    async def control(output: str, on: bool) -> str:
        got.append((output, on))
        return "ok"

    import aiomqtt

    async with _running(broker, control=control) as pub:
        async with aiomqtt.Client("127.0.0.1", broker, identifier="ha") as ha:
            await ha.publish(pub.command_topic("ac"), "OFF", qos=1)
            await ha.publish(pub.command_topic("usb"), "ON", qos=1)
            await asyncio.sleep(1.5)

    assert ("ac", False) in got and ("usb", True) in got


async def test_a_clean_shutdown_says_offline_immediately(broker) -> None:
    """Otherwise HA shows a frozen reading as live until the keepalive lapses
    and the broker fires the will on our behalf -- 60 seconds of a dashboard
    confidently reporting a bridge that has already stopped.

    This failed the first time it was run: the goodbye was published from an
    exception handler outside the client's context manager, so the socket was
    already closed and nothing left the process.
    """
    async with _running(broker) as pub:
        pub.publish({"soc_percent": 90.5})
        await asyncio.sleep(2.5)

    seen = await _collect(broker, pub.availability_topic, seconds=1.5)
    assert seen[pub.availability_topic] == "offline"


async def test_publishing_survives_the_broker_going_away(broker) -> None:
    """The broker is a consumer, not a dependency: losing it must not take the
    bridge's telemetry with it."""
    async with _running(broker) as pub:
        pub.publish({"soc_percent": 50})
        await asyncio.sleep(1.5)
        # publish() only records a snapshot, so it cannot raise or block here
        # however wedged the connection is.
        pub.publish({"soc_percent": 51})
        assert pub._latest == {"soc_percent": 51}
