"""Solar production cut on local calendar days.

Every figure here is a comparison someone will act on -- "today is worse than
yesterday", "this month is down on last" -- so the ways it can lie are the point
of the tests: a partial day averaged with whole ones, a day boundary taken in
UTC, an outage charted as a fortnight of no sun, a never-reported sensor charted
as a genuine zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ecoflow_nut import solar


def _bucket(when: datetime, solar_w=0.0, in_w=0.0, out_w=0.0) -> dict[str, object]:
    return {
        "ts": when.astimezone().isoformat(),
        "solar_w": solar_w,
        "in_w": in_w,
        "out_w": out_w,
        "bat_w": 0.0,
    }


def _hours(day: datetime, count: int, **kw) -> list[dict[str, object]]:
    return [_bucket(day + timedelta(hours=h), **kw) for h in range(count)]


# -- daily_totals ---------------------------------------------------------- #


def test_watts_per_bucket_integrate_to_kwh() -> None:
    """1000 W held for 24 hourly buckets is 24 kWh, not 24 000."""
    day = solar.local_midnight(datetime.now().astimezone().date())
    (row,) = solar.daily_totals(_hours(day, 24, solar_w=1000.0), 3600)
    assert row["solar_kwh"] == 24.0


def test_days_are_cut_at_local_midnight() -> None:
    """Cut in UTC, an evening's production lands on tomorrow west of Greenwich.

    Grouping on the stored UTC stamp would move a European summer evening's last
    two hours into the next day, so every day's total would be wrong by however
    much sun was left at 22:00 local.
    """
    today = datetime.now().astimezone().date()
    yesterday = solar.local_midnight(today - timedelta(days=1))
    rows = (
        _hours(yesterday + timedelta(hours=22), 2, solar_w=100.0)   # late yesterday
        + _hours(solar.local_midnight(today), 2, solar_w=100.0)     # early today
    )
    days = solar.daily_totals(rows, 3600)
    assert [d["date"] for d in days] == [
        (today - timedelta(days=1)).isoformat(), today.isoformat()
    ]
    assert [d["solar_kwh"] for d in days] == [0.2, 0.2]


def test_a_station_that_never_reports_pv_is_not_a_month_of_bad_weather() -> None:
    """avg() over an all-NULL column is NULL, and that has to survive the sum."""
    day = solar.local_midnight(datetime.now().astimezone().date())
    rows = [{**b, "solar_w": None} for b in _hours(day, 24, in_w=200.0)]
    (row,) = solar.daily_totals(rows, 3600)
    assert row["solar_kwh"] is None
    assert row["solar_share"] is None
    assert row["grid_kwh"] == 4.8, "the grid figure is still real"


def test_a_genuine_zero_is_reported_as_zero() -> None:
    day = solar.local_midnight(datetime.now().astimezone().date())
    (row,) = solar.daily_totals(_hours(day, 24, solar_w=0.0, in_w=200.0), 3600)
    assert row["solar_kwh"] == 0.0
    assert row["solar_share"] == 0.0


def test_a_short_day_is_marked_as_one() -> None:
    day = solar.local_midnight(datetime.now().astimezone().date())
    (row,) = solar.daily_totals(_hours(day, 6, solar_w=500.0), 3600)
    assert row["whole"] is False
    assert row["hours"] == 6.0


# -- compare_today --------------------------------------------------------- #


def test_today_is_compared_against_yesterday_at_the_same_hour() -> None:
    """Against yesterday's *total*, every morning reads as a collapse.

    Yesterday here produced steadily all day and today is matching it hour for
    hour; at 06:00 the honest answer is "level", not "80% down".
    """
    now = datetime.now().astimezone().replace(hour=6, minute=0, second=0, microsecond=0)
    today = solar.local_midnight(now.date())
    yesterday = solar.local_midnight(now.date() - timedelta(days=1))
    rows = (
        _hours(yesterday, 24, solar_w=100.0)   # a full day
        + _hours(today, 6, solar_w=100.0)      # the same rate, six hours in
    )
    out = solar.compare_today(rows, 3600, now)
    assert out["today_kwh"] == out["yesterday_kwh"] == 0.6
    assert out["yesterday_total_kwh"] == 2.4, "the full day is still reported"
    assert out["through_minutes"] == 360


def test_a_day_with_nothing_recorded_compares_as_unknown() -> None:
    """None, not zero: "yesterday produced nothing" is a different claim."""
    now = datetime.now().astimezone().replace(hour=9, minute=0, second=0, microsecond=0)
    rows = _hours(solar.local_midnight(now.date()), 9, solar_w=250.0)
    out = solar.compare_today(rows, 3600, now)
    assert out["today_kwh"] == 2.25
    assert out["yesterday_kwh"] is None


# -- summarise ------------------------------------------------------------- #


def _month(now: datetime, days_back: int, solar_w: float) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for back in range(days_back, 0, -1):
        rows += _hours(
            solar.local_midnight(now.date() - timedelta(days=back)),
            24, solar_w=solar_w, in_w=200.0,
        )
    return rows


def test_todays_part_day_stays_out_of_the_daily_average() -> None:
    """Otherwise the average sags every morning and recovers every afternoon.

    Three whole days at 2.4 kWh with two hours of today on the end must still
    average 2.4, not 1.8.
    """
    now = datetime.now().astimezone().replace(hour=2, minute=0, second=0, microsecond=0)
    rows = _month(now, 3, 100.0) + _hours(
        solar.local_midnight(now.date()), 2, solar_w=100.0, in_w=200.0
    )
    out = solar.summarise(rows, 3600, [], 900, now=now)
    assert out["window"]["whole_days"] == 3
    assert out["window"]["daily_avg_kwh"] == 2.4
    assert out["window"]["solar_kwh"] == 7.4, "the total still counts every hour"


def test_the_best_day_is_never_a_part_day() -> None:
    """A half-recorded day cannot be the best; it can only be short."""
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    rows = (
        _hours(solar.local_midnight(now.date() - timedelta(days=2)), 24, solar_w=50.0)
        + _hours(solar.local_midnight(now.date() - timedelta(days=1)), 24, solar_w=100.0)
        # Half a day at triple the rate: the biggest figure in the window, and
        # still not a day anyone can compare with the two above it.
        + _hours(solar.local_midnight(now.date()), 12, solar_w=300.0)
    )
    out = solar.summarise(rows, 3600, [], 900, now=now)
    assert out["today"]["solar_kwh"] == 3.6
    assert out["best"] == {
        "date": (now.date() - timedelta(days=1)).isoformat(), "solar_kwh": 2.4
    }


def test_an_outage_is_charted_as_a_gap_not_as_days_of_no_sun() -> None:
    """Days with no rows at all still take their place on the axis."""
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    rows = _hours(
        solar.local_midnight(now.date() - timedelta(days=5)), 24, solar_w=100.0
    ) + _hours(solar.local_midnight(now.date()), 12, solar_w=100.0)
    out = solar.summarise(rows, 3600, [], 900, now=now)

    dates = [d["date"] for d in out["days"]]
    assert len(dates) == 6, "the whole span is charted, gap included"
    assert dates == sorted(dates)
    missing = [d for d in out["days"] if d["hours"] == 0]
    assert len(missing) == 4
    assert all(d["solar_kwh"] is None for d in missing), "no rows is not zero kWh"
    # And the gap does not dilute anything measured.
    assert out["window"]["recorded_days"] == 2
    assert out["window"]["daily_avg_kwh"] == 2.4


def test_nothing_before_the_first_recorded_day_is_invented() -> None:
    """A fresh install should not open with three weeks of empty bars."""
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    rows = _hours(
        solar.local_midnight(now.date() - timedelta(days=1)), 24, solar_w=100.0
    )
    out = solar.summarise(rows, 3600, [], 900, now=now)
    assert [d["date"] for d in out["days"]] == [
        (now.date() - timedelta(days=1)).isoformat(), now.date().isoformat()
    ]


def test_a_station_without_pv_reports_nothing_to_show() -> None:
    """The card hides on this rather than charting a flat zero forever."""
    now = datetime.now().astimezone()
    rows = [{**b, "solar_w": None} for b in _month(now, 3, 0.0)]
    out = solar.summarise(rows, 3600, [], 900, now=now)
    assert out["reported"] is False
    assert out["window"]["solar_share"] is None


def test_an_empty_store_does_not_raise() -> None:
    out = solar.summarise([], 3600, [], 900, now=datetime.now().astimezone())
    assert out["days"] == []
    assert out["best"] is None
    assert out["month"] is None
    assert out["window"]["daily_avg_kwh"] is None
