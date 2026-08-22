"""BLE transport for EcoFlow power stations using ``bleak``.

Implements the connection lifecycle, the EcoFlow session handshake (encrypt
types 0 / 1 / 7) and wire-level frame (re)assembly. Decoding the resulting
packets is the model driver's job (see :mod:`ecoflow_nut.devices`): this module
hands each frame to the configured driver, which merges it into a rolling
:class:`~ecoflow_nut.state.DeviceState`.

Both frame generations are handled here. The V2 frames used by the DELTA 2
generation are two bytes shorter in the header than the V3 frames of the DELTA 3
generation, which matters for reassembly and for the authentication packets --
those must go out in the version the device speaks.

Handshake / encryption details mirror the reverse engineering in
https://github.com/rabits/ha-ef-ble (Apache-2.0). The verified-correct read
path -- V3 framing + protobuf decode -- is exercised by the test suite against
real captured frames. The encrypted handshakes (type 1 and especially the
ECDH-based type 7) require hardware to validate end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Final

import structlog
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from . import delta3, devices, keydata, protocol
from .config import BleConfig, EcoflowConfig
from .devices import DeviceDriver
from .protocol import Packet, PacketError, crc8, crc16
from .state import DeviceState

log = structlog.get_logger(__name__)

# The two GATT characteristic UUID sets used by EcoFlow devices. The "rfcomm"
# pair covers the DELTA / River families; "nordic_uart" covers some others.
_CHARACTERISTICS: Final = {
    "rfcomm": {
        "notify": "00000003-0000-1000-8000-00805f9b34fb",
        "write": "00000002-0000-1000-8000-00805f9b34fb",
    },
    "nordic_uart": {
        "notify": "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
        "write": "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
    },
}

_ENC_PACKET_PREFIX: Final = b"\x5a\x5a"
_AUTH_DST: Final = 0x35


# --------------------------------------------------------------------------- #
# Encryption strategies
# --------------------------------------------------------------------------- #
class Encryption(ABC):
    def __init__(self, session_key: bytes, iv: bytes) -> None:
        self.session_key = session_key
        self.iv = iv

    @abstractmethod
    def encrypt(self, plaintext: bytes) -> bytes: ...

    @abstractmethod
    def decrypt(self, ciphertext: bytes) -> bytes: ...


class Type1Encryption(Encryption):
    """AES-CBC with zero padding (encrypt_type 1)."""

    def encrypt(self, plaintext: bytes) -> bytes:
        padded_len = (len(plaintext) + 15) // 16 * 16
        padded = plaintext + b"\x00" * (padded_len - len(plaintext))
        return AES.new(self.session_key, AES.MODE_CBC, self.iv).encrypt(padded)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return AES.new(self.session_key, AES.MODE_CBC, self.iv).decrypt(ciphertext)


class Type7Encryption(Encryption):
    """AES-CBC with PKCS7 padding (encrypt_type 7, ECDH session)."""

    def encrypt(self, plaintext: bytes) -> bytes:
        cipher = AES.new(self.session_key, AES.MODE_CBC, self.iv)
        return cipher.encrypt(pad(plaintext, AES.block_size))

    def decrypt(self, ciphertext: bytes) -> bytes:
        aligned = len(ciphertext) - len(ciphertext) % AES.block_size
        if aligned == 0:
            return ciphertext
        decrypted = AES.new(self.session_key, AES.MODE_CBC, self.iv).decrypt(
            ciphertext[:aligned]
        )
        try:
            return unpad(decrypted, AES.block_size)
        except ValueError:
            return decrypted


# --------------------------------------------------------------------------- #
# EncPacket wrapper (encrypt_type 7 outer frame, and unencrypted commands)
# --------------------------------------------------------------------------- #
def encode_enc_packet(
    frame_type: int, payload: bytes, enc: Encryption | None = None
) -> bytes:
    """Wrap a payload in the ``0x5A5A`` EncPacket frame used on the BLE channel."""
    if enc is not None:
        body = enc.encrypt(payload)
    else:
        body = payload
    data = _ENC_PACKET_PREFIX + bytes([frame_type << 4, 0x01])
    data += struct.pack("<H", len(body) + 2)
    data += body
    data += struct.pack("<H", crc16(data))
    return data


# Frame type constants.
_FRAME_COMMAND: Final = 0x00
_FRAME_PROTOCOL: Final = 0x01


# --------------------------------------------------------------------------- #
# Frame assemblers: turn raw notification bytes into decrypted packet payloads
# --------------------------------------------------------------------------- #
class FrameAssembler(ABC):
    write_with_response: bool = False

    def __init__(self) -> None:
        self._buffer = b""

    @abstractmethod
    def encode(self, packet: Packet) -> bytes: ...

    @abstractmethod
    def reassemble(self, data: bytes) -> list[bytes]: ...


class PassthroughAssembler(FrameAssembler):
    """encrypt_type 0: plain V2/V3 frames, no outer encryption."""

    write_with_response = False

    def encode(self, packet: Packet) -> bytes:
        return packet.to_bytes()

    def reassemble(self, data: bytes) -> list[bytes]:
        data = self._buffer + data
        self._buffer = b""
        payloads: list[bytes] = []
        while data:
            start = data.find(b"\xaa")
            if start < 0:
                data = b""
                break
            data = data[start:]
            if len(data) < 5:
                break
            if crc8(data[:4]) != data[4]:
                # Not a real header -- a 0xAA inside a payload. Resync past it.
                data = data[1:]
                continue
            # Each generation counts its length field differently.
            frame_len = protocol.frame_length(data)
            if frame_len is None or len(data) < frame_len:
                break
            payloads.append(data[:frame_len])
            data = data[frame_len:]
        self._buffer = data
        return payloads


class EncPacketAssembler(FrameAssembler):
    """encrypt_type 7: ``0x5A5A`` EncPacket wrapper with AES-CBC body."""

    write_with_response = True

    def __init__(self, encryption: Encryption) -> None:
        super().__init__()
        self._enc = encryption

    def encode(self, packet: Packet) -> bytes:
        return encode_enc_packet(_FRAME_PROTOCOL, packet.to_bytes(), self._enc)

    def reassemble(self, data: bytes) -> list[bytes]:
        data = self._buffer + data
        self._buffer = b""
        payloads: list[bytes] = []
        while data:
            start = data.find(_ENC_PACKET_PREFIX)
            if start < 0:
                data = b""
                break
            data = data[start:]
            if len(data) < 8:
                break
            payload_len = struct.unpack_from("<H", data, 4)[0]
            if payload_len > 10_000:
                data = data[2:]
                continue
            end = 6 + payload_len
            if end > len(data):
                nxt = data[2:].find(_ENC_PACKET_PREFIX)
                if nxt >= 0:
                    data = data[2 + nxt :]
                    continue
                break
            header = data[:6]
            body = data[6 : end - 2]
            frame_crc = struct.unpack_from("<H", data, end - 2)[0]
            if crc16(header + body) != frame_crc:
                data = data[2:]
                continue
            data = data[end:]
            payloads.append(self._enc.decrypt(body))
        self._buffer = data
        return payloads


class RawHeaderAssembler(FrameAssembler):
    """encrypt_type 1: 5-byte plaintext header + AES-CBC body."""

    write_with_response = False

    def __init__(self, encryption: Encryption) -> None:
        super().__init__()
        self._enc = encryption

    def encode(self, packet: Packet) -> bytes:
        raw = packet.to_bytes()
        return raw[:5] + self._enc.encrypt(raw[5:])

    def reassemble(self, data: bytes) -> list[bytes]:
        data = self._buffer + data
        self._buffer = b""
        payloads: list[bytes] = []
        while data:
            start = data.find(b"\xaa")
            if start < 0:
                data = b""
                break
            data = data[start:]
            if len(data) < 5:
                break
            if crc8(data[:4]) != data[4]:
                data = data[1:]
                continue
            payload_length = struct.unpack_from("<H", data, 2)[0]
            version = data[1] & 0x0F
            inner_overhead = 15 if version >= 3 else 13
            inner_len = inner_overhead + payload_length
            encrypted_len = (inner_len + 15) // 16 * 16
            frame_len = 5 + encrypted_len
            if len(data) < frame_len:
                break
            header = data[:5]
            body = data[5:frame_len]
            data = data[frame_len:]
            decrypted = self._enc.decrypt(body)
            payloads.append(header + decrypted[:inner_len])
        self._buffer = data
        return payloads


def gen_session_key(seed: bytes, srand: bytes) -> bytes:
    """Derive the type-7 session key from a device seed and random value.

    Mirrors the ha-ef-ble implementation: two 64-bit numbers are looked up from
    the firmware key table using ``seed``, the other two come from ``srand``,
    and the MD5 of the concatenation is the session key.
    """
    pos = seed[0] * 0x10 + ((seed[1] - 1) & 0xFF) * 0x100
    num0 = struct.unpack("<Q", keydata.get8bytes(pos))[0]
    num1 = struct.unpack("<Q", keydata.get8bytes(pos + 8))[0]
    if len(srand) >= 0x20:
        raise NotImplementedError("srand >= 32 bytes is not supported")
    num2 = struct.unpack("<Q", srand[0:8])[0]
    num3 = struct.unpack("<Q", srand[8:16])[0]
    data = struct.pack("<QQQQ", num0, num1, num2, num3)
    return hashlib.md5(data).digest()


# --------------------------------------------------------------------------- #
# Scan helper
# --------------------------------------------------------------------------- #
def parse_encrypt_type(manufacturer_data: dict[int, bytes]) -> int | None:
    """Extract the EcoFlow encrypt_type from advertisement manufacturer data.

    capability flags live in byte 22 of the EcoFlow manufacturer payload; the
    encrypt_type is bits 3-5.
    """
    for raw in manufacturer_data.values():
        if len(raw) > 22:
            return (raw[22] & 0b0111000) >> 3
    return None


def bluez_device_path(mac: str, adapter: str) -> str:
    """BlueZ's D-Bus object path for a device, e.g. /org/bluez/hci0/dev_AA_BB_..."""
    return f"/org/bluez/{adapter or 'hci0'}/dev_{mac.upper().replace(':', '_')}"


async def clear_stale_connection(mac: str, adapter: str) -> bool:
    """Drop a connection BlueZ is holding to ``mac``. Returns True if it dropped one.

    This is ``bluetoothctl disconnect <mac>``, over the same D-Bus interface
    that command uses. It exists because BlueZ keeps connections alive after the
    process that opened them dies, and an EcoFlow -- one BLE client at a time --
    stops advertising while it believes it still has a client. Nothing on the
    host can see the device again until that link is taken down.

    Best-effort by design: every failure path returns False so the caller falls
    back to reporting an ordinary "not found during scan".
    """
    try:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus
    except ImportError:  # pragma: no cover - dbus_fast ships with bleak on Linux
        return False

    path = bluez_device_path(mac, adapter)
    bus = None
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        # Only claim to have cleared something if BlueZ really was connected;
        # Disconnect succeeds either way, and a log line that cries wolf on
        # every failed scan is worse than no log line.
        connected = await bus.call(
            Message(
                destination="org.bluez",
                path=path,
                interface="org.freedesktop.DBus.Properties",
                member="Get",
                signature="ss",
                body=["org.bluez.Device1", "Connected"],
            )
        )
        if connected is None or connected.message_type is not MessageType.METHOD_RETURN:
            # No such object: BlueZ has never seen this device, so the scan
            # found nothing for the ordinary reason.
            return False
        if not connected.body[0].value:
            return False

        reply = await bus.call(
            Message(
                destination="org.bluez",
                path=path,
                interface="org.bluez.Device1",
                member="Disconnect",
            )
        )
        if reply is not None and reply.message_type is MessageType.METHOD_RETURN:
            return True
        log.warning(
            "ble.stale_link_clear_failed",
            mac=mac,
            error=_dbus_error(reply),
            note="is the service user in the 'bluetooth' group?",
        )
        return False
    except Exception as exc:  # noqa: BLE001 - diagnosis only, never fatal
        log.warning("ble.stale_link_clear_failed", mac=mac, error=str(exc))
        return False
    finally:
        if bus is not None:
            bus.disconnect()


def _dbus_error(reply: object) -> str:
    if reply is None:
        return "no reply"
    body = getattr(reply, "body", None) or []
    return f"{getattr(reply, 'error_name', '?')}: {body[0] if body else ''}".strip()


async def scan_devices(adapter: str | None, timeout: float) -> list[dict[str, object]]:
    """Scan for BLE devices, annotating the ones that look like EcoFlow units.

    A diagnostic for identifying an unknown power station: EcoFlow advertises a
    name of the form ``EF-<model prefix><serial tail>`` (``EF-D3…`` for a
    DELTA 3, ``EF-R35…`` for a DELTA 2 Max), and the manufacturer payload
    carries the ``encrypt_type`` the handshake must use.
    """
    found: dict[str, dict[str, object]] = {}

    def _cb(device: BLEDevice, adv) -> None:
        mfr = dict(getattr(adv, "manufacturer_data", {}) or {})
        previous = found.get(device.address, {})
        # Names and manufacturer data arrive in different advertisement packets,
        # so merge rather than replace: keep whichever fields we have seen.
        name = device.name or previous.get("name") or ""
        if not mfr and previous.get("manufacturer_data"):
            payloads = previous["manufacturer_data"]
        else:
            payloads = {str(k): v.hex() for k, v in mfr.items()}
        encrypt_type = parse_encrypt_type(mfr)
        if encrypt_type is None:
            encrypt_type = previous.get("encrypt_type")  # type: ignore[assignment]
        found[device.address] = {
            "mac": device.address,
            "name": name,
            "rssi": getattr(adv, "rssi", None),
            "looks_like_ecoflow": str(name).upper().startswith("EF-"),
            "encrypt_type": encrypt_type,
            "manufacturer_data": payloads,
        }

    scanner = BleakScanner(detection_callback=_cb, adapter=adapter)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()
    # EcoFlow devices first, then by signal strength.
    return sorted(
        found.values(),
        key=lambda d: (not d["looks_like_ecoflow"], -(d["rssi"] or -999)),  # type: ignore[operator]
    )


# --------------------------------------------------------------------------- #
# BLE client
# --------------------------------------------------------------------------- #
class EcoFlowBLE:
    """Manages a single EcoFlow BLE connection and exposes its rolling state."""

    def __init__(
        self,
        ecoflow: EcoflowConfig,
        ble: BleConfig,
        on_state: Callable[[DeviceState], None] | None = None,
        on_packet: Callable[[Packet], None] | None = None,
        on_display_payload: Callable[[bytes], None] | None = None,
    ) -> None:
        self._ecoflow = ecoflow
        self._ble = ble
        self._on_state = on_state
        # Fires for every decoded frame, recognised or not -- used by the
        # ``sniff`` diagnostic to see traffic no driver claims yet.
        self._on_packet = on_packet
        # Raw DisplayPropertyUpload payload, before decoding drops the fields we
        # do not recognise. Only the protocol diagnostic ('read --raw') uses it;
        # the daemon leaves it unset. DELTA 3 generation only -- that payload is
        # protobuf, where the DELTA 2's is a fixed-width struct, so 'sniff' is
        # the equivalent diagnostic there.
        self._on_display_payload = on_display_payload
        # Raises ValueError on an unknown model, at construction rather than
        # after a connection has been established.
        self._driver: DeviceDriver = devices.get_driver(ecoflow.model)

        self.state = DeviceState()
        self._client: BleakClient | None = None
        self._notify_uuid: str | None = None
        self._write_uuid: str | None = None
        self._encryption: Encryption | None = None
        self._assembler: FrameAssembler = PassthroughAssembler()
        self._encrypt_type = 0
        self._authenticated = asyncio.Event()
        self._last_read_monotonic: float = 0.0
        # When the link came up, so a drop can report how long it lasted. An
        # unstable link is diagnosed from that number -- dropping every 30s and
        # dropping once a day have completely different causes -- and guessing
        # at it from the gaps between log lines is no way to work.
        self._connected_monotonic: float = 0.0
        # Last raw notification, to drop BlueZ's duplicate deliveries.
        self._last_notification: tuple[float, bytes] | None = None
        # Strong references to in-flight reply tasks (see _reply).
        self._reply_tasks: set[asyncio.Task[None]] = set()

    @property
    def driver(self) -> DeviceDriver:
        return self._driver

    @property
    def packet_version(self) -> int:
        """Frame version for control/auth: the config override, else the driver's."""
        override = self._ecoflow.packet_version
        return override if override is not None else self._driver.packet_version

    @property
    def ack_frames(self) -> bool:
        """Echo recognised frames back? The config override, else the driver's."""
        override = self._ecoflow.ack_frames
        return override if override is not None else self._driver.ack_frames

    @property
    def last_read_monotonic(self) -> float:
        return self._last_read_monotonic

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def _resolve_encrypt_type(self, adv) -> int:
        configured = self._ecoflow.encrypt_type
        if isinstance(configured, int):
            return configured
        if isinstance(configured, str) and configured.isdigit():
            return int(configured)
        # "auto": read from the advertisement, default to 7 (modern devices).
        # There may be no advertisement at all -- see connect() -- in which case
        # the default stands, which is right for every current EcoFlow. Pin
        # ecoflow.encrypt_type in config for an older one.
        adv_type = parse_encrypt_type(getattr(adv, "manufacturer_data", {}) or {})
        if adv_type is not None:
            return adv_type
        return 7

    async def connect(self) -> None:
        """Find the configured MAC, connect, and run the handshake."""
        mac = self._ecoflow.mac.upper()
        log.info("ble.scanning", mac=mac, adapter=self._ble.adapter)
        device, adv = await self._find_device(mac)

        # A silent scan is not proof of absence. A station that BlueZ still
        # holds a connection to does not advertise, and BlueZ does not drop
        # connections when the process that made them exits or is killed
        # (bluez/bluez#89) -- the state any crash, OOM kill or `kill -9` leaves
        # behind. The device is then invisible to every scanner on the host, so
        # scanning again cannot help; the link has to be taken down first.
        if device is None:
            log.warning("ble.not_advertising", mac=mac)
            if await clear_stale_connection(mac, self._ble.adapter):
                log.warning("ble.stale_link_cleared", mac=mac, note="rescanning")
                device, adv = await self._find_device(mac)
        if device is None:
            raise BleakConnectionError(f"device {mac} not found during scan")

        self._encrypt_type = await self._resolve_encrypt_type(adv)
        log.info("ble.found", mac=mac, encrypt_type=self._encrypt_type)

        self._authenticated.clear()
        log.debug("ble.connecting", mac=mac)
        self._client = await self._establish(device)
        self._connected_monotonic = time.monotonic()
        log.info("ble.connected", mac=mac, rssi=getattr(adv, "rssi", None))
        self._resolve_characteristics()
        log.debug(
            "ble.characteristics",
            notify=self._notify_uuid,
            write=self._write_uuid,
        )
        log.info("ble.handshake_start", encrypt_type=self._encrypt_type)
        await self._handshake()
        log.info("ble.handshake_done")

    async def _establish(self, device: BLEDevice) -> BleakClient:
        """Connect, preferring bleak-retry-connector to survive BlueZ's habit of
        accepting a connection and then immediately dropping it on first tries.

        Always takes a scanned device. Handing bleak a bare address instead does
        not avoid the scan -- its BlueZ backend just resolves the address with
        ``find_device_by_address``, the same scan, one level down.
        """
        try:
            from bleak_retry_connector import establish_connection
        except ImportError:
            client = BleakClient(
                device,
                timeout=self._ble.connect_timeout_seconds,
                disconnected_callback=self._on_disconnect,
                bluez={"adapter": self._ble.adapter} if self._ble.adapter else {},
            )
            await client.connect()
            return client

        return await establish_connection(
            BleakClient,
            device,
            device.name or self._ecoflow.mac,
            disconnected_callback=self._on_disconnect,
            max_attempts=4,
        )

    async def _find_device(self, mac: str) -> tuple[BLEDevice | None, object]:
        found: dict[str, tuple[BLEDevice, object]] = {}
        event = asyncio.Event()

        def _cb(device: BLEDevice, adv) -> None:
            if device.address.upper() == mac:
                found[mac] = (device, adv)
                event.set()

        scanner = BleakScanner(detection_callback=_cb, adapter=self._ble.adapter)
        await scanner.start()
        try:
            await asyncio.wait_for(event.wait(), timeout=self._ble.scan_timeout_seconds)
        except TimeoutError:
            return None, None
        finally:
            await scanner.stop()
        return found.get(mac, (None, None))

    def _resolve_characteristics(self) -> None:
        assert self._client is not None
        services = self._client.services
        for pair in _CHARACTERISTICS.values():
            notify = services.get_characteristic(pair["notify"])
            write = services.get_characteristic(pair["write"])
            if notify is not None and write is not None:
                self._notify_uuid = pair["notify"]
                self._write_uuid = pair["write"]
                return
        chars = [c.uuid for c in services.characteristics.values()]
        raise BleakConnectionError(
            f"no known EcoFlow characteristics found; device exposes: {chars}"
        )

    # -- handshake ---------------------------------------------------------- #
    async def _handshake(self) -> None:
        if self._encrypt_type == 0:
            self._assembler = PassthroughAssembler()
            await self._start_notify(self._on_notify)
            await self._request_auth_status()
            await self._auto_authenticate()
        elif self._encrypt_type == 1:
            sn = self._ecoflow.serial
            session_key = hashlib.md5(sn.encode()).digest()
            iv = hashlib.md5(sn[::-1].encode()).digest()
            self._encryption = Type1Encryption(session_key, iv)
            self._assembler = RawHeaderAssembler(self._encryption)
            await self._start_notify(self._on_notify)
            await self._request_auth_status()
            await self._auto_authenticate()
        else:
            await self._ecdh_handshake()

    async def _ecdh_handshake(self) -> None:
        import ecdsa  # imported lazily so type-0/1 paths don't require it

        if not self._ecoflow.user_id:
            raise BleakConnectionError(
                "encrypt_type 7 (ECDH) requires 'ecoflow.user_id' in config; see README"
            )

        private_key = ecdsa.SigningKey.generate(curve=ecdsa.SECP160r1)
        public_key = private_key.get_verifying_key()

        # Step 1: exchange public keys.
        log.debug("ecdh.step1_send_pubkey")
        resp = await self._request(
            encode_enc_packet(_FRAME_COMMAND, b"\x01\x00" + public_key.to_string())
        )
        log.debug("ecdh.step1_resp", hex=resp[:64].hex(), length=len(resp))
        simple = self._parse_simple(resp)
        if simple is None or len(simple) < 3:
            raise BleakConnectionError(f"ECDH: invalid public key response: {resp.hex()}")
        ecdh_size = _ecdh_type_size(simple[2])
        dev_pub = ecdsa.VerifyingKey.from_string(
            simple[3 : 3 + ecdh_size], curve=ecdsa.SECP160r1
        )
        shared = ecdsa.ECDH(
            ecdsa.SECP160r1, private_key, dev_pub
        ).generate_sharedsecret_bytes()
        iv = hashlib.md5(shared).digest()
        self._encryption = Type7Encryption(shared[:16], iv)
        log.debug("ecdh.step1_shared_ok", curve_type=simple[2])

        # Step 2: get key info and derive the session key.
        log.debug("ecdh.step2_send_keyinfo")
        resp = await self._request(encode_enc_packet(_FRAME_COMMAND, b"\x02"))
        log.debug("ecdh.step2_resp", hex=resp[:64].hex(), length=len(resp))
        enc = self._parse_simple(resp)
        if enc is None or enc[0] != 0x02:
            raise BleakConnectionError(f"ECDH: invalid key-info response: {resp.hex()}")
        decrypted = self._encryption.decrypt(enc[1:])
        session_key = gen_session_key(decrypted[16:18], decrypted[:16])
        self._encryption = Type7Encryption(session_key, iv)
        self._assembler = EncPacketAssembler(self._encryption)
        log.debug("ecdh.step2_session_key_ok")

        # Step 3: auth status, then authenticate.
        log.debug("ecdh.step3_auth")
        await self._request_auth_status()
        await self._start_notify(self._on_notify)
        await self._auto_authenticate()
        log.debug("ecdh.step3_auth_sent")

    async def _request_auth_status(self) -> None:
        """Wake the auth state machine before sending credentials (cmd 0x89)."""
        version = self.packet_version
        await self._send_packet(
            Packet(0x21, _AUTH_DST, 0x35, 0x89, b"", 0x01, 0x01, version)
        )

    async def _auto_authenticate(self) -> None:
        digest = hashlib.md5(
            (self._ecoflow.user_id + self._ecoflow.serial).encode("ascii")
        ).digest()
        payload = "".join(f"{b:02X}" for b in digest).encode("ascii")
        # Credentials must go out in the frame version the model speaks: the
        # DELTA 2 generation is V2, the DELTA 3 generation V3. Getting this
        # wrong makes the device drop the link instead of replying.
        version = self.packet_version
        packet = Packet(0x21, _AUTH_DST, 0x35, 0x86, payload, 0x01, 0x01, version)
        await self._send_packet(packet)

    # -- IO ----------------------------------------------------------------- #
    async def _start_notify(self, callback) -> None:
        assert self._client is not None and self._notify_uuid is not None
        await self._client.start_notify(self._notify_uuid, callback)

    async def _write(self, data: bytes) -> None:
        assert self._client is not None and self._write_uuid is not None
        await self._client.write_gatt_char(
            self._write_uuid,
            bytearray(data),
            response=self._assembler.write_with_response,
        )

    async def _send_packet(self, packet: Packet) -> None:
        await self._write(self._assembler.encode(packet))

    async def _request(self, data: bytes, timeout: float = 10.0) -> bytes:
        """Write a command frame and await a single notification response."""
        assert self._client is not None and self._notify_uuid is not None
        future: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()

        def _cb(_char: BleakGATTCharacteristic, value: bytearray) -> None:
            if not future.done():
                future.set_result(bytes(value))

        await self._client.start_notify(self._notify_uuid, _cb)
        try:
            log.debug("ble.request_write", length=len(data), hex=data[:48].hex())
            await self._write(data)
            resp = await asyncio.wait_for(future, timeout=timeout)
            return resp
        finally:
            try:
                await self._client.stop_notify(self._notify_uuid)
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass

    def _parse_simple(self, data: bytes) -> bytes | None:
        """Extract the payload of a single ``0x5A5A`` command frame."""
        start = data.find(_ENC_PACKET_PREFIX)
        if start < 0 or len(data) - start < 8:
            return None
        data = data[start:]
        end = 6 + struct.unpack_from("<H", data, 4)[0]
        if end > len(data):
            return None
        body = data[6 : end - 2]
        if crc16(data[:6] + body) != struct.unpack_from("<H", data, end - 2)[0]:
            return None
        return body

    # -- notifications ------------------------------------------------------ #
    def _on_notify(self, _char: BleakGATTCharacteristic, value: bytearray) -> None:
        data = bytes(value)
        log.debug("ble.notify", length=len(data), hex=data[:48].hex())
        if self._is_duplicate(data):
            log.debug("ble.notify_duplicate", length=len(data))
            return
        try:
            payloads = self._assembler.reassemble(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("ble.reassemble_error", error=str(exc))
            return
        for raw in payloads:
            self._handle_payload(raw)

    def _is_duplicate(self, data: bytes) -> bool:
        """True if this notification repeats the previous one within a few ms.

        BlueZ can deliver the same notification twice (both through the
        AcquireNotify pipe and as a PropertiesChanged signal). Telemetry merges
        are idempotent so a duplicate is harmless there, but we answer every
        recognised frame to keep the device streaming -- and replying twice
        doubles the write traffic on a link that is already chatty.

        Two genuinely distinct heartbeats never carry identical bytes this
        close together: they arrive ~1 s apart and their counters differ.
        """
        now = time.monotonic()
        previous = self._last_notification
        self._last_notification = (now, data)
        return previous is not None and previous[1] == data and now - previous[0] < 0.05

    def _handle_payload(self, raw: bytes) -> None:
        try:
            packet = protocol.parse_frame(raw, xor_payload=self._driver.xor_payload)
        except PacketError as exc:
            log.debug("ble.packet_parse_skip", error=str(exc))
            return

        if not self._authenticated.is_set():
            self._authenticated.set()
            log.info("ble.authenticated")

        log.debug(
            "ble.packet",
            src=f"0x{packet.src:02x}",
            cmd_set=f"0x{packet.cmd_set:02x}",
            cmd_id=f"0x{packet.cmd_id:02x}",
            plen=len(packet.payload),
        )

        if self._on_packet is not None:
            self._on_packet(packet)
        if self._on_display_payload is not None and delta3.is_display_packet(packet):
            self._on_display_payload(packet.payload)

        if self._driver.handle_packet(self.state, packet):
            log.debug(
                "ble.telemetry",
                soc=self.state.soc_percent,
                ac_present=self.state.ac_input_present,
                ac_in=self.state.ac_input_watts,
                ac_out=self.state.ac_output_watts,
            )
            self._last_read_monotonic = asyncio.get_event_loop().time()
            if self._on_state is not None:
                self._on_state(self.state)
            if self.ack_frames:
                # Keep a strong reference: a bare create_task may be garbage-
                # collected while it is still running, and its exception then
                # surfaces as an unretrieved-future warning instead of being
                # handled below.
                task = asyncio.create_task(self._reply(packet))
                self._reply_tasks.add(task)
                task.add_done_callback(self._reply_tasks.discard)

    async def _reply(self, packet: Packet) -> None:
        # The link may have dropped between the frame arriving and this task
        # being scheduled -- writing then raises BrokenPipeError from the D-Bus
        # transport rather than a BLE-level error.
        if not self.is_connected:
            return
        reply = Packet(
            src=packet.dst,
            dst=packet.src,
            cmd_set=packet.cmd_set,
            cmd_id=packet.cmd_id,
            payload=packet.payload,
            dsrc=0x01,
            ddst=0x01,
            version=packet.version,
            seq=packet.seq,
        )
        try:
            await self._send_packet(reply)
        except Exception as exc:  # noqa: BLE001 - a reply is best-effort
            log.debug("ble.reply_failed", error=str(exc))

    def _cancel_replies(self) -> None:
        """Drop in-flight replies; their connection is gone."""
        for task in list(self._reply_tasks):
            task.cancel()
        self._reply_tasks.clear()

    async def wait_authenticated(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._authenticated.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    # -- control ------------------------------------------------------------ #
    async def send_command_packet(self, packet: Packet) -> None:
        await self._send_packet(packet)

    def _on_disconnect(self, _client: BleakClient) -> None:
        held = (
            round(time.monotonic() - self._connected_monotonic, 1)
            if self._connected_monotonic
            else None
        )
        log.warning("ble.disconnected", link_seconds=held)
        self._connected_monotonic = 0.0
        self._authenticated.clear()
        self._cancel_replies()

    async def disconnect(self) -> None:
        self._cancel_replies()
        if self._client is not None and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                log.debug("ble.disconnect_error", error=str(exc))
        self._client = None

        # Then make sure BlueZ agrees, because it need not. bleak's view of the
        # link and BlueZ's can diverge -- a half-open link reads as disconnected
        # to bleak while BlueZ still holds the ACL -- and the branch above skips
        # the teardown in exactly that case. BlueZ then keeps the link after we
        # exit, the station stops advertising, and the next start burns a full
        # scan timeout discovering it. Cheap to be sure: one D-Bus round trip
        # that no-ops when the link really is down.
        #
        # Never let this raise. It runs in the reconnect loop's finally, where an
        # exception would escape the handler that exists to keep reconnecting.
        try:
            if await clear_stale_connection(self._ecoflow.mac.upper(), self._ble.adapter):
                log.info("ble.link_released_via_bluez")
        except Exception as exc:  # noqa: BLE001
            log.debug("ble.disconnect_error", error=str(exc))


class BleakConnectionError(RuntimeError):
    """Raised for connection/handshake failures."""


def _ecdh_type_size(curve_num: int) -> int:
    return {1: 52, 2: 56, 3: 64, 4: 64}.get(curve_num, 40)
