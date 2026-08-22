"""Translate power-station telemetry into NUT variables and a dummy-ups file.

The NUT ``dummy-ups`` driver in "repeating" mode reads a file of ``name: value``
lines and republishes them. We rewrite that file every poll cycle so any NUT
client (Unraid, Synology, upsc, ...) sees fresh values served by ``upsd``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .config import NutConfig
from .state import DeviceState

# Status flags
STATUS_ONLINE = "OL"
STATUS_ON_BATTERY = "OB"
STATUS_LOW_BATTERY = "LB"

# Runtime sentinel used when nothing is being drawn from the pack.
_RUNTIME_IDLE_SECONDS = 99999
_INVERTER_EFFICIENCY = 0.9


def derive_status(state: DeviceState, nut: NutConfig) -> str:
    """Derive ``ups.status`` from telemetry.

    * On line (``OL``) when the mains are present at the AC input.
    * On battery + low (``OB LB``) when SoC drops below the low threshold.
    * Otherwise on battery (``OB``).

    "Mains present" is read from whichever evidence the model gives us. Measured
    AC input voltage is preferred and used alone: it stays at nominal on a full
    battery drawing nothing, whereas input watts fall to zero there and would
    otherwise report a false outage. Models that do not report voltage (the
    DELTA 3) fall back to the charger flag confirmed by input watts.
    """
    ac_watts = state.ac_input_watts or 0.0
    drawing = ac_watts > nut.ac_input_present_min_watts
    volts = state.ac_input_voltage

    if volts is not None:
        ac_present = volts >= nut.ac_input_present_min_volts
    elif state.ac_input_present is not None:
        # The charger flag alone can be stale, so require live draw to confirm.
        ac_present = state.ac_input_present and drawing
    else:
        ac_present = drawing

    soc = state.soc_percent if state.soc_percent is not None else 100.0

    if ac_present:
        return STATUS_ONLINE
    if soc < nut.thresholds.low_battery_percent:
        return f"{STATUS_ON_BATTERY} {STATUS_LOW_BATTERY}"
    return STATUS_ON_BATTERY


def estimate_runtime_seconds(state: DeviceState, nut: NutConfig) -> int:
    """Estimate battery runtime in seconds from SoC and current AC output load."""
    soc = state.soc_percent if state.soc_percent is not None else 0.0
    remaining_wh = (soc / 100.0) * nut.battery_capacity_wh * _INVERTER_EFFICIENCY
    load = state.ac_output_watts or 0.0
    if load > 0:
        return int((remaining_wh / load) * 3600)
    return _RUNTIME_IDLE_SECONDS


def build_variables(state: DeviceState, nut: NutConfig) -> dict[str, str]:
    """Build the full ordered NUT variable mapping for the current state."""
    static = nut.static_values
    status = derive_status(state, nut)
    runtime = estimate_runtime_seconds(state, nut)
    load_watts = int(state.ac_output_watts or 0)
    # NUT defines ups.load as load percent of capacity, not watts. Derive it
    # from the AC output against the nominal real power; ups.realpower carries
    # the actual watts.
    load_percent = (
        int(round(load_watts / nut.realpower_nominal * 100))
        if nut.realpower_nominal
        else 0
    )

    variables: dict[str, str] = {
        "device.mfr": static.manufacturer,
        "device.model": static.model,
        "device.serial": static.serial,
        "device.type": "ups",
        "ups.mfr": static.manufacturer,
        "ups.model": static.model,
        "ups.serial": static.serial,
        "ups.status": status,
        "ups.load": str(load_percent),
        "ups.realpower": str(load_watts),
        "ups.realpower.nominal": str(nut.realpower_nominal),
        "battery.charge": str(int(state.soc_percent or 0)),
        "battery.charge.low": str(nut.thresholds.low_battery_percent),
        "battery.charge.warning": str(nut.battery_warning_percent),
        "battery.runtime": str(runtime),
        "battery.runtime.low": str(nut.battery_runtime_low_seconds),
        "battery.type": nut.battery_type,
        "battery.voltage.nominal": _fmt(nut.battery_voltage_nominal),
        # Report the measured mains voltage when the model provides it, so
        # clients see a real reading (and a real 0 V during an outage) rather
        # than a constant. Falls back to the configured nominal otherwise.
        "input.voltage": (
            _fmt(state.ac_input_voltage)
            if state.ac_input_voltage is not None
            else str(static.input_voltage)
        ),
        "input.frequency": str(static.input_frequency),
        "input.transfer.low": str(nut.input_transfer_low),
        "input.transfer.high": str(nut.input_transfer_high),
        "output.voltage": str(static.output_voltage),
        "output.frequency": str(static.output_frequency),
    }
    # Optional, and only when the pack actually reports it. battery.temperature
    # is a standard NUT variable, so adding it here is all any NUT client needs
    # -- upsc, Unraid and HA already understand it. Omitted rather than zeroed
    # on a model whose BMS is silent: 0 degrees is a reading, not an absence.
    if state.battery_temp_c is not None:
        variables["battery.temperature"] = _fmt(state.battery_temp_c)
    return variables


def _fmt(value: float) -> str:
    """Format a float without a trailing ``.0`` for whole numbers."""
    return str(int(value)) if float(value).is_integer() else str(value)


def render(variables: dict[str, str]) -> str:
    """Render variables to dummy-ups file content (``name: value`` lines)."""
    return "".join(f"{name}: {value}\n" for name, value in variables.items())


class NutWriter:
    """Writes the dummy-ups ``.dev`` file atomically each poll cycle."""

    def __init__(self, config: NutConfig) -> None:
        self._config = config
        self._path = Path(config.dev_file_path)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, state: DeviceState) -> dict[str, str]:
        """Render the current state and atomically replace the ``.dev`` file."""
        variables = build_variables(state, self._config)
        content = render(variables)
        self._atomic_write(content)
        return variables

    def _atomic_write(self, content: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, prefix=".ecoflow-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
            # mkstemp creates 0600; the dummy-ups driver runs as a different
            # user (nut) and must be able to read the state file.
            os.chmod(tmp, 0o644)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
