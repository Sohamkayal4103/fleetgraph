"""Authenticated inter-node agent transport (Stage 13A).

Turns the existing external-agent records into a real, authenticated network transport:
a stable Ed25519 node identity, signed canonical envelopes, explicit peer trust, a durable
outbox/inbox with idempotent deduplication, signed delivery acknowledgements, and a managed
delivery worker. This package establishes transport reliability + identity ONLY — it never
executes inbound messages as missions/routes/tools (Stage 13B adds coordinator-driven peer
messaging and reply-resumed missions).
"""

from __future__ import annotations

from aithernet.transport.service import TransportService

__all__ = ["TransportService"]
