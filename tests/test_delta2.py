"""DELTA 2 generation protocol tests.

The layouts here were transcribed from the ha-ef-ble reverse engineering and
cross-checked field-by-field against its struct definitions; these tests pin the
resulting sizes so a future edit cannot silently shift an offset, and exercise
the decode path end to end through real V2 frames built by our own codec.
"""

import pytest

from ecoflow_nut import delta2, devices, rawstruct, sniffer
from ecoflow_nut.protocol import Packet
from ecoflow_nut.state import DeviceState

# Sizes verified against ha-ef-ble's generated struct formats. A change here
# means a field was added, removed or re-typed -- and every offset after it moved.
EXPECTED_SIZES = {
    "PD_DELTA2_MAX": (delta2.PD_DELTA2_MAX, 137),
    "PD_DELTA2": (delta2.PD_DELTA2, 147),
    "EMS_HEARTBEAT": (delta2.EMS_HEARTBEAT, 46),
    "INV_DELTA": (delta2.INV_DELTA, 67),
    "BMS_HEARTBEAT": (delta2.BMS_HEARTBEAT, 69),
}


@pytest.mark.parametrize("name", sorted(EXPECTED_SIZES))
def test_layout_sizes_are_pinned(name):
    layout, expected = EXPECTED_SIZES[name]
    assert rawstruct.size_of(layout) == expected


def _frame(src: int, cmd_id: int, payload: bytes, cmd_set: int = 0x20) -> Packet:
    """Round-trip a heartbeat through the real V2 wire format."""
    raw = Packet(
        src=src, dst=0x21, cmd_set=cmd_set, cmd_id=cmd_id, payload=payload, version=2
    ).to_bytes()
    return Packet.from_bytes(raw)


# --------------------------------------------------------------------------- #
# rawstruct
# --------------------------------------------------------------------------- #
def test_rawstruct_roundtrip():
    layout = (("a", "B"), ("b", "H"), ("c", "f"), ("d", "4s"))
    packed = rawstruct.pack(layout, {"a": 7, "b": 600, "c": 1.5, "d": b"abcd"})
    assert len(packed) == rawstruct.size_of(layout) == 11
    assert rawstruct.unpack(layout, packed) == {
        "a": 7,
        "b": 600,
        "c": 1.5,
        "d": b"abcd",
    }


def test_rawstruct_decodes_short_payload_up_to_the_boundary():
    """Older firmware omits the tail; everything before it must still decode."""
    layout = (("a", "B"), ("b", "H"), ("c", "I"))
    values = rawstruct.unpack(layout, bytes([9, 0x58, 0x02]))
    assert values == {"a": 9, "b": 600}
    assert "c" not in values


def test_rawstruct_ignores_trailing_bytes():
    """Newer firmware appends fields we have no names for; they are skipped."""
    layout = (("a", "B"),)
    assert rawstruct.unpack(layout, b"\x05extra") == {"a": 5}


# --------------------------------------------------------------------------- #
# Telemetry decode
# --------------------------------------------------------------------------- #
def test_ems_heartbeat_sets_soc_and_remaining_times():
    payload = rawstruct.pack(
        delta2.EMS_HEARTBEAT,
        {"f32_lcd_show_soc": 73.5, "chg_remain_time": 120, "dsg_remain_time": 4300},
    )
    state = DeviceState()
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x02, payload))
    assert state.soc_percent == pytest.approx(73.5)
    assert state.soc_source == "ems"
    assert state.remain_charge_minutes == 120
    assert state.remain_discharge_minutes == 4300


def test_inv_heartbeat_drives_the_ups_view():
    payload = rawstruct.pack(
        delta2.INV_DELTA,
        {
            "input_watts": 412,
            "output_watts": 260,
            "cfg_ac_enabled": 1,
            "ac_in_vol": 230_400,  # millivolts
            "err_code": 0,
        },
    )
    state = DeviceState()
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x04, 0x02, payload))
    assert state.ac_input_watts == pytest.approx(412)
    assert state.ac_output_watts == pytest.approx(260)
    assert state.ac_input_voltage == pytest.approx(230.4)
    assert state.ac_output_on is True
    assert state.ac_input_present is True


def test_inv_zero_mains_voltage_means_outage():
    payload = rawstruct.pack(
        delta2.INV_DELTA, {"ac_in_vol": 0, "output_watts": 180, "cfg_ac_enabled": 1}
    )
    state = DeviceState()
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x04, 0x02, payload))
    assert state.ac_input_voltage == 0
    assert state.ac_input_present is False
    # Still inverting -- the load is now running off the battery.
    assert state.ac_output_watts == pytest.approx(180)


def test_pd_heartbeat_sets_totals_and_port_watts():
    payload = rawstruct.pack(
        delta2.PD_DELTA2_MAX,
        {"watts_in_sum": 500, "watts_out_sum": 310, "usb1_watt": 7, "typec1_watts": 45},
    )
    state = DeviceState()
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, payload))
    assert state.input_watts == pytest.approx(500)
    assert state.output_watts == pytest.approx(310)
    assert state.usb_output_watts == pytest.approx(7)
    assert state.usbc_output_watts == pytest.approx(45)


def test_delta2_reads_ac_watts_from_pd_but_delta2max_does_not():
    """The two models report AC power from different subsystems."""
    payload = rawstruct.pack(
        delta2.PD_DELTA2, {"ac_input_watts": 350, "ac_output_watts": 120}
    )
    state = DeviceState()
    delta2.DELTA2.handle_packet(state, _frame(0x02, 0x02, payload))
    assert state.ac_input_watts == pytest.approx(350)
    assert state.ac_output_watts == pytest.approx(120)

    # The DELTA 2 Max's PD layout has no AC watts at those offsets, so its
    # driver must ignore whatever happens to sit there and wait for the INV.
    other = DeviceState()
    delta2.DELTA2_MAX.handle_packet(other, _frame(0x02, 0x02, payload))
    assert other.ac_input_watts is None
    assert other.ac_output_watts is None


# --------------------------------------------------------------------------- #
# SoC source precedence
# --------------------------------------------------------------------------- #
def test_ems_soc_wins_over_bms_and_pd_regardless_of_arrival_order():
    state = DeviceState()
    ems = rawstruct.pack(delta2.EMS_HEARTBEAT, {"f32_lcd_show_soc": 64.0})
    bms = rawstruct.pack(delta2.BMS_HEARTBEAT, {"f32_show_soc": 61.0})
    pd = rawstruct.pack(delta2.PD_DELTA2_MAX, {"soc": 60})

    delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x02, ems))
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x32, bms))
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, pd))
    assert state.soc_percent == pytest.approx(64.0)
    assert state.soc_source == "ems"


def test_bms_soc_used_and_kept_current_while_the_ems_is_silent():
    state = DeviceState()
    for value in (61.0, 59.5):
        payload = rawstruct.pack(delta2.BMS_HEARTBEAT, {"f32_show_soc": value})
        delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x32, payload))
        # A lesser source must keep updating, not freeze at its first reading.
        assert state.soc_percent == pytest.approx(value)
    assert state.soc_source == "bms"


def test_extra_battery_soc_does_not_overwrite_the_main_pack():
    state = DeviceState()
    main = rawstruct.pack(delta2.BMS_HEARTBEAT, {"f32_show_soc": 80.0})
    extra = rawstruct.pack(delta2.BMS_HEARTBEAT, {"f32_show_soc": 12.0})
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x32, main))
    # src 0x06 is an attached expansion battery reporting its own charge.
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x06, 0x32, extra))
    assert state.soc_percent == pytest.approx(80.0)


def test_unknown_frame_is_reported_as_unhandled():
    state = DeviceState()
    unknown = _frame(0x99, 0x77, b"\x01\x02")
    assert delta2.DELTA2_MAX.handle_packet(state, unknown) is False


# --------------------------------------------------------------------------- #
# Control commands
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("enabled", [True, False])
def test_ac_command_targets_the_model_specific_subsystem(enabled):
    """The AC relay lives on the inverter (0x04) on a Max, the MPPT (0x05) otherwise."""
    for driver, expected_dst in ((delta2.DELTA2_MAX, 0x04), (delta2.DELTA2, 0x05)):
        packet = driver.set_ac_enabled_packet(enabled)
        assert packet.dst == expected_dst
        assert (packet.src, packet.cmd_set, packet.cmd_id) == (0x21, 0x20, 0x42)
        assert packet.payload[0] == (1 if enabled else 0)
        # Trailing 0xFF are "leave unchanged" for the settings this opcode carries.
        assert packet.payload[1:] == b"\xff" * 6


@pytest.mark.parametrize("enabled", [True, False])
def test_usb_and_dc_commands(enabled):
    usb = delta2.DELTA2_MAX.set_usb_enabled_packet(enabled)
    assert (usb.src, usb.dst, usb.cmd_set, usb.cmd_id) == (0x21, 0x02, 0x20, 0x22)
    assert usb.payload == bytes([1 if enabled else 0])

    dc = delta2.DELTA2_MAX.set_dc_enabled_packet(enabled)
    assert (dc.src, dc.dst, dc.cmd_set, dc.cmd_id) == (0x21, 0x05, 0x20, 0x51)
    assert dc.payload == bytes([1 if enabled else 0])


def test_commands_are_v2_frames_that_round_trip():
    """V2 framing has no dsrc/ddst, so the payload starts two bytes earlier."""
    for kind in devices.OUTPUT_KINDS:
        packet = delta2.DELTA2_MAX.output_packet(kind, True)
        assert packet.version == 2
        decoded = Packet.from_bytes(packet.to_bytes())
        assert (decoded.src, decoded.dst) == (packet.src, packet.dst)
        assert (decoded.cmd_set, decoded.cmd_id) == (packet.cmd_set, packet.cmd_id)
        assert decoded.payload == packet.payload


def test_output_packet_rejects_unknown_kind():
    with pytest.raises(ValueError):
        delta2.DELTA2_MAX.output_packet("fan", True)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "model,expected",
    [
        ("delta2max", "delta2max"),
        ("DELTA 2 Max", "delta2max"),
        ("delta-2-max", "delta2max"),
        ("R351", "delta2max"),
        ("delta2", "delta2"),
        ("DELTA 2", "delta2"),
        ("delta3", "delta3"),
        ("DELTA 3", "delta3"),
    ],
)
def test_model_aliases_resolve(model, expected):
    assert devices.get_driver(model).name == expected


def test_unknown_model_names_the_supported_ones():
    with pytest.raises(ValueError, match="delta2max"):
        devices.get_driver("powerstream")


@pytest.mark.parametrize("model", ["e2000", "E2000", "EF-E2"])
def test_unidentified_e2_prefix_is_rejected_rather_than_guessed(model):
    """``EF-E2`` matches no known EcoFlow module and rejects V2 framing.

    Silently aliasing it onto a generation would point a real device at the
    wrong protocol; make the user pick one, after ``probe`` says which.
    """
    with pytest.raises(ValueError):
        devices.get_driver(model)


def test_packet_versions_match_the_protocol_generation():
    assert devices.get_driver("delta2max").packet_version == 2
    assert devices.get_driver("delta2").packet_version == 2
    assert devices.get_driver("delta3").packet_version == 3


def test_payload_obfuscation_is_per_model():
    """Not a per-generation trait: the DELTA 2 XORs payloads, the Max does not."""
    assert devices.get_driver("delta2").xor_payload is True
    assert devices.get_driver("delta2max").xor_payload is False
    assert devices.get_driver("delta3").xor_payload is True


# --------------------------------------------------------------------------- #
# Diagnostics (the `sniff` command's decode)
# --------------------------------------------------------------------------- #
def test_sniffer_labels_and_decodes_a_known_message():
    payload = rawstruct.pack(delta2.INV_DELTA, {"ac_in_vol": 230_000, "output_watts": 90})
    info = sniffer.describe_packet(delta2.DELTA2_MAX, _frame(0x04, 0x02, payload))
    assert info["message"] == "inv"
    assert info["fields"]["output_watts"] == 90
    assert info["src"] == "0x04"


def test_sniffer_still_reports_an_unknown_frame():
    """The whole point of a capture: frames no driver claims are still usable."""
    info = sniffer.describe_packet(delta2.DELTA2_MAX, _frame(0x99, 0x77, b"\xde\xad"))
    assert "message" not in info
    assert info["payload_hex"] == "dead"


def test_sniffer_renders_raw_bytes_as_hex_for_json():
    payload = rawstruct.pack(delta2.PD_DELTA2_MAX, {"error_code": b"\x01\x02\x03\x04"})
    info = sniffer.describe_packet(delta2.DELTA2_MAX, _frame(0x02, 0x02, payload))
    assert info["fields"]["error_code"] == "01020304"


def test_layout_coverage_flags_a_short_payload():
    """A firmware variant that omits the tail should be reported, not hidden."""
    short = rawstruct.pack(delta2.INV_DELTA, {})[:20]
    note = sniffer.layout_coverage(delta2.DELTA2_MAX, _frame(0x04, 0x02, short))
    assert note is not None and "shorter" in note


def test_layout_coverage_flags_unmapped_trailing_bytes():
    long = rawstruct.pack(delta2.INV_DELTA, {}) + b"\x00" * 8
    note = sniffer.layout_coverage(delta2.DELTA2_MAX, _frame(0x04, 0x02, long))
    assert note is not None and "longer" in note


def test_layout_coverage_silent_on_an_exact_match():
    exact = rawstruct.pack(delta2.INV_DELTA, {})
    assert sniffer.layout_coverage(delta2.DELTA2_MAX, _frame(0x04, 0x02, exact)) is None
