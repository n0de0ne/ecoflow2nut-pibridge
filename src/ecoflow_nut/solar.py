"""Solar production grouped by local calendar day.

The energy summary answers "what did this window cost". This answers a different
question -- did today beat yesterday, is this month down on the last one, which
day is the best the array has managed -- and none of those mean anything against
a sliding window. A rolling 24 hours cannot tell you today was poor; it can only
tell you the last 24 hours were.

So everything here is cut on *local* calendar boundaries, because that is the day
the person asking lives in. It reads the same watts-per-bucket series the costing
does, so the store keeps a single query shape.

Two rules run through it:

* A partial day is never averaged with whole ones. Today is a few hours old at
  breakfast, and folding it into a daily average drags the average down every
  morning and lets it climb back all afternoon -- a number that moves for no
  reason anyone can act on.
* Today is compared against yesterday *at the same time of day*, never against
  yesterday's total. "1.2 kWh today, 6.4 kWh yesterday" reads as a collapse at
  09:00 on the sunniest day of the year.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

# A day short of this many hours of samples is partial: today so far, or a day
# the bridge spent partly offline. Either way it is not comparable with a whole
# one, so it is charted but kept out of every average.
WHOLE_DAY_HOURS = 23.0


def _local(ts: Any) -> datetime | None:
    """Parse a stored bucket timestamp into the host's local zone."""
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    # Both stores hand back UTC-aware stamps; the guard is for a store that ever
    # stops doing so, where assuming local would silently shift every boundary.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone()


def local_midnight(day: date) -> datetime:
    """Local midnight starting ``day``, as an aware datetime."""
    return datetime.combine(day, time.min).astimezone()


def daily_totals(
    series: list[dict[str, Any]], bucket_seconds: int
) -> list[dict[str, Any]]:
    """One row per local calendar day, in kWh, oldest first.

    ``peak_w`` is the highest *bucket average* of the day, not an instantaneous
    peak -- hourly buckets flatten a midday spike -- which is why the UI labels
    it by the bucket width rather than calling it a peak.
    """
    hours = bucket_seconds / 3600.0
    days: dict[date, dict[str, Any]] = {}
    for item in series:
        when = _local(item.get("ts"))
        if when is None:
            continue
        row = days.setdefault(
            when.date(),
            {"solar_kwh": 0.0, "grid_kwh": 0.0, "load_kwh": 0.0,
             "peak_w": 0.0, "hours": 0.0, "reported": False},
        )
        raw_solar = item.get("solar_w")
        # avg() over an all-NULL column is NULL. A station that never reports PV
        # has to stay distinguishable from one reporting a real zero, or every
        # model without a solar sensor gets charted as a month of bad weather.
        row["reported"] = row["reported"] or raw_solar is not None
        solar_w = float(raw_solar or 0.0)
        row["solar_kwh"] += solar_w * hours / 1000.0
        row["grid_kwh"] += float(item.get("in_w") or 0.0) * hours / 1000.0
        row["load_kwh"] += float(item.get("out_w") or 0.0) * hours / 1000.0
        row["peak_w"] = max(row["peak_w"], solar_w)
        row["hours"] += hours

    out = []
    for day in sorted(days):
        row = days[day]
        supply = row["solar_kwh"] + row["grid_kwh"]
        out.append(
            {
                "date": day.isoformat(),
                "solar_kwh": round(row["solar_kwh"], 3) if row["reported"] else None,
                "grid_kwh": round(row["grid_kwh"], 3),
                "load_kwh": round(row["load_kwh"], 3),
                "peak_w": round(row["peak_w"]) if row["reported"] else None,
                "hours": round(row["hours"], 2),
                "whole": row["hours"] >= WHOLE_DAY_HOURS,
                "solar_share": (
                    round(row["solar_kwh"] / supply, 4)
                    if row["reported"] and supply > 0 else None
                ),
            }
        )
    return out


def _sum_between(
    series: list[dict[str, Any]], bucket_seconds: int, start: datetime, end: datetime
) -> float | None:
    """Solar kWh over ``[start, end)``, or None where nothing was recorded."""
    hours = bucket_seconds / 3600.0
    total = 0.0
    seen = False
    for item in series:
        when = _local(item.get("ts"))
        if when is None or not (start <= when < end):
            continue
        raw = item.get("solar_w")
        if raw is None:
            continue
        seen = True
        total += float(raw) * hours / 1000.0
    return round(total, 3) if seen else None


def compare_today(
    series: list[dict[str, Any]], bucket_seconds: int, now: datetime
) -> dict[str, Any]:
    """Today's harvest so far against yesterday's at the same point in the day.

    ``series`` must cover from yesterday's local midnight to now, at a bucket
    fine enough for "the same point" to mean something -- an hour of midday sun
    is a material slice of a day's total.
    """
    today = local_midnight(now.date())
    yesterday = local_midnight(now.date() - timedelta(days=1))
    # Measured from each midnight rather than by clock time, so the comparison
    # survives the hour a DST change adds or removes.
    elapsed = now - today
    return {
        "today_kwh": _sum_between(series, bucket_seconds, today, now),
        "yesterday_kwh": _sum_between(
            series, bucket_seconds, yesterday, yesterday + elapsed
        ),
        "yesterday_total_kwh": _sum_between(series, bucket_seconds, yesterday, today),
        "through_minutes": round(elapsed.total_seconds() / 60),
    }


def _month_total(days: list[dict[str, Any]], month: date) -> dict[str, Any] | None:
    """Solar kWh for the calendar month containing ``month``, from charted days."""
    rows = [
        d for d in days
        if d["solar_kwh"] is not None
        and date.fromisoformat(d["date"]).replace(day=1) == month.replace(day=1)
    ]
    if not rows:
        return None
    return {
        "month": month.replace(day=1).isoformat(),
        "solar_kwh": round(sum(d["solar_kwh"] for d in rows), 2),
        "days": len(rows),
    }


def _pad_calendar(days: list[dict[str, Any]], end: date) -> list[dict[str, Any]]:
    """Fill in every date from the first recorded day to ``end``.

    A day the bridge was switched off for produces no rows at all, so without
    this the strip closes the gap and a fortnight's outage reads as a fortnight
    of ordinary days. Padding stops at the first recorded day rather than at the
    start of the window: on a fresh install, 27 empty bars before the data says
    nothing except that the install is new.
    """
    if not days:
        return days
    by_date = {d["date"]: d for d in days}
    out, at = [], date.fromisoformat(days[0]["date"])
    while at <= end:
        key = at.isoformat()
        out.append(by_date.get(key) or {
            "date": key, "solar_kwh": None, "grid_kwh": None, "load_kwh": None,
            "peak_w": None, "hours": 0.0, "whole": False, "solar_share": None,
        })
        at += timedelta(days=1)
    return out


def summarise(
    daily_series: list[dict[str, Any]],
    daily_bucket: int,
    pace_series: list[dict[str, Any]],
    pace_bucket: int,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Everything the solar panel on the energy page needs, in one payload."""
    days = daily_totals(daily_series, daily_bucket)
    reported = any(d["solar_kwh"] is not None for d in days)
    whole = [d for d in days if d["whole"] and d["solar_kwh"] is not None]
    by_date = {d["date"]: d for d in days}

    harvest = sum(d["solar_kwh"] or 0.0 for d in days)
    grid = sum(d["grid_kwh"] for d in days)
    best = max(whole, key=lambda d: d["solar_kwh"], default=None)

    # The current month is nearly always partial, so it is reported beside the
    # previous one's total *and* a per-day average -- the average is the only
    # half of that pair you can compare across months of different lengths.
    this_month = _month_total(days, now.date())
    last_month = _month_total(days, now.date().replace(day=1) - timedelta(days=1))

    # Only after every total is taken: the padded rows are placeholders for the
    # chart's x-axis, and summing them as zeroes would report an outage as a run
    # of days that produced nothing.
    charted = _pad_calendar(days, now.date())

    return {
        "enabled": True,
        "reported": reported,
        "bucket_seconds": daily_bucket,
        "days": charted,
        "today": by_date.get(now.date().isoformat()),
        "yesterday": by_date.get((now.date() - timedelta(days=1)).isoformat()),
        "pace": compare_today(pace_series, pace_bucket, now),
        "best": best and {"date": best["date"], "solar_kwh": best["solar_kwh"]},
        "month": this_month,
        "prev_month": last_month,
        "window": {
            "days": len(charted),
            "recorded_days": len(days),
            "whole_days": len(whole),
            "solar_kwh": round(harvest, 2),
            "grid_kwh": round(grid, 2),
            "load_kwh": round(sum(d["load_kwh"] for d in days), 2),
            # Whole days only: today is a few hours old at breakfast, and letting
            # it into the average makes the average swing all day for no reason.
            "daily_avg_kwh": round(
                sum(d["solar_kwh"] for d in whole) / len(whole), 2
            ) if whole else None,
            "solar_share": round(harvest / (harvest + grid), 4)
            if reported and harvest + grid > 0 else None,
        },
    }
