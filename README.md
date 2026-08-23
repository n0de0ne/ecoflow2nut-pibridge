# ecoflow-nut-bridge

*🇬🇧 English · [🇫🇷 Français](README.fr.md)*

Expose an **EcoFlow portable power station** as a standard
**NUT (Network UPS Tools)** UPS over Bluetooth Low Energy. The bridge polls the
station over BLE, translates its telemetry (state of charge, AC input/output
watts, AC-input-present) into NUT variables, writes a `dummy-ups` state file and
runs `upsd` on port 4141 — so any NUT client (Unraid's built-in client,
Synology, `upsc`, …) can monitor it as if it were a normal UPS.

Both EcoFlow BLE protocol generations are supported — the **DELTA 2** family
(DELTA 2, **DELTA 2 Max**, DELTA 2 Black, DELTA 3 1500) and the **DELTA 3**
family (DELTA 3, DELTA 3 Plus, River 3) — selected with one config setting.

> ⚠️ **Disclaimer.** This project is **not affiliated with, authorized, or
> endorsed by EcoFlow**. It speaks an undocumented BLE protocol reconstructed by
> the community (see [Credits](#10-credits)). Use at your own risk. It does not
> use the EcoFlow cloud API for telemetry or control.

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Disclaimer](#2-disclaimer)
3. [Hardware support](#3-hardware-support)
4. [Quick start (Docker on Unraid)](#4-quick-start-docker-on-unraid)
5. [Production deployment (Pi Zero 2W)](#5-production-deployment-raspberry-pi-zero-2w)
6. [Configuration reference](#6-configuration-reference)
7. [NUT client setup](#7-nut-client-setup)
8. [Troubleshooting](#8-troubleshooting)
9. [Architecture](#9-architecture)
10. [Credits](#10-credits)

---

## 1. What it does

A single async daemon connects to the power station over BLE, polls state every few
seconds, derives NUT `ups.status` / `battery.charge` / `ups.load` / runtime, and
keeps a `dummy-ups` `.dev` file fresh. The NUT `dummy-ups` driver re-reads that
file and `upsd` serves it on 4141. The same Python code runs unchanged in a
Docker container (validation) and as a bare-metal systemd service (production);
only the wrapper differs.

It also exposes manual control commands — toggle **AC**, **USB** and **12V DC**
outputs — as Python functions and a small CLI. An optional, opt-in
**auto-shutdown** policy can cut AC output when the battery is critically low
(see [Auto-shutdown](#auto-shutdown)); it is disabled by default. For per-load
control it can also drive a downstream **HomeKit-over-BLE outlet** (e.g. an Eve
Energy) — shedding a single device while the station's other AC sockets stay
powered (see [Per-load shedding](#per-load-shedding-with-a-homekit-outlet)).

## 2. Disclaimer

See the banner above. The BLE protocol is reverse-engineered, may change with
firmware updates, and is implemented on a best-effort basis. **Correctness of
the read path (SoC + AC status) is prioritised over feature completeness.**

## 3. Hardware support

EcoFlow ships **two incompatible BLE protocol generations**. Both are
implemented; pick one with `ecoflow.model` in `config.yaml`.

| `model` | Devices | Serial prefix | BLE name | Wire protocol |
|---------|---------|---------------|----------|---------------|
| `delta2max` | **DELTA 2 Max** (2048 Wh, 2400 W AC) | `R351`, `R354` | `EF-R35…` | V2 frames, fixed-width structs |
| `delta2` | DELTA 2, DELTA 2 Black, DELTA 3 1500 | `R331`/`R335`, `R701`, `D361`/`D365` | `EF-R33…` | V2 frames, fixed-width structs |
| `delta3` | DELTA 3 (1024 Wh, 1800 W AC), DELTA 3 Plus, River 3 | `P231` | `EF-D3…` | V3 frames, protobuf (`pd335`) |

| Item | Detail |
|------|--------|
| Test host | Unraid + Realtek RTL8821CU USB BT dongle (BlueZ) |
| Production host | Raspberry Pi Zero 2W, Raspberry Pi OS Lite 64-bit, integrated BT |

Model spelling is forgiving — `"DELTA 2 Max"`, `"delta-2-max"`, `"delta2max"`
and the serial prefix `"R351"` all select the same driver.

> ### Your serial prefix is not on the list?
> **Set `model` to the generation anyway.** Selection is by configuration, not by
> sniffing the serial number. Other integrations gate on a hardcoded prefix list,
> so a regional variant or a newer hardware revision gets rejected as an
> "unsupported device" even when it speaks a protocol they already implement —
> this bridge has no such gate. Confirm the guess with
> [`ecoflow-nut sniff`](#verifying-an-unlisted-device).

> ### Protocol note — DELTA 2 ≠ DELTA 3
> The **DELTA 2 generation** sends telemetry as packed little-endian C structs,
> one per subsystem (PD / EMS / BMS / inverter / MPPT), identified by the frame's
> `(src, cmd_set, cmd_id)` — there are no field tags in the payload. Control is a
> per-function `cmd_id` with a small raw payload.
>
> The **DELTA 3 generation** sends a protobuf `DisplayPropertyUpload` and takes a
> single `ConfigWrite` message, over a two-bytes-longer V3 frame.
>
> They share only the CRC8/CRC16 framing and the authentication handshake. Full
> details, field tables and verification status: [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

> ### Authentication
> Modern units negotiate an encrypted session (`encrypt_type 7`, ECDH). The final
> authentication step hashes `md5(user_id + serial)`, where `user_id` is your
> EcoFlow account user id. This id is used **only once, locally, to derive the
> BLE session secret** — no telemetry or control traffic goes through the cloud.
> Obtain it once (e.g. via the EcoFlow login API or app diagnostics) and set it
> as `ecoflow.user_id` in the config. If your unit advertises `encrypt_type 0`
> or `1`, no `user_id` is required. See [Troubleshooting](#8-troubleshooting).

### Verifying an unlisted device

Two diagnostics identify a device and confirm the driver guess without touching
the daemon:

```bash
# 1. Find the MAC, the advertised name (which encodes the model) and encrypt_type.
ecoflow-nut scan

# 2. Connect and dump every frame the device sends, decoded against the
#    configured model's layouts. Compare the values with the unit's own screen.
ecoflow-nut sniff --seconds 60

# 3. Keep a full capture for offline analysis / bug reports.
ecoflow-nut sniff --out frames.jsonl
```

`sniff` reports **every** frame, including ones no driver claims, and flags any
payload that is shorter or longer than the layout expects — which is how you
tell "wrong generation configured" from "right generation, firmware variant".

## 4. Quick start (Docker on Unraid)

The container bundles BlueZ, the NUT server, and the bridge daemon.

1. Create a config from the example:

   ```bash
   mkdir -p config
   cp config/config.example.yaml config/config.yaml
   # edit config/config.yaml: set mac, serial, and user_id (if needed)
   ```

2. Use the provided compose file (edit the image owner / timezone):

   ```bash
   docker compose up -d
   docker compose logs -f
   ```

   It runs with `network_mode: host` (so the container can see the kernel
   Bluetooth adapter and `upsd` is reachable at `<host>:4141`) and
   `privileged: true`. The container starts its own `bluetoothd`, so no host
   D-Bus mount is needed.

3. Verify:

   ```bash
   docker exec ecoflow-nut-bridge upsc ecoflow@localhost:4141
   ```

   You should see sensible `battery.charge`, `ups.status`, `ups.load`, etc.

Images are built and published automatically to
`ghcr.io/<owner>/ecoflow2nut-pibridge` for `linux/amd64` and `linux/arm64` on
every push to `main`.

## 5. Production deployment (Raspberry Pi Zero 2W)

Bare-metal systemd, no Docker. From a checkout on the Pi:

```bash
sudo ./systemd/install.sh
```

The installer:

* installs `bluez`, `nut-server`, `nut-client`, `python3-venv`;
* creates the `ecoflow` service user (in the `bluetooth` and `nut` groups);
* builds a venv at `/opt/ecoflow-nut-bridge/.venv` and installs the package;
* drops NUT config into `/etc/nut/` and sets `MODE=netserver`;
* installs config at `/etc/ecoflow-nut/config.yaml`;
* installs and enables the `ecoflow-nut-bridge.service` systemd unit;
* adds a drop-in so `nut-server` starts **after** the bridge. The bridge's
  `ExecStartPre` seeds the dummy-ups state file first, so the driver always has
  a file to read at boot (otherwise `dummy-ups` fails on a cold start and NUT
  stays down until a manual restart).

Then:

```bash
sudo nano /etc/ecoflow-nut/config.yaml   # MAC / serial / user_id / auto_shutdown
sudo nano /etc/nut/upsd.users            # set real passwords
sudo systemctl start ecoflow-nut-bridge  # seeds the state file, then connects
sudo systemctl restart nut-server        # starts upsd + dummy-ups driver
upsc ecoflow@localhost:4141
```

Raspberry Pi OS runs `bluetoothd` by default, so BLE works without extra setup
(unlike the bare Unraid host). Watch progress with `journalctl -u
ecoflow-nut-bridge -f` — look for `ble.authenticated` then `state.updated`.

> The Pi is powered from the station's own USB-A port, so keep `auto_shutdown.cut_usb`
> at its default `false` — cutting USB would kill the bridge itself.

## 6. Configuration reference

Full annotated example: [`config/config.example.yaml`](config/config.example.yaml).

| Key | Default | Meaning |
|-----|---------|---------|
| `ecoflow.mac` | — (required) | BLE MAC of the power station (find it with `ecoflow-nut scan`) |
| `ecoflow.serial` | — | Device serial (used for auth + reported to NUT) |
| `ecoflow.model` | `delta3` | Protocol driver: `delta2max`, `delta2`, `delta3` or `raw` (see [Hardware support](#3-hardware-support)) |
| `ecoflow.poll_interval_seconds` | `5` | How often the BLE link is checked for liveness — a watchdog, **not** a sample rate (see [How often is data sampled?](#how-often-is-data-sampled)) |
| `nut.min_write_interval_seconds` | `2` | Minimum seconds between rewrites of the NUT state file. A status change is **always** published immediately |
| `ecoflow.encrypt_type` | `auto` | `auto` reads it from the advertisement; or force `0`/`1`/`7` |
| `ecoflow.user_id` | `""` | EcoFlow account user id, required for `encrypt_type 7` |
| `ble.adapter` | `hci0` | BlueZ adapter |
| `ble.connect_timeout_seconds` | `30` | BLE connect timeout |
| `ble.reconnect_backoff_max_seconds` | `60` | Max exponential reconnect backoff |
| `nut.dev_file_path` | `/var/run/nut/ecoflow.dev` | dummy-ups state file (must match `ups.conf`) |
| `nut.battery_capacity_wh` | `1024` | Pack capacity for runtime estimate — set to your model's rating (DELTA 2 Max: `2048`) |
| `nut.realpower_nominal` | `1800` | Rated AC output, used to derive `ups.load` percent (DELTA 2 Max: `2400`) |
| `nut.ac_input_present_min_watts` | `10` | Fallback "mains present" threshold for models that do not measure input voltage |
| `nut.ac_input_present_min_volts` | `50` | "Mains present" threshold where the device measures input voltage (DELTA 2 generation) |
| `nut.thresholds.low_battery_percent` | `25` | SoC below this → `OB LB` |
| `nut.thresholds.critical_battery_percent` | `10` | Informational; auto-cut uses `auto_shutdown.trigger_soc_percent` |
| `nut.static_values.*` | — | Nameplate values reported verbatim (voltage, frequency, mfr, model, serial) |
| `logging.level` / `logging.format` | `INFO` / `json` | structlog level and `json`/`console` output |
| `auto_shutdown.enabled` | `false` | Master switch for the auto-cut policy (opt-in) |
| `auto_shutdown.trigger_soc_percent` | `10` | Arm + cut at/below this SoC, on battery only |
| `auto_shutdown.recover_soc_percent` | `15` | "Recovered" SoC: disarms the SoC trigger, and gates `restore_on_recovery` — output is restored only once SoC climbs back to this |
| `auto_shutdown.grace_period_seconds` | `300` | Delay after arming (SoC trigger) before cutting |
| `auto_shutdown.min_load_watts` | `null` | Low-load trigger: cut when AC output stays ≤ this (on battery, any SoC). `null` disables |
| `auto_shutdown.load_grace_seconds` | `60` | Debounce for the low-load trigger |
| `auto_shutdown.cut_ac` / `cut_usb` / `cut_dc` | `true`/`false`/`false` | Which station outputs to cut |
| `auto_shutdown.cut_eve` | `false` | Also cut a downstream HomeKit-over-BLE outlet (see [Per-load shedding](#per-load-shedding-with-a-homekit-outlet)) |
| `auto_shutdown.eve_confirm_idle_watts` | `null` | Read the Eve's own draw and only cut once it is at/below this. `null` cuts immediately (see [Confirming at the outlet](#confirming-at-the-outlet-before-cutting)) |
| `auto_shutdown.restore_on_recovery` | `false` | Re-enable cut outputs when power/SoC recovers |
| `eve.enabled` | `false` | Master switch for the HomeKit-over-BLE outlet |
| `eve.device_id` | `""` | HomeKit accessory id (from `eve discover`) |
| `eve.adapter` | `hci1` | Bluetooth adapter for the outlet — ideally a **separate** dongle from the EcoFlow link |
| `eve.pairing_file` | `/var/lib/ecoflow-nut/eve-pairing.json` | Where aiohomekit pairing data is persisted |
| `eve.setup_code` | `""` | 8-digit HomeKit code (e.g. `123-45-678`), needed only to pair |
| `switchbot.enabled` | `false` | Master switch for the SwitchBot Bot (manual power-button presser) |
| `switchbot.mac` | `""` | BLE MAC of the Bot (from `switchbot scan`) |
| `switchbot.adapter` | `hci0` | Bluetooth adapter for the Bot (on-demand connect) |

### Auto-shutdown

Disabled by default. When `auto_shutdown.enabled` is true, **two independent
triggers** (either can fire, both only while on battery) arm a cut:

- **SoC trigger** — SoC drops to `trigger_soc_percent`, then after
  `grace_period_seconds` (during which your NUT clients shut down off the
  `OB LB` status) it sends `set_ac_enabled(false)` once. Re-arms only after
  recovery; a climb back to `recover_soc_percent` disarms it.
- **Low-load trigger** — AC output stays at/below `min_load_watts` for
  `load_grace_seconds`, at **any** SoC. This catches "the protected equipment
  has finished shutting down, so there's nothing left to power" and cuts the
  idle inverter to preserve the battery. A load above the threshold resets the
  debounce. Disabled unless `min_load_watts` is set.

`cut_usb`/`cut_dc` are available but default off; **never enable `cut_usb` if the
bridge host is powered from the station's USB port.**

This complements — does not replace — normal NUT behaviour: clients shut
themselves down from `ups.status` (`OB LB`); auto-shutdown additionally protects
the pack by cutting output after they've gone down.

### Per-load shedding with a HomeKit outlet

The station's AC output is a **single, all-or-nothing bank** — the AC toggle
switches every AC socket at once. To shed **one** load while keeping the others
live, the bridge can drive a downstream **HomeKit-over-BLE smart outlet** (e.g. an
Eve Energy, the BLE / non-Thread model) as an independent cut target. The bridge
becomes the outlet's HomeKit controller (HAP over BLE via the optional
[`aiohomekit`](https://pypi.org/project/aiohomekit/) extra) — no Apple hub
involved.

> Install the extra: `pip install ecoflow-nut-bridge[eve]`

**Motivating example — keep the network up, shed the server.** Plug the router /
fibre ONT straight into the station's AC sockets, and plug an Unraid server into
the Eve outlet (which is itself on a station socket). On a grid outage you want
Unraid to shut down cleanly and then *fully drop* so the small network load runs
on the remaining battery for as long as possible; when grid power returns, Unraid
should power back up:

```yaml
auto_shutdown:
  enabled: true
  min_load_watts: 30           # set ABOVE network-only draw, BELOW network+idle-Unraid
  load_grace_seconds: 60
  cut_ac: false                # keep the station's AC bank ON (router/fibre stay up)
  cut_usb: false
  cut_dc: false
  cut_eve: true                # the only thing we cut is the Unraid outlet
  restore_on_recovery: true    # turn Unraid back on when AC returns
eve:
  enabled: true
  device_id: "AA:BB:CC:11:22:33"
  adapter: "hci1"              # a SECOND BT dongle; keep hci0 for the EcoFlow
```

How it plays out, driven entirely by the existing **low-load trigger**:

1. Grid fails → the station switches to battery → NUT publishes `OB`, then `OB LB`
   at the low-battery threshold → Unraid shuts itself down gracefully (NUT client).
2. Once Unraid halts, total AC draw collapses below `min_load_watts`; after
   `load_grace_seconds` the bridge turns the **Eve outlet off** — the router /
   fibre keep running on the still-live AC bank, now stretching the battery much
   further.
3. Grid returns (battery charging again) → the recovery path turns the **Eve
   outlet back on**. With the server's BIOS set to *"restore / power on after AC
   loss"*, applying power reboots Unraid automatically.

Pick `min_load_watts` so it sits between your network-only draw and your
network-plus-idle-server draw (e.g. fibre+switch ≈ 15 W, +idle Unraid ≈ 75 W →
`30` works). The threshold is what distinguishes "server still running" from
"server has finished shutting down".

#### Confirming at the outlet before cutting

That total-AC threshold is a *proxy*. It has to be tuned between two loads, it
breaks if you plug something else into the bank, and it can fire while the server
is still writing to its array — at which point the bridge pulls the plug on a
half-finished shutdown.

If your Eve reports power (most metering Eve Energy units do), the bridge can ask
the outlet directly instead of inferring. When a trigger fires it connects, reads
the outlet's **own** draw, and only sends the off command once that draw is
genuinely idle:

```yaml
auto_shutdown:
  eve_confirm_idle_watts: 5      # cut only once the outlet reports <= 5 W
  eve_confirm_poll_seconds: 15
  eve_confirm_samples: 2         # two consecutive idle readings, so a dip
                                 # mid-shutdown isn't mistaken for "finished"
  eve_confirm_timeout_seconds: null
```

**Check your hardware first.** The watt reading uses Eve's vendor characteristic,
and not every SKU exposes it over BLE:

```bash
ecoflow-nut --config config.yaml eve status --all
```

Look for a readable (`pr`) entry of type `e863f10d-…`. Run it with the server up
and again with it off — those two numbers are exactly what to set
`eve_confirm_idle_watts` between (a powered-down PSU typically idles at 1–3 W).
Leave the setting blank and nothing changes: the outlet is cut immediately, as
before.

While waiting, the dashboard's auto-shutdown badge reads *Waiting for load to
drop · N W* rather than *CUT sent*, so you can see what it is actually doing. The
Eve **Off** button always overrides and cuts immediately.

> **It waits indefinitely by default.** `eve_confirm_timeout_seconds: null` means
> the outlet is never cut without a confirmed idle reading — the safest thing for
> the server, and the reason this feature exists. The cost: if the outlet becomes
> unreachable, or the server hangs still drawing power, nothing is ever shed and
> the battery drains to empty, where the server loses power uncleanly anyway. Set
> a number of seconds to bound that.

> **Give the outlet its own radio.** Confirmation holds one BLE connection open
> for the whole wait (far cheaper than reconnecting per sample, but continuous
> rather than one brief blip). On a shared `hci0` that contends with the DELTA 3
> link the entire time, so a separate dongle (`eve.adapter: hci1`) goes from
> recommended to strongly advised once this is on.

When confirmation is enabled the Eve is cut **before** the EcoFlow outputs, not
after — cutting the AC bank first would kill the outlet, and the server behind it,
before the confirmation could mean anything.

#### Setup (one-time)

The bridge becomes the outlet's **sole HomeKit controller** — a HAP accessory
pairs with one controller only — so first **reset the Eve and remove it from
Apple Home**, and have its **8-digit HomeKit setup code** ready.

Install the extra and make sure the pairing-file directory is writable by the
service user:

```bash
sudo /opt/ecoflow-nut-bridge/.venv/bin/pip install "/opt/ecoflow-nut-bridge[eve]"
sudo install -d -o ecoflow -g nut /var/lib/ecoflow-nut
```

Then discover, pair and verify. On a **single-radio** host (no second dongle,
`eve.adapter: hci0`), **stop the bridge first** — pairing does a heavy scan that
would fight the live EcoFlow link; once paired, day-to-day control coexists fine:

```bash
EVE='sudo -u ecoflow /opt/ecoflow-nut-bridge/.venv/bin/ecoflow-nut --config /etc/ecoflow-nut/config.yaml'

sudo systemctl stop ecoflow-nut-bridge     # single radio only; skip if eve has its own dongle

$EVE eve discover            # find the accessory's device_id ...
$EVE eve scan                # ... or a raw scan that also shows the paired flag
# set eve.device_id + eve.setup_code in the config, then:
$EVE eve pair                # ECDH handshake; persists to eve.pairing_file
$EVE eve on  && $EVE eve status   # verify the relay clicks
$EVE eve off

sudo systemctl start ecoflow-nut-bridge
```

After pairing you can blank `eve.setup_code` (it's only used to pair). The
device id is matched case-insensitively, so either case works in the config.

> **`eve scan`** is a diagnostic: it bypasses aiohomekit and decodes the raw
> HomeKit advertisement, reporting each accessory's `device_id`, category and
> **`paired`** flag — handy to tell "not advertising" apart from "still paired to
> Apple Home" if `discover` comes back empty.

> **Bluetooth radios.** The EcoFlow link is a persistent, latency-sensitive BLE
> session. The bridge talks to the outlet **on demand** (connect → write →
> disconnect), so on a shared single adapter (`eve.adapter: hci0`) the EcoFlow
> link is only briefly perturbed during an actual cut/restore (you may see one
> `daemon.reconnect_wait` afterwards — it self-heals). For a busier setup, give
> the outlet its **own** USB BT dongle (`eve.adapter: hci1`) so it never contends.

> **Web dashboard.** With `web.enabled` and `eve.enabled`, an **Eve outlet** row
> appears in *Port controls* with On/Off buttons (enter the control token to use
> them). Its LED shows the **last-commanded** state — the outlet is not polled, to
> spare BLE airtime — so it reads `?` until the first command or auto-shutdown
> action.

> **Recovery semantics.** `restore_on_recovery` turns the outlet back on after
> AC returns, but **only once SoC has climbed back to `recover_soc_percent`** —
> so the server isn't re-powered until the battery again holds enough charge to
> shut it down cleanly if mains drops again. If the grid returns at a low SoC,
> the outlet stays off and is switched on when charging reaches that level.
> This also survives a **full drain that reboots the bridge**: on startup, if it
> comes up on AC but below `recover_soc_percent`, it holds the outlet off until
> SoC recovers (so the server doesn't boot without a shutdown buffer).

### Server power button (SwitchBot)

Optionally, a **SwitchBot Bot** (the mechanical button pusher) can physically
press a machine's power button — handy to boot a server that doesn't
auto-power-on. It's plain BLE (no pairing) and needs no extra dependency:

```bash
ecoflow-nut switchbot scan         # find the Bot's MAC
# set switchbot.enabled + switchbot.mac in the config, then:
ecoflow-nut switchbot press        # momentary press (also: on / off in switch mode)
```

With `web.enabled` and `switchbot.enabled`, a **Server power** *Press* button
appears in the dashboard's *Port controls* (control token required). Like the
Eve outlet it connects on demand (one brief BLE blip per press on a shared
radio).

It is **manual only** — deliberately *not* wired into auto-shutdown: a power
button is a toggle, so an automated press could shut down a server that has
already auto-booted from AC restore. Password-protected Bots are not supported.

### NUT variable mapping

| NUT variable | Source |
|--------------|--------|
| `ups.status` | `OL` if AC present and drawing > `ac_input_present_min_watts`; `OB LB` if SoC < low threshold; else `OB` |
| `battery.charge` | `cms_batt_soc` (SoC %) |
| `battery.runtime` | `(SoC/100 · capacity_wh · 0.9) / ac_output_watts · 3600`, or `99999` when idle |
| `ups.realpower` | AC output watts (`pow_get_ac_out`, abs) |
| `ups.load` | AC output as a percent of `ups.realpower.nominal` |
| `ups.realpower.nominal` | `1800` |
| `input.*` / `output.*` | static nameplate values |

### CLI

```bash
ecoflow-nut --config config.yaml read     # connect, read one frame, dump JSON
ecoflow-nut --config config.yaml run      # run the daemon (default mode)
ecoflow-nut --config config.yaml ac on    # toggle AC output  (also: ac off)
ecoflow-nut --config config.yaml read --raw          # every protobuf field the device sends
ecoflow-nut --config config.yaml read --raw --watch  # ... and mark what changes
ecoflow-nut --config config.yaml usb on   # toggle USB output (also: usb off)
ecoflow-nut --config config.yaml dc on    # toggle 12V DC out (also: dc off)
```

Optional HomeKit-over-BLE outlet (the `[eve]` extra; see
[Per-load shedding](#per-load-shedding-with-a-homekit-outlet)):

```bash
ecoflow-nut --config config.yaml eve discover   # list pairable HomeKit accessories
ecoflow-nut --config config.yaml eve scan       # raw scan: device_id + paired flag
ecoflow-nut --config config.yaml eve pair       # pair (needs device_id + setup_code)
ecoflow-nut --config config.yaml eve on         # toggle outlet (also: off / status)
ecoflow-nut --config config.yaml eve status --all   # dump every characteristic
```

Unlike `ac`/`usb`/`dc`, the `eve` commands connect to the outlet directly (its
own BLE accessory), so don't run one concurrently with a dashboard toggle.

The station allows only **one** BLE connection at a time, so the `ac`/`usb`/`dc`
commands talk to the **running daemon** over a local control socket
(`control_socket_path`) and it sends the command on its existing connection —
no need to stop the bridge. If no daemon is running, the CLI falls back to
connecting directly. So with the daemon up you can just run, in the same place
the daemon runs:

```bash
# bare metal (Pi)
sudo -u ecoflow /opt/ecoflow-nut-bridge/.venv/bin/ecoflow-nut \
  --config /etc/ecoflow-nut/config.yaml ac off

# docker
docker exec ecoflow-nut-bridge ecoflow-nut --config /app/config/config.yaml ac off
```

### Web UI & data logging

An optional control dashboard runs **inside the daemon**, so it shares the single
BLE connection — the page shows live telemetry and its toggles go out over the
existing link (no second connection, no stopping the bridge). It is **disabled by
default**.

```yaml
web:
  enabled: true
  host: "0.0.0.0"
  port: 8080
  auth_token: ""            # required for control actions; prefer ECOFLOW_WEB_TOKEN
  require_auth_for_read: false
```

Install the extra and start the bridge:

```bash
pip install "ecoflow-nut-bridge[web]"        # or [server] for web + postgres
ECOFLOW_WEB_TOKEN=somesecret ecoflow-nut --config config.yaml run
# open http://<bridge-host>:8080
```

The UI is a small single-page app with four tabs — a side rail on a desktop, a
floating tab bar on a phone. Each tab is a real link (`#/dashboard`, `#/history`,
`#/energy`, `#/settings`), so they can be bookmarked and the back button works.
The published Docker image already includes the web + Postgres extras; just set
`web.enabled: true` and expose port 8080.

It is built phone-first: one column, 44px touch targets, and safe-area insets so
nothing hides under a notch or a home indicator. The desktop rail is the single
breakpoint on top of that. Surfaces are translucent and blur what scrolls behind
them, over a fixed backdrop that gives the blur something to work on; where a
browser has no `backdrop-filter` the surfaces fall back to opaque and the page is
unchanged but flat. Add it to a phone's home screen and it runs without browser
chrome, drawing under the status bar.

**Dashboard** — SoC, AC in/out watts, USB/USB-C watts, status, runtime and
charge/discharge estimates, with on/off buttons for **AC**, **USB** and **12V
DC**, plus the auto-shutdown state and a live enable/disable. When the
[HomeKit outlet](#per-load-shedding-with-a-homekit-outlet) is enabled, a fourth
**Eve outlet** control appears (showing its last commanded on/off state), and a
[SwitchBot](#server-power-button-switchbot) **Press** button if that's enabled.

A coloured LED sits next to each control. **AC** and **12V DC** carry the
device's own switch flag. **USB** does not — no model in the DELTA 2 generation
publishes one — so its state is read off the draw instead: power leaving a port
that can be switched off proves the switch is closed. Only a bank at zero watts
is genuinely ambiguous (off, or on with nothing plugged in) and only that shows
`?`; hovering says so. Toggling USB from the bridge also records what was
commanded, since at zero draw nothing else ever would. A port that is on but
idle reads `ON · idle`; one that is drawing shows its watts, with USB summing
all six ports (two USB-A, two fast-charge USB-A, two USB-C). The **auto-shutdown**
badge is grey
*Disabled*, green *Monitoring*, pulsing amber *ARMED · cutting in Ns*, or pulsing
red *CUT sent*. Turning the USB output off pops a confirmation, since the bridge
host is often powered from the station's USB port.

A **Solar input** tile and chart series show PV harvest on models that report it
(the DELTA 2 generation); it reads `–` rather than `0` on models that do not, so
"not reported" stays distinguishable from "reporting zero".

A **Battery health** card appears on models whose BMS reports it (the DELTA 2
generation): pack temperature with the hot/cold cell range beside it, the
cell-voltage spread (which widens long before capacity or SoH move, and is the
earliest warning a failing cell gives), charge cycles and SoH, and measured
capacity against nameplate. Under it, any limit the station is enforcing on
itself — a charge ceiling, a discharge floor, a capped AC charge rate — because
without them that behaviour is indistinguishable from a fault. Pack temperature
is also published as NUT's standard `battery.temperature`, so `upsc`, Unraid
and Home Assistant pick it up with no configuration. Every field here was
confirmed carrying live, cross-checked values on real hardware first; the same
BMS publishes several that are permanently zero on this firmware, and those are
deliberately left out (see [`docs/PROTOCOL.md`](docs/PROTOCOL.md)).

**History** — a chart you can navigate. Scroll or pinch to zoom about the
cursor, drag to pan, double-click or **Reset** to go back to the selected range.
**Live** pins the right edge to now and un-pins as soon as you pan away. Zooming
in genuinely increases resolution: the window is re-fetched at a bucket width
derived from the canvas width, and the caption tells you what you're looking at
("330 points · 4 min average"). Hovering snaps a crosshair to the nearest actual
sample and reads out every series at that point; over a gap in the data it
disappears rather than inventing a value, and lines break across outages instead
of drawing through them. Every stored metric is chartable — SoC, AC in/out, USB,
and total input/output watts — toggled from the legend and remembered per
browser. With the canvas focused, arrow keys pan, `+`/`-` zoom, `Home`/`End` jump
to the ends, and `1`–`5` pick a range preset.

**Energy** — when history logging is on, grid energy (kWh), the **Heures Creuses
/ Heures Pleines** split and cost, average and peak draw, and a projected €/day
and €/month, so you can see what your network stack and server cost to run. It
follows the same window as the chart, so zooming into last Tuesday costs last
Tuesday. See [Pricing](#electricity-pricing).

*Where the energy came from* splits everything that came **in** across solar and
grid. The kWh is the headline — on the bar itself where a segment is wide
enough to hold it, and again under each key with the percentage beside it. It
is a share of input, not of what the load drew — between the two sits the battery,
whose own level moves over the window. On a station that reports no PV at all the
split reads `–` rather than "100% grid": a station with no solar sensor is not
the same as one that harvested nothing, and only the second supports the claim.

**Settings** — a dedicated page for the "runtime-safe" config: the full
auto-shutdown policy (trigger/recover SoC %, grace periods, min-load watts, which
outputs to cut, restore-on-recovery), NUT thresholds (low/warning %, runtime-low,
AC-present watts, transfer points), the BLE link check, battery capacity /
nominal power, the electricity pricing, and the history sample interval and
retention (see [How often is data sampled?](#how-often-is-data-sampled) — this is
the knob that controls chart detail). Grouped into sections with a search box;
percentages get a slider bound to a number box; values are validated as you type
against the same bounds the bridge enforces. Only the fields you actually changed
are submitted, a counter shows how many are pending, and navigating away with
unsaved edits asks first. Changes apply **immediately** (no restart) and persist
to `settings_file` (`/var/lib/ecoflow-nut/settings.json`), which is overlaid back
onto the YAML at the next startup. Edits require the control token.

The same page also holds browser-only preferences — theme (system/dark/light) and
refresh rate — which are stored locally and never sent to the bridge.

**Freshness.** The pill in the header distinguishes the two ways live data can
stop: *live · Ns ago* when telemetry is current, amber *BLE stale* when the
bridge is up but the device link has gone quiet, and *bridge unreachable* when
the page cannot reach the bridge at all. Polling paces itself off the device's
own `poll_interval_seconds` (override it under Settings), pauses entirely while
the tab is in the background, and backs off when requests fail. Click the pill —
or press `r` — to refresh immediately.

**Auth.** Control actions (port toggles, auto-shutdown, settings) require
`auth_token` — sent as an `X-Auth-Token` header, `Authorization: Bearer`, or
`?token=`. The 🔑 button in the header opens a dialog that validates the token
against `GET /api/auth/check` before storing it, so a typo says so immediately
rather than failing on your next action; "Remember on this device" is optional,
and *Sign out* clears it. If no token is configured on the bridge the controls
are disabled and only the read-only dashboard is served; set
`require_auth_for_read: true` to also gate telemetry (the page and its assets
stay reachable so the browser can still show the unlock prompt). The token can
cut power, so keep the UI on a trusted network.

#### Telemetry history

When a store is enabled the daemon writes one telemetry sample per frame received
from the device (subject to `min_interval_seconds`, below) and the dashboard's
history charts read it back (down-sampled server-side). The bridge
runs fine if the store is absent or down — logging failures are swallowed and
never interrupt the NUT path. Both backends store the same columns (`ts, device,
soc_percent, ac_input_watts, ac_output_watts, usb/usbc watts, input/output watts,
runtime_seconds, status` + discharge/charge estimates), so you can query either
directly for your own dashboards (Grafana, etc.). Pick **one** — if both are
enabled, Postgres wins.

`GET /api/history` serves it back, either over an absolute window (what the
chart's zoom and pan use) or the last N minutes:

```bash
# absolute window, at most 500 buckets
curl 'http://bridge:8080/api/history?since=1735689600&until=1735776000&max_points=500'
# or relative, unchanged from before
curl 'http://bridge:8080/api/history?minutes=1440'
```

`since`/`until` are epoch seconds and the window is half-open, `[since, until)`.
The response echoes `since`, `until` and the `bucket_seconds` actually used.
Spans are capped at 30 days and `max_points` at 2000 so a client can't ask a Pi
to scan its whole table; an empty or future window is a normal empty result, not
an error. `GET /api/energy` takes the same window. Buckets are anchored to the
Unix epoch rather than to the window start, so a given bucket covers the same
interval regardless of how you asked for it.

If you put the UI behind a reverse proxy on a sub-path, proxy `/static/` too —
the page resolves its assets relative to itself, so a sub-path mount works, but
only if those requests reach the bridge.

##### How often is data sampled?

**The bridge does not poll the DELTA 3.** It subscribes to a BLE notify
characteristic and the device pushes `DisplayPropertyUpload` frames on its own
firmware schedule; the bridge just acks each one to keep the stream flowing.
There is no command to request a frame or change the device's reporting rate, so
**the device's push cadence is a hard ceiling on resolution**.

`ecoflow.poll_interval_seconds` is therefore *not* a sample rate, despite the
name — it only sets how often the bridge checks the link is still up, i.e. how
long a dropped connection goes unnoticed before reconnecting. Lowering it adds
no datapoints and sends nothing extra over the radio.

What you actually control is how many of those frames get **stored**:

```yaml
sqlite:
  min_interval_seconds: 10   # >= this many seconds between stored rows; 0 = every frame
  retention_days: 90         # rows older than this are deleted
```

Lower it for finer charts, at the cost of SD-card writes and disk. Over 90 days,
roughly: `30` → ~40 MB, `10` → ~115 MB, `5` → ~230 MB, `0` → bounded only by the
device's push rate. Both values are editable live from the dashboard's
**Settings → History** section and take effect on the next write, with no
restart.

Retention is applied at startup and every 6 hours thereafter. Note that deleting
rows does not shrink the SQLite file — freed pages are reused for new samples, so
the file plateaus rather than growing without bound. That is deliberate:
`VACUUM` would rewrite the whole database, which is worse for an SD card than the
space it reclaims.

**Option A — SQLite (local, self-contained, recommended for a Pi).** A single
file on the bridge host, no server and **no extra Python dependency** (stdlib
`sqlite3`). Just enable it:

```yaml
sqlite:
  enabled: true
  path: "/var/lib/ecoflow-nut/telemetry.db"   # persistent (NOT /var/run)
  min_interval_seconds: 10
  retention_days: 90
```

The systemd unit declares `StateDirectory=ecoflow-nut`, so
`/var/lib/ecoflow-nut` is created owned by the service user automatically. WAL
mode keeps SD-card wear low. Keep the file on local storage — SQLite over an
NFS/SMB share is unreliable; use Postgres for a remote database.

**Option B — Postgres (central/remote server).** For a shared database on
another host. Requires the `[postgres]` extra:

```yaml
postgres:
  enabled: true
  dsn: ""                   # prefer the ECOFLOW_PG_DSN env var
  min_interval_seconds: 0
  retention_days: 0         # 0 = keep forever
```

```bash
pip install "ecoflow-nut-bridge[postgres]"   # or [server] for web + postgres
ECOFLOW_PG_DSN=postgresql://ecoflow:secret@db-host:5432/ecoflow \
  ecoflow-nut --config config.yaml run
```

The table is created automatically on first connect (Postgres 14+; tested
against **Postgres 17**). See [`docker-compose.example.yml`](docker-compose.example.yml)
for a bridge + Postgres 17 stack.

#### Electricity pricing

With history logging enabled, the dashboard's Energy panel estimates running
cost from a time-of-use tariff. Cost is metered against **AC input (grid draw)**
— the energy actually pulled from the wall, including battery-charging losses.

```yaml
pricing:
  enabled: true
  currency: "€"
  hc_start: "22:00"   # Heures Creuses (off-peak) window start — may wrap midnight
  hc_end: "06:00"     # all other hours are Heures Pleines (peak)
  price_hc: 0.18      # off-peak €/kWh
  price_hp: 0.27      # peak €/kWh
```

Each logged sample is classified HC/HP by its **local** time-of-day, integrated
to kWh, and priced. The panel shows the HC/HP split, total cost, average/peak
draw and a projected €/day and €/month over the selected range. All of these
values are also editable live from the web UI's Settings panel.

> **What's stored vs derived.** Only the **power samples** are persisted (in the
> telemetry store). The kWh and cost figures are **computed on the fly** from
> those samples and your current prices — nothing monetary is written to the
> database. A practical upshot: editing a price re-prices the *entire* history
> retroactively. (Prices/HC hours live in `settings.json`, not the telemetry DB;
> there is no per-day price history, so a mid-history tariff change re-prices all
> past days at the new rate.)

### Home Assistant (MQTT discovery)

Point the bridge at your broker and every sensor, binary sensor and switch
appears in HA as one device. No YAML on the HA side — discovery does it.

```yaml
mqtt:
  enabled: true
  host: "10.0.1.50"           # your HA / Mosquitto host
  port: 1883
  username: "ecoflow"
  base_topic: "ecoflow"
  discovery_prefix: "homeassistant"
  device_id: ""               # defaults to nut.device_name; unique per station
```

```bash
pip install "ecoflow-nut-bridge[mqtt]"   # systemd/install.sh already does this
```

Put the password in `ECOFLOW_MQTT_PASSWORD` (a systemd drop-in) rather than in
`config.yaml`, which is world-readable in `/etc`.

Check the settings before restarting anything — this connects, announces the
entities and reports, exiting non-zero if it cannot:

```bash
ecoflow-nut --config /etc/ecoflow-nut/config.yaml mqtt test
```

Worth using: the daemon retries a bad broker forever and logs the same
`mqtt.disconnected` line each time, so a wrong host, a wrong password and a
missing `[mqtt]` extra all look identical in the journal. This says which.
Then restart and watch for `mqtt.connected` followed by `mqtt.announced`:

```bash
sudo systemctl restart ecoflow-nut-bridge
journalctl -u ecoflow-nut-bridge -f | grep mqtt
```

Everything is published as one retained JSON message per update on
`<base_topic>/<device_id>/state`, with each entity reading it through a
`value_template` — so a reading costs one message rather than one per entity,
and HA sees current values the instant it subscribes. Availability is a
retained last-will, so HA marks the device unavailable if the bridge dies
rather than showing a frozen reading forever.

#### Attributing your homelab's consumption to solar vs grid

The Energy dashboard wants **cumulative energy**, not power it has to
integrate. The bridge publishes the station's own lifetime Wh odometers for
exactly this — `device_class: energy` with `state_class: total_increasing`:

| Entity | Maps to |
|---|---|
| **Solar energy** | Energy dashboard → *Solar panels* → "Solar production energy" |
| **Grid energy in** | → *Grid consumption* |
| **AC output energy** | → *Individual devices*, to see what the homelab drew |

Settings → Dashboards → Energy, then add each in the matching slot. HA does
the solar-vs-grid split from there, and because these counters live on the
station rather than in HA, they survive a bridge restart, an HA restart and
any gap between the two. An integration helper over `Solar input` (watts)
would only count the hours HA happened to be up and listening.

Two caveats specific to this generation:

* Solar is the **XT60 input**, which is also the car/DC charging input. If you
  ever charge from a car socket through the same connector it lands in the
  same counter. On this firmware `sun_chg_power` stays 0 and the harvest is
  booked under `dc_chg_power`, so the bridge sums both names (see
  [`docs/PROTOCOL.md`](docs/PROTOCOL.md)).
* **AC output energy is what left the station**, not what the grid supplied —
  between the two sit the battery and the inverter. Over a day it converges;
  over an hour it will not.

The bridge's own Energy page answers a narrower question — the solar/grid
split of what came *in* over a chosen window, priced against your HC/HP
tariff. HA's Energy dashboard is the better home for long-run attribution.

## 7. NUT client setup

### Unraid (built-in NUT client)

Settings → **UPS Settings**:

* UPS Type / mode: **Network UPS Tools — remote** (or "Custom").
* Remote NUT server IP: the bridge host.
* UPS name: `ecoflow`, port `4141`.
* Username/password: the `monuser` credentials from `upsd.users`.

### Synology DSM

Control Panel → **Hardware & Power → UPS**:

* Enable UPS support → **Network UPS server**.
* Network UPS server IP: the bridge host.
* (Synology assumes UPS name `ups`; if needed, add an `[ups]` alias section to
  `ups.conf` pointing at the same `.dev` file, or rename `[ecoflow]`.)

### Any host with `upsc`

```bash
upsc ecoflow@<bridge-host>:4141
upsc ecoflow@<bridge-host>:4141 battery.charge
```

## 8. Troubleshooting

**BLE: device not found during scan.**
Confirm the MAC with `bluetoothctl` → `scan on`. Ensure only one thing talks to
the station at a time — the EcoFlow phone app holds the BLE connection
exclusively, so close it. On Docker you need `privileged: true` and the
`/var/run/dbus` mount.

**`encrypt_type 7 (ECDH) requires 'ecoflow.user_id'`.**
Your unit uses the encrypted handshake. Set `ecoflow.user_id` (see
[Authentication](#authentication)). To inspect the advertised type, run
`ecoflow-nut --config config.yaml read` with `logging.format: console` and look
at the `encrypt_type` in the `ble.found` log line.

**Realtek RTL8821CU dongle issues (Unraid).**
The chip needs firmware/driver support in the **host kernel** — verify with
`dmesg | grep -i bluetooth` that the firmware loaded and `hci0` was registered.
The container ships and starts its own `bluetoothd` + D-Bus, so the host does
**not** need a BlueZ userspace (Unraid has none by default). However, the kernel
only exposes the Bluetooth adapter in the **host network namespace**, so the
container must run with **host networking** for BLE to work — in `bridge` mode
the container will not see `hci0`. If you have a working host BlueZ and prefer to
use it, set `ECOFLOW_USE_HOST_DBUS=1` and bind-mount `/var/run/dbus`.

**Frequent BLE disconnects.**
Expected — the bridge reconnects with exponential backoff (up to
`reconnect_backoff_max_seconds`). If no successful read happens for 2 minutes, a
watchdog exits the process so systemd/Docker restarts it cleanly. Keep the
antenna/host within a few metres of the unit.

**`upsc` returns "Driver not connected".**
The dummy-ups driver couldn't read the `.dev` file. Check the bridge actually
wrote it (`cat /var/run/nut/ecoflow.dev`), that `port` in `ups.conf` matches
`nut.dev_file_path`, and that NUT is in `netserver` mode.

**Values look stale or partial.**
Each BLE frame only carries *changed* fields; the bridge accumulates them. SoC
and AC status appear within a few seconds of connecting.

## 9. Architecture

```
                          ┌──────────────────────── bridge host (Pi / Unraid) ───┐
  EcoFlow power station   │                                                       │
  ┌───────────────┐  BLE  │  ┌────────────────────┐    writes    ┌─────────────┐ │
  │ DELTA 2 gen:  │◀─────▶│  │  ecoflow-nut daemon │ ───────────▶ │ ecoflow.dev │ │
  │  V2 + structs │ GATT  │  │  • bleak transport  │  (dummy-ups  │  state file │ │
  │ DELTA 3 gen:  │ 0002/ │  │  • V2/V3 framing    │   format)    └──────┬──────┘ │
  │  V3 + protobuf│ 0003  │  │  • model driver     │                     │ reads  │
  └───────────────┘       │  │  • NUT translation  │              ┌──────▼──────┐ │
                          │  └────────────────────┘              │ dummy-ups   │ │
                          │                                       │   driver    │ │
                          │                                       └──────┬──────┘ │
                          │                                  ┌───────────▼──────┐ │
   NUT clients  ◀─────────┼──────────  TCP :4141  ──────────│       upsd        │ │
  (Unraid, Synology,      │                                  └──────────────────┘ │
   upsc, …)               └───────────────────────────────────────────────────────┘
```

## 10. Credits

The EcoFlow BLE protocol is undocumented. This implementation stands on the
shoulders of community reverse-engineering work — in particular
[rabits/ha-ef-ble](https://github.com/rabits/ha-ef-ble) (modern encrypted
protocol, protobuf field numbers, handshake) and
[vwt12eh8/hassio-ecoflow](https://github.com/vwt12eh8/hassio-ecoflow) (framing
and CRC), with cross-checks against
[tolwi/hassio-ecoflow-cloud](https://github.com/tolwi/hassio-ecoflow-cloud),
[nielsole/ecoflow-bt-reverse-engineering](https://github.com/nielsole/ecoflow-bt-reverse-engineering)
and `anton-ptashnik/ecoflow-api-py`. See [NOTICE](NOTICE) for license details of
vendored material. Built on [bleak](https://github.com/hbldh/bleak) and
[Network UPS Tools](https://networkupstools.org/).

Licensed under the [MIT License](LICENSE).
```
