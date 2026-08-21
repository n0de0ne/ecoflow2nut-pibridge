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
    usb_output_watts: float | None = None
    usbc_output_watts: float | None = None
    ac_input_present: bool | None = None
    ac_output_on: bool | None = None
    remain_charge_minutes: int | None = None
    remain_discharge_minutes: int | None = None
    error_code: int | None = None
    # Mains voltage measured at the AC input, when the device reports it. A far
    # more dependable "grid is up" signal than input watts, which fall to ~0 on
    # a full battery with no load. ``None`` on models that do not report it.
    ac_input_voltage: float | None = None
    # Which subsystem last set ``soc_percent`` (see ``SOC_PRIORITY``). Only used
    # by drivers that receive SoC from more than one subsystem.
    soc_source: str | None = None

    @property
    def is_complete(self) -> bool:
        """True once the essential value (SoC) has been seen.

        Devices do not include every field in every frame -- notably AC presence
        is often absent -- so we only require SoC before publishing. AC presence
        falls back to input voltage and then watts (see
        ``nut_writer.derive_status``).
        """
        return self.soc_percent is not None

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
