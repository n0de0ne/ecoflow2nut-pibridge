# DELTA 3 BLE protocol notes & verification status

This documents what is implemented, what is verified, and the known gaps, so a
maintainer with hardware can close them efficiently.

## Transport

* GATT characteristics (tried in order): the EcoFlow "rfcomm" pair
  `00000002…` (write) / `00000003…` (notify), then the Nordic UART pair.
* Frames are EcoFlow **V3** (`0xAA 0x13 …`): header CRC8 over the first 4 bytes,
  trailing CRC16/ARC for non-sentinel frames, payload XOR-deobfuscated with
  `seq[0]`. Implemented in `protocol.Packet`.
* Telemetry arrives as `DisplayPropertyUpload` protobuf in a frame with
  `src=0x02, cmd_set=0xFE, cmd_id=0x15`.
* Control is a `ConfigWrite` protobuf in `Packet(src=0x20, dst=0x02,
  cmd_set=0xFE, cmd_id=0x11, version=0x13)`.

## Decoded fields (read) — VERIFIED

These protobuf field numbers (`pd335_sys.proto`) are decoded and the read path is
unit-tested against **real captured frames** (`tests/data/real_frames.txt`),
yielding SoC = 75 %, AC-in = 46.3 W, etc.

| Field | # | Meaning |
|-------|---|---------|
| `cms_batt_soc` | 262 | State of charge (%) — primary |
| `bms_batt_soc` | 242 | SoC fallback |
| `pow_get_ac_in` | 54 | AC input watts |
| `pow_get_ac_out` | 368 | AC output watts (reported negative; abs() taken) |
| `pow_get_qcusb1` / `pow_get_qcusb2` | 9 / 10 | USB-A watts per port (negative; abs() taken, both summed) |
| `pow_get_typec1` / `pow_get_typec2` | 11 / 12 | USB-C watts per port (negative; abs() taken, both summed) |
| `pow_in_sum_w` | 3 | Total input watts |
| `pow_out_sum_w` | 4 | Total output watts |
| `plug_in_info_ac_charger_flag` | 202 | AC charger connected (AC-input-present) |
| `flow_info_ac_out` | 367 | AC output flow state — a bitmask, see below |
| `flow_info_qcusb1/2`, `flow_info_typec1/2` | 13-16 | USB port flow state (one master switch drives all four) |
| `flow_info_12v` | 33 | 12V DC output flow state |
| `pow_get_12v` | 37 | 12V DC output watts |
| `cms_chg_rem_time` / `cms_dsg_rem_time` | 269 / 268 | Remaining minutes |
| `errcode` | 1 | Error code |

## Commands (write) — field numbers verified, hardware-untested

`ConfigWrite` bool fields (`pd335_sys.proto`):

| Function | Field | # |
|----------|-------|---|
| `set_ac_enabled` | `cfg_ac_out_open` | 76 |
| `set_usb_enabled` | `cfg_usb_open` | 19 |
| `set_dc_enabled` | `cfg_dc_12v_out_open` | 18 |

The packet build/parse round-trips in tests, but the device's acceptance of
these has **not** been confirmed on a real DELTA 3.

## Inspecting what the device actually sends

The bridge decodes 13 fields; a real frame carries about **110**. Everything else
is silently discarded by `delta3.merge_display_payload`, which makes "the value
is missing" hard to tell apart from "the value is in a field we ignore".

```bash
sudo systemctl stop ecoflow-nut-bridge     # the device allows one BLE link
ecoflow-nut --config /etc/ecoflow-nut/config.yaml read --raw
ecoflow-nut --config /etc/ecoflow-nut/config.yaml read --raw --watch
```

`--raw` prints every field: number, wire type, value, and the name we know it by
(blank = undecoded). `--watch` keeps going and marks `+` first sight, `*` changed
value, `?` undecoded and in a range worth suspecting — change something on the
unit and whichever number moves is the field you are looking for.

Frames are **partial**: each carries only what changed, which is why `--watch`
accumulates rather than diffing against the previous frame alone.

## Gaps / TODO

### `flow_info_*` is a bitmask, not a boolean

Only the low two bits carry the port state: `0b10` and `0b11` mean on, anything
else means off (`delta3.flow_is_on`). This matches the check the EcoFlow app
makes and ha-ef-ble's `flow_is_on`.

It matters. A real DELTA 3 reports **14** (`0b1110`) for an active port and
**4** (`0b0100`) for an inactive one, so `bool(value)` — which this code used
for `flow_info_ac_out` until the values were actually inspected — reads an
inactive-but-present port as ON. Observed on one device in a single frame:

| field | value | state |
|---|---|---|
| `flow_info_qcusb1/2`, `flow_info_typec1/2` (13-16) | 14 | on |
| `flow_info_ac_in` (47) | 15 | on |
| `flow_info_ac_out` (367) | 14 | on |
| `flow_info_12v` (33) | 4 | **off** |
| `flow_info_pv` (360), `flow_info_pv2` (414) | 8 | **off** |
| `flow_info_dcp_in/out` (423/424) | 0 | off |

Field numbers here were read out of `pd335_sys_pb2.py`'s serialized descriptor
in ha-ef-ble rather than guessed; every constant this project already used was
confirmed against it at the same time.

1. **Auth (`encrypt_type 7`) is hardware-untested.** The ECDH handshake, session
   key derivation (`gen_session_key` + vendored `keydata.py`) and `EncPacket`
   framing mirror `ha-ef-ble` but cannot be validated here without the device.
   The `encrypt_type 0/1` paths are simpler and self-contained. Requires
   `ecoflow.user_id`.
2. **`set_dc_enabled`** targets the 12V DC port (`cfg_dc_12v_out_open`). If a
   given DELTA 3 variant exposes DC differently, log the raw `ConfigWrite` ack
   and adjust the field number. No separate ack parsing is implemented yet —
   commands are fire-and-forget.
3. **`flow_info_ac_out` (367)** is not present in every frame; `ac_output_on`
   stays last-known until a frame includes it. `ups.status` does not currently
   depend on it (it uses AC-input-present + SoC), so this is informational only.
4. **Auto-shutdown / auto-cut** cuts AC output (`set_ac_enabled(false)`) on
   critical battery; it reuses the same `ConfigWrite` command path, so it shares
   the "field numbers verified, hardware-untested" caveat above. Disabled by
   default (`auto_shutdown.enabled`).
