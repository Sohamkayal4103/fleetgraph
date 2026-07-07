"""Node→control-plane HTTP client (Stage 14F §13-§15, §24).

Signs every node-facing request with the node's Ed25519 identity using the canonical
signed-request scheme shared with ingestion. Enrollment proves key possession by signing a server
challenge. Heartbeats are bounded and outbound. Update checks read the signed release manifest and
verify the detached signature against a distributed public key before any download is applied.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx

from aithernet.data.ingest_auth import (
    HEADER_DIGEST,
    HEADER_KEY_ID,
    HEADER_NODE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TENANT,
    HEADER_TIMESTAMP,
    canonical_request_bytes,
    content_digest,
)
from aithernet.transport.identity import NodeIdentity


class HostedClientError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


class HostedClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- enrollment --------------------------------------------------------------------------

    def enroll(self, *, code: str, identity: NodeIdentity, node_id: str,
               software_version: str | None = None, display_name: str | None = None,
               os_family: str | None = None) -> dict:
        challenge = self._post("/v1/node/enroll/challenge",
                               {"code": code, "public_key": identity.public_key_b64})
        signature = identity.sign(challenge["challenge"].encode("utf-8"))
        return self._post("/v1/node/enroll", {
            "code": code, "public_key": identity.public_key_b64, "node_id": node_id,
            "signature": signature, "software_version": software_version,
            "display_name": display_name or node_id, "os_family": os_family,
        })

    # -- signed node requests ----------------------------------------------------------------

    def heartbeat(self, *, identity: NodeIdentity, tenant_id: str, node_id: str,
                  payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self._signed("POST", "/v1/node/heartbeat", identity, tenant_id, node_id, body)

    def get_config(self, *, identity: NodeIdentity, tenant_id: str, node_id: str) -> dict:
        return self._signed("GET", "/v1/node/config", identity, tenant_id, node_id, b"")

    def get_release(self, *, identity: NodeIdentity, tenant_id: str, node_id: str) -> dict:
        return self._signed("GET", "/v1/node/release", identity, tenant_id, node_id, b"")

    # -- research / owner-archive (beta.9) ----------------------------------------------------

    def mint_research_capability(self, *, identity: NodeIdentity, tenant_id: str, node_id: str,
                                 consent: bool = True) -> dict:
        """Mint a Research Upload Capability (consent-gated). Returns the raw token ONCE."""
        body = json.dumps({"consent": bool(consent)}, separators=(",", ":")).encode("utf-8")
        return self._signed("POST", "/v1/node/research/capability", identity, tenant_id, node_id,
                            body)

    def upload_research_package(self, *, identity: NodeIdentity, tenant_id: str, node_id: str,
                                bundle: bytes, capability_token: str,
                                idempotency_key: str | None = None) -> dict:
        """Upload one sealed, sanitized research package to the hosted owner archive (signed)."""
        target = "/v1/node/research/packages"
        headers = self._sign_headers("POST", target, identity, tenant_id, node_id, bundle)
        if capability_token:
            headers["x-aithernet-research-capability"] = capability_token
        if idempotency_key:
            headers["x-aithernet-idempotency"] = idempotency_key
        try:
            resp = httpx.post(self.base_url + target, content=bundle, headers=headers,
                              timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise HostedClientError("unreachable", str(exc)) from exc
        return self._json(resp)

    # -- downloads ---------------------------------------------------------------------------

    def download(self, release_id: str, name: str, *, identity: NodeIdentity | None = None,
                 tenant_id: str | None = None, node_id: str | None = None) -> tuple[bytes, str]:
        # Enrolled-node update downloads are AUTHENTICATED (signed) — artifact bytes are no longer
        # served from a public URL. The verification key remains available publicly.
        if identity is not None and tenant_id and node_id:
            target = f"/v1/node/downloads/{release_id}/{name}"
            headers = self._sign_headers("GET", target, identity, tenant_id, node_id, b"")
            url = self.base_url + target
        else:
            # Public fallback: only works for the verification key (.pub).
            headers = {}
            url = f"{self.base_url}/v1/downloads/{release_id}/{name}"
        resp = httpx.get(url, headers=headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise HostedClientError("download_failed", str(resp.status_code))
        return resp.content, resp.headers.get("x-aithernet-sha256", "")

    # -- internals ---------------------------------------------------------------------------

    def _sign_headers(self, method: str, target: str, identity: NodeIdentity, tenant_id: str,
                      node_id: str, body: bytes) -> dict:
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        digest = content_digest(body)
        message = canonical_request_bytes(
            tenant_id=tenant_id, node_id=node_id, key_id="default", method=method,
            target=target, timestamp=timestamp, nonce=nonce, digest=digest)
        return {
            HEADER_TENANT: tenant_id, HEADER_NODE: node_id, HEADER_KEY_ID: "default",
            HEADER_TIMESTAMP: timestamp, HEADER_NONCE: nonce, HEADER_DIGEST: digest,
            HEADER_SIGNATURE: identity.sign(message), "content-type": "application/octet-stream",
        }

    def _signed(self, method: str, target: str, identity: NodeIdentity, tenant_id: str,
                node_id: str, body: bytes) -> dict:
        headers = self._sign_headers(method, target, identity, tenant_id, node_id, body)
        url = self.base_url + target
        try:
            resp = httpx.request(method, url, content=body, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise HostedClientError("unreachable", str(exc)) from exc
        return self._json(resp)

    def _post(self, target: str, payload: dict) -> dict:
        try:
            resp = httpx.post(self.base_url + target, json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise HostedClientError("unreachable", str(exc)) from exc
        return self._json(resp)

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            detail = None
            try:
                detail = resp.json().get("detail")
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            raise HostedClientError("hosted_error", detail)
        return resp.json()
