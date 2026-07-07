"""Shared helpers for Stage 13A agent-transport tests (no network/Gemini/Codex/GNU Radio).

Provides in-process node runtimes with isolated temp identity dirs + databases, a routing
fake transport that delivers straight into a target node's authenticated inbound handler,
and small failure-injecting transports for the delivery-worker tests.
"""

from __future__ import annotations

from aithernet.config.settings import (
    AgentTransportConfig,
    IdentityConfig,
    NodeConfig,
    TransportInboundConfig,
    TransportLocalDevelopmentConfig,
    TransportOutboundConfig,
)
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.transport import AgentMessageSendRequest, PeerCreate, PeerRole
from aithernet.transport.errors import (
    TransportConnectionError,
    TransportError,
    TransportHTTPError,
    TransportTimeoutError,
)
from aithernet.transport.http_transport import TransportResponse

# Map an inbound TransportError code -> (http status, retryable) as a real peer would respond.
_CODE_HTTP = {
    "peer_not_trusted": (403, False),
    "key_mismatch": (403, False),
    "invalid_signature": (401, False),
    "recipient_mismatch": (421, False),
    "expired_or_skewed": (400, False),
    "message_id_collision": (409, False),
    "malformed_envelope": (400, False),
    "unsupported_protocol_version": (400, False),
    "payload_too_large": (413, False),
}


def make_runtime(tmp_path, node_id: str, name: str, **outbound) -> NodeRuntime:
    """Build a transport-enabled node runtime with an isolated identity dir + database."""
    base = tmp_path / node_id
    base.mkdir(parents=True, exist_ok=True)
    transport = AgentTransportConfig(
        identity=IdentityConfig(state_directory=str(base / "identity")),
        inbound=TransportInboundConfig(),
        outbound=TransportOutboundConfig(enabled=outbound.pop("worker_enabled", False), **outbound),
        local_development=TransportLocalDevelopmentConfig(allow_insecure_http=True),
    )
    config = NodeConfig(
        node_id=node_id,
        node_name=name,
        database_url=f"sqlite:///{base / 'node.db'}",
        agent_transport=transport,
    )
    return NodeRuntime.from_config(config)


async def init_and_peer(a: NodeRuntime, b: NodeRuntime, *, trust: bool = True) -> tuple[str, str]:
    """Initialize identities and add each node as a peer of the other. Returns (b_peer_on_a,
    a_peer_on_b). When ``trust`` is False the peers are left untrusted."""
    ia = await a.transport.initialize_identity()
    ib = await b.transport.initialize_identity()
    pb = await a.transport.create_peer(
        PeerCreate(name="B", role=PeerRole.NODE, endpoint_url="http://b.local",
                   expected_node_id=b.config.node_id, public_key=ib.public_key_b64)
    )
    pa = await b.transport.create_peer(
        PeerCreate(name="A", role=PeerRole.NODE, endpoint_url="http://a.local",
                   expected_node_id=a.config.node_id, public_key=ia.public_key_b64)
    )
    if trust:
        await a.transport.trust_peer(pb, public_key=ib.public_key_b64)
        await b.transport.trust_peer(pa, public_key=ia.public_key_b64)
    return pb, pa


class RoutingTransport:
    """Fake transport that delivers straight into ``target``'s inbound handler.

    Mirrors a real peer: an inbound rejection becomes an HTTP error with the right status and
    retryability. ``ack_override`` lets a test simulate a bad/forged acknowledgement.
    """

    def __init__(self, target: NodeRuntime, *, ack_override: dict | None = None) -> None:
        self.target = target
        self.ack_override = ack_override
        self.calls = 0

    async def deliver(self, *, endpoint, envelope, tls_verify, allow_insecure_http):
        self.calls += 1
        try:
            ack = await self.target.transport.handle_inbound(envelope)
        except TransportError as exc:
            http_status, retryable = _CODE_HTTP.get(exc.code, (400, False))
            raise TransportHTTPError(
                str(exc), status_code=http_status, retryable=retryable
            ) from exc
        return TransportResponse(status_code=200, body=self.ack_override or ack)

    async def fetch_manifest(self, *, endpoint, tls_verify, allow_insecure_http):
        return self.target.transport.build_local_manifest()

    async def healthcheck(self, *, endpoint, tls_verify, allow_insecure_http):
        return {"status": "ok"}


class FailingTransport:
    """Fake transport that always raises a chosen transport error (retry-path tests)."""

    def __init__(self, exc: TransportError) -> None:
        self.exc = exc
        self.calls = 0

    async def deliver(self, *, endpoint, envelope, tls_verify, allow_insecure_http):
        self.calls += 1
        raise self.exc

    async def fetch_manifest(self, **_):  # pragma: no cover - unused
        raise self.exc

    async def healthcheck(self, **_):  # pragma: no cover - unused
        raise self.exc


def conn_error() -> TransportConnectionError:
    return TransportConnectionError("connection refused")


def timeout_error() -> TransportTimeoutError:
    return TransportTimeoutError("timed out")


def http_5xx() -> TransportHTTPError:
    return TransportHTTPError("server error", status_code=503, retryable=True)


def http_4xx() -> TransportHTTPError:
    return TransportHTTPError("bad request", status_code=400, retryable=False)


async def send(a: NodeRuntime, peer_id: str, *, subject="hi", text="hello", **kw) -> str:
    """Create one outbound message and return its outbox record id."""
    return await a.transport.create_outbound_message(
        AgentMessageSendRequest(peer_id=peer_id, subject=subject, text=text, **kw)
    )
