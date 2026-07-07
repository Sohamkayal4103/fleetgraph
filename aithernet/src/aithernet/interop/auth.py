"""Signed-request authentication for external agents (Stage 14D).

An external agent authenticates each request by signing a *canonical* description of the
request with its Ed25519 private key. No secret is transmitted with the request — only a
detached signature over deterministic bytes. The canonical message binds:

    agent id + key id + method + request target + timestamp + nonce + body digest

which provides request integrity, identity, an expiration window, and replay protection
(timestamp skew + a persisted nonce cache). Verification is deterministic and returns a
stable failure ``code`` (never a stack trace).

An optional operator-issued bearer credential (hashed at rest, shown once) is supported as
an alternative when ``authentication.bearer_tokens_enabled`` is set; it carries no nonce or
signature and is matched by constant-time hash comparison.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aithernet.state.models import InteropAgent, InteropCredential, utcnow
from aithernet.transport.canonical import canonical_bytes
from aithernet.transport.identity import verify_signature

# Canonical header names (case-insensitive on the wire; lower-cased here).
HEADER_AGENT = "x-aithernet-agent"
HEADER_KEY_ID = "x-aithernet-key-id"
HEADER_TIMESTAMP = "x-aithernet-timestamp"
HEADER_NONCE = "x-aithernet-nonce"
HEADER_DIGEST = "x-aithernet-content-digest"
HEADER_SIGNATURE = "x-aithernet-signature"
HEADER_AUTHORIZATION = "authorization"


def content_digest(body: bytes) -> str:
    """Return the canonical content digest of a request/response body."""
    return "sha256:" + hashlib.sha256(body).hexdigest()


def hash_secret(secret: str) -> str:
    """Hash a bearer secret for storage (never store or log the plaintext)."""
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def canonical_request_bytes(
    *,
    agent_id: str,
    key_id: str,
    method: str,
    target: str,
    timestamp: str,
    nonce: str,
    digest: str,
) -> bytes:
    """Deterministic bytes both sides sign/verify (excludes the signature itself)."""
    return canonical_bytes(
        {
            "agent_id": agent_id,
            "key_id": key_id,
            "method": method.upper(),
            "target": target,
            "timestamp": str(timestamp),
            "nonce": nonce,
            "content_digest": digest,
        }
    )


@dataclass
class AuthContext:
    """The outcome of authenticating one inbound agent request."""

    ok: bool
    code: str = "ok"
    agent: InteropAgent | None = None
    agent_id: str | None = None
    key_id: str | None = None
    nonce: str | None = None
    nonce_expires_at: datetime | None = None
    method: str = "ed25519"
    detail: str = ""
    # Permissions snapshot (copied so callers don't hold an ORM row across sessions).
    permissions: list[str] = field(default_factory=list)


def _check_credential_validity(cred: InteropCredential, now: datetime) -> str | None:
    if cred.status != "active":
        return "key_revoked"
    if cred.expires_at is not None and cred.expires_at < now:
        return "key_expired"
    return None


def verify_signed_request(
    *,
    agent: InteropAgent | None,
    credentials: list[InteropCredential],
    headers: dict[str, str],
    method: str,
    target: str,
    body: bytes,
    allowed_skew_seconds: int,
    nonce_seen: bool,
    nonce_retention_seconds: int,
    now: datetime | None = None,
) -> AuthContext:
    """Verify a canonically signed agent request. Pure: no I/O, deterministic codes.

    ``credentials`` are the agent's active Ed25519 credentials (the caller supplies them so
    this stays I/O-free and unit-testable). ``nonce_seen`` is whether the (agent, nonce) pair
    already exists in the replay cache. On success the caller must persist the nonce.
    """
    now = now or utcnow()
    low = {k.lower(): v for k, v in headers.items()}

    agent_id = low.get(HEADER_AGENT)
    key_id = low.get(HEADER_KEY_ID)
    timestamp = low.get(HEADER_TIMESTAMP)
    nonce = low.get(HEADER_NONCE)
    digest = low.get(HEADER_DIGEST)
    signature = low.get(HEADER_SIGNATURE)

    if not all([agent_id, key_id, timestamp, nonce, digest, signature]):
        return AuthContext(ok=False, code="missing_auth_header")

    if agent is None:
        return AuthContext(ok=False, code="unknown_agent", agent_id=agent_id, key_id=key_id)
    if agent.status == "disabled":
        return AuthContext(ok=False, code="agent_disabled", agent_id=agent_id, key_id=key_id)
    if agent.status == "revoked":
        return AuthContext(ok=False, code="agent_revoked", agent_id=agent_id, key_id=key_id)
    if agent.id != agent_id:
        return AuthContext(ok=False, code="agent_mismatch", agent_id=agent_id, key_id=key_id)

    cred = next((c for c in credentials if c.key_id == key_id and c.kind == "ed25519"), None)
    if cred is None or not cred.public_key:
        return AuthContext(ok=False, code="unknown_key_id", agent_id=agent_id, key_id=key_id)
    invalid = _check_credential_validity(cred, now)
    if invalid is not None:
        return AuthContext(ok=False, code=invalid, agent_id=agent_id, key_id=key_id)

    # Timestamp / expiration / clock-skew handling.
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return AuthContext(ok=False, code="bad_timestamp", agent_id=agent_id, key_id=key_id)
    request_time = datetime.fromtimestamp(ts, tz=now.tzinfo)
    delta = (now - request_time).total_seconds()
    if delta > allowed_skew_seconds:
        return AuthContext(ok=False, code="timestamp_expired", agent_id=agent_id, key_id=key_id)
    if delta < -allowed_skew_seconds:
        return AuthContext(ok=False, code="timestamp_in_future", agent_id=agent_id, key_id=key_id)

    # Body integrity.
    if digest != content_digest(body):
        return AuthContext(ok=False, code="body_digest_mismatch", agent_id=agent_id, key_id=key_id)

    # Signature over the canonical request.
    message = canonical_request_bytes(
        agent_id=agent_id, key_id=key_id, method=method, target=target,
        timestamp=timestamp, nonce=nonce, digest=digest,
    )
    if not verify_signature(cred.public_key, message, signature):
        return AuthContext(ok=False, code="invalid_signature", agent_id=agent_id, key_id=key_id)

    # Replay protection — last, so a forged signature never poisons the nonce cache.
    if nonce_seen:
        return AuthContext(ok=False, code="nonce_replayed", agent_id=agent_id, key_id=key_id)

    return AuthContext(
        ok=True,
        code="ok",
        agent=agent,
        agent_id=agent_id,
        key_id=key_id,
        nonce=nonce,
        nonce_expires_at=now + timedelta(seconds=nonce_retention_seconds),
        method="ed25519",
        permissions=list(agent.permissions_json or []),
    )


def verify_bearer(
    *,
    agent: InteropAgent | None,
    credentials: list[InteropCredential],
    authorization_header: str | None,
    now: datetime | None = None,
) -> AuthContext:
    """Verify an ``Authorization: Bearer <token>`` credential (constant-time hash compare)."""
    now = now or utcnow()
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        return AuthContext(ok=False, code="missing_auth_header")
    if agent is None:
        return AuthContext(ok=False, code="unknown_agent")
    if agent.status != "active":
        return AuthContext(ok=False, code=f"agent_{agent.status}")
    token = authorization_header.split(" ", 1)[1].strip()
    token_hash = hash_secret(token)
    for cred in credentials:
        if cred.kind != "bearer" or cred.status != "active" or not cred.token_hash:
            continue
        if cred.expires_at is not None and cred.expires_at < now:
            continue
        if hmac.compare_digest(cred.token_hash, token_hash):
            return AuthContext(
                ok=True, code="ok", agent=agent, agent_id=agent.id, key_id=cred.key_id,
                method="bearer", permissions=list(agent.permissions_json or []),
            )
    return AuthContext(ok=False, code="invalid_bearer", agent_id=agent.id)
