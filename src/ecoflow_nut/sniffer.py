"""Frame-level diagnostics for identifying and verifying an unknown device.

The bridge only decodes the messages it recognises; this module describes
*every* frame a device sends, so a model whose layouts are unconfirmed can be
checked against real hardware. It is the tool for answering "does this unit
actually speak the protocol we assumed, and are the field offsets right?".

Nothing here is on the daemon's hot path -- it exists for ``ecoflow-nut sniff``.
"""

from __future__ import annotations

from typing import Any

from . import protocol, rawstruct
from .delta2 import Delta2Driver, layouts_for
from .protocol import Packet

# A frame key: the (src, cmd_set, cmd_id) triple that identifies a message type.
FrameKey = tuple[int, int, int]


def frame_key(packet: Packet) -> FrameKey:
    return (packet.src, packet.cmd_set, packet.cmd_id)


def format_key(key: FrameKey) -> str:
    src, cmd_set, cmd_id = key
    return f"src=0x{src:02x} cmd_set=0x{cmd_set:02x} cmd_id=0x{cmd_id:02x}"


def _jsonable(value: Any) -> Any:
    """Make a decoded field safe for JSON output (raw bytes become hex)."""
    if isinstance(value, bytes):
        return value.hex()
    return value


def describe_packet(driver: Any, packet: Packet) -> dict[str, Any]:
    """Describe one frame: addressing, payload, and a best-effort decode.

    The decode is attempted with whatever the configured ``driver`` implies --
    the DELTA 2 generation's fixed-width layouts, or a protobuf parse for the
    DELTA 3 generation. An unrecognised frame still yields its addressing and
    payload hex, which is what makes the capture useful for reverse engineering.
    """
    info: dict[str, Any] = {
        "src": f"0x{packet.src:02x}",
        "dst": f"0x{packet.dst:02x}",
        "cmd_set": f"0x{packet.cmd_set:02x}",
        "cmd_id": f"0x{packet.cmd_id:02x}",
        "version": f"0x{packet.version:02x}",
        "payload_len": len(packet.payload),
        "payload_hex": packet.payload.hex(),
    }

    if isinstance(driver, Delta2Driver):
        known = layouts_for(driver).get((packet.src, packet.cmd_id))
        if known is not None:
            label, layout = known
            fields = rawstruct.unpack(layout, packet.payload)
            info["message"] = label
            info["expected_len"] = rawstruct.size_of(layout)
            info["decoded_fields"] = len(fields)
            info["fields"] = {k: _jsonable(v) for k, v in fields.items()}
        return info

    # DELTA 3 generation: everything interesting is a protobuf message.
    try:
        tagged = protocol.decode_message(packet.payload)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never raise
        info["decode_error"] = str(exc)
        return info
    info["protobuf"] = {str(k): _jsonable(v) for k, v in tagged.items()}
    return info


def layout_coverage(driver: Any, packet: Packet) -> str | None:
    """Explain how a payload's length compares with the layout we assumed.

    A mismatch is the single most useful signal when validating a model we have
    not seen: a payload *shorter* than expected means the tail fields are absent
    (older firmware, or a different variant), while a *longer* one means the
    firmware appends fields we have no names for. Neither is fatal -- decoding
    stops at the boundary -- but both are worth reporting.
    """
    if not isinstance(driver, Delta2Driver):
        return None
    known = layouts_for(driver).get((packet.src, packet.cmd_id))
    if known is None:
        return None
    label, layout = known
    expected = rawstruct.size_of(layout)
    actual = len(packet.payload)
    if actual == expected:
        return None
    if actual < expected:
        missing = len(layout) - len(rawstruct.unpack(layout, packet.payload))
        return (
            f"{label}: payload {actual} B is shorter than the {expected} B layout; "
            f"the last {missing} field(s) are unavailable"
        )
    return (
        f"{label}: payload {actual} B is longer than the {expected} B layout; "
        f"{actual - expected} trailing byte(s) are not mapped"
    )
