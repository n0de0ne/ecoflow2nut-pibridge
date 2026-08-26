"""Tests for energy integration and HC/HP cost computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ecoflow_nut.config import PricingConfig
from ecoflow_nut.pricing import compute_energy, is_off_peak


def test_is_off_peak_wrapped_window() -> None:
    # HC 22:00 -> 06:00 (wraps midnight).
    start, end = 22 * 60, 6 * 60
    assert is_off_peak(23 * 60, start, end) is True  # 23:00 off-peak
    assert is_off_peak(2 * 60, start, end) is True  # 02:00 off-peak
    assert is_off_peak(12 * 60, start, end) is False  # noon peak
    assert is_off_peak(6 * 60, start, end) is False  # 06:00 boundary -> peak


def test_is_off_peak_normal_window() -> None:
    start, end = 1 * 60, 5 * 60  # 01:00 -> 05:00
    assert is_off_peak(3 * 60, start, end) is True
    assert is_off_peak(0, start, end) is False


def _series(watts: float, hours: float, bucket_seconds: int, start_hour_utc: int):
    """A constant-power series of given duration starting at a UTC hour."""
    n = int(hours * 3600 / bucket_seconds)
    base = datetime(2026, 1, 15, start_hour_utc, 0, tzinfo=UTC)
    out = []
    for i in range(n):
        ts = (base + timedelta(seconds=i * bucket_seconds)).isoformat()
        out.append({"ts": ts, "in_w": watts, "out_w": watts * 0.9})
    return out


def test_energy_integration_constant_load() -> None:
    # 100 W for 2 hours = 0.2 kWh of grid energy.
    bucket = 60
    series = _series(100, 2, bucket, start_hour_utc=12)
    pricing = PricingConfig(enabled=True, price_hc=0.10, price_hp=0.20)
    out = compute_energy(series, bucket, pricing)
    assert abs(out["grid_kwh"] - 0.2) < 1e-6
    assert abs(out["load_kwh"] - 0.18) < 1e-6
    assert out["avg_grid_watts"] == 100.0
    assert out["peak_grid_watts"] == 100.0


def test_cost_split_uses_local_tariff_window() -> None:
    # Run entirely inside the HC window in UTC; with UTC as local tz the whole
    # 0.2 kWh should land in HC and be priced at the HC rate.
    bucket = 60
    series = _series(100, 2, bucket, start_hour_utc=23)  # 23:00-01:00
    pricing = PricingConfig(
        enabled=True, hc_start="22:00", hc_end="06:00", price_hc=0.10, price_hp=0.20
    )
    out = compute_energy(series, bucket, pricing)
    # Depending on the test host's local tz the split shifts, but HC+HP must
    # always equal the total grid energy and total cost is internally consistent.
    assert abs(out["hc_kwh"] + out["hp_kwh"] - out["grid_kwh"]) < 1e-6
    assert abs(out["hc_cost"] + out["hp_cost"] - out["total_cost"]) < 1e-6


def test_empty_series() -> None:
    out = compute_energy([], 60, PricingConfig(enabled=True))
    assert out["grid_kwh"] == 0.0
    assert out["total_cost"] == 0.0
    assert out["avg_grid_watts"] == 0.0


def test_projection_scales_to_day_and_month() -> None:
    # 200 W for 1 hour, priced flat at 0.20 -> 0.04 € over 1h.
    bucket = 60
    series = _series(200, 1, bucket, start_hour_utc=12)
    pricing = PricingConfig(enabled=True, price_hc=0.20, price_hp=0.20)
    out = compute_energy(series, bucket, pricing)
    assert abs(out["total_cost"] - 0.04) < 1e-6
    # per-day ~= hourly cost * 24
    assert abs(out["cost_per_day"] - 0.04 * 24) < 1e-3


def test_both_backends_meter_cost_on_ac_input_only() -> None:
    """Solar must not reach the bill on either store.

    The SQLite path is exercised for real in test_db_sqlite; Postgres has no
    server here, so its query is pinned by text instead of going unchecked.
    ``input_watts`` is the device's total intake -- mains plus PV plus the car
    port -- and metering against it would charge for every free watt harvested.
    """
    import inspect

    from ecoflow_nut import db, db_sqlite

    sources = {
        "postgres": inspect.getsource(db.TelemetryStore.energy_series),
        "sqlite": inspect.getsource(db_sqlite.SqliteTelemetryStore._energy_series_sync),
    }
    for backend, source in sources.items():
        assert "avg(ac_input_watts)" in source, f"{backend} does not meter on AC input"
        assert "avg(input_watts)" not in source, (
            f"{backend} meters on total input, which bills solar as grid energy"
        )


def _hourly(hours: int, **watts: float) -> list[dict[str, float | str]]:
    """One bucket per hour at noon-ish, so every bucket lands in the HP window."""
    return [
        {"ts": f"2026-06-0{d + 1}T12:00:00+00:00", **watts} for d in range(hours)
    ]


def test_a_solar_run_costs_nothing_and_shows_what_it_saved() -> None:
    """The question behind the feature: what did the servers cost, and what did
    the sun cover?"""
    pricing = PricingConfig(enabled=True, price_hp=0.20, price_hc=0.10)
    # Two hours: 500 W of load, entirely covered by 500 W of PV, nothing bought.
    series = _hourly(2, in_w=0.0, out_w=500.0, solar_w=500.0)

    money = compute_energy(series, 3600, pricing)

    assert money["grid_kwh"] == 0.0, "nothing came off the wall"
    assert money["total_cost"] == 0.0, "so there is no bill"
    assert money["load_kwh"] == pytest.approx(1.0), "the kit still used 1 kWh"
    assert money["load_cost"] == pytest.approx(0.20), "which would have cost 20c"
    assert money["solar_kwh"] == pytest.approx(1.0)
    assert money["solar_savings"] == pytest.approx(0.20)
    assert money["net_saving"] == pytest.approx(0.20), "the whole load cost was avoided"


def test_running_purely_on_the_grid_saves_nothing() -> None:
    """The control case: no solar, no battery movement, so cost == load cost."""
    pricing = PricingConfig(enabled=True, price_hp=0.20, price_hc=0.10)
    series = _hourly(2, in_w=500.0, out_w=500.0, solar_w=0.0)

    money = compute_energy(series, 3600, pricing)

    assert money["total_cost"] == pytest.approx(0.20)
    assert money["load_cost"] == pytest.approx(0.20)
    assert money["net_saving"] == pytest.approx(0.0)
    assert money["solar_savings"] == 0.0


def test_charging_the_battery_from_the_grid_shows_a_negative_saving() -> None:
    """Buying more than you delivered is a real state, not a number to clamp.

    Over a window shorter than a charge cycle this is what stashing cheap
    off-peak energy looks like, and hiding it would make the figure a lie.
    """
    pricing = PricingConfig(enabled=True, price_hp=0.20, price_hc=0.10)
    series = _hourly(2, in_w=1000.0, out_w=200.0, solar_w=0.0)

    money = compute_energy(series, 3600, pricing)

    assert money["grid_kwh"] == pytest.approx(2.0)
    assert money["load_kwh"] == pytest.approx(0.4)
    assert money["net_saving"] < 0
    assert money["net_saving"] == pytest.approx(0.4 * 0.20 - 2.0 * 0.20)


def test_each_bucket_is_valued_at_its_own_tariff() -> None:
    """HC and HP differ, so a saving must be worth what it actually displaced."""
    pricing = PricingConfig(
        enabled=True, price_hp=0.20, price_hc=0.10, hc_start="22:00", hc_end="06:00"
    )
    series = [
        {"ts": "2026-06-01T02:00:00+00:00", "in_w": 0.0, "out_w": 0.0, "solar_w": 1000.0},
        {"ts": "2026-06-01T12:00:00+00:00", "in_w": 0.0, "out_w": 0.0, "solar_w": 1000.0},
    ]
    money = compute_energy(series, 3600, pricing)
    assert money["solar_kwh"] == pytest.approx(2.0)
    # One kWh displaced off-peak at 0.10, one on-peak at 0.20 -- not 2 x either.
    assert money["solar_savings"] == pytest.approx(0.30)


# ---------------------------------------------------------------------- #
# Solar / grid split
# ---------------------------------------------------------------------- #


def test_the_split_is_a_share_of_what_came_in() -> None:
    """Three parts grid to one part solar, whatever the load happened to do."""
    series = _hourly(4, in_w=750.0, out_w=1000.0, solar_w=250.0)

    money = compute_energy(series, 3600, PricingConfig(enabled=True))

    assert money["input_kwh"] == pytest.approx(4.0)
    assert money["solar_share"] == pytest.approx(0.25)
    assert money["grid_share"] == pytest.approx(0.75)


def test_the_two_shares_always_account_for_the_whole() -> None:
    """Anything else draws a stacked bar that does not fill its own track."""
    series = _hourly(3, in_w=317.0, out_w=400.0, solar_w=91.0)

    money = compute_energy(series, 3600, PricingConfig(enabled=True))

    assert money["solar_share"] + money["grid_share"] == pytest.approx(1.0, abs=1e-3)


def test_a_model_that_never_reports_solar_has_no_split() -> None:
    """Not 100% grid: a station with no PV sensor is not a station harvesting
    nothing, and only one of those two supports the claim.

    avg() over an all-NULL column comes back NULL, which is what tells them
    apart -- coercing that to 0.0 would quietly assert the stronger one.
    """
    series = _hourly(2, in_w=500.0, out_w=500.0, solar_w=None)

    money = compute_energy(series, 3600, PricingConfig(enabled=True))

    assert money["solar_reported"] is False
    assert money["solar_share"] is None
    assert money["grid_share"] is None


def test_a_reported_zero_harvest_is_a_real_hundred_percent_grid() -> None:
    """The other side of it: a PV-capable station that harvested nothing at
    night did genuinely run entirely off the wall."""
    series = _hourly(2, in_w=500.0, out_w=500.0, solar_w=0.0)

    money = compute_energy(series, 3600, PricingConfig(enabled=True))

    assert money["solar_reported"] is True
    assert money["solar_share"] == pytest.approx(0.0)
    assert money["grid_share"] == pytest.approx(1.0)


def test_a_window_with_no_input_at_all_has_no_split() -> None:
    """Running the load off the battery with nothing coming in: the ratio is
    0/0, and a bar has to show that as unknown rather than as either end."""
    series = _hourly(2, in_w=0.0, out_w=500.0, solar_w=0.0)

    money = compute_energy(series, 3600, PricingConfig(enabled=True))

    assert money["input_kwh"] == 0.0
    assert money["solar_share"] is None
    assert money["grid_share"] is None


def test_an_empty_series_has_no_split() -> None:
    money = compute_energy([], 3600, PricingConfig(enabled=True))
    assert money["solar_share"] is None and money["grid_share"] is None


# ---------------------------------------------------------------------- #
# Energy balance over a window
# ---------------------------------------------------------------------- #


def test_the_window_balances_the_same_way_the_live_figure_does() -> None:
    """Conservation, integrated: in - out - battery is what the box burned.

    600 W in and 400 W out for two hours with the pack taking 150 W leaves
    50 W of conversion, or 0.1 kWh over the window.
    """
    series = _hourly(2, in_w=600.0, out_w=400.0, solar_w=0.0, bat_w=150.0)

    money = compute_energy(series, 3600, PricingConfig(enabled=True))

    assert money["input_kwh"] == pytest.approx(1.2)
    assert money["load_kwh"] == pytest.approx(0.8)
    assert money["battery_kwh"] == pytest.approx(0.3)
    assert money["conversion_kwh"] == pytest.approx(0.1)


def test_a_discharging_window_balances_too() -> None:
    """The pack as a source: it covers the load and the losses together."""
    series = _hourly(2, in_w=0.0, out_w=200.0, solar_w=0.0, bat_w=-225.0)

    money = compute_energy(series, 3600, PricingConfig(enabled=True))

    assert money["battery_kwh"] == pytest.approx(-0.45)
    assert money["conversion_kwh"] == pytest.approx(0.05)


def test_a_window_with_no_pack_samples_refuses_to_balance() -> None:
    """The database gained battery_watts partway through its life, so older
    rows hold NULL. Treating that as zero would fold every watt-hour the
    battery moved into "conversion" -- over a day that is hundreds of Wh
    presented as loss, which is worse than saying nothing.
    """
    series = _hourly(2, in_w=600.0, out_w=400.0, solar_w=0.0, bat_w=None)

    money = compute_energy(series, 3600, PricingConfig(enabled=True))

    assert money["battery_reported"] is False
    assert money["battery_kwh"] is None
    assert money["conversion_kwh"] is None
    # The figures that never needed the pack are still there.
    assert money["grid_kwh"] == pytest.approx(1.2)


def test_a_pack_that_ended_where_it_started_shows_a_net_of_zero() -> None:
    """Net change, not throughput: a full charge and discharge nets out, and
    the losses of both still land in the residual."""
    series = [
        {"ts": "2026-06-01T12:00:00+00:00", "in_w": 500.0, "out_w": 100.0,
         "solar_w": 0.0, "bat_w": 350.0},
        {"ts": "2026-06-01T13:00:00+00:00", "in_w": 0.0, "out_w": 300.0,
         "solar_w": 0.0, "bat_w": -350.0},
    ]

    money = compute_energy(series, 3600, PricingConfig(enabled=True))

    assert money["battery_kwh"] == pytest.approx(0.0)
    # 0.5 kWh in, 0.4 kWh out, pack net zero -> 0.1 kWh burned.
    assert money["conversion_kwh"] == pytest.approx(0.1)
