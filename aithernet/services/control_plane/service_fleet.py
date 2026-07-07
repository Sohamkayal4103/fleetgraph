"""Fleet domain (Stage 14F §13-§16): one-time signed enrollment, node credentials, heartbeat.

Enrollment proves possession of the node's Ed25519 private key by signing a server challenge; a
bare public key is never accepted. Node-facing requests authenticate as the enrolled node via a
signed request (timestamp + nonce + body digest), NOT a website session cookie. Heartbeats are
bounded, never create mission steps, and network silence alone is never treated as failure.
"""

from __future__ import annotations

from sqlalchemy import select

from aithernet.data.ingest_auth import (
    HEADER_DIGEST,
    HEADER_KEY_ID,
    HEADER_NODE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TENANT,
    HEADER_TIMESTAMP,
    canonical_request_bytes,
    content_digest,
)
from services.control_plane import security
from services.control_plane.errors import (
    AuthError,
    ControlPlaneError,
    ForbiddenError,
    NotFoundError,
)
from services.control_plane.models import (
    HostedEnrollmentChallenge,
    HostedEnrollmentCode,
    HostedMesh,
    HostedMeshMember,
    HostedNode,
    HostedNodeCapabilitySummary,
    HostedNodeCredential,
    HostedNodeHeartbeat,
    HostedNodeNonce,
    HostedNodeReleaseState,
)
from services.control_plane.roles import NODES_ENROLL, NODES_READ, NODES_REVOKE
from services.control_plane.timeutil import ensure_aware

#: Heartbeat fields a node MUST NOT send (defensively stripped if present).
_HEARTBEAT_FORBIDDEN = frozenset(
    {"mission_prompt", "conversation", "credential", "secret", "env", "private_path",
     "rf_capture", "serial", "logs", "password"}
)


class NodeAuthResult:
    def __init__(self, tenant_id: str, node_id: str, node_pk: str, key_id: str) -> None:
        self.tenant_id = tenant_id
        self.node_id = node_id
        self.node_pk = node_pk
        self.key_id = key_id


class FleetMixin:
    # -- enrollment codes --------------------------------------------------------------------

    def create_enrollment_code(
        self, principal, *, tenant_id: str, node_name_constraint: str | None = None,
        os_constraint: str | None = None, ttl_seconds: int | None = None,
    ) -> dict:
        self.require_tenant_permission(principal, tenant_id, NODES_ENROLL)
        ttl = ttl_seconds or self.config.enrollment.code_ttl_seconds
        with self._session() as s:
            self._check_rate(s, "enrollment", principal.user_id,
                             self.config.rate_limits.enrollment_per_minute)
            raw = security.generate_token(18)
            code = HostedEnrollmentCode(
                tenant_id=tenant_id, code_digest=security.token_digest(raw),
                creator_user_id=principal.user_id, node_name_constraint=node_name_constraint,
                os_constraint=os_constraint, expires_at=self.expiry(ttl), created_at=self.now(),
            )
            s.add(code)
            self._audit(s, action="enrollment.code.create", actor_user_id=principal.user_id,
                        tenant_id=tenant_id)
            s.flush()
            cid = code.id
            s.commit()
        return {"enrollment_code_id": cid, "code": raw, "tenant_id": tenant_id,
                "expires_at": code.expires_at.isoformat()}

    def revoke_enrollment_code(self, principal, code_id: str) -> dict:
        with self._session() as s:
            code = s.get(HostedEnrollmentCode, code_id)
            if code is None:
                raise NotFoundError("enrollment_code_not_found")
            self.require_tenant_permission(principal, code.tenant_id, NODES_ENROLL)
            code.status = "revoked"
            self._audit(s, action="enrollment.code.revoke", actor_user_id=principal.user_id,
                        tenant_id=code.tenant_id)
            s.commit()
            return {"enrollment_code_id": code_id, "revoked": True}

    # -- enrollment (challenge -> signed proof) ----------------------------------------------

    def enroll_challenge(self, *, code: str, public_key: str, rate_key: str = "global") -> dict:
        digest = security.token_digest(code)
        with self._session() as s:
            self._check_rate(s, "enrollment", rate_key,
                             self.config.rate_limits.enrollment_per_minute)
            row = self._validatable_code(s, digest)
            nonce = security.generate_token(18)
            challenge = HostedEnrollmentChallenge(
                code_digest=digest, public_key=public_key, nonce=nonce,
                expires_at=self.expiry(self.config.enrollment.challenge_ttl_seconds),
                created_at=self.now(),
            )
            s.add(challenge)
            s.commit()
            # The node must sign exactly this string with its Ed25519 private key.
            return {"nonce": nonce, "challenge": _challenge_message(row.tenant_id, nonce)}

    def enroll(
        self, *, code: str, public_key: str, node_id: str, signature: str,
        software_version: str | None = None, display_name: str | None = None,
        os_family: str | None = None, rate_key: str = "global",
    ) -> dict:
        digest = security.token_digest(code)
        fingerprint = security.fingerprint_public_key(public_key)
        with self._session() as s:
            self._check_rate(s, "enrollment", rate_key,
                             self.config.rate_limits.enrollment_per_minute)
            # The most recent challenge for this (code, key) — consumed or not, so a network
            # retry that re-presents the same proof is idempotent rather than failing.
            challenge = s.execute(
                select(HostedEnrollmentChallenge).where(
                    HostedEnrollmentChallenge.code_digest == digest,
                    HostedEnrollmentChallenge.public_key == public_key,
                ).order_by(HostedEnrollmentChallenge.created_at.desc())
            ).scalars().first()
            if challenge is None or ensure_aware(challenge.expires_at) <= self.now():
                raise ControlPlaneError("enrollment_challenge_missing")
            message = _challenge_message_for(s, digest, challenge.nonce)
            if not security.verify_signature(public_key, message.encode("utf-8"), signature):
                raise AuthError("enrollment_proof_invalid")
            # Idempotent re-enrollment of the SAME key (already bound) — no code re-validation.
            existing = s.execute(
                select(HostedNode).where(HostedNode.public_key_fingerprint == fingerprint)
            ).scalar_one_or_none()
            if existing is not None:
                code_now = s.execute(
                    select(HostedEnrollmentCode).where(HostedEnrollmentCode.code_digest == digest)
                ).scalar_one_or_none()
                if code_now is not None and (
                    existing.tenant_id != code_now.tenant_id or existing.node_id != node_id
                ):
                    raise ForbiddenError("node_key_already_bound")
                challenge.consumed = True
                s.commit()
                return self._enroll_result(existing, idempotent=True)
            # New enrollment: the code must be usable and the challenge unconsumed (replay guard).
            if challenge.consumed:
                raise ControlPlaneError("enrollment_challenge_consumed")
            code_row = self._validatable_code(s, digest)
            tenant_id = code_row.tenant_id
            # A different key cannot reuse an already-consumed code.
            if code_row.used_count >= code_row.maximum_uses:
                raise ControlPlaneError("enrollment_code_consumed")
            if code_row.node_name_constraint and display_name \
                    and code_row.node_name_constraint != display_name:
                raise ControlPlaneError("node_name_constraint")
            if code_row.os_constraint and os_family and code_row.os_constraint != os_family:
                raise ControlPlaneError("os_constraint")
            node = HostedNode(
                tenant_id=tenant_id, node_id=node_id,
                display_name=display_name or node_id, public_key_fingerprint=fingerprint,
                software_version=software_version, release_channel=self.config.releases.channel,
                enrolled_via_code=code_row.id, enrolled_at=self.now(),
            )
            s.add(node)
            s.flush()
            s.add(HostedNodeCredential(
                node_pk=node.id, tenant_id=tenant_id, node_id=node_id, key_id="default",
                public_key=public_key, created_at=self.now(),
            ))
            s.add(HostedNodeReleaseState(
                node_pk=node.id, tenant_id=tenant_id, current_version=software_version,
                channel=self.config.releases.channel, last_seen_at=self.now(),
            ))
            code_row.used_count += 1
            if code_row.used_count >= code_row.maximum_uses:
                code_row.status = "consumed"
            challenge.consumed = True
            self._audit(s, action="node.enroll", tenant_id=tenant_id, target=node_id,
                        actor_kind="node")
            s.commit()
            return self._enroll_result(node, idempotent=False)

    def _validatable_code(self, s, digest: str) -> HostedEnrollmentCode:
        row = s.execute(
            select(HostedEnrollmentCode).where(HostedEnrollmentCode.code_digest == digest)
        ).scalar_one_or_none()
        if row is None:
            raise ControlPlaneError("enrollment_code_invalid")
        if row.status in ("revoked", "consumed"):
            raise ControlPlaneError("enrollment_code_unusable")
        if ensure_aware(row.expires_at) <= self.now():
            raise ControlPlaneError("enrollment_code_expired")
        return row

    def _enroll_result(self, node: HostedNode, *, idempotent: bool) -> dict:
        return {
            "hosted_node_id": node.id, "tenant_id": node.tenant_id, "node_id": node.node_id,
            "fingerprint": node.public_key_fingerprint, "release_channel": node.release_channel,
            "control_plane_base_url": self.config.urls.control_plane,
            "ingestion_base_url": self.config.urls.ingestion, "idempotent": idempotent,
        }

    # -- node-facing signed-request authentication -------------------------------------------

    def authenticate_node(self, *, headers: dict, method: str, target: str,
                          body: bytes) -> NodeAuthResult:
        h = {k.lower(): v for k, v in headers.items()}
        tenant_id = h.get(HEADER_TENANT)
        node_id = h.get(HEADER_NODE)
        key_id = h.get(HEADER_KEY_ID, "default")
        timestamp = h.get(HEADER_TIMESTAMP)
        nonce = h.get(HEADER_NONCE)
        digest = h.get(HEADER_DIGEST)
        signature = h.get(HEADER_SIGNATURE)
        if not all([tenant_id, node_id, timestamp, nonce, digest, signature]):
            raise AuthError("node_auth_incomplete")
        if digest != content_digest(body):
            raise AuthError("node_auth_digest_mismatch")
        try:
            skew = abs(int(self.now().timestamp()) - int(timestamp))
        except ValueError as exc:
            raise AuthError("node_auth_timestamp") from exc
        if skew > 300:
            raise AuthError("node_auth_clock_skew")
        with self._session() as s:
            cred = s.execute(
                select(HostedNodeCredential).where(
                    HostedNodeCredential.tenant_id == tenant_id,
                    HostedNodeCredential.node_id == node_id,
                    HostedNodeCredential.key_id == key_id,
                    HostedNodeCredential.status == "active",
                )
            ).scalar_one_or_none()
            if cred is None:
                raise AuthError("node_credential_unknown")
            node = s.get(HostedNode, cred.node_pk)
            if node is None or node.status != "active":
                raise ForbiddenError("node_revoked")
            message = canonical_request_bytes(
                tenant_id=tenant_id, node_id=node_id, key_id=key_id, method=method,
                target=target, timestamp=timestamp, nonce=nonce, digest=digest,
            )
            if not security.verify_signature(cred.public_key, message, signature):
                raise AuthError("node_signature_invalid")
            # Replay protection: a (node, nonce) pair is single-use within its retention window.
            existing = s.execute(
                select(HostedNodeNonce).where(
                    HostedNodeNonce.node_id == node_id, HostedNodeNonce.nonce == nonce
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise AuthError("node_nonce_replay")
            s.add(HostedNodeNonce(node_id=node_id, nonce=nonce, expires_at=self.expiry(600)))
            s.commit()
            return NodeAuthResult(tenant_id, node_id, node.id, key_id)

    # -- heartbeat ---------------------------------------------------------------------------

    def record_heartbeat(self, auth: NodeAuthResult, payload: dict) -> dict:
        clean = {k: v for k, v in (payload or {}).items() if k not in _HEARTBEAT_FORBIDDEN}
        sequence = int(clean.get("sequence", 0) or 0)
        with self._session() as s:
            node = s.get(HostedNode, auth.node_pk)
            if node is None or node.status != "active":
                raise ForbiddenError("node_revoked")
            last = s.execute(
                select(HostedNodeHeartbeat).where(
                    HostedNodeHeartbeat.node_pk == auth.node_pk
                ).order_by(HostedNodeHeartbeat.sequence.desc())
            ).scalars().first()
            if last is not None and sequence and sequence <= last.sequence:
                # A stale/replayed sequence is acknowledged but not advanced.
                return {"accepted": False, "reason": "stale_sequence", "sequence": last.sequence}
            version = _bounded_str(clean.get("software_version"), 40)
            s.add(HostedNodeHeartbeat(
                node_pk=auth.node_pk, tenant_id=auth.tenant_id, sequence=sequence,
                software_version=version,
                readiness_summary=_bounded_str(clean.get("readiness_summary"), 24),
                payload_json=_bounded_payload(clean), created_at=self.now(),
            ))
            if version:
                node.software_version = version
            self._upsert_capability(s, auth, clean)
            self._upsert_release_state(s, auth, clean, version)
            s.commit()
            return {"accepted": True, "sequence": sequence}

    def _upsert_capability(self, s, auth: NodeAuthResult, clean: dict) -> None:
        cap = s.get(HostedNodeCapabilitySummary, auth.node_pk)
        families = [str(x)[:40] for x in (clean.get("hardware_families") or [])][:16]
        if cap is None:
            cap = HostedNodeCapabilitySummary(node_pk=auth.node_pk, tenant_id=auth.tenant_id)
            s.add(cap)
        cap.hardware_families_json = families
        cap.device_count = int(clean.get("device_count", 0) or 0)
        cap.data_export_state = _bounded_str(clean.get("data_export_state"), 24)
        cap.updated_at = self.now()

    def _upsert_release_state(self, s, auth: NodeAuthResult, clean: dict, version) -> None:
        state = s.get(HostedNodeReleaseState, auth.node_pk)
        if state is None:
            state = HostedNodeReleaseState(node_pk=auth.node_pk, tenant_id=auth.tenant_id)
            s.add(state)
        if version:
            state.current_version = version
        state.channel = _bounded_str(clean.get("release_channel"), 24) or state.channel \
            or self.config.releases.channel
        state.update_state = _bounded_str(clean.get("update_state"), 24)
        state.last_seen_at = self.now()

    def node_config(self, auth: NodeAuthResult) -> dict:
        return {
            "tenant_id": auth.tenant_id, "node_id": auth.node_id,
            "release_channel": self.config.releases.channel,
            "ingestion_base_url": self.config.urls.ingestion,
            "heartbeat_interval_seconds": self.config.heartbeat.interval_seconds,
        }

    # -- fleet queries -----------------------------------------------------------------------

    def list_nodes(self, principal, tenant_id: str | None = None) -> list[dict]:
        with self._session() as s:
            stmt = select(HostedNode)
            if principal.is_platform_admin and tenant_id is None:
                pass
            elif tenant_id is not None:
                self.require_tenant_permission(principal, tenant_id, NODES_READ)
                stmt = stmt.where(HostedNode.tenant_id == tenant_id)
            else:
                ids = principal.tenant_ids() or [""]
                stmt = stmt.where(HostedNode.tenant_id.in_(ids))
            rows = s.execute(stmt.order_by(HostedNode.enrolled_at.desc())).scalars().all()
            return [self._node_dict(s, n) for n in rows]

    def get_node(self, principal, hosted_node_id: str) -> dict:
        with self._session() as s:
            node = s.get(HostedNode, hosted_node_id)
            if node is None:
                raise NotFoundError("node_not_found")
            self._authorize_node_view(principal, node, NODES_READ)
            return self._node_dict(s, node)

    def revoke_node(self, principal, hosted_node_id: str) -> dict:
        with self._session() as s:
            node = s.get(HostedNode, hosted_node_id)
            if node is None:
                raise NotFoundError("node_not_found")
            self._authorize_node_view(principal, node, NODES_REVOKE)
            node.status = "revoked"
            for cred in s.execute(
                select(HostedNodeCredential).where(HostedNodeCredential.node_pk == node.id)
            ).scalars().all():
                cred.status = "revoked"
            self._audit(s, action="node.revoke", actor_user_id=principal.user_id,
                        tenant_id=node.tenant_id, target=node.node_id)
            s.commit()
            return {"hosted_node_id": hosted_node_id, "status": "revoked"}

    def restore_node(self, principal, hosted_node_id: str) -> dict:
        with self._session() as s:
            node = s.get(HostedNode, hosted_node_id)
            if node is None:
                raise NotFoundError("node_not_found")
            self._authorize_node_view(principal, node, NODES_REVOKE)
            node.status = "active"
            for cred in s.execute(
                select(HostedNodeCredential).where(HostedNodeCredential.node_pk == node.id)
            ).scalars().all():
                cred.status = "active"
            self._audit(s, action="node.restore", actor_user_id=principal.user_id,
                        tenant_id=node.tenant_id, target=node.node_id)
            s.commit()
            return {"hosted_node_id": hosted_node_id, "status": "active"}

    def rename_node(self, principal, hosted_node_id: str, display_name: str) -> dict:
        """Rename an enrolled node (account-owner action; never changes the node key/identity)."""
        name = (display_name or "").strip()
        if not name or len(name) > 120:
            raise ControlPlaneError("invalid_display_name")
        with self._session() as s:
            node = s.get(HostedNode, hosted_node_id)
            if node is None:
                raise NotFoundError("node_not_found")
            self._authorize_node_view(principal, node, NODES_REVOKE)
            node.display_name = name
            self._audit(s, action="node.rename", actor_user_id=principal.user_id,
                        tenant_id=node.tenant_id, target=node.node_id)
            s.commit()
            return {"hosted_node_id": hosted_node_id, "display_name": name}

    def list_enrollment_codes(self, principal, tenant_id: str) -> list[dict]:
        """List a tenant's enrollment codes (digest only — the plaintext code is never stored)."""
        self.require_tenant_permission(principal, tenant_id, NODES_ENROLL)
        with self._session() as s:
            rows = s.execute(
                select(HostedEnrollmentCode).where(
                    HostedEnrollmentCode.tenant_id == tenant_id
                ).order_by(HostedEnrollmentCode.created_at.desc())
            ).scalars().all()
            return [
                {"enrollment_code_id": c.id, "tenant_id": c.tenant_id, "status": c.status,
                 "used_count": c.used_count, "maximum_uses": c.maximum_uses,
                 "node_name_constraint": c.node_name_constraint,
                 "created_at": c.created_at.isoformat(),
                 "expires_at": c.expires_at.isoformat() if c.expires_at else None}
                for c in rows
            ]

    def node_heartbeats(self, principal, hosted_node_id: str, limit: int = 50) -> list[dict]:
        with self._session() as s:
            node = s.get(HostedNode, hosted_node_id)
            if node is None:
                raise NotFoundError("node_not_found")
            self._authorize_node_view(principal, node, NODES_READ)
            rows = s.execute(
                select(HostedNodeHeartbeat).where(
                    HostedNodeHeartbeat.node_pk == node.id
                ).order_by(HostedNodeHeartbeat.created_at.desc()).limit(limit)
            ).scalars().all()
            return [
                {"sequence": h.sequence, "software_version": h.software_version,
                 "readiness_summary": h.readiness_summary, "created_at": h.created_at.isoformat()}
                for h in rows
            ]

    def fleet_summary(self, principal, tenant_id: str | None = None) -> dict:
        nodes = self.list_nodes(principal, tenant_id)
        states: dict[str, int] = {}
        for n in nodes:
            states[n["state"]] = states.get(n["state"], 0) + 1
        return {"total": len(nodes), "by_state": states}

    # -- meshes (beta.3; tenant-scoped; synchronize the node-side first-class mesh model) ----

    def create_mesh(self, principal, *, tenant_id: str, display_name: str) -> dict:
        self.require_tenant_permission(principal, tenant_id, NODES_ENROLL)
        name = (display_name or "").strip() or "mesh"
        with self._session() as s:
            mesh = HostedMesh(tenant_id=tenant_id, display_name=name[:160],
                              owner_user_id=principal.user_id, created_at=self.now())
            s.add(mesh)
            self._audit(s, action="mesh.create", actor_user_id=principal.user_id,
                        tenant_id=tenant_id)
            s.flush()
            mid = mesh.id
            s.commit()
        return {"mesh_id": mid, "tenant_id": tenant_id, "display_name": name}

    def list_meshes(self, principal, tenant_id: str) -> list[dict]:
        self.require_tenant_permission(principal, tenant_id, NODES_READ)
        with self._session() as s:
            rows = s.execute(
                select(HostedMesh).where(HostedMesh.tenant_id == tenant_id)
                .order_by(HostedMesh.created_at.desc())
            ).scalars().all()
            return [self._mesh_dict(s, m) for m in rows]

    def get_mesh(self, principal, mesh_id: str) -> dict:
        with self._session() as s:
            mesh = s.get(HostedMesh, mesh_id)
            if mesh is None:
                raise NotFoundError("mesh_not_found")
            self.require_tenant_permission(principal, mesh.tenant_id, NODES_READ)
            out = self._mesh_dict(s, mesh)
            out["members"] = [
                {"hosted_node_id": m.hosted_node_id, "node_id": m.node_id, "role": m.role,
                 "revoked": m.revoked}
                for m in s.execute(
                    select(HostedMeshMember).where(HostedMeshMember.mesh_id == mesh_id)
                ).scalars().all()
            ]
            return out

    def add_mesh_member(self, principal, mesh_id: str, *, hosted_node_id: str,
                        role: str = "member") -> dict:
        with self._session() as s:
            mesh = s.get(HostedMesh, mesh_id)
            if mesh is None:
                raise NotFoundError("mesh_not_found")
            self.require_tenant_permission(principal, mesh.tenant_id, NODES_ENROLL)
            node = s.get(HostedNode, hosted_node_id)
            if node is None:
                raise NotFoundError("node_not_found")
            # A hosted mesh is tenant-scoped; same-tenant membership only. Cross-tenant bridges are
            # an explicit operator action performed node-side (a shared mesh), never implied here.
            if node.tenant_id != mesh.tenant_id:
                raise ForbiddenError("cross_tenant_membership_not_allowed")
            existing = s.execute(
                select(HostedMeshMember).where(
                    HostedMeshMember.mesh_id == mesh_id,
                    HostedMeshMember.hosted_node_id == hosted_node_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = HostedMeshMember(
                    mesh_id=mesh_id, tenant_id=mesh.tenant_id, hosted_node_id=hosted_node_id,
                    node_id=node.node_id, role=role, created_at=self.now())
                s.add(existing)
            else:
                existing.revoked = False
                existing.role = role
            self._audit(s, action="mesh.member.add", actor_user_id=principal.user_id,
                        tenant_id=mesh.tenant_id, target=node.node_id)
            s.commit()
            return {"mesh_id": mesh_id, "hosted_node_id": hosted_node_id, "role": role}

    def revoke_mesh_member(self, principal, mesh_id: str, hosted_node_id: str) -> dict:
        with self._session() as s:
            mesh = s.get(HostedMesh, mesh_id)
            if mesh is None:
                raise NotFoundError("mesh_not_found")
            self.require_tenant_permission(principal, mesh.tenant_id, NODES_REVOKE)
            member = s.execute(
                select(HostedMeshMember).where(
                    HostedMeshMember.mesh_id == mesh_id,
                    HostedMeshMember.hosted_node_id == hosted_node_id,
                )
            ).scalar_one_or_none()
            if member is None:
                raise NotFoundError("mesh_member_not_found")
            member.revoked = True
            self._audit(s, action="mesh.member.revoke", actor_user_id=principal.user_id,
                        tenant_id=mesh.tenant_id, target=member.node_id)
            s.commit()
            return {"mesh_id": mesh_id, "hosted_node_id": hosted_node_id, "revoked": True}

    def _mesh_dict(self, s, mesh: HostedMesh) -> dict:
        members = s.execute(
            select(HostedMeshMember).where(
                HostedMeshMember.mesh_id == mesh.id, HostedMeshMember.revoked == False  # noqa: E712
            )
        ).scalars().all()
        return {"mesh_id": mesh.id, "tenant_id": mesh.tenant_id,
                "display_name": mesh.display_name, "policy_version": mesh.policy_version,
                "revoked": mesh.revoked, "member_count": len(members),
                "created_at": mesh.created_at.isoformat()}

    def _node_mesh_ids(self, s, hosted_node_id: str) -> list[str]:
        return [
            m.mesh_id for m in s.execute(
                select(HostedMeshMember).where(
                    HostedMeshMember.hosted_node_id == hosted_node_id,
                    HostedMeshMember.revoked == False,  # noqa: E712
                )
            ).scalars().all()
        ]

    # -- internal ----------------------------------------------------------------------------

    def _authorize_node_view(self, principal, node: HostedNode, permission: str) -> None:
        if principal.is_platform_admin:
            return
        self.require_tenant_permission(principal, node.tenant_id, permission)

    def _node_dict(self, s, node: HostedNode) -> dict:
        last = s.execute(
            select(HostedNodeHeartbeat).where(
                HostedNodeHeartbeat.node_pk == node.id
            ).order_by(HostedNodeHeartbeat.created_at.desc())
        ).scalars().first()
        cap = s.get(HostedNodeCapabilitySummary, node.id)
        return {
            "hosted_node_id": node.id, "tenant_id": node.tenant_id, "node_id": node.node_id,
            "display_name": node.display_name, "fingerprint": node.public_key_fingerprint,
            "software_version": node.software_version, "release_channel": node.release_channel,
            "status": node.status, "state": self._node_state(node, last),
            "last_heartbeat_at": last.created_at.isoformat() if last else None,
            "hardware_families": cap.hardware_families_json if cap else [],
            "device_count": cap.device_count if cap else 0,
            "data_export_state": cap.data_export_state if cap else None,
            "mesh_memberships": self._node_mesh_ids(s, node.id),
            "enrolled_at": node.enrolled_at.isoformat(),
        }

    def _node_state(self, node: HostedNode, last: HostedNodeHeartbeat | None) -> str:
        # Explicit fleet states (beta.3): revoked / never_seen / degraded / online / stale /
        # offline. Network silence alone is never "failed" — it advances online->stale->offline.
        if node.status == "revoked":
            return "revoked"
        if last is None:
            return "never_seen"
        age = (self.now() - ensure_aware(last.created_at)).total_seconds()
        hb = self.config.heartbeat
        readiness = (last.readiness_summary or "").lower()
        recently_seen = age <= hb.stale_seconds
        healthy = ("ready", "ok", "healthy", "online")
        # A recent heartbeat reporting non-ready service health is 'degraded' (still reachable).
        if recently_seen and readiness and readiness not in healthy:
            return "degraded"
        if age <= hb.recent_seconds:
            return "online"
        if age <= hb.offline_seconds:
            return "stale"
        return "offline"


def _challenge_message(tenant_id: str, nonce: str) -> str:
    return f"aithernet-enroll:{tenant_id}:{nonce}"


def _challenge_message_for(s, digest: str, nonce: str) -> str:
    code = s.execute(
        select(HostedEnrollmentCode).where(HostedEnrollmentCode.code_digest == digest)
    ).scalar_one_or_none()
    tenant_id = code.tenant_id if code else ""
    return _challenge_message(tenant_id, nonce)


def _bounded_str(value, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _bounded_payload(payload: dict) -> dict:
    out: dict = {}
    for key, value in list(payload.items())[:40]:
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)[:40]] = (value[:120] if isinstance(value, str) else value)
        elif isinstance(value, list):
            out[str(key)[:40]] = [str(v)[:60] for v in value[:16]]
    return out
