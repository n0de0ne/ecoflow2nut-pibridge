"""Packet framing and protobuf codec tests.

The frames in ``tests/data/real_frames.txt`` are genuine DisplayPropertyUpload
frames captured from an EcoFlow device of the same (pd335/pr705) protocol family
that the DELTA 3 belongs to. They share identical protobuf field numbers, so
they validate the read path end to end without hardware.
"""

from pathlib import Path

import pytest

from ecoflow_nut import delta3
from ecoflow_nut.delta3 import DeviceState
from ecoflow_nut.protocol import (
    WIRE_I32,
    WIRE_VARINT,
    Packet,
    PacketError,
    ProtoField,
    _read_varint,
    decode_message,
    encode_bool_field,
    encode_message,
    encode_varint,
)

_DATA = Path(__file__).parent / "data" / "real_frames.txt"


def _load_real_frames() -> list[bytes]:
    frames = []
    for line in _DATA.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            frames.append(bytes.fromhex(line))
    return frames


REAL_FRAMES = _load_real_frames()


def test_packet_roundtrip():
    packet = Packet(
        src=0x21,
        dst=0x02,
        cmd_set=0xFE,
        cmd_id=0x11,
        payload=b"\xe0\x04\x01",
        dsrc=0x01,
        ddst=0x01,
        version=3,
    )
    decoded = Packet.from_bytes(packet.to_bytes())
    assert (decoded.src, decoded.dst) == (packet.src, packet.dst)
    assert (decoded.cmd_set, decoded.cmd_id) == (packet.cmd_set, packet.cmd_id)
    assert decoded.payload == packet.payload


def test_packet_rejects_bad_prefix():
    with pytest.raises(PacketError):
        Packet.from_bytes(b"\x00" * 20)


def test_packet_rejects_bad_crc16():
    corrupt = bytearray(Packet(0x21, 0x02, 0xFE, 0x11, b"\x01", version=3).to_bytes())
    corrupt[-1] ^= 0xFF
    with pytest.raises(PacketError):
        Packet.from_bytes(bytes(corrupt))


@pytest.mark.parametrize("frame", REAL_FRAMES)
def test_real_frames_parse_as_display_packets(frame):
    packet = Packet.from_bytes(frame, xor_payload=True)
    assert packet.src == delta3.DISPLAY_SRC
    assert packet.cmd_set == delta3.DISPLAY_CMD_SET
    assert packet.cmd_id == delta3.DISPLAY_CMD_ID


def test_real_frame_decodes_expected_telemetry():
    packet = Packet.from_bytes(REAL_FRAMES[0], xor_payload=True)
    fields = decode_message(packet.payload)
    assert fields[delta3.F_CMS_BATT_SOC] == pytest.approx(75.0)
    assert fields[delta3.F_POW_GET_AC_IN] == pytest.approx(46.32, abs=0.1)
    assert fields[delta3.F_POW_IN_SUM_W] == pytest.approx(53.0)


def test_state_merge_from_real_frame():
    packet = Packet.from_bytes(REAL_FRAMES[0], xor_payload=True)
    state = DeviceState()
    delta3.merge_display_payload(state, packet.payload)
    assert state.soc_percent == pytest.approx(75.0)
    assert state.ac_input_watts == pytest.approx(46.3, abs=0.1)
    # AC output is reported negative on the wire; we expose absolute load.
    assert state.ac_output_watts == pytest.approx(46.3, abs=0.1)


def test_state_merge_accumulates_across_frames():
    state = DeviceState()
    for frame in REAL_FRAMES:
        packet = Packet.from_bytes(frame, xor_payload=True)
        delta3.merge_display_payload(state, packet.payload)
    # SoC seen in the first frame must persist even though later frames omit it.
    assert state.soc_percent == pytest.approx(75.0)


def test_varint_roundtrip():
    for value in (0, 1, 127, 128, 300, 608, 16384, 2**31):
        encoded = encode_varint(value)
        decoded, pos = _read_varint(encoded, 0)
        assert decoded == value
        assert pos == len(encoded)


def test_protobuf_message_roundtrip():
    encoded = encode_message(
        [ProtoField(76, WIRE_VARINT, 1), ProtoField(262, WIRE_I32, 73.5)]
    )
    decoded = decode_message(encoded)
    assert decoded[76] == 1
    assert decoded[262] == pytest.approx(73.5)


def test_encode_bool_field():
    assert decode_message(encode_bool_field(76, True))[76] == 1
    assert decode_message(encode_bool_field(76, False))[76] == 0


def test_config_packet_builders_roundtrip():
    for builder, field_num in (
        (delta3.set_ac_enabled_packet, delta3.CFG_AC_OUT_OPEN),
        (delta3.set_usb_enabled_packet, delta3.CFG_USB_OPEN),
        (delta3.set_dc_enabled_packet, delta3.CFG_DC_12V_OUT_OPEN),
    ):
        packet = builder(True)
        assert packet.cmd_set == delta3.CONFIG_CMD_SET
        assert packet.cmd_id == delta3.CONFIG_CMD_ID
        decoded = Packet.from_bytes(packet.to_bytes())
        assert decode_message(decoded.payload)[field_num] == 1
