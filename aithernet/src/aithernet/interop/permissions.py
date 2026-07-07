"""External-agent permission constants and helpers (Stage 14D).

Permissions are explicit, least-privilege, and never imply node administration, peer-trust
modification, SDR transmission, or fault injection. Administrative actions live entirely in
the operator-only management API, not in any agent permission.
"""

from __future__ import annotations

PERM_MISSION_SUBMIT = "mission.submit"
PERM_MISSION_READ_OWN = "mission.read.own"
PERM_MISSION_CANCEL_OWN = "mission.cancel.own"
PERM_MISSION_SUBSCRIBE_OWN = "mission.subscribe.own"
PERM_ARTIFACT_METADATA_READ_OWN = "artifact.metadata.read.own"
PERM_ARTIFACT_CONTENT_READ_OWN = "artifact.content.read.own"
PERM_CONVERSATION_READ_OWN = "conversation.read.own"
PERM_CALLBACK_MANAGE_OWN = "callback.manage.own"
PERM_WEBSOCKET_CONNECT = "websocket.connect"
PERM_AGENT_PROFILE_READ_OWN = "agent.profile.read.own"

#: The complete set of grantable agent permissions.
ALL_PERMISSIONS = frozenset(
    {
        PERM_MISSION_SUBMIT,
        PERM_MISSION_READ_OWN,
        PERM_MISSION_CANCEL_OWN,
        PERM_MISSION_SUBSCRIBE_OWN,
        PERM_ARTIFACT_METADATA_READ_OWN,
        PERM_ARTIFACT_CONTENT_READ_OWN,
        PERM_CONVERSATION_READ_OWN,
        PERM_CALLBACK_MANAGE_OWN,
        PERM_WEBSOCKET_CONNECT,
        PERM_AGENT_PROFILE_READ_OWN,
    }
)

#: A sensible default grant for a freshly provisioned interactive agent.
DEFAULT_PERMISSIONS = (
    PERM_MISSION_SUBMIT,
    PERM_MISSION_READ_OWN,
    PERM_MISSION_CANCEL_OWN,
    PERM_MISSION_SUBSCRIBE_OWN,
    PERM_ARTIFACT_METADATA_READ_OWN,
    PERM_ARTIFACT_CONTENT_READ_OWN,
    PERM_CONVERSATION_READ_OWN,
    PERM_CALLBACK_MANAGE_OWN,
    PERM_WEBSOCKET_CONNECT,
    PERM_AGENT_PROFILE_READ_OWN,
)


def normalize_permissions(values: list[str] | None) -> list[str]:
    """Return the subset of requested permissions that are valid, de-duplicated + sorted."""
    if not values:
        return []
    return sorted({v for v in values if v in ALL_PERMISSIONS})
