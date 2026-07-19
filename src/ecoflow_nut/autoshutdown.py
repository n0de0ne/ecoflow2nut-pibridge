"""Auto-shutdown decision logic.

A small, side-effect-free state machine that decides when to cut the DELTA 3's
output. Kept separate from the daemon and the BLE layer so the policy can be
unit-tested deterministically.

Two independent triggers can arm a cut (whichever fires first wins); both only
act while on battery:

* **SoC trigger** -- SoC drops to ``trigger_soc_percent``; after
  ``grace_period_seconds`` (giving NUT clients time to shut down off the
  ``OB LB`` status) it cuts. Hysteresis: re-arms only after recovery, and a
  climb back to ``recover_soc_percent`` disarms it.
* **Low-load trigger** -- AC output stays at/below ``min_load_watts`` for
  ``load_grace_seconds`` (a "protected gear has powered off" signal), at any
  SoC. A momentary load above the threshold resets its debounce.

Recovery -- AC returns -- clears both arm timers. If a cut had happened and
``restore_on_recovery`` is set, ``RESTORE`` is emitted to turn output back on,
but only once SoC has climbed back to ``recover_soc_percent`` -- so protected
gear is not re-powered until the battery again holds enough charge to shut it
down cleanly if mains drops again. Until then the cut is held.
"""

from __future__ import annotations

from enum import Enum

from .config import AutoShutdownConfig


class ShutdownAction(Enum):
    NONE = "none"
    ARMED = "armed"
    DISARMED = "disarmed"
    CUT = "cut"
    RESTORE = "restore"


class AutoShutdownController:
    """Decides shutdown actions from successive telemetry observations."""

    def __init__(self, config: AutoShutdownConfig) -> None:
        self._config = config
        self._soc_armed_at: float | None = None
        self._load_armed_at: float | None = None
        self._triggered = False

    @property
    def armed(self) -> bool:
        return self._soc_armed_at is not None or self._load_armed_at is not None

    @property
    def triggered(self) -> bool:
        return self._triggered

    def seconds_until_cut(self, now: float) -> float | None:
        """Remaining grace seconds across all armed triggers, or None."""
        if self._triggered:
            return None
        remaining = []
        if self._soc_armed_at is not None:
            remaining.append(
                self._config.grace_period_seconds - (now - self._soc_armed_at)
            )
        if self._load_armed_at is not None:
            remaining.append(
                self._config.load_grace_seconds - (now - self._load_armed_at)
            )
        if not remaining:
            return None
        return max(0.0, min(remaining))

    def evaluate(
        self,
        now: float,
        soc: float | None,
        on_battery: bool,
        output_watts: float | None = None,
    ) -> ShutdownAction:
        """Advance the state machine for one observation and return any action."""
        if not self._config.enabled:
            return ShutdownAction.NONE

        # On line power: arms are battery-only; restore a prior cut only once
        # SoC has recovered (see _on_line_power).
        if not on_battery:
            return self._on_line_power(soc)

        if self._triggered:
            return ShutdownAction.NONE

        cfg = self._config
        armed_event = False
        disarm_event = False

        # --- SoC trigger ---
        if soc is not None and soc <= cfg.trigger_soc_percent:
            if self._soc_armed_at is None:
                self._soc_armed_at = now
                armed_event = True
        elif soc is not None and soc >= cfg.recover_soc_percent:
            # SoC climbed back out of the danger band (e.g. solar/charging while
            # still nominally "on battery").
            if self._soc_armed_at is not None:
                self._soc_armed_at = None
                disarm_event = True
        # else: hysteresis band -- hold the SoC arm state as-is.

        # --- Low-load trigger (any SoC) ---
        if cfg.min_load_watts is not None and output_watts is not None:
            if output_watts <= cfg.min_load_watts:
                if self._load_armed_at is None:
                    self._load_armed_at = now
                    armed_event = True
            elif self._load_armed_at is not None:
                self._load_armed_at = None
                disarm_event = True

        # --- Cut if any armed trigger's grace has elapsed ---
        if (
            self._soc_armed_at is not None
            and now - self._soc_armed_at >= cfg.grace_period_seconds
        ):
            self._triggered = True
            return ShutdownAction.CUT
        if (
            self._load_armed_at is not None
            and now - self._load_armed_at >= cfg.load_grace_seconds
        ):
            self._triggered = True
            return ShutdownAction.CUT

        if armed_event:
            return ShutdownAction.ARMED
        if disarm_event and not self.armed:
            return ShutdownAction.DISARMED
        return ShutdownAction.NONE

    def _on_line_power(self, soc: float | None) -> ShutdownAction:
        """Handle an observation taken while on line power (mains/charging).

        The arm timers are battery-only, so they always clear here. A latched
        cut is restored only once SoC has climbed back to ``recover_soc_percent``
        -- so protected gear is not powered up again until the battery holds
        enough charge to shut it down cleanly should mains drop again. Until
        then the cut is held (no action).
        """
        was_armed = self.armed
        self._soc_armed_at = None
        self._load_armed_at = None

        if self._triggered:
            if not self._config.restore_on_recovery:
                self._triggered = False
                return ShutdownAction.DISARMED
            if soc is None or soc >= self._config.recover_soc_percent:
                self._triggered = False
                return ShutdownAction.RESTORE
            # Mains is back but the battery has not recovered enough yet: keep
            # the cut in place and wait for it to charge.
            return ShutdownAction.NONE

        if was_armed:
            return ShutdownAction.DISARMED
        return ShutdownAction.NONE

    def force_triggered(self) -> None:
        """Mark a cut as already in effect.

        Used at daemon startup when we boot on line power but below the recover
        threshold -- almost certainly recovering from a deep discharge that
        power-cycled the host. Treating it as a latched cut makes the restore
        gate hold protected gear off until SoC climbs back to
        ``recover_soc_percent``.
        """
        self._soc_armed_at = None
        self._load_armed_at = None
        self._triggered = True
