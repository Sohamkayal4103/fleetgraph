"""Reference external-agent client for the Aithernet Stage 14D gateway.

A small, dependency-light demonstration of how a NON-Aithernet program authenticates to a
node, submits idempotent missions, verifies signed webhook callbacks, deduplicates by
message id, acknowledges deliveries, and connects a WebSocket with a replay cursor. It is NOT
a production SDK and introduces no framework — copy what you need.

Requires only ``cryptography`` and ``httpx`` (both already used by Aithernet). Generate a
fresh key with :func:`generate_keypair`; never commit a real private key.

Canonical request signing matches ``aithernet.interop.auth``:

    sign( canonical_json{ agent_id, key_id, method, target, timestamp, nonce, content_digest } )
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _canonical_bytes(obj: dict) -> bytes:
    return json.dumps(
        {k: v for k, v in obj.items() if k != "signature"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def content_digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    """Return ``(private_key, public_key_b64)`` for a fresh Ed25519 identity."""
    private = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    return private, public_b64


def fingerprint(public_key_b64: str) -> str:
    raw = base64.b64decode(public_key_b64)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def sign_canonical(private: Ed25519PrivateKey, message: bytes) -> str:
    return base64.b64encode(private.sign(message)).decode("ascii")


def build_auth_headers(
    *, private: Ed25519PrivateKey, agent_id: str, key_id: str, method: str,
    target: str, body: bytes, timestamp: int | None = None, nonce: str | None = None,
) -> dict[str, str]:
    """Build the X-Aithernet-* signed-request headers for one request."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    nonce = nonce or secrets.token_hex(16)
    digest = content_digest(body)
    message = _canonical_bytes({
        "agent_id": agent_id, "key_id": key_id, "method": method.upper(), "target": target,
        "timestamp": str(timestamp), "nonce": nonce, "content_digest": digest,
    })
    return {
        "X-Aithernet-Agent": agent_id,
        "X-Aithernet-Key-Id": key_id,
        "X-Aithernet-Timestamp": str(timestamp),
        "X-Aithernet-Nonce": nonce,
        "X-Aithernet-Content-Digest": digest,
        "X-Aithernet-Signature": sign_canonical(private, message),
        "Content-Type": "application/json",
    }


def verify_callback(
    *, node_public_key_b64: str, headers: dict[str, str], body: bytes,
    allowed_skew_seconds: int = 300,
) -> tuple[bool, str]:
    """Verify a signed webhook callback from the node (integrity + identity + freshness)."""
    low = {k.lower(): v for k, v in headers.items()}
    required = ["x-aithernet-key-id", "x-aithernet-timestamp", "x-aithernet-nonce",
                "x-aithernet-content-digest", "x-aithernet-signature", "x-aithernet-agent"]
    if not all(h in low for h in required):
        return False, "missing_header"
    if low["x-aithernet-content-digest"] != content_digest(body):
        return False, "body_digest_mismatch"
    try:
        ts = int(low["x-aithernet-timestamp"])
    except ValueError:
        return False, "bad_timestamp"
    if abs(time.time() - ts) > allowed_skew_seconds:
        return False, "timestamp_out_of_window"
    message = _canonical_bytes({
        "agent_id": low["x-aithernet-agent"], "key_id": low["x-aithernet-key-id"],
        "method": "POST", "target": low.get("x-aithernet-target", ""),
        "timestamp": low["x-aithernet-timestamp"], "nonce": low["x-aithernet-nonce"],
        "content_digest": low["x-aithernet-content-digest"],
    })
    # NOTE: the node signs over the request target (path); a receiver that knows its own path
    # can reconstruct it. For demonstration we accept the digest+identity proof below.
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(node_public_key_b64))
        public.verify(base64.b64decode(low["x-aithernet-signature"]), message)
        return True, "ok"
    except Exception:  # noqa: BLE001
        return False, "invalid_signature"


class CallbackDeduper:
    """Tracks seen message ids so a duplicate callback triggers no second logical action."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def is_new(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        return True


class ExternalAgentClient:
    """Thin signed-request client. Pass any object with ``.post``/``.get`` (e.g. httpx)."""

    def __init__(self, http, *, base_url: str, agent_id: str, key_id: str,
                 private: Ed25519PrivateKey) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.key_id = key_id
        self.private = private

    def _headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        return build_auth_headers(
            private=self.private, agent_id=self.agent_id, key_id=self.key_id,
            method=method, target=target, body=body,
        )

    def submit_mission(self, *, objective: str, idempotency_key: str,
                       correlation_id: str | None = None, context: dict | None = None):
        target = "/agent-api/missions"
        payload = {"objective": objective, "idempotency_key": idempotency_key}
        if correlation_id:
            payload["correlation_id"] = correlation_id
        if context:
            payload["context"] = context
        body = json.dumps(payload).encode("utf-8")
        return self.http.post(self.base_url + target, content=body,
                              headers=self._headers("POST", target, body))

    def get_mission(self, mission_id: str):
        target = f"/agent-api/missions/{mission_id}"
        return self.http.get(self.base_url + target, headers=self._headers("GET", target, b""))

    def get_artifact(self, artifact_id: str):
        target = f"/agent-api/artifacts/{artifact_id}"
        return self.http.get(self.base_url + target, headers=self._headers("GET", target, b""))

    def acknowledge(self, *, message_id: str, payload_digest: str | None = None):
        target = "/agent-api/receipts"
        payload = {"message_id": message_id}
        if payload_digest:
            payload["payload_digest"] = payload_digest
        body = json.dumps(payload).encode("utf-8")
        return self.http.post(self.base_url + target, content=body,
                              headers=self._headers("POST", target, body))

    def ws_hello(self, *, subscription_id: str) -> dict:
        """Build the WebSocket ``hello`` frame (auth over the /agent-api/ws target)."""
        headers = build_auth_headers(
            private=self.private, agent_id=self.agent_id, key_id=self.key_id,
            method="GET", target="/agent-api/ws", body=b"",
        )
        return {"type": "hello", "subscription_id": subscription_id, "auth": headers}
