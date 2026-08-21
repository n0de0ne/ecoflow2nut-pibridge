"""EcoFlow BLE wire protocol: CRC, V2/V3 packet framing and a minimal protobuf codec.

This is a clean-room implementation of the framing used by modern EcoFlow power
stations (the ``pd335`` / DELTA 3 family). The protocol details mirror what the
community reverse-engineering projects discovered -- primarily
https://github.com/rabits/ha-ef-ble and https://github.com/vwt12eh8/hassio-ecoflow
-- but the code here is our own.

A frame on the wire looks like::

    aa 13 <len:2> <crc8> 0d <seq:4> 00 00 <src> <dst> <dsrc> <ddst>
       <cmd_set> <cmd_id> <payload...> [crc16:2]

* byte 0     : ``0xAA`` prefix
* byte 1     : version. Low nibble is the structural version (2 or 3); bit 0x10
               marks the "sentinel" variant (0x13) which carries no trailing
               CRC16 and may end the payload with ``0xBBBB``.
* bytes 2-3  : payload length, little-endian
* byte 4     : CRC8 of bytes 0-3 (header)
* byte 5     : product magic (``0x0d``)
* bytes 6-9  : sequence. When ``seq[0] != 0`` and the device obfuscates, the
               payload is XOR'd byte-wise with ``seq[0]``.
* bytes 10-11: static zeroes
* byte 12    : src, byte 13: dst
* (V3 only) bytes 14-15: dsrc, ddst
* cmd_set, cmd_id then payload, then (non-sentinel) CRC16 little-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, ClassVar, Final

_PREFIX: Final = 0xAA
_PRODUCT_MAGIC: Final = 0x0D


# --------------------------------------------------------------------------- #
# CRC
# --------------------------------------------------------------------------- #
def _build_crc8_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return table


def _build_crc16_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC8_TABLE: Final = _build_crc8_table()
_CRC16_TABLE: Final = _build_crc16_table()


def crc8(data: bytes) -> int:
    """CRC-8 (poly 0x07, init 0x00) as used by EcoFlow frame headers."""
    crc = 0
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc


def crc16(data: bytes) -> int:
    """CRC-16/ARC (poly 0xA001 reflected, init 0x0000) as used by EcoFlow frames."""
    crc = 0
    for byte in data:
        crc = _CRC16_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFF


# --------------------------------------------------------------------------- #
# Packet
# --------------------------------------------------------------------------- #
class PacketError(ValueError):
    """Raised when a byte stream cannot be parsed into a :class:`Packet`."""


@dataclass(slots=True)
class Packet:
    """A decoded EcoFlow application packet (V2/V3)."""

    src: int
    dst: int
    cmd_set: int
    cmd_id: int
    payload: bytes = b""
    dsrc: int = 1
    ddst: int = 1
    version: int = 3
    seq: bytes = b"\x00\x00\x00\x00"

    @classmethod
    def from_bytes(cls, data: bytes, *, xor_payload: bool = False) -> Packet:
        """Decode a single complete frame. Raises :class:`PacketError` on failure."""
        if len(data) < 5 or data[0] != _PREFIX:
            raise PacketError(f"bad prefix: {data[:8].hex()}")

        version_byte = data[1]
        version = version_byte & 0x0F
        sentinel = bool(version_byte & 0x10)

        min_len = 18 if version == 2 else 20
        if len(data) < min_len:
            raise PacketError(f"frame too small ({len(data)} bytes)")

        if crc8(data[:4]) != data[4]:
            raise PacketError("header CRC8 mismatch")

        payload_length = struct.unpack_from("<H", data, 2)[0]

        if version in (2, 3) and not sentinel:
            if crc16(data[:-2]) != struct.unpack_from("<H", data, len(data) - 2)[0]:
                raise PacketError("frame CRC16 mismatch")

        seq = data[6:10]
        src = data[12]
        dst = data[13]

        if version == 2:
            payload_start = 16
            dsrc = ddst = 0
            cmd_set, cmd_id = data[14], data[15]
        else:
            payload_start = 18
            dsrc, ddst, cmd_set, cmd_id = data[14], data[15], data[16], data[17]

        payload = b""
        if payload_length > 0:
            payload = data[payload_start : payload_start + payload_length]
            if xor_payload and seq[0] != 0:
                payload = bytes(b ^ seq[0] for b in payload)
            if sentinel and payload[-2:] == b"\xbb\xbb":
                payload = payload[:-2]

        return cls(
            src=src,
            dst=dst,
            cmd_set=cmd_set,
            cmd_id=cmd_id,
            payload=payload,
            dsrc=dsrc,
            ddst=ddst,
            version=version_byte,
            seq=seq,
        )

    def to_bytes(self) -> bytes:
        """Serialize to a complete V2/V3 frame (header CRC8 + trailing CRC16)."""
        version = self.version & 0x0F
        header = bytes([_PREFIX, self.version]) + struct.pack("<H", len(self.payload))
        header += bytes([crc8(header)])

        body = bytes([_PRODUCT_MAGIC]) + self.seq + b"\x00\x00"
        body += bytes([self.src, self.dst])
        if version >= 3:
            body += bytes([self.dsrc, self.ddst])
        body += bytes([self.cmd_set, self.cmd_id])
        body += self.payload

        frame = header + body
        frame += struct.pack("<H", crc16(frame))
        return frame


V4_VERSION: Final = 0x04


@dataclass(slots=True)
class PacketV4:
    """A decoded EcoFlow V4 application packet.

    V4 is a distinct wire format used by EcoFlow's newest devices -- not a
    variant of V2/V3. The addressing fields live in an 8-byte *inner* command
    header that is itself obfuscated, so the frame cannot be read with the
    V2/V3 parser at all::

        [0]     0xAA prefix
        [1]     version (0x04)
        [2:4]   payload length LE -- counts the inner header too
        [4]     CRC8 of bytes 0..3, and the XOR key for everything from [8]
        [5]     enc_type[7:5] | check_type[4:2] | is_rw_cmd[1] | is_ack[0]
        [6]     v4_type_a
        [7]     v4_type_b -- a second XOR key for the payload when non-zero
        [8:16]  inner header: cmd_flags, frame_type, payload_type, time_snap,
                src, dst, cmd_set, cmd_id   (XOR'd with the CRC8 byte)
        [16:]   payload (XOR'd with the CRC8 byte, then with v4_type_b)
        [-2:]   CRC16 LE over the obfuscated bytes

    Note this is only used to *read* frames. Outgoing packets keep the V2/V3
    serialisation even when the configured version is 4, matching the reference
    implementation: the handshake stage is not V4-framed.
    """

    src: int
    dst: int
    cmd_set: int
    cmd_id: int
    payload: bytes = b""
    enc_type: int = 0
    check_type: int = 0
    is_rw_cmd: bool = False
    is_ack: bool = False
    frame_type: int = 0
    payload_type: int = 0
    cmd_flags: int = 0x20
    v4_type_a: int = 0
    v4_type_b: int = 0
    time_snap_b0: int = 0

    #: Bytes before the obfuscated inner header. ClassVar, not a field: a
    #: slots dataclass would otherwise turn these into per-instance values.
    HEADER_LEN: ClassVar[int] = 8
    #: Size of the inner command header, counted inside ``payload length``.
    INNER_LEN: ClassVar[int] = 8

    @classmethod
    def from_bytes(cls, data: bytes) -> PacketV4:
        """Decode a single complete V4 frame. Raises :class:`PacketError`."""
        if len(data) < 18 or data[0] != _PREFIX:
            raise PacketError(f"bad V4 frame: {data[:8].hex()}")

        payload_length = struct.unpack_from("<H", data, 2)[0]
        if len(data) != cls.HEADER_LEN + payload_length + 2:
            raise PacketError(
                f"V4 length mismatch: got {len(data)}, "
                f"expected {cls.HEADER_LEN + payload_length + 2}"
            )
        if payload_length < cls.INNER_LEN:
            raise PacketError(f"V4 payload too short: {payload_length}")
        if crc8(data[:4]) != data[4]:
            raise PacketError("V4 header CRC8 mismatch")
        if crc16(data[:-2]) != struct.unpack_from("<H", data, len(data) - 2)[0]:
            raise PacketError("V4 frame CRC16 mismatch")

        type_byte = data[5]
        v4_type_b = data[7]

        # The header CRC8 doubles as the obfuscation key for everything after it.
        xor_key = data[4]
        body = bytes(b ^ xor_key for b in data[8 : 8 + payload_length])

        payload = body[cls.INNER_LEN :]
        if v4_type_b:
            # A second obfuscation layer, applied to the application payload only.
            payload = bytes(b ^ v4_type_b for b in payload)

        return cls(
            src=body[4],
            dst=body[5],
            cmd_set=body[6],
            cmd_id=body[7],
            payload=payload,
            enc_type=(type_byte >> 5) & 0x7,
            check_type=(type_byte >> 2) & 0x7,
            is_rw_cmd=bool((type_byte >> 1) & 0x1),
            is_ack=bool(type_byte & 0x1),
            cmd_flags=body[0],
            frame_type=body[1],
            payload_type=body[2],
            time_snap_b0=body[3],
            v4_type_a=data[6],
            v4_type_b=v4_type_b,
        )

    def to_bytes(self) -> bytes:
        """Serialize to a complete V4 frame."""
        inner = bytes(
            [
                self.cmd_flags,
                self.frame_type,
                self.payload_type,
                self.time_snap_b0,
                self.src,
                self.dst,
                self.cmd_set,
                self.cmd_id,
            ]
        )
        type_byte = (
            ((self.enc_type & 0x7) << 5)
            | ((self.check_type & 0x7) << 2)
            | (int(self.is_rw_cmd) << 1)
            | int(self.is_ack)
        )
        header = bytes([_PREFIX, V4_VERSION]) + struct.pack(
            "<H", self.INNER_LEN + len(self.payload)
        )
        xor_key = crc8(header)
        header += bytes([xor_key, type_byte, self.v4_type_a, self.v4_type_b])

        payload = self.payload
        if self.v4_type_b:
            payload = bytes(b ^ self.v4_type_b for b in payload)
        frame = header + bytes(b ^ xor_key for b in inner + payload)
        return frame + struct.pack("<H", crc16(frame))

    def as_packet(self) -> Packet:
        """Adapt to a :class:`Packet` so model drivers need not know about V4."""
        return Packet(
            src=self.src,
            dst=self.dst,
            cmd_set=self.cmd_set,
            cmd_id=self.cmd_id,
            payload=self.payload,
            version=V4_VERSION,
        )


def frame_length(data: bytes) -> int | None:
    """Total length of the frame starting at ``data``, or None if not yet known.

    Every generation puts a length at bytes 2-3 but counts different things:
    V4 counts from the inner header, V2/V3 count the payload alone and differ
    from each other by the two dsrc/ddst bytes.
    """
    if len(data) < 4:
        return None
    length = struct.unpack_from("<H", data, 2)[0]
    version = data[1]
    if version == V4_VERSION:
        return PacketV4.HEADER_LEN + length + 2
    return (16 if (version & 0x0F) == 2 else 18) + length + 2


def parse_frame(data: bytes, *, xor_payload: bool = False) -> Packet:
    """Decode one frame of any generation into a :class:`Packet`."""
    if len(data) > 1 and data[1] == V4_VERSION:
        return PacketV4.from_bytes(data).as_packet()
    return Packet.from_bytes(data, xor_payload=xor_payload)


# --------------------------------------------------------------------------- #
# Minimal protobuf codec
# --------------------------------------------------------------------------- #
# Only the slice of the protobuf wire format that the DisplayPropertyUpload and
# ConfigWrite messages need: varint, 32-bit and 64-bit fixed, length-delimited.
WIRE_VARINT: Final = 0
WIRE_I64: Final = 1
WIRE_LEN: Final = 2
WIRE_I32: Final = 5


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_fields(buf: bytes) -> list[tuple[int, int, Any]]:
    """Decode a message to ``[(field_number, wire_type, value), ...]``, in order.

    Like :func:`decode_message` but keeps the wire type and every occurrence of a
    repeated field. Used by the ``read --raw`` diagnostic: when hunting for an
    undecoded field, knowing whether it arrived as a varint or a float is half
    the information.
    """
    out: list[tuple[int, int, Any]] = []
    pos = 0
    length = len(buf)
    while pos < length:
        tag, pos = _read_varint(buf, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07
        value: Any
        if wire_type == WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire_type == WIRE_I32:
            value = struct.unpack_from("<f", buf, pos)[0]
            pos += 4
        elif wire_type == WIRE_I64:
            value = struct.unpack_from("<d", buf, pos)[0]
            pos += 8
        elif wire_type == WIRE_LEN:
            size, pos = _read_varint(buf, pos)
            value = buf[pos : pos + size]
            pos += size
        else:  # pragma: no cover - groups are not used by these messages
            raise PacketError(f"unsupported wire type {wire_type} at offset {pos}")
        out.append((field_number, wire_type, value))
    return out


def decode_message(buf: bytes) -> dict[int, Any]:
    """Decode the top-level fields of a protobuf message.

    Returns ``{field_number: value}``. ``i32`` fields are returned as 32-bit
    floats (every fixed32 field we care about is a float); ``i64`` as doubles;
    varints as ints; length-delimited as raw ``bytes``. If a field appears more
    than once the last occurrence wins.
    """
    out: dict[int, Any] = {}
    pos = 0
    length = len(buf)
    while pos < length:
        tag, pos = _read_varint(buf, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07
        value: Any
        if wire_type == WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire_type == WIRE_I32:
            value = struct.unpack_from("<f", buf, pos)[0]
            pos += 4
        elif wire_type == WIRE_I64:
            value = struct.unpack_from("<d", buf, pos)[0]
            pos += 8
        elif wire_type == WIRE_LEN:
            size, pos = _read_varint(buf, pos)
            value = buf[pos : pos + size]
            pos += size
        else:  # pragma: no cover - groups are not used by these messages
            raise PacketError(f"unsupported wire type {wire_type} at offset {pos}")
        out[field_number] = value
    return out


@dataclass(slots=True)
class ProtoField:
    """A single field to encode into a protobuf message."""

    number: int
    wire_type: int
    value: Any


def encode_message(fields: list[ProtoField]) -> bytes:
    """Encode a small protobuf message from explicit field descriptors."""
    out = bytearray()
    for f in fields:
        out += encode_varint((f.number << 3) | f.wire_type)
        if f.wire_type == WIRE_VARINT:
            out += encode_varint(int(f.value))
        elif f.wire_type == WIRE_I32:
            out += struct.pack("<f", float(f.value))
        elif f.wire_type == WIRE_I64:
            out += struct.pack("<d", float(f.value))
        elif f.wire_type == WIRE_LEN:
            raw = bytes(f.value)
            out += encode_varint(len(raw)) + raw
        else:  # pragma: no cover
            raise PacketError(f"unsupported wire type {f.wire_type}")
    return bytes(out)


def encode_bool_field(number: int, value: bool) -> bytes:
    """Convenience: encode a single ``bool`` (varint) field as a message."""
    return encode_message([ProtoField(number, WIRE_VARINT, 1 if value else 0)])
