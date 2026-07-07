"""Stage 14D — secure external-agent interoperability gateway.

Authenticated external (non-Aithernet) agents enroll an identity, submit idempotent
missions, and receive durable signed webhook / WebSocket callbacks with replay. This package
is deliberately separate from the Aithernet node-to-node peer transport; it reuses the same
Ed25519 identity + canonical-signing helpers but never duplicates peer delivery.
"""

from __future__ import annotations

from aithernet.interop.service import InteropService

__all__ = ["InteropService"]
