"""Hosted control-plane ORM models (Stage 14F §7).

An independent declarative ``Base`` + database, distinct from the node store and the Stage 14E
ingestion store. The control plane and ingestion service agree on canonical *tenant* and *node*
identifiers (the same string ids) but never share a database connection — integration happens
through explicit service APIs. Secrets are never stored: invitation/enrollment/reset tokens are
stored only as digests, passwords only as scrypt hashes, node identity only as a public key.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for hosted control-plane models (independent of node + ingestion)."""


def _pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_uuid)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


# -- accounts & tenancy --------------------------------------------------------------------------


class HostedTenant(Base):
    __tablename__ = "hosted_tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="individual")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = _created()


class HostedUser(Base):
    __tablename__ = "hosted_users"
    __table_args__ = (UniqueConstraint("email", name="uq_hosted_user_email"),)

    id: Mapped[str] = _pk()
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class HostedRole(Base):
    """Seeded reference table describing each role's bounded permission set."""

    __tablename__ = "hosted_roles"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    permissions_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class HostedPlatformGrant(Base):
    """A platform-level role grant (release_manager / support_operator) — NOT tenant-scoped.

    Distinct from ``is_platform_admin`` (full platform authority) and from tenant memberships."""

    __tablename__ = "hosted_platform_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "role", name="uq_hosted_platform_grant"),
    )

    id: Mapped[str] = _pk()
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = _created()


class HostedMembership(Base):
    __tablename__ = "hosted_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_hosted_membership"),
    )

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="tenant_viewer")
    created_at: Mapped[datetime] = _created()


class HostedInvitation(Base):
    __tablename__ = "hosted_invitations"

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    proposed_tenant_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="tenant_admin")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    token_digest: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    inviter_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    policy_bundle_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    maximum_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(String(280), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class HostedSession(Base):
    __tablename__ = "hosted_sessions"

    id: Mapped[str] = _pk()
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    token_digest: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    csrf_secret: Mapped[str] = mapped_column(String(80), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()


class HostedPasswordReset(Base):
    __tablename__ = "hosted_password_resets"

    id: Mapped[str] = _pk()
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    token_digest: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()


# -- policies ------------------------------------------------------------------------------------


class HostedPolicyDocument(Base):
    __tablename__ = "hosted_policy_documents"
    __table_args__ = (
        UniqueConstraint("policy_type", "version", name="uq_hosted_policy_version"),
    )

    id: Mapped[str] = _pk()
    policy_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    effective_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    document_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    document_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    required_categories_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    optional_categories_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tenant_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class HostedPolicyAcceptance(Base):
    __tablename__ = "hosted_policy_acceptances"

    id: Mapped[str] = _pk()
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    policy_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    policy_type: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    document_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="portal")
    required_scopes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    optional_choices_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence_id: Mapped[str] = mapped_column(String(36), nullable=False, default=new_uuid)
    withdrawn: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    accepted_at: Mapped[datetime] = _created()


# -- enrollment & fleet --------------------------------------------------------------------------


class HostedEnrollmentCode(Base):
    __tablename__ = "hosted_enrollment_codes"

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    code_digest: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    creator_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    node_name_constraint: Mapped[str | None] = mapped_column(String(120), nullable=True)
    os_constraint: Mapped[str | None] = mapped_column(String(40), nullable=True)
    maximum_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()


class HostedEnrollmentChallenge(Base):
    """A short-lived server nonce a node must sign with its Ed25519 key (proof of possession)."""

    __tablename__ = "hosted_enrollment_challenges"

    id: Mapped[str] = _pk()
    code_digest: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    public_key: Mapped[str] = mapped_column(String(120), nullable=False)
    nonce: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()


class HostedNode(Base):
    __tablename__ = "hosted_nodes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "node_id", name="uq_hosted_node"),
        UniqueConstraint("public_key_fingerprint", name="uq_hosted_node_fpr"),
    )

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    public_key_fingerprint: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    software_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    release_channel: Mapped[str] = mapped_column(String(24), nullable=False, default="early-access")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    enrolled_via_code: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enrolled_at: Mapped[datetime] = _created()


class HostedMesh(Base):
    """A hosted, tenant-scoped mesh (beta.3). Synchronizes the node-side first-class mesh model.

    A mesh is bound to exactly one tenant (its authority). Cross-tenant communication is never
    implied by co-tenancy — it requires an explicit shared mesh that an administrator creates and
    populates. Holds no private key.
    """

    __tablename__ = "hosted_meshes"

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revoked: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = _created()


class HostedMeshMember(Base):
    """One node's membership in a :class:`HostedMesh` (additive; never holds a private key)."""

    __tablename__ = "hosted_mesh_members"
    __table_args__ = (
        UniqueConstraint("mesh_id", "hosted_node_id", name="uq_hosted_mesh_member"),
    )

    id: Mapped[str] = _pk()
    mesh_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    hosted_node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    revoked: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = _created()


class HostedNodeCredential(Base):
    __tablename__ = "hosted_node_credentials"
    __table_args__ = (
        UniqueConstraint("node_pk", "key_id", name="uq_hosted_node_credential"),
    )

    id: Mapped[str] = _pk()
    node_pk: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    key_id: Mapped[str] = mapped_column(String(80), nullable=False)
    public_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = _created()


class HostedNodeNonce(Base):
    __tablename__ = "hosted_node_nonces"
    __table_args__ = (
        UniqueConstraint("node_id", "nonce", name="uq_hosted_node_nonce"),
    )

    id: Mapped[str] = _pk()
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    nonce: Mapped[str] = mapped_column(String(120), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True)


class HostedNodeHeartbeat(Base):
    __tablename__ = "hosted_node_heartbeats"

    id: Mapped[str] = _pk()
    node_pk: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    software_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    readiness_summary: Mapped[str | None] = mapped_column(String(24), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class HostedNodeCapabilitySummary(Base):
    __tablename__ = "hosted_node_capability_summaries"

    node_pk: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    hardware_families_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    device_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data_export_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class HostedNodeReleaseState(Base):
    __tablename__ = "hosted_node_release_states"

    node_pk: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    current_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    channel: Mapped[str] = mapped_column(String(24), nullable=False, default="early-access")
    update_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# -- releases & downloads ------------------------------------------------------------------------


class HostedReleaseChannel(Base):
    __tablename__ = "hosted_release_channels"

    name: Mapped[str] = mapped_column(String(24), primary_key=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    current_release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class HostedRelease(Base):
    __tablename__ = "hosted_releases"
    __table_args__ = (
        UniqueConstraint("version", "channel", name="uq_hosted_release"),
    )

    id: Mapped[str] = _pk()
    version: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    minimum_supported_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    maximum_upgrade_jump: Mapped[str | None] = mapped_column(String(40), nullable=True)
    schema_compat: Mapped[str | None] = mapped_column(String(80), nullable=True)
    os_support_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    architecture_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    manifest_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    manifest_signature: Mapped[str | None] = mapped_column(String(160), nullable=True)
    notes_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    signing_key_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    supersedes: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rollback_target: Mapped[str | None] = mapped_column(String(40), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class HostedReleaseArtifact(Base):
    __tablename__ = "hosted_release_artifacts"

    id: Mapped[str] = _pk()
    release_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="wheel")
    architecture: Mapped[str] = mapped_column(String(24), nullable=False, default="any")
    os_family: Mapped[str] = mapped_column(String(24), nullable=False, default="any")
    sha256: Mapped[str] = mapped_column(String(80), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_type: Mapped[str] = mapped_column(
        String(80), nullable=False, default="application/octet-stream")
    relative_object_path: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created()


# -- support -------------------------------------------------------------------------------------


class HostedSupportCase(Base):
    __tablename__ = "hosted_support_cases"

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    opener_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="general")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    assignee_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = _created()


class HostedSupportMessage(Base):
    __tablename__ = "hosted_support_messages"

    id: Mapped[str] = _pk()
    case_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    author_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    author_role: Mapped[str] = mapped_column(String(24), nullable=False, default="customer")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = _created()


class HostedSupportBundle(Base):
    __tablename__ = "hosted_support_bundles"

    id: Mapped[str] = _pk()
    case_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    uploader_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sha256: Mapped[str] = mapped_column(String(80), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relative_object_path: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created()


# -- audit & rate limiting -----------------------------------------------------------------------


class HostedAuditRecord(Base):
    __tablename__ = "hosted_audit_records"

    id: Mapped[str] = _pk()
    actor_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(120), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class HostedRateLimitState(Base):
    __tablename__ = "hosted_rate_limit_state"
    __table_args__ = (
        UniqueConstraint("bucket", "key", "window_start", name="uq_hosted_rate_window"),
    )

    id: Mapped[str] = _pk()
    bucket: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    window_start: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class HostedBootstrapState(Base):
    """Single-row marker recording that the first platform admin has been created."""

    __tablename__ = "hosted_bootstrap_state"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="singleton")
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# -- public intake (early-access requests + mailing list) ----------------------------------------


class HostedEarlyAccessRequest(Base):
    """A public early-access request. With auto-approve OFF this is a reviewable queue item; with
    auto-approve ON it records that a system invitation was issued. Stores only the bounded fields
    needed for rate-limiting, dedup and audit — never a role/tenant chosen by the browser."""

    __tablename__ = "hosted_early_access_requests"

    id: Mapped[str] = _pk()
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    # pending | invited | approved | rejected | duplicate_suppressed
    mailing_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invitation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HostedMailingSubscriber(Base):
    """A mailing-list subscriber with DOUBLE OPT-IN. A confirmed subscription is separate from any
    Aithernet account; tokens are stored only as digests; unsubscribe is deterministic."""

    __tablename__ = "hosted_mailing_subscribers"

    id: Mapped[str] = _pk()
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    # pending | confirmed | unsubscribed
    confirm_token_digest: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    confirm_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unsubscribe_token_digest: Mapped[str | None] = mapped_column(
        String(80), nullable=True, index=True)
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="public_form")
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------------------
# beta.9 — hosted research / owner-archive pipeline. Enrolled nodes upload
# SANITIZED research packages (signed with their node identity + a scoped
# Research Upload Capability) into the OWNER/ADMIN archive. No client Google
# credentials are ever involved; the owner Drive connection is server-side only.
# Secrets are never stored (capability tokens only as digests; owner OAuth token
# only server-side in HostedOwnerArchiveConnection, never returned to clients).
# ---------------------------------------------------------------------------

class HostedResearchCapability(Base):
    """A scoped, revocable, expiring Research Upload Capability for one enrolled node.

    Minted by the control plane after enrollment + consent. It grants ONLY the ability to upload
    sanitized research packages to Aithernet hosted ingestion — never Google Drive access, never
    owner credentials. The raw token is returned once at mint; only its digest is stored."""

    __tablename__ = "research_upload_capabilities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "node_id", name="uq_research_capability_node"),
    )

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_pk: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    token_digest: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    scopes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_package_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=8 * 1024 * 1024)
    rate_limit_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=240)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class HostedResearchPackage(Base):
    """An accepted (or quarantined) sanitized research package uploaded by an enrolled node.

    The payload bytes live in the hosted blob store (object_ref); this row is secret-free metadata
    only. Deduplicated by (tenant_id, package_sha256)."""

    __tablename__ = "research_packages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "package_sha256", name="uq_research_package_dedup"),
    )

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_pk: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    package_sha256: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    release_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted", index=True)
    # accepted | quarantined
    object_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    consent_state: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    received_at: Mapped[datetime] = _created()


class HostedResearchPackageArtifact(Base):
    """A per-package artifact manifest entry (workspace-relative path + hash only; never bytes)."""

    __tablename__ = "research_package_artifacts"

    id: Mapped[str] = _pk()
    package_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    artifact_kind: Mapped[str | None] = mapped_column(String(48), nullable=True)
    created_at: Mapped[datetime] = _created()


class HostedResearchQuarantine(Base):
    """A package that failed server-side secret scanning. Metadata + category ONLY — never the
    secret; the payload is NOT archived and NOT written to owner Drive."""

    __tablename__ = "research_quarantine"

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    package_sha256: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    reason_category: Mapped[str] = mapped_column(
        String(48), nullable=False, default="residual_secret")
    detail: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created()


class HostedResearchArchiveJob(Base):
    """A queued job to write one accepted package into the owner/admin Google Drive archive.

    Queued at ingest; processed by the archive worker. If owner Drive is not connected the job
    stays ``queued`` (the client upload still succeeds — archiving is a server-side concern)."""

    __tablename__ = "research_archive_jobs"

    id: Mapped[str] = _pk()
    package_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    # queued | synced | failed
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    drive_file_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HostedResearchArchiveLedger(Base):
    """An append-only ledger row recording an archive outcome (secret-free)."""

    __tablename__ = "research_archive_ledger"

    id: Mapped[str] = _pk()
    package_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    # accepted | archived | quarantined | archive_failed
    drive_file_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    detail: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = _created()


class HostedResearchCollectionPolicy(Base):
    """The active research-collection policy for a tenant (or the platform default, tenant_id='*').

    Controls whether collection is allowed, whether beta nodes are auto-approved for a capability,
    and the max package size / retention. Owner/admin managed only."""

    __tablename__ = "research_collection_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_research_policy_tenant"),
    )

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    collection_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_approve_beta: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_package_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=8 * 1024 * 1024)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    categories_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HostedOwnerArchiveConnection(Base):
    """The SERVER-SIDE owner Google Drive connection (one per platform, tenant_id='*' by default).

    Stores the owner's OAuth refresh token server-side ONLY. It is never returned to any client API
    and never leaves the control plane. Clients neither see nor manage this."""

    __tablename__ = "research_owner_archive_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_owner_archive_tenant"),
    )

    id: Mapped[str] = _pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, default="*")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="disconnected", index=True)
    # disconnected | connected | error (error => token revoked/expired; owner must reconnect)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)  # server-side only
    root_folder_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    root_folder_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    account_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    account_email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    scope: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Bounded, non-secret failure category surfaced to the admin (e.g. "reconnect_required").
    error_category: Mapped[str | None] = mapped_column(String(48), nullable=True)
    connected_via: Mapped[str | None] = mapped_column(String(24), nullable=True)  # oauth | manual
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class HostedResearchOAuthState(Base):
    """A short-lived, single-use OAuth state nonce for the admin owner-Drive connect flow.

    Minted by an authenticated, CSRF-guarded admin request (``drive/oauth/start``) and bound to
    that admin ``user_id``. The OAuth callback validates the returned ``state`` against this row
    (exists, not consumed, not expired, and the current admin session matches ``user_id``) before
    exchanging the code. This is the CSRF/login-CSRF defence for the OAuth round-trip; it holds no
    secret."""

    __tablename__ = "research_oauth_states"
    __table_args__ = (
        UniqueConstraint("state", name="uq_research_oauth_state"),
    )

    id: Mapped[str] = _pk()
    state: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="*")
    account_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    redirect_uri: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()
