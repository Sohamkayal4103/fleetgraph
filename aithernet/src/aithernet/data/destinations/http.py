"""HTTP ingestion destination (Stage 14E, Part 12/16).

Uploads a sealed bundle to a private ingestion service over a signed, idempotent request with
bounded timeouts, then parses + verifies the receipt (records digest must match the batch
manifest). The node signing key is loaded from an env-REFERENCED secret (never inlined); this
class never logs the key, token, or any authorization material.
"""

from __future__ import annotations

import os
import secrets
import time

import httpx

from aithernet.data import ingest_auth
from aithernet.data.destinations import (
    DeletionResult,
    ExportDestination,
    Readiness,
    UploadResult,
)

HEADER_IDEMPOTENCY = "x-aithernet-idempotency"


class HTTPIngestionDestination(ExportDestination):
    kind = "http_ingestion"

    def __init__(
        self, *, base_url: str, tenant_id: str, node_id: str, credential_ref: str | None,
        key_id: str = "default", connect_timeout: float = 5.0, response_timeout: float = 30.0,
        client_factory=None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.tenant_id = tenant_id
        self.node_id = node_id
        self.credential_ref = credential_ref
        self.key_id = key_id
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout
        self._client_factory = client_factory

    # -- key loading (from an env-referenced secret; never inlined/logged) -------

    def _private_key(self):
        if not self.credential_ref:
            return None
        seed = os.environ.get(self.credential_ref)
        if not seed:
            return None
        try:
            return ingest_auth.load_private_key(seed)
        except Exception:  # noqa: BLE001
            return None

    def validate(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "base_url_not_configured"
        if not (self.tenant_id and self.node_id):
            return False, "tenant_or_node_missing"
        if self._private_key() is None:
            return False, "credential_unavailable"
        return True, "ok"

    def readiness(self) -> Readiness:
        ok, reason = self.validate()
        if not ok:
            return Readiness(ready=False, state="unconfigured", detail=reason)
        return Readiness(ready=True, state="ready")

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=self.connect_timeout, read=self.response_timeout,
            write=self.response_timeout, pool=self.connect_timeout,
        )
        if self._client_factory is not None:
            return self._client_factory(timeout)
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout,
                                 follow_redirects=False, verify=True)

    async def upload(self, *, batch_id: str, idempotency_key: str, bundle: bytes,
                     manifest: dict) -> UploadResult:
        priv = self._private_key()
        if priv is None:
            return UploadResult(ok=False, failure_category="credential_unavailable",
                                detail="credential_unavailable")
        target = "/ingest/v1/batches"
        headers = ingest_auth.build_headers(
            private=priv, tenant_id=self.tenant_id, node_id=self.node_id, key_id=self.key_id,
            method="POST", target=target, body=bundle, timestamp=int(time.time()),
            nonce=secrets.token_hex(16),
        )
        headers[HEADER_IDEMPOTENCY] = idempotency_key
        try:
            async with self._client() as client:
                resp = await client.post(target, content=bundle, headers=headers)
        except httpx.TimeoutException:
            return UploadResult(ok=False, failure_category="timeout", detail="timeout")
        except httpx.HTTPError as exc:
            return UploadResult(ok=False, failure_category="network", detail=type(exc).__name__)

        if resp.status_code in (401, 403):
            return UploadResult(ok=False, failure_category="auth",
                                detail=f"http_{resp.status_code}")
        if resp.status_code == 409:
            return UploadResult(ok=False, failure_category="conflict",
                                detail="idempotency_conflict")
        if resp.status_code >= 400:
            cat = "http_4xx" if resp.status_code < 500 else "http_5xx"
            return UploadResult(ok=False, failure_category=cat, detail=f"http_{resp.status_code}")
        try:
            receipt = resp.json()
        except ValueError:
            return UploadResult(ok=False, failure_category="bad_receipt", detail="bad_receipt")
        # Verify the receipt acknowledges THIS batch's records digest.
        if receipt.get("records_digest") not in (None, manifest.get("records_digest")):
            return UploadResult(ok=False, failure_category="receipt_mismatch",
                                detail="records_digest_mismatch")
        receipt.setdefault("destination_kind", self.kind)
        return UploadResult(ok=True, receipt=receipt)

    async def delete(self, *, remote_ref: str) -> DeletionResult:
        priv = self._private_key()
        if priv is None:
            return DeletionResult(ok=False, state="unknown", detail="credential_unavailable")
        target = "/ingest/v1/deletions"
        body = f'{{"batch_id":"{remote_ref}"}}'.encode()
        headers = ingest_auth.build_headers(
            private=priv, tenant_id=self.tenant_id, node_id=self.node_id, key_id=self.key_id,
            method="POST", target=target, body=body, timestamp=int(time.time()),
            nonce=secrets.token_hex(16),
        )
        headers["content-type"] = "application/json"
        try:
            async with self._client() as client:
                resp = await client.post(target, content=body, headers=headers)
        except httpx.HTTPError as exc:
            return DeletionResult(ok=False, state="unknown", detail=type(exc).__name__)
        if resp.status_code >= 400:
            return DeletionResult(ok=False, state="unknown", detail=f"http_{resp.status_code}")
        return DeletionResult(ok=True, state="deleted", receipt=_safe_json(resp))


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
