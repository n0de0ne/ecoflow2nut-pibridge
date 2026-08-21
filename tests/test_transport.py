"""Frame reassembly tests for both EcoFlow wire generations.

BLE notifications are a byte stream, not message boundaries: a frame can be
split across notifications and several can arrive together. Reassembly must
therefore work from the frame header alone -- and the V2 frames used by the
DELTA 2 generation carry no dsrc/ddst, so they are two bytes shorter than the
V3 frames of the DELTA 3 generation.
"""

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
        EcoflowConfig(mac="AA:BB:CC:DD:EE:FF", serial="E201X", model="e2000"),
        BleConfig(),
    )
