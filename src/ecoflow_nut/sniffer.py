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


class FieldWatch:
    """Watches how each decoded field moves across a capture.

    A lifetime odometer is the one field kind a single frame cannot identify:
    ``ac_chg_power = 4642`` looks identical whether it counts watt-hours taken
    from the wall or watt-hours pushed into the pack, and getting that wrong
    sends a number straight into someone's energy accounting. What tells them
    apart is watching it climb against a load you can measure -- which is what
    this records: the rise of every field that only ever rises, and the mean of
    every field that does not, so the two can be read side by side.

    Ten minutes at a steady load is enough to be sure. Rates are per hour, so a
    counter of watt-hours reports its rise directly in watts.
    """

    def __init__(self) -> None:
        # (frame key, field) -> stats, because a name can occur in two layouts.
        self._seen: dict[tuple[FrameKey, str], dict[str, Any]] = {}

    def observe(self, key: FrameKey, fields: dict[str, Any], at: float) -> None:
        for name, value in fields.items():
            # Bools are ints in Python and are flags here, not measurements.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            stat = self._seen.get((key, name))
            if stat is None:
                self._seen[(key, name)] = {
                    "first": value, "last": value, "at": at, "last_at": at,
                    "sum": float(value), "n": 1, "rising": True,
                }
                continue
            if value < stat["last"]:
                stat["rising"] = False
            stat["last"] = value
            stat["last_at"] = at
            stat["sum"] += float(value)
            stat["n"] += 1

    def counters(self) -> list[dict[str, Any]]:
        """Fields that only rose, and by how much per hour. Biggest rise first."""
        out = []
        for (key, name), stat in self._seen.items():
            span = stat["last_at"] - stat["at"]
            rise = stat["last"] - stat["first"]
            if not stat["rising"] or rise <= 0 or span <= 0:
                continue
            out.append({
                "key": key, "field": name, "first": stat["first"],
                "last": stat["last"], "rise": rise, "seconds": span,
                "per_hour": rise * 3600.0 / span,
            })
        return sorted(out, key=lambda r: -r["rise"])

    def means(self, match: str) -> list[dict[str, Any]]:
        """Mean of every field whose name contains ``match``, for comparison."""
        out = [
            {"key": key, "field": name, "mean": stat["sum"] / stat["n"],
             "samples": stat["n"]}
            for (key, name), stat in self._seen.items()
            if match in name
        ]
        return sorted(out, key=lambda r: r["field"])

    def stuck_at_zero(self) -> list[tuple[FrameKey, str]]:
        """Fields that never left zero -- the shape of a field this firmware
        does not populate, which is worth knowing before trusting one."""
        return sorted(
            (key, name)
            for (key, name), stat in self._seen.items()
            if stat["first"] == 0 and stat["last"] == 0 and stat["n"] > 1
        )
