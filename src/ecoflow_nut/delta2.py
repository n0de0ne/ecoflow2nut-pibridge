"""DELTA 2 generation protocol: fixed-width heartbeats and V2 control commands.

Covers the EcoFlow DELTA 2 family -- DELTA 2 (``R331``/``R335``), **DELTA 2 Max**
(``R351``/``R354``), DELTA 2 Black (``R701``) and DELTA 3 1500 (``D361``/``D365``)
-- which share one protocol that is **completely different** from the DELTA 3's:

* frames are EcoFlow **V2** (no ``dsrc``/``ddst``, payload starts at byte 16),
  not V3;
* telemetry is **not protobuf**. Each subsystem pushes a packed little-endian
  struct, identified by the frame's ``(src, cmd_set, cmd_id)`` rather than by
  field tags inside the payload (see :mod:`ecoflow_nut.rawstruct`);
* control commands are per-function ``cmd_id`` values with small raw payloads,
  not a single ``ConfigWrite`` message.

Layouts and command opcodes mirror the reverse engineering in
https://github.com/rabits/ha-ef-ble (Apache-2.0); the code here is our own.

Every field the NUT bridge actually needs -- SoC, AC input/output watts, AC
input voltage, remaining runtime, AC on/off -- comes from the EMS, BMS and INV
heartbeats, whose layouts are shared across the *whole* generation. Only the PD
and MPPT tails differ per model, so an unrecognised sibling still yields a
working UPS from the shared messages alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import rawstruct
from .protocol import Packet
from .rawstruct import Layout
from .state import DeviceState

# --------------------------------------------------------------------------- #
# Frame addressing
# --------------------------------------------------------------------------- #
# Which subsystem a heartbeat came from, as (src, cmd_set, cmd_id). The INV
# heartbeat is matched on src/cmd_id only -- its cmd_set varies by model.
SRC_PD = 0x02
SRC_EMS = 0x03
SRC_INV = 0x04
SRC_MPPT = 0x05

CMD_SET_HEARTBEAT = 0x20
CMD_ID_HEARTBEAT = 0x02
CMD_ID_BMS_HEARTBEAT = 0x32

# Control commands. All are V2 frames sent from src 0x21 (the phone/controller).
CTRL_SRC = 0x21
CMD_SET_CONTROL = 0x20
CMD_ID_SET_USB = 0x22  # dst 0x02 (PD)
CMD_ID_SET_DC_12V = 0x51  # dst 0x05 (MPPT)
CMD_ID_SET_AC = 0x42  # dst is model specific (see Delta2Driver.ac_dst)

PACKET_VERSION = 2

# Below this many volts on the AC input we consider the mains gone. Well under
# any nominal supply (100 V / 120 V / 230 V) but far above sensor noise.
AC_PRESENT_MIN_VOLTS = 50.0


# --------------------------------------------------------------------------- #
# Heartbeat layouts
# --------------------------------------------------------------------------- #
# PD (src 0x02) -- ports, totals and a coarse integer SoC. Shared prefix for
# every model in the generation; each model appends its own tail.
PD_BASE: Layout = (
    ("model", "B"),
    ("error_code", "4s"),
    ("sys_ver", "4s"),
    ("wifi_ver", "4s"),
    ("wifi_auto_recovery", "B"),
    ("soc", "B"),
    ("watts_out_sum", "H"),
    ("watts_in_sum", "H"),
    ("remain_time", "i"),
    ("quiet_mode", "B"),
    ("dc_out_state", "B"),
    ("usb1_watt", "B"),
    ("usb2_watt", "B"),
    ("qc_usb1_watt", "B"),
    ("qc_usb2_watt", "B"),
    ("typec1_watts", "B"),
    ("typec2_watts", "B"),
    ("typec1_temp", "B"),
    ("typec2_temp", "B"),
    ("car_state", "B"),
    ("car_watts", "B"),
    ("car_temp", "B"),
    ("standby_min", "H"),
    ("lcd_off_sec", "H"),
    ("lcd_brightness", "B"),
    ("dc_chg_power", "I"),
    ("sun_chg_power", "I"),
    ("ac_chg_power", "I"),
    ("dc_dsg_power", "I"),
    ("ac_dsg_power", "I"),
    ("usb_used_time", "I"),
    ("usb_qc_used_time", "I"),
    ("type_c_used_time", "I"),
    ("car_used_time", "I"),
    ("inv_used_time", "I"),
    ("dc_in_used_time", "I"),
    ("mppt_used_time", "I"),
)

# DELTA 2 Max / DELTA Pro PD tail ("mr350" firmware family).
PD_MR350_CORE: Layout = PD_BASE + (
    ("bms_kit_state", "H"),
    ("other_kit_state", "B"),
    ("reversed", "H"),
    ("sys_chg_flag", "B"),
    ("wifi_rssi", "B"),
    ("wireless_watts", "B"),
    ("screen_state", "14s"),
)

PD_DELTA2_MAX: Layout = PD_MR350_CORE + (
    ("first_xt150_watts", "H"),
    ("second_xt150_watts", "H"),
    ("inv_in_watts", "H"),
    ("inv_out_watts", "H"),
    ("pv1_charge_type", "B"),
    ("pv1_charge_watts", "H"),
    ("pv2_charge_type", "B"),
    ("pv2_charge_watts", "H"),
    ("pv_charge_prio_set", "B"),
    ("ac_auto_on_cfg_set", "B"),
    ("ac_auto_out_config", "B"),
    ("main_ac_out_soc", "B"),
    ("ac_auto_out_pause", "B"),
    ("watthis_config", "B"),
    ("bp_power_soc", "B"),
    ("hysteresis_add", "B"),
    ("relay_switch_cnt", "I"),
)

# DELTA 2 PD tail ("mr330" firmware family).
PD_MR330_CORE: Layout = PD_BASE + (
    ("reverser", "H"),
    ("screen_state", "14s"),
    ("ext_rj45_port", "B"),
    ("ext_3p8_port", "B"),
    ("ext_4p8_port", "B"),
    ("syc_chg_dsg_state", "B"),
    ("wifi_rssi", "B"),
    ("wireless_watts", "B"),
)

PD_DELTA2: Layout = PD_MR330_CORE + (
    ("charge_type", "B"),
    ("ac_input_watts", "H"),
    ("ac_output_watts", "H"),
    ("dc_pv_input_watts", "H"),
    ("dc_pv_output_watts", "H"),
    ("cfg_ac_enabled", "B"),
    ("pv_priority", "B"),
    ("ac_auto_on", "B"),
    ("watthis_config", "B"),
    ("bp_power_soc", "B"),
    ("hysteresis_soc", "B"),
    ("reply_switch_cnt", "I"),
    ("ac_auto_out_config", "B"),
    ("min_auto_soc", "B"),
    ("ac_auto_out_pause", "B"),
    ("schedule_id", "I"),
    ("heartbeat_duration", "I"),
    ("bkw_watts_in_power", "H"),
    ("input_power_limit_flag", "B"),
    ("ac_charge_flag", "B"),
    ("cloud_ctrl_en", "B"),
    ("redun_charge_flag", "B"),
)

# EMS (src 0x03, cmd_id 0x02) -- the SoC actually shown on the LCD, plus the
# device's own remaining-time estimates. Identical across the generation.
EMS_HEARTBEAT: Layout = (
    ("chg_state", "B"),
    ("chg_cmd", "B"),
    ("dsg_cmd", "B"),
    ("chg_vol", "I"),
    ("chg_amp", "I"),
    ("fan_level", "B"),
    ("max_charge_soc", "B"),
    ("bms_model", "B"),
    ("lcd_show_soc", "B"),
    ("open_ups_flag", "B"),
    ("bms_warning_state", "B"),
    ("chg_remain_time", "I"),
    ("dsg_remain_time", "I"),
    ("ems_is_normal_flag", "B"),
    ("f32_lcd_show_soc", "f"),
    ("bms_is_connt", "3s"),
    ("max_available_num", "B"),
    ("open_bms_idx", "B"),
    ("para_vol_min", "I"),
    ("para_vol_max", "I"),
    ("min_dsg_soc", "B"),
    ("open_oil_eb_soc", "B"),
    ("close_oil_eb_soc", "B"),
)

# BMS (src 0x03/0x06, cmd_id 0x32) -- pack-level SoC, used when the EMS is quiet.
BMS_HEARTBEAT: Layout = (
    ("num", "B"),
    ("type", "B"),
    ("cell_id", "B"),
    ("err_code", "I"),
    ("sys_ver", "I"),
    ("soc", "B"),
    ("vol", "I"),
    # Signed: pack current runs negative while discharging, and read unsigned a
    # discharge decodes as roughly 4.3 billion. Same four bytes either way, so
    # the layout size is unchanged.
    ("amp", "i"),
    ("temp", "B"),
    ("open_bms_idx", "B"),
    ("design_cap", "I"),
    ("remain_cap", "I"),
    ("full_cap", "I"),
    ("cycles", "I"),
    ("soh", "B"),
    ("max_cell_vol", "H"),
    ("min_cell_vol", "H"),
    ("max_cell_temp", "B"),
    ("min_cell_temp", "B"),
    ("max_mos_temp", "B"),
    ("min_mos_temp", "B"),
    ("bms_fault", "B"),
    ("bq_sys_stat_reg", "B"),
    ("tag_chg_amp", "I"),
    ("f32_show_soc", "f"),
    ("input_watts", "I"),
    ("output_watts", "I"),
    ("remain_time", "I"),
)

# INV (src 0x04, cmd_id 0x02) -- the inverter: AC in/out watts, mains voltage and
# the AC output enable flag. This is the message the UPS status is built from.
INV_BASE: Layout = (
    ("err_code", "I"),
    ("sys_ver", "I"),
    ("charger_type", "B"),
    ("input_watts", "H"),
    ("output_watts", "H"),
    ("inv_type", "B"),
    ("inv_out_vol", "I"),
    ("inv_out_amp", "I"),
    ("inv_out_freq", "B"),
)

INV_DELTA: Layout = INV_BASE + (
    ("ac_in_vol", "I"),
    ("ac_in_amp", "I"),
    ("ac_in_freq", "B"),
    ("out_temp", "H"),
    ("dc_in_vol", "I"),
    ("dc_in_amp", "I"),
    ("dc_in_temp", "H"),
    ("fan_state", "B"),
    ("cfg_ac_enabled", "B"),
    ("cfg_ac_xboost", "B"),
    ("cfg_ac_out_voltage", "I"),
    ("cfg_ac_out_freq", "B"),
    ("cfg_ac_work_mode", "B"),
    ("cfg_pause_flag", "B"),
    ("ac_dip_switch", "B"),
    ("cfg_fast_chg_watts", "H"),
    ("cfg_slow_chg_watts", "H"),
    ("standby_mins", "H"),
    ("discharge_type", "B"),
    ("ac_passby_auto_en", "B"),
    ("pr_balance_mode", "B"),
    ("ac_chg_rated_power", "H"),
    ("cfg_gfci_enable", "B"),
)

# MPPT (src 0x05, cmd_id 0x02) -- solar / car input and the 12 V output. Decoded
# for the sniffer's benefit; the UPS view does not depend on it.
MPPT_BASE: Layout = (
    ("fault_code", "I"),
    ("sw_ver", "4s"),
    ("in_vol", "I"),
    ("in_amp", "I"),
    ("in_watts", "H"),
    ("out_val", "I"),
    ("out_amp", "I"),
    ("out_watts", "H"),
    ("mppt_temp", "h"),
    ("xt60_chg_type", "B"),
    ("cfg_chg_type", "B"),
    ("chg_type", "B"),
    ("chg_state", "B"),
    ("dcdc_12v_vol", "I"),
    ("dcdc_12v_amp", "I"),
    ("dcdc_12v_watts", "H"),
    ("car_out_vol", "I"),
    ("car_out_amp", "I"),
    ("car_out_watts", "H"),
    ("car_temp", "h"),
    ("car_state", "B"),
    ("dc24v_temp", "h"),
    ("dc24v_state", "B"),
    ("chg_pause_flag", "B"),
    ("cfg_dc_chg_current", "I"),
)

MPPT_MR350: Layout = MPPT_BASE + (
    ("pv2_in_vol", "I"),
    ("pv2_in_amp", "I"),
    ("pv2_in_watts", "H"),
    ("pv2_mppt_temp", "H"),
    ("pv2_xt60_chg_type", "B"),
    ("pv2_cfg_chg_type", "B"),
    ("pv2_chg_type", "B"),
    ("pv2_chg_state", "B"),
    ("pv2_chg_pause_flag", "B"),
    ("car_standby_mins", "H"),
)

MPPT_MR330: Layout = MPPT_BASE + (
    ("beep_state", "B"),
    ("cfg_ac_enabled", "B"),
    ("cfg_ac_xboost", "B"),
    ("cfg_ac_out_voltage", "I"),
    ("cfg_ac_out_freq", "B"),
    ("cfg_chg_watts", "H"),
    ("ac_standby_mins", "H"),
    ("discharge_type", "B"),
    ("car_standby_mins", "H"),
    ("power_standby_mins", "H"),
    ("screen_standby_mins", "H"),
    ("pay_flag", "H"),
)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Delta2Driver:
    """Decodes and controls one DELTA 2 generation model.

    ``pd_layout``/``mppt_layout`` carry the model-specific heartbeat tails, and
    ``ac_dst`` the subsystem that owns the AC relay -- the inverter (0x04) on the
    DELTA 2 Max, the MPPT board (0x05) on the DELTA 2.
    """

    name: str
    pd_layout: Layout
    mppt_layout: Layout
    ac_dst: int
    # The DELTA 2 reports AC input watts in its PD heartbeat; the DELTA 2 Max
    # reports them on the inverter instead.
    ac_watts_from_pd: bool
    # Whether payloads are XOR-obfuscated with seq[0]. This is per *model*, not
    # per generation: the DELTA 2 obfuscates, the DELTA 2 Max does not. It only
    # bites on frames with a non-zero seq[0], so a wrong value shows up as a
    # recognised message full of nonsense values -- check it with ``sniff``.
    xor_payload: bool
    packet_version: int = PACKET_VERSION
    # This generation broadcasts its heartbeats unprompted, so an echo buys
    # nothing and costs a full-size write several times a second.
    ack_frames: bool = False

    # -- read ---------------------------------------------------------------- #
    def handle_packet(self, state: DeviceState, packet: Packet) -> bool:
        """Merge a heartbeat into ``state``. False if we don't know the frame."""
        src, cmd_set, cmd_id = packet.src, packet.cmd_set, packet.cmd_id
        payload = packet.payload

        if cmd_id == CMD_ID_HEARTBEAT and cmd_set == CMD_SET_HEARTBEAT:
            if src == SRC_PD:
                self._merge_pd(state, rawstruct.unpack(self.pd_layout, payload))
                return True
            if src == SRC_EMS:
                _merge_ems(state, rawstruct.unpack(EMS_HEARTBEAT, payload))
                return True
            if src == SRC_MPPT:
                _merge_mppt(state, rawstruct.unpack(self.mppt_layout, payload))
                return True
        # The inverter's cmd_set differs between models, so match on src alone.
        if src == SRC_INV and cmd_id == CMD_ID_HEARTBEAT:
            _merge_inv(state, rawstruct.unpack(INV_DELTA, payload))
            return True
        if cmd_id == CMD_ID_BMS_HEARTBEAT and cmd_set == CMD_SET_HEARTBEAT:
            # src 0x03 is the built-in pack; 0x06+ are attached extra batteries,
            # which report their own SoC and must not overwrite the main one.
            if src == SRC_EMS:
                _merge_bms(state, rawstruct.unpack(BMS_HEARTBEAT, payload))
            return True
        return False

    def _merge_pd(self, state: DeviceState, pd: dict[str, Any]) -> None:
        if (v := pd.get("watts_in_sum")) is not None:
            state.input_watts = float(v)
        if (v := pd.get("watts_out_sum")) is not None:
            state.output_watts = float(v)
        _merge_pd_ports(state, pd)
        if (v := pd.get("soc")) is not None:
            state.update_soc(float(v), "pd")
        if self.ac_watts_from_pd:
            if (v := pd.get("ac_input_watts")) is not None:
                state.ac_input_watts = float(v)
            if (v := pd.get("ac_output_watts")) is not None:
                state.ac_output_watts = float(v)

    # -- control ------------------------------------------------------------- #
    def output_packet(self, kind: str, enabled: bool) -> Packet:
        if kind == "ac":
            return self.set_ac_enabled_packet(enabled)
        if kind == "usb":
            return self.set_usb_enabled_packet(enabled)
        if kind == "dc":
            return self.set_dc_enabled_packet(enabled)
        raise ValueError(f"unknown output: {kind}")

    def set_ac_enabled_packet(self, enabled: bool) -> Packet:
        """Toggle the AC output bank.

        The trailing ``0xFF`` bytes are "leave unchanged" placeholders for the
        X-Boost / output-voltage / frequency settings this opcode also carries.
        """
        payload = bytes([1 if enabled else 0]) + b"\xff" * 6
        return self._control(self.ac_dst, CMD_ID_SET_AC, payload)

    def set_usb_enabled_packet(self, enabled: bool) -> Packet:
        return self._control(SRC_PD, CMD_ID_SET_USB, bytes([1 if enabled else 0]))

    def set_dc_enabled_packet(self, enabled: bool) -> Packet:
        return self._control(SRC_MPPT, CMD_ID_SET_DC_12V, bytes([1 if enabled else 0]))

    def _control(self, dst: int, cmd_id: int, payload: bytes) -> Packet:
        return Packet(
            src=CTRL_SRC,
            dst=dst,
            cmd_set=CMD_SET_CONTROL,
            cmd_id=cmd_id,
            payload=payload,
            version=PACKET_VERSION,
        )


def _merge_pd_ports(state: DeviceState, pd: dict[str, Any]) -> None:
    """Update the USB and 12V DC port readings from a PD heartbeat.

    This generation has six USB ports, not the DELTA 3's four: two standard
    USB-A, two fast-charge USB-A and two USB-C. Reading only the first of each
    kind -- as this driver first did -- reports 0 W for anything charging on,
    say, the second USB-C port, which is the one most people reach for.

    The 12V DC port is EcoFlow's "car" port throughout this protocol, so its
    switch and its draw arrive under those names.

    There is no USB switch flag anywhere in this generation's PD heartbeat --
    the AC and 12V ports each have one, USB simply does not -- so the state is
    read off the draw instead. Power leaving a port that can be switched off
    proves the switch is closed: that is an entailment, not a guess, and it is
    the only claim made here. Zero draw stays unknown, because a disabled bank
    and an enabled bank with nothing plugged in look identical.
    """
    usb_watts: list[float] = []
    for field, attr in (
        ("usb1_watt", "usb_a1_watts"),
        ("usb2_watt", "usb_a2_watts"),
        ("qc_usb1_watt", "usb_a3_watts"),
        ("qc_usb2_watt", "usb_a4_watts"),
        ("typec1_watts", "usb_c1_watts"),
        ("typec2_watts", "usb_c2_watts"),
    ):
        if (v := pd.get(field)) is not None:
            setattr(state, attr, float(v))
            usb_watts.append(float(v))
    state.recompute_usb_totals()
    if any(w > 0 for w in usb_watts):
        state.usb_output_on = True

    if (v := pd.get("car_watts")) is not None:
        state.dc_output_watts = float(v)
    if (v := pd.get("dc_out_state")) is not None:
        state.dc_output_on = bool(v)


# What the device sends for "I have no estimate": 5999 minutes, which renders
# as a perfectly plausible-looking 99h 59m. A full battery on mains reports it
# for charge time, and it would otherwise reach NUT's battery.runtime and the
# telemetry history as if it were a real reading.
NO_ESTIMATE_MINUTES = 5999


def _remain_minutes(value: Any) -> int | None:
    """A remaining-time field, or None where the device means "unknown"."""
    minutes = int(value)
    if minutes >= NO_ESTIMATE_MINUTES or minutes < 0:
        return None
    return minutes


def _merge_ems(state: DeviceState, ems: dict[str, Any]) -> None:
    if (v := ems.get("f32_lcd_show_soc")) is not None:
        state.update_soc(float(v), "ems")
    elif (v := ems.get("lcd_show_soc")) is not None:
        state.update_soc(float(v), "ems")
    if (v := ems.get("chg_remain_time")) is not None:
        state.remain_charge_minutes = _remain_minutes(v)
    if (v := ems.get("dsg_remain_time")) is not None:
        state.remain_discharge_minutes = _remain_minutes(v)
    # The limits the station enforces on itself, set in the EcoFlow app. Read
    # 100 and 0 on the capture, i.e. no limit in force -- which is exactly the
    # case that has to look different from "charges to 90% and stops".
    if (v := ems.get("max_charge_soc")) is not None:
        state.charge_limit_percent = int(v)
    if (v := ems.get("min_dsg_soc")) is not None:
        state.discharge_limit_percent = int(v)
    if (v := ems.get("fan_level")) is not None:
        state.fan_on = int(v) > 0


def _merge_bms(state: DeviceState, bms: dict[str, Any]) -> None:
    if (v := bms.get("f32_show_soc")) is not None:
        state.update_soc(float(v), "bms")
    elif (v := bms.get("soc")) is not None:
        state.update_soc(float(v), "bms")

    # Power at the pack, from its own volts and amps -- both in milli-units.
    #
    # This is the honest figure, and it is a long way from what the ports imply.
    # Captured on an E2000 taking solar: 326 W in and 214 W out of the station
    # makes the arithmetic say +112 W, while the pack was drawing 52.8 V at
    # 0.9 A -- about 46 W. The rest is conversion loss, and the arithmetic has
    # no way to see it.
    #
    # Confirmed twice over from the same capture, rather than assumed:
    # remain_cap/full_cap gives 92.4% against the 92.5% the device reports, and
    # its own chg_remain_time of 206 minutes over the 2979 mAh still to fill
    # implies 868 mA -- inside the 388..978 mA actually observed.
    #
    # The input_watts/output_watts pair sitting beside these is dead on this
    # firmware: both read 0 throughout a capture where the battery was plainly
    # charging. Kept only as a fallback for models that do fill them in.
    vol_mv, amp_ma = bms.get("vol"), bms.get("amp")
    if vol_mv is not None and amp_ma is not None:
        state.battery_watts = round(float(vol_mv) * float(amp_ma) / 1e6, 1)
    else:
        charge, discharge = bms.get("input_watts"), bms.get("output_watts")
        if charge is not None or discharge is not None:
            state.battery_watts = float(charge or 0.0) - float(discharge or 0.0)

    # Pack health. Every field here was confirmed live and self-consistent on
    # an E2000: remain_cap/full_cap gave 89.9% against the 90.5% the device
    # reported, full_cap/design_cap 98.2% at 1 cycle, and a 3 mV cell spread
    # across a 16S pack whose cell voltages sum to the measured pack voltage.
    for field, attr, scale in (
        ("temp", "battery_temp_c", 1.0),
        ("max_cell_temp", "cell_temp_max_c", 1.0),
        ("min_cell_temp", "cell_temp_min_c", 1.0),
        ("soh", "battery_soh_percent", 1.0),
    ):
        if (v := bms.get(field)) is not None:
            setattr(state, attr, round(float(v) * scale, 1))
    for field, attr in (
        ("max_cell_vol", "cell_mv_max"),
        ("min_cell_vol", "cell_mv_min"),
        ("cycles", "battery_cycles"),
        ("full_cap", "battery_full_mah"),
        ("design_cap", "battery_design_mah"),
    ):
        if (v := bms.get(field)) is not None:
            setattr(state, attr, int(v))


def _merge_mppt(state: DeviceState, mppt: dict[str, Any]) -> None:
    """Sum the PV channels into ``solar_input_watts``.

    The MPPT is the solar charge controller, and it owns the XT60 connector
    that doubles as the car/DC input -- so these watts are what that port is
    harvesting right now, whatever is plugged into it. Models with a single
    channel simply have no ``pv2_in_watts``.

    Deliberately not folded into ``ac_input_watts`` or the UPS status: solar is
    not a utility feed, and treating it as one would report "on line" during an
    outage right up until dusk.
    """
    channels = [mppt.get("in_watts"), mppt.get("pv2_in_watts")]
    present = [float(v) for v in channels if v is not None]
    if present:
        state.solar_input_watts = round(sum(present), 1)


def _merge_inv(state: DeviceState, inv: dict[str, Any]) -> None:
    if (v := inv.get("input_watts")) is not None:
        state.ac_input_watts = float(v)
    if (v := inv.get("output_watts")) is not None:
        state.ac_output_watts = float(v)
    if (v := inv.get("cfg_ac_enabled")) is not None:
        state.ac_output_on = bool(v)
    if (v := inv.get("err_code")) is not None:
        state.error_code = int(v)
    if (v := inv.get("out_temp")) is not None:
        state.inverter_temp_c = round(float(v), 1)
    if (v := inv.get("fan_state")) is not None:
        state.fan_on = int(v) > 0
    # What the unit will actually pull from the wall. cfg_slow_chg_watts is the
    # user-set ceiling (400 W on the capture); ac_chg_rated_power is the
    # hardware maximum (2400 W) and is not what it will do.
    if (v := inv.get("cfg_slow_chg_watts")) is not None:
        state.ac_charge_watts = int(v)
    # Voltages and currents are reported in milli-units.
    if (v := inv.get("ac_in_vol")) is not None:
        volts = round(float(v) / 1000.0, 1)
        state.ac_input_voltage = volts
        # Mains voltage is the authoritative "grid is up" signal: unlike input
        # watts it stays high on a full battery drawing nothing.
        state.ac_input_present = volts >= AC_PRESENT_MIN_VOLTS


DELTA2_MAX = Delta2Driver(
    name="delta2max",
    pd_layout=PD_DELTA2_MAX,
    mppt_layout=MPPT_MR350,
    ac_dst=SRC_INV,
    ac_watts_from_pd=False,
    xor_payload=False,
)

DELTA2 = Delta2Driver(
    name="delta2",
    pd_layout=PD_DELTA2,
    mppt_layout=MPPT_MR330,
    ac_dst=SRC_MPPT,
    ac_watts_from_pd=True,
    xor_payload=True,
)


# Which heartbeat layout to show for a frame, used by the ``sniff`` command to
# annotate captures. Keyed by (src, cmd_id); cmd_set is not discriminating here.
def layouts_for(driver: Delta2Driver) -> dict[tuple[int, int], tuple[str, Layout]]:
    """Map ``(src, cmd_id)`` to a human label and layout, for diagnostics."""
    return {
        (SRC_PD, CMD_ID_HEARTBEAT): ("pd", driver.pd_layout),
        (SRC_EMS, CMD_ID_HEARTBEAT): ("ems", EMS_HEARTBEAT),
        (SRC_EMS, CMD_ID_BMS_HEARTBEAT): ("bms", BMS_HEARTBEAT),
        (0x06, CMD_ID_BMS_HEARTBEAT): ("bms.extra1", BMS_HEARTBEAT),
        (0x07, CMD_ID_BMS_HEARTBEAT): ("bms.extra2", BMS_HEARTBEAT),
        (SRC_INV, CMD_ID_HEARTBEAT): ("inv", INV_DELTA),
        (SRC_MPPT, CMD_ID_HEARTBEAT): ("mppt", driver.mppt_layout),
    }
