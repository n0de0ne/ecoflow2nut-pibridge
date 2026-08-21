"""Frame reassembly tests for both EcoFlow wire generations.

BLE notifications are a byte stream, not message boundaries: a frame can be
split across notifications and several can arrive together. Reassembly must
therefore work from the frame header alone -- and the V2 frames used by the
DELTA 2 generation carry no dsrc/ddst, so they are two bytes shorter than the
V3 frames of the DELTA 3 generation.
"""

import pytest

from ecoflow_nut.ble_client import PassthroughAssembler
from ecoflow_nut.protocol import Packet


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
