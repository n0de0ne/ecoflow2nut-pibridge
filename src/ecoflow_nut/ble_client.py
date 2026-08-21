"""BLE transport for EcoFlow power stations using ``bleak``.

Implements the connection lifecycle, the EcoFlow session handshake (encrypt
types 0 / 1 / 7), wire-level frame (re)assembly, and decoding of incoming
packets into a rolling :class:`~ecoflow_nut.delta3.DeviceState`.

Handshake / encryption details mirror the reverse engineering in
https://github.com/rabits/ha-ef-ble (Apache-2.0). The verified-correct read
path -- V3 framing + protobuf decode -- is exercised by the test suite against
real captured frames. The encrypted handshakes (type 1 and especially the
ECDH-based type 7 used by the DELTA 3) require hardware to validate end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Final

import structlog
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from . import delta3, keydata
from .config import BleConfig, EcoflowConfig
from .delta3 import DeviceState
from .protocol import Packet, PacketError, crc16

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
            if len(data) < 20:
                break
            length = struct.unpack_from("<H", data, 2)[0]
            frame_len = 18 + length + 2
            if len(data) < frame_len:
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
        from .protocol import crc8

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
        on_display_payload: Callable[[bytes], None] | None = None,
    ) -> None:
        self._ecoflow = ecoflow
        self._ble = ble
        self._on_state = on_state
        # Raw DisplayPropertyUpload payload, before decoding drops the fields we
        # do not recognise. Only the protocol diagnostic ('read --raw') uses it;
        # the daemon leaves it unset.
        self._on_display_payload = on_display_payload

        self.state = DeviceState()
        self._client: BleakClient | None = None
        self._notify_uuid: str | None = None
        self._write_uuid: str | None = None
        self._encryption: Encryption | None = None
        self._assembler: FrameAssembler = PassthroughAssembler()
        self._encrypt_type = 0
        self._authenticated = asyncio.Event()
        self._last_read_monotonic: float = 0.0

    @property
    def last_read_monotonic(self) -> float:
        return self._last_read_monotonic

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def _resolve_encrypt_type(self, device: BLEDevice, adv) -> int:
        configured = self._ecoflow.encrypt_type
        if isinstance(configured, int):
            return configured
        if isinstance(configured, str) and configured.isdigit():
            return int(configured)
        # "auto": read from the advertisement, default to 7 (modern devices).
        adv_type = parse_encrypt_type(getattr(adv, "manufacturer_data", {}) or {})
        if adv_type is not None:
            return adv_type
        return 7

    async def connect(self) -> None:
        """Scan for the configured MAC, connect, and run the handshake."""
        mac = self._ecoflow.mac.upper()
        log.info("ble.scanning", mac=mac, adapter=self._ble.adapter)
        device, adv = await self._find_device(mac)
        if device is None:
            raise BleakConnectionError(f"device {mac} not found during scan")

        self._encrypt_type = await self._resolve_encrypt_type(device, adv)
        log.info("ble.found", mac=mac, encrypt_type=self._encrypt_type)

        self._authenticated.clear()
        log.debug("ble.connecting", mac=mac)
        self._client = await self._establish(device)
        log.info("ble.connected", mac=mac)
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
        accepting a connection and then immediately dropping it on first tries."""
        try:
            from bleak_retry_connector import establish_connection
        except ImportError:
            client = BleakClient(
                device,
                timeout=self._ble.connect_timeout_seconds,
                disconnected_callback=self._on_disconnect,
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
            await self._auto_authenticate()
        elif self._encrypt_type == 1:
            sn = self._ecoflow.serial
            session_key = hashlib.md5(sn.encode()).digest()
            iv = hashlib.md5(sn[::-1].encode()).digest()
            self._encryption = Type1Encryption(session_key, iv)
            self._assembler = RawHeaderAssembler(self._encryption)
            await self._start_notify(self._on_notify)
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
        await self._send_packet(Packet(0x21, _AUTH_DST, 0x35, 0x89, b"", 0x01, 0x01, 3))
        await self._start_notify(self._on_notify)
        await self._auto_authenticate()
        log.debug("ecdh.step3_auth_sent")

    async def _auto_authenticate(self) -> None:
        digest = hashlib.md5(
            (self._ecoflow.user_id + self._ecoflow.serial).encode("ascii")
        ).digest()
        payload = "".join(f"{b:02X}" for b in digest).encode("ascii")
        packet = Packet(0x21, _AUTH_DST, 0x35, 0x86, payload, 0x01, 0x01, 3)
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
        log.debug("ble.notify", length=len(value), hex=bytes(value[:48]).hex())
        try:
            payloads = self._assembler.reassemble(bytes(value))
        except Exception as exc:  # noqa: BLE001
            log.warning("ble.reassemble_error", error=str(exc))
            return
        for raw in payloads:
            self._handle_payload(raw)

    def _handle_payload(self, raw: bytes) -> None:
        try:
            packet = Packet.from_bytes(raw, xor_payload=True)
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

        if delta3.DISPLAY_SRC == packet.src and self.state.is_display_packet(packet):
            if self._on_display_payload is not None:
                self._on_display_payload(packet.payload)
            self.state.merge_display_payload(packet.payload)
            log.debug(
                "ble.display",
                soc=self.state.soc_percent,
                ac_present=self.state.ac_input_present,
                ac_in=self.state.ac_input_watts,
                ac_out=self.state.ac_output_watts,
            )
            self._last_read_monotonic = asyncio.get_event_loop().time()
            if self._on_state is not None:
                self._on_state(self.state)
            # Reply so the device keeps streaming richer data.
            asyncio.create_task(self._reply(packet))

    async def _reply(self, packet: Packet) -> None:
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
        except Exception as exc:  # noqa: BLE001
            log.debug("ble.reply_failed", error=str(exc))

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
        log.warning("ble.disconnected")
        self._authenticated.clear()

    async def disconnect(self) -> None:
        if self._client is not None and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                log.debug("ble.disconnect_error", error=str(exc))
        self._client = None


class BleakConnectionError(RuntimeError):
    """Raised for connection/handshake failures."""


def _ecdh_type_size(curve_num: int) -> int:
    return {1: 52, 2: 56, 3: 64, 4: 64}.get(curve_num, 40)
