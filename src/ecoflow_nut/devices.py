"""Registry mapping the configured ``ecoflow.model`` to a protocol driver.

EcoFlow power stations fall into two incompatible protocol generations, and the
bridge needs to know which one it is talking to *before* the first frame arrives
(the framing version differs, so it cannot be sniffed reliably):

``delta3``
    The ``pd335`` generation -- V3 frames, protobuf ``DisplayPropertyUpload``
    telemetry, ``ConfigWrite`` control. DELTA 3 / River 3 and siblings.
``delta2max`` / ``delta2``
    The DELTA 2 generation -- V2 frames, fixed-width binary heartbeats, per-
    function control opcodes. See :mod:`ecoflow_nut.delta2`.
``raw``
    Not a protocol: connect and capture only, for a device we cannot identify
    yet. Lets ``probe``/``sniff`` work on an unknown unit without guessing.

Selection is by configuration, deliberately **not** by serial-number prefix.
Upstream projects gate on a hardcoded list of prefixes, so a regional or
later-revision unit gets rejected as "unsupported" even when it speaks a
protocol they already implement. Naming the model in ``config.yaml`` keeps an
unrecognised serial from locking anyone out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from . import delta2, delta3
from .protocol import Packet
from .state import DeviceState

# The output kinds every driver understands, as used by the CLI, the control
# socket and the auto-shutdown policy.
OUTPUT_KINDS = ("ac", "usb", "dc")


class DeviceDriver(Protocol):
    """What the daemon needs from a model's protocol implementation.

    Read-only members, so frozen dataclass drivers satisfy the protocol.
    """

    @property
    def name(self) -> str:
        """Canonical model name, for logging and diagnostics."""
        ...

    @property
    def packet_version(self) -> int:
        """EcoFlow frame version (2 or 3) for control and authentication."""
        ...

    @property
    def xor_payload(self) -> bool:
        """Whether incoming payloads are XOR-obfuscated with ``seq[0]``.

        This differs *per model*, not per generation, and only takes effect on
        frames whose ``seq[0]`` is non-zero -- so getting it wrong is invisible
        on some devices and produces garbage field values on others. If ``sniff``
        shows a recognised message whose numbers are nonsense, flip this first.
        """
        ...

    def handle_packet(self, state: DeviceState, packet: Packet) -> bool:
        """Merge a telemetry frame into ``state``; False if not recognised."""
        ...

    def output_packet(self, kind: str, enabled: bool) -> Packet:
        """Build a command packet toggling ``kind`` (see ``OUTPUT_KINDS``)."""
        ...


@dataclass(frozen=True, slots=True)
class RawDriver:
    """A driver for a device whose protocol is not known yet.

    Connects and authenticates like any other model but decodes nothing, so
    ``scan``/``probe``/``sniff`` can be pointed at an unidentified unit without
    pretending to understand its telemetry. Frames still reach the sniffer,
    which is what makes a capture possible in the first place.

    Not usable for the daemon: it publishes no telemetry (the watchdog will
    restart it) and refuses control commands rather than send a guessed opcode
    at hardware.
    """

    name: str = "raw"
    #: V3 is the majority default; override with ``ecoflow.packet_version``.
    packet_version: int = 3
    #: Off by default -- de-obfuscating a device that does not obfuscate turns
    #: good frames into noise, and the sniffer should show what actually arrived.
    xor_payload: bool = False

    def handle_packet(self, state: DeviceState, packet: Packet) -> bool:
        return False

    def output_packet(self, kind: str, enabled: bool) -> Packet:
        raise ValueError(
            f"cannot control {kind!r}: ecoflow.model is 'raw', so no control "
            "opcodes are known for this device. Identify it with 'sniff' first."
        )


# Canonical model name -> driver.
DRIVERS: dict[str, DeviceDriver] = {
    "delta3": delta3.DRIVER,
    "delta2": delta2.DELTA2,
    "delta2max": delta2.DELTA2_MAX,
    "raw": RawDriver(),
}

# Spellings people reasonably write in config.yaml, and the product codes
# EcoFlow prints on the unit, mapped onto a canonical name. Keys are compared
# after stripping everything but letters and digits, so "DELTA 2 Max",
# "delta-2-max" and "delta2max" all land here identically.
#
ALIASES = {
    # DELTA 2 Max (2048 Wh / 2400 W).
    "d2max": "delta2max",
    "deltamax2": "delta2max",
    "r351": "delta2max",
    "r354": "delta2max",
    # EcoFlow E2000 (EU/UK, 2048 Wh / 2400 W), serial prefix E201. CONFIRMED on
    # hardware, not inferred from the matching spec sheet: a unit authenticates
    # with V2 framing and streams the DELTA 2 Max subsystem set, including a PD
    # heartbeat of exactly 137 bytes (PD_DELTA2_MAX's size). It advertises as
    # EF-R35 + serial tail -- the DELTA 2 Max name prefix -- so only the serial
    # prefix is new, which is precisely what prefix-whitelisting projects reject.
    "e2000": "delta2max",
    "e201": "delta2max",
    "efe2000": "delta2max",
    "efe2000eucbox": "delta2max",
    # DELTA 2 and the siblings sharing its PD layout.
    "d2": "delta2",
    "e980": "delta2",
    "r331": "delta2",
    "r335": "delta2",
    "r701": "delta2",
    "delta21500": "delta2",
    "delta31500": "delta2",
    "d361": "delta2",
    "d365": "delta2",
    # DELTA 3 family.
    "d3": "delta3",
    "p231": "delta3",
    "river3": "delta3",
    "delta3plus": "delta3",
    # Diagnostic: a device we cannot name yet (see RawDriver).
    "unknown": "raw",
    "unsupported": "raw",
}


def normalise(model: str) -> str:
    """Reduce a model string to comparable form: lowercase alphanumerics only."""
    return "".join(c for c in model.lower() if c.isalnum())


def canonical_name(model: str) -> str:
    """Resolve ``model`` (any accepted spelling) to a canonical driver name."""
    key = normalise(model)
    return ALIASES.get(key, key)


def get_driver(model: str) -> DeviceDriver:
    """Look up the driver for a configured model name.

    Raises ``ValueError`` naming the supported models if it is not recognised.
    """
    name = canonical_name(model)
    driver = DRIVERS.get(name)
    if driver is None:
        supported = ", ".join(sorted(DRIVERS))
        raise ValueError(f"unsupported ecoflow.model {model!r}; supported: {supported}")
    return driver
