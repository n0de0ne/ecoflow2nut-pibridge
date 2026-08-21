"""Decoder for EcoFlow's fixed-width binary heartbeat messages.

The DELTA 2 generation (DELTA 2, DELTA 2 Max, DELTA Max, DELTA Pro, RIVER 2)
predates the protobuf ``DisplayPropertyUpload`` used by the DELTA 3 family: its
telemetry is a packed, little-endian C struct per subsystem (PD, EMS, BMS, INV,
MPPT), each field at a fixed offset with no tags and no alignment padding.

Firmware revisions extend these structs by *appending* fields, so a payload can
be shorter than the layout we know (older firmware, or a variant that omits the
tail) or longer (newer firmware with fields we have no name for). Decoding
therefore stops at the first field that would run past the end of the buffer and
ignores any trailing bytes, which makes a partially-understood layout still
yield every field before the unknown part.
"""

from __future__ import annotations

import struct
from typing import Any

# A layout is an ordered sequence of (field name, struct format character).
# Formats are the single-value subset of the ``struct`` language: "B"/"H"/"I"
# for unsigned 8/16/32-bit, "b"/"h"/"i" signed, "f" float, "<n>s" raw bytes.
Layout = tuple[tuple[str, str], ...]


def size_of(layout: Layout) -> int:
    """Total packed size in bytes of a fully-populated ``layout``."""
    return struct.calcsize("<" + "".join(fmt for _, fmt in layout))


def unpack(layout: Layout, data: bytes) -> dict[str, Any]:
    """Decode ``data`` into ``{field_name: value}`` following ``layout``.

    Fields are read in order until one does not fit; the rest are simply absent
    from the result rather than raising, so callers must treat every key as
    optional.
    """
    values: dict[str, Any] = {}
    offset = 0
    total = len(data)
    for name, fmt in layout:
        spec = "<" + fmt
        width = struct.calcsize(spec)
        if offset + width > total:
            break
        (values[name],) = struct.unpack_from(spec, data, offset)
        offset += width
    return values


def pack(layout: Layout, values: dict[str, Any]) -> bytes:
    """Encode ``values`` following ``layout``. Missing fields default to zero.

    Only used to build synthetic frames in the test suite; the bridge never
    writes these structs to a device.
    """
    out = bytearray()
    for name, fmt in layout:
        value = values.get(name)
        if value is None:
            value = b"" if fmt.endswith("s") else 0
        out += struct.pack("<" + fmt, value)
    return bytes(out)
