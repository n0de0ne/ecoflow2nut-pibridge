"""End-to-end wiring test for a DELTA 2 Max.

Drives the whole read path the daemon uses -- BLE byte stream -> frame
reassembly -> packet decode -> driver merge -> NUT variables -- with synthetic
heartbeats, so the pieces are proven to fit together without hardware.
"""

import pytest

from ecoflow_nut import devices, rawstruct
from ecoflow_nut.ble_client import PassthroughAssembler
from ecoflow_nut.config import NutConfig
from ecoflow_nut.delta2 import BMS_HEARTBEAT, EMS_HEARTBEAT, INV_DELTA, PD_DELTA2_MAX
from ecoflow_nut.nut_writer import build_variables
from ecoflow_nut.protocol import Packet
from ecoflow_nut.state import DeviceState


@pytest.fixture
def nut() -> NutConfig:
    """A DELTA 2 Max nameplate: 2048 Wh pack, 2400 W inverter."""
    cfg = NutConfig(battery_capacity_wh=2048, realpower_nominal=2400)
    cfg.static_values.model = "DELTA 2 Max"
    cfg.static_values.serial = "R351XXXXXXXXXXXX"
    return cfg


def _heartbeat(src: int, cmd_id: int, layout, values) -> bytes:
    """Serialise one heartbeat exactly as the device would put it on the wire."""
    return Packet(
        src=src,
        dst=0x21,
        cmd_set=0x20,
        cmd_id=cmd_id,
        payload=rawstruct.pack(layout, values),
        version=2,
    ).to_bytes()


def _feed(driver, state: DeviceState, stream: bytes) -> int:
    """Push a raw byte stream through reassembly and the driver, as the client does."""
    handled = 0
    for raw in PassthroughAssembler().reassemble(stream):
        if driver.handle_packet(state, Packet.from_bytes(raw)):
            handled += 1
    return handled


ON_MAINS = (
    _heartbeat(
        0x03,
        0x02,
        EMS_HEARTBEAT,
        {"f32_lcd_show_soc": 87.5, "chg_remain_time": 200, "dsg_remain_time": 1400},
    )
    + _heartbeat(
        0x04,
        0x02,
        INV_DELTA,
        {
            "input_watts": 240,
            "output_watts": 180,
            "ac_in_vol": 232_500,
            "cfg_ac_enabled": 1,
        },
    )
    + _heartbeat(
        0x02,
        0x02,
        PD_DELTA2_MAX,
        {"watts_in_sum": 245, "watts_out_sum": 190, "usb1_watt": 5, "typec1_watts": 30},
    )
    + _heartbeat(0x03, 0x32, BMS_HEARTBEAT, {"f32_show_soc": 87.2})
)


def test_a_full_heartbeat_round_reads_as_a_healthy_ups(nut):
    driver = devices.get_driver("delta2max")
    state = DeviceState()
    # All four heartbeats arrive in one notification and must all be understood.
    assert _feed(driver, state, ON_MAINS) == 4

    assert state.soc_percent == pytest.approx(87.5)
    assert state.soc_source == "ems"  # the LCD float, not the coarser BMS value
    assert state.ac_input_voltage == pytest.approx(232.5)
    assert state.input_watts == pytest.approx(245)
    assert state.usbc_output_watts == pytest.approx(30)
    assert state.is_complete

    variables = build_variables(state, nut)
    assert variables["ups.status"] == "OL"
    assert variables["battery.charge"] == "87"
    assert variables["ups.realpower"] == "180"
    assert variables["ups.load"] == "8"  # 180 W of 2400 W
    assert variables["input.voltage"] == "232.5"
    assert variables["device.model"] == "DELTA 2 Max"


def test_grid_outage_flips_the_status_and_then_reports_low_battery(nut):
    driver = devices.get_driver("delta2max")
    state = DeviceState()
    _feed(driver, state, ON_MAINS)
    assert build_variables(state, nut)["ups.status"] == "OL"

    # Mains fail: the inverter keeps supplying the load from the battery.
    outage = _heartbeat(
        0x04,
        0x02,
        INV_DELTA,
        {"input_watts": 0, "output_watts": 180, "ac_in_vol": 0, "cfg_ac_enabled": 1},
    )
    _feed(driver, state, outage)
    variables = build_variables(state, nut)
    assert variables["ups.status"] == "OB"
    assert variables["input.voltage"] == "0"
    # 87.5% of 2048 Wh at 90% efficiency, drawn at 180 W -> a bit under 9 hours.
    assert int(variables["battery.runtime"]) == pytest.approx(32256, rel=0.01)

    # The pack drains below the low-battery threshold.
    drained = _heartbeat(0x03, 0x02, EMS_HEARTBEAT, {"f32_lcd_show_soc": 18.0})
    _feed(driver, state, drained)
    assert build_variables(state, nut)["ups.status"] == "OB LB"


def test_full_battery_idle_on_mains_is_not_mistaken_for_an_outage(nut):
    """A charged pack with no load draws ~0 W but is still on the grid.

    Reporting OB here would tell every NUT client to begin shutting down.
    """
    driver = devices.get_driver("delta2max")
    state = DeviceState()
    _feed(driver, state, ON_MAINS)
    idle = _heartbeat(
        0x04,
        0x02,
        INV_DELTA,
        {"input_watts": 0, "output_watts": 0, "ac_in_vol": 230_100, "cfg_ac_enabled": 1},
    )
    _feed(driver, state, idle)
    assert build_variables(state, nut)["ups.status"] == "OL"


def test_delta3_frames_are_not_decoded_as_delta2(nut):
    """Configuring the wrong generation must read as "no telemetry", not garbage.

    A silent misdecode would publish plausible-looking nonsense to NUT clients.
    """
    driver = devices.get_driver("delta2max")
    state = DeviceState()
    # A DELTA 3 DisplayPropertyUpload: V3 framing, src 0x02, cmd_set 0xFE/0x15.
    delta3_frame = Packet(
        src=0x02, dst=0x21, cmd_set=0xFE, cmd_id=0x15, payload=b"\x08\x01", version=3
    ).to_bytes()
    assert _feed(driver, state, delta3_frame) == 0
    assert not state.is_complete
