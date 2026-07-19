"""Auto-shutdown state-machine tests."""

import pytest

from ecoflow_nut.autoshutdown import AutoShutdownController, ShutdownAction
from ecoflow_nut.config import AutoShutdownConfig


def _cfg(**kw) -> AutoShutdownConfig:
    base = dict(
        enabled=True,
        trigger_soc_percent=10,
        recover_soc_percent=15,
        grace_period_seconds=300,
    )
    base.update(kw)
    return AutoShutdownConfig(**base)


def test_disabled_never_acts():
    c = AutoShutdownController(AutoShutdownConfig(enabled=False))
    assert c.evaluate(0, 5, on_battery=True) is ShutdownAction.NONE
    assert c.evaluate(1000, 1, on_battery=True) is ShutdownAction.NONE


def test_no_action_above_trigger():
    c = AutoShutdownController(_cfg())
    assert c.evaluate(0, 50, on_battery=True) is ShutdownAction.NONE
    assert c.armed is False


def test_does_not_arm_on_ac():
    c = AutoShutdownController(_cfg())
    # Even at low SoC, do nothing while on line power.
    assert c.evaluate(0, 5, on_battery=False) is ShutdownAction.NONE
    assert c.armed is False


def test_arms_then_cuts_after_grace():
    c = AutoShutdownController(_cfg(grace_period_seconds=300))
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.ARMED
    assert c.armed is True
    # Before the grace period elapses: no cut.
    assert c.evaluate(100, 8, on_battery=True) is ShutdownAction.NONE
    assert c.seconds_until_cut(100) == pytest.approx(200)
    # After the grace period: cut once.
    assert c.evaluate(300, 7, on_battery=True) is ShutdownAction.CUT
    assert c.triggered is True
    # Subsequent observations do not re-cut.
    assert c.evaluate(400, 6, on_battery=True) is ShutdownAction.NONE


def test_hysteresis_band_keeps_counting():
    c = AutoShutdownController(_cfg(grace_period_seconds=300))
    assert c.evaluate(0, 10, on_battery=True) is ShutdownAction.ARMED
    # SoC bounces to 12 (between trigger 10 and recover 15): stays armed.
    assert c.evaluate(150, 12, on_battery=True) is ShutdownAction.NONE
    assert c.armed is True
    assert c.evaluate(300, 12, on_battery=True) is ShutdownAction.CUT


def test_recovery_disarms_before_cut():
    c = AutoShutdownController(_cfg())
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.ARMED
    # AC returns -> disarm.
    assert c.evaluate(50, 9, on_battery=False) is ShutdownAction.DISARMED
    assert c.armed is False
    # Can arm again later.
    assert c.evaluate(100, 9, on_battery=True) is ShutdownAction.ARMED


def test_recovery_by_soc_climb():
    c = AutoShutdownController(_cfg(recover_soc_percent=15))
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.ARMED
    assert c.evaluate(60, 16, on_battery=True) is ShutdownAction.DISARMED


def test_restore_when_soc_already_recovered():
    c = AutoShutdownController(_cfg(grace_period_seconds=0, restore_on_recovery=True))
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.CUT
    # Mains returns and SoC is already above recover -> restore immediately.
    assert c.evaluate(10, 60, on_battery=False) is ShutdownAction.RESTORE


def test_restore_waits_for_recover_soc():
    c = AutoShutdownController(
        _cfg(grace_period_seconds=0, recover_soc_percent=15, restore_on_recovery=True)
    )
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.CUT
    # Mains back but SoC still below recover -> hold the cut, do not restore.
    assert c.evaluate(10, 9, on_battery=False) is ShutdownAction.NONE
    assert c.triggered is True
    assert c.evaluate(20, 14, on_battery=False) is ShutdownAction.NONE
    # SoC reaches the recover threshold while charging -> restore now.
    assert c.evaluate(30, 15, on_battery=False) is ShutdownAction.RESTORE
    assert c.triggered is False


def test_force_triggered_holds_until_recover():
    c = AutoShutdownController(
        _cfg(grace_period_seconds=0, recover_soc_percent=10, restore_on_recovery=True)
    )
    # Simulate a reboot mid-discharge: pretend a cut is already in effect.
    c.force_triggered()
    assert c.triggered is True
    # On line power but below recover -> stay cut.
    assert c.evaluate(0, 4, on_battery=False) is ShutdownAction.NONE
    # Charged back to recover -> restore.
    assert c.evaluate(10, 10, on_battery=False) is ShutdownAction.RESTORE


def test_no_restore_when_disabled():
    c = AutoShutdownController(_cfg(grace_period_seconds=0, restore_on_recovery=False))
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.CUT
    assert c.evaluate(10, 9, on_battery=False) is ShutdownAction.DISARMED


# --- low-load trigger -------------------------------------------------------- #
def test_low_load_cuts_after_debounce_at_any_soc():
    c = AutoShutdownController(_cfg(min_load_watts=15, load_grace_seconds=60))
    # Healthy SoC, but on battery with a low load -> arm the load trigger.
    assert c.evaluate(0, 90, on_battery=True, output_watts=5) is ShutdownAction.ARMED
    assert c.evaluate(30, 90, on_battery=True, output_watts=5) is ShutdownAction.NONE
    assert c.evaluate(60, 90, on_battery=True, output_watts=5) is ShutdownAction.CUT


def test_low_load_debounce_resets_on_load_return():
    c = AutoShutdownController(_cfg(min_load_watts=15, load_grace_seconds=60))
    assert c.evaluate(0, 90, on_battery=True, output_watts=5) is ShutdownAction.ARMED
    # Load comes back above threshold before the debounce elapses -> disarm.
    assert (
        c.evaluate(30, 90, on_battery=True, output_watts=200) is ShutdownAction.DISARMED
    )
    assert c.armed is False
    # Still below grace from a fresh arm afterwards.
    assert c.evaluate(40, 90, on_battery=True, output_watts=5) is ShutdownAction.ARMED
    assert c.evaluate(99, 90, on_battery=True, output_watts=5) is ShutdownAction.NONE
    assert c.evaluate(100, 90, on_battery=True, output_watts=5) is ShutdownAction.CUT


def test_low_load_inactive_when_unconfigured():
    c = AutoShutdownController(_cfg())  # min_load_watts None
    assert c.evaluate(0, 90, on_battery=True, output_watts=0) is ShutdownAction.NONE
    assert c.evaluate(1000, 90, on_battery=True, output_watts=0) is ShutdownAction.NONE


def test_low_load_only_on_battery():
    c = AutoShutdownController(_cfg(min_load_watts=15, load_grace_seconds=0))
    # On line power: low load must not cut.
    assert c.evaluate(0, 90, on_battery=False, output_watts=0) is ShutdownAction.NONE
    assert c.armed is False


def test_soc_trigger_still_wins_when_load_high():
    c = AutoShutdownController(_cfg(min_load_watts=15, grace_period_seconds=0))
    # Heavy load but critically low SoC -> SoC trigger cuts.
    assert c.evaluate(0, 8, on_battery=True, output_watts=500) is ShutdownAction.CUT
