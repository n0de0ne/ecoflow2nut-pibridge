"""NUT writer and status/runtime derivation tests."""

import pytest

from ecoflow_nut.config import NutConfig
from ecoflow_nut.nut_writer import (
    NutWriter,
    build_variables,
    derive_status,
    estimate_runtime_seconds,
    render,
)
from ecoflow_nut.state import DeviceState


@pytest.fixture
def nut() -> NutConfig:
    cfg = NutConfig(battery_capacity_wh=1024)
    cfg.static_values.serial = "P231ZE1APH560861"
    return cfg


def test_status_online_when_ac_present_and_charging(nut):
    state = DeviceState(soc_percent=80, ac_input_present=True, ac_input_watts=200)
    assert derive_status(state, nut) == "OL"


def test_status_on_battery_when_no_ac(nut):
    state = DeviceState(soc_percent=80, ac_input_present=False, ac_input_watts=0)
    assert derive_status(state, nut) == "OB"


def test_status_on_battery_low_below_threshold(nut):
    state = DeviceState(soc_percent=20, ac_input_present=False, ac_input_watts=0)
    assert derive_status(state, nut) == "OB LB"


def test_status_ac_present_but_negligible_draw_is_on_battery(nut):
    # AC plugged in but drawing < threshold (e.g. 5 W) is treated as on battery.
    state = DeviceState(soc_percent=80, ac_input_present=True, ac_input_watts=5)
    assert derive_status(state, nut) == "OB"


def test_status_infers_ac_from_watts_when_flag_absent(nut):
    # The DELTA 3 omits the AC-charger flag in many frames; fall back to watts.
    state = DeviceState(soc_percent=80, ac_input_present=None, ac_input_watts=300)
    assert derive_status(state, nut) == "OL"


def test_status_online_on_measured_mains_even_with_no_draw(nut):
    """The regression a watts-only rule causes on a DELTA 2 Max.

    A full battery with an idle load draws ~0 W from the mains. Judging on watts
    alone would call that an outage and start shutting NUT clients down, so a
    model that measures input voltage must be believed over the wattage.
    """
    state = DeviceState(soc_percent=100, ac_input_watts=0, ac_input_voltage=230.4)
    assert derive_status(state, nut) == "OL"


def test_status_on_battery_when_measured_mains_collapse(nut):
    state = DeviceState(soc_percent=80, ac_input_watts=0, ac_input_voltage=0.0)
    assert derive_status(state, nut) == "OB"


def test_status_ignores_stale_charger_flag_when_voltage_is_measured(nut):
    # Voltage is the ground truth; a lingering charger flag must not override it.
    state = DeviceState(
        soc_percent=80, ac_input_present=True, ac_input_watts=200, ac_input_voltage=0.0
    )
    assert derive_status(state, nut) == "OB"


def test_input_voltage_variable_reports_the_measurement_when_available(nut):
    live = build_variables(DeviceState(soc_percent=50, ac_input_voltage=228.6), nut)
    assert live["input.voltage"] == "228.6"
    # Models that do not measure it keep publishing the configured nominal.
    static = build_variables(DeviceState(soc_percent=50), nut)
    assert static["input.voltage"] == str(nut.static_values.input_voltage)


def test_state_complete_with_soc_only():
    # Publishing must not require the AC-charger flag, only SoC.
    assert DeviceState(soc_percent=80).is_complete is True
    assert DeviceState().is_complete is False


def test_runtime_idle_when_no_load(nut):
    state = DeviceState(soc_percent=50, ac_output_watts=0)
    assert estimate_runtime_seconds(state, nut) == 99999


def test_runtime_scales_with_load(nut):
    # 50% of 1024 Wh * 0.9 = 460.8 Wh; at 460.8 W that is exactly 1 hour.
    state = DeviceState(soc_percent=50, ac_output_watts=460.8)
    assert estimate_runtime_seconds(state, nut) == pytest.approx(3600, abs=2)


def test_build_variables_has_required_fields(nut):
    state = DeviceState(
        soc_percent=75,
        ac_input_present=True,
        ac_input_watts=100,
        ac_output_watts=250,
    )
    variables = build_variables(state, nut)
    assert variables["ups.status"] == "OL"
    assert variables["battery.charge"] == "75"
    # ups.load is a percent of nominal (250 W of 1800 W ≈ 14%); watts live in
    # ups.realpower.
    assert variables["ups.load"] == "14"
    assert variables["ups.realpower"] == "250"
    assert variables["ups.realpower.nominal"] == "1800"
    assert variables["device.serial"] == "P231ZE1APH560861"
    assert variables["battery.type"] == "LiFePO4"
    assert variables["battery.voltage.nominal"] == "51.2"


def test_render_is_parseable_dummy_ups_format(nut):
    state = DeviceState(soc_percent=60, ac_input_present=False, ac_output_watts=120)
    content = render(build_variables(state, nut))
    parsed = {}
    for line in content.splitlines():
        name, _, value = line.partition(": ")
        parsed[name] = value
    assert parsed["battery.charge"] == "60"
    assert parsed["ups.status"] == "OB"
    # Every line must be "name: value" with no empty names/values.
    for line in content.splitlines():
        assert ": " in line
        name, _, value = line.partition(": ")
        assert name and value


def test_writer_writes_atomically(tmp_path, nut):
    nut.dev_file_path = str(tmp_path / "sub" / "ecoflow.dev")
    writer = NutWriter(nut)
    state = DeviceState(soc_percent=42, ac_input_present=False, ac_output_watts=80)
    writer.write(state)
    dev = tmp_path / "sub" / "ecoflow.dev"
    content = dev.read_text()
    assert "battery.charge: 42" in content
    # The dummy-ups driver runs as the 'nut' user and must be able to read it.
    assert (dev.stat().st_mode & 0o444) == 0o444
    # No leftover temp files in the directory.
    leftovers = [p for p in (tmp_path / "sub").iterdir() if p.name != "ecoflow.dev"]
    assert leftovers == []
