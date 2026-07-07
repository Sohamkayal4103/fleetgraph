"""External-agent interoperability service (Stage 14D).

The single orchestration surface for the external-agent gateway: identity provisioning,
signed-request authentication, idempotent mission submission, ownership-scoped reads,
subscription fan-out, durable webhook + WebSocket delivery, receipts/acknowledgements, and
sanitized diagnostics. It builds on the existing mission/artifact/conversation subsystems
(which stay authoritative) and on the node's Ed25519 identity + canonical-signing helpers.

Notification infrastructure NEVER creates a MissionStep: it only persists interop messages
and emits sanitized internal events.
"""

from __future__ import annotations

import hashlib
import random
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aithernet.interop import events as ev
from aithernet.interop import permissions as perms
from aithernet.interop.auth import (
    AuthContext,
    hash_secret,
    verify_bearer,
    verify_signed_request,
)
from aithernet.interop.endpoints import Resolver, system_resolver, validate_endpoint
from aithernet.interop.envelope import envelope_from_message, payload_digest
from aithernet.interop.webhook import WebhookDeliverer
from aithernet.interop.websocket import InteropWebsocketManager, LiveConnection
from aithernet.interop.worker import InteropDeliveryWorker
from aithernet.schemas.missions import MissionCreate
from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import (
    InteropAgentRepository,
    InteropAuditRepository,
    InteropCredentialRepository,
    InteropDeliveryRepository,
    InteropEndpointRepository,
    InteropMessageRepository,
    InteropNonceRepository,
    InteropReceiptRepository,
    InteropSessionRepository,
    InteropSubmissionRepository,
    InteropSubscriptionRepository,
    MissionRepository,
    RFArtifactRepository,
)
from aithernet.transport.identity import fingerprint_for_public_key

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime


class InteropError(Exception):
    """Base class for clean, deterministic interop errors (mapped to HTTP codes by the API)."""

    code = "interop_error"


class NotFoundError(InteropError):
    code = "not_found"


class ForbiddenError(InteropError):
    code = "forbidden"


class ConflictError(InteropError):
    code = "conflict"


class ValidationError(InteropError):
    code = "validation_error"


class DisabledError(InteropError):
    code = "disabled"


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class InteropService:
    """The external-agent gateway service (Stage 14D)."""

    def __init__(
        self,
        runtime: NodeRuntime,
        *,
        resolver: Resolver | None = None,
        webhook_client_factory=None,
    ) -> None:
        self.runtime = runtime
        self.node_id = runtime.config.node_id
        self.config = runtime.config.external_agents
        self.resolver: Resolver = resolver or system_resolver
        self._webhook_client_factory = webhook_client_factory
        self.ws = InteropWebsocketManager(
            maximum_connections=self.config.websocket.maximum_connections,
            per_agent=self.config.websocket.per_agent_connections,
        )
        self.worker = InteropDeliveryWorker(self)

    # -- lifecycle --------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def start(self) -> None:
        if not self.enabled:
            return
        await self.worker.start()

    async def shutdown(self) -> None:
        await self.worker.shutdown()

    # -- helpers ----------------------------------------------------------------

    def _identity(self):
        return self.runtime.transport.ensure_identity()

    def _deliverer(self) -> WebhookDeliverer | None:
        identity = self._identity()
        if identity is None:
            return None
        return WebhookDeliverer(
            identity,
            self.config.callbacks,
            self.config.delivery,
            resolver=self.resolver,
            client_factory=self._webhook_client_factory,
        )

    def _audit(self, session, *, agent_id: str | None, event_type: str, detail: str = "") -> None:
        InteropAuditRepository(session).create(
            agent_id=agent_id, event_type=event_type, detail=detail[:300]
        )

    async def _emit(self, event_type: str, message: str, *, agent_id: str | None = None,
                    payload: dict | None = None) -> None:
        # Sanitized internal telemetry only — never secrets/signatures/URLs/bodies.
        with __import__("contextlib").suppress(Exception):
            await self.runtime.publish_ephemeral_event(
                event_type=event_type, source="interop", message=message,
                payload={"agent_id": agent_id, **(payload or {})},
            )

    # -- agent provisioning (operator) ------------------------------------------

    def create_agent(
        self,
        *,
        display_name: str,
        public_key: str | None = None,
        key_id: str | None = None,
        permissions: list[str] | None = None,
        owner: str | None = None,
        endpoint_policy: str = "default",
        provisioning_source: str = "operator",
        metadata: dict | None = None,
    ) -> dict:
        granted = perms.normalize_permissions(
            permissions if permissions is not None else list(perms.DEFAULT_PERMISSIONS)
        )
        fingerprint = (
            fingerprint_for_public_key(public_key) if public_key else None
        )
        with self.runtime.session_scope() as session:
            agents = InteropAgentRepository(session)
            if fingerprint is not None and agents.get_by_fingerprint(fingerprint) is not None:
                raise ConflictError("an agent with this public key already exists")
            agent = agents.create(
                display_name=display_name[:160],
                status="active",
                identity_type="ed25519" if public_key else "bearer",
                fingerprint=fingerprint,
                owner=owner,
                provisioning_source=provisioning_source,
                permissions_json=granted,
                endpoint_policy=endpoint_policy,
                metadata_json=_bounded_metadata(metadata),
            )
            if public_key:
                InteropCredentialRepository(session).create(
                    agent_id=agent.id, kind="ed25519",
                    key_id=key_id or fingerprint, public_key=public_key,
                    fingerprint=fingerprint, status="active",
                )
            self._audit(session, agent_id=agent.id, event_type=ev.EVENT_AGENT_CREATED,
                        detail=f"display_name={display_name[:80]}")
            session.commit()
            return self.agent_summary(agent.id)

    def add_credential(
        self, agent_id: str, *, kind: str = "ed25519", public_key: str | None = None,
        key_id: str | None = None, expires_at: datetime | None = None,
    ) -> dict:
        """Add an Ed25519 key (rotation) or generate a one-time bearer token.

        For a bearer credential the plaintext token is returned exactly ONCE under
        ``secret`` and only its hash is stored. Ed25519 credentials never carry a secret.
        """
        with self.runtime.session_scope() as session:
            agent = self._require_agent(session, agent_id)
            creds = InteropCredentialRepository(session)
            secret: str | None = None
            if kind == "ed25519":
                if not public_key:
                    raise ValidationError("ed25519 credential requires a public_key")
                fp = fingerprint_for_public_key(public_key)
                cred = creds.create(
                    agent_id=agent.id, kind="ed25519", key_id=key_id or fp,
                    public_key=public_key, fingerprint=fp, status="active", expires_at=expires_at,
                )
                if agent.fingerprint is None:
                    agent.fingerprint = fp
            elif kind == "bearer":
                if not self.config.authentication.bearer_tokens_enabled:
                    raise ValidationError("bearer credentials are not enabled")
                secret = "aith-" + secrets.token_urlsafe(32)
                cred = creds.create(
                    agent_id=agent.id, kind="bearer", key_id=key_id or new_uuid()[:12],
                    token_hash=hash_secret(secret), status="active", expires_at=expires_at,
                )
            else:
                raise ValidationError(f"unsupported credential kind: {kind}")
            self._audit(session, agent_id=agent.id, event_type=ev.EVENT_CREDENTIAL_CREATED,
                        detail=f"kind={kind} key_id={cred.key_id}")
            session.commit()
            result = {"credential_id": cred.id, "key_id": cred.key_id, "kind": cred.kind}
            if secret is not None:
                result["secret"] = secret  # shown ONCE; never returned by list/show
            return result

    def revoke_credential(self, agent_id: str, credential_id: str, *, reason: str = "") -> dict:
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            cred = InteropCredentialRepository(session).get(credential_id)
            if cred is None or cred.agent_id != agent_id:
                raise NotFoundError("credential not found")
            cred.status = "revoked"
            cred.revoked_reason = reason[:300] or "revoked"
            self._audit(session, agent_id=agent_id, event_type=ev.EVENT_CREDENTIAL_REVOKED,
                        detail=f"key_id={cred.key_id}")
            session.commit()
            return {"credential_id": cred.id, "status": "revoked"}

    def set_status(self, agent_id: str, status: str, *, reason: str = "") -> dict:
        if status not in ("active", "disabled", "revoked"):
            raise ValidationError("status must be active|disabled|revoked")
        with self.runtime.session_scope() as session:
            agent = self._require_agent(session, agent_id)
            agent.status = status
            agent.disabled_reason = reason[:300] if status != "active" else None
            event = ev.EVENT_AGENT_ENABLED if status == "active" else ev.EVENT_AGENT_DISABLED
            self._audit(session, agent_id=agent_id, event_type=event, detail=f"status={status}")
            session.commit()
        return self.agent_summary(agent_id)

    def set_permissions(self, agent_id: str, permissions: list[str]) -> dict:
        granted = perms.normalize_permissions(permissions)
        with self.runtime.session_scope() as session:
            agent = self._require_agent(session, agent_id)
            agent.permissions_json = granted
            session.commit()
        return self.agent_summary(agent_id)

    # -- endpoints --------------------------------------------------------------

    def register_endpoint(self, agent_id: str, url: str) -> dict:
        policy = self.config.callbacks
        with self.runtime.session_scope() as session:
            agent = self._require_agent(session, agent_id)
            result = validate_endpoint(url, policy, resolver=self.resolver)
            endpoints = InteropEndpointRepository(session)
            status = "rejected" if not result.allowed else (
                "pending_verification" if policy.verification_required else "verified"
            )
            endpoint = endpoints.create(
                agent_id=agent.id, url=url[: policy.maximum_url_length],
                scheme=result.scheme or "https", host=result.host or "",
                port=result.port, status=status, policy_result_json=result.sanitized(),
                signing_key_id=(self._identity().fingerprint if self._identity() else None),
                verified_at=(utcnow() if status == "verified" else None),
            )
            event = (
                ev.EVENT_ENDPOINT_REJECTED if not result.allowed
                else ev.EVENT_ENDPOINT_REGISTERED
            )
            self._audit(session, agent_id=agent_id, event_type=event,
                        detail=f"host={result.host} reason={result.reason}")
            session.commit()
            if not result.allowed:
                raise ValidationError(f"endpoint rejected: {result.reason}")
            return self._endpoint_summary(endpoint)

    def verify_endpoint(self, agent_id: str, endpoint_id: str, *, approve: bool = True) -> dict:
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            endpoint = InteropEndpointRepository(session).get(endpoint_id)
            if endpoint is None or endpoint.agent_id != agent_id:
                raise NotFoundError("endpoint not found")
            # Re-validate the policy at verification time (rebinding protection).
            result = validate_endpoint(endpoint.url, self.config.callbacks, resolver=self.resolver)
            if not result.allowed:
                endpoint.status = "rejected"
                endpoint.policy_result_json = result.sanitized()
                session.commit()
                raise ValidationError(f"endpoint failed re-validation: {result.reason}")
            endpoint.status = "verified" if approve else "pending_verification"
            endpoint.verified_at = utcnow() if approve else None
            self._audit(session, agent_id=agent_id, event_type=ev.EVENT_ENDPOINT_VERIFIED,
                        detail=f"endpoint={endpoint_id}")
            session.commit()
            return self._endpoint_summary(endpoint)

    def disable_endpoint(self, agent_id: str, endpoint_id: str, *, reason: str = "") -> dict:
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            endpoint = InteropEndpointRepository(session).get(endpoint_id)
            if endpoint is None or endpoint.agent_id != agent_id:
                raise NotFoundError("endpoint not found")
            endpoint.status = "disabled"
            endpoint.disabled_reason = reason[:300] or "disabled"
            session.commit()
            return self._endpoint_summary(endpoint)

    def list_endpoints(self, agent_id: str) -> list[dict]:
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            return [
                self._endpoint_summary(e)
                for e in InteropEndpointRepository(session).list_for_agent(agent_id)
            ]

    # -- subscriptions ----------------------------------------------------------

    def create_subscription(
        self, agent_id: str, *, delivery_mode: str = "webhook",
        filters: dict | None = None, endpoint_id: str | None = None,
    ) -> dict:
        if delivery_mode not in ("webhook", "websocket", "pull"):
            raise ValidationError("delivery_mode must be webhook|websocket|pull")
        clean_filters = _bounded_filters(filters)
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            subs = InteropSubscriptionRepository(session)
            existing = subs.list_for_agent(agent_id)
            if len(existing) >= self.config.quotas.subscriptions_per_agent:
                raise ValidationError("subscription quota exceeded")
            if delivery_mode == "webhook":
                if endpoint_id is None:
                    raise ValidationError("webhook subscription requires an endpoint_id")
                endpoint = InteropEndpointRepository(session).get(endpoint_id)
                if endpoint is None or endpoint.agent_id != agent_id:
                    raise NotFoundError("endpoint not found")
                if endpoint.status != "verified":
                    raise ValidationError("endpoint is not verified")
            sub = subs.create(
                agent_id=agent_id, endpoint_id=endpoint_id, delivery_mode=delivery_mode,
                filters_json=clean_filters, status="active", next_sequence=1,
                retention_window_seconds=self.config.websocket.replay_retention_seconds,
            )
            self._audit(session, agent_id=agent_id, event_type=ev.EVENT_SUBSCRIPTION_CREATED,
                        detail=f"mode={delivery_mode}")
            session.commit()
            return self._subscription_summary(sub)

    def list_subscriptions(self, agent_id: str) -> list[dict]:
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            return [
                self._subscription_summary(s)
                for s in InteropSubscriptionRepository(session).list_for_agent(agent_id)
            ]

    def set_subscription_state(
        self, agent_id: str, subscription_id: str, status: str, *, reason: str = ""
    ) -> dict:
        if status not in ("active", "paused", "revoked"):
            raise ValidationError("status must be active|paused|revoked")
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            sub = InteropSubscriptionRepository(session).get(subscription_id)
            if sub is None or sub.agent_id != agent_id:
                raise NotFoundError("subscription not found")
            sub.status = status
            sub.reason = reason[:300] if status != "active" else None
            session.commit()
            return self._subscription_summary(sub)

    def delete_subscription(self, agent_id: str, subscription_id: str) -> None:
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            sub = InteropSubscriptionRepository(session).get(subscription_id)
            if sub is None or sub.agent_id != agent_id:
                raise NotFoundError("subscription not found")
            sub.status = "revoked"
            session.commit()

    # -- authentication ---------------------------------------------------------

    def authenticate(
        self, *, headers: dict[str, str], method: str, target: str, body: bytes
    ) -> AuthContext:
        """Authenticate an inbound agent request. Persists the nonce on success."""
        low = {k.lower(): v for k, v in headers.items()}
        agent_id = low.get("x-aithernet-agent")
        auth_cfg = self.config.authentication
        now = utcnow()
        with self.runtime.session_scope() as session:
            agents = InteropAgentRepository(session)
            agent = agents.get(agent_id) if agent_id else None
            creds_repo = InteropCredentialRepository(session)
            # Bearer path (only if no signed-request headers and bearer enabled).
            if "x-aithernet-signature" not in low and auth_cfg.bearer_tokens_enabled:
                creds = creds_repo.list_for_agent(agent_id) if agent_id else []
                ctx = verify_bearer(
                    agent=agent, credentials=creds,
                    authorization_header=low.get("authorization"), now=now,
                )
            else:
                if not auth_cfg.signed_requests_enabled:
                    return AuthContext(ok=False, code="signed_requests_disabled")
                creds = creds_repo.get_active_ed25519(agent_id) if agent_id else []
                nonce = low.get("x-aithernet-nonce")
                nonce_seen = bool(
                    agent_id and nonce and InteropNonceRepository(session).exists(agent_id, nonce)
                )
                ctx = verify_signed_request(
                    agent=agent, credentials=creds, headers=headers, method=method,
                    target=target, body=body,
                    allowed_skew_seconds=auth_cfg.allowed_clock_skew_seconds,
                    nonce_seen=nonce_seen, nonce_retention_seconds=auth_cfg.nonce_retention_seconds,
                    now=now,
                )
            if ctx.ok:
                if ctx.nonce is not None and ctx.nonce_expires_at is not None:
                    InteropNonceRepository(session).create(
                        agent_id=ctx.agent_id, nonce=ctx.nonce, key_id=ctx.key_id,
                        request_target=target[:255], expires_at=ctx.nonce_expires_at,
                    )
                if agent is not None:
                    agent.last_authenticated_at = now
                # Opportunistic purge of expired nonces (bounded).
                InteropNonceRepository(session).purge_expired(now)
                session.commit()
            else:
                self._audit(session, agent_id=agent_id, event_type=ev.EVENT_AUTH_FAILED,
                            detail=f"code={ctx.code}")
                session.commit()
            return ctx

    # -- mission submission (idempotent) ----------------------------------------

    async def submit_mission(self, *, agent_id: str, body: dict) -> dict:
        """Idempotently accept an external mission and return a durable receipt."""
        idempotency_key = (body.get("idempotency_key") or "").strip()
        objective = (body.get("objective") or "").strip()
        if not idempotency_key:
            raise ValidationError("idempotency_key is required")
        if not objective:
            raise ValidationError("objective is required")
        # Only client-provided fields contribute to the idempotency digest — a server-generated
        # correlation id must never make two identical resubmissions look different.
        explicit_correlation = (body.get("correlation_id") or "")[:80]
        context = _bounded_metadata(body.get("context"))
        request_digest = _digest(
            f"{objective}\x1f{explicit_correlation}\x1f{_stable(context)}"
        )
        correlation_id = explicit_correlation or new_uuid()

        # Idempotency check (fast path: identical resubmission returns the original receipt).
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id, require_active=True)
            existing = InteropSubmissionRepository(session).get_for(agent_id, idempotency_key)
            if existing is not None:
                if existing.request_digest != request_digest:
                    raise ConflictError("idempotency key reused with a different request body")
                return dict(existing.receipt_json or {})

        # Create the mission via the authoritative mission subsystem.
        mission = await self.runtime.create_mission(
            MissionCreate(
                content=objective,
                source_type="external_agent",
                source_id=agent_id,
                metadata={
                    "interop": True,
                    "correlation_id": correlation_id,
                    "idempotency_key": idempotency_key,
                    "context": context,
                },
            )
        )
        receipt = {
            "acceptance_id": new_uuid(),
            "mission_id": mission.id,
            "correlation_id": correlation_id,
            "status": "accepted",
            "created_at": utcnow().isoformat(),
            "status_url": f"/agent-api/missions/{mission.id}",
            "schema_version": "1",
        }
        with self.runtime.session_scope() as session:
            try:
                InteropSubmissionRepository(session).create(
                    agent_id=agent_id, idempotency_key=idempotency_key,
                    request_digest=request_digest, mission_id=mission.id,
                    correlation_id=correlation_id, status="accepted", receipt_json=receipt,
                )
                session.commit()
            except Exception:
                # A concurrent identical submission won the unique-constraint race; return its
                # stored receipt (true idempotency, single mission of record per key).
                session.rollback()
                existing = InteropSubmissionRepository(session).get_for(agent_id, idempotency_key)
                if existing is not None:
                    return dict(existing.receipt_json or {})
                raise
        # Enqueue the durable accepted notification (after the submission is durable).
        await self._notify_mission(
            mission_id=mission.id, agent_id=agent_id, event_type=ev.EXT_MISSION_ACCEPTED,
            payload={"status": "accepted", "correlation_id": correlation_id},
            correlation_id=correlation_id,
        )
        # Queue the mission for autonomous execution when the worker is enabled (so the agent's
        # mission actually runs and produces started/completed/artifact callbacks). Best-effort:
        # a queueing failure never invalidates the durable acceptance the agent already holds.
        if self.runtime.config.mission_execution.enabled:
            with __import__("contextlib").suppress(Exception):
                await self.runtime.start_mission(mission.id)
        return receipt

    # -- ownership-scoped reads -------------------------------------------------

    def get_mission_for_agent(self, agent_id: str, mission_id: str) -> dict:
        with self.runtime.session_scope() as session:
            mission = self._require_owned_mission(session, agent_id, mission_id)
            return _mission_summary(mission)

    async def cancel_mission_for_agent(self, agent_id: str, mission_id: str) -> dict:
        with self.runtime.session_scope() as session:
            self._require_owned_mission(session, agent_id, mission_id)
        with __import__("contextlib").suppress(Exception):
            await self.runtime.cancel_mission_run(mission_id)
        return {"mission_id": mission_id, "status": "cancel_requested"}

    def list_mission_events_for_agent(
        self, agent_id: str, mission_id: str, *, limit: int = 200
    ) -> list[dict]:
        with self.runtime.session_scope() as session:
            self._require_owned_mission(session, agent_id, mission_id)
            repo = InteropMessageRepository(session)
            rows = [
                m for m in repo.list_for_agent(agent_id, limit=limit)
                if m.mission_id == mission_id
            ]
            return [envelope_from_message(m) for m in rows]

    def get_artifact_for_agent(self, agent_id: str, artifact_id: str) -> dict:
        with self.runtime.session_scope() as session:
            artifact = RFArtifactRepository(session).get(artifact_id)
            if artifact is None:
                raise NotFoundError("artifact not found")
            mission_id = artifact.mission_id
            if mission_id is None or not self._owns_mission(session, agent_id, mission_id):
                raise ForbiddenError("artifact not owned by this agent")
            return _artifact_metadata(artifact)

    def read_artifact_content(self, agent_id: str, artifact_id: str):
        """Return ``(metadata, bytes)`` for an owned artifact (bounded read), else raise.

        Content is streamed from the content-addressed store by digest; no filesystem path is
        ever exposed to the agent. The byte cap protects the node from an oversized read.
        """
        max_bytes = 64 * 1024 * 1024
        with self.runtime.session_scope() as session:
            artifact = RFArtifactRepository(session).get(artifact_id)
            if artifact is None:
                raise NotFoundError("artifact not found")
            if artifact.mission_id is None or not self._owns_mission(
                session, agent_id, artifact.mission_id
            ):
                raise ForbiddenError("artifact not owned by this agent")
            meta = _artifact_metadata(artifact)
            digest = artifact.digest or artifact.content_hash
        if not digest:
            raise NotFoundError("artifact content unavailable")
        try:
            store = self.runtime.artifacts.store
            size = store.object_size(digest)
            data = store.read_range(digest, offset=0, length=min(size, max_bytes))
        except Exception as exc:  # noqa: BLE001
            raise NotFoundError("artifact content unavailable") from exc
        return meta, data

    def get_conversation_for_agent(self, agent_id: str, conversation_id: str) -> dict:
        """Return the bounded set of interop messages an agent owns on a conversation."""
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            rows = [
                m for m in InteropMessageRepository(session).list_for_agent(agent_id, limit=500)
                if m.conversation_id == conversation_id
            ]
            if not rows:
                raise NotFoundError("conversation not found for this agent")
            return {
                "conversation_id": conversation_id,
                "messages": [envelope_from_message(m) for m in rows],
            }

    async def follow_up_conversation(
        self, agent_id: str, conversation_id: str, *, body: dict
    ) -> dict:
        """Submit a follow-up message tied to a conversation (idempotent via idempotency_key)."""
        body = dict(body or {})
        body.setdefault("correlation_id", conversation_id)
        return await self.submit_mission(agent_id=agent_id, body=body)

    # -- notification fan-out (NEVER creates a MissionStep) ---------------------

    async def maybe_notify_from_event(self, event_type: str, mission_id: str | None) -> None:
        """Hook from ``runtime._emit_event``: translate a mission event into agent callbacks."""
        if not self.enabled or mission_id is None:
            return
        ext_type = ev.MISSION_EVENT_MAP.get(event_type)
        if ext_type is None:
            return
        with self.runtime.session_scope() as session:
            mission = MissionRepository(session).get(mission_id)
            if mission is None or mission.source_type != "external_agent" or not mission.source_id:
                return
            agent_id = mission.source_id
            correlation_id = (mission.metadata_json or {}).get("correlation_id")
            status = mission.status
        await self._notify_mission(
            mission_id=mission_id, agent_id=agent_id, event_type=ext_type,
            payload={"status": status}, correlation_id=correlation_id,
        )
        # Artifact-ready notifications on completion.
        if ext_type == ev.EXT_MISSION_COMPLETED:
            await self._notify_artifacts(mission_id, agent_id, correlation_id)

    async def _notify_artifacts(
        self, mission_id: str, agent_id: str, correlation_id: str | None
    ) -> None:
        with self.runtime.session_scope() as session:
            artifacts = RFArtifactRepository(session).list(mission_id=mission_id, limit=50)
            metas = [_artifact_metadata(a) for a in artifacts]
        for meta in metas:
            await self._notify_mission(
                mission_id=mission_id, agent_id=agent_id, event_type=ev.EXT_ARTIFACT_READY,
                payload={"artifact": meta}, artifact_id=meta["artifact_id"],
                correlation_id=correlation_id,
            )

    async def _notify_mission(
        self, *, mission_id: str, agent_id: str, event_type: str, payload: dict,
        artifact_id: str | None = None, correlation_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """Persist a durable message per matching subscription, then push live WS sessions."""
        if not self.enabled:
            return
        live_pushes: list[tuple[str, str, dict]] = []
        with self.runtime.session_scope() as session:
            subs = InteropSubscriptionRepository(session)
            messages_repo = InteropMessageRepository(session)
            now = utcnow()
            retention = self.config.delivery.message_retention_seconds
            for sub in subs.list_for_agent(agent_id):
                if sub.status != "active":
                    continue
                if not _filters_match(sub.filters_json or {}, event_type, mission_id):
                    continue
                if event_type == ev.EXT_MISSION_STATUS and _is_redundant_status(
                    messages_repo, sub.id, payload.get("status")
                ):
                    continue
                seq = subs.allocate_sequence(sub.id)
                if seq is None:
                    continue
                digest = payload_digest(payload)
                message = messages_repo.create(
                    message_id=new_uuid(), schema_version="1", agent_id=agent_id,
                    subscription_id=sub.id, sequence=seq, event_type=event_type,
                    delivery_mode=sub.delivery_mode, mission_id=mission_id,
                    artifact_id=artifact_id, correlation_id=correlation_id,
                    conversation_id=conversation_id, payload_json=payload,
                    payload_digest=digest, status="pending",
                    max_attempts=self.config.delivery.maximum_attempts,
                    next_attempt_at=now,
                    expires_at=now + timedelta(seconds=retention),
                )
                self._audit(session, agent_id=agent_id, event_type=ev.EVENT_MESSAGE_ENQUEUED,
                            detail=f"type={event_type} seq={seq}")
                if sub.delivery_mode == "websocket":
                    live_pushes.append((agent_id, sub.id, envelope_from_message(message)))
            session.commit()
        # Live WebSocket push outside the DB transaction; webhook is handled by the worker.
        for agent, sub_id, envelope in live_pushes:
            self.ws.push(agent_id=agent, subscription_id=sub_id, envelope=envelope)

    # -- delivery (called by the worker) ----------------------------------------

    async def deliver_message(self, record_id: str, *, owner: str) -> None:
        """Deliver one claimed webhook message; persist terminal/retry/dead-letter state."""
        with self.runtime.session_scope() as session:
            message = InteropMessageRepository(session).get(record_id)
            if message is None or message.delivery_mode != "webhook":
                return
            sub = InteropSubscriptionRepository(session).get(message.subscription_id)
            endpoint = (
                InteropEndpointRepository(session).get(sub.endpoint_id)
                if sub and sub.endpoint_id else None
            )
            envelope = envelope_from_message(message)
            agent_id = message.agent_id
            attempt_no = message.attempt_count
            url = endpoint.url if endpoint else None
            endpoint_ok = endpoint is not None and endpoint.status == "verified"

        deliverer = self._deliverer()
        if url is None or not endpoint_ok or deliverer is None:
            self._finalize_failure(
                record_id, owner=owner, attempt_no=attempt_no, agent_id=agent_id,
                category="endpoint_unavailable", status_code=None,
            )
            return

        await self._emit(ev.EVENT_DELIVERY_STARTED, "Webhook delivery started.",
                         agent_id=agent_id, payload={"message_id": envelope["message_id"]})
        outcome = await deliverer.deliver(url=url, agent_id=agent_id, envelope=envelope)
        if outcome.ok:
            self._finalize_success(record_id, owner=owner, attempt_no=attempt_no,
                                   agent_id=agent_id, status_code=outcome.status_code)
            await self._emit(ev.EVENT_DELIVERY_SUCCEEDED, "Webhook delivered.", agent_id=agent_id)
        else:
            self._finalize_failure(
                record_id, owner=owner, attempt_no=attempt_no, agent_id=agent_id,
                category=outcome.failure_category or "unknown", status_code=outcome.status_code,
                detail=outcome.detail,
            )

    def _finalize_success(self, record_id, *, owner, attempt_no, agent_id, status_code):
        with self.runtime.session_scope() as session:
            now = utcnow()
            InteropDeliveryRepository(session).create(
                message_pk=record_id, agent_id=agent_id, attempt_no=attempt_no, mode="webhook",
                outcome="success", status_code=status_code, started_at=now,
            )
            InteropMessageRepository(session).release_and_update(
                record_id, owner=owner,
                fields={"status": "delivered", "delivered_at": now,
                        "last_status_code": status_code, "failure_category": None},
            )
            agent = InteropAgentRepository(session).get(agent_id)
            if agent is not None:
                agent.last_delivery_at = now
            session.commit()

    def _finalize_failure(self, record_id, *, owner, attempt_no, agent_id, category,
                          status_code, detail=""):
        cfg = self.config.delivery
        with self.runtime.session_scope() as session:
            now = utcnow()
            repo = InteropMessageRepository(session)
            message = repo.get(record_id)
            if message is None:
                return
            InteropDeliveryRepository(session).create(
                message_pk=record_id, agent_id=agent_id, attempt_no=attempt_no, mode="webhook",
                outcome="failure", status_code=status_code, failure_category=category,
                detail=(detail or "")[:300], started_at=now,
            )
            if message.attempt_count >= message.max_attempts:
                repo.release_and_update(
                    record_id, owner=owner,
                    fields={"status": "dead_letter", "failure_category": category,
                            "last_status_code": status_code,
                            "dead_letter_reason": f"{category}:{detail}"[:300]},
                )
                dead = True
            else:
                backoff = min(
                    cfg.retry_max_seconds,
                    cfg.retry_base_seconds * (2 ** max(0, message.attempt_count - 1)),
                )
                backoff += random.uniform(0, cfg.retry_base_seconds)
                repo.release_and_update(
                    record_id, owner=owner,
                    fields={"status": "retry_wait", "failure_category": category,
                            "last_status_code": status_code,
                            "next_attempt_at": now + timedelta(seconds=backoff)},
                )
                dead = False
            session.commit()
        if dead:
            # Best-effort: re-run notify is unnecessary; just emit sanitized telemetry.
            import asyncio
            with __import__("contextlib").suppress(Exception):
                asyncio.get_running_loop().create_task(
                    self._emit(ev.EVENT_DELIVERY_DEAD_LETTERED, "Delivery dead-lettered.",
                               agent_id=agent_id, payload={"category": category})
                )

    def redrive(self, agent_id: str, message_pk: str) -> dict:
        """Manually re-queue a dead-lettered message for one more delivery cycle."""
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            message = InteropMessageRepository(session).get(message_pk)
            if message is None or message.agent_id != agent_id:
                raise NotFoundError("message not found")
            if message.status != "dead_letter":
                raise ValidationError("only dead-lettered messages can be redriven")
            message.status = "pending"
            message.next_attempt_at = utcnow()
            message.attempt_count = 0
            message.dead_letter_reason = None
            session.commit()
            return {"message_id": message.message_id, "status": "pending"}

    # -- receipts / acknowledgements --------------------------------------------

    def record_receipt(
        self, *, agent_id: str, message_id: str, payload_digest: str | None = None,
        sequence: int | None = None, source: str = "webhook_app",
    ) -> dict:
        """Idempotently record an application acknowledgement of a delivered message."""
        with self.runtime.session_scope() as session:
            receipts = InteropReceiptRepository(session)
            existing = receipts.get_for(agent_id, message_id)
            if existing is not None:
                return {"message_id": message_id, "accepted": existing.accepted,
                        "duplicate": True}
            message = InteropMessageRepository(session).get_by_message_id(message_id)
            if message is None:
                raise NotFoundError("message not found")
            if message.agent_id != agent_id:
                self._audit(session, agent_id=agent_id, event_type=ev.EVENT_RECEIPT_REJECTED,
                            detail="cross_agent")
                session.commit()
                raise ForbiddenError("receipt for a message owned by a different agent")
            if payload_digest is not None and payload_digest != message.payload_digest:
                receipts.create(
                    agent_id=agent_id, message_id=message_id,
                    subscription_id=message.subscription_id,
                    sequence=message.sequence, payload_digest=payload_digest, source=source,
                    accepted=False, reason="payload_digest_mismatch",
                )
                self._audit(session, agent_id=agent_id, event_type=ev.EVENT_RECEIPT_REJECTED,
                            detail="digest_mismatch")
                session.commit()
                raise ValidationError("payload_digest mismatch")
            receipts.create(
                agent_id=agent_id, message_id=message_id, subscription_id=message.subscription_id,
                sequence=message.sequence, payload_digest=message.payload_digest, source=source,
                accepted=True,
            )
            message.status = "acknowledged"
            message.acknowledged_at = utcnow()
            sub = InteropSubscriptionRepository(session).get(message.subscription_id)
            if sub is not None and message.sequence > sub.last_acked_sequence:
                sub.last_acked_sequence = message.sequence
            self._audit(session, agent_id=agent_id, event_type=ev.EVENT_RECEIPT_ACCEPTED,
                        detail=f"seq={message.sequence}")
            session.commit()
            return {"message_id": message_id, "accepted": True, "duplicate": False}

    # -- websocket session helpers ----------------------------------------------

    def open_session(self, agent_id: str, subscription_id: str | None, *, label: str | None = None):
        with self.runtime.session_scope() as session:
            row = InteropSessionRepository(session).create(
                agent_id=agent_id, subscription_id=subscription_id, status="open",
                remote_label=(label or "")[:120] or None,
            )
            session.commit()
            return row.id

    def close_session(self, session_id: str, *, reason: str = "client_close",
                      last_acked: int = 0) -> None:
        with self.runtime.session_scope() as session:
            row = InteropSessionRepository(session).get(session_id)
            if row is not None:
                row.status = "closed"
                row.disconnected_at = utcnow()
                row.close_reason = reason[:120]
                row.last_acked_sequence = last_acked
                session.commit()

    def replay(self, agent_id: str, subscription_id: str, after_sequence: int) -> dict:
        """Replay persisted messages for a subscription after an acknowledged cursor."""
        with self.runtime.session_scope() as session:
            sub = InteropSubscriptionRepository(session).get(subscription_id)
            if sub is None or sub.agent_id != agent_id:
                raise NotFoundError("subscription not found")
            repo = InteropMessageRepository(session)
            lowest = repo.min_sequence(subscription_id)
            if lowest is not None and after_sequence + 1 < lowest:
                return {"replay_unavailable": True, "earliest_sequence": lowest}
            rows = repo.replay(subscription_id, after_sequence=after_sequence)
            return {"replay_unavailable": False,
                    "events": [envelope_from_message(m) for m in rows]}

    # -- read surfaces / diagnostics --------------------------------------------

    def list_agents(self, *, status: str | None = None) -> list[dict]:
        with self.runtime.session_scope() as session:
            return [
                self._agent_summary_row(a)
                for a in InteropAgentRepository(session).list(status=status)
            ]

    def agent_summary(self, agent_id: str) -> dict:
        with self.runtime.session_scope() as session:
            agent = self._require_agent(session, agent_id)
            return self._agent_summary_row(agent)

    def list_credentials(self, agent_id: str) -> list[dict]:
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            return [
                {"credential_id": c.id, "kind": c.kind, "key_id": c.key_id,
                 "fingerprint": c.fingerprint, "status": c.status,
                 "expires_at": c.expires_at.isoformat() if c.expires_at else None}
                for c in InteropCredentialRepository(session).list_for_agent(agent_id)
            ]

    def list_deliveries(self, agent_id: str, *, status: str | None = None,
                        limit: int = 200) -> list[dict]:
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            return [
                self._message_summary(m)
                for m in InteropMessageRepository(session).list_for_agent(
                    agent_id, status=status, limit=limit
                )
            ]

    def get_delivery(self, agent_id: str, message_pk: str) -> dict:
        with self.runtime.session_scope() as session:
            self._require_agent(session, agent_id)
            message = InteropMessageRepository(session).get(message_pk)
            if message is None or message.agent_id != agent_id:
                raise NotFoundError("message not found")
            summary = self._message_summary(message)
            summary["attempts"] = [
                {"attempt_no": d.attempt_no, "outcome": d.outcome, "status_code": d.status_code,
                 "failure_category": d.failure_category,
                 "created_at": d.created_at.isoformat() if d.created_at else None}
                for d in InteropDeliveryRepository(session).list_for_message(message_pk)
            ]
            return summary

    def diagnostics(self) -> dict:
        cb = self.config.callbacks
        with self.runtime.session_scope() as session:
            agents = InteropAgentRepository(session).list(limit=10000)
            counts = InteropMessageRepository(session).counts_by_status()
            open_ws = InteropSessionRepository(session).count_open()
        return {
            "enabled": self.enabled,
            "callbacks_enabled": cb.enabled,
            "websocket_enabled": self.config.websocket.enabled,
            "worker_running": self.worker.is_running(),
            "worker_degraded": self.worker.is_degraded(),
            "live_websocket_connections": self.ws.total,
            "agents_total": len(agents),
            "agents_active": sum(1 for a in agents if a.status == "active"),
            "messages_by_status": counts,
            "pending_messages": counts.get("pending", 0) + counts.get("retry_wait", 0),
            "dead_letter_messages": counts.get("dead_letter", 0),
            "open_websocket_sessions": open_ws,
        }

    # -- internal ---------------------------------------------------------------

    def _require_agent(self, session, agent_id: str, *, require_active: bool = False):
        agent = InteropAgentRepository(session).get(agent_id) if agent_id else None
        if agent is None:
            raise NotFoundError("agent not found")
        if require_active and agent.status != "active":
            raise DisabledError(f"agent is {agent.status}")
        return agent

    def _owns_mission(self, session, agent_id: str, mission_id: str) -> bool:
        mission = MissionRepository(session).get(mission_id)
        return (
            mission is not None
            and mission.source_type == "external_agent"
            and mission.source_id == agent_id
        )

    def _require_owned_mission(self, session, agent_id: str, mission_id: str):
        mission = MissionRepository(session).get(mission_id)
        if mission is None:
            raise NotFoundError("mission not found")
        if mission.source_type != "external_agent" or mission.source_id != agent_id:
            raise ForbiddenError("mission not owned by this agent")
        return mission

    @staticmethod
    def _agent_summary_row(agent) -> dict:
        return {
            "agent_id": agent.id, "display_name": agent.display_name, "status": agent.status,
            "identity_type": agent.identity_type, "fingerprint": agent.fingerprint,
            "owner": agent.owner, "permissions": list(agent.permissions_json or []),
            "endpoint_policy": agent.endpoint_policy,
            "last_authenticated_at": _iso(agent.last_authenticated_at),
            "last_delivery_at": _iso(agent.last_delivery_at),
            "created_at": _iso(agent.created_at),
        }

    @staticmethod
    def _endpoint_summary(endpoint) -> dict:
        return {
            "endpoint_id": endpoint.id, "host": endpoint.host, "scheme": endpoint.scheme,
            "port": endpoint.port, "status": endpoint.status,
            "policy_result": endpoint.policy_result_json,
            "verified_at": _iso(endpoint.verified_at),
        }

    @staticmethod
    def _subscription_summary(sub) -> dict:
        return {
            "subscription_id": sub.id, "delivery_mode": sub.delivery_mode,
            "status": sub.status, "filters": sub.filters_json,
            "endpoint_id": sub.endpoint_id, "next_sequence": sub.next_sequence,
            "last_acked_sequence": sub.last_acked_sequence,
            "created_at": _iso(sub.created_at),
        }

    @staticmethod
    def _message_summary(message) -> dict:
        return {
            "message_pk": message.id, "message_id": message.message_id,
            "event_type": message.event_type, "delivery_mode": message.delivery_mode,
            "subscription_id": message.subscription_id, "sequence": message.sequence,
            "status": message.status, "attempt_count": message.attempt_count,
            "max_attempts": message.max_attempts, "last_status_code": message.last_status_code,
            "failure_category": message.failure_category,
            "mission_id": message.mission_id, "artifact_id": message.artifact_id,
            "next_attempt_at": _iso(message.next_attempt_at),
            "delivered_at": _iso(message.delivered_at),
            "acknowledged_at": _iso(message.acknowledged_at),
            "created_at": _iso(message.created_at),
        }


# --- module-level pure helpers --------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _stable(obj) -> str:
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _bounded_metadata(metadata: dict | None, *, max_keys: int = 32, max_len: int = 4096) -> dict:
    """Return a size + key-bounded copy of a metadata dict (drops oversize/non-scalar)."""
    if not isinstance(metadata, dict):
        return {}
    out: dict = {}
    for i, (key, value) in enumerate(metadata.items()):
        if i >= max_keys:
            break
        if not isinstance(key, str) or len(key) > 128:
            continue
        if isinstance(value, str) and len(value) > max_len:
            value = value[:max_len]
        if isinstance(value, (str, int, float, bool, type(None), list, dict)):
            out[key] = value
    return out


def _bounded_filters(filters: dict | None) -> dict:
    filters = filters or {}
    out: dict = {}
    event_types = filters.get("event_types")
    if isinstance(event_types, list):
        out["event_types"] = [e for e in event_types if isinstance(e, str)][:32]
    for key in ("mission_ids", "conversation_ids"):
        values = filters.get(key)
        if isinstance(values, list):
            out[key] = [v for v in values if isinstance(v, str)][:128]
    if isinstance(filters.get("terminal_only"), bool):
        out["terminal_only"] = filters["terminal_only"]
    if isinstance(filters.get("include_artifact_ready"), bool):
        out["include_artifact_ready"] = filters["include_artifact_ready"]
    return out


def _filters_match(filters: dict, event_type: str, mission_id: str | None) -> bool:
    is_terminal = event_type in ev.EXT_TERMINAL
    if filters.get("terminal_only") and not is_terminal and event_type != ev.EXT_ARTIFACT_READY:
        return False
    if event_type == ev.EXT_ARTIFACT_READY and filters.get("include_artifact_ready") is False:
        return False
    event_types = filters.get("event_types")
    if event_types and event_type not in event_types and not is_terminal:
        return False
    mission_ids = filters.get("mission_ids")
    if mission_ids and mission_id is not None and mission_id not in mission_ids:
        return False
    return True


def _is_redundant_status(messages_repo, subscription_id: str, status) -> bool:
    """Collapse a status update identical to the most recent one on this subscription."""
    if status is None:
        return False
    rows = messages_repo.replay(subscription_id, after_sequence=0, limit=500)
    for m in reversed(rows):
        if m.event_type == ev.EXT_MISSION_STATUS:
            return (m.payload_json or {}).get("status") == status
    return False


def _mission_summary(mission) -> dict:
    return {
        "mission_id": mission.id, "status": mission.status,
        "objective": mission.content,
        "correlation_id": (mission.metadata_json or {}).get("correlation_id"),
        "created_at": _iso(mission.created_at), "updated_at": _iso(mission.updated_at),
    }


def _artifact_metadata(artifact) -> dict:
    """Sanitized artifact metadata for agents — NO filesystem/store-internal paths."""
    return {
        "artifact_id": artifact.id,
        "display_name": artifact.display_name,
        "media_type": artifact.media_type,
        "artifact_kind": artifact.artifact_kind,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.digest or artifact.content_hash,
        "availability_status": artifact.availability_state,
        "produced_by_mission": artifact.mission_id,
        "created_at": _iso(artifact.created_at),
        "retrieval_reference": f"/agent-api/artifacts/{artifact.id}",
    }


# re-export for the API layer
__all__ = ["InteropService", "AuthContext", "LiveConnection", "InteropError", "asdict"]
