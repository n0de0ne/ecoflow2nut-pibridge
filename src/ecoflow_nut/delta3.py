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

# Field number -> the name we know it by, for the 'read --raw' diagnostic. The
# device sends far more fields than we decode; this labels the ones we do so an
# unknown field stands out in the dump.
DISPLAY_FIELD_NAMES: dict[int, str] = {
    F_ERRCODE: "errcode",
    F_POW_IN_SUM_W: "pow_in_sum_w",
    F_POW_OUT_SUM_W: "pow_out_sum_w",
    F_POW_GET_QCUSB1: "pow_get_qcusb1",
    F_POW_GET_TYPEC1: "pow_get_typec1",
    F_POW_GET_AC_IN: "pow_get_ac_in",
    F_PLUG_IN_INFO_AC_CHARGER_FLAG: "plug_in_info_ac_charger_flag",
    F_BMS_BATT_SOC: "bms_batt_soc",
    F_CMS_BATT_SOC: "cms_batt_soc",
    F_CMS_DSG_REM_TIME: "cms_dsg_rem_time",
    F_CMS_CHG_REM_TIME: "cms_chg_rem_time",
    F_FLOW_INFO_AC_OUT: "flow_info_ac_out",
    F_POW_GET_AC_OUT: "pow_get_ac_out",
}

# Field numbers worth a second look when hunting for undecoded ports/flags. The
# USB block is 9-12 (two USB-A + two USB-C on a DELTA 3, of which we decode only
# the first of each), and flow_info_ac_out at 367 sits at the end of a run of
# varints that are very likely its per-port siblings.
SUSPECT_FIELDS: frozenset[int] = frozenset(range(9, 13)) | frozenset(range(360, 368))

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


@dataclass(slots=True)
class DeviceState:
    """Accumulated DELTA 3 telemetry.

    The device only includes changed fields in each DisplayPropertyUpload, so we
    merge successive frames into a single rolling state and keep last-known
    values for fields that are absent in a given frame.
    """

    soc_percent: float | None = None
    ac_input_watts: float | None = None
    ac_output_watts: float | None = None
    input_watts: float | None = None
    output_watts: float | None = None
    usb_output_watts: float | None = None
    usbc_output_watts: float | None = None
    ac_input_present: bool | None = None
    ac_output_on: bool | None = None
    remain_charge_minutes: int | None = None
    remain_discharge_minutes: int | None = None
    error_code: int | None = None

    @property
    def is_complete(self) -> bool:
        """True once the essential value (SoC) has been seen.

        The DELTA 3 does not include every field in every frame -- notably the
        AC-charger flag (field 202) is often absent -- so we only require SoC to
        start publishing. AC presence falls back to AC input watts when the flag
        has not been seen yet (see ``nut_writer.derive_status``).
        """
        return self.soc_percent is not None

    def merge_display_payload(self, payload: bytes) -> None:
        """Merge a DisplayPropertyUpload protobuf payload into this state."""
        fields = protocol.decode_message(payload)

        if (v := fields.get(F_CMS_BATT_SOC)) is not None:
            self.soc_percent = round(float(v), 1)
        elif (v := fields.get(F_BMS_BATT_SOC)) is not None:
            self.soc_percent = round(float(v), 1)

        if (v := fields.get(F_POW_GET_AC_IN)) is not None:
            self.ac_input_watts = round(float(v), 1)
        if (v := fields.get(F_POW_GET_AC_OUT)) is not None:
            # AC output is reported negative; expose it as a positive load.
            self.ac_output_watts = round(abs(float(v)), 1)
        if (v := fields.get(F_POW_IN_SUM_W)) is not None:
            self.input_watts = round(float(v), 1)
        if (v := fields.get(F_POW_OUT_SUM_W)) is not None:
            self.output_watts = round(float(v), 1)
        if (v := fields.get(F_POW_GET_QCUSB1)) is not None:
            self.usb_output_watts = round(abs(float(v)), 1)
        if (v := fields.get(F_POW_GET_TYPEC1)) is not None:
            self.usbc_output_watts = round(abs(float(v)), 1)

        if (v := fields.get(F_PLUG_IN_INFO_AC_CHARGER_FLAG)) is not None:
            self.ac_input_present = bool(v)
        if (v := fields.get(F_FLOW_INFO_AC_OUT)) is not None:
            self.ac_output_on = bool(v)

        if (v := fields.get(F_CMS_CHG_REM_TIME)) is not None:
            self.remain_charge_minutes = int(v)
        if (v := fields.get(F_CMS_DSG_REM_TIME)) is not None:
            self.remain_discharge_minutes = int(v)
        if (v := fields.get(F_ERRCODE)) is not None:
            self.error_code = int(v)

    def is_display_packet(self, packet: Packet) -> bool:
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
