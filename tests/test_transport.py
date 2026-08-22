"""Frame reassembly tests for both EcoFlow wire generations.

BLE notifications are a byte stream, not message boundaries: a frame can be
split across notifications and several can arrive together. Reassembly must
therefore work from the frame header alone -- and the V2 frames used by the
DELTA 2 generation carry no dsrc/ddst, so they are two bytes shorter than the
V3 frames of the DELTA 3 generation.
"""

import asyncio
from dataclasses import fields

import pytest

from ecoflow_nut import protocol
from ecoflow_nut.ble_client import PassthroughAssembler
from ecoflow_nut.protocol import Packet, PacketError, PacketV4


def _frame(version: int, payload: bytes) -> bytes:
    return Packet(
        src=0x03, dst=0x21, cmd_set=0x20, cmd_id=0x02, payload=payload, version=version
    ).to_bytes()


@pytest.mark.parametrize("version", [2, 3])
def test_single_frame_reassembles(version):
    frame = _frame(version, bytes(range(40)))
    assert PassthroughAssembler().reassemble(frame) == [frame]


@pytest.mark.parametrize("version", [2, 3])
def test_frames_of_both_versions_have_the_expected_length(version):
    """V2 payloads start at byte 16, V3 at 18; both carry a trailing CRC16."""
    payload = bytes(20)
    overhead = 16 if version == 2 else 18
    assert len(_frame(version, payload)) == overhead + len(payload) + 2


@pytest.mark.parametrize("version", [2, 3])
def test_back_to_back_frames_split_correctly(version):
    first = _frame(version, b"\x01" * 10)
    second = _frame(version, b"\x02" * 30)
    assert PassthroughAssembler().reassemble(first + second) == [first, second]


@pytest.mark.parametrize("version", [2, 3])
def test_frame_split_across_notifications_is_buffered(version):
    frame = _frame(version, bytes(range(50)))
    assembler = PassthroughAssembler()
    assert assembler.reassemble(frame[:17]) == []
    assert assembler.reassemble(frame[17:]) == [frame]


def test_mixed_versions_in_one_stream():
    """A misjudged header length would consume into the next frame."""
    v2 = _frame(2, b"\xaa" * 12)
    v3 = _frame(3, b"\xbb" * 12)
    assert PassthroughAssembler().reassemble(v2 + v3) == [v2, v3]


def test_resyncs_past_a_stray_prefix_byte():
    frame = _frame(2, b"\x01\x02\x03")
    assert PassthroughAssembler().reassemble(b"\xaa\xaa" + frame) == [frame]


@pytest.mark.parametrize("version", [2, 3])
def test_reassembled_frames_parse_back_to_the_original_packet(version):
    payload = bytes(range(24))
    frame = _frame(version, payload)
    (raw,) = PassthroughAssembler().reassemble(frame)
    packet = Packet.from_bytes(raw)
    assert (packet.src, packet.cmd_set, packet.cmd_id) == (0x03, 0x20, 0x02)
    assert packet.payload == payload


# --------------------------------------------------------------------------- #
# V4 framing
# --------------------------------------------------------------------------- #
def _v4(payload: bytes, **kw) -> PacketV4:
    return PacketV4(src=0x03, dst=0x21, cmd_set=0x20, cmd_id=0x02, payload=payload, **kw)


@pytest.mark.parametrize("v4_type_b", [0x00, 0x5A, 0xC3])
def test_v4_roundtrip_including_the_second_xor_layer(v4_type_b):
    """V4 obfuscates twice: everything past the header with the CRC8 byte, then
    the payload again with v4_type_b when it is non-zero."""
    packet = _v4(bytes(range(40)), v4_type_b=v4_type_b)
    decoded = PacketV4.from_bytes(packet.to_bytes())
    assert (decoded.src, decoded.dst) == (0x03, 0x21)
    assert (decoded.cmd_set, decoded.cmd_id) == (0x20, 0x02)
    assert decoded.payload == bytes(range(40))
    assert decoded.v4_type_b == v4_type_b


def test_v4_addressing_is_not_readable_as_plaintext():
    """The inner header is obfuscated, so a V2/V3 parser cannot find src/dst."""
    raw = _v4(b"\x00" * 8, v4_type_b=0x11).to_bytes()
    # Bytes 12/13 are where V2/V3 keep src/dst; in V4 they are XOR'd payload.
    assert (raw[12], raw[13]) != (0x03, 0x21)


def test_v4_length_and_dispatch():
    raw = _v4(bytes(12)).to_bytes()
    # V4 counts the 8-byte inner header inside its length field.
    assert protocol.frame_length(raw) == len(raw) == 8 + (8 + 12) + 2
    packet = protocol.parse_frame(raw)
    assert packet.version == protocol.V4_VERSION
    assert (packet.src, packet.cmd_set, packet.cmd_id) == (0x03, 0x20, 0x02)


def test_v4_rejects_corrupt_frames():
    raw = bytearray(_v4(bytes(8)).to_bytes())
    raw[-1] ^= 0xFF
    with pytest.raises(PacketError):
        PacketV4.from_bytes(bytes(raw))


def test_v4_frames_reassemble_from_a_byte_stream():
    first, second = _v4(b"\x01" * 10).to_bytes(), _v4(b"\x02" * 20).to_bytes()
    assert PassthroughAssembler().reassemble(first + second) == [first, second]


def test_v4_constants_are_not_dataclass_fields():
    """HEADER_LEN/INNER_LEN must stay ClassVars, not per-instance values."""
    assert "HEADER_LEN" not in {f.name for f in fields(PacketV4)}
    assert "INNER_LEN" not in {f.name for f in fields(PacketV4)}


def test_duplicate_notifications_are_dropped():
    """BlueZ can deliver the same notification twice, ~1 ms apart.

    Observed on a real E2000: every frame logged twice with identical bytes.
    Merges are idempotent, but we answer recognised frames, and replying twice
    doubles write traffic on an already chatty link.
    """
    client = _client_for_dedupe_test()
    frame = _frame(2, b"\x01" * 8)
    assert client._is_duplicate(frame) is False
    assert client._is_duplicate(frame) is True


def test_distinct_notifications_are_not_dropped():
    client = _client_for_dedupe_test()
    assert client._is_duplicate(_frame(2, b"\x01" * 8)) is False
    assert client._is_duplicate(_frame(2, b"\x02" * 8)) is False


def _client_for_dedupe_test():
    from ecoflow_nut.ble_client import EcoFlowBLE
    from ecoflow_nut.config import BleConfig, EcoflowConfig

    return EcoFlowBLE(
        EcoflowConfig(mac="DE:AD:BE:EF:00:01", serial="E201X", model="e2000"),
        BleConfig(),
    )



def test_bluez_device_path_matches_bluetoothctl():
    """The path BlueZ exposes a device at, which is what Disconnect targets."""
    from ecoflow_nut.ble_client import bluez_device_path

    assert (
        bluez_device_path("dc:06:75:a8:3e:29", "hci0")
        == "/org/bluez/hci0/dev_DC_06_75_A8_3E_29"
    )
    # An unset adapter still has to produce a usable path.
    assert bluez_device_path("DC:06:75:A8:3E:29", "").endswith("dev_DC_06_75_A8_3E_29")


async def test_a_silent_scan_takes_down_a_stale_link_and_rescans(monkeypatch):
    """BlueZ holds connections past the death of the process that made them.

    An EcoFlow serves one client at a time, so while BlueZ believes it still has
    one the station stops advertising and no scanner on the host can see it.
    Scanning again cannot help -- the link has to be dropped first, which is all
    `bluetoothctl disconnect` does and is exactly what a human had to keep
    doing by hand.
    """
    from ecoflow_nut import ble_client

    client = _client_for_dedupe_test()
    scans: list[str] = []
    cleared: list[tuple[str, str]] = []
    device = object()

    async def _find(mac: str):
        scans.append(mac)
        # Invisible until the stale link is dropped, then it advertises again.
        return (device, None) if cleared else (None, None)

    async def _clear(mac: str, adapter: str) -> bool:
        cleared.append((mac, adapter))
        return True

    monkeypatch.setattr(client, "_find_device", _find)
    monkeypatch.setattr(ble_client, "clear_stale_connection", _clear)
    monkeypatch.setattr(client, "_establish", _fake_establish)
    monkeypatch.setattr(client, "_resolve_characteristics", lambda: None)
    monkeypatch.setattr(client, "_handshake", _noop_handshake)

    await client.connect()
    assert cleared == [("DE:AD:BE:EF:00:01", "hci0")]
    assert len(scans) == 2, "rescan once the device can advertise again"


async def test_nothing_to_clear_still_reports_the_scan_failure(monkeypatch):
    """A device that is genuinely absent must fail, not retry forever."""
    from ecoflow_nut import ble_client
    from ecoflow_nut.ble_client import BleakConnectionError

    client = _client_for_dedupe_test()

    async def _find(_mac: str):
        return None, None

    async def _clear(_mac: str, _adapter: str) -> bool:
        return False

    monkeypatch.setattr(client, "_find_device", _find)
    monkeypatch.setattr(ble_client, "clear_stale_connection", _clear)

    with pytest.raises(BleakConnectionError, match="not found during scan"):
        await client.connect()


async def test_a_healthy_scan_never_touches_the_link(monkeypatch):
    """Clearing is for the stuck case only; it must not disturb a live one."""
    from ecoflow_nut import ble_client

    client = _client_for_dedupe_test()
    device = object()

    async def _find(_mac: str):
        return device, None

    async def _clear(_mac: str, _adapter: str) -> bool:  # pragma: no cover
        raise AssertionError("must not disconnect a device that is advertising")

    monkeypatch.setattr(client, "_find_device", _find)
    monkeypatch.setattr(ble_client, "clear_stale_connection", _clear)
    monkeypatch.setattr(client, "_establish", _fake_establish)
    monkeypatch.setattr(client, "_resolve_characteristics", lambda: None)
    monkeypatch.setattr(client, "_handshake", _noop_handshake)

    await client.connect()


def test_a_drop_reports_how_long_the_link_lasted():
    """Dropping every 30s and dropping once a day need different fixes."""
    import time as _time

    client = _client_for_dedupe_test()
    client._connected_monotonic = _time.monotonic() - 42
    events: list[dict] = []

    class _Log:
        def warning(self, event: str, **kw) -> None:
            events.append({"event": event, **kw})

    client_log = _Log()
    import ecoflow_nut.ble_client as mod

    original, mod.log = mod.log, client_log
    try:
        client._on_disconnect(None)
    finally:
        mod.log = original

    assert events[0]["event"] == "ble.disconnected"
    assert events[0]["link_seconds"] == pytest.approx(42, abs=1)


async def _noop_handshake() -> None:
    return None


async def _fake_establish(_device):
    return _FakeBleakClient()


class _FakeBleakClient:
    async def connect(self) -> None:
        return None


class _FakeBus:
    """Stands in for the system bus: records calls, replies as told."""

    def __init__(self, connected=None, disconnect_ok: bool = True) -> None:
        self._connected = connected
        self._disconnect_ok = disconnect_ok
        self.calls: list[str] = []
        self.closed = False

    async def connect(self):
        return self

    def disconnect(self) -> None:
        self.closed = True

    async def call(self, message):
        from dbus_fast import Message, Variant

        self.calls.append(message.member)
        message.serial = message.serial or 1  # a real bus stamps this on send
        if message.member == "Get":
            if self._connected is None:
                return _error_reply(message, "org.freedesktop.DBus.Error.UnknownObject")
            return Message.new_method_return(
                message, signature="v", body=[Variant("b", self._connected)]
            )
        if self._disconnect_ok:
            return Message.new_method_return(message)
        return _error_reply(message, "org.freedesktop.DBus.Error.AccessDenied")


def _error_reply(request, name: str):
    from dbus_fast import Message

    request.serial = request.serial or 1
    return Message.new_error(request, name, "denied")


def _install_fake_bus(monkeypatch, bus: _FakeBus) -> None:
    import dbus_fast.aio

    monkeypatch.setattr(dbus_fast.aio, "MessageBus", lambda **_kw: bus)


async def test_clearing_disconnects_a_link_bluez_is_holding(monkeypatch):
    from ecoflow_nut.ble_client import clear_stale_connection

    bus = _FakeBus(connected=True)
    _install_fake_bus(monkeypatch, bus)

    assert await clear_stale_connection("DC:06:75:A8:3E:29", "hci0") is True
    assert bus.calls == ["Get", "Disconnect"]
    assert bus.closed, "the bus must not be leaked on any path"


async def test_clearing_leaves_a_device_bluez_is_not_holding_alone(monkeypatch):
    """Then the scan found nothing for the ordinary reason: it is not there."""
    from ecoflow_nut.ble_client import clear_stale_connection

    bus = _FakeBus(connected=False)
    _install_fake_bus(monkeypatch, bus)

    assert await clear_stale_connection("DC:06:75:A8:3E:29", "hci0") is False
    assert bus.calls == ["Get"], "never Disconnect what is not connected"


async def test_clearing_a_device_bluez_has_never_seen_is_not_an_error(monkeypatch):
    from ecoflow_nut.ble_client import clear_stale_connection

    bus = _FakeBus(connected=None)
    _install_fake_bus(monkeypatch, bus)

    assert await clear_stale_connection("DC:06:75:A8:3E:29", "hci0") is False


async def test_a_refused_disconnect_is_reported_not_raised(monkeypatch):
    """Best-effort: a permission problem must degrade, not kill the reconnect."""
    from ecoflow_nut.ble_client import clear_stale_connection

    bus = _FakeBus(connected=True, disconnect_ok=False)
    _install_fake_bus(monkeypatch, bus)

    assert await clear_stale_connection("DC:06:75:A8:3E:29", "hci0") is False
    assert bus.closed


async def test_a_bus_that_will_not_connect_is_not_fatal(monkeypatch):
    import dbus_fast.aio

    from ecoflow_nut.ble_client import clear_stale_connection

    class _Broken:
        async def connect(self):
            raise OSError("no system bus")

    monkeypatch.setattr(dbus_fast.aio, "MessageBus", lambda **_kw: _Broken())
    assert await clear_stale_connection("DC:06:75:A8:3E:29", "hci0") is False


async def test_disconnect_makes_sure_bluez_agrees(monkeypatch):
    """bleak thinking the link is gone is not the same as BlueZ letting it go.

    A half-open link reads as disconnected to bleak while BlueZ still holds the
    ACL, so the bleak-level teardown is skipped -- and BlueZ keeps that link
    after the process exits. The next start then burns a whole scan timeout
    finding out, which is what made every restart need a hand.
    """
    from ecoflow_nut import ble_client

    client = _client_for_dedupe_test()
    cleared: list[tuple[str, str]] = []

    async def _clear(mac: str, adapter: str) -> bool:
        cleared.append((mac, adapter))
        return True

    monkeypatch.setattr(ble_client, "clear_stale_connection", _clear)

    await client.disconnect()  # nothing bleak-side to tear down
    assert cleared == [("DE:AD:BE:EF:00:01", "hci0")]


async def test_disconnect_survives_bluez_being_unreachable(monkeypatch):
    """Teardown must never raise: it runs in the reconnect loop's finally.

    An exception here escapes the handler whose whole job is to keep
    reconnecting, turning a transient D-Bus problem into a dead bridge.
    """
    from ecoflow_nut import ble_client

    client = _client_for_dedupe_test()

    async def _boom(_mac: str, _adapter: str) -> bool:
        raise OSError("no system bus")

    monkeypatch.setattr(ble_client, "clear_stale_connection", _boom)
    await client.disconnect()  # must not raise


def test_the_delta2_generation_does_not_echo_frames_back():
    """Five heartbeats a second, echoed, is five full-size writes a second.

    This generation broadcasts on its own timer and asks for nothing, so the
    echo buys no data and spends radio time on a link that is meant to be
    carrying telemetry one way.
    """
    from ecoflow_nut import delta2, delta3

    assert delta2.DELTA2_MAX.ack_frames is False
    assert delta2.DELTA2.ack_frames is False
    # The DELTA 3 sends one periodic frame and does expect it acknowledged.
    assert delta3.DRIVER.ack_frames is True


def test_ack_frames_can_be_forced_from_config():
    """A one-line rollback if some model does turn out to need the echo."""
    from ecoflow_nut.ble_client import EcoFlowBLE
    from ecoflow_nut.config import BleConfig, EcoflowConfig

    def _client(**kw):
        return EcoFlowBLE(
            EcoflowConfig(mac="DE:AD:BE:EF:00:01", serial="E201X", model="e2000", **kw),
            BleConfig(),
        )

    assert _client().ack_frames is False, "driver default"
    assert _client(ack_frames=True).ack_frames is True
    assert _client(ack_frames=False).ack_frames is False


async def test_no_reply_task_is_spawned_when_acks_are_off(monkeypatch):
    """The saving is real only if the write never happens."""
    client = _client_for_dedupe_test()
    replied: list[object] = []

    async def _reply(packet):  # pragma: no cover - must never run
        replied.append(packet)

    monkeypatch.setattr(client, "_reply", _reply)
    client._handle_payload(_frame(2, b"\x00" * 60))
    await asyncio.sleep(0)
    assert replied == []
    assert not client._reply_tasks


async def test_a_reply_is_still_sent_when_acks_are_on(monkeypatch):
    """The other half: the DELTA 3's ack must not have been switched off."""
    from ecoflow_nut.ble_client import EcoFlowBLE
    from ecoflow_nut.config import BleConfig, EcoflowConfig

    client = EcoFlowBLE(
        EcoflowConfig(
            mac="DE:AD:BE:EF:00:01", serial="E201X", model="e2000", ack_frames=True
        ),
        BleConfig(),
    )
    replied: list[object] = []

    async def _reply(packet):
        replied.append(packet)

    monkeypatch.setattr(client, "_reply", _reply)
    client._handle_payload(_frame(2, b"\x00" * 60))
    await asyncio.sleep(0)
    assert len(replied) == 1


def test_eve_reports_a_missing_adapter_by_name(monkeypatch):
    """eve.adapter defaults to hci1, and most Pis only have hci0.

    Without this the symptom is a scan that silently finds nothing, which reads
    as "the outlet is not there" rather than "you have no such radio".
    """
    from ecoflow_nut import eve_outlet

    monkeypatch.setattr(eve_outlet, "available_adapters", lambda: ["hci0"])

    with pytest.raises(eve_outlet.EveError) as exc:
        eve_outlet.check_adapter("hci1")
    message = str(exc.value)
    assert "hci1" in message, "name the adapter that is missing"
    assert "hci0" in message, "and the one that is actually there"
    assert "eve.adapter" in message, "and the setting that fixes it"

    eve_outlet.check_adapter("hci0")  # the one that exists is fine


def test_eve_adapter_check_stays_quiet_when_it_cannot_tell(monkeypatch):
    """A host that keeps its adapters elsewhere must not be blocked."""
    from ecoflow_nut import eve_outlet

    monkeypatch.setattr(eve_outlet, "available_adapters", list)
    eve_outlet.check_adapter("hci9")  # must not raise
