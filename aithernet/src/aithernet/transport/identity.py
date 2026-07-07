"""Stable cryptographic node identity (Stage 13A, Part B).

Each node owns a locally generated Ed25519 keypair used to sign every outbound transport
message, acknowledgement, and capability manifest, and to let peers authenticate it. The
private key:

  * is generated locally and stored ONLY in a dedicated identity directory (an XDG state
    location outside the repository by default) — never in the SQLite database;
  * is written with restrictive ``0o600`` permissions inside a ``0o700`` directory;
  * is never returned by an API, printed by the CLI, logged, or placed in an event.

Only the *public* identity document (node id/name, version, public key, fingerprint, ...)
is ever shared.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from aithernet.state.models import utcnow
from aithernet.transport.errors import IdentityError

#: Transport identity scheme + current identity-document version.
IDENTITY_SCHEME = "ed25519"
IDENTITY_VERSION = 1

#: File names inside the identity directory.
_PRIVATE_KEY_FILE = "node_ed25519_private.pem"
_PUBLIC_DOC_FILE = "identity.json"


def default_identity_dir() -> Path:
    """The default identity directory: ``$XDG_STATE_HOME/aithernet/identity`` (outside repo)."""
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "aithernet" / "identity"


def fingerprint_for_public_key(public_key_b64: str) -> str:
    """Return the canonical ``sha256:<hex>`` fingerprint of a base64 raw Ed25519 public key."""
    raw = base64.b64decode(public_key_b64)
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def short_fingerprint(fingerprint: str) -> str:
    """A concise, readable fingerprint form for CLI/UI (never the private key)."""
    body = fingerprint.split(":", 1)[-1]
    return f"sha256:{body[:16]}" if body else fingerprint


def verify_signature(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    """Verify an Ed25519 ``signature_b64`` over ``message`` using a base64 raw public key.

    Returns ``False`` on any verification failure or malformed input — never raises, so
    callers treat an unverifiable signature as a clean rejection.
    """
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        public.verify(base64.b64decode(signature_b64), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


@dataclass(frozen=True)
class NodeIdentity:
    """A loaded node identity: public metadata plus the in-memory private key.

    The private key is held only in memory for signing; it is never serialized by
    :meth:`public_document` and never leaves this object.
    """

    node_id: str
    node_name: str
    identity_version: int
    public_key_b64: str
    fingerprint: str
    created_at: datetime
    enabled: bool
    _private_key: Ed25519PrivateKey
    rotated_at: datetime | None = None

    def sign(self, message: bytes) -> str:
        """Return a base64 Ed25519 signature over ``message``."""
        return base64.b64encode(self._private_key.sign(message)).decode("ascii")

    def public_document(self) -> dict:
        """The shareable public identity document (NEVER includes private material)."""
        doc = {
            "scheme": IDENTITY_SCHEME,
            "identity_version": self.identity_version,
            "node_id": self.node_id,
            "node_name": self.node_name,
            "public_key": self.public_key_b64,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at.isoformat(),
            "enabled": self.enabled,
        }
        if self.rotated_at is not None:
            doc["rotated_at"] = self.rotated_at.isoformat()
        return doc


class IdentityManager:
    """Loads, generates, and persists the node's Ed25519 identity on local disk."""

    def __init__(self, directory: Path, *, node_id: str, node_name: str) -> None:
        self.directory = Path(directory)
        self.node_id = node_id
        self.node_name = node_name

    @property
    def private_key_path(self) -> Path:
        return self.directory / _PRIVATE_KEY_FILE

    @property
    def public_doc_path(self) -> Path:
        return self.directory / _PUBLIC_DOC_FILE

    def exists(self) -> bool:
        return self.private_key_path.is_file()

    # -- load --------------------------------------------------------------------

    def load(self) -> NodeIdentity:
        """Load the identity from disk, raising :class:`IdentityError` if missing/unreadable."""
        if not self.exists():
            raise IdentityError(
                f"No node identity found in '{self.directory}'. Initialize it first.",
                code="identity_missing",
            )
        try:
            pem = self.private_key_path.read_bytes()
            private = serialization.load_pem_private_key(pem, password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise IdentityError(f"Node identity could not be loaded: {exc}") from exc
        if not isinstance(private, Ed25519PrivateKey):
            raise IdentityError("Node identity key is not an Ed25519 private key.")

        public_b64 = self._public_b64(private)
        meta = self._read_public_doc()
        created_at = self._parse_dt(meta.get("created_at")) or utcnow()
        rotated_at = self._parse_dt(meta.get("rotated_at"))
        return NodeIdentity(
            node_id=meta.get("node_id", self.node_id),
            node_name=meta.get("node_name", self.node_name),
            identity_version=int(meta.get("identity_version", IDENTITY_VERSION)),
            public_key_b64=public_b64,
            fingerprint=fingerprint_for_public_key(public_b64),
            created_at=created_at,
            enabled=bool(meta.get("enabled", True)),
            _private_key=private,
            rotated_at=rotated_at,
        )

    def load_or_none(self) -> NodeIdentity | None:
        return self.load() if self.exists() else None

    # -- initialize / rotate -----------------------------------------------------

    def initialize(self, *, rotate: bool = False) -> NodeIdentity:
        """Generate the identity if absent (idempotent); regenerate only when ``rotate``.

        Without ``rotate`` an existing identity is returned unchanged — startup never
        rotates keys. With ``rotate`` a new key is generated and the prior creation time is
        preserved as ``created_at`` while ``rotated_at`` records the rotation.
        """
        if self.exists() and not rotate:
            return self.load()

        prior_created = None
        if self.exists() and rotate:
            with contextlib.suppress(Exception):
                prior_created = self._parse_dt(self._read_public_doc().get("created_at"))

        private = Ed25519PrivateKey.generate()
        public_b64 = self._public_b64(private)
        now = utcnow()
        created_at = prior_created or now
        rotated_at = now if rotate and prior_created is not None else None

        self._write_private_key(private)
        document = {
            "scheme": IDENTITY_SCHEME,
            "identity_version": IDENTITY_VERSION,
            "node_id": self.node_id,
            "node_name": self.node_name,
            "public_key": public_b64,
            "fingerprint": fingerprint_for_public_key(public_b64),
            "created_at": created_at.isoformat(),
            "enabled": True,
        }
        if rotated_at is not None:
            document["rotated_at"] = rotated_at.isoformat()
        self._write_public_doc(document)
        return self.load()

    # -- internal helpers --------------------------------------------------------

    @staticmethod
    def _public_b64(private: Ed25519PrivateKey) -> str:
        raw = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode("ascii")

    def _ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.directory.chmod(stat.S_IRWXU)  # 0o700

    def _write_private_key(self, private: Ed25519PrivateKey) -> None:
        self._ensure_dir()
        pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path = self.private_key_path
        # Create with 0o600 from the start (umask-safe) so the key is never world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, pem)
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600

    def _write_public_doc(self, document: dict) -> None:
        self._ensure_dir()
        self.public_doc_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            self.public_doc_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)

    def _read_public_doc(self) -> dict:
        try:
            return json.loads(self.public_doc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _parse_dt(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
