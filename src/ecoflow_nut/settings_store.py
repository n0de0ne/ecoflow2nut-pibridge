"""Live-editable settings: schema, validation, and JSON persistence.

The web UI lets the user edit a curated set of "runtime-safe" config values
(auto-shutdown policy, NUT thresholds, poll interval, capacity, and electricity
pricing). Edits are applied to the in-memory :class:`~ecoflow_nut.config.Config`
objects (so they take effect without a restart) and written to a small JSON file
that is overlaid back onto the YAML config at the next startup.

Each editable value is described by a :class:`Field` with a dotted ``key`` (e.g.
``auto_shutdown.trigger_soc_percent``) and an attribute ``path`` from ``Config``.
Keys are dotted to avoid collisions (both ``auto_shutdown`` and ``pricing`` have
an ``enabled``). The schema is sent to the browser so the form renders itself.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config

log = structlog.get_logger(__name__)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True, slots=True)
class Field:
    """One editable setting: where it lives, its type and validation bounds."""

    key: str  # dotted, unique, e.g. "auto_shutdown.trigger_soc_percent"
    path: tuple[str, ...]  # attribute path from Config to the value
    type: str  # bool | int | float | float_or_null | str | time
    label: str
    group: str
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""

    @property
    def step(self) -> float | None:
        if self.type == "float" or self.type == "float_or_null":
            return 0.01
        if self.type == "int":
            return 1
        return None


# The curated, runtime-safe editable surface. Order is the UI render order.
FIELDS: tuple[Field, ...] = (
    # --- Auto-shutdown policy ---
    Field(
        "auto_shutdown.enabled",
        ("auto_shutdown", "enabled"),
        "bool",
        "Enabled",
        "Auto-shutdown",
    ),
    Field(
        "auto_shutdown.trigger_soc_percent",
        ("auto_shutdown", "trigger_soc_percent"),
        "int",
        "Trigger SoC %",
        "Auto-shutdown",
        0,
        100,
        "Arm + cut at/below this charge (on battery).",
    ),
    Field(
        "auto_shutdown.recover_soc_percent",
        ("auto_shutdown", "recover_soc_percent"),
        "int",
        "Recover SoC %",
        "Auto-shutdown",
        0,
        100,
        "Disarm once charge climbs back to this.",
    ),
    Field(
        "auto_shutdown.grace_period_seconds",
        ("auto_shutdown", "grace_period_seconds"),
        "int",
        "Grace period (s)",
        "Auto-shutdown",
        0,
        86400,
        "Wait after arming (SoC trigger) before cutting.",
    ),
    Field(
        "auto_shutdown.min_load_watts",
        ("auto_shutdown", "min_load_watts"),
        "float_or_null",
        "Min load (W)",
        "Auto-shutdown",
        0,
        5000,
        "Low-load trigger: cut when AC out stays at/below this. Blank = off.",
    ),
    Field(
        "auto_shutdown.load_grace_seconds",
        ("auto_shutdown", "load_grace_seconds"),
        "int",
        "Load grace (s)",
        "Auto-shutdown",
        0,
        86400,
    ),
    Field(
        "auto_shutdown.cut_ac",
        ("auto_shutdown", "cut_ac"),
        "bool",
        "Cut AC",
        "Auto-shutdown",
    ),
    Field(
        "auto_shutdown.cut_usb",
        ("auto_shutdown", "cut_usb"),
        "bool",
        "Cut USB",
        "Auto-shutdown",
        help="Keep OFF if the bridge host is USB-powered.",
    ),
    Field(
        "auto_shutdown.cut_dc",
        ("auto_shutdown", "cut_dc"),
        "bool",
        "Cut 12V DC",
        "Auto-shutdown",
    ),
    Field(
        "auto_shutdown.restore_on_recovery",
        ("auto_shutdown", "restore_on_recovery"),
        "bool",
        "Restore on recovery",
        "Auto-shutdown",
    ),
    # --- NUT thresholds ---
    Field(
        "nut.low_battery_percent",
        ("nut", "thresholds", "low_battery_percent"),
        "int",
        "Low battery %",
        "NUT thresholds",
        0,
        100,
        "Below this -> OB LB; clients begin their shutdown.",
    ),
    Field(
        "nut.battery_warning_percent",
        ("nut", "battery_warning_percent"),
        "int",
        "Warning battery %",
        "NUT thresholds",
        0,
        100,
    ),
    Field(
        "nut.battery_runtime_low_seconds",
        ("nut", "battery_runtime_low_seconds"),
        "int",
        "Runtime low (s)",
        "NUT thresholds",
        0,
        86400,
    ),
    Field(
        "nut.ac_input_present_min_watts",
        ("nut", "ac_input_present_min_watts"),
        "int",
        "AC-present min (W)",
        "NUT thresholds",
        0,
        5000,
        "AC input above this counts as 'on line' (OL).",
    ),
    Field(
        "nut.input_transfer_low",
        ("nut", "input_transfer_low"),
        "int",
        "Transfer low (V)",
        "NUT thresholds",
        0,
        500,
    ),
    Field(
        "nut.input_transfer_high",
        ("nut", "input_transfer_high"),
        "int",
        "Transfer high (V)",
        "NUT thresholds",
        0,
        500,
    ),
    # --- Device / capacity ---
    Field(
        "ecoflow.poll_interval_seconds",
        ("ecoflow", "poll_interval_seconds"),
        "int",
        "BLE link check (s)",
        "Device",
        1,
        3600,
        "How often the bridge checks the BLE link is alive -- a watchdog, not a "
        "sample rate. The DELTA 3 pushes telemetry on its own schedule and cannot "
        "be asked to send faster; see History below for chart resolution.",
    ),
    Field(
        "nut.battery_capacity_wh",
        ("nut", "battery_capacity_wh"),
        "int",
        "Battery capacity (Wh)",
        "Device",
        1,
        100000,
    ),
    Field(
        "nut.realpower_nominal",
        ("nut", "realpower_nominal"),
        "int",
        "Nominal power (W)",
        "Device",
        1,
        100000,
    ),
    # --- Telemetry history ---
    # Both backends are listed because the schema is static and cannot know which
    # store is enabled; only one is ever active (Postgres wins if both are on).
    # These are read per write / per prune, so edits take effect immediately.
    Field(
        "sqlite.min_interval_seconds",
        ("sqlite", "min_interval_seconds"),
        "int",
        "SQLite sample interval (s)",
        "History",
        0,
        3600,
        "Minimum gap between stored samples. Lower = finer charts but more "
        "SD-card writes; 0 stores every frame. The device's own push rate is the "
        "ceiling either way.",
    ),
    Field(
        "sqlite.retention_days",
        ("sqlite", "retention_days"),
        "int",
        "SQLite retention (days)",
        "History",
        0,
        3650,
        "Samples older than this are deleted at the next prune (every 6h). "
        "Lowering it discards history. 0 keeps everything.",
    ),
    Field(
        "postgres.min_interval_seconds",
        ("postgres", "min_interval_seconds"),
        "int",
        "Postgres sample interval (s)",
        "History",
        0,
        3600,
        "As above, for the Postgres backend.",
    ),
    Field(
        "postgres.retention_days",
        ("postgres", "retention_days"),
        "int",
        "Postgres retention (days)",
        "History",
        0,
        3650,
        "As above, for the Postgres backend.",
    ),
    # --- Electricity pricing ---
    Field("pricing.enabled", ("pricing", "enabled"), "bool", "Show cost", "Pricing"),
    Field("pricing.currency", ("pricing", "currency"), "str", "Currency", "Pricing"),
    Field(
        "pricing.hc_start",
        ("pricing", "hc_start"),
        "time",
        "Heures Creuses start",
        "Pricing",
        help="Off-peak window start (HH:MM).",
    ),
    Field(
        "pricing.hc_end",
        ("pricing", "hc_end"),
        "time",
        "Heures Creuses end",
        "Pricing",
        help="Off-peak window end (HH:MM).",
    ),
    Field(
        "pricing.price_hc",
        ("pricing", "price_hc"),
        "float",
        "HC price /kWh",
        "Pricing",
        0,
        100,
    ),
    Field(
        "pricing.price_hp",
        ("pricing", "price_hp"),
        "float",
        "HP price /kWh",
        "Pricing",
        0,
        100,
    ),
)

_FIELDS_BY_KEY = {f.key: f for f in FIELDS}


def schema() -> list[dict[str, Any]]:
    """The field schema sent to the browser to render the settings form."""
    return [
        {
            "key": f.key,
            "label": f.label,
            "group": f.group,
            "type": f.type,
            "min": f.minimum,
            "max": f.maximum,
            "step": f.step,
            "help": f.help,
        }
        for f in FIELDS
    ]


def _resolve(config: Config, path: tuple[str, ...]) -> tuple[Any, str]:
    """Return the (parent_object, attribute_name) for a dotted path."""
    obj: Any = config
    for attr in path[:-1]:
        obj = getattr(obj, attr)
    return obj, path[-1]


def current_values(config: Config) -> dict[str, Any]:
    """Flat dict of every editable value's current state."""
    values: dict[str, Any] = {}
    for f in FIELDS:
        parent, attr = _resolve(config, f.path)
        values[f.key] = getattr(parent, attr)
    return values


def _coerce(field: Field, value: Any) -> Any:
    """Validate and coerce one incoming value, or raise ValueError."""
    t = field.type
    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{field.key}: expected true/false")
        return value
    if t == "str":
        if not isinstance(value, str):
            raise ValueError(f"{field.key}: expected text")
        return value
    if t == "time":
        if not isinstance(value, str) or not _TIME_RE.match(value):
            raise ValueError(f"{field.key}: expected HH:MM (00:00-23:59)")
        return value
    if t == "float_or_null":
        if value is None or value == "":
            return None
        return _coerce_number(field, value, integer=False)
    if t == "int":
        return _coerce_number(field, value, integer=True)
    if t == "float":
        return _coerce_number(field, value, integer=False)
    raise ValueError(f"{field.key}: unknown field type {t}")


def _coerce_number(field: Field, value: Any, *, integer: bool) -> Any:
    if isinstance(value, bool):  # bool is a subclass of int; reject it here
        raise ValueError(f"{field.key}: expected a number")
    try:
        num: float = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field.key}: expected a number") from None
    if field.minimum is not None and num < field.minimum:
        raise ValueError(f"{field.key}: must be >= {field.minimum}")
    if field.maximum is not None and num > field.maximum:
        raise ValueError(f"{field.key}: must be <= {field.maximum}")
    return int(num) if integer else num


def apply_updates(config: Config, updates: dict[str, Any]) -> list[str]:
    """Validate and apply a dict of ``key -> value``. Returns the changed keys.

    All values are validated *before* any are applied, so a bad value rejects the
    whole batch (no partial application). Unknown keys raise ValueError.
    """
    coerced: dict[str, Any] = {}
    for key, raw in updates.items():
        field = _FIELDS_BY_KEY.get(key)
        if field is None:
            raise ValueError(f"unknown setting: {key}")
        coerced[key] = _coerce(field, raw)

    # Cross-field sanity: recover should be >= trigger to make hysteresis sane.
    trig = coerced.get("auto_shutdown.trigger_soc_percent")
    rec = coerced.get("auto_shutdown.recover_soc_percent")
    if trig is not None and rec is not None and rec < trig:
        raise ValueError("recover SoC % must be >= trigger SoC %")

    changed: list[str] = []
    for key, value in coerced.items():
        field = _FIELDS_BY_KEY[key]
        parent, attr = _resolve(config, field.path)
        if getattr(parent, attr) != value:
            setattr(parent, attr, value)
            changed.append(key)
    return changed


class SettingsStore:
    """Loads/saves the live-edited settings overlay as JSON."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def load_into(self, config: Config) -> None:
        """Overlay any persisted settings onto ``config`` (best-effort)."""
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("settings.load_failed", error=str(exc))
            return
        if not isinstance(data, dict):
            return
        # Apply per-key so one bad/stale key can't drop the whole file.
        for key, value in data.items():
            try:
                apply_updates(config, {key: value})
            except ValueError as exc:
                log.warning("settings.skip_key", key=key, error=str(exc))
        log.info("settings.loaded", path=str(self._path), count=len(data))

    def save(self, config: Config) -> None:
        """Atomically persist the full current editable state to JSON."""
        values = current_values(config)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as handle:
                json.dump(values, handle, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("settings.save_failed", error=str(exc))
