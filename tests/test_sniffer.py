"""The capture's field watcher.

A lifetime odometer is the one field kind a single frame cannot identify:
``ac_chg_power = 4642`` reads the same whether it counts watt-hours drawn from
the wall or watt-hours pushed into the pack, and that distinction goes straight
into someone's Home Assistant energy accounting. Telling them apart means
watching the counter climb against a load you can measure, which is what this
records.
"""

from __future__ import annotations

from ecoflow_nut.sniffer import FieldWatch

PD = (0x02, 0x02, 0x01)
MPPT = (0x05, 0x02, 0x01)


def test_a_counter_reports_its_rise_per_hour() -> None:
    """A Wh counter's rise per hour is watts, which is the whole comparison."""
    watch = FieldWatch()
    watch.observe(PD, {"ac_chg_power": 4642}, 0.0)
    watch.observe(PD, {"ac_chg_power": 4645}, 60.0)
    (row,) = watch.counters()
    assert row["field"] == "ac_chg_power"
    assert row["rise"] == 3
    assert round(row["per_hour"]) == 180


def test_a_field_that_ever_fell_is_not_a_counter() -> None:
    """Live watts wander up and down; only an odometer never goes back."""
    watch = FieldWatch()
    for at, value in enumerate([100, 180, 140, 200]):
        watch.observe(PD, {"watts_in_sum": value}, float(at))
    assert watch.counters() == []


def test_a_counter_that_never_moved_is_not_reported_as_rising() -> None:
    watch = FieldWatch()
    watch.observe(PD, {"sun_chg_power": 0}, 0.0)
    watch.observe(PD, {"sun_chg_power": 0}, 600.0)
    assert watch.counters() == []


def test_a_field_pinned_at_zero_is_named() -> None:
    """How you catch a field this firmware does not populate: sun_chg_power
    reads 0 for a lifetime on the DELTA 2 Max while PV is actively charging."""
    watch = FieldWatch()
    for at in range(3):
        watch.observe(PD, {"sun_chg_power": 0, "dc_chg_power": 2398 + at}, float(at))
    assert (PD, "sun_chg_power") in watch.stuck_at_zero()
    assert (PD, "dc_chg_power") not in watch.stuck_at_zero()


def test_means_give_the_measured_side_of_the_comparison() -> None:
    watch = FieldWatch()
    for at, value in enumerate([180, 190, 200]):
        watch.observe(PD, {"watts_in_sum": value, "soc": 81}, float(at))
    (row,) = watch.means("watt")
    assert row["field"] == "watts_in_sum"
    assert row["mean"] == 190.0
    assert row["samples"] == 3


def test_the_same_field_name_in_two_frames_stays_separate() -> None:
    """Names repeat across layouts; merging them would average two subsystems."""
    watch = FieldWatch()
    watch.observe(PD, {"watts": 10}, 0.0)
    watch.observe(PD, {"watts": 20}, 10.0)
    watch.observe(MPPT, {"watts": 500}, 0.0)
    watch.observe(MPPT, {"watts": 900}, 10.0)
    assert {(r["key"], r["rise"]) for r in watch.counters()} == {(PD, 10), (MPPT, 400)}


def test_flags_and_strings_are_not_measurements() -> None:
    """`car_state` is a bool in Python's eyes an int, and would chart as one."""
    watch = FieldWatch()
    watch.observe(PD, {"car_state": False, "sys_ver": "1.2.3"}, 0.0)
    watch.observe(PD, {"car_state": True, "sys_ver": "1.2.3"}, 10.0)
    assert watch.counters() == []
    assert watch.means("car") == []


def test_a_single_frame_yields_no_rate() -> None:
    """One reading is a value, not a trend -- dividing by a zero span would
    report an infinite rate for a counter nobody has watched yet."""
    watch = FieldWatch()
    watch.observe(PD, {"ac_chg_power": 4642}, 0.0)
    assert watch.counters() == []


def test_the_biggest_riser_is_reported_first() -> None:
    watch = FieldWatch()
    watch.observe(PD, {"ac_chg_power": 0, "dc_dsg_power": 0}, 0.0)
    watch.observe(PD, {"ac_chg_power": 50, "dc_dsg_power": 2}, 60.0)
    assert [r["field"] for r in watch.counters()] == ["ac_chg_power", "dc_dsg_power"]
