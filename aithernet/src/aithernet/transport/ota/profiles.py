"""Phase 1: profile-based modem architecture.

An :class:`RFLinkProfile` describes ONLY the waveform/link parameters — never frequency, gain,
transmit power, antenna, or hardware identity (those belong to the authorization plan + hardware
policy). Profiles are an explicit, validated plugin surface: "flexible protocols" means a documented
approved-profile registry, NOT unrestricted runtime waveform generation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

# beta.2: DESCRIPTIVE qualification states — they INFORM the operator, they do NOT vendor-lock or
# prohibit physical use. A technically-valid profile may be transmitted once the LOCAL operator
# enables TX and confirms the plan, regardless of qualification state. Back-compat constant NAMES
# are retained (their string VALUES are now the descriptive states).
QUAL_EXPERIMENTAL = "experimental"            # newly generated/registered; not yet validated
QUAL_SOFTWARE_VALIDATED = "software_validated"  # passed deterministic simulation/channel validation
QUAL_OPERATOR_CUSTOM = "operator_custom"      # operator-authored; accepted with local confirmation
QUAL_HARDWARE_TESTED = "hardware_tested"      # the local operator has confirmed it on real hardware

QUALIFICATION_STATES = (QUAL_EXPERIMENTAL, QUAL_SOFTWARE_VALIDATED, QUAL_OPERATOR_CUSTOM,
                        QUAL_HARDWARE_TESTED)

# Back-compat aliases (used by Phase-4 candidate validation + tests).
PROFILE_UNAPPROVED = QUAL_EXPERIMENTAL
PROFILE_APPROVED_SIMULATION = QUAL_SOFTWARE_VALIDATED
PROFILE_APPROVED_PHYSICAL = QUAL_HARDWARE_TESTED


@dataclass(frozen=True)
class RFLinkProfile:
    profile_id: str
    modulation: str                # "bpsk" | "qpsk" | ...
    symbol_rate: int
    sample_rate: int
    occupied_bandwidth_hz: int
    samples_per_symbol: int
    preamble: bytes
    sync_word: bytes
    fec_scheme: str                # "none" | "repetition3" | "hamming74" | ...
    fec_rate: str                  # e.g. "1/3", "4/7", "1"
    crc_scheme: str                # "crc16" | "crc32"
    interleaver: str | None
    max_frame_payload_bytes: int
    max_message_bytes: int
    ack_mode: str                  # "stop_and_wait" | "none"
    duplex_mode: str               # "half" | "simplex"
    timeout_seconds: float
    max_retries: int

    def digest(self) -> str:
        """Stable sha256 over the profile (bytes hex) — binds authorization to the waveform."""
        d = asdict(self)
        d["preamble"] = self.preamble.hex()
        d["sync_word"] = self.sync_word.hex()
        body = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(body.encode()).hexdigest()

    def to_manifest(self) -> dict:
        d = asdict(self)
        d["preamble"] = self.preamble.hex()
        d["sync_word"] = self.sync_word.hex()
        d["profile_digest"] = self.digest()
        return d


class ProfileValidationError(ValueError):
    """Raised when a profile is structurally invalid or unsafe to register."""


_SUPPORTED_MODULATION = {"bpsk", "qpsk"}
_SUPPORTED_FEC = {"none", "repetition3", "hamming74"}
_SUPPORTED_CRC = {"crc16", "crc32"}


def validate_profile(p: RFLinkProfile) -> None:
    """Structurally validate a profile (does NOT approve physical TX). Raises on any problem."""
    if not p.profile_id or " " in p.profile_id:
        raise ProfileValidationError("profile_id must be a non-empty token")
    if p.modulation not in _SUPPORTED_MODULATION:
        raise ProfileValidationError(f"unsupported modulation {p.modulation!r}")
    if p.fec_scheme not in _SUPPORTED_FEC:
        raise ProfileValidationError(f"unsupported fec_scheme {p.fec_scheme!r}")
    if p.crc_scheme not in _SUPPORTED_CRC:
        raise ProfileValidationError(f"unsupported crc_scheme {p.crc_scheme!r}")
    for name, val in (("symbol_rate", p.symbol_rate), ("sample_rate", p.sample_rate),
                      ("samples_per_symbol", p.samples_per_symbol),
                      ("max_frame_payload_bytes", p.max_frame_payload_bytes),
                      ("max_message_bytes", p.max_message_bytes)):
        if val <= 0:
            raise ProfileValidationError(f"{name} must be positive")
    if p.sample_rate != p.symbol_rate * p.samples_per_symbol:
        raise ProfileValidationError("sample_rate must equal symbol_rate * samples_per_symbol")
    if p.max_frame_payload_bytes > p.max_message_bytes:
        raise ProfileValidationError("frame payload cannot exceed max message size")
    if not p.preamble or not p.sync_word:
        raise ProfileValidationError("preamble and sync_word are required")
    if p.max_retries < 0:
        raise ProfileValidationError("max_retries must be >= 0")
    if p.timeout_seconds <= 0:
        raise ProfileValidationError("timeout_seconds must be positive")


# -- the beta.1 reference profile: a robust, low-rate BPSK link -------------------------------
REFERENCE_BPSK = RFLinkProfile(
    profile_id="ref-bpsk-1k",
    modulation="bpsk",
    symbol_rate=1000,
    sample_rate=8000,
    occupied_bandwidth_hz=1200,
    samples_per_symbol=8,
    preamble=bytes([0xAA] * 4),       # 0xAAAAAAAA alternating for timing acquisition
    sync_word=bytes([0x2D, 0xD4]),    # a 16-bit sync marker
    fec_scheme="repetition3",
    fec_rate="1/3",
    crc_scheme="crc16",
    interleaver=None,
    max_frame_payload_bytes=128,
    max_message_bytes=65536,
    ack_mode="stop_and_wait",
    duplex_mode="half",
    timeout_seconds=2.0,
    max_retries=4,
)

# A QPSK variant demonstrating the plugin surface (same framing, 2 bits/symbol).
REFERENCE_QPSK = RFLinkProfile(
    profile_id="ref-qpsk-2k",
    modulation="qpsk",
    symbol_rate=2000,
    sample_rate=8000,
    occupied_bandwidth_hz=2400,
    samples_per_symbol=4,
    preamble=bytes([0xAA] * 4),
    sync_word=bytes([0x2D, 0xD4]),
    fec_scheme="repetition3",
    fec_rate="1/3",
    crc_scheme="crc16",
    interleaver=None,
    max_frame_payload_bytes=128,
    max_message_bytes=65536,
    ack_mode="stop_and_wait",
    duplex_mode="half",
    timeout_seconds=2.0,
    max_retries=4,
)


class ProfileRegistry:
    """A bounded registry of profiles + approval state. No profile is physical-approved here."""

    def __init__(self) -> None:
        self._profiles: dict[str, RFLinkProfile] = {}
        self._state: dict[str, str] = {}
        for p in (REFERENCE_BPSK, REFERENCE_QPSK):
            validate_profile(p)
            self._profiles[p.profile_id] = p
            self._state[p.profile_id] = PROFILE_APPROVED_SIMULATION

    def register_candidate(self, p: RFLinkProfile) -> str:
        """Register a generated profile as UNAPPROVED (never directly transmittable)."""
        validate_profile(p)
        self._profiles[p.profile_id] = p
        self._state[p.profile_id] = PROFILE_UNAPPROVED
        return p.digest()

    def promote_to_simulation(self, profile_id: str) -> None:
        if profile_id not in self._profiles:
            raise KeyError(profile_id)
        self._state[profile_id] = PROFILE_APPROVED_SIMULATION

    def state(self, profile_id: str) -> str:
        return self._state.get(profile_id, PROFILE_UNAPPROVED)

    def get(self, profile_id: str) -> RFLinkProfile:
        return self._profiles[profile_id]

    def list_ids(self) -> list[str]:
        return sorted(self._profiles)

    def approved_for_simulation(self, profile_id: str) -> bool:
        return self._state.get(profile_id) in (
            QUAL_SOFTWARE_VALIDATED, QUAL_HARDWARE_TESTED, QUAL_OPERATOR_CUSTOM)

    # -- beta.2: descriptive qualification (informational; never a prohibition) ---------------
    def qualification_state(self, profile_id: str) -> str:
        return self._state.get(profile_id, QUAL_EXPERIMENTAL)

    def mark_operator_custom(self, profile_id: str) -> None:
        """Record that the operator authored/accepted this profile (after local confirmation)."""
        if profile_id not in self._profiles:
            raise KeyError(profile_id)
        self._state[profile_id] = QUAL_OPERATOR_CUSTOM

    def mark_hardware_tested(self, profile_id: str) -> None:
        """Record that the LOCAL operator confirmed this profile on real hardware (their report)."""
        if profile_id not in self._profiles:
            raise KeyError(profile_id)
        self._state[profile_id] = QUAL_HARDWARE_TESTED

    def register_operator_custom(self, p: RFLinkProfile) -> str:
        """Register an operator-authored profile. Structurally validated, then operator_custom."""
        validate_profile(p)
        self._profiles[p.profile_id] = p
        self._state[p.profile_id] = QUAL_OPERATOR_CUSTOM
        return p.digest()

    def usable_physically(self, profile_id: str, *, operator_confirmed: bool) -> bool:
        """Whether a profile may be used for PHYSICAL TX. A technically-valid (registered) profile
        is usable once the LOCAL operator confirms — qualification state does NOT prohibit it.

        This is the beta.2 correction: 'not yet hardware-tested' INFORMS the operator (recorded
        in the plan + research record) but is never an unremovable vendor lock. Invalid profiles are
        still rejected elsewhere for concrete technical reasons (malformed graph, bad sample rate,
        hardware range violation, missing TX sink, failed build, etc.)."""
        return profile_id in self._profiles and operator_confirmed
