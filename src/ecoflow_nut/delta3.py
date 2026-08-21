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

from . import protocol
from .protocol import Packet, ProtoField
from .state import DeviceState

__all__ = [
    "DRIVER",
    "Delta3Driver",
    "DeviceState",
    "merge_display_payload",
    "set_ac_enabled_packet",
    "set_dc_enabled_packet",
    "set_usb_enabled_packet",
]

# --- DisplayPropertyUpload field numbers (read) ---------------------------- #
F_ERRCODE = 1
F_POW_IN_SUM_W = 3  # total input watts (float)
F_POW_OUT_SUM_W = 4  # total output watts (float)
F_POW_GET_QCUSB1 = 9  # USB-A port 1 watts (float)
F_POW_GET_TYPEC1 = 11  # USB-C port 1 watts (float)
F_PLUG_IN_INFO_AC_CHARGER_FLAG = 202  # AC charger connected (bool/uint32)
F_BMS_BATT_SOC = 242  # BMS state of charge (float %)
F_CMS_BATT_SOC = 262  # combined/displayed state of charge (float %)
F_CMS_DSG_REM_TIME = 268  # discharge remaining time (uint32, minutes)
F_CMS_CHG_REM_TIME = 269  # charge remaining time (uint32, minutes)
F_FLOW_INFO_AC_OUT = 367  # AC output flow state (uint32, 0=off)
F_POW_GET_AC_IN = 54  # AC input watts (float)
F_POW_GET_AC_OUT = 368  # AC output watts (float, reported negative)

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
    if (v := fields.get(F_POW_GET_QCUSB1)) is not None:
        state.usb_output_watts = round(abs(float(v)), 1)
    if (v := fields.get(F_POW_GET_TYPEC1)) is not None:
        state.usbc_output_watts = round(abs(float(v)), 1)

    if (v := fields.get(F_PLUG_IN_INFO_AC_CHARGER_FLAG)) is not None:
        state.ac_input_present = bool(v)
    if (v := fields.get(F_FLOW_INFO_AC_OUT)) is not None:
        state.ac_output_on = bool(v)

    if (v := fields.get(F_CMS_CHG_REM_TIME)) is not None:
        state.remain_charge_minutes = int(v)
    if (v := fields.get(F_CMS_DSG_REM_TIME)) is not None:
        state.remain_discharge_minutes = int(v)
    if (v := fields.get(F_ERRCODE)) is not None:
        state.error_code = int(v)


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

    def set_ac_enabled_packet(self, enabled: bool) -> Packet:
        return set_ac_enabled_packet(enabled)

    def set_usb_enabled_packet(self, enabled: bool) -> Packet:
        return set_usb_enabled_packet(enabled)

    def set_dc_enabled_packet(self, enabled: bool) -> Packet:
        return set_dc_enabled_packet(enabled)


DRIVER = Delta3Driver()
