"""Lifetime watt-hour odometers for the battery, integrated and persisted.

The station keeps its own counter for every *port* -- how much has come in from
the wall, how much has left the inverter -- but none for the pack. Nothing on
the wire says how many watt-hours have gone into or out of the battery, and
Home Assistant's Energy dashboard needs both: it computes what the house used as
``grid + solar + battery_out - battery_in``, so with the battery half missing
every watt-hour that went into storage is booked as though the house burned it.
On a station charging from PV that is not a rounding error -- it is the entire
difference between "the panels covered the load" and "the panels filled the
pack for tonight".

So the pair is integrated here rather than left to an HA helper, for two
reasons. This side sees every BLE frame where HA sees one publish interval. And
this side persists: a Riemann-sum helper's total is only ever as complete as
HA's own uptime, and starts again from zero the day someone recreates it.

The counters only ever climb, which is what ``state_class: total_increasing``
means. If the file is lost they restart at zero, and HA reads that as a meter
replacement rather than as a negative day.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Longer than this between two readings and the gap is a dropped BLE link, not
# a slow frame. Integrating across a reconnect would credit the pack with
# however long it was away at whatever it happened to be doing beforehand -- a
# four-minute outage during a 400 W charge invents 27 Wh from nothing.
MAX_GAP_SECONDS = 30.0
# The counters are worth at most a minute of drift; an SD card is not worth
# a write per frame.
SAVE_INTERVAL_SECONDS = 60.0


class EnergyMeter:
    """Two monotonic Wh totals, from the pack's signed watts."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_gap_seconds: float = MAX_GAP_SECONDS,
        save_interval_seconds: float = SAVE_INTERVAL_SECONDS,
    ) -> None:
        self._path = Path(path)
        self._max_gap = max_gap_seconds
        self._save_interval = save_interval_seconds
        self.charge_wh = 0.0
        self.discharge_wh = 0.0
        self._last_at: float | None = None
        self._last_w: float = 0.0
        self._saved_at: float | None = None
        self._dirty = False

    # -- persistence -------------------------------------------------------- #
    def load(self) -> None:
        """Restore the totals from disk (best-effort: absent or bad reads as 0)."""
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("energy_meter.load_failed", error=str(exc))
            return
        if not isinstance(data, dict):
            return
        # Read each independently and refuse anything that is not a real
        # non-negative number: a corrupt half-write should cost one counter, and
        # a negative would make a "total_increasing" sensor lie in both
        # directions at once.
        for name in ("charge_wh", "discharge_wh"):
            value = data.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if value >= 0:
                    setattr(self, name, float(value))
        log.info(
            "energy_meter.loaded",
            charge_wh=round(self.charge_wh),
            discharge_wh=round(self.discharge_wh),
        )

    def save(self, now: float | None = None, *, force: bool = False) -> None:
        """Persist the totals, at most once per save interval unless forced."""
        if not self._dirty and not force:
            return
        if not force and now is not None and self._saved_at is not None:
            if now - self._saved_at < self._save_interval:
                return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as handle:
                json.dump(
                    {"charge_wh": self.charge_wh, "discharge_wh": self.discharge_wh},
                    handle,
                )
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("energy_meter.save_failed", error=str(exc))
            return
        self._saved_at = now
        self._dirty = False

    # -- integration -------------------------------------------------------- #
    def add(self, watts: float, now: float) -> None:
        """Fold one signed reading in. Positive charges the pack, negative drains it.

        The interval between two readings is credited at their mean, so a ramp
        is integrated as a ramp rather than as a step. A span that changes sign
        averages towards zero and adds little to either total, which is the
        conservative way to be wrong about it.
        """
        last_at, last_w = self._last_at, self._last_w
        self._last_at, self._last_w = now, watts
        if last_at is None:
            return
        dt = now - last_at
        # A clock that went backwards (or a duplicate stamp) has nothing to add.
        if dt <= 0 or dt > self._max_gap:
            return
        wh = (last_w + watts) / 2 * dt / 3600.0
        if wh > 0:
            self.charge_wh += wh
        elif wh < 0:
            self.discharge_wh -= wh
        else:
            return
        self._dirty = True

    def totals(self) -> dict[str, int]:
        """The pair as whole watt-hours, for publishing.

        Rounded, because a counter that moves in the third decimal every two
        seconds is noise in every consumer's database, and rounding a rising
        float still rises.
        """
        return {
            "battery_charge_energy_wh": round(self.charge_wh),
            "battery_discharge_energy_wh": round(self.discharge_wh),
        }
