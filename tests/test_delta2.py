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


def test_pd_heartbeat_counts_every_usb_port():
    """Six ports on this generation, not the DELTA 3's four.

    Reading only the first USB-A and the first USB-C reports 0 W for anything
    charging on the second USB-C -- the port most people reach for.
    """
    payload = rawstruct.pack(
        delta2.PD_DELTA2_MAX,
        {
            "usb1_watt": 5,
            "usb2_watt": 7,
            "qc_usb1_watt": 18,
            "qc_usb2_watt": 12,
            "typec1_watts": 45,
            "typec2_watts": 60,
        },
    )
    state = DeviceState()
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, payload))
    assert state.usb_output_watts == pytest.approx(5 + 7 + 18 + 12)
    assert state.usbc_output_watts == pytest.approx(45 + 60)


def test_pd_heartbeat_reports_the_12v_dc_port():
    """The 12V port is EcoFlow's "car" port throughout this protocol.

    Without these the dashboard's DC tile never leaves "?", so pressing its
    On/Off buttons gives no feedback that anything happened.
    """
    payload = rawstruct.pack(delta2.PD_DELTA2_MAX, {"dc_out_state": 1, "car_watts": 84})
    state = DeviceState()
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, payload))
    assert state.dc_output_on is True
    assert state.dc_output_watts == pytest.approx(84)

    off = rawstruct.pack(delta2.PD_DELTA2_MAX, {"dc_out_state": 0, "car_watts": 0})
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, off))
    assert state.dc_output_on is False
    assert state.dc_output_watts == pytest.approx(0)


def test_a_partial_pd_frame_does_not_zero_an_unmentioned_usb_port():
    """Frames are partial; totals come from last-known values, not this frame."""
    state = DeviceState()
    first = rawstruct.pack(delta2.PD_DELTA2_MAX, {"typec1_watts": 45, "typec2_watts": 60})
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, first))
    assert state.usbc_output_watts == pytest.approx(105)

    # A frame too short to reach typec2 must leave that port's last reading be.
    short_of_typec2 = delta2.PD_BASE[: _index_of(delta2.PD_BASE, "typec2_watts")]
    truncated = rawstruct.pack(delta2.PD_DELTA2_MAX, {"typec1_watts": 30})
    cut = truncated[: rawstruct.size_of(short_of_typec2)]
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, cut))
    assert state.usb_c2_watts == pytest.approx(60), "port 2 was never mentioned"
    assert state.usbc_output_watts == pytest.approx(90)


def test_a_usb_port_that_is_drawing_proves_the_bank_is_on():
    """This generation sends no USB switch flag, so the draw stands in.

    The dashboard used to render "? · 1W" -- a question mark next to proof of
    the answer. Power leaving a port that can be switched off means the switch
    is closed; nothing weaker is claimed here.
    """
    state = DeviceState()
    payload = rawstruct.pack(delta2.PD_DELTA2_MAX, {"qc_usb1_watt": 1})
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, payload))
    assert state.usb_output_on is True


def test_an_idle_usb_bank_stays_unknown():
    """A disabled bank and an enabled bank with nothing plugged in both read
    zero watts, and no flag separates them. Claiming either would be a guess."""
    state = DeviceState()
    payload = rawstruct.pack(
        delta2.PD_DELTA2_MAX,
        {"usb1_watt": 0, "usb2_watt": 0, "qc_usb1_watt": 0, "qc_usb2_watt": 0,
         "typec1_watts": 0, "typec2_watts": 0},
    )
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, payload))
    assert state.usb_output_on is None


def test_an_idle_frame_does_not_retract_a_known_usb_state():
    """Unplugging the last charger is not evidence the bank was switched off,
    and a tile that flips ON -> ? on an idle frame reads as a fault."""
    state = DeviceState()
    drawing = rawstruct.pack(delta2.PD_DELTA2_MAX, {"typec1_watts": 30})
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, drawing))
    assert state.usb_output_on is True

    idle = rawstruct.pack(delta2.PD_DELTA2_MAX, {"typec1_watts": 0})
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x02, 0x02, idle))
    assert state.usb_output_on is True, "still on as far as anything here knows"


def _index_of(layout, name: str) -> int:
    return next(i for i, (field, _) in enumerate(layout) if field == name)


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


def test_a_genuinely_unknown_model_still_raises():
    """Superseded for EF-E2 (now identified), but the guard itself must remain."""
    with pytest.raises(ValueError):
        devices.get_driver("some-future-ecoflow")


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


# --------------------------------------------------------------------------- #
# Raw (unidentified device) driver
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model", ["raw", "unknown", "unsupported"])
def test_raw_driver_is_reachable_for_unidentified_devices(model):
    assert devices.get_driver(model).name == "raw"


def test_raw_driver_decodes_nothing_but_still_lets_frames_through():
    """It must not claim frames: the sniffer shows them, the state stays empty."""
    driver = devices.get_driver("raw")
    state = DeviceState()
    payload = rawstruct.pack(delta2.EMS_HEARTBEAT, {"f32_lcd_show_soc": 50.0})
    assert driver.handle_packet(state, _frame(0x03, 0x02, payload)) is False
    assert state.soc_percent is None


def test_raw_driver_refuses_control_rather_than_guessing_an_opcode():
    driver = devices.get_driver("raw")
    for kind in devices.OUTPUT_KINDS:
        with pytest.raises(ValueError, match="no control opcodes"):
            driver.output_packet(kind, True)


def test_raw_driver_defaults_are_the_safe_ones_for_an_unknown_device():
    driver = devices.get_driver("raw")
    # V3 is the majority default across EcoFlow's range...
    assert driver.packet_version == 3
    # ...and de-obfuscating a device that does not obfuscate would turn good
    # frames into noise, hiding the very thing a capture is meant to reveal.
    assert driver.xor_payload is False


# --------------------------------------------------------------------------- #
# EcoFlow E2000 (confirmed on hardware)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model", ["e2000", "E2000", "E201", "EFE2000-EU-CBOX"])
def test_e2000_resolves_to_the_delta2max_driver(model):
    """Confirmed against hardware, not inferred from the spec sheet.

    A unit with serial E201ZE1APH560861 authenticates with V2 framing and
    streams the DELTA 2 Max subsystem set. Only its serial prefix is new, which
    is exactly what a prefix-whitelisting integration rejects.
    """
    assert devices.get_driver(model).name == "delta2max"


def test_e2000_observed_frames_are_all_understood():
    """The (src, cmd_set, cmd_id) triples captured from a real E2000.

    Payload lengths are the observed ones, several of which are *longer* than
    the layouts we know -- newer firmware appending fields. Decoding must cope
    rather than reject, which is why rawstruct stops at the boundary.
    """
    observed = [
        (0x02, 0x20, 0x02, 137),  # PD -- exactly PD_DELTA2_MAX's size
        (0x03, 0x20, 0x02, 55),  # EMS (layout is 46)
        (0x03, 0x20, 0x32, 192),  # BMS (layout is 69)
        (0x04, 0x20, 0x02, 72),  # INV (layout is 67)
        (0x05, 0x20, 0x02, 92),  # MPPT
    ]
    driver = devices.get_driver("e2000")
    for src, cmd_set, cmd_id, plen in observed:
        packet = _frame(src, cmd_id, bytes(plen), cmd_set=cmd_set)
        assert (
            driver.handle_packet(DeviceState(), packet) is True
        ), f"src=0x{src:02x} cmd_set=0x{cmd_set:02x} cmd_id=0x{cmd_id:02x} unhandled"


def test_e2000_pd_heartbeat_length_matches_the_delta2max_layout():
    """137 bytes is the single strongest identification signal we have."""
    assert rawstruct.size_of(delta2.PD_DELTA2_MAX) == 137
    assert rawstruct.size_of(delta2.PD_DELTA2) != 137


# --------------------------------------------------------------------------- #
# Solar (PV) input
# --------------------------------------------------------------------------- #
def test_mppt_heartbeat_sums_both_pv_channels():
    payload = rawstruct.pack(delta2.MPPT_MR350, {"in_watts": 180, "pv2_in_watts": 120})
    state = DeviceState()
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x05, 0x02, payload))
    assert state.solar_input_watts == pytest.approx(300)


def test_single_channel_model_reports_its_one_pv_input():
    """The DELTA 2's MPPT layout has no second channel; the sum must still work."""
    payload = rawstruct.pack(delta2.MPPT_MR330, {"in_watts": 95})
    state = DeviceState()
    assert delta2.DELTA2.handle_packet(state, _frame(0x05, 0x02, payload))
    assert state.solar_input_watts == pytest.approx(95)


def test_solar_never_counts_as_mains():
    """Solar is not a utility feed.

    Treating it as one would report "on line" through a daytime outage, right
    up until dusk -- exactly when clients would need the warning most.
    """
    state = DeviceState()
    solar = rawstruct.pack(delta2.MPPT_MR350, {"in_watts": 400})
    outage = rawstruct.pack(delta2.INV_DELTA, {"ac_in_vol": 0, "output_watts": 150})
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x05, 0x02, solar))
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x04, 0x02, outage))

    assert state.solar_input_watts == pytest.approx(400)
    assert state.ac_input_present is False
    assert state.ac_input_watts == pytest.approx(0)

    from ecoflow_nut.config import NutConfig
    from ecoflow_nut.nut_writer import derive_status

    assert derive_status(state, NutConfig()) == "OB"


def test_solar_is_none_on_a_model_that_never_reports_it():
    """None means "not reported", which must stay distinguishable from zero."""
    state = DeviceState()
    payload = rawstruct.pack(delta2.EMS_HEARTBEAT, {"f32_lcd_show_soc": 50.0})
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x02, payload))
    assert state.solar_input_watts is None


def test_a_missing_time_estimate_is_not_a_hundred_hours():
    """5999 minutes is the device's "no estimate", not 99h 59m.

    A full battery on mains reports it for time-to-charge, and it renders as a
    perfectly plausible duration -- so without this it reaches the dashboard,
    NUT's battery.runtime and the telemetry history as a real reading.
    """
    payload = rawstruct.pack(
        delta2.EMS_HEARTBEAT,
        {"chg_remain_time": 5999, "dsg_remain_time": 5999, "f32_lcd_show_soc": 100.0},
    )
    state = DeviceState()
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x02, payload))
    assert state.remain_charge_minutes is None
    assert state.remain_discharge_minutes is None
    assert state.soc_percent == pytest.approx(100), "the rest of the frame still merges"


def test_real_time_estimates_still_come_through():
    payload = rawstruct.pack(
        delta2.EMS_HEARTBEAT, {"chg_remain_time": 95, "dsg_remain_time": 512}
    )
    state = DeviceState()
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x02, payload))
    assert state.remain_charge_minutes == 95
    assert state.remain_discharge_minutes == 512


def test_a_partial_ems_frame_keeps_the_last_known_estimate():
    """Only assign what the frame carried -- truncation must not wipe a reading."""
    state = DeviceState()
    full = rawstruct.pack(delta2.EMS_HEARTBEAT, {"chg_remain_time": 95})
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x02, full))
    assert state.remain_charge_minutes == 95

    ems = delta2.EMS_HEARTBEAT
    short_of_chg = ems[: _index_of(ems, "chg_remain_time")]
    cut = full[: rawstruct.size_of(short_of_chg)]
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x02, cut))
    assert state.remain_charge_minutes == 95, "never mentioned, so never changed"


def test_pack_power_comes_from_the_bms_volts_and_amps():
    """Straight from a live E2000 capture, taking solar at 92.5%.

    Two independent checks on the same frames say these units are right:
    remain_cap/full_cap gives 92.4% against the 92.5% the device reported, and
    its own chg_remain_time of 206 minutes over the 2979 mAh still to fill
    implies 868 mA -- inside the 388..978 mA observed.

    It also shows why the ports cannot stand in: the station reported 326 W in
    and 214 W out, so the arithmetic said +112 W while the pack was taking
    about a third of that.
    """
    payload = rawstruct.pack(
        delta2.BMS_HEARTBEAT,
        {"vol": 52807, "amp": 978, "remain_cap": 36301, "full_cap": 39280,
         "f32_show_soc": 92.5},
    )
    state = DeviceState()
    assert delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x32, payload))
    assert state.battery_watts == pytest.approx(51.6, abs=0.1)


def test_a_discharging_pack_reads_negative():
    """Pack current is signed; read unsigned it decodes as ~4.3 billion."""
    payload = rawstruct.pack(delta2.BMS_HEARTBEAT, {"vol": 51200, "amp": -4000})
    state = DeviceState()
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x32, payload))
    assert state.battery_watts == pytest.approx(-204.8, abs=0.1)


def test_amp_is_signed_in_the_layout():
    """A four-byte field either way, so the pinned 69-byte size cannot catch it."""
    assert dict(delta2.BMS_HEARTBEAT)["amp"] == "i"


def test_battery_watts_stays_unset_when_the_pack_does_not_report_it():
    """A frame that never reaches the pack figures must leave them unknown.

    Zero would claim the battery is idle; None lets the UI fall back to the
    port arithmetic and say so.
    """
    state = DeviceState()
    ems = rawstruct.pack(delta2.EMS_HEARTBEAT, {"f32_lcd_show_soc": 55.0})
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x02, ems))
    assert state.battery_watts is None

    # A BMS frame truncated before `vol` decodes SoC but nothing about power.
    short = delta2.BMS_HEARTBEAT[: _index_of(delta2.BMS_HEARTBEAT, "vol")]
    cut = rawstruct.pack(delta2.BMS_HEARTBEAT, {"soc": 55})[: rawstruct.size_of(short)]
    delta2.DELTA2_MAX.handle_packet(state, _frame(0x03, 0x32, cut))
    assert state.battery_watts is None
