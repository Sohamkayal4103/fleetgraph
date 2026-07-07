"""Hosted security primitives (Stage 14F §10, §13, §23).

No plaintext secrets are persisted. Passwords use scrypt (a maintained, memory-hard KDF from the
``cryptography`` library — the §10 "equivalent maintained memory-hard password mechanism"). Raw
invitation / enrollment / reset / session tokens are shown once and stored only as SHA-256
digests. Node enrollment proves Ed25519 key possession through the existing signed-challenge
helpers. Release manifests are signed with detached Ed25519 signatures. Comparisons that matter
are constant-time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# Re-exported Ed25519 helpers (single canonical implementation, reused not reinvented).
from aithernet.data.ingest_auth import (  # noqa: F401
    canonical_request_bytes,
    content_digest,
    generate_keypair,
    load_private_key,
    public_key_b64,
    sign,
    verify_signature,
)

# scrypt cost parameters (interactive-login tuned; n is a power of two).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_KEYLEN = 32
_SCRYPT_SALT_BYTES = 16

#: Minimum hosted password length (§10).
MIN_PASSWORD_LENGTH = 12

#: A small bounded set of obviously-compromised/default passwords rejected outright (§10).
_BANNED_PASSWORDS = frozenset(
    {
        "password", "password1", "password123", "12345678", "123456789", "qwertyui",
        "changeme123", "letmein123", "aithernet123", "admin12345", "iloveyou1", "welcome123",
    }
)


# -- random tokens + digests ---------------------------------------------------------------------


def generate_token(nbytes: int = 32) -> str:
    """A URL-safe high-entropy one-time token (shown once; only its digest is stored)."""
    return secrets.token_urlsafe(nbytes)


def token_digest(token: str) -> str:
    """SHA-256 digest of a raw token (what is persisted/indexed — never the token)."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


# -- password hashing (scrypt) -------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = os.urandom(_SCRYPT_SALT_BYTES)
    derived = _scrypt(salt).derive(password.encode("utf-8"))
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        kdf = Scrypt(salt=salt, length=len(expected), n=int(n), r=int(r), p=int(p))
        kdf.verify(password.encode("utf-8"), expected)
        return True
    except Exception:  # noqa: BLE001 — any parse/verify failure is a non-match
        return False


def _scrypt(salt: bytes) -> Scrypt:
    return Scrypt(salt=salt, length=_SCRYPT_KEYLEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)


def password_policy_error(password: str) -> str | None:
    """Return a sanitized policy-violation code, or ``None`` if the password is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return "password_too_short"
    if password.lower() in _BANNED_PASSWORDS:
        return "password_compromised_or_default"
    if password.strip() == "" or len(set(password)) < 4:
        return "password_too_simple"
    return None


# -- session + CSRF signing (HMAC; no third-party dependency) ------------------------------------


def sign_session_token(signing_key: str, session_id: str, secret: str) -> str:
    """A signed opaque session cookie value: ``<session_id>.<hmac>``. The DB stores only a digest
    of this whole value, so a stolen cookie cannot be reconstructed from the database."""
    mac = _hmac(signing_key, f"{session_id}:{secret}")
    return f"{session_id}.{mac}"


def session_id_from_token(token: str) -> str | None:
    parts = token.split(".", 1)
    return parts[0] if len(parts) == 2 and parts[0] else None


def make_csrf_token(signing_key: str, csrf_secret: str) -> str:
    """A CSRF token bound to the session's per-session secret (double-submit + HMAC)."""
    return _hmac(signing_key, f"csrf:{csrf_secret}")


def verify_csrf_token(signing_key: str, csrf_secret: str, presented: str) -> bool:
    return constant_time_equals(make_csrf_token(signing_key, csrf_secret), presented or "")


def _hmac(signing_key: str, message: str) -> str:
    digest = hmac.new(signing_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(digest.digest()).decode("ascii").rstrip("=")


# -- key fingerprints ----------------------------------------------------------------------------


def fingerprint_public_key(public_key_b64_str: str) -> str:
    """A stable, non-secret fingerprint of an Ed25519 public key (fleet display + uniqueness)."""
    try:
        raw = base64.b64decode(public_key_b64_str)
    except Exception:  # noqa: BLE001
        raw = public_key_b64_str.encode("utf-8")
    return "fpr:" + hashlib.sha256(raw).hexdigest()[:32]
