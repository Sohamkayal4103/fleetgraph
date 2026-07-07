"""Callback-endpoint policy + SSRF prevention for external-agent webhooks (Stage 14D).

Callback registration and delivery are a high-risk network boundary. Every URL is
normalized and validated against an explicit policy, and every *resolved IP address* is
checked — at registration AND again immediately before each delivery — so DNS rebinding
cannot redirect a delivery to a prohibited destination after approval.

DNS resolution is performed through an injectable ``Resolver`` so tests can drive
rebinding and prohibited-address scenarios deterministically without real network access.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aithernet.config.settings import ExternalAgentsCallbacksConfig

#: A resolver maps a hostname to a list of IP-address strings.
Resolver = Callable[[str], list[str]]


def system_resolver(host: str) -> list[str]:
    """Default resolver using the OS resolver (A + AAAA)."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


@dataclass
class EndpointPolicyResult:
    """The sanitized outcome of validating one callback endpoint."""

    allowed: bool
    reason: str = "ok"
    scheme: str = ""
    host: str = ""
    port: int | None = None
    normalized_url: str = ""
    resolved: list[str] = field(default_factory=list)

    def sanitized(self) -> dict:
        """A dict safe to persist/return (no secrets; counts not raw internals)."""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "resolved_count": len(self.resolved),
        }


def _ip_is_prohibited(ip: ipaddress._BaseAddress) -> str | None:
    if ip.is_loopback:
        return "loopback_destination"
    if ip.is_link_local:  # covers 169.254.0.0/16 incl. the cloud metadata address
        return "link_local_destination"
    if ip.is_multicast:
        return "multicast_destination"
    if ip.is_unspecified:
        return "unspecified_destination"
    if ip.is_reserved:
        return "reserved_destination"
    if isinstance(ip, ipaddress.IPv4Address) and ip == ipaddress.IPv4Address("255.255.255.255"):
        return "broadcast_destination"
    if ip.is_private:
        return "private_destination"
    return None


def _allowlisted(
    ip: ipaddress._BaseAddress, host: str, policy: ExternalAgentsCallbacksConfig
) -> bool:
    if host in policy.allowed_hosts:
        return True
    for cidr in policy.allowed_cidrs:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def validate_endpoint(
    url: str,
    policy: ExternalAgentsCallbacksConfig,
    *,
    resolver: Resolver | None = None,
) -> EndpointPolicyResult:
    """Validate a callback URL against the policy, resolving + checking every address."""
    resolver = resolver or system_resolver

    if not url or len(url) > policy.maximum_url_length:
        return EndpointPolicyResult(allowed=False, reason="url_length")

    try:
        parts = urlsplit(url)
    except ValueError:
        return EndpointPolicyResult(allowed=False, reason="malformed_url")

    scheme = (parts.scheme or "").lower()
    if scheme not in ("https", "http"):
        return EndpointPolicyResult(allowed=False, reason="unsupported_scheme", scheme=scheme)
    if scheme == "http" and not policy.allow_private_networks:
        # http is only permitted under an explicit private-development policy.
        return EndpointPolicyResult(allowed=False, reason="http_not_allowed", scheme=scheme)
    if policy.https_required and scheme != "https" and not policy.allow_private_networks:
        return EndpointPolicyResult(allowed=False, reason="https_required", scheme=scheme)

    if parts.username or parts.password or "@" in (parts.netloc or ""):
        return EndpointPolicyResult(allowed=False, reason="userinfo_not_allowed", scheme=scheme)
    if parts.fragment:
        return EndpointPolicyResult(allowed=False, reason="fragment_not_allowed", scheme=scheme)

    host = parts.hostname
    if not host or "*" in host or host != host.strip() or " " in host:
        return EndpointPolicyResult(allowed=False, reason="bad_host", scheme=scheme)

    try:
        port = parts.port
    except ValueError:
        return EndpointPolicyResult(allowed=False, reason="bad_port", scheme=scheme, host=host)
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if policy.allowed_ports and effective_port not in policy.allowed_ports:
        return EndpointPolicyResult(
            allowed=False, reason="port_not_allowed", scheme=scheme, host=host, port=effective_port
        )

    if policy.allowed_hosts and host not in policy.allowed_hosts:
        return EndpointPolicyResult(
            allowed=False, reason="host_not_allowlisted", scheme=scheme, host=host,
            port=effective_port,
        )

    # Resolve and validate EVERY address (rebinding protection re-runs this before delivery).
    try:
        addresses = resolver(host)
    except Exception:  # noqa: BLE001 — resolution failure is a clean policy reason, not a crash
        return EndpointPolicyResult(
            allowed=False, reason="dns_resolution_failed", scheme=scheme, host=host,
            port=effective_port,
        )
    if not addresses:
        return EndpointPolicyResult(
            allowed=False, reason="dns_no_address", scheme=scheme, host=host, port=effective_port
        )

    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return EndpointPolicyResult(
                allowed=False, reason="bad_resolved_address", scheme=scheme, host=host,
                port=effective_port, resolved=addresses,
            )
        prohibited = _ip_is_prohibited(ip)
        if prohibited is not None:
            # An allowlisted private host/CIDR is permitted only under the private policy.
            if policy.allow_private_networks and _allowlisted(ip, host, policy):
                continue
            return EndpointPolicyResult(
                allowed=False, reason=prohibited, scheme=scheme, host=host,
                port=effective_port, resolved=addresses,
            )

    normalized = f"{scheme}://{host}:{effective_port}{parts.path or ''}"
    if parts.query:
        normalized += f"?{parts.query}"
    return EndpointPolicyResult(
        allowed=True, reason="ok", scheme=scheme, host=host, port=effective_port,
        normalized_url=normalized, resolved=addresses,
    )
