"""Phase 1: the pluggable peer-transport interface.

Every transport (loopback, IP, hosted relay, simulated RF, RF OTA) implements :class:`PeerTransport`
and carries the SAME canonical :class:`~aithernet.transport.envelope.MessageEnvelope`. Selection and
health are recorded so routing is deterministic and auditable; the modem layer never sees mission
semantics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from aithernet.transport.envelope import MessageEnvelope


class DeliveryStatus(str, Enum):
    DELIVERED = "delivered"          # the far side acknowledged receipt
    SENT = "sent"                    # emitted, no acknowledgement model (simplex)
    FAILED = "failed"                # exhausted retries / unrecoverable
    REJECTED = "rejected"            # transport refused (policy/auth/capability)
    EXPIRED = "expired"              # message expiry passed before delivery


@dataclass(frozen=True)
class DeliveryReceipt:
    """The outcome of one :meth:`PeerTransport.send`. Never carries secret material."""

    transport_id: str
    message_id: str
    status: DeliveryStatus
    correlation_id: str | None = None
    profile_id: str | None = None
    frames_sent: int = 0
    frames_acked: int = 0
    retries: int = 0
    latency_ms: float | None = None
    detail: str = ""
    simulated: bool = True            # software/sim by default; physical paths set this False

    def to_dict(self) -> dict:
        return {
            "transport_id": self.transport_id, "message_id": self.message_id,
            "status": self.status.value, "correlation_id": self.correlation_id,
            "profile_id": self.profile_id, "frames_sent": self.frames_sent,
            "frames_acked": self.frames_acked, "retries": self.retries,
            "latency_ms": self.latency_ms, "detail": self.detail, "simulated": self.simulated,
        }


@dataclass(frozen=True)
class TransportHealth:
    transport_id: str
    healthy: bool
    detail: str = ""
    last_success_ms: float | None = None
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        return {"transport_id": self.transport_id, "healthy": self.healthy,
                "detail": self.detail, "last_success_ms": self.last_success_ms,
                "consecutive_failures": self.consecutive_failures}


@dataclass(frozen=True)
class TransportCapabilities:
    """What a transport can do — used by deterministic selection. No hardware identity here."""

    transport_id: str
    kind: str                          # ip | loopback | relay | simulated_rf | rf_ota
    simplex: bool = False              # True when there is no return/ack channel
    supports_ack: bool = True
    supports_fragmentation: bool = True
    max_message_bytes: int = 1 << 20
    profiles: tuple[str, ...] = field(default_factory=tuple)
    tx_requires_authorization: bool = False   # physical RF sets this True
    physical: bool = False

    def to_dict(self) -> dict:
        return {"transport_id": self.transport_id, "kind": self.kind, "simplex": self.simplex,
                "supports_ack": self.supports_ack,
                "supports_fragmentation": self.supports_fragmentation,
                "max_message_bytes": self.max_message_bytes, "profiles": list(self.profiles),
                "tx_requires_authorization": self.tx_requires_authorization,
                "physical": self.physical}


@runtime_checkable
class PeerTransport(Protocol):
    """A pluggable carrier for canonical signed peer envelopes."""

    transport_id: str

    async def send(self, envelope: MessageEnvelope) -> DeliveryReceipt: ...

    def receive(self) -> AsyncIterator[MessageEnvelope]: ...

    async def health(self) -> TransportHealth: ...

    async def capabilities(self) -> TransportCapabilities: ...
