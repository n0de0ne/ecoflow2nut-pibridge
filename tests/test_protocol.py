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
    decode_fields,
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


def test_decode_fields_keeps_wire_types():
    """The raw diagnostic needs the wire type: a varint flag and a float reading
    are the difference between "port is on" and "port is drawing 3 W"."""
    packet = Packet.from_bytes(REAL_FRAMES[0], xor_payload=True)
    rows = decode_fields(packet.payload)
    by_number = {num: (wire, value) for num, wire, value in rows}
    assert by_number[delta3.F_CMS_BATT_SOC][0] == WIRE_I32
    assert by_number[delta3.F_ERRCODE][0] == WIRE_VARINT
    # Same values as decode_message, just with the wire type retained.
    assert by_number[delta3.F_POW_GET_AC_IN][1] == pytest.approx(46.32, abs=0.1)


def test_decode_fields_sees_more_than_we_decode():
    """Guards the premise of `read --raw`: most of the frame is undecoded, so a
    missing reading is far more likely a field we ignore than a parsing bug."""
    seen = set()
    for frame in REAL_FRAMES:
        packet = Packet.from_bytes(frame, xor_payload=True)
        seen.update(num for num, _, _ in decode_fields(packet.payload))
    assert len(seen) > 100
    assert seen >= set(delta3.DISPLAY_FIELD_NAMES)
    undecoded = seen - set(delta3.DISPLAY_FIELD_NAMES)
    # The run of varints ending at flow_info_ac_out (367). One of these is very
    # likely a per-port flow flag, which would give USB a true ON/OFF state
    # instead of one inferred from watts -- still unidentified.
    assert {362, 363, 364, 365, 366} <= undecoded


def test_usb_watt_fields_decode_from_a_real_frame():
    """Fields 9/11 had no coverage at all; all four ports read zero here (the
    capture device had nothing plugged into USB)."""
    packet = Packet.from_bytes(REAL_FRAMES[0], xor_payload=True)
    fields = decode_message(packet.payload)
    assert fields[delta3.F_POW_GET_QCUSB1] == pytest.approx(0.0)
    assert fields[delta3.F_POW_GET_TYPEC1] == pytest.approx(0.0)
    state = DeviceState()
    state.merge_display_payload(packet.payload)
    assert state.usb_output_watts == pytest.approx(0.0)
    assert state.usbc_output_watts == pytest.approx(0.0)


def _usb_payload(**ports: float) -> bytes:
    """A synthetic DisplayPropertyUpload carrying only the named USB ports."""
    numbers = {
        "a1": delta3.F_POW_GET_QCUSB1,
        "a2": delta3.F_POW_GET_QCUSB2,
        "c1": delta3.F_POW_GET_TYPEC1,
        "c2": delta3.F_POW_GET_TYPEC2,
    }
    return encode_message(
        [ProtoField(numbers[name], WIRE_I32, watts) for name, watts in ports.items()]
    )


def test_second_usb_port_is_counted():
    """The reported bug: a load on port 2 was invisible, so the dashboard showed
    0 W and "idle/off" while the EcoFlow app reported the port drawing. On a real
    DELTA 3 the draw arrived on field 12 with 9/10/11 all at 0.0."""
    state = DeviceState()
    state.merge_display_payload(_usb_payload(a1=0.0, a2=0.0, c1=0.0, c2=-1.0))
    assert state.usbc_output_watts == pytest.approx(1.0)
    assert state.usb_output_watts == pytest.approx(0.0)


def test_usb_ports_of_the_same_type_are_summed():
    state = DeviceState()
    state.merge_display_payload(_usb_payload(a1=-2.5, a2=-1.5, c1=-3.0, c2=-1.0))
    assert state.usb_output_watts == pytest.approx(4.0)
    assert state.usbc_output_watts == pytest.approx(4.0)


def test_a_partial_frame_does_not_zero_an_unmentioned_port():
    """Frames carry only what changed, so summing just what arrived would drop a
    port's last-known draw the moment a frame omitted it."""
    state = DeviceState()
    state.merge_display_payload(_usb_payload(c1=0.0, c2=-1.0))
    assert state.usbc_output_watts == pytest.approx(1.0)
    # A later frame mentions only port 1; port 2 is still drawing.
    state.merge_display_payload(_usb_payload(c1=0.0))
    assert state.usbc_output_watts == pytest.approx(1.0)
    # And when port 2 genuinely drops to zero, the total follows.
    state.merge_display_payload(_usb_payload(c2=0.0))
    assert state.usbc_output_watts == pytest.approx(0.0)


def test_usb_totals_stay_none_until_a_port_reports():
    """ "Not reported" must read differently from "reported zero"."""
    state = DeviceState()
    state.merge_display_payload(encode_message([]))
    assert state.usb_output_watts is None
    assert state.usbc_output_watts is None


@pytest.mark.parametrize("value", [2, 3, 14, 15])
def test_flow_is_on_accepts_active_masks(value):
    assert delta3.flow_is_on(value) is True


@pytest.mark.parametrize("value", [0, 4, 8, 12])
def test_flow_is_on_rejects_inactive_masks(value):
    """The whole point: bool() says True for 4, 8 and 12, and all three appear
    on a real DELTA 3 for ports that are OFF."""
    assert delta3.flow_is_on(value) is False
    assert bool(value) is not delta3.flow_is_on(value) or value == 0


def _flags_payload(**flags: int) -> bytes:
    numbers = {
        "usb_a1": delta3.F_FLOW_INFO_QCUSB1,
        "usb_c2": delta3.F_FLOW_INFO_TYPEC2,
        "ac_out": delta3.F_FLOW_INFO_AC_OUT,
        "dc": delta3.F_FLOW_INFO_12V,
    }
    return encode_message(
        [ProtoField(numbers[k], WIRE_VARINT, v) for k, v in flags.items()]
    )


def test_port_flags_decode_from_real_device_values():
    """Values observed on a real DELTA 3: USB and AC active at 14, 12V off at 4."""
    state = DeviceState()
    state.merge_display_payload(_flags_payload(usb_a1=14, ac_out=14, dc=4))
    assert state.usb_output_on is True
    assert state.ac_output_on is True
    assert state.dc_output_on is False


def test_ac_output_off_is_not_read_as_on():
    """Regression: ac_output_on used bool(), so an inactive 4 read as ON."""
    state = DeviceState()
    state.merge_display_payload(_flags_payload(ac_out=4))
    assert state.ac_output_on is False


def test_any_usb_port_flag_answers_for_the_master_switch():
    """One switch drives all four ports, and frames are partial -- whichever
    port's flag arrives has to be enough."""
    state = DeviceState()
    state.merge_display_payload(_flags_payload(usb_c2=14))
    assert state.usb_output_on is True
    state.merge_display_payload(_flags_payload(usb_a1=4))
    assert state.usb_output_on is False


def test_port_flags_stay_none_until_reported():
    state = DeviceState()
    state.merge_display_payload(encode_message([]))
    assert state.usb_output_on is None
    assert state.dc_output_on is None


def test_field_names_cover_every_decoded_constant():
    """The diagnostic labels known fields; a new constant must not go unlabelled."""
    constants = {
        value
        for name, value in vars(delta3).items()
        if name.startswith("F_") and isinstance(value, int)
    }
    assert constants == set(delta3.DISPLAY_FIELD_NAMES)


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
