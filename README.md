# ecoflow-nut-bridge

*🇬🇧 English · [🇫🇷 Français](README.fr.md)*

Expose an **EcoFlow DELTA 3** portable power station as a standard
**NUT (Network UPS Tools)** UPS over Bluetooth Low Energy. The bridge polls the
DELTA 3 over BLE, translates its telemetry (state of charge, AC input/output
watts, AC-input-present) into NUT variables, writes a `dummy-ups` state file and
runs `upsd` on port 4141 — so any NUT client (Unraid's built-in client,
Synology, `upsc`, …) can monitor the DELTA 3 as if it were a normal UPS.

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

A single async daemon connects to the DELTA 3 over BLE, polls state every few
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
Energy) — shedding a single device while the DELTA 3's other AC sockets stay
powered (see [Per-load shedding](#per-load-shedding-with-a-homekit-outlet)).

## 2. Disclaimer

See the banner above. The BLE protocol is reverse-engineered, may change with
firmware updates, and is implemented on a best-effort basis. **Correctness of
the read path (SoC + AC status) is prioritised over feature completeness.**

## 3. Hardware support

| Item | Detail |
|------|--------|
| Confirmed device | EcoFlow **DELTA 3** (1024 Wh, 1800 W AC), serial prefix `P231`, BLE name `EF-D3` |
| Protocol family | `pd335` (modern encrypted protobuf BLE protocol) |
| Test host | Unraid + Realtek RTL8821CU USB BT dongle (BlueZ) |
| Production host | Raspberry Pi Zero 2W, Raspberry Pi OS Lite 64-bit, integrated BT |

Other EcoFlow models in the same family (DELTA 3 Plus/Max, River 3, …) use the
same framing and protobuf field numbers and will likely work with adjusted
config, but only the DELTA 3 is targeted here.

> ### Protocol note — DELTA 3 ≠ DELTA 2
> The DELTA **2** uses an older *plaintext* BLE protocol with fixed byte offsets.
> The DELTA **3** uses the newer **encrypted, protobuf-based** protocol
> (`DisplayPropertyUpload` / `ConfigWrite` messages over a V3 frame with CRC8 +
> CRC16 and an XOR-deobfuscated payload). This bridge implements the **DELTA 3**
> protocol. The read/decode path is unit-tested against **real captured frames**
> from a sibling device that shares identical protobuf field numbers.

> ### Authentication
> The DELTA 3 negotiates an encrypted session (`encrypt_type 7`, ECDH). The final
> authentication step hashes `md5(user_id + serial)`, where `user_id` is your
> EcoFlow account user id. This id is used **only once, locally, to derive the
> BLE session secret** — no telemetry or control traffic goes through the cloud.
> Obtain it once (e.g. via the EcoFlow login API or app diagnostics) and set it
> as `ecoflow.user_id` in the config. If your unit advertises `encrypt_type 0`
> or `1`, no `user_id` is required. See [Troubleshooting](#8-troubleshooting).

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

> The Pi is powered from the DELTA 3's own USB-A port, so keep `auto_shutdown.cut_usb`
> at its default `false` — cutting USB would kill the bridge itself.

## 6. Configuration reference

Full annotated example: [`config/config.example.yaml`](config/config.example.yaml).

| Key | Default | Meaning |
|-----|---------|---------|
| `ecoflow.mac` | — (required) | BLE MAC of the DELTA 3 |
| `ecoflow.serial` | — | Device serial (used for auth + reported to NUT) |
| `ecoflow.poll_interval_seconds` | `5` | How often the NUT file is refreshed from the latest state |
| `ecoflow.encrypt_type` | `auto` | `auto` reads it from the advertisement; or force `0`/`1`/`7` |
| `ecoflow.user_id` | `""` | EcoFlow account user id, required for `encrypt_type 7` |
| `ble.adapter` | `hci0` | BlueZ adapter |
| `ble.connect_timeout_seconds` | `30` | BLE connect timeout |
| `ble.reconnect_backoff_max_seconds` | `60` | Max exponential reconnect backoff |
| `nut.dev_file_path` | `/var/run/nut/ecoflow.dev` | dummy-ups state file (must match `ups.conf`) |
| `nut.battery_capacity_wh` | `1024` | Pack capacity for runtime estimate |
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
| `auto_shutdown.cut_ac` / `cut_usb` / `cut_dc` | `true`/`false`/`false` | Which DELTA 3 outputs to cut |
| `auto_shutdown.cut_eve` | `false` | Also cut a downstream HomeKit-over-BLE outlet (see [Per-load shedding](#per-load-shedding-with-a-homekit-outlet)) |
| `auto_shutdown.restore_on_recovery` | `false` | Re-enable cut outputs when power/SoC recovers |
| `eve.enabled` | `false` | Master switch for the HomeKit-over-BLE outlet |
| `eve.device_id` | `""` | HomeKit accessory id (from `eve discover`) |
| `eve.adapter` | `hci1` | Bluetooth adapter for the outlet — ideally a **separate** dongle from the DELTA 3 |
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
bridge host is powered from the DELTA 3's USB port.**

This complements — does not replace — normal NUT behaviour: clients shut
themselves down from `ups.status` (`OB LB`); auto-shutdown additionally protects
the pack by cutting output after they've gone down.

### Per-load shedding with a HomeKit outlet

The DELTA 3's AC output is a **single, all-or-nothing bank** — `set_ac_enabled`
switches every AC socket at once. To shed **one** load while keeping the others
live, the bridge can drive a downstream **HomeKit-over-BLE smart outlet** (e.g. an
Eve Energy, the BLE / non-Thread model) as an independent cut target. The bridge
becomes the outlet's HomeKit controller (HAP over BLE via the optional
[`aiohomekit`](https://pypi.org/project/aiohomekit/) extra) — no Apple hub
involved.

> Install the extra: `pip install ecoflow-nut-bridge[eve]`

**Motivating example — keep the network up, shed the server.** Plug the router /
fibre ONT straight into the DELTA 3's AC sockets, and plug an Unraid server into
the Eve outlet (which is itself on a DELTA 3 socket). On a grid outage you want
Unraid to shut down cleanly and then *fully drop* so the small network load runs
on the remaining battery for as long as possible; when grid power returns, Unraid
should power back up:

```yaml
auto_shutdown:
  enabled: true
  min_load_watts: 30           # set ABOVE network-only draw, BELOW network+idle-Unraid
  load_grace_seconds: 60
  cut_ac: false                # keep the DELTA 3 AC bank ON (router/fibre stay up)
  cut_usb: false
  cut_dc: false
  cut_eve: true                # the only thing we cut is the Unraid outlet
  restore_on_recovery: true    # turn Unraid back on when AC returns
eve:
  enabled: true
  device_id: "AA:BB:CC:11:22:33"
  adapter: "hci1"              # a SECOND BT dongle; keep hci0 for the DELTA 3
```

How it plays out, driven entirely by the existing **low-load trigger**:

1. Grid fails → DELTA 3 switches to battery → NUT publishes `OB`, then `OB LB`
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
would fight the live DELTA 3 link; once paired, day-to-day control coexists fine:

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

> **Bluetooth radios.** The DELTA 3 link is a persistent, latency-sensitive BLE
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
```

Unlike `ac`/`usb`/`dc`, the `eve` commands connect to the outlet directly (its
own BLE accessory), so don't run one concurrently with a dashboard toggle.

The DELTA 3 allows only **one** BLE connection at a time, so the `ac`/`usb`/`dc`
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

The dashboard shows SoC, AC in/out watts, USB/USB-C watts, status, runtime and
charge/discharge estimates (auto-refreshing), with on/off buttons for **AC**,
**USB** and **12V DC**, plus the auto-shutdown state (and a live enable/disable).
When the [HomeKit outlet](#per-load-shedding-with-a-homekit-outlet) is enabled, a
fourth **Eve outlet** control appears in the same panel (showing its last
commanded on/off state), and a [SwitchBot](#server-power-button-switchbot) **Press**
button if that's enabled. The published Docker image already includes the web +
Postgres extras; just set `web.enabled: true` and expose port 8080.

It also provides:

* **Visual status indicators** — a coloured LED next to each control. The **AC**
  output shows a true ON/OFF from the device's decoded flag (`flow_info_ac_out`).
  **USB** is inferred from power draw (the device exposes no USB enable flag), so
  it reads `ON · NW` when drawing and `— idle/off` otherwise — 0 W is ambiguous,
  noted in its tooltip. **12V DC** shows `n/a` (the device sends no DC
  telemetry). The **auto-shutdown** badge is grey *Disabled*, green *Monitoring*,
  pulsing amber *ARMED · cutting in Ns*, or pulsing red *CUT sent*.
* **Live settings editing** — a Settings panel edits "runtime-safe" config from
  the browser: the full auto-shutdown policy (trigger/recover SoC %, grace
  periods, min-load watts, which outputs to cut, restore-on-recovery), NUT
  thresholds (low/warning %, runtime-low, AC-present watts, transfer points),
  poll interval, battery capacity / nominal power, and the electricity pricing.
  Changes apply **immediately** (no restart) and persist to `settings_file`
  (`/var/lib/ecoflow-nut/settings.json`), which is overlaid back onto the YAML
  at the next startup. Edits require the control token.
* **USB-off guard** — turning the USB output off pops a confirmation, since the
  bridge host (a Pi) is often powered from the DELTA 3's USB port.
* **Hover detail** — the history chart shows the exact SoC / AC-in / AC-out
  values (and local time) at the point under your cursor.
* **Energy & cost** — when history logging is on, an Energy panel reports grid
  energy (kWh), the **Heures Creuses / Heures Pleines** split and cost, average
  and peak draw, and a projected €/day and €/month — so you can see what your
  network stack and server cost to run. See [Pricing](#electricity-pricing).

**Auth.** Control actions (port toggles, auto-shutdown) require `auth_token` —
sent as an `X-Auth-Token` header, `Authorization: Bearer`, or `?token=`. The
browser prompts for it and stores it locally. If no token is configured the
controls are disabled and only the read-only dashboard is served; set
`require_auth_for_read: true` to also gate telemetry. The token can cut power, so
keep the UI on a trusted network.

#### Telemetry history

When a store is enabled the daemon writes one telemetry sample per poll and the
dashboard's history charts read it back (down-sampled server-side). The bridge
runs fine if the store is absent or down — logging failures are swallowed and
never interrupt the NUT path. Both backends store the same columns (`ts, device,
soc_percent, ac_input_watts, ac_output_watts, usb/usbc watts, input/output watts,
runtime_seconds, status` + discharge/charge estimates), so you can query either
directly for your own dashboards (Grafana, etc.). Pick **one** — if both are
enabled, Postgres wins.

**Option A — SQLite (local, self-contained, recommended for a Pi).** A single
file on the bridge host, no server and **no extra Python dependency** (stdlib
`sqlite3`). Just enable it:

```yaml
sqlite:
  enabled: true
  path: "/var/lib/ecoflow-nut/telemetry.db"   # persistent (NOT /var/run)
  min_interval_seconds: 30
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
the DELTA 3 at a time — the EcoFlow phone app holds the BLE connection
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
   EcoFlow DELTA 3        │                                                       │
  ┌───────────────┐  BLE  │  ┌────────────────────┐    writes    ┌─────────────┐ │
  │  pd335 fw     │◀─────▶│  │  ecoflow-nut daemon │ ───────────▶ │ ecoflow.dev │ │
  │ DisplayProp.  │ GATT  │  │  • bleak transport  │  (dummy-ups  │  state file │ │
  │ ConfigWrite   │ 0002/ │  │  • V3 frame + CRC   │   format)    └──────┬──────┘ │
  └───────────────┘ 0003  │  │  • protobuf decode  │                     │ reads  │
                          │  │  • NUT translation  │              ┌──────▼──────┐ │
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
