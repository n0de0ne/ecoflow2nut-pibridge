"""Model-independent telemetry state shared by every EcoFlow device driver.

Each driver (:mod:`ecoflow_nut.delta3`, :mod:`ecoflow_nut.delta2`) speaks a
different wire protocol but merges what it decodes into this one structure, so
everything downstream -- the NUT writer, the web UI, the telemetry stores -- is
written once against a single shape.

Devices only report *changed* values, so a driver merges successive frames into
a rolling state and keeps the last-known value for anything a given frame omits.
"""

from __future__ import annotations

from dataclasses import dataclass

# How much to trust each state-of-charge source, highest wins. A device may
# publish SoC from several subsystems at different resolutions (a coarse integer
# byte from the PD module, a float from the BMS, the float actually shown on the
# LCD from the EMS); ranking them stops a coarse source from clobbering a
# precise one that arrived moments earlier.
SOC_PRIORITY = {"pd": 0, "bms": 1, "ems": 2}


@dataclass(slots=True)
class DeviceState:
    """Accumulated telemetry for one power station."""

    soc_percent: float | None = None
    ac_input_watts: float | None = None
    ac_output_watts: float | None = None
    input_watts: float | None = None
    output_watts: float | None = None
    # Totals across all ports of each type -- what the UI, NUT and the telemetry
    # store consume.
    usb_output_watts: float | None = None
    usbc_output_watts: float | None = None
    # Per-port readings behind those totals. Held separately because frames are
    # partial: a frame carrying only port 1 must not wipe what port 2 last
    # reported, so the totals are recomputed from last-known values rather than
    # from whatever happened to arrive.
    #
    # Four USB-A slots because the count is model-dependent: the DELTA 3 has two
    # (both fast-charge), the DELTA 2 generation has four (two standard, two
    # fast-charge). A model simply leaves the slots it does not have at None.
    usb_a1_watts: float | None = None
    usb_a2_watts: float | None = None
    usb_a3_watts: float | None = None
    usb_a4_watts: float | None = None
    usb_c1_watts: float | None = None
    usb_c2_watts: float | None = None
    dc_output_watts: float | None = None
    ac_input_present: bool | None = None
    ac_output_on: bool | None = None
    usb_output_on: bool | None = None
    dc_output_on: bool | None = None
    remain_charge_minutes: int | None = None
    remain_discharge_minutes: int | None = None
    error_code: int | None = None
    # Power into (+) or out of (-) the battery itself, as the pack's own BMS
    # reports it. Distinct from inputs-minus-outputs: that arithmetic misses
    # conversion losses and cannot tell a full battery idling on mains from one
    # quietly trickling. ``None`` on models whose BMS does not report it, where
    # the UI falls back to the arithmetic.
    battery_watts: float | None = None
    # Live solar (PV) input in watts, summed across the device's PV channels.
    # On the DELTA 2 generation the solar and car/DC inputs share one physical
    # XT60 connector, driven by the MPPT charge controller -- so this is
    # whatever that port is currently harvesting. ``None`` on models that do
    # not report it.
    solar_input_watts: float | None = None
    # Mains voltage measured at the AC input, when the device reports it. A far
    # more dependable "grid is up" signal than input watts, which fall to ~0 on
    # a full battery with no load. ``None`` on models that do not report it.
    ac_input_voltage: float | None = None
    # Which subsystem last set ``soc_percent`` (see ``SOC_PRIORITY``). Only used
    # by drivers that receive SoC from more than one subsystem.
    soc_source: str | None = None

    # -- pack health -------------------------------------------------------- #
    # Everything below is reported by the battery itself rather than derived,
    # and every field here was confirmed carrying live, self-consistent values
    # on real hardware before being surfaced -- the BMS also publishes several
    # fields that are permanently zero on this firmware (see docs/PROTOCOL.md).
    #
    # Pack temperature in degrees Celsius. Maps to NUT's own
    # ``battery.temperature``, so every NUT client already knows what to do
    # with it.
    battery_temp_c: float | None = None
    # Hottest and coldest cell. The spread matters more than either number: a
    # pack whose cells diverge in temperature is a pack with a problem.
    cell_temp_max_c: float | None = None
    cell_temp_min_c: float | None = None
    # Highest and lowest cell voltage, in millivolts. Their difference is the
    # earliest warning a failing cell gives -- it widens long before capacity
    # or SoH move.
    cell_mv_max: int | None = None
    cell_mv_min: int | None = None
    # Full charge/discharge cycles, and the BMS's own health estimate (%).
    battery_cycles: int | None = None
    battery_soh_percent: float | None = None
    # Capacity the pack can hold now against its nameplate, both in mAh. The
    # ratio is capacity fade, which is measured rather than estimated.
    battery_full_mah: int | None = None
    battery_design_mah: int | None = None
    # Inverter heatsink temperature (deg C) and whether the fan is running.
    inverter_temp_c: float | None = None
    fan_on: bool | None = None
    # Limits the station is enforcing on itself, as set in the EcoFlow app.
    # These are why a station "will not charge past 90%" or "only draws 400 W",
    # and without them that behaviour looks like a fault.
    charge_limit_percent: int | None = None
    discharge_limit_percent: int | None = None
    ac_charge_watts: int | None = None

    @property
    def is_complete(self) -> bool:
        """True once the essential value (SoC) has been seen.

        Devices do not include every field in every frame -- notably AC presence
        is often absent -- so we only require SoC before publishing. AC presence
        falls back to input voltage and then watts (see
        ``nut_writer.derive_status``).
        """
        return self.soc_percent is not None

    def recompute_usb_totals(self) -> None:
        """Re-sum the USB-A and USB-C totals from the per-port readings.

        Sum last-known values rather than only what the current frame carried,
        so a partial frame cannot zero a port it simply did not mention. Each
        total stays None until at least one port of that type has been seen, so
        "not reported" reads differently from "reported zero".
        """
        usb_a = (
            self.usb_a1_watts,
            self.usb_a2_watts,
            self.usb_a3_watts,
            self.usb_a4_watts,
        )
        for total, ports in (
            ("usb_output_watts", usb_a),
            ("usbc_output_watts", (self.usb_c1_watts, self.usb_c2_watts)),
        ):
            if any(p is not None for p in ports):
                setattr(self, total, round(sum(p or 0.0 for p in ports), 1))

    def update_soc(self, value: float, source: str) -> None:
        """Set SoC from ``source``, unless a more trusted source already has.

        Once the EMS has reported, a coarser BMS or PD reading is ignored; if the
        EMS never reports, the lesser source keeps updating normally.
        """
        current = SOC_PRIORITY.get(self.soc_source or "", -1)
        if SOC_PRIORITY.get(source, -1) < current:
            return
        self.soc_percent = round(float(value), 1)
        self.soc_source = source
