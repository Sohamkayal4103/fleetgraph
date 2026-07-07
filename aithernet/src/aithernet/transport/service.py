"""The node-owned agent-transport service (Stage 13A).

``TransportService`` ties identity, peer trust, the durable outbox/inbox, the signed
inbound handler, and outbound delivery together. It is constructed by the node runtime and
started/stopped by the FastAPI lifespan. It NEVER executes inbound messages as missions,
routes, tool calls, or shell operations — Stage 13A is transport reliability + identity
only. Coordinator-driven peer selection and reply-resumed missions are Stage 13B.
"""

from __future__ import annotations

import contextlib
import random
from datetime import datetime, timedelta
from pathlib import Path

from aithernet.sanitize import redact_secrets
from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import (
    AgentInboxRepository,
    AgentOutboxRepository,
    MeshRepository,
    PeerRepository,
)
from aithernet.transport import events as ev
from aithernet.transport.ack import AckStatus, build_ack, verify_ack
from aithernet.transport.envelope import (
    DELIVERABLE_KINDS,
    build_envelope,
    is_expired,
    parse_envelope,
    validate_recipient,
    validate_timing,
)
from aithernet.transport.errors import (
    EnvelopeError,
    IdentityError,
    KeyMismatchError,
    MeshAuthorizationError,
    MessageIdCollisionError,
    PeerTrustError,
    SignatureError,
    TransportError,
)
from aithernet.transport.identity import (
    IdentityManager,
    NodeIdentity,
    default_identity_dir,
    fingerprint_for_public_key,
)
from aithernet.transport.manifest import (
    CAP_AGENT_TRANSPORT,
    CAP_CODING_AGENT,
    CAP_COORDINATOR_REASONING,
    CAP_GNURADIO_MCP,
    CAP_MISSION_ACCEPTANCE,
    build_manifest,
    verify_manifest,
)


def _short_err(text: str | None, limit: int = 400) -> str | None:
    if not text:
        return text
    return redact_secrets(text)[:limit]


class TransportService:
    """Owns identity, peers, outbox/inbox, the inbound handler, and outbound delivery."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.config = runtime.config.agent_transport
        self.node_id = runtime.config.node_id
        self.node_name = runtime.config.node_name
        self._identity: NodeIdentity | None = None
        self._identity_manager = IdentityManager(
            self._identity_dir(), node_id=self.node_id, node_name=self.node_name
        )
        # Built lazily so importing the service never opens a network client.
        self._transport = None
        self._worker = None

    # -- identity ----------------------------------------------------------------

    def _identity_dir(self) -> Path:
        configured = self.config.identity.state_directory
        return Path(configured) if configured else default_identity_dir()

    @property
    def identity(self) -> NodeIdentity | None:
        return self._identity

    def ensure_identity(self) -> NodeIdentity | None:
        """Load the on-disk identity into memory if present (no generation)."""
        if self._identity is None and self._identity_manager.exists():
            self._identity = self._identity_manager.load()
        return self._identity

    def require_identity(self) -> NodeIdentity:
        identity = self.ensure_identity()
        if identity is None:
            raise IdentityError("Node identity is not initialized.", code="identity_missing")
        return identity

    async def initialize_identity(self, *, rotate: bool = False) -> NodeIdentity:
        """Generate the identity if absent (idempotent); rotate only on explicit request."""
        existed = self._identity_manager.exists()
        identity = self._identity_manager.initialize(rotate=rotate)
        self._identity = identity
        if not existed or rotate:
            await self._emit(
                ev.EVENT_IDENTITY_INITIALIZED,
                f"Node identity {'rotated' if rotate else 'initialized'} for {self.node_id}.",
                payload={"fingerprint": identity.fingerprint, "rotated": rotate},
            )
        return identity

    def identity_status(self) -> dict:
        identity = self.ensure_identity()
        if identity is None:
            return {"initialized": False, "state_directory": str(self._identity_dir())}
        from aithernet.transport.identity import short_fingerprint

        return {
            "initialized": True,
            "node_id": identity.node_id,
            "node_name": identity.node_name,
            "identity_version": identity.identity_version,
            "fingerprint": identity.fingerprint,
            "fingerprint_short": short_fingerprint(identity.fingerprint),
            "public_key": identity.public_key_b64,
            "created_at": identity.created_at,
            "rotated_at": identity.rotated_at,
            "enabled": identity.enabled,
            "state_directory": str(self._identity_dir()),
        }

    def public_identity_document(self) -> dict:
        return self.require_identity().public_document()

    # -- capability manifest -----------------------------------------------------

    def local_capabilities(self) -> list[str]:
        caps = [CAP_MISSION_ACCEPTANCE, CAP_COORDINATOR_REASONING, CAP_AGENT_TRANSPORT]
        with contextlib.suppress(Exception):
            if self.runtime.coding_agent.is_configured():
                caps.append(CAP_CODING_AGENT)
        with contextlib.suppress(Exception):
            if self.runtime.mcp.is_configured():
                caps.append(CAP_GNURADIO_MCP)
        return caps

    def build_local_manifest(self) -> dict:
        identity = self.require_identity()
        return build_manifest(
            identity,
            node_name=self.node_name,
            endpoint=self.runtime.config.base_url,
            capabilities=self.local_capabilities(),
        )

    # -- peers -------------------------------------------------------------------

    async def create_peer(self, payload) -> str:
        """Create an UNTRUSTED peer; returns the peer id."""
        fingerprint = None
        if payload.public_key:
            fingerprint = fingerprint_for_public_key(payload.public_key)
            if payload.fingerprint and payload.fingerprint != fingerprint:
                raise KeyMismatchError("Provided fingerprint does not match the public key.")
        with self.runtime.session_scope() as session:
            peer = PeerRepository(session).create(
                name=payload.name,
                role=payload.role.value if hasattr(payload.role, "value") else str(payload.role),
                endpoint_url=payload.endpoint_url,
                expected_node_id=payload.expected_node_id,
                public_key=payload.public_key,
                fingerprint=fingerprint,
                transport=payload.transport,
                tls_verify=payload.tls_verify,
                metadata=payload.metadata,
            )
            session.commit()
            peer_id = peer.id
        await self._emit(
            ev.EVENT_PEER_CREATED,
            f"Peer '{payload.name}' created (untrusted).",
            payload={"peer_id": peer_id, "fingerprint": fingerprint},
        )
        return peer_id

    async def trust_peer(
        self, peer_id: str, *, public_key: str | None = None, fingerprint: str | None = None
    ) -> None:
        """Explicitly trust a peer, pinning its public key. Never silently replaces a key."""
        with self.runtime.session_scope() as session:
            repo = PeerRepository(session)
            peer = repo.get(peer_id)
            if peer is None:
                raise PeerTrustError("Peer not found.", code="peer_not_found")
            new_key = public_key or peer.public_key
            if not new_key:
                raise PeerTrustError(
                    "Cannot trust a peer without a public key.", code="no_public_key"
                )
            # A trusted peer's pinned key may never be silently replaced by a different key.
            if (
                peer.public_key
                and public_key
                and public_key != peer.public_key
                and peer.trust_state == "trusted"
            ):
                await self._emit_key_mismatch(peer_id, "trust key replacement attempt")
                raise KeyMismatchError(
                    "Refusing to replace the pinned key of a trusted peer.", code="key_mismatch"
                )
            computed_fp = fingerprint_for_public_key(new_key)
            if fingerprint and fingerprint != computed_fp:
                raise KeyMismatchError("Fingerprint does not match the public key.")
            repo.update(
                peer_id,
                fields={
                    "public_key": new_key,
                    "fingerprint": computed_fp,
                    "trust_state": "trusted",
                    "enabled": True,
                },
            )
            session.commit()
        await self._emit(
            ev.EVENT_PEER_TRUSTED,
            f"Peer {peer_id} trusted.",
            payload={"peer_id": peer_id, "fingerprint": computed_fp},
        )

    async def revoke_peer(self, peer_id: str) -> None:
        with self.runtime.session_scope() as session:
            repo = PeerRepository(session)
            if repo.get(peer_id) is None:
                raise PeerTrustError("Peer not found.", code="peer_not_found")
            repo.update(peer_id, fields={"trust_state": "revoked", "enabled": False})
            session.commit()
        await self._emit(
            ev.EVENT_PEER_REVOKED, f"Peer {peer_id} revoked.", payload={"peer_id": peer_id}
        )

    async def set_peer_enabled(self, peer_id: str, *, enabled: bool) -> None:
        with self.runtime.session_scope() as session:
            repo = PeerRepository(session)
            if repo.get(peer_id) is None:
                raise PeerTrustError("Peer not found.", code="peer_not_found")
            repo.update(peer_id, fields={"enabled": enabled})
            session.commit()

    # -- peer health / manifest --------------------------------------------------

    async def test_peer(self, peer_id: str) -> dict:
        """Healthcheck a peer endpoint; record success/failure. Never trusts on its own."""
        peer = self._peer_or_raise(peer_id)
        transport = self._ensure_transport()
        try:
            health = await transport.healthcheck(
                endpoint=peer["endpoint_url"],
                tls_verify=peer["tls_verify"],
                allow_insecure_http=self.config.local_development.allow_insecure_http,
            )
        except TransportError as exc:
            self._record_peer_failure(peer_id, str(exc))
            await self._emit(
                ev.EVENT_PEER_HEALTH_FAILED,
                f"Peer {peer_id} health check failed.",
                payload={"peer_id": peer_id, "error_type": exc.code},
            )
            return {"ok": False, "error_type": exc.code}
        self._record_peer_success(peer_id)
        await self._emit(
            ev.EVENT_PEER_HEALTH_SUCCEEDED,
            f"Peer {peer_id} health check succeeded.",
            payload={"peer_id": peer_id},
        )
        return {"ok": True, "health": health}

    async def refresh_manifest(self, peer_id: str) -> dict:
        """Fetch + verify a peer manifest and store it. A manifest never creates trust."""
        peer = self._peer_or_raise(peer_id)
        transport = self._ensure_transport()
        manifest = await transport.fetch_manifest(
            endpoint=peer["endpoint_url"],
            tls_verify=peer["tls_verify"],
            allow_insecure_http=self.config.local_development.allow_insecure_http,
        )
        # The manifest must be signed by the peer's CONFIGURED trusted key — a manifest key
        # that differs from the pinned key is rejected, never adopted.
        pinned_key = peer["public_key"]
        if pinned_key:
            man_key = manifest.get("public_key")
            if man_key and man_key != pinned_key:
                await self._emit_key_mismatch(peer_id, "manifest key differs from pinned key")
                raise KeyMismatchError(
                    "Manifest public key does not match the pinned peer key.", code="key_mismatch"
                )
            ok, reason = verify_manifest(manifest, public_key_b64=pinned_key)
            if not ok:
                raise SignatureError(f"Manifest verification failed: {reason}.")
        capabilities = manifest.get("capabilities", [])
        with self.runtime.session_scope() as session:
            PeerRepository(session).update(
                peer_id,
                fields={
                    "last_manifest_json": manifest,
                    "capability_snapshot_json": {"capabilities": capabilities},
                    "capability_snapshot_at": utcnow(),
                },
            )
            session.commit()
        await self._emit(
            ev.EVENT_PEER_MANIFEST_REFRESHED,
            f"Peer {peer_id} manifest refreshed.",
            payload={"peer_id": peer_id, "capabilities": capabilities},
        )
        return manifest

    # -- inbound (authenticated) -------------------------------------------------

    async def handle_inbound(self, raw_body: object) -> dict:
        """Authenticate, deduplicate, and store one inbound envelope; return a signed ACK.

        Raises a :class:`TransportError` subclass on every rejection so the API maps it to a
        sanitized status. NEVER executes the message — the payload is stored as data only.
        """
        identity = self.require_identity()  # needed to sign the ack (else IdentityError -> 503)
        envelope = parse_envelope(
            raw_body, maximum_payload_bytes=self.config.inbound.maximum_payload_bytes
        )
        if envelope.kind not in DELIVERABLE_KINDS:
            raise EnvelopeError(f"Unsupported message kind '{envelope.kind}'.", code="bad_kind")

        validate_recipient(envelope, local_node_id=self.node_id)

        # Resolve + authenticate the sender peer.
        with self.runtime.session_scope() as session:
            peer = PeerRepository(session).get_by_node_id(envelope.sender.node_id)
            peer_snapshot = self._peer_snapshot(peer) if peer is not None else None
        if peer_snapshot is None:
            await self._emit_rejected(envelope, "unknown_peer")
            raise PeerTrustError("Sender peer is not known to this node.")
        if peer_snapshot["trust_state"] != "trusted" or not peer_snapshot["enabled"]:
            await self._emit_rejected(envelope, "peer_not_trusted")
            raise PeerTrustError("Sender peer is not trusted or is disabled.")
        if not peer_snapshot["public_key"]:
            await self._emit_rejected(envelope, "peer_no_key")
            raise PeerTrustError("Sender peer has no pinned public key.")
        if (
            peer_snapshot["fingerprint"]
            and envelope.sender.fingerprint != peer_snapshot["fingerprint"]
        ):
            await self._emit_key_mismatch(peer_snapshot["id"], "inbound fingerprint mismatch")
            raise KeyMismatchError("Sender fingerprint does not match the pinned peer key.")
        if not envelope.verify_signature_with(peer_snapshot["public_key"]):
            await self._emit_rejected(envelope, "invalid_signature")
            raise SignatureError("Envelope signature did not verify.")

        validate_timing(
            envelope, accepted_clock_skew_seconds=self.config.inbound.accepted_clock_skew_seconds
        )

        # beta.3 mesh authorization: an authenticated, key-trusted peer is still rejected here
        # (before any inbox/MissionEngine ingress) unless it shares a mesh with this node. When no
        # mesh is configured, the bilateral peer-trust model stays in force.
        await self._enforce_mesh(envelope)

        # Idempotent persistence + signed acknowledgement.
        envelope_hash = envelope.canonical_hash()
        return await self._store_and_ack(identity, envelope, peer_snapshot, envelope_hash)

    def _evaluate_mesh(self, envelope):
        """Run the mesh ingress guard against this node's OWN local mesh membership truth."""
        from aithernet.mesh import evaluate_mesh_ingress

        with self.runtime.session_scope() as session:
            views = MeshRepository(session).mesh_views()
        return evaluate_mesh_ingress(
            local_node_id=self.node_id,
            sender_node_id=envelope.sender.node_id,
            sender_fingerprint=envelope.sender.fingerprint,
            envelope_mesh_id=envelope.mesh_id,
            envelope_authority_id=envelope.authority_id,
            local_meshes=views,
        )

    async def _enforce_mesh(self, envelope) -> None:
        result = self._evaluate_mesh(envelope)
        if not result.allowed:
            await self._emit_rejected(envelope, "mesh_not_authorized")
            raise MeshAuthorizationError(f"Mesh authorization denied: {result.reason}.")

    async def authenticate_envelope(self, raw_body: object, *, expected_kind: str | None = None):
        """Verify a signed envelope and return ``(envelope, peer_snapshot)`` WITHOUT storing it.

        Reuses the full Stage 13A authentication pipeline (trust, pinned key, signature, timing,
        recipient) so the Stage 13D.2 artifact endpoint can authenticate a byte-range pull
        request without creating an inbox record. Raises a :class:`TransportError` on rejection.
        """
        self.require_identity()
        envelope = parse_envelope(
            raw_body, maximum_payload_bytes=self.config.inbound.maximum_payload_bytes
        )
        if expected_kind is not None and envelope.kind != expected_kind:
            raise EnvelopeError(f"Unexpected message kind '{envelope.kind}'.", code="bad_kind")
        validate_recipient(envelope, local_node_id=self.node_id)
        with self.runtime.session_scope() as session:
            peer = PeerRepository(session).get_by_node_id(envelope.sender.node_id)
            peer_snapshot = self._peer_snapshot(peer) if peer is not None else None
        if peer_snapshot is None:
            raise PeerTrustError("Sender peer is not known to this node.")
        if peer_snapshot["trust_state"] != "trusted" or not peer_snapshot["enabled"]:
            raise PeerTrustError("Sender peer is not trusted or is disabled.")
        if not peer_snapshot["public_key"]:
            raise PeerTrustError("Sender peer has no pinned public key.")
        if (
            peer_snapshot["fingerprint"]
            and envelope.sender.fingerprint != peer_snapshot["fingerprint"]
        ):
            raise KeyMismatchError("Sender fingerprint does not match the pinned peer key.")
        if not envelope.verify_signature_with(peer_snapshot["public_key"]):
            raise SignatureError("Envelope signature did not verify.")
        validate_timing(
            envelope, accepted_clock_skew_seconds=self.config.inbound.accepted_clock_skew_seconds
        )
        await self._enforce_mesh(envelope)
        return envelope, peer_snapshot

    async def _store_and_ack(self, identity, envelope, peer_snapshot, envelope_hash) -> dict:
        with self.runtime.session_scope() as session:
            inbox = AgentInboxRepository(session)
            existing = inbox.get_by_message_id(envelope.message_id)
            if existing is not None:
                if existing.envelope_hash != envelope_hash:
                    # Same id, different content -> tampering/collision. Never overwrite.
                    session.rollback()
                    await self._emit_rejected(envelope, "message_id_collision")
                    raise MessageIdCollisionError(
                        "message_id already received with different content."
                    )
                # Genuine duplicate -> idempotent: same outcome, one logical record.
                existing.duplicate_count = (existing.duplicate_count or 0) + 1
                ack = build_ack(
                    identity,
                    original_message_id=envelope.message_id,
                    status=AckStatus(existing.ack_status),
                    inbox_record_id=existing.id,
                    duplicate=True,
                )
                session.commit()
                await self._emit_received(
                    ev.EVENT_MESSAGE_DUPLICATE, envelope, peer_snapshot, duplicate=True
                )
                return ack

            ack_id = new_uuid()
            record = inbox.create(
                message_id=envelope.message_id,
                peer_id=peer_snapshot["id"],
                sender_node_id=envelope.sender.node_id,
                sender_fingerprint=envelope.sender.fingerprint,
                kind=envelope.kind,
                envelope_hash=envelope_hash,
                envelope=envelope.authenticated_dict(),
                payload=envelope.payload,
                protocol_version=envelope.protocol_version,
                conversation_id=envelope.conversation_id,
                correlation_id=envelope.correlation_id,
                causation_id=envelope.causation_id,
                reply_to_message_id=envelope.reply_to_message_id,
                mission_id=envelope.mission_id,
                mission_run_id=envelope.mission_run_id,
                mission_step_id=envelope.mission_step_id,
                ack_status=AckStatus.ACCEPTED.value,
                ack_id=ack_id,
            )
            ack = build_ack(
                identity,
                original_message_id=envelope.message_id,
                status=AckStatus.ACCEPTED,
                inbox_record_id=record.id,
                duplicate=False,
            )
            session.commit()
            new_record_id = record.id
        await self._emit_received(
            ev.EVENT_MESSAGE_RECEIVED, envelope, peer_snapshot, duplicate=False
        )
        # Stage 13B application processing for a NEW record only (duplicates never reach here,
        # preserving Stage 13A idempotency). The ACK above is a TRANSPORT acknowledgement, NOT a
        # semantic reply — application processing runs separately and never executes the payload.
        comms = getattr(self.runtime, "comms", None)
        if comms is not None:
            with contextlib.suppress(Exception):
                await comms.on_inbound_stored(new_record_id, peer_snapshot)
        # Stage 13D.2: artifact-control messages (a distinct application inside the same signed
        # envelope) are dispatched to the artifact service. ACK is still transport-only.
        artifacts = getattr(self.runtime, "artifacts", None)
        if artifacts is not None:
            with contextlib.suppress(Exception):
                await artifacts.on_inbound_control(new_record_id, peer_snapshot)
        return ack

    # -- outbound: create + deliver ----------------------------------------------

    async def create_outbound_message(self, payload) -> str:
        """Persist + sign an outbound message to a peer. Returns the outbox record id."""
        identity = self.require_identity()
        peer = self._peer_or_raise(payload.peer_id)
        if peer["trust_state"] == "revoked" or not peer["enabled"]:
            raise PeerTrustError("Cannot send to a revoked or disabled peer.")
        if not peer["expected_node_id"]:
            raise PeerTrustError("Peer has no expected node id to address.", code="no_node_id")

        body: dict = dict(payload.data or {})
        if payload.subject is not None:
            body.setdefault("subject", payload.subject)
        if payload.text is not None:
            body.setdefault("text", payload.text)

        envelope = build_envelope(
            identity=identity,
            recipient_node_id=peer["expected_node_id"],
            recipient_agent_id=None,
            kind=payload.kind,
            payload=body,
            conversation_id=payload.conversation_id,
            correlation_id=payload.correlation_id,
            reply_to_message_id=payload.reply_to_message_id,
            mission_id=payload.mission_id,
            content_type=payload.content_type,
            expires_in_seconds=payload.expires_in_seconds,
        )
        with self.runtime.session_scope() as session:
            record = AgentOutboxRepository(session).create(
                message_id=envelope.message_id,
                peer_id=peer["id"],
                kind=payload.kind,
                envelope=envelope.authenticated_dict(),
                envelope_hash=envelope.canonical_hash(),
                protocol_version=envelope.protocol_version,
                conversation_id=payload.conversation_id,
                correlation_id=payload.correlation_id,
                reply_to_message_id=payload.reply_to_message_id,
                mission_id=payload.mission_id,
                expires_at=self._parse_iso(envelope.expires_at),
                max_attempts=self.config.outbound.max_attempts,
            )
            session.commit()
            record_id = record.id
        await self._emit(
            ev.EVENT_MESSAGE_QUEUED,
            f"Outbound {payload.kind} queued to peer {peer['id']}.",
            mission_id=payload.mission_id,
            payload={
                "message_id": envelope.message_id,
                "peer_id": peer["id"],
                "record_id": record_id,
            },
        )
        return record_id

    async def deliver_record(self, record_id: str, *, owner: str) -> str:
        """Attempt ONE delivery of a claimed outbox record; persist the resulting state.

        Returns the terminal/intermediate status. Verifies a signed ACK before marking the
        record ``acknowledged`` — a bare HTTP 200 is never acknowledgement.
        """
        snap = self._outbox_snapshot(record_id)
        if snap is None:
            return "missing"
        identity = self.ensure_identity()
        if identity is None:
            self._finish(record_id, owner, status="retry_scheduled",
                         next_attempt_at=utcnow() + timedelta(seconds=5),
                         error_type="identity_missing", last_error="identity not initialized")
            return "retry_scheduled"

        # Never send an expired message.
        if is_expired(snap["expires_at_iso"]):
            self._finish(record_id, owner, status="failed", error_type="expired",
                         last_error="message expired before delivery")
            await self._emit(ev.EVENT_MESSAGE_FAILED, f"Message {snap['message_id']} expired.",
                             mission_id=snap["mission_id"],
                             payload={"message_id": snap["message_id"]})
            return "failed"

        peer = self._peer_snapshot_by_id(snap["peer_id"])
        if peer is None or peer["trust_state"] == "revoked" or not peer["enabled"]:
            self._finish(record_id, owner, status="failed", error_type="peer_not_deliverable",
                         last_error="peer revoked or disabled")
            await self._emit(ev.EVENT_MESSAGE_FAILED,
                             f"Message {snap['message_id']} not deliverable (peer).",
                             mission_id=snap["mission_id"],
                             payload={"message_id": snap["message_id"], "peer_id": snap["peer_id"]})
            return "failed"

        await self._emit(
            ev.EVENT_DELIVERY_STARTED,
            f"Delivery started for {snap['message_id']}.",
            mission_id=snap["mission_id"],
            payload={"message_id": snap["message_id"], "attempt": snap["attempt_count"]},
        )

        transport = self._ensure_transport()
        try:
            response = await transport.deliver(
                endpoint=peer["endpoint_url"],
                envelope=snap["envelope"],
                tls_verify=peer["tls_verify"],
                allow_insecure_http=self.config.local_development.allow_insecure_http,
            )
        except TransportError as exc:
            return await self._handle_delivery_error(record_id, owner, snap, peer, exc)

        # Transport accepted the request -> verify the SIGNED ack before acknowledging.
        ok, detail = verify_ack(
            response.body,
            public_key_b64=peer["public_key"],
            expected_message_id=snap["message_id"],
            expected_node_id=peer["expected_node_id"],
        )
        if not ok:
            # HTTP 200 without a valid signed ACK is a delivery FAILURE, not acknowledgement.
            return await self._handle_ack_failure(record_id, owner, snap, peer, detail)

        self._finish(
            record_id, owner, status="acknowledged",
            delivered_at=utcnow(), acknowledged_at=utcnow(),
            remote_ack_id=response.body.get("acknowledgement_id"),
            http_status=response.status_code, error_type=None, last_error=None,
        )
        self._record_peer_success(snap["peer_id"])
        await self._emit(ev.EVENT_MESSAGE_DELIVERED, f"Message {snap['message_id']} delivered.",
                         mission_id=snap["mission_id"],
                         payload={"message_id": snap["message_id"], "peer_id": snap["peer_id"]})
        await self._emit(ev.EVENT_MESSAGE_ACKNOWLEDGED,
                         f"Message {snap['message_id']} acknowledged.",
                         mission_id=snap["mission_id"],
                         payload={"message_id": snap["message_id"], "ack_status": detail})
        return "acknowledged"

    async def _handle_delivery_error(
        self, record_id, owner, snap, peer, exc: TransportError
    ) -> str:
        self._record_peer_failure(snap["peer_id"], str(exc))
        http_status = getattr(exc, "status_code", None)
        if exc.retryable and snap["attempt_count"] < snap["max_attempts"]:
            delay = self._backoff(snap["attempt_count"])
            self._finish(record_id, owner, status="retry_scheduled",
                         next_attempt_at=utcnow() + timedelta(seconds=delay),
                         http_status=http_status, error_type=exc.code,
                         last_error=_short_err(str(exc)))
            await self._emit(ev.EVENT_MESSAGE_RETRY_SCHEDULED,
                             f"Delivery of {snap['message_id']} scheduled for retry.",
                             mission_id=snap["mission_id"],
                             payload={"message_id": snap["message_id"], "error_type": exc.code,
                                      "attempt": snap["attempt_count"]})
            return "retry_scheduled"
        if exc.retryable:  # retryable but attempts exhausted
            self._finish(record_id, owner, status="dead_letter",
                         http_status=http_status, error_type=exc.code,
                         last_error=_short_err(str(exc)),
                         dead_letter_reason=f"max attempts reached ({exc.code})")
            await self._emit(ev.EVENT_MESSAGE_DEAD_LETTER,
                             f"Message {snap['message_id']} dead-lettered.",
                             mission_id=snap["mission_id"],
                             payload={"message_id": snap["message_id"], "error_type": exc.code})
            return "dead_letter"
        # Permanent error -> failed, never auto-retried.
        self._finish(record_id, owner, status="failed", http_status=http_status,
                     error_type=exc.code, last_error=_short_err(str(exc)))
        await self._emit(ev.EVENT_MESSAGE_FAILED,
                         f"Message {snap['message_id']} permanently failed.",
                         mission_id=snap["mission_id"],
                         payload={"message_id": snap["message_id"], "error_type": exc.code})
        return "failed"

    async def _handle_ack_failure(self, record_id, owner, snap, peer, detail: str) -> str:
        self._record_peer_failure(snap["peer_id"], f"invalid_ack:{detail}")
        if snap["attempt_count"] < snap["max_attempts"]:
            delay = self._backoff(snap["attempt_count"])
            self._finish(record_id, owner, status="retry_scheduled",
                         delivered_at=utcnow(),
                         next_attempt_at=utcnow() + timedelta(seconds=delay),
                         error_type="invalid_ack", last_error=f"invalid ack: {detail}")
            await self._emit(ev.EVENT_MESSAGE_RETRY_SCHEDULED,
                             f"Delivery of {snap['message_id']} produced an invalid ack; retrying.",
                             mission_id=snap["mission_id"],
                             payload={"message_id": snap["message_id"],
                                      "error_type": "invalid_ack"})
            return "retry_scheduled"
        self._finish(record_id, owner, status="dead_letter", delivered_at=utcnow(),
                     error_type="invalid_ack", last_error=f"invalid ack: {detail}",
                     dead_letter_reason="max attempts reached (invalid_ack)")
        await self._emit(ev.EVENT_MESSAGE_DEAD_LETTER,
                         f"Message {snap['message_id']} dead-lettered (invalid ack).",
                         mission_id=snap["mission_id"],
                         payload={"message_id": snap["message_id"], "error_type": "invalid_ack"})
        return "dead_letter"

    # -- outbound operator actions -----------------------------------------------

    async def send_now(self, record_id: str) -> str:
        """Attempt ONE synchronous delivery of an already-persisted record (Part M send_now).

        Claims the record with a temporary owner so it uses the SAME durable delivery path
        and atomic-claim semantics as the worker (never bypasses the outbox). If the worker
        already holds the record, this is a no-op and the worker delivers it.
        """
        owner = f"send-now-{new_uuid()[:8]}"
        now = utcnow()
        expiry = now + timedelta(seconds=self.config.outbound.delivery_lease_seconds)
        with self.runtime.session_scope() as session:
            claimed = AgentOutboxRepository(session).claim(
                record_id, owner=owner, now=now, claim_expiry=expiry
            )
            session.commit()
        if claimed != 1:
            return "not_claimed"
        return await self.deliver_record(record_id, owner=owner)

    async def retry_message(self, record_id: str) -> bool:
        """Operator-requested retry of a failed/dead-lettered message (same message id)."""
        with self.runtime.session_scope() as session:
            repo = AgentOutboxRepository(session)
            record = repo.get(record_id)
            if record is None:
                return False
            if record.status not in ("failed", "dead_letter"):
                return record.status in ("pending", "retry_scheduled")
            repo.update(record_id, fields={
                "status": "retry_scheduled", "next_attempt_at": utcnow(),
                "dead_letter_reason": None, "claim_owner": None, "claim_expires_at": None,
            })
            session.commit()
        return True

    async def cancel_message(self, record_id: str) -> bool:
        """Cancel a not-yet-terminal message so it is never claimed/delivered again."""
        with self.runtime.session_scope() as session:
            repo = AgentOutboxRepository(session)
            record = repo.get(record_id)
            if record is None:
                return False
            if record.status in ("acknowledged", "cancelled"):
                return record.status == "cancelled"
            repo.update(record_id, fields={
                "status": "cancelled", "claim_owner": None, "claim_expires_at": None,
                "next_attempt_at": None,
            })
            session.commit()
            message_id = record.message_id
            mission_id = record.mission_id
        await self._emit(ev.EVENT_MESSAGE_CANCELLED, f"Message {message_id} cancelled.",
                         mission_id=mission_id, payload={"message_id": message_id})
        return True

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        """Load/auto-initialize identity and start the delivery worker (never blocks startup)."""
        if not self.config.enabled:
            return
        if self.config.identity.auto_initialize and not self._identity_manager.exists():
            with contextlib.suppress(Exception):
                await self.initialize_identity()
        self.ensure_identity()
        if self.config.outbound.enabled:
            from aithernet.transport.worker import DeliveryWorker

            self._worker = DeliveryWorker(self)
            await self._worker.start()

    async def shutdown(self) -> None:
        if self._worker is not None:
            await self._worker.shutdown()
            self._worker = None

    def transport_status(self) -> dict:
        with self.runtime.session_scope() as session:
            outbox_counts = AgentOutboxRepository(session).counts_by_status()
            inbox_count = AgentInboxRepository(session).count()
            peers = PeerRepository(session).list(limit=1000)
            peer_count = len(peers)
            trusted = sum(1 for p in peers if p.trust_state == "trusted")
        worker = self._worker
        worker_status = {
            "enabled": self.config.outbound.enabled,
            "running": worker.is_running() if worker is not None else False,
            "degraded": worker.is_degraded() if worker is not None else False,
            "worker_count": self.config.outbound.worker_count,
            "in_flight": int(outbox_counts.get("in_flight", 0)),
            "last_error": worker.last_error() if worker is not None else None,
        }
        return {
            "enabled": self.config.enabled,
            "identity": self.identity_status(),
            "worker": worker_status,
            "outbox_counts": outbox_counts,
            "inbox_count": inbox_count,
            "peer_count": peer_count,
            "trusted_peer_count": trusted,
        }

    # -- read helpers ------------------------------------------------------------

    def list_peers(self, *, limit: int = 100, offset: int = 0):
        from aithernet.schemas.transport import PeerRead

        with self.runtime.session_scope() as session:
            rows = PeerRepository(session).list(limit=limit, offset=offset)
            return [PeerRead.model_validate(self._peer_attrs(row)) for row in rows]

    def get_peer_read(self, peer_id: str):
        from aithernet.schemas.transport import PeerRead

        with self.runtime.session_scope() as session:
            row = PeerRepository(session).get(peer_id)
            if row is None:
                return None
            return PeerRead.model_validate(self._peer_attrs(row))

    def list_outbox(self, *, status=None, peer_id=None, limit=100, offset=0):
        from aithernet.schemas.transport import OutboxMessageRead

        with self.runtime.session_scope() as session:
            rows = AgentOutboxRepository(session).list(
                status=status, peer_id=peer_id, limit=limit, offset=offset
            )
            return [OutboxMessageRead.model_validate(r) for r in rows]

    def get_outbox_read(self, record_id: str):
        from aithernet.schemas.transport import OutboxMessageRead

        with self.runtime.session_scope() as session:
            row = AgentOutboxRepository(session).get(record_id)
            return OutboxMessageRead.model_validate(row) if row is not None else None

    def list_inbox(self, *, peer_id=None, limit=100, offset=0):
        from aithernet.schemas.transport import InboxMessageRead

        with self.runtime.session_scope() as session:
            rows = AgentInboxRepository(session).list(peer_id=peer_id, limit=limit, offset=offset)
            return [InboxMessageRead.model_validate(r) for r in rows]

    # -- internal helpers --------------------------------------------------------

    def _ensure_transport(self):
        if self._transport is None:
            from aithernet.transport.http_transport import HTTPAgentTransport

            out = self.config.outbound
            self._transport = HTTPAgentTransport(
                connect_timeout_seconds=out.connect_timeout_seconds,
                request_timeout_seconds=out.request_timeout_seconds,
                maximum_response_bytes=out.maximum_response_bytes,
            )
        return self._transport

    def _backoff(self, attempt: int) -> float:
        out = self.config.outbound
        base = out.initial_backoff_seconds * (2 ** max(0, attempt - 1))
        base = min(base, out.maximum_backoff_seconds)
        return base + random.uniform(0, out.jitter_seconds)

    def _finish(self, record_id: str, owner: str, **fields) -> None:
        with self.runtime.session_scope() as session:
            AgentOutboxRepository(session).release_and_update(record_id, owner=owner, fields=fields)
            session.commit()

    def _record_peer_success(self, peer_id: str) -> None:
        with self.runtime.session_scope() as session:
            PeerRepository(session).record_success(peer_id)
            session.commit()

    def _record_peer_failure(self, peer_id: str, error: str) -> None:
        with self.runtime.session_scope() as session:
            PeerRepository(session).record_failure(peer_id, error=_short_err(error))
            session.commit()

    def _peer_or_raise(self, peer_id: str) -> dict:
        snap = self._peer_snapshot_by_id(peer_id)
        if snap is None:
            raise PeerTrustError("Peer not found.", code="peer_not_found")
        return snap

    def _peer_snapshot_by_id(self, peer_id: str) -> dict | None:
        with self.runtime.session_scope() as session:
            peer = PeerRepository(session).get(peer_id)
            return self._peer_snapshot(peer) if peer is not None else None

    @staticmethod
    def _peer_snapshot(peer) -> dict:
        return {
            "id": peer.id,
            "endpoint_url": peer.endpoint_url,
            "expected_node_id": peer.expected_node_id,
            "public_key": peer.public_key,
            "fingerprint": peer.fingerprint,
            "trust_state": peer.trust_state,
            "enabled": peer.enabled,
            "tls_verify": peer.tls_verify,
        }

    @staticmethod
    def _peer_attrs(peer) -> dict:
        last_manifest = peer.last_manifest_json or None
        last_manifest_at = peer.capability_snapshot_at if last_manifest else None
        data = {c.name: getattr(peer, c.name) for c in peer.__table__.columns}
        data["last_manifest_at"] = last_manifest_at
        return data

    def _outbox_snapshot(self, record_id: str) -> dict | None:
        with self.runtime.session_scope() as session:
            r = AgentOutboxRepository(session).get(record_id)
            if r is None:
                return None
            return {
                "message_id": r.message_id,
                "peer_id": r.peer_id,
                "envelope": r.envelope_json,
                "attempt_count": r.attempt_count,
                "max_attempts": r.max_attempts,
                "mission_id": r.mission_id,
                "expires_at_iso": r.expires_at.isoformat() if r.expires_at else None,
            }

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(value)
        return None

    # -- event emit helpers ------------------------------------------------------

    async def _emit(self, event_type: str, message: str, *, mission_id: str | None = None,
                    payload: dict | None = None) -> None:
        await self.runtime._emit_event(
            event_type=event_type, mission_id=mission_id, source="transport",
            message=message, payload=payload or {},
        )

    async def _emit_received(self, event_type, envelope, peer_snapshot, *, duplicate) -> None:
        await self._emit(
            event_type,
            f"Inbound {envelope.kind} {'(duplicate) ' if duplicate else ''}from peer "
            f"{peer_snapshot['id']}.",
            mission_id=envelope.mission_id,
            payload={
                "message_id": envelope.message_id,
                "peer_id": peer_snapshot["id"],
                "kind": envelope.kind,
                "duplicate": duplicate,
            },
        )

    async def _emit_rejected(self, envelope, reason: str) -> None:
        await self._emit(
            ev.EVENT_MESSAGE_REJECTED,
            f"Inbound message rejected ({reason}).",
            payload={"message_id": getattr(envelope, "message_id", None), "reason": reason},
        )

    async def _emit_key_mismatch(self, peer_id: str, reason: str) -> None:
        await self._emit(
            ev.EVENT_PEER_KEY_MISMATCH,
            f"Peer {peer_id} key mismatch ({reason}).",
            payload={"peer_id": peer_id, "reason": reason},
        )
