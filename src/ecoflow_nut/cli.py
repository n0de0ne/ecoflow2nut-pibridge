"""Manual control / inspection CLI for the EcoFlow NUT bridge."""

from __future__ import annotations

import asyncio
import json
import socket as _socket
import sys
from typing import Any

import click
import structlog

from . import delta3, eve_outlet, protocol
from . import switchbot as switchbot_mod
from .ble_client import EcoFlowBLE
from .config import Config, load_config
from .delta3 import DeviceState
from .main import OUTPUT_BUILDERS, configure_logging, run_daemon, seed_state
from .nut_writer import NutWriter, build_variables

log = structlog.get_logger(__name__)


async def _read_once(config: Config, timeout: float = 30.0) -> DeviceState:
    """Connect, wait for one complete telemetry frame, and return the state."""
    done = asyncio.Event()
    client = EcoFlowBLE(
        config.ecoflow,
        config.ble,
        on_state=lambda state: done.set() if state.is_complete else None,
    )
    await client.connect()
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except TimeoutError:
        log.warning("cli.read_timeout", note="returning partial state")
    finally:
        await client.disconnect()
    return client.state


_WIRE_NAMES = {0: "varint", 1: "i64", 2: "bytes", 5: "float"}


def _format_value(wire: int, value: Any) -> str:
    if wire == protocol.WIRE_LEN:
        raw = bytes(value)
        return f"<{len(raw)} bytes> {raw.hex()}" if raw else "<empty>"
    if wire in (protocol.WIRE_I32, protocol.WIRE_I64):
        return f"{float(value):g}"
    return str(value)


def _print_fields(payload: bytes, seen: dict[int, str]) -> None:
    """Print one frame's field table, marking what is new or changed.

    ``seen`` accumulates across frames and is updated in place. Accumulating
    matters: the device sends *partial* frames carrying only what changed, so
    diffing against the previous frame alone would flag every long-stable field
    as "changed" the moment it happens to be included again.

    Markers: ``+`` first sight, ``*`` value changed, ``?`` undecoded and in a
    range worth suspecting.
    """
    rows = protocol.decode_fields(payload)

    click.echo(f"{'FIELD':>6} {'WIRE':<7} {'NAME':<30} VALUE")
    for num, wire, value in sorted(rows, key=lambda r: r[0]):
        name = delta3.DISPLAY_FIELD_NAMES.get(num, "")
        rendered = _format_value(wire, value)
        if num not in seen:
            marker = "+"
        elif seen[num] != rendered:
            marker = "*"
        elif not name and num in delta3.SUSPECT_FIELDS:
            marker = "?"
        else:
            marker = " "
        seen[num] = rendered
        wire_name = _WIRE_NAMES.get(wire, str(wire))
        click.echo(f"{marker}{num:>5} {wire_name:<7} {name:<30} {rendered}")

    known = sum(1 for num in seen if num in delta3.DISPLAY_FIELD_NAMES)
    click.echo(
        f"\n{len(rows)} fields this frame; {len(seen)} seen so far, "
        f"{known} decoded, {len(seen) - known} unknown"
    )


async def _read_raw(config: Config, *, watch: bool, timeout: float = 30.0) -> None:
    """Print the full protobuf field table for each DisplayPropertyUpload frame."""
    frames: asyncio.Queue[bytes] = asyncio.Queue()
    client = EcoFlowBLE(
        config.ecoflow,
        config.ble,
        on_display_payload=frames.put_nowait,
    )
    await client.connect()
    try:
        seen: dict[int, str] = {}
        count = 0
        while True:
            try:
                payload = await asyncio.wait_for(frames.get(), timeout=timeout)
            except TimeoutError:
                raise click.ClickException(
                    f"no telemetry frame within {timeout:.0f}s "
                    "(is the bridge still running and holding the BLE link?)"
                ) from None
            count += 1
            click.echo(f"\n=== frame {count} ===")
            _print_fields(payload, seen)
            if not watch:
                return
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        click.echo("\nstopped")
    finally:
        await client.disconnect()


async def _send(config: Config, packet) -> None:
    client = EcoFlowBLE(config.ecoflow, config.ble)
    await client.connect()
    try:
        if not await client.wait_authenticated(timeout=30):
            raise click.ClickException("authentication timed out")
        await client.send_command_packet(packet)
        # Give the write a moment to flush before disconnecting.
        await asyncio.sleep(1.0)
    finally:
        await client.disconnect()


def _send_via_socket(path: str, command: str) -> str | None:
    """Ask a running daemon to run the command. None if no daemon is listening."""
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(20)
    try:
        sock.connect(path)
    except OSError:
        return None  # no daemon -> caller falls back to a direct connection
    try:
        sock.sendall((command + "\n").encode())
        return sock.recv(256).decode("utf-8", "replace").strip()
    except OSError as exc:
        return f"error: {exc}"
    finally:
        sock.close()


def _toggle(config: Config, kind: str, state: str) -> None:
    on = state.lower() in ("on", "true", "1", "enable", "enabled")
    command = f"{kind} {'on' if on else 'off'}"
    # Prefer the running daemon: it owns the single BLE connection, so this works
    # live without stopping the bridge.
    resp = _send_via_socket(config.control_socket_path, command)
    if resp is not None:
        click.echo(f"daemon: {resp}")
        if resp.startswith("error"):
            raise SystemExit(1)
        return
    # No daemon listening: connect directly (only works if nothing else holds BLE).
    click.echo("no running daemon; connecting directly...")
    asyncio.run(_send(config, OUTPUT_BUILDERS[kind](on)))
    click.echo(f"sent (direct): {command}")


@click.group()
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    show_default=True,
    help="Path to the YAML config file.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """EcoFlow DELTA 3 NUT bridge."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command()
@click.option(
    "--raw",
    "show_raw",
    is_flag=True,
    help="Dump every protobuf field the device sends, not just the decoded ones.",
)
@click.option(
    "--watch",
    "watch",
    is_flag=True,
    help="With --raw: keep printing, marking fields that changed. Ctrl-C to stop.",
)
@click.pass_context
def read(ctx: click.Context, show_raw: bool, watch: bool) -> None:
    """Connect, read one telemetry frame, and dump state + NUT variables.

    With --raw, print the full field table instead: number, wire type, value and
    the name we know it by. Most of what the device sends is undecoded, so this
    is how you identify a field the bridge is missing -- add --watch and change
    something on the unit (plug a load into a USB port, say) to see which field
    number moves.

    Note this opens its own BLE connection, and the DELTA 3 accepts only one at a
    time, so stop the bridge first: sudo systemctl stop ecoflow-nut-bridge
    """
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)

    if show_raw:
        asyncio.run(_read_raw(config, watch=watch))
        return
    if watch:
        raise click.UsageError("--watch only applies with --raw")

    state = asyncio.run(_read_once(config))
    variables = build_variables(state, config.nut)
    click.echo(
        json.dumps(
            {
                "soc_percent": state.soc_percent,
                "ac_input_watts": state.ac_input_watts,
                "ac_output_watts": state.ac_output_watts,
                "usb_output_watts": state.usb_output_watts,
                "usbc_output_watts": state.usbc_output_watts,
                "input_watts": state.input_watts,
                "output_watts": state.output_watts,
                "ac_input_present": state.ac_input_present,
                "ac_output_on": state.ac_output_on,
                "remain_charge_minutes": state.remain_charge_minutes,
                "remain_discharge_minutes": state.remain_discharge_minutes,
                "nut": variables,
            },
            indent=2,
        )
    )


@cli.group()
@click.pass_context
def ac(ctx: click.Context) -> None:  # noqa: D401
    """Toggle AC output."""


@cli.group()
@click.pass_context
def usb(ctx: click.Context) -> None:  # noqa: D401
    """Toggle USB output."""


@cli.group()
@click.pass_context
def dc(ctx: click.Context) -> None:  # noqa: D401
    """Toggle 12V DC output."""


def _register_toggle(group: click.Group, kind: str) -> None:
    @group.command("on")
    @click.pass_context
    def _on(ctx: click.Context) -> None:
        config = load_config(ctx.obj["config_path"])
        configure_logging(config.logging.level, config.logging.format)
        _toggle(config, kind, "on")

    @group.command("off")
    @click.pass_context
    def _off(ctx: click.Context) -> None:
        config = load_config(ctx.obj["config_path"])
        configure_logging(config.logging.level, config.logging.format)
        _toggle(config, kind, "off")


_register_toggle(ac, "ac")
_register_toggle(usb, "usb")
_register_toggle(dc, "dc")


@cli.group()
@click.pass_context
def eve(ctx: click.Context) -> None:  # noqa: D401
    """Control a downstream HomeKit-over-BLE outlet (e.g. Eve Energy).

    Sheds a single load (e.g. an Unraid server) independently of the DELTA 3's
    all-or-nothing AC bank. Requires the [eve] extra and a one-time pairing.
    """


@eve.command("discover")
@click.option("--timeout", default=10, show_default=True, help="Scan seconds.")
@click.pass_context
def eve_discover(ctx: click.Context, timeout: int) -> None:
    """Scan for HomeKit-over-BLE accessories (to find a device_id to pair)."""
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    found = asyncio.run(eve_outlet.discover(config.eve.adapter, timeout))
    click.echo(json.dumps(found, indent=2))


@eve.command("scan")
@click.option("--timeout", default=15, show_default=True, help="Scan seconds.")
@click.pass_context
def eve_scan(ctx: click.Context, timeout: int) -> None:
    """Low-level BLE scan that decodes HomeKit adverts (a diagnostic).

    Surfaces every device the radio sees -- bypassing aiohomekit's filtering --
    and for HomeKit accessories shows their device_id and paired state, to tell
    "not advertising" apart from "still paired to Apple Home".
    """
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    found = asyncio.run(eve_outlet.raw_scan(config.eve.adapter, timeout))
    click.echo(json.dumps(found, indent=2))


@eve.command("pair")
@click.pass_context
def eve_pair(ctx: click.Context) -> None:
    """Pair with the configured outlet and persist its pairing data.

    Set eve.device_id and eve.setup_code in the config first. The outlet must be
    reset and removed from Apple Home (a HAP accessory pairs to one controller).
    """
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    try:
        device_id = asyncio.run(eve_outlet.pair(config.eve))
    except eve_outlet.EveError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"paired {device_id} -> {config.eve.pairing_file}")


def _eve_set(config: Config, on: bool) -> None:
    try:
        asyncio.run(eve_outlet.EveOutlet(config.eve).set(on))
    except eve_outlet.EveError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"eve {'on' if on else 'off'}")


@eve.command("on")
@click.pass_context
def eve_on(ctx: click.Context) -> None:
    """Turn the outlet on."""
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    _eve_set(config, True)


@eve.command("off")
@click.pass_context
def eve_off(ctx: click.Context) -> None:
    """Turn the outlet off."""
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    _eve_set(config, False)


@eve.command("status")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Dump every characteristic the accessory exposes (diagnostic).",
)
@click.pass_context
def eve_status(ctx: click.Context, show_all: bool) -> None:
    """Read the outlet's current on/off state (and power, when it meters).

    With --all, list every characteristic the paired accessory exposes. That is
    how you find out whether a given unit reports power at all: look for a
    readable ("pr") entry of type e863f10d-... -- Eve's instantaneous watts.
    Auto-shutdown's Eve confirmation needs that characteristic to exist.
    """
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    outlet = eve_outlet.EveOutlet(config.eve)
    try:
        if show_all:
            rows = asyncio.run(outlet.characteristics())
        else:
            reading = asyncio.run(outlet.read())
    except eve_outlet.EveError as exc:
        raise click.ClickException(str(exc)) from exc

    if not show_all:
        state = "unknown" if reading.on is None else ("on" if reading.on else "off")
        if reading.watts is None:
            click.echo(f"{state} (this outlet does not report power)")
        else:
            click.echo(f"{state}, {reading.watts:.1f} W")
        return

    click.echo(f"{'AID':>4} {'IID':>5}  {'PERMS':<10} {'TYPE':<36} VALUE")
    for row in rows:
        perms = ",".join(row.get("perms") or [])
        value = row.get("value", "")
        unit = row.get("unit") or ""
        label = row.get("description") or ""
        click.echo(
            f"{row['aid']:>4} {row['iid']:>5}  {perms:<10} "
            f"{str(row.get('type', '')):<36} {value}{unit}"
            + (f"   ({label})" if label else "")
        )


@cli.group()
@click.pass_context
def switchbot(ctx: click.Context) -> None:  # noqa: D401
    """Control a SwitchBot Bot (mechanical button pusher) over BLE.

    A convenience to physically press a server's power button. Plain BLE, no
    pairing. Manual only -- not wired into auto-shutdown.
    """


@switchbot.command("scan")
@click.option("--timeout", default=10, show_default=True, help="Scan seconds.")
@click.pass_context
def switchbot_scan(ctx: click.Context, timeout: int) -> None:
    """Scan for nearby SwitchBot devices (to find the Bot's MAC)."""
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    found = asyncio.run(switchbot_mod.scan(config.switchbot.adapter, timeout))
    click.echo(json.dumps(found, indent=2))


def _switchbot_send(config: Config, action: str) -> None:
    try:
        message = asyncio.run(switchbot_mod.SwitchBot(config.switchbot).send(action))
    except switchbot_mod.SwitchBotError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(message)


@switchbot.command("press")
@click.pass_context
def switchbot_press(ctx: click.Context) -> None:
    """Press the button (momentary)."""
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    _switchbot_send(config, "press")


@switchbot.command("on")
@click.pass_context
def switchbot_on(ctx: click.Context) -> None:
    """Send 'on' (Bot in switch mode)."""
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    _switchbot_send(config, "on")


@switchbot.command("off")
@click.pass_context
def switchbot_off(ctx: click.Context) -> None:
    """Send 'off' (Bot in switch mode)."""
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    _switchbot_send(config, "off")


@cli.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Run the bridge daemon (default deployment mode)."""
    sys.exit(run_daemon(ctx.obj["config_path"]))


@cli.command()
@click.pass_context
def seed(ctx: click.Context) -> None:
    """Write an initial placeholder NUT state file.

    Used as a systemd ExecStartPre so the dummy-ups driver has a file to read
    before the first BLE telemetry arrives (otherwise the driver fails at boot).
    """
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    NutWriter(config.nut).write(seed_state())
    click.echo(f"seeded {config.nut.dev_file_path}")


def main() -> None:
    cli(obj={})


if __name__ == "__main__":  # pragma: no cover
    main()
