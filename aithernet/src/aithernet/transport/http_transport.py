"""Outbound HTTP agent transport (Stage 13A, Part J).

A real HTTP transport that POSTs signed envelopes to a peer's ``/agent/v1/messages`` and
fetches manifests/health. It enforces bounded timeouts, TLS verification for HTTPS,
response-size limits, no cross-host redirects, and structured secret-redacted errors. It
NEVER touches mission lifecycle and never leaks environment or auth material — outbound
requests carry only the signed envelope body (no bearer/secret headers).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

from aithernet.sanitize import redact_secrets
from aithernet.transport.errors import (
    EnvelopeError,
    PayloadTooLargeError,
    TransportConnectionError,
    TransportError,
    TransportHTTPError,
    TransportTimeoutError,
)

#: HTTP statuses that warrant a transport retry (transient). All other 4xx are permanent.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

_INBOUND_MESSAGES_PATH = "/agent/v1/messages"
_MANIFEST_PATH = "/agent/v1/manifest"
_HEALTH_PATH = "/agent/v1/health"


@dataclass(frozen=True)
class TransportResponse:
    """A transport-level response: the peer accepted the HTTP request and returned a body."""

    status_code: int
    body: dict


class AgentTransport(Protocol):
    """The transport abstraction the delivery worker depends on."""

    async def deliver(
        self, *, endpoint: str, envelope: dict, tls_verify: bool, allow_insecure_http: bool
    ) -> TransportResponse: ...

    async def fetch_manifest(
        self, *, endpoint: str, tls_verify: bool, allow_insecure_http: bool
    ) -> dict: ...

    async def healthcheck(
        self, *, endpoint: str, tls_verify: bool, allow_insecure_http: bool
    ) -> dict: ...


def _validate_endpoint(endpoint: str | None, *, allow_insecure_http: bool) -> str:
    """Validate a peer endpoint URL and return its normalized base (no trailing slash)."""
    if not endpoint:
        raise EnvelopeError("Peer has no endpoint URL configured.", code="no_endpoint")
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise EnvelopeError(
            f"Invalid peer endpoint scheme: '{parsed.scheme}'.", code="bad_endpoint"
        )
    if parsed.scheme == "http" and not allow_insecure_http:
        raise EnvelopeError(
            "Plain-HTTP peer endpoint rejected (allow_insecure_http is disabled).",
            code="insecure_http",
        )
    return endpoint.rstrip("/")


class HTTPAgentTransport:
    """Real HTTP transport using httpx with bounded timeouts and TLS verification."""

    def __init__(
        self,
        *,
        connect_timeout_seconds: float,
        request_timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> None:
        self._connect_timeout = connect_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._max_response_bytes = maximum_response_bytes

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self._request_timeout, connect=self._connect_timeout)

    async def deliver(
        self, *, endpoint: str, envelope: dict, tls_verify: bool, allow_insecure_http: bool
    ) -> TransportResponse:
        base = _validate_endpoint(endpoint, allow_insecure_http=allow_insecure_http)
        return await self._request(
            "POST", base + _INBOUND_MESSAGES_PATH, tls_verify=tls_verify, json=envelope
        )

    async def fetch_manifest(
        self, *, endpoint: str, tls_verify: bool, allow_insecure_http: bool
    ) -> dict:
        base = _validate_endpoint(endpoint, allow_insecure_http=allow_insecure_http)
        response = await self._request("GET", base + _MANIFEST_PATH, tls_verify=tls_verify)
        return response.body

    async def healthcheck(
        self, *, endpoint: str, tls_verify: bool, allow_insecure_http: bool
    ) -> dict:
        base = _validate_endpoint(endpoint, allow_insecure_http=allow_insecure_http)
        response = await self._request("GET", base + _HEALTH_PATH, tls_verify=tls_verify)
        return response.body

    async def _request(
        self, method: str, url: str, *, tls_verify: bool, json: dict | None = None
    ) -> TransportResponse:
        """Perform one bounded HTTP request, mapping failures to structured transport errors."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout(),
                verify=tls_verify,
                follow_redirects=False,  # never follow redirects to unrelated hosts
            ) as client:
                response = await client.request(method, url, json=json)
        except httpx.TimeoutException as exc:
            raise TransportTimeoutError(
                f"Transport request timed out: {type(exc).__name__}."
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportConnectionError(
                f"Transport connection failed: {type(exc).__name__}."
            ) from exc

        content = response.content
        if len(content) > self._max_response_bytes:
            raise PayloadTooLargeError("Peer response exceeded the configured size limit.")

        if response.status_code >= 400:
            body_preview = redact_secrets(content[:300].decode("utf-8", errors="replace"))
            retryable = response.status_code in _RETRYABLE_STATUS
            raise TransportHTTPError(
                f"Peer returned HTTP {response.status_code}: {body_preview}",
                status_code=response.status_code,
                retryable=retryable,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise TransportError(
                "Peer returned a non-JSON response.", code="non_json_response", retryable=False
            ) from exc
        if not isinstance(body, dict):
            raise TransportError(
                "Peer returned a non-object JSON body.", code="non_object_response",
                retryable=False,
            )
        return TransportResponse(status_code=response.status_code, body=body)
