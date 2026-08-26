"""Arithmetic that DeviceState does on its own readings."""

from __future__ import annotations

import pytest

from ecoflow_nut.state import DeviceState

# ---------------------------------------------------------------------- #
# Power balance
# ---------------------------------------------------------------------- #


def test_the_residual_is_what_the_station_burns_converting() -> None:
    """The numbers are the ones off the dashboard that prompted this.

    251 W in, 188 W out, a pack gaining 49 W. The 14 W left over is the
    inverter, the charger and the unit's own electronics -- and solar minus
    that residual (64 - 14 = 50 W) lands within a watt of what the pack was
    actually taking, which is what says the model is right rather than merely
    self-consistent.
    """
    state = DeviceState(
        soc_percent=81.5,
        ac_input_watts=187.0,
        solar_input_watts=64.0,
        ac_output_watts=187.0,
        dc_output_watts=0.0,
        usb_output_watts=1.0,
        battery_watts=49.0,
    )
    balance = state.power_balance()
    assert balance is not None
    assert balance["supply_watts"] == pytest.approx(251.0)
    assert balance["draw_watts"] == pytest.approx(188.0)
    assert balance["conversion_watts"] == pytest.approx(14.0)


def test_the_same_arithmetic_holds_on_battery() -> None:
    """Discharging, the pack is a source rather than a sink, and the residual
    is what the box costs on top of the load it is carrying."""
    state = DeviceState(
        soc_percent=60.0,
        ac_input_watts=0.0,
        solar_input_watts=0.0,
        ac_output_watts=200.0,
        battery_watts=-225.0,
    )
    balance = state.power_balance()
    assert balance is not None
    assert balance["conversion_watts"] == pytest.approx(25.0)


@pytest.mark.parametrize(
    "missing", ["battery_watts", "ac_input_watts", "ac_output_watts"]
)
def test_a_missing_term_yields_no_balance_at_all(missing: str) -> None:
    """The residual is a difference of larger numbers, so treating an unknown
    as zero does not make it approximate -- it makes it wrong by exactly that
    unknown, with nothing on screen to say so."""
    fields: dict[str, float | None] = {
        "soc_percent": 50.0,
        "ac_input_watts": 100.0,
        "ac_output_watts": 80.0,
        "battery_watts": 15.0,
    }
    fields[missing] = None
    assert DeviceState(**fields).power_balance() is None


def test_ports_the_model_does_not_have_count_as_zero() -> None:
    """A station with no solar and no 12V port has not hidden watts there."""
    state = DeviceState(
        soc_percent=50.0, ac_input_watts=100.0, ac_output_watts=80.0,
        battery_watts=15.0,
    )
    balance = state.power_balance()
    assert balance is not None
    assert balance["conversion_watts"] == pytest.approx(5.0)


def test_sensors_disagreeing_reads_as_a_negative_residual() -> None:
    """Deliberately not clamped. A station cannot make power, so a negative
    here means two sensors disagree; the caller has to be able to see that
    rather than be handed a plausible-looking zero."""
    state = DeviceState(
        soc_percent=50.0, ac_input_watts=100.0, ac_output_watts=105.0,
        battery_watts=0.0,
    )
    balance = state.power_balance()
    assert balance is not None
    assert balance["conversion_watts"] == pytest.approx(-5.0)
