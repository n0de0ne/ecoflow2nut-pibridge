"""DELTA 3 specific protocol: DisplayPropertyUpload decoding and control commands.

The DELTA 3 (``pd335`` family, serial prefix ``P231``, advertised name ``EF-D3``)
reports state in a ``DisplayPropertyUpload`` protobuf and accepts control via a
``ConfigWrite`` protobuf. The protobuf field numbers below were taken from the
``pd335_sys.proto`` definition recovered by the ha-ef-ble project and were
cross-checked by decoding real captured frames from a sibling device
(River 3 / ``pr705``), which shares identical field numbers for these fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import protocol
from .protocol import Packet, ProtoField
from .state import DeviceState

__all__ = [
    "DRIVER",
    "Delta3Driver",
    "DeviceState",
    "flow_is_on",
    "is_display_packet",
    "merge_display_payload",
    "set_ac_enabled_packet",
    "set_dc_enabled_packet",
    "set_usb_enabled_packet",
]

# --- DisplayPropertyUpload field numbers (read) ---------------------------- #
F_ERRCODE = 1
F_POW_IN_SUM_W = 3  # total input watts (float)
F_POW_OUT_SUM_W = 4  # total output watts (float)
# A DELTA 3 has two USB-A and two USB-C ports and reports each separately, all
# as negative floats (like AC output). Reading only port 1 of each made a load on
# a second port invisible: the dashboard showed 0 W and "idle/off" while the
# EcoFlow app reported the port on and drawing. Field 12 is confirmed against a
# real DELTA 3 (it carried the bridge's own ~1 W with 9/10/11 all at 0.0);
# 10 is inferred from the pattern and reads 0.0 on the same frame.
F_POW_GET_QCUSB1 = 9  # USB-A port 1 watts (float)
F_POW_GET_QCUSB2 = 10  # USB-A port 2 watts (float)
F_POW_GET_TYPEC1 = 11  # USB-C port 1 watts (float)
F_POW_GET_TYPEC2 = 12  # USB-C port 2 watts (float)
# Per-port flow state. NOT booleans -- see flow_is_on(). The DELTA 3's USB is a
# single master switch (cfg_usb_open), so all four USB ports report the same
# state; any of them answers "is USB on".
F_FLOW_INFO_QCUSB1 = 13
F_FLOW_INFO_QCUSB2 = 14
F_FLOW_INFO_TYPEC1 = 15
F_FLOW_INFO_TYPEC2 = 16
F_FLOW_INFO_12V = 33  # 12V DC output flow state
F_POW_GET_12V = 37  # 12V DC output watts (float)
F_PLUG_IN_INFO_AC_CHARGER_FLAG = 202  # AC charger connected (bool/uint32)
F_BMS_BATT_SOC = 242  # BMS state of charge (float %)
F_CMS_BATT_SOC = 262  # combined/displayed state of charge (float %)
F_CMS_DSG_REM_TIME = 268  # discharge remaining time (uint32, minutes)
F_CMS_CHG_REM_TIME = 269  # charge remaining time (uint32, minutes)
F_FLOW_INFO_AC_OUT = 367  # AC output flow state (uint32, 0=off)
F_POW_GET_AC_IN = 54  # AC input watts (float)
F_POW_GET_AC_OUT = 368  # AC output watts (float, reported negative)

# Field number -> the name we know it by, for the 'read --raw' diagnostic. The
# device sends far more fields than we decode; this labels the ones we do so an
# unknown field stands out in the dump.
DISPLAY_FIELD_NAMES: dict[int, str] = {
    F_ERRCODE: "errcode",
    F_FLOW_INFO_QCUSB1: "flow_info_qcusb1",
    F_FLOW_INFO_QCUSB2: "flow_info_qcusb2",
    F_FLOW_INFO_TYPEC1: "flow_info_typec1",
    F_FLOW_INFO_TYPEC2: "flow_info_typec2",
    F_FLOW_INFO_12V: "flow_info_12v",
    F_POW_GET_12V: "pow_get_12v",
    F_POW_IN_SUM_W: "pow_in_sum_w",
    F_POW_OUT_SUM_W: "pow_out_sum_w",
    F_POW_GET_QCUSB1: "pow_get_qcusb1",
    F_POW_GET_QCUSB2: "pow_get_qcusb2",
    F_POW_GET_TYPEC1: "pow_get_typec1",
    F_POW_GET_TYPEC2: "pow_get_typec2",
    F_POW_GET_AC_IN: "pow_get_ac_in",
    F_PLUG_IN_INFO_AC_CHARGER_FLAG: "plug_in_info_ac_charger_flag",
    F_BMS_BATT_SOC: "bms_batt_soc",
    F_CMS_BATT_SOC: "cms_batt_soc",
    F_CMS_DSG_REM_TIME: "cms_dsg_rem_time",
    F_CMS_CHG_REM_TIME: "cms_chg_rem_time",
    F_FLOW_INFO_AC_OUT: "flow_info_ac_out",
    F_POW_GET_AC_OUT: "pow_get_ac_out",
}

# Field numbers worth a second look in the 'read --raw' dump: the neighbourhoods
# of things we already decode, where a sibling field we have missed is most
# likely to be hiding.
SUSPECT_FIELDS: frozenset[int] = (
    frozenset(range(9, 17)) | frozenset(range(33, 38)) | frozenset(range(360, 369))
)


def flow_is_on(value: Any) -> bool:
    """Interpret a ``flow_info_*`` value as a port on/off state.

    These are bitmasks, not booleans: a real DELTA 3 reports 14 (0b1110) for an
    active port and 4 (0b0100) for an inactive one, so a plain ``bool()`` reads
    every inactive-but-present port as ON. Only the low two bits carry the
    state; this matches the check the EcoFlow app itself makes (and
    ha-ef-ble's ``flow_is_on``). Values whose low bits are 0b01 are unknown and
    treated as off.
    """
    return (int(value) & 0b11) in (0b10, 0b11)


# The frame that carries DisplayPropertyUpload.
DISPLAY_SRC = 0x02
DISPLAY_CMD_SET = 0xFE
DISPLAY_CMD_ID = 0x15

# --- ConfigWrite field numbers (control) ----------------------------------- #
CFG_DC_12V_OUT_OPEN = 18  # 12V DC output enable (bool)
CFG_USB_OPEN = 19  # USB output enable (bool)
CFG_AC_OUT_OPEN = 76  # AC output enable (bool)

# ConfigWrite frame addressing (from ha-ef-ble delta3 _send_config_packet).
CONFIG_SRC = 0x20
CONFIG_DST = 0x02
CONFIG_CMD_SET = 0xFE
CONFIG_CMD_ID = 0x11
CONFIG_VERSION = 0x13


def merge_display_payload(state: DeviceState, payload: bytes) -> None:
    """Merge a DisplayPropertyUpload protobuf payload into ``state``.

    The device only includes changed fields in each upload, so absent fields
    keep their last-known value.
    """
    fields = protocol.decode_message(payload)

    if (v := fields.get(F_CMS_BATT_SOC)) is not None:
        state.soc_percent = round(float(v), 1)
    elif (v := fields.get(F_BMS_BATT_SOC)) is not None:
        state.soc_percent = round(float(v), 1)

    if (v := fields.get(F_POW_GET_AC_IN)) is not None:
        state.ac_input_watts = round(float(v), 1)
    if (v := fields.get(F_POW_GET_AC_OUT)) is not None:
        # AC output is reported negative; expose it as a positive load.
        state.ac_output_watts = round(abs(float(v)), 1)
    if (v := fields.get(F_POW_IN_SUM_W)) is not None:
        state.input_watts = round(float(v), 1)
    if (v := fields.get(F_POW_OUT_SUM_W)) is not None:
        state.output_watts = round(float(v), 1)
    _merge_usb(state, fields)

    if (v := fields.get(F_POW_GET_12V)) is not None:
        state.dc_output_watts = round(abs(float(v)), 1)

    if (v := fields.get(F_PLUG_IN_INFO_AC_CHARGER_FLAG)) is not None:
        state.ac_input_present = bool(v)
    if (v := fields.get(F_FLOW_INFO_AC_OUT)) is not None:
        state.ac_output_on = flow_is_on(v)
    if (v := fields.get(F_FLOW_INFO_12V)) is not None:
        state.dc_output_on = flow_is_on(v)
    # One master switch drives all four USB ports, so whichever port's flag
    # a (partial) frame happens to carry answers for the lot.
    for number in (
        F_FLOW_INFO_QCUSB1,
        F_FLOW_INFO_QCUSB2,
        F_FLOW_INFO_TYPEC1,
        F_FLOW_INFO_TYPEC2,
    ):
        if (v := fields.get(number)) is not None:
            state.usb_output_on = flow_is_on(v)

    if (v := fields.get(F_CMS_CHG_REM_TIME)) is not None:
        state.remain_charge_minutes = int(v)
    if (v := fields.get(F_CMS_DSG_REM_TIME)) is not None:
        state.remain_discharge_minutes = int(v)
    if (v := fields.get(F_ERRCODE)) is not None:
        state.error_code = int(v)


def _merge_usb(state: DeviceState, fields: dict[int, Any]) -> None:
    """Update the per-port USB readings, then recompute the two totals.

    Ports report negative watts (as AC output does); we expose the load as a
    positive number. The DELTA 3's two USB-A ports are both fast-charge, so
    they take the first two of the state's four USB-A slots.
    """
    for number, attr in (
        (F_POW_GET_QCUSB1, "usb_a1_watts"),
        (F_POW_GET_QCUSB2, "usb_a2_watts"),
        (F_POW_GET_TYPEC1, "usb_c1_watts"),
        (F_POW_GET_TYPEC2, "usb_c2_watts"),
    ):
        if (v := fields.get(number)) is not None:
            setattr(state, attr, round(abs(float(v)), 1))
    state.recompute_usb_totals()


def is_display_packet(packet: Packet) -> bool:
    return (
        packet.src == DISPLAY_SRC
        and packet.cmd_set == DISPLAY_CMD_SET
        and packet.cmd_id == DISPLAY_CMD_ID
    )


def _config_packet(field_number: int, enabled: bool) -> Packet:
    payload = protocol.encode_message(
        [ProtoField(field_number, protocol.WIRE_VARINT, 1 if enabled else 0)]
    )
    return Packet(
        src=CONFIG_SRC,
        dst=CONFIG_DST,
        cmd_set=CONFIG_CMD_SET,
        cmd_id=CONFIG_CMD_ID,
        payload=payload,
        dsrc=0x01,
        ddst=0x01,
        version=CONFIG_VERSION,
    )


def set_ac_enabled_packet(enabled: bool) -> Packet:
    """Build a ConfigWrite packet to toggle AC output."""
    return _config_packet(CFG_AC_OUT_OPEN, enabled)


def set_usb_enabled_packet(enabled: bool) -> Packet:
    """Build a ConfigWrite packet to toggle USB output."""
    return _config_packet(CFG_USB_OPEN, enabled)


def set_dc_enabled_packet(enabled: bool) -> Packet:
    """Build a ConfigWrite packet to toggle 12V DC output."""
    return _config_packet(CFG_DC_12V_OUT_OPEN, enabled)


@dataclass(frozen=True, slots=True)
class Delta3Driver:
    """The DELTA 3 (``pd335``) protocol as a device driver."""

    name: str = "delta3"
    packet_version: int = 3
    # The captured frames in tests/data carry a non-zero seq[0] and only decode
    # to sensible telemetry once de-obfuscated, so this family does XOR.
    xor_payload: bool = True

    def handle_packet(self, state: DeviceState, packet: Packet) -> bool:
        if not is_display_packet(packet):
            return False
        merge_display_payload(state, packet.payload)
        return True

    def output_packet(self, kind: str, enabled: bool) -> Packet:
        if kind == "ac":
            return set_ac_enabled_packet(enabled)
        if kind == "usb":
            return set_usb_enabled_packet(enabled)
        if kind == "dc":
            return set_dc_enabled_packet(enabled)
        raise ValueError(f"unknown output: {kind}")


DRIVER = Delta3Driver()
