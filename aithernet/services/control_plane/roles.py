"""Hosted roles + bounded permissions (Stage 14F §11).

Authorization is always enforced server-side from the authenticated principal — never trusted
from client request data. A customer role can never view another tenant, publish platform
policies/releases, retrieve hosted secrets, modify node trust directly, or run node commands.
"""

from __future__ import annotations

# -- permissions ---------------------------------------------------------------------------------

TENANT_READ = "tenant.read"
TENANT_MANAGE = "tenant.manage"
USERS_INVITE = "users.invite"
USERS_MANAGE = "users.manage"
NODES_ENROLL = "nodes.enroll"
NODES_READ = "nodes.read"
NODES_REVOKE = "nodes.revoke"
FLEET_READ = "fleet.read"
POLICIES_ACCEPT = "policies.accept"
POLICIES_PUBLISH = "policies.publish"
RELEASES_READ = "releases.read"
RELEASES_PUBLISH = "releases.publish"
SUPPORT_CREATE = "support.create"
SUPPORT_READ_OWN = "support.read.own"
SUPPORT_MANAGE = "support.manage"
INGESTION_READ_SUMMARY = "ingestion.read.summary"
DATASETS_READ_SUMMARY = "datasets.read.summary"
RESEARCH_UPLOAD = "research.upload"           # hold a Research Upload Capability (consent-gated)
RESEARCH_READ_SUMMARY = "research.read.summary"    # view own nodes' consent/upload status
RESEARCH_ARCHIVE_MANAGE = "research.archive.manage"    # platform-only: owner Drive + archive ops
DELETIONS_REQUEST = "deletions.request"
DELETIONS_MANAGE = "deletions.manage"
PLATFORM_AUDIT_READ = "platform.audit.read"

# -- roles ---------------------------------------------------------------------------------------

PLATFORM_ADMIN = "platform_admin"
TENANT_ADMIN = "tenant_admin"
TENANT_OPERATOR = "tenant_operator"
TENANT_VIEWER = "tenant_viewer"
SUPPORT_OPERATOR = "support_operator"
RELEASE_MANAGER = "release_manager"

_CUSTOMER_VIEW = frozenset(
    {
        TENANT_READ, NODES_READ, FLEET_READ, POLICIES_ACCEPT, RELEASES_READ,
        SUPPORT_CREATE, SUPPORT_READ_OWN, INGESTION_READ_SUMMARY, DATASETS_READ_SUMMARY,
        RESEARCH_READ_SUMMARY,
    }
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    TENANT_VIEWER: _CUSTOMER_VIEW,
    TENANT_OPERATOR: _CUSTOMER_VIEW | {NODES_ENROLL, DELETIONS_REQUEST, RESEARCH_UPLOAD},
    TENANT_ADMIN: _CUSTOMER_VIEW
    | {TENANT_MANAGE, USERS_INVITE, USERS_MANAGE, NODES_ENROLL, NODES_REVOKE, DELETIONS_REQUEST,
       RESEARCH_UPLOAD},
    SUPPORT_OPERATOR: frozenset({SUPPORT_MANAGE, SUPPORT_READ_OWN, FLEET_READ, NODES_READ}),
    RELEASE_MANAGER: frozenset({RELEASES_READ, RELEASES_PUBLISH}),
    # platform_admin permissions are evaluated by is_platform_admin, not this map (see below).
    PLATFORM_ADMIN: frozenset(),
}

# Platform admins hold every permission, including the platform-only ones.
_PLATFORM_ONLY = frozenset(
    {POLICIES_PUBLISH, RELEASES_PUBLISH, DELETIONS_MANAGE, SUPPORT_MANAGE, PLATFORM_AUDIT_READ,
     RESEARCH_ARCHIVE_MANAGE}
)
ALL_PERMISSIONS = (
    set().union(*ROLE_PERMISSIONS.values()) | _PLATFORM_ONLY
)

CUSTOMER_ROLES = (TENANT_ADMIN, TENANT_OPERATOR, TENANT_VIEWER)
ALL_ROLES = (
    PLATFORM_ADMIN, TENANT_ADMIN, TENANT_OPERATOR, TENANT_VIEWER, SUPPORT_OPERATOR, RELEASE_MANAGER,
)


def role_permissions(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def role_has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
