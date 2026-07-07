"""Phase 1: the OTA identity + security layer.

The OTA layer adds authenticated encryption (ChaCha20-Poly1305) over the serialized canonical
:class:`MessageEnvelope`, binding every security-relevant header field as AEAD associated data, plus
replay/sequence/expiry/receiver validation. The canonical envelope's own Ed25519 signature still
provides peer-identity binding on ingress — successful demodulation is NEVER treated as
authentication. No key material is logged, returned, or placed in any header/artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from aithernet.transport.envelope import MessageEnvelope

OTA_PROTOCOL_VERSION = 1
NONCE_BYTES = 12  # ChaCha20-Poly1305 nonce


@dataclass(frozen=True)
class OTASecurityHeader:
    """Cleartext, integrity-bound header. All fields are AEAD associated data (tamper-evident)."""

    protocol_version: int
    sender_node_id: str
    receiver_node_id: str
    peer_key_id: str
    message_id: str
    correlation_id: str | None
    sequence: int
    timestamp: int            # unix seconds
    expiry: int               # unix seconds (absolute)
    nonce_hex: str            # AEAD nonce (public, unique per message)
    payload_type: str         # "peer_envelope"
    payload_length: int       # ciphertext length
    payload_digest: str       # sha256 of the PLAINTEXT serialized envelope
    transport_profile_id: str

    def aad(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()

    def to_dict(self) -> dict:
        return asdict(self)


class OTASecurityError(Exception):
    """Raised on any AEAD/replay/expiry/receiver failure (never reveals key material)."""


def _serialize_envelope(env: MessageEnvelope) -> bytes:
    # The FULL envelope including its signature (so the receiver verifies peer identity on ingress).
    return json.dumps(env.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()


def seal(env: MessageEnvelope, *, key: bytes, sender_node_id: str, receiver_node_id: str,
         peer_key_id: str, sequence: int, transport_profile_id: str,
         expiry_seconds: float = 30.0, now: int | None = None) -> tuple[OTASecurityHeader, bytes]:
    """AEAD-seal a canonical envelope for OTA. Returns (header, ciphertext)."""
    if len(key) != 32:
        raise OTASecurityError("key must be 32 bytes")
    plaintext = _serialize_envelope(env)
    digest = "sha256:" + hashlib.sha256(plaintext).hexdigest()
    ts = int(now if now is not None else time.time())
    nonce = os.urandom(NONCE_BYTES)
    header = OTASecurityHeader(
        protocol_version=OTA_PROTOCOL_VERSION, sender_node_id=sender_node_id,
        receiver_node_id=receiver_node_id, peer_key_id=peer_key_id, message_id=env.message_id,
        correlation_id=env.correlation_id, sequence=sequence, timestamp=ts,
        expiry=ts + int(expiry_seconds), nonce_hex=nonce.hex(), payload_type="peer_envelope",
        payload_length=0, payload_digest=digest, transport_profile_id=transport_profile_id)
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, header.aad())
    # finalize payload_length now that we know the ciphertext size (re-bind AAD with the length)
    header = OTASecurityHeader(**{**asdict(header), "payload_length": len(ct)})
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, header.aad())
    return header, ct


def open_sealed(header: OTASecurityHeader, ciphertext: bytes, *, key: bytes,
                expected_receiver: str, replay_guard: ReplayGuard | None = None,
                now: int | None = None) -> MessageEnvelope:
    """Validate + AEAD-open an OTA message into the canonical envelope. Raises OTASecurityError."""
    if len(key) != 32:
        raise OTASecurityError("key must be 32 bytes")
    # receiver-address validation (constant-time compare on the id)
    if not _ct_eq(header.receiver_node_id.encode(), expected_receiver.encode()):
        raise OTASecurityError("receiver mismatch")
    ts = int(now if now is not None else time.time())
    if ts > header.expiry:
        raise OTASecurityError("message expired")
    if header.payload_length != len(ciphertext):
        raise OTASecurityError("payload length mismatch")
    if replay_guard is not None:
        replay_guard.check(header)   # raises on replay / out-of-window sequence
    nonce = bytes.fromhex(header.nonce_hex)
    try:
        plaintext = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, header.aad())
    except Exception as exc:  # noqa: BLE001 — never leak crypto internals
        raise OTASecurityError("authentication failed") from exc
    if "sha256:" + hashlib.sha256(plaintext).hexdigest() != header.payload_digest:
        raise OTASecurityError("payload digest mismatch")
    if replay_guard is not None:
        replay_guard.commit(header)
    return MessageEnvelope.model_validate(json.loads(plaintext))


def _ct_eq(a: bytes, b: bytes) -> bool:
    """Constant-time comparison over fixed-width digests (avoids length/early-exit leaks)."""
    import hmac
    return hmac.compare_digest(hashlib.sha256(a).digest(), hashlib.sha256(b).digest())


class ReplayGuard:
    """Per-sender nonce + monotonic sequence-window replay protection."""

    def __init__(self, window: int = 256) -> None:
        self._seen_nonce: set[str] = set()
        self._max_seq: dict[str, int] = {}
        self._window = window

    def check(self, header: OTASecurityHeader) -> None:
        if header.nonce_hex in self._seen_nonce:
            raise OTASecurityError("replayed nonce")
        sender = header.sender_node_id
        hi = self._max_seq.get(sender)
        if hi is not None and header.sequence <= hi - self._window:
            raise OTASecurityError("sequence outside replay window")

    def commit(self, header: OTASecurityHeader) -> None:
        self._seen_nonce.add(header.nonce_hex)
        sender = header.sender_node_id
        self._max_seq[sender] = max(self._max_seq.get(sender, -1), header.sequence)
