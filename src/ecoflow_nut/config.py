"""YAML configuration loading with typed dataclasses and sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONTROL_SOCKET = "/var/run/nut/ecoflow-nut.sock"

# Environment overrides for secrets, so they need not live in the YAML file.
ENV_WEB_TOKEN = "ECOFLOW_WEB_TOKEN"
ENV_PG_DSN = "ECOFLOW_PG_DSN"


@dataclass(slots=True)
class EcoflowConfig:
    mac: str
    serial: str
    model: str = "delta3"
    poll_interval_seconds: int = 5
    # encrypt_type and user_id are only needed for the encrypted (type-7/ECDH)
    # handshake. encrypt_type "auto" reads it from the BLE advertisement.
    encrypt_type: str | int = "auto"
    user_id: str = ""


@dataclass(slots=True)
class BleConfig:
    adapter: str = "hci0"
    connect_timeout_seconds: int = 30
    reconnect_backoff_max_seconds: int = 60
    scan_timeout_seconds: int = 30


@dataclass(slots=True)
class NutThresholds:
    low_battery_percent: int = 25
    critical_battery_percent: int = 10


@dataclass(slots=True)
class NutStaticValues:
    input_voltage: int = 230
    input_frequency: int = 50
    output_voltage: int = 230
    output_frequency: int = 50
    manufacturer: str = "EcoFlow"
    model: str = "DELTA 3"
    serial: str = ""


@dataclass(slots=True)
class NutConfig:
    device_name: str = "ecoflow"
    dev_file_path: str = "/var/run/nut/ecoflow.dev"
    battery_capacity_wh: int = 1024
    realpower_nominal: int = 1800
    battery_warning_percent: int = 35
    battery_runtime_low_seconds: int = 300
    battery_type: str = "LiFePO4"
    battery_voltage_nominal: float = 51.2
    input_transfer_low: int = 200
    input_transfer_high: int = 250
    ac_input_present_min_watts: int = 10
    thresholds: NutThresholds = field(default_factory=NutThresholds)
    static_values: NutStaticValues = field(default_factory=NutStaticValues)


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"


@dataclass(slots=True)
class AutoShutdownConfig:
    """Policy for automatically cutting the DELTA 3's output on critical battery.

    Disabled by default -- cutting output is destructive. Only acts while on
    battery; the grace period gives NUT clients time to shut down first.
    """

    enabled: bool = False
    trigger_soc_percent: int = 10
    recover_soc_percent: int = 15
    grace_period_seconds: int = 300
    # Low-load trigger: when on battery and AC output stays at/below
    # min_load_watts for load_grace_seconds, cut (a "protected gear has powered
    # off" signal). None disables this trigger.
    min_load_watts: float | None = None
    load_grace_seconds: int = 60
    cut_ac: bool = True
    cut_usb: bool = False  # never default true: a Pi may be powered from USB
    cut_dc: bool = False
    # Also cut a downstream HomeKit-over-BLE outlet (see EveOutletConfig). Lets a
    # single load (e.g. an Unraid server) be shed independently of the DELTA 3's
    # all-or-nothing AC bank, keeping other AC sockets (router/fibre) powered.
    cut_eve: bool = False
    restore_on_recovery: bool = False


@dataclass(slots=True)
class WebConfig:
    """Embedded control/telemetry web UI served from inside the daemon.

    Disabled by default. When enabled, an async HTTP server runs in the daemon's
    event loop, so it shares the single BLE connection: the dashboard reads live
    state and control actions go out over the existing link. Control actions
    (toggling AC/USB/DC, changing auto-shutdown) require ``auth_token``; the
    read-only dashboard is open unless ``require_auth_for_read`` is set.
    """

    enabled: bool = False
    host: str = "0.0.0.0"  # noqa: S104 - intentionally LAN-reachable
    port: int = 8080
    # Shared secret for control actions. Prefer the ECOFLOW_WEB_TOKEN env var.
    auth_token: str = ""
    # Also require the token to view the dashboard / read telemetry.
    require_auth_for_read: bool = False


@dataclass(slots=True)
class EveOutletConfig:
    """Drive a HomeKit-over-BLE smart outlet (e.g. an Eve Energy, BLE/non-Thread).

    Lets the bridge cut a *single* downstream load (e.g. an Unraid server)
    independently of the DELTA 3's all-or-nothing AC bank, so the other AC
    sockets (a router / fibre ONT) stay powered. Disabled by default.

    The bridge becomes the accessory's sole HomeKit controller: reset the outlet
    and remove it from Apple Home first, then pair it once with
    ``ecoflow-nut eve pair``. Pairing data is persisted to ``pairing_file`` and
    survives restarts; ``setup_code`` is only needed during pairing.

    Strongly prefer a SEPARATE Bluetooth adapter from the DELTA 3 link (default
    ``hci1``): the EcoFlow session is persistent and latency-sensitive, so
    sharing one radio can stall telemetry. Connections are made on demand
    (connect -> write -> disconnect) to minimise contention if you must share.
    """

    enabled: bool = False
    # HomeKit accessory id (shown by 'eve discover'; saved at pairing time).
    device_id: str = ""
    # Bluetooth adapter for the HomeKit link -- ideally a second dongle.
    adapter: str = "hci1"
    # Where aiohomekit pairing data is persisted (JSON, {device_id: data}).
    pairing_file: str = "/var/lib/ecoflow-nut/eve-pairing.json"
    # 8-digit HomeKit setup code, e.g. "123-45-678". Only needed to pair; may be
    # left empty once the outlet is paired.
    setup_code: str = ""
    connect_timeout_seconds: int = 30


@dataclass(slots=True)
class SwitchBotConfig:
    """Manual control of a SwitchBot Bot (mechanical button pusher) over BLE.

    A convenience to physically press the server's power button from the CLI or
    web dashboard -- e.g. to boot a machine that doesn't auto-power-on. Plain BLE
    GATT, no pairing. Disabled by default; it is *not* wired into auto-shutdown
    (a power button is a toggle, so an automated press could shut down a server
    that has already auto-booted). Password-protected Bots are not supported.
    """

    enabled: bool = False
    # BLE MAC of the Bot (find it with 'ecoflow-nut switchbot scan').
    mac: str = ""
    # Bluetooth adapter (shares the DELTA 3 radio by default; on-demand connect).
    adapter: str = "hci0"
    connect_timeout_seconds: int = 20


@dataclass(slots=True)
class PostgresConfig:
    """Optional Postgres telemetry logging.

    Disabled by default. When enabled with a ``dsn`` (or the ECOFLOW_PG_DSN env
    var), the daemon records one sample row per poll and the web UI's history
    charts read from it. The bridge runs fine with no database.
    """

    enabled: bool = False
    dsn: str = ""  # e.g. postgresql://user:pass@host:5432/ecoflow
    table: str = "ecoflow_samples"
    # Minimum seconds between persisted samples (decouples DB write rate from the
    # BLE poll interval). 0 logs every complete frame.
    min_interval_seconds: int = 0
    retention_days: int = 0  # 0 disables automatic pruning of old rows


@dataclass(slots=True)
class SqliteConfig:
    """Optional local SQLite telemetry logging.

    Disabled by default. A 100%-local, zero-extra-dependency store (Python stdlib
    ``sqlite3``): the daemon records one sample row per poll into a single file on
    the bridge host and the web UI's history charts read from it. Ideal when the
    bridge should be self-contained with no separate database server. The bridge
    runs fine if the file can't be written. If both ``postgres`` and ``sqlite``
    are enabled, Postgres takes precedence.
    """

    enabled: bool = False
    path: str = "/var/lib/ecoflow-nut/telemetry.db"
    table: str = "ecoflow_samples"
    min_interval_seconds: int = 0
    retention_days: int = 0  # 0 disables automatic pruning of old rows


@dataclass(slots=True)
class PricingConfig:
    """Time-of-use electricity pricing for the web UI's cost estimate.

    French "Heures Creuses / Heures Pleines" (off-peak / peak) tariff. Cost is
    metered against AC *input* (grid draw). The HC window is a single span that
    may wrap midnight (default 22:00 -> 06:00); all other hours are HP. Prices
    are per kWh in ``currency``. These are editable live from the web UI.
    """

    enabled: bool = False
    currency: str = "€"
    hc_start: str = "22:00"
    hc_end: str = "06:00"
    price_hc: float = 0.0
    price_hp: float = 0.0


@dataclass(slots=True)
class Config:
    ecoflow: EcoflowConfig
    ble: BleConfig = field(default_factory=BleConfig)
    nut: NutConfig = field(default_factory=NutConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    auto_shutdown: AutoShutdownConfig = field(default_factory=AutoShutdownConfig)
    eve: EveOutletConfig = field(default_factory=EveOutletConfig)
    switchbot: SwitchBotConfig = field(default_factory=SwitchBotConfig)
    web: WebConfig = field(default_factory=WebConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    sqlite: SqliteConfig = field(default_factory=SqliteConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    # Local control socket: the running daemon listens here so the CLI can send
    # output commands over the daemon's existing BLE connection (the device only
    # allows one connection at a time).
    control_socket_path: str = DEFAULT_CONTROL_SOCKET
    # Where the web UI persists live-edited settings (overlaid on the YAML at
    # startup). Lives in the systemd StateDirectory so it survives restarts.
    settings_file: str = "/var/lib/ecoflow-nut/settings.json"


def _filter(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Keep only keys that are fields of ``cls`` to tolerate extra YAML keys."""
    names = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return {k: v for k, v in data.items() if k in names}


def load_config(path: str | Path) -> Config:
    """Load and validate configuration from a YAML file."""
    raw = yaml.safe_load(Path(path).read_text()) or {}

    eco_raw = raw.get("ecoflow")
    if not eco_raw or not eco_raw.get("mac"):
        raise ValueError("config: 'ecoflow.mac' is required")
    ecoflow = EcoflowConfig(**_filter(EcoflowConfig, eco_raw))

    ble = BleConfig(**_filter(BleConfig, raw.get("ble", {})))

    nut_raw = dict(raw.get("nut", {}))
    thresholds = NutThresholds(**_filter(NutThresholds, nut_raw.pop("thresholds", {})))
    static_raw = _filter(NutStaticValues, nut_raw.pop("static_values", {}))
    static = NutStaticValues(**static_raw)
    if not static.serial:
        static.serial = ecoflow.serial
    nut = NutConfig(
        **_filter(NutConfig, nut_raw), thresholds=thresholds, static_values=static
    )

    logging_cfg = LoggingConfig(**_filter(LoggingConfig, raw.get("logging", {})))
    auto_shutdown = AutoShutdownConfig(
        **_filter(AutoShutdownConfig, raw.get("auto_shutdown", {}))
    )
    eve = EveOutletConfig(**_filter(EveOutletConfig, raw.get("eve", {})))
    switchbot = SwitchBotConfig(**_filter(SwitchBotConfig, raw.get("switchbot", {})))

    web = WebConfig(**_filter(WebConfig, raw.get("web", {})))
    # Secrets may be supplied via the environment instead of the YAML file.
    web.auth_token = os.environ.get(ENV_WEB_TOKEN, web.auth_token)

    postgres = PostgresConfig(**_filter(PostgresConfig, raw.get("postgres", {})))
    postgres.dsn = os.environ.get(ENV_PG_DSN, postgres.dsn)

    sqlite = SqliteConfig(**_filter(SqliteConfig, raw.get("sqlite", {})))
    pricing = PricingConfig(**_filter(PricingConfig, raw.get("pricing", {})))

    return Config(
        ecoflow=ecoflow,
        ble=ble,
        nut=nut,
        logging=logging_cfg,
        auto_shutdown=auto_shutdown,
        eve=eve,
        switchbot=switchbot,
        web=web,
        postgres=postgres,
        sqlite=sqlite,
        pricing=pricing,
        control_socket_path=raw.get("control_socket_path", DEFAULT_CONTROL_SOCKET),
        settings_file=raw.get("settings_file", "/var/lib/ecoflow-nut/settings.json"),
    )
