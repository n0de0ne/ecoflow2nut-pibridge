# EcoFlow BLE protocol notes & verification status

This documents what is implemented, what is verified, and the known gaps, so a
maintainer with hardware can close them efficiently.

EcoFlow ships **two incompatible protocol generations**, and this bridge speaks
both. Which one is used is chosen by `ecoflow.model` in `config.yaml` — see
[Choosing a driver](#choosing-a-driver).

|                | DELTA 2 generation | DELTA 3 generation |
|----------------|--------------------|--------------------|
| Models | DELTA 2, **DELTA 2 Max**, DELTA 2 Black, DELTA 3 1500 | DELTA 3, DELTA 3 Plus, River 3 |
| Serial prefix | `R331`/`R335`, `R351`/`R354`, `R701`, `D361`/`D365` | `P231` |
| BLE name | `EF-R33…`, `EF-R35…` | `EF-D3…` |
| Frame version | **V2** (payload at byte 16) | **V3** (payload at byte 18) |
| Telemetry | packed little-endian structs, one per subsystem | protobuf `DisplayPropertyUpload` |
| Control | per-function `cmd_id`, small raw payloads | one `ConfigWrite` protobuf |
| Module | `delta2.py` | `delta3.py` |

## Choosing a driver

`ecoflow.model` selects the driver. Spelling is normalised (`"DELTA 2 Max"`,
`"delta-2-max"`, `"delta2max"` and the serial prefix `"R351"` all resolve to the
same driver), and the supported names are listed in `devices.py`.

Selection is deliberately **by configuration, not by serial-number prefix**.
Upstream integrations gate on a hardcoded prefix list, so a regional variant or
a later hardware revision is rejected as "unsupported" even when it speaks a
protocol they already implement. Naming the generation in config means an
unrecognised serial cannot lock you out.

## Transport (both generations)

* GATT characteristics (tried in order): the EcoFlow "rfcomm" pair
  `00000002…` (write) / `00000003…` (notify), then the Nordic UART pair.
* Header CRC8 over the first 4 bytes; trailing CRC16/ARC for non-sentinel
  frames; payload XOR-deobfuscated with `seq[0]`. Implemented in
  `protocol.Packet`, which handles V2 and V3.
* Authentication is `md5(user_id + serial)` uppercase-hex, sent as `cmd_set
  0x35 / cmd_id 0x86` after a `0x89` status request. **These packets must use
  the frame version the model speaks** — V2 for the DELTA 2 generation, V3 for
  the DELTA 3 — which is why the driver carries `packet_version`.
* Reassembly is length-driven, so it must know the header size for the frame's
  version (16 vs 18 bytes); see `PassthroughAssembler`.

> ### Payload obfuscation is per *model*, not per generation
> Some devices XOR the payload byte-wise with `seq[0]`. This is **not** a
> property of the generation: the DELTA 2 obfuscates and the DELTA 2 Max does
> not; River 3 does and (per the reference implementation) the DELTA 3 does not,
> though the real captured frames in `tests/data/` only decode correctly *with*
> it, so `delta3` keeps it on here.
>
> It only takes effect on frames whose `seq[0]` is non-zero, so a wrong setting
> is completely invisible on some devices and produces total garbage on others.
> The driver carries it as `xor_payload`. **If `sniff` shows a recognised
> message whose values are nonsense, flip this flag first** — it is the single
> most likely thing to be wrong on an unverified model.

---

## DELTA 2 generation (`delta2.py`)

### Telemetry — layouts verified against reference, hardware-untested

Telemetry is **not** protobuf: each subsystem pushes a fixed-width, packed
little-endian struct with no tags, identified by the frame's `(src, cmd_set,
cmd_id)`. Decoding is `rawstruct.unpack`, which stops at the first field that
does not fit — so a firmware revision that omits the tail still decodes
everything before it, and one that appends unknown fields is simply truncated.

| Frame `(src, cmd_set, cmd_id)` | Message | Feeds |
|--------------------------------|---------|-------|
| `0x02, 0x20, 0x02` | PD heartbeat | total in/out watts, USB-A/C watts, coarse SoC |
| `0x03, 0x20, 0x02` | EMS heartbeat | **SoC** (`f32_lcd_show_soc`), charge/discharge remaining |
| `0x03, 0x20, 0x32` | BMS heartbeat (main pack) | SoC fallback |
| `0x06/0x07, 0x20, 0x32` | BMS heartbeat (extra batteries) | ignored for SoC |
| `0x04, *, 0x02` | INV heartbeat | **AC in/out watts, AC input voltage, AC on/off** |
| `0x05, 0x20, 0x02` | MPPT heartbeat | solar / 12 V (not used by the UPS view) |

The struct layouts in `delta2.py` were transcribed from the `ha-ef-ble` model
definitions and **verified programmatically**: every layout's `struct` format
string and field ordering matches the reference byte-for-byte, and
`tests/test_delta2.py` pins the resulting sizes (PD Max 137 B, PD 147 B, EMS
46 B, INV 67 B, BMS 69 B) so an edit cannot silently shift an offset.

Every field the UPS view needs comes from the **EMS, BMS and INV** messages,
whose layouts are shared across the whole generation. Only the PD and MPPT tails
are model-specific, so an unlisted sibling still produces a working UPS.

### SoC precedence

A device reports SoC from up to three subsystems at different resolutions. They
are ranked (`state.SOC_PRIORITY`) so a coarse source never clobbers a precise
one, while a lesser source still keeps updating if the better one is silent:
`ems` (LCD float) > `bms` (pack float) > `pd` (integer byte).

### Commands — opcodes verified against reference, hardware-untested

All are V2 frames from `src 0x21`, `cmd_set 0x20`:

| Function | `dst` | `cmd_id` | Payload |
|----------|-------|----------|---------|
| AC output | `0x04` (DELTA 2 Max) / `0x05` (DELTA 2) | `0x42` | `[on] FF FF FF FF FF FF` |
| USB output | `0x02` | `0x22` | `[on]` |
| 12 V DC output | `0x05` | `0x51` | `[on]` |

The trailing `0xFF` bytes on the AC command are "leave unchanged" placeholders
for the X-Boost / output voltage / frequency settings that opcode also carries.

> **The AC relay lives on a different subsystem per model** — the inverter on a
> DELTA 2 Max, the MPPT board on a DELTA 2. Sending to the wrong `dst` is
> silently ignored, so this is the first thing to check if AC control does
> nothing.

### AC-present detection

The DELTA 2 generation reports **measured mains voltage** (`inv.ac_in_vol`, in
millivolts). That is the authoritative "grid is up" signal and `nut_writer` uses
it alone where available: unlike input watts it stays at nominal on a full
battery with an idle load, where a watts-only rule would report a false outage
and start shutting NUT clients down. `input.voltage` is published as the real
measurement rather than a configured constant.

---

## DELTA 3 generation (`delta3.py`)

### Decoded fields (read) — VERIFIED

These protobuf field numbers (`pd335_sys.proto`) are decoded and the read path is
unit-tested against **real captured frames** (`tests/data/real_frames.txt`),
yielding SoC = 75 %, AC-in = 46.3 W, etc.

Telemetry arrives as `DisplayPropertyUpload` in a frame with
`src=0x02, cmd_set=0xFE, cmd_id=0x15`.

| Field | # | Meaning |
|-------|---|---------|
| `cms_batt_soc` | 262 | State of charge (%) — primary |
| `bms_batt_soc` | 242 | SoC fallback |
| `pow_get_ac_in` | 54 | AC input watts |
| `pow_get_ac_out` | 368 | AC output watts (reported negative; abs() taken) |
| `pow_in_sum_w` | 3 | Total input watts |
| `pow_out_sum_w` | 4 | Total output watts |
| `plug_in_info_ac_charger_flag` | 202 | AC charger connected (AC-input-present) |
| `flow_info_ac_out` | 367 | AC output on/off |
| `cms_chg_rem_time` / `cms_dsg_rem_time` | 269 / 268 | Remaining minutes |
| `errcode` | 1 | Error code |

### Commands (write) — field numbers verified, hardware-untested

Control is a `ConfigWrite` protobuf in `Packet(src=0x20, dst=0x02,
cmd_set=0xFE, cmd_id=0x11, version=0x13)`.

| Function | `ConfigWrite` field | # |
|----------|-------|---|
| `set_ac_enabled` | `cfg_ac_out_open` | 76 |
| `set_usb_enabled` | `cfg_usb_open` | 19 |
| `set_dc_enabled` | `cfg_dc_12v_out_open` | 18 |

The packet build/parse round-trips in tests, but the device's acceptance of
these has **not** been confirmed on a real DELTA 3.

---

## When a device disconnects during authentication

Sending the wrong frame version does **not** produce a timeout: the device
accepts the BLE connection, receives an auth packet it cannot parse, and
**drops the link**. In Home Assistant's ef_ble that surfaces as
`Disconnected from device (... _async_ble_device_disconnected ...)` shortly
after "running authentication procedure"; raising the connection timeout
cannot help, because nothing is being waited for.

`ecoflow-nut probe` sweeps the candidate versions and reports which one keeps
the link up and yields frames; put the winner in `ecoflow.packet_version`.

Distinguishing the two failure modes matters:

| Symptom | Meaning |
|---------|---------|
| Connects, then **disconnected** by the device | Wrong frame version for this model |
| Connects, stays up, but **silent** | Version plausible; check `ecoflow.serial` and `ecoflow.user_id` — auth hashes both |
| Frames arrive but values are nonsense | Right version, wrong `xor_payload` or wrong layout |

### EcoFlow E2000 — CONFIRMED as the DELTA 2 Max protocol

The **EcoFlow E2000** (2048 Wh, 2400 W; `EFE2000-EU-CBOX`, EU/UK) speaks the
**DELTA 2 Max protocol**, verified against hardware:

* it authenticates with **V2** framing (v3 and v4 do not);
* it streams the DELTA 2 Max subsystem set — PD, EMS, BMS, INV, MPPT and the
  extra-battery kit frame — at exactly the `(src, cmd_set, cmd_id)` triples in
  the table above;
* its **PD heartbeat is 137 bytes, exactly `PD_DELTA2_MAX`'s size** (the
  DELTA 2's is 147), which is the single strongest identification signal.

Its serial prefix is **`E201`** (e.g. `E201ZE1APH560861`) but it advertises as
**`EF-R35` + serial tail** — the DELTA 2 Max name prefix — and the full serial
appears as ASCII in the advertisement's manufacturer data. So **only the serial
prefix is new**: everything else is a DELTA 2 Max.

That is exactly why prefix-whitelisting integrations reject it as an
"unsupported device" while a protocol they already implement works fine. It is
the concrete case for selecting a driver by configuration, not by serial.

`ecoflow.model: e2000` (or `E201`, or `delta2max`) selects the driver;
`packet_version` needs no override since `delta2max` already defaults to V2.

> **Observed firmware drift.** Several heartbeats are *longer* than the layouts
> we know: EMS 55 B (layout 46), INV 72 B (67), BMS 192 B (69). `rawstruct`
> decodes up to the boundary and ignores the rest, so appended fields are
> harmless. It would only break if the firmware *inserted* a field rather than
> appending — which would shift every later offset, and shows up immediately as
> implausible values in `sniff`.

### V4 framing

EcoFlow's newest devices use a **V4** frame, a different layout rather than a
V2/V3 variant: the addressing lives in an 8-byte *inner* header that is itself
XOR'd with the header CRC8 byte, and the application payload is XOR'd a second
time with `v4_type_b` when that is non-zero. `protocol.PacketV4` reads and
writes it (verified byte-for-byte against the reference codec).

Only *reading* uses V4. Outgoing packets keep V2/V3 serialisation even when the
configured version is 4 — the handshake stage is not V4-framed, matching the
reference implementation.

### When every frame version behaves identically

If v2, v3 and v4 all fail the same way, **the frame version is not the
problem** — it only affects the auth packet, so a real framing mismatch would
change the symptom. Look instead at causes that are version-independent:

1. **Another BLE client holds the device.** EcoFlow stations accept exactly one
   connection at a time, and the phone app grabs it whenever it is running —
   including backgrounded on iOS. Force-quit it everywhere, or disable that
   phone's Bluetooth, before testing.
2. **Wrong `user_id`.** Authentication is `md5(user_id + serial)`; an id from a
   different EcoFlow account fails for every frame version.
3. **Wrong or unavailable serial.** An *unidentified* device is exactly the case
   where a serial derived from the advertisement may be wrong — and the auth
   hash needs the full serial, not the four characters in the BLE name.
4. **`encrypt_type` mismatch.** Anything that is not 0 or 1 is treated as the
   type-7 ECDH exchange; a device using something else fails during session-key
   negotiation, before the auth packet is ever sent.

## Bluetooth proxies are not a supported transport

This bridge talks to a **local** Bluetooth adapter through BlueZ/bleak. It does
not go through an ESPHome/ESP32 Bluetooth proxy, and that is deliberate:

* An ESP32 proxy offers **3 active connection slots** by default. EcoFlow
  stations hold a *continuous* connection, so each one occupies a slot
  permanently — a few devices exhaust the pool and nothing else can connect.
* The type-7 handshake uses **write-with-response**, which is exactly the
  operation known to stall through a proxy. The reference implementation caps
  its disconnect timeout specifically because of this ("notably through an
  ESPHome proxy"), and its own docs report dropped packets for EcoFlow devices
  over proxies.

A stall there lands *mid-authentication* and is therefore **identical at every
frame version** — a useful discriminator: if v2, v3 and v4 all behave the same
through a proxy, suspect the transport, not the protocol. Run the bridge on a
host with its own radio (a Pi) before concluding a device is unsupported.

## Identifying an unknown device on real hardware

`config/config.unknown-device.yaml` is a ready-made diagnostic config using
`model: raw` — connect and capture, decode nothing, assume nothing:

```bash
# 1. Confirm the device is advertising, and read its encrypt_type.
ecoflow-nut --config config/config.unknown-device.yaml scan

# 2. Find a frame version that authenticates and holds the link.
ecoflow-nut --config config/config.unknown-device.yaml probe

# 3. Capture whatever it sends.
ecoflow-nut --config config/config.unknown-device.yaml sniff --out frames.jsonl
```

The config sets `logging.level: DEBUG`, so the handshake is logged step by step
(`ble.found` with the encrypt_type, `ble.handshake_start`, `ecdh.step1_*`,
`ecdh.step2_*`, `ble.authenticated`). **Where the log stops is the diagnosis**:

| Last line seen | Meaning |
|----------------|---------|
| `ble.scanning` only | Device not advertising — it is asleep, or another client already holds it |
| `ble.connected`, then nothing | Handshake stalled; suspect the transport or `encrypt_type` |
| `ecdh.step1_resp` then stops | Public-key exchange rejected — `encrypt_type` is probably not 7 |
| `ecdh.step2_session_key_ok` then stops | Session established, credentials rejected — check `serial` and `user_id` |
| `ble.authenticated` | Auth succeeded; anything after is a decoding problem, not a connection one |

Remember the device accepts **one** BLE client at a time: close the EcoFlow
phone app (fully — backgrounded on iOS still holds the link) and stop any other
integration that might be connected before testing.

## Verifying a device against this implementation

`ecoflow-nut sniff` connects like the daemon does and reports **every** frame
the device sends — including ones no driver claims — with a best-effort decode
against the configured model's layouts:

```bash
ecoflow-nut scan                          # find the MAC, BLE name, encrypt_type
ecoflow-nut sniff --seconds 60            # summary of every frame kind seen
ecoflow-nut sniff --out frames.jsonl      # full capture for offline analysis
```

What to check:

1. **Frame kinds.** Do the `(src, cmd_set, cmd_id)` triples match the table
   above? If a device sends nothing recognisable, it is a different generation
   than configured — try the other `model`.
2. **Payload lengths.** `sniff` flags any payload shorter or longer than the
   layout expects. Shorter means the tail fields are unavailable; longer means
   the firmware appends fields we have no names for. Neither is fatal.
3. **Field values against the unit's screen.** SoC, AC input watts and AC output
   watts should match what the display shows. If they are plausible but wrong,
   an offset is off — compare the payload hex with the layout in `delta2.py`.

## Gaps / TODO

1. **No hardware validation of either generation's write path.** Command packets
   round-trip in tests and match the reference byte-for-byte, but no device has
   acknowledged one here. Commands are fire-and-forget; no ack parsing exists.
2. **Auth (`encrypt_type 7`) is hardware-untested.** The ECDH handshake, session
   key derivation (`gen_session_key` + vendored `keydata.py`) and `EncPacket`
   framing mirror `ha-ef-ble` but cannot be validated without the device. The
   `encrypt_type 0/1` paths are simpler and self-contained. Requires
   `ecoflow.user_id`.
3. **DELTA 2 MPPT heartbeat is decoded but unused.** Solar and 12 V readings are
   available in `delta2.MPPT_MR350`/`MPPT_MR330` but are not merged into
   `DeviceState`; the UPS view does not need them.
4. **Extra batteries are ignored.** `AllKitDetailData` (`0x03, 0x03, 0x0E`) on
   the DELTA 2 generation reports attached expansion packs. Their SoC is
   deliberately not merged (the EMS already reports the combined figure), but
   total capacity is not adjusted either — set `nut.battery_capacity_wh` to the
   real total by hand if you run expansion batteries.
5. **`flow_info_ac_out` (367)** on the DELTA 3 is not present in every frame;
   `ac_output_on` stays last-known until a frame includes it. `ups.status` does
   not depend on it, so this is informational only.
6. **Auto-shutdown / auto-cut** reuses the same command path, so it shares the
   "verified against reference, hardware-untested" caveat. Disabled by default
   (`auto_shutdown.enabled`).
