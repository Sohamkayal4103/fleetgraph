"""First-class mesh model + tenant-isolation guard (1.0.0-beta.3).

A *mesh* is the explicit trust group that authorizes peer communication. Public-key trust
(Stage 13A) authenticates *who* a peer is; mesh membership authorizes *whether* two
authenticated nodes may exchange messages at all. The two are deliberately separate:

* A mesh has an **authority** — either a hosted ``tenant`` or a ``standalone`` owner identity
  (the owner node's fingerprint). The authority binds the mesh to exactly one tenant or one
  standalone owner so a mesh can never silently span tenants.
* A mesh has **members** — node ids with pinned public keys, roles and scopes. The local node
  is itself a member. A node is reachable inside a mesh only while it is a non-revoked member.
* Cross-tenant communication requires an **explicit shared mesh**: an administrator (or both
  standalone owners) adds the foreign node to a mesh. Two nodes that can merely reach each
  other over IP or RF — even mutually key-trusted — are NOT authorized until they share a mesh.

This module holds only the pure, side-effect-free decision logic and value types. The ORM
store lives in :mod:`aithernet.state.models` / ``repositories`` and the ingress wiring lives in
:mod:`aithernet.transport.service`. Keeping the decision pure makes the isolation rules
exhaustively unit-testable without a database, a radio, or a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

#: Mesh authority kinds. ``tenant`` = a hosted control-plane tenant; ``standalone`` = a local
#: owner identity (the owner node's fingerprint) with no hosted account.
AUTHORITY_TENANT = "tenant"
AUTHORITY_STANDALONE = "standalone"

#: Default scopes a member may exercise inside a mesh. Scopes are additive capabilities; an
#: empty member scope set inherits the mesh default scopes.
SCOPE_MESSAGE = "message"          # send/receive agent messages
SCOPE_MISSION = "mission"          # submit/accept mission requests
SCOPE_ARTIFACT = "artifact"        # offer/pull artifacts
SCOPE_STATUS = "status"            # publish/query mission status
DEFAULT_MESH_SCOPES = (SCOPE_MESSAGE, SCOPE_MISSION, SCOPE_ARTIFACT, SCOPE_STATUS)

MEMBER_ROLE = "member"
ADMIN_ROLE = "admin"


class MeshDecision(str, Enum):
    """The outcome of a mesh ingress evaluation."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class MeshMemberView:
    """A bounded, secret-free view of one mesh member (as the local node knows it)."""

    node_id: str
    fingerprint: str | None
    role: str = MEMBER_ROLE
    scopes: tuple[str, ...] = ()
    revoked: bool = False


@dataclass(frozen=True)
class MeshView:
    """A bounded, secret-free view of one mesh + its membership, as the local node knows it."""

    mesh_id: str
    display_name: str
    authority_type: str
    authority_id: str
    policy_version: int = 1
    scopes: tuple[str, ...] = DEFAULT_MESH_SCOPES
    revoked: bool = False
    members: tuple[MeshMemberView, ...] = field(default_factory=tuple)

    def member(self, node_id: str) -> MeshMemberView | None:
        for m in self.members:
            if m.node_id == node_id:
                return m
        return None

    def effective_scopes(self, member: MeshMemberView) -> frozenset[str]:
        """A member's own scopes if set, else the mesh default scopes."""
        return frozenset(member.scopes or self.scopes)


@dataclass(frozen=True)
class MeshGateResult:
    """The reasoned outcome of a mesh ingress check (deny reasons are explicit + secret-free)."""

    decision: MeshDecision
    reason: str
    mesh_id: str | None = None
    authority_type: str | None = None
    authority_id: str | None = None
    enforced: bool = True

    @property
    def allowed(self) -> bool:
        return self.decision is MeshDecision.ALLOW


def evaluate_mesh_ingress(
    *,
    local_node_id: str,
    sender_node_id: str,
    sender_fingerprint: str | None,
    envelope_mesh_id: str | None,
    envelope_authority_id: str | None,
    local_meshes: list[MeshView],
    required_scope: str | None = None,
) -> MeshGateResult:
    """Decide whether an authenticated inbound message is authorized by mesh membership.

    ``local_meshes`` is the set of meshes THIS node belongs to (its own local truth — never the
    sender's claim). The sender cannot fabricate membership: every check reads the local store.

    Backward-compatibility: if the local node has **no** meshes configured, mesh enforcement is
    inactive and the result is a permissive ``ALLOW`` (the bilateral per-peer trust model from
    beta.2 stays in force). Once a node is organized into one or more meshes, every inbound
    message MUST name a mesh that both nodes belong to, or it is rejected here — before the
    message is ever handed to canonical peer ingress / the MissionEngine.
    """
    if not local_meshes:
        return MeshGateResult(
            MeshDecision.ALLOW,
            "no mesh configured on this node (bilateral peer trust)",
            enforced=False,
        )

    if not envelope_mesh_id:
        return MeshGateResult(
            MeshDecision.DENY,
            "message carries no mesh id but this node enforces mesh membership",
        )

    mesh = next((m for m in local_meshes if m.mesh_id == envelope_mesh_id), None)
    if mesh is None:
        return MeshGateResult(
            MeshDecision.DENY,
            "message mesh id is not a mesh this node belongs to",
            mesh_id=envelope_mesh_id,
        )
    if mesh.revoked:
        return MeshGateResult(
            MeshDecision.DENY, "mesh has been revoked on this node", mesh_id=mesh.mesh_id
        )

    # The authority must match the mesh's authority (a tenant id or a standalone owner id). This
    # is what makes cross-tenant spoofing impossible: a foreign tenant id never matches.
    if envelope_authority_id is not None and envelope_authority_id != mesh.authority_id:
        return MeshGateResult(
            MeshDecision.DENY,
            "message authority does not match the mesh authority (cross-tenant rejected)",
            mesh_id=mesh.mesh_id,
            authority_type=mesh.authority_type,
            authority_id=mesh.authority_id,
        )

    local_member = mesh.member(local_node_id)
    if local_member is None or local_member.revoked:
        return MeshGateResult(
            MeshDecision.DENY,
            "this node is not a current member of the named mesh",
            mesh_id=mesh.mesh_id,
        )

    sender_member = mesh.member(sender_node_id)
    if sender_member is None:
        return MeshGateResult(
            MeshDecision.DENY,
            "sender is not a member of the named mesh",
            mesh_id=mesh.mesh_id,
            authority_type=mesh.authority_type,
            authority_id=mesh.authority_id,
        )
    if sender_member.revoked:
        return MeshGateResult(
            MeshDecision.DENY,
            "sender membership in the named mesh has been revoked",
            mesh_id=mesh.mesh_id,
            authority_type=mesh.authority_type,
            authority_id=mesh.authority_id,
        )
    # The sender's mesh-member key must match the key it signed with (defence in depth — the
    # transport layer already pinned the peer key; this rejects a stale/rotated mesh record).
    if (
        sender_member.fingerprint
        and sender_fingerprint
        and sender_member.fingerprint != sender_fingerprint
    ):
        return MeshGateResult(
            MeshDecision.DENY,
            "sender key does not match its pinned mesh-member key",
            mesh_id=mesh.mesh_id,
        )

    if required_scope is not None and required_scope not in mesh.effective_scopes(sender_member):
        return MeshGateResult(
            MeshDecision.DENY,
            f"sender is not authorized for the '{required_scope}' scope in this mesh",
            mesh_id=mesh.mesh_id,
            authority_type=mesh.authority_type,
            authority_id=mesh.authority_id,
        )

    return MeshGateResult(
        MeshDecision.ALLOW,
        "sender and receiver are both members of the named mesh",
        mesh_id=mesh.mesh_id,
        authority_type=mesh.authority_type,
        authority_id=mesh.authority_id,
    )
