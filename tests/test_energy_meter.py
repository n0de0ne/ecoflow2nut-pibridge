"""The battery's watt-hour odometers.

These feed Home Assistant's Energy dashboard as `total_increasing` sensors,
which trusts them to be monotonic and complete: a drop reads as a meter
replacement and a gap reads as energy that never existed. So the ways they could
break that trust -- a BLE reconnect integrated as hours of charging, a restart
that starts again from zero, a corrupt file -- are what is pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path

from ecoflow_nut.energy_meter import EnergyMeter


def _meter(tmp_path: Path, **kw) -> EnergyMeter:
    return EnergyMeter(tmp_path / "energy_meter.json", **kw)


def test_a_steady_charge_integrates_to_watt_hours(tmp_path: Path) -> None:
    """3600 W held for an hour is 3600 Wh."""
    meter = _meter(tmp_path, max_gap_seconds=3600)
    meter.add(3600.0, 0.0)
    meter.add(3600.0, 3600.0)
    assert meter.charge_wh == 3600.0
    assert meter.discharge_wh == 0.0


def test_discharge_lands_in_its_own_counter_as_a_positive(tmp_path: Path) -> None:
    """Both sensors only ever climb; the direction is which one moves."""
    meter = _meter(tmp_path, max_gap_seconds=3600)
    meter.add(-1800.0, 0.0)
    meter.add(-1800.0, 3600.0)
    assert meter.charge_wh == 0.0
    assert meter.discharge_wh == 1800.0


def test_a_ramp_is_integrated_as_a_ramp(tmp_path: Path) -> None:
    """The interval is credited at the mean of its ends, not at either one.

    0 W rising to 200 W over an hour is 100 Wh. Taking the last reading would
    book 200, the first would book 0, and on a station whose charge rate tracks
    the sun that error runs one way all morning.
    """
    meter = _meter(tmp_path, max_gap_seconds=3600)
    meter.add(0.0, 0.0)
    meter.add(200.0, 3600.0)
    assert meter.charge_wh == 100.0


def test_the_first_reading_alone_adds_nothing(tmp_path: Path) -> None:
    """There is no interval yet -- an assumed one would be invented energy."""
    meter = _meter(tmp_path)
    meter.add(500.0, 10.0)
    assert meter.charge_wh == 0.0
    assert meter.discharge_wh == 0.0


def test_a_dropped_link_is_not_integrated_across(tmp_path: Path) -> None:
    """The gap between two frames is a reconnect, not four minutes of charging.

    Crediting it at whatever the pack was doing before the link went would have
    invented 27 Wh here, and HA has no way to tell that from a real 27 Wh.
    """
    meter = _meter(tmp_path, max_gap_seconds=30)
    meter.add(400.0, 0.0)
    meter.add(400.0, 240.0)        # four minutes away
    assert meter.charge_wh == 0.0
    # And the meter picks straight back up on the next real interval.
    meter.add(400.0, 250.0)
    assert meter.charge_wh > 0


def test_a_clock_that_goes_backwards_adds_nothing(tmp_path: Path) -> None:
    meter = _meter(tmp_path)
    meter.add(400.0, 100.0)
    meter.add(400.0, 90.0)
    assert meter.charge_wh == 0.0


def test_totals_survive_a_restart(tmp_path: Path) -> None:
    """The whole reason this is not an HA helper: a restart must cost nothing."""
    meter = _meter(tmp_path, max_gap_seconds=3600)
    meter.add(3600.0, 0.0)
    meter.add(3600.0, 1800.0)
    meter.add(-3600.0, 1800.0)
    meter.add(-3600.0, 3600.0)
    meter.save(force=True)

    revived = _meter(tmp_path)
    revived.load()
    assert revived.charge_wh == meter.charge_wh == 1800.0
    assert revived.discharge_wh == meter.discharge_wh == 1800.0


def test_a_missing_file_starts_from_zero(tmp_path: Path) -> None:
    meter = _meter(tmp_path)
    meter.load()
    assert (meter.charge_wh, meter.discharge_wh) == (0.0, 0.0)


def test_a_corrupt_file_does_not_stop_the_daemon(tmp_path: Path) -> None:
    """Energy accounting is a nice-to-have; refusing to boot over it is not."""
    (tmp_path / "energy_meter.json").write_text("{not json")
    meter = _meter(tmp_path)
    meter.load()
    assert (meter.charge_wh, meter.discharge_wh) == (0.0, 0.0)


def test_a_half_written_file_keeps_the_half_that_reads(tmp_path: Path) -> None:
    (tmp_path / "energy_meter.json").write_text(
        json.dumps({"charge_wh": 1234.5, "discharge_wh": "nonsense"})
    )
    meter = _meter(tmp_path)
    meter.load()
    assert meter.charge_wh == 1234.5
    assert meter.discharge_wh == 0.0


def test_a_negative_total_is_refused(tmp_path: Path) -> None:
    """A "total_increasing" sensor cannot start below zero and climb into it."""
    (tmp_path / "energy_meter.json").write_text(
        json.dumps({"charge_wh": -50.0, "discharge_wh": 10.0})
    )
    meter = _meter(tmp_path)
    meter.load()
    assert meter.charge_wh == 0.0
    assert meter.discharge_wh == 10.0


def test_saving_is_throttled_but_a_forced_save_always_lands(tmp_path: Path) -> None:
    """A write per BLE frame would be a few hundred thousand a day on an SD card."""
    path = tmp_path / "energy_meter.json"
    meter = _meter(tmp_path, max_gap_seconds=3600, save_interval_seconds=60)
    meter.add(3600.0, 0.0)
    meter.add(3600.0, 3600.0)

    meter.save(now=0.0)                       # first save: nothing to throttle yet
    first = json.loads(path.read_text())["charge_wh"]
    meter.add(3600.0, 7200.0)
    meter.save(now=30.0)                      # inside the interval
    assert json.loads(path.read_text())["charge_wh"] == first
    meter.save(now=30.0, force=True)          # shutdown
    assert json.loads(path.read_text())["charge_wh"] > first


def test_totals_are_published_as_whole_watt_hours(tmp_path: Path) -> None:
    """A counter that moves in the third decimal every two seconds is noise in
    every consumer's database -- and rounding a rising float still rises."""
    meter = _meter(tmp_path, max_gap_seconds=3600)
    meter.add(100.0, 0.0)
    meter.add(100.0, 3600.0)
    assert meter.totals() == {
        "battery_charge_energy_wh": 100,
        "battery_discharge_energy_wh": 0,
    }


def test_the_totals_never_go_down(tmp_path: Path) -> None:
    """Whatever the pack does, both counters are monotonic -- which is the whole
    contract of `state_class: total_increasing`."""
    meter = _meter(tmp_path, max_gap_seconds=3600)
    seen = [(0.0, 0.0)]
    for i, watts in enumerate([200, -150, 0, 900, -900, 40, -40, 0, 300]):
        meter.add(float(watts), i * 600.0)
        seen.append((meter.charge_wh, meter.discharge_wh))
    for (c0, d0), (c1, d1) in zip(seen, seen[1:], strict=False):
        assert c1 >= c0 and d1 >= d0
