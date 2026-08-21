"""Manual control / inspection CLI for the EcoFlow NUT bridge."""

from __future__ import annotations

import asyncio
import json
import socket as _socket
import sys

import click
import structlog

from . import devices, eve_outlet, sniffer
from . import switchbot as switchbot_mod
from .ble_client import EcoFlowBLE
from .config import Config, load_config
from .main import configure_logging, run_daemon, seed_state
from .nut_writer import NutWriter, build_variables
from .state import DeviceState

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
    driver = devices.get_driver(config.ecoflow.model)
    asyncio.run(_send(config, driver.output_packet(kind, on)))
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
    """EcoFlow-to-NUT bridge."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command()
@click.pass_context
def read(ctx: click.Context) -> None:
    """Connect, read one telemetry frame, and dump state + NUT variables."""
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    state = asyncio.run(_read_once(config))
    variables = build_variables(state, config.nut)
    click.echo(
        json.dumps(
            {
                "soc_percent": state.soc_percent,
                "ac_input_watts": state.ac_input_watts,
                "ac_output_watts": state.ac_output_watts,
                "ac_input_present": state.ac_input_present,
                "ac_output_on": state.ac_output_on,
                "nut": variables,
            },
            indent=2,
        )
    )


@cli.command()
@click.option("--timeout", default=10, show_default=True, help="Scan seconds.")
@click.option("--all", "show_all", is_flag=True, help="Include non-EcoFlow devices.")
@click.pass_context
def scan(ctx: click.Context, timeout: int, show_all: bool) -> None:
    """Scan for EcoFlow power stations and show how to configure them.

    Use this to find a device's MAC, its advertised name (which encodes the
    model -- ``EF-D3…`` is a DELTA 3, ``EF-R35…`` a DELTA 2 Max) and the
    encrypt_type its handshake needs.
    """
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    from .ble_client import scan_devices

    found = asyncio.run(scan_devices(config.ble.adapter, timeout))
    if not show_all:
        found = [d for d in found if d["looks_like_ecoflow"]]
        if not found:
            click.echo(
                "no EcoFlow devices seen. The unit advertises only while awake and "
                "not already connected -- close the EcoFlow app first, then retry "
                "with --all to list every BLE device in range."
            )
            return
    click.echo(json.dumps(found, indent=2))


@cli.command()
@click.option("--seconds", default=20, show_default=True, help="Listen time per attempt.")
@click.pass_context
def probe(ctx: click.Context, seconds: int) -> None:
    """Find which frame version a device authenticates with.

    Sending the wrong version makes a device **drop the connection** during
    authentication rather than answer, so "connected, then disconnected" is the
    signature of a wrong guess -- not something a longer timeout can fix.

    This connects once per candidate version and reports which one keeps the
    link up and produces telemetry. Put the winner in ``ecoflow.packet_version``.
    """
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    asyncio.run(_probe(config, seconds))


# Candidate frame versions, most likely first: every modern EcoFlow device uses
# V3; only the DELTA 2 generation, River 2 and Wave 2 still speak V2.
PROBE_VERSIONS = (3, 2)


async def _probe(config: Config, seconds: int) -> None:
    results: list[tuple[int, str, int]] = []
    for version in PROBE_VERSIONS:
        config.ecoflow.packet_version = version
        click.echo(f"\n=== trying packet_version {version} ===")
        frames: list[object] = []
        client = EcoFlowBLE(config.ecoflow, config.ble, on_packet=frames.append)
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001 - report and try the next version
            click.echo(f"  connect failed: {exc}")
            results.append((version, f"connect failed: {exc}", 0))
            continue
        try:
            authed = await client.wait_authenticated(timeout=seconds)
            # Keep listening briefly: a device that accepts the auth starts
            # streaming telemetry, one that rejects it drops the link instead.
            await asyncio.sleep(min(seconds, 10))
            connected = client.is_connected
            if authed and connected:
                verdict = "AUTHENTICATED, link stable"
            elif authed and not connected:
                verdict = "replied then disconnected (version likely wrong)"
            elif not connected:
                verdict = "disconnected by device (version likely wrong)"
            else:
                verdict = "connected but silent (check serial / user_id)"
            click.echo(f"  {verdict} -- {len(frames)} frame(s)")
            results.append((version, verdict, len(frames)))
        finally:
            await client.disconnect()

    click.echo("\n--- summary ---")
    for version, verdict, count in results:
        click.echo(f"  packet_version {version}: {verdict} ({count} frames)")
    winners = [v for v, _, count in results if count > 0]
    if winners:
        click.echo(
            f"\nSet 'ecoflow.packet_version: {winners[0]}' in config.yaml, then run "
            "'ecoflow-nut sniff' to identify the telemetry layout."
        )
    else:
        click.echo(
            "\nNo version produced frames. If every attempt was disconnected by the "
            "device, it may use a frame format this bridge does not implement yet "
            "(EcoFlow also has a V4 format) -- capture the raw traffic and open an "
            "issue. If attempts were silent instead, re-check ecoflow.serial and "
            "ecoflow.user_id: authentication hashes both."
        )


@cli.command()
@click.option("--seconds", default=60, show_default=True, help="Capture duration.")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write every frame to this file as JSON lines, for offline analysis.",
)
@click.option("--verbose", is_flag=True, help="Log every frame, not just new kinds.")
@click.pass_context
def sniff(ctx: click.Context, seconds: int, out_path: str | None, verbose: bool) -> None:
    """Capture and decode raw telemetry frames (a reverse-engineering aid).

    Connects like the daemon does but reports *every* frame the device sends --
    including ones no driver claims yet -- with a best-effort decode against the
    configured model's layouts. Use it to confirm a new model really speaks the
    protocol we assumed, and that the field offsets line up with the values on
    the unit's own screen.
    """
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    asyncio.run(_sniff(config, seconds, out_path, verbose))


async def _sniff(
    config: Config, seconds: int, out_path: str | None, verbose: bool
) -> None:
    driver = devices.get_driver(config.ecoflow.model)
    counts: dict[sniffer.FrameKey, int] = {}
    latest: dict[sniffer.FrameKey, dict] = {}
    warnings: dict[sniffer.FrameKey, str] = {}
    handle = open(out_path, "w") if out_path else None  # noqa: SIM115

    def _on_packet(packet) -> None:
        key = sniffer.frame_key(packet)
        first = key not in counts
        counts[key] = counts.get(key, 0) + 1
        info = sniffer.describe_packet(driver, packet)
        latest[key] = info
        if (note := sniffer.layout_coverage(driver, packet)) is not None:
            warnings[key] = note
        if handle is not None:
            handle.write(json.dumps(info) + "\n")
        if first or verbose:
            label = info.get("message", "unknown")
            click.echo(f"{sniffer.format_key(key)} len={info['payload_len']:<4} {label}")

    client = EcoFlowBLE(config.ecoflow, config.ble, on_packet=_on_packet)
    click.echo(f"connecting to {config.ecoflow.mac} as model {driver.name}...")
    await client.connect()
    try:
        if not await client.wait_authenticated(timeout=30):
            click.echo("warning: no frame arrived within 30s (auth may have failed)")
        await asyncio.sleep(seconds)
    finally:
        await client.disconnect()
        if handle is not None:
            handle.close()

    click.echo("")
    if not counts:
        raise click.ClickException(
            "no frames captured. Check ecoflow.serial/user_id and that nothing else "
            "holds the BLE connection (the device accepts only one client)."
        )
    click.echo(f"--- {sum(counts.values())} frames, {len(counts)} distinct kinds ---")
    for key, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        info = latest[key]
        label = info.get("message", "unknown")
        click.echo(f"\n{sniffer.format_key(key)}  x{count}  {label}")
        if note := warnings.get(key):
            click.echo(f"  ! {note}")
        fields = info.get("fields") or info.get("protobuf")
        if fields:
            for name, value in fields.items():
                click.echo(f"    {name} = {value}")
        else:
            click.echo(f"    payload = {info['payload_hex']}")

    click.echo("\n--- merged state ---")
    click.echo(json.dumps(_state_summary(client.state), indent=2))
    if out_path:
        click.echo(f"\nwrote {sum(counts.values())} frames to {out_path}")


def _state_summary(state: DeviceState) -> dict[str, object]:
    return {
        "soc_percent": state.soc_percent,
        "soc_source": state.soc_source,
        "ac_input_watts": state.ac_input_watts,
        "ac_output_watts": state.ac_output_watts,
        "ac_input_voltage": state.ac_input_voltage,
        "input_watts": state.input_watts,
        "output_watts": state.output_watts,
        "ac_input_present": state.ac_input_present,
        "ac_output_on": state.ac_output_on,
        "remain_charge_minutes": state.remain_charge_minutes,
        "remain_discharge_minutes": state.remain_discharge_minutes,
        "error_code": state.error_code,
    }


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
@click.pass_context
def eve_status(ctx: click.Context) -> None:
    """Read the outlet's current on/off state."""
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    try:
        value = asyncio.run(eve_outlet.EveOutlet(config.eve).status())
    except eve_outlet.EveError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("unknown" if value is None else ("on" if value else "off"))


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
