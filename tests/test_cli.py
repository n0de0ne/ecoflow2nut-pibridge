"""CLI tests that do not require BLE hardware."""

import pytest
from click.testing import CliRunner

from ecoflow_nut.cli import cli
from ecoflow_nut.devices import DRIVERS, OUTPUT_KINDS
from ecoflow_nut.main import parse_control_command


def _write_config(tmp_path) -> str:
    dev = tmp_path / "nut" / "ecoflow.dev"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "ecoflow:\n"
        '  mac: "AA:BB:CC:DD:EE:FF"\n'
        '  serial: "P231XXXXXXXXXXXX"\n'
        "nut:\n"
        f'  dev_file_path: "{dev}"\n'
        "logging:\n"
        '  format: "console"\n'
    )
    return str(cfg)


def test_seed_writes_state_file(tmp_path):
    cfg = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", cfg, "seed"])
    assert result.exit_code == 0, result.output
    dev = tmp_path / "nut" / "ecoflow.dev"
    content = dev.read_text()
    assert "battery.charge: 100" in content
    assert "ups.status: OL" in content


def test_help_lists_commands():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("read", "run", "seed", "ac", "usb", "dc"):
        assert cmd in result.output


@pytest.mark.parametrize(
    "line,expected",
    [
        ("ac off", ("ac", False)),
        ("ac on", ("ac", True)),
        ("USB OFF", ("usb", False)),
        ("  dc   on  ", ("dc", True)),
    ],
)
def test_parse_control_command_ok(line, expected):
    assert parse_control_command(line) == expected


@pytest.mark.parametrize("line", ["", "ac", "ac maybe", "fan on", "ac on off"])
def test_parse_control_command_rejects(line):
    with pytest.raises(ValueError):
        parse_control_command(line)


def test_every_driver_builds_every_output_kind():
    assert set(OUTPUT_KINDS) == {"ac", "usb", "dc"}
    for name, driver in DRIVERS.items():
        for kind in OUTPUT_KINDS:
            for enabled in (True, False):
                packet = driver.output_packet(kind, enabled)
                assert packet.payload, f"{name}/{kind} produced an empty payload"
