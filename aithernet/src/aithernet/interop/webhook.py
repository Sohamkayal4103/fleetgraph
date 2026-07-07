"""Signed webhook delivery for external-agent callbacks (Stage 14D).

A callback is an HTTP POST of the canonical event envelope, signed with the node's Ed25519
identity over a canonical description of the request (method, target, timestamp, nonce,
content digest, key id). The receiver verifies integrity + sender identity without any
shared secret. Delivery re-validates the endpoint policy (DNS rebinding protection),
disables redirects by default, and bounds timeouts + response size.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from aithernet.config.settings import ExternalAgentsCallbacksConfig, ExternalAgentsDeliveryConfig
from aithernet.interop.auth import (
    HEADER_AGENT,
    HEADER_DIGEST,
    HEADER_KEY_ID,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    canonical_request_bytes,
    content_digest,
)
from aithernet.interop.endpoints import Resolver, validate_endpoint
from aithernet.transport.identity import NodeIdentity


@dataclass
class DeliveryOutcome:
    """The result of one webhook delivery attempt (sanitized; no response body retained)."""

    ok: bool
    status_code: int | None = None
    failure_category: str | None = None
    detail: str = ""


# Signed-callback header names (distinct from the inbound agent->node headers).
HEADER_EVENT_TYPE = "x-aithernet-event-type"
HEADER_SEQUENCE = "x-aithernet-sequence"
HEADER_MESSAGE_ID = "x-aithernet-message-id"


class WebhookDeliverer:
    """Delivers signed webhook callbacks with SSRF re-validation and bounded I/O."""

    def __init__(
        self,
        identity: NodeIdentity,
        callbacks: ExternalAgentsCallbacksConfig,
        delivery: ExternalAgentsDeliveryConfig,
        *,
        resolver: Resolver | None = None,
        client_factory=None,
    ) -> None:
        self.identity = identity
        self.callbacks = callbacks
        self.delivery = delivery
        self.resolver = resolver
        # Injectable for tests (e.g. an ASGITransport-backed client to a local receiver).
        self._client_factory = client_factory

    def signing_key_id(self) -> str:
        return self.identity.fingerprint

    def _headers(
        self, *, target: str, agent_id: str, body: bytes, envelope: dict
    ) -> dict[str, str]:
        digest = content_digest(body)
        timestamp = str(int(_now_ts()))
        nonce = secrets.token_hex(16)
        message = canonical_request_bytes(
            agent_id=agent_id, key_id=self.identity.fingerprint, method="POST",
            target=target, timestamp=timestamp, nonce=nonce, digest=digest,
        )
        signature = self.identity.sign(message)
        return {
            "content-type": "application/json",
            HEADER_AGENT: agent_id,
            HEADER_KEY_ID: self.identity.fingerprint,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_DIGEST: digest,
            HEADER_SIGNATURE: signature,
            HEADER_EVENT_TYPE: str(envelope.get("event_type", "")),
            HEADER_SEQUENCE: str(envelope.get("sequence", "")),
            HEADER_MESSAGE_ID: str(envelope.get("message_id", "")),
        }

    async def deliver(self, *, url: str, agent_id: str, envelope: dict) -> DeliveryOutcome:
        """Deliver one envelope. Returns a sanitized outcome (never raises)."""
        # Re-validate the endpoint immediately before connecting (rebinding protection).
        policy = validate_endpoint(url, self.callbacks, resolver=self.resolver)
        if not policy.allowed:
            return DeliveryOutcome(
                ok=False, failure_category="endpoint_policy", detail=policy.reason
            )

        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
        target = urlsplit(url).path or "/"
        headers = self._headers(target=target, agent_id=agent_id, body=body, envelope=envelope)

        timeout = httpx.Timeout(
            connect=self.delivery.connect_timeout_seconds,
            read=self.delivery.response_timeout_seconds,
            write=self.delivery.response_timeout_seconds,
            pool=self.delivery.connect_timeout_seconds,
        )
        try:
            client = self._make_client(timeout)
            async with client:
                resp = await client.post(url, content=body, headers=headers)
                # Bound the response body we read; a transport ack only needs the status.
                _ = resp.content[: self.callbacks.maximum_response_bytes]
        except httpx.TimeoutException:
            return DeliveryOutcome(ok=False, failure_category="timeout", detail="request_timeout")
        except httpx.TooManyRedirects:
            return DeliveryOutcome(ok=False, failure_category="redirect", detail="redirect_blocked")
        except httpx.HTTPError as exc:
            return DeliveryOutcome(
                ok=False, failure_category="network", detail=type(exc).__name__
            )

        if 200 <= resp.status_code < 300:
            return DeliveryOutcome(ok=True, status_code=resp.status_code)
        category = "http_4xx" if 400 <= resp.status_code < 500 else "http_5xx"
        return DeliveryOutcome(ok=False, status_code=resp.status_code, failure_category=category)

    def _make_client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(timeout)
        return httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=self.callbacks.redirects_enabled,
            max_redirects=self.callbacks.maximum_redirects,
            verify=True,
        )


def _now_ts() -> float:
    import time

    return time.time()
