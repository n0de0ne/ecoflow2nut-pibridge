"""Energy integration and Heures Creuses / Heures Pleines cost estimation.

Given a uniform time series of average watts per bucket (from the telemetry
store), integrate to energy (kWh) and split it across the off-peak (HC) and peak
(HP) tariff windows by the *local* time-of-day of each bucket. Cost is metered
against AC **input** (grid draw), per the user's configuration.

Three money figures come out, and they answer different questions:

* ``total_cost`` -- what was actually bought at the wall. The bill.
* ``load_cost`` -- what the load consumed, valued at grid rates. "What my
  servers cost to run", independent of where the energy came from.
* ``net_saving`` -- ``load_cost - total_cost``. What not buying it all from
  the grid was worth, capturing both solar and off-peak battery arbitrage.

``solar_savings`` values the harvest alone at the tariff it displaced. It is
an estimate, and slightly generous: PV arrives as DC into the battery, so
some of it is lost to round-trip efficiency before it reaches the load, and
energy stored in one tariff window may be spent in another. Over a window
shorter than a full charge/discharge cycle, every figure here is distorted
by the battery's own state moving -- they converge over days, not minutes.

The HC window is a single span that may wrap midnight (e.g. 22:00 -> 06:00);
every other minute of the day is HP.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import PricingConfig


def _parse_hhmm(value: str) -> int:
    """Minutes-since-midnight for an ``HH:MM`` string (0 on bad input)."""
    try:
        hh, mm = value.split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return 0


def is_off_peak(minute_of_day: int, hc_start: int, hc_end: int) -> bool:
    """True if ``minute_of_day`` falls in the HC window (handles midnight wrap)."""
    if hc_start == hc_end:
        return False  # empty / full-day ambiguous -> treat as all peak
    if hc_start < hc_end:
        return hc_start <= minute_of_day < hc_end
    # Wrapped window, e.g. 22:00 -> 06:00.
    return minute_of_day >= hc_start or minute_of_day < hc_end


def compute_energy(
    series: list[dict[str, Any]],
    bucket_seconds: int,
    pricing: PricingConfig,
) -> dict[str, Any]:
    """Integrate a watts-per-bucket series into energy + HC/HP cost.

    ``series`` items are ``{"ts": iso, "in_w": float|None, "out_w": float|None}``
    in chronological order. Energy per bucket = ``avg_w * bucket_seconds`` (Wh-s),
    converted to kWh. Buckets are classified HC/HP by their local-time hour.
    """
    hc_start = _parse_hhmm(pricing.hc_start)
    hc_end = _parse_hhmm(pricing.hc_end)
    wh_per_bucket = bucket_seconds / 3600.0  # multiply by watts -> Wh

    in_kwh = out_kwh = solar_kwh = hc_kwh = hp_kwh = 0.0
    sum_in = peak_in = 0.0
    # Valued at each bucket's own tariff, since both vary through the day.
    load_cost = solar_savings = 0.0
    n = 0
    for item in series:
        in_w = float(item.get("in_w") or 0.0)
        out_w = float(item.get("out_w") or 0.0)
        solar_w = float(item.get("solar_w") or 0.0)
        e_in = in_w * wh_per_bucket / 1000.0
        e_out = out_w * wh_per_bucket / 1000.0
        e_solar = solar_w * wh_per_bucket / 1000.0
        in_kwh += e_in
        out_kwh += e_out
        solar_kwh += e_solar
        sum_in += in_w
        peak_in = max(peak_in, in_w)
        n += 1
        minute = _local_minute_of_day(item.get("ts"))
        off_peak = minute is not None and is_off_peak(minute, hc_start, hc_end)
        rate = pricing.price_hc if off_peak else pricing.price_hp
        if off_peak:
            hc_kwh += e_in
        else:
            hp_kwh += e_in
        load_cost += e_out * rate
        solar_savings += e_solar * rate

    hc_cost = hc_kwh * pricing.price_hc
    hp_cost = hp_kwh * pricing.price_hp
    total_cost = hc_cost + hp_cost
    span_hours = (n * bucket_seconds) / 3600.0 if n else 0.0
    per_hour_cost = total_cost / span_hours if span_hours > 0 else 0.0

    return {
        "currency": pricing.currency,
        "pricing_enabled": pricing.enabled,
        "span_hours": round(span_hours, 2),
        "grid_kwh": round(in_kwh, 3),
        "load_kwh": round(out_kwh, 3),
        "solar_kwh": round(solar_kwh, 3),
        # What the load itself would cost buying every kWh from the grid --
        # "what my servers cost to run" -- as opposed to total_cost, which is
        # what was actually bought at the wall.
        "load_cost": round(load_cost, 4),
        # Harvested PV valued at the tariff it displaced.
        "solar_savings": round(solar_savings, 4),
        # The bottom line: running the load on grid alone, minus what was
        # actually bought. Captures solar *and* off-peak battery arbitrage,
        # which valuing the harvest alone does not.
        "net_saving": round(load_cost - total_cost, 4),
        "hc_kwh": round(hc_kwh, 3),
        "hp_kwh": round(hp_kwh, 3),
        "hc_cost": round(hc_cost, 4),
        "hp_cost": round(hp_cost, 4),
        "total_cost": round(total_cost, 4),
        "avg_grid_watts": round(sum_in / n, 1) if n else 0.0,
        "peak_grid_watts": round(peak_in, 1),
        "cost_per_day": round(per_hour_cost * 24, 3),
        "cost_per_month": round(per_hour_cost * 24 * 30, 2),
        "hc_window": f"{pricing.hc_start}-{pricing.hc_end}",
    }


def _local_minute_of_day(ts: Any) -> int | None:
    """Local-time minute-of-day for an ISO timestamp string (UTC-aware)."""
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    # Stored timestamps are UTC-aware; convert to the host's local zone so the
    # HC/HP classification matches the wall clock the tariff is defined in.
    local = dt.astimezone()
    return local.hour * 60 + local.minute
