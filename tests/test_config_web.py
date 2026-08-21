"""Config parsing for the web UI and Postgres sections."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoflow_nut.config import load_config

_BASE = """
ecoflow:
  mac: "DE:AD:BE:EF:00:01"
  serial: "P231TEST"
"""


def _write(tmp_path: Path, extra: str = "") -> str:
    path = tmp_path / "config.yaml"
    path.write_text(_BASE + extra)
    return str(path)


def test_defaults_disable_web_and_postgres(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path))
    assert config.web.enabled is False
    assert config.web.port == 8080
    assert config.postgres.enabled is False
    assert config.postgres.table == "ecoflow_samples"


def test_parses_web_and_postgres_sections(tmp_path: Path) -> None:
    extra = """
web:
  enabled: true
  port: 9000
  auth_token: "filetoken"
  require_auth_for_read: true
postgres:
  enabled: true
  dsn: "postgresql://u:p@db/eco"
  min_interval_seconds: 30
  retention_days: 7
"""
    config = load_config(_write(tmp_path, extra))
    assert config.web.enabled is True
    assert config.web.port == 9000
    assert config.web.auth_token == "filetoken"
    assert config.web.require_auth_for_read is True
    assert config.postgres.dsn == "postgresql://u:p@db/eco"
    assert config.postgres.min_interval_seconds == 30
    assert config.postgres.retention_days == 7


def test_parses_sqlite_section(tmp_path: Path) -> None:
    extra = """
sqlite:
  enabled: true
  path: "/data/eco.db"
  min_interval_seconds: 30
  retention_days: 90
"""
    config = load_config(_write(tmp_path, extra))
    assert config.sqlite.enabled is True
    assert config.sqlite.path == "/data/eco.db"
    assert config.sqlite.min_interval_seconds == 30
    assert config.sqlite.retention_days == 90
    # Postgres stays disabled / independent.
    assert config.postgres.enabled is False


def test_env_overrides_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = """
web:
  auth_token: "filetoken"
postgres:
  dsn: "file-dsn"
"""
    monkeypatch.setenv("ECOFLOW_WEB_TOKEN", "envtoken")
    monkeypatch.setenv("ECOFLOW_PG_DSN", "env-dsn")
    config = load_config(_write(tmp_path, extra))
    assert config.web.auth_token == "envtoken"
    assert config.postgres.dsn == "env-dsn"


# --------------------------------------------------------------------------- #
# Placeholder rejection
# --------------------------------------------------------------------------- #
def _config_with(tmp_path, mac: str, serial: str, user_id: str = "123") -> str:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'ecoflow:\n  mac: "{mac}"\n  serial: "{serial}"\n  user_id: "{user_id}"\n'
    )
    return str(cfg)


@pytest.mark.parametrize(
    "mac,serial",
    [
        ("AA:BB:CC:DD:EE:FF", "E201ZE1APH560861"),  # example MAC left in place
        ("DE:AD:BE:EF:00:01", "REPLACE-WITH-FULL-SERIAL"),
        ("DE:AD:BE:EF:00:01", "E201XXXXXXXXXXXX"),
    ],
)
def test_example_placeholders_are_rejected_with_a_useful_message(tmp_path, mac, serial):
    """A placeholder left in place otherwise fails far from its cause.

    An unedited MAC surfaces as "device not found during scan", which reads
    like a radio or range problem rather than an unfinished config.
    """
    with pytest.raises(ValueError, match="placeholder"):
        load_config(_config_with(tmp_path, mac, serial))


def test_a_real_looking_config_is_accepted(tmp_path):
    config = load_config(_config_with(tmp_path, "DC:06:75:A8:3E:29", "E201ZE1APH560861"))
    assert config.ecoflow.mac == "DC:06:75:A8:3E:29"
