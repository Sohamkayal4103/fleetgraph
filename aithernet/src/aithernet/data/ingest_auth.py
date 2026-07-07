"""Signed-request authentication for node→ingestion batch upload (Stage 14E, Part 16).

Extends the established Ed25519 canonical-signing approach with explicit tenant + node identity.
A batch request signs ``{tenant_id, node_id, key_id, method, target, timestamp, nonce,
content_digest}``. Used by the HTTP destination (to sign) and by the ingestion service (to
verify). It is a SEPARATE scheme from external-agent permissions — a node may only submit as
itself, and tenant/node isolation is enforced server-side.
"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

HEADER_TENANT = "x-aithernet-tenant"
HEADER_NODE = "x-aithernet-node"
HEADER_KEY_ID = "x-aithernet-key-id"
HEADER_TIMESTAMP = "x-aithernet-timestamp"
HEADER_NONCE = "x-aithernet-nonce"
HEADER_DIGEST = "x-aithernet-content-digest"
HEADER_SIGNATURE = "x-aithernet-signature"


def content_digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def canonical_request_bytes(
    *, tenant_id: str, node_id: str, key_id: str, method: str, target: str,
    timestamp: str, nonce: str, digest: str,
) -> bytes:
    return json.dumps(
        {
            "tenant_id": tenant_id, "node_id": node_id, "key_id": key_id,
            "method": method.upper(), "target": target, "timestamp": str(timestamp),
            "nonce": nonce, "content_digest": digest,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def load_private_key(seed_b64: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a base64 32-byte seed (from an env-referenced secret)."""
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64))


def public_key_b64(private: Ed25519PrivateKey) -> str:
    return base64.b64encode(
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")


def generate_keypair() -> tuple[str, str]:
    """Return ``(private_seed_b64, public_key_b64)`` for operator provisioning (never committed)."""
    priv = Ed25519PrivateKey.generate()
    seed = base64.b64encode(
        priv.private_bytes_raw() if hasattr(priv, "private_bytes_raw")
        else _raw_private(priv)
    ).decode("ascii")
    return seed, public_key_b64(priv)


def _raw_private(priv: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives.serialization import (
        Encoding as Enc,
    )
    from cryptography.hazmat.primitives.serialization import (
        NoEncryption,
        PrivateFormat,
    )

    return priv.private_bytes(Enc.Raw, PrivateFormat.Raw, NoEncryption())


def sign(private: Ed25519PrivateKey, message: bytes) -> str:
    return base64.b64encode(private.sign(message)).decode("ascii")


def verify_signature(public_key_b64_str: str, message: bytes, signature_b64: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64_str))
        pub.verify(base64.b64decode(signature_b64), message)
        return True
    except Exception:  # noqa: BLE001
        return False


def build_headers(
    *, private: Ed25519PrivateKey, tenant_id: str, node_id: str, key_id: str,
    method: str, target: str, body: bytes, timestamp: int, nonce: str,
) -> dict[str, str]:
    digest = content_digest(body)
    message = canonical_request_bytes(
        tenant_id=tenant_id, node_id=node_id, key_id=key_id, method=method, target=target,
        timestamp=str(timestamp), nonce=nonce, digest=digest,
    )
    return {
        HEADER_TENANT: tenant_id, HEADER_NODE: node_id, HEADER_KEY_ID: key_id,
        HEADER_TIMESTAMP: str(timestamp), HEADER_NONCE: nonce, HEADER_DIGEST: digest,
        HEADER_SIGNATURE: sign(private, message), "content-type": "application/octet-stream",
    }
