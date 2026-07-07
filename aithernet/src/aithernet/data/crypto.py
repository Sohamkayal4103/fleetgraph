"""Authenticated encryption + pseudonymization primitives (Stage 14E).

Uses the project's already-audited ``cryptography`` library — AES-256-GCM for the
authenticated-encryption envelope. No cryptography is invented here. Keys are supplied by the
caller (loaded from an env-referenced secret, never inlined or committed); this module never
loads, logs, or persists key material.

Pseudonymization is keyed HMAC-SHA-256 truncated to a stable token. It is **pseudonymization,
not anonymization** — a stable per-(tenant) key maps an identifier to a stable token; the same
input yields the same token within a tenant, and different tenants yield different tokens
(tenant separation), preserving enough lineage to honour later deletion requests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_VERSION = "aes-256-gcm.v1"
_NONCE_BYTES = 12


@dataclass
class EncryptionEnvelope:
    """The sealed-bundle ciphertext + non-secret metadata (never contains the key)."""

    version: str
    nonce_b64: str
    ciphertext: bytes
    aad_b64: str | None = None

    def metadata(self) -> dict:
        """Non-secret encryption metadata safe to persist in a batch row."""
        return {
            "scheme": self.version,
            "nonce_b64": self.nonce_b64,
            "aad_b64": self.aad_b64,
            "ciphertext_bytes": len(self.ciphertext),
        }


def generate_key_b64() -> str:
    """Generate a fresh base64 AES-256 key (for operator key provisioning, not committed)."""
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii")


def _load_key(key_b64: str) -> bytes:
    raw = base64.b64decode(key_b64)
    if len(raw) not in (16, 24, 32):
        raise ValueError("encryption key must be a base64 AES-128/192/256 key")
    return raw


def encrypt(plaintext: bytes, key_b64: str, *, aad: bytes | None = None) -> EncryptionEnvelope:
    """Encrypt with AES-GCM. The returned envelope carries no key material."""
    key = _load_key(key_b64)
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return EncryptionEnvelope(
        version=ENVELOPE_VERSION,
        nonce_b64=base64.b64encode(nonce).decode("ascii"),
        ciphertext=ct,
        aad_b64=base64.b64encode(aad).decode("ascii") if aad else None,
    )


def decrypt(envelope: EncryptionEnvelope, key_b64: str) -> bytes:
    key = _load_key(key_b64)
    nonce = base64.b64decode(envelope.nonce_b64)
    aad = base64.b64decode(envelope.aad_b64) if envelope.aad_b64 else None
    return AESGCM(key).decrypt(nonce, envelope.ciphertext, aad)


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def pseudonymize(value: str, *, key: bytes | str, namespace: str = "id") -> str:
    """Return a stable keyed-HMAC-SHA-256 pseudonym for ``value``.

    ``key`` is the tenant-separated derived key (bytes); a ``str`` is accepted only for
    low-level unit tests. Pseudonymization, not anonymization: same key + input → same token.
    """
    if value is None:
        return ""
    key_bytes = key if isinstance(key, bytes) else key.encode("utf-8")
    mac = hmac.new(key_bytes, f"{namespace}\x1f{value}".encode(), hashlib.sha256)
    return "pseu_" + base64.urlsafe_b64encode(mac.digest()[:15]).decode("ascii").rstrip("=")


# --- Secret-backed pseudonymization-key derivation (Stage 14E hardening) ----

#: Minimum master-secret entropy required (256 bits).
MIN_PSEUDONYM_KEY_BYTES = 32
#: A fixed, non-secret application salt for the HKDF (domain separation, not a secret).
_HKDF_SALT = b"aithernet/data-platform/pseudonym/v1"


def load_master_secret(secret_value: str | None) -> bytes | None:
    """Decode + validate a base64 master pseudonymization secret (>= 256 bits).

    ``secret_value`` is the value read from the environment by the caller (the config stores
    only the env-var NAME). Returns ``None`` when missing or too weak — the caller must then
    FAIL CLOSED, never fall back to a public/predictable value.
    """
    if not secret_value:
        return None
    try:
        raw = base64.b64decode(secret_value, validate=True)
    except (ValueError, _binascii_error()):
        return None
    if len(raw) < MIN_PSEUDONYM_KEY_BYTES:
        return None
    return raw


def generate_master_secret_b64() -> str:
    """Generate a fresh base64 256-bit master secret (operator provisioning; never committed)."""
    return base64.b64encode(os.urandom(MIN_PSEUDONYM_KEY_BYTES)).decode("ascii")


def derive_tenant_pseudonym_key(
    master: bytes, *, tenant_id: str, policy_version: str, key_id: str
) -> bytes:
    """Derive a tenant-separated 256-bit pseudonymization key via HKDF-SHA-256.

    Binding ``tenant_id | policy_version | key_id`` into the HKDF ``info`` guarantees: tenant
    separation (different tenant → different key), rotation traceability (different key id →
    different key), and policy versioning — all from the master secret + config (never the DB),
    so pseudonyms are stable across process/node restart and database backup/restore.
    """
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    info = f"{tenant_id}\x1f{policy_version}\x1f{key_id}".encode()
    hkdf = HKDF(algorithm=SHA256(), length=32, salt=_HKDF_SALT, info=info)
    return hkdf.derive(master)


def _binascii_error():
    import binascii

    return binascii.Error
