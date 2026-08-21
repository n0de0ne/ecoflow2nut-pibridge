"""Tests for the live-editable settings: validation, apply, persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoflow_nut.config import Config, EcoflowConfig
from ecoflow_nut.settings_store import (
    SettingsStore,
    apply_updates,
    current_values,
    schema,
)


def _config() -> Config:
    return Config(ecoflow=EcoflowConfig(mac="DE:AD:BE:EF:00:01", serial="P231"))


def test_schema_and_values_cover_same_keys() -> None:
    config = _config()
    keys = {f["key"] for f in schema()}
    assert keys == set(current_values(config))
    # A few expected keys are present.
    assert "auto_shutdown.trigger_soc_percent" in keys
    assert "pricing.price_hc" in keys
    assert "nut.low_battery_percent" in keys


def test_apply_updates_sets_nested_values() -> None:
    config = _config()
    changed = apply_updates(
        config,
        {
            "auto_shutdown.trigger_soc_percent": 12,
            "nut.low_battery_percent": 30,
            "pricing.price_hc": 0.18,
            "ecoflow.poll_interval_seconds": 10,
        },
    )
    assert set(changed) == {
        "auto_shutdown.trigger_soc_percent",
        "nut.low_battery_percent",
        "pricing.price_hc",
        "ecoflow.poll_interval_seconds",
    }
    assert config.auto_shutdown.trigger_soc_percent == 12
    assert config.nut.thresholds.low_battery_percent == 30
    assert config.pricing.price_hc == 0.18
    assert config.ecoflow.poll_interval_seconds == 10


def test_unchanged_value_not_reported() -> None:
    config = _config()
    config.auto_shutdown.trigger_soc_percent = 10
    assert apply_updates(config, {"auto_shutdown.trigger_soc_percent": 10}) == []


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValueError, match="unknown setting"):
        apply_updates(_config(), {"does.not.exist": 1})


def test_out_of_range_rejected_atomically() -> None:
    config = _config()
    with pytest.raises(ValueError, match="<= 100"):
        apply_updates(
            config,
            {
                "auto_shutdown.trigger_soc_percent": 5,  # valid
                "nut.low_battery_percent": 250,  # invalid -> whole batch rejected
            },
        )
    # Nothing applied because validation runs before any mutation.
    assert config.auto_shutdown.trigger_soc_percent != 5


def test_recover_below_trigger_rejected() -> None:
    with pytest.raises(ValueError, match="recover SoC"):
        apply_updates(
            _config(),
            {
                "auto_shutdown.trigger_soc_percent": 20,
                "auto_shutdown.recover_soc_percent": 10,
            },
        )


def test_min_load_watts_accepts_null() -> None:
    config = _config()
    apply_updates(config, {"auto_shutdown.min_load_watts": None})
    assert config.auto_shutdown.min_load_watts is None
    apply_updates(config, {"auto_shutdown.min_load_watts": 15})
    assert config.auto_shutdown.min_load_watts == 15


def test_bool_rejects_non_bool() -> None:
    with pytest.raises(ValueError, match="true/false"):
        apply_updates(_config(), {"auto_shutdown.enabled": 1})


def test_time_validation() -> None:
    config = _config()
    apply_updates(config, {"pricing.hc_start": "22:00"})
    assert config.pricing.hc_start == "22:00"
    with pytest.raises(ValueError, match="HH:MM"):
        apply_updates(config, {"pricing.hc_start": "25:00"})


def test_persistence_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(str(path))
    config = _config()
    apply_updates(config, {"pricing.price_hp": 0.25, "pricing.currency": "€"})
    store.save(config)
    assert path.exists()

    fresh = _config()
    SettingsStore(str(path)).load_into(fresh)
    assert fresh.pricing.price_hp == 0.25
    assert fresh.pricing.currency == "€"


def test_load_skips_bad_keys(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"bogus.key": 1, "pricing.price_hc": 0.2}')
    config = _config()
    SettingsStore(str(path)).load_into(config)  # must not raise
    assert config.pricing.price_hc == 0.2


def test_load_missing_file_is_noop(tmp_path: Path) -> None:
    SettingsStore(str(tmp_path / "nope.json")).load_into(_config())  # no raise
