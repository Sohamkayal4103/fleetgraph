"""External-agent connection contracts: schemas, enums, and errors (Stage 7).

These models are the stable boundary between the API/CLI and the communication runtime.
An external agent is a message/mission *source*; the contracts here deliberately carry no
credential value — only an ``auth_type`` hint — so the node never stores a raw secret
(secure secret storage is a later stage). A supplied secret in ``metadata`` is rejected,
not silently persisted.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from aithernet.schemas.mission_steps import MissionStepRunResponse
from aithernet.schemas.missions import MissionRead

#: Metadata keys that look like raw credentials. We refuse to persist these in Stage 7
#: rather than storing a secret; a connection records only an ``auth_type`` hint.
SECRET_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "secret",
        "auth_token",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "bearer",
        "credential",
        "credentials",
        "private_key",
    }
)


# -- errors ----------------------------------------------------------------------


class CommunicationError(Exception):
    """Base class for external-agent connection failures."""


class ExternalAgentNotFoundError(CommunicationError):
    """Raised when an operation references an external-agent id that does not exist."""


class ExternalAgentValidationError(CommunicationError):
    """Raised when a request is structurally valid but semantically rejected.

    Used for an invalid ``run_step``/``create_mission`` combination, an unknown status
    value, or a supplied raw secret — surfaced by the API as HTTP 400.
    """


class ExternalAgentDisabledError(CommunicationError):
    """Raised when a disabled agent attempts to send a message (surfaced as HTTP 400)."""


# -- enums -----------------------------------------------------------------------


class ExternalAgentStatusValue(str, Enum):
    """Connection states an external agent can occupy."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    DISABLED = "disabled"


class MessageDirection(str, Enum):
    """Direction of an external-agent message relative to this node."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


def find_secret_metadata_keys(metadata: dict) -> list[str]:
    """Return metadata keys that look like raw credentials (never stored in Stage 7).

    Used to reject (not echo) a smuggled secret: callers raise with the key *names* only,
    so the secret *value* is never reflected back in an error response.
    """
    if not isinstance(metadata, dict):
        return []
    return sorted(key for key in metadata if str(key).lower() in SECRET_METADATA_KEYS)


# -- agent schemas ---------------------------------------------------------------


class ExternalAgentCreate(BaseModel):
    """Body for ``POST /agents/connect``.

    Carries no credential value: ``auth_type`` records the scheme (e.g. "none", "bearer",
    "custom") only. A raw secret in ``metadata`` is rejected.
    """

    name: str = Field(min_length=1, description="Human-readable name for the external agent.")
    agent_type: str = Field(
        default="external",
        description="Class of agent (e.g. external, manager, peer, user_agent).",
    )
    endpoint_url: str | None = Field(
        default=None,
        description="Optional connection info, stored for later use; never called in Stage 7.",
    )
    transport: str = Field(
        default="local", description="Transport hint (e.g. local, http, websocket)."
    )
    auth_type: str = Field(
        default="none", description="Auth scheme hint (e.g. none, bearer, custom); no secret."
    )
    metadata: dict = Field(
        default_factory=dict, description="Arbitrary non-secret structured metadata."
    )


class ExternalAgentRead(BaseModel):
    """External agent as returned by the API (never includes a secret)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    agent_type: str
    endpoint_url: str | None
    transport: str
    auth_type: str | None
    status: ExternalAgentStatusValue
    metadata: dict = Field(validation_alias="metadata_json", serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime | None


class ExternalAgentStatusUpdate(BaseModel):
    """Body for ``PATCH /agents/{agent_id}/status``."""

    status: ExternalAgentStatusValue = Field(description="New connection status for the agent.")


# -- message schemas -------------------------------------------------------------


class ExternalAgentMessageCreate(BaseModel):
    """Body for ``POST /agents/{agent_id}/message``.

    ``create_mission`` (default true) turns the message into a node mission. ``run_step``
    additionally runs exactly one mission step; it requires ``create_mission`` to be true
    (Stage 7 disallows the ambiguous combination).
    """

    message_type: str = Field(
        default="mission_request",
        description="Kind of message (e.g. mission_request, status_request, reply, event).",
    )
    content: str = Field(min_length=1, description="The message body / mission instruction.")
    payload: dict = Field(
        default_factory=dict, description="Arbitrary structured payload for the message."
    )
    create_mission: bool = Field(
        default=True, description="Create a node mission from this message."
    )
    run_step: bool = Field(
        default=False,
        description="Run exactly one mission step (requires create_mission=true).",
    )


class ExternalAgentMessageRead(BaseModel):
    """An external-agent message as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    direction: MessageDirection
    message_type: str
    content: str
    payload: dict = Field(validation_alias="payload_json", serialization_alias="payload")
    mission_id: str | None
    created_at: datetime


class ExternalAgentMessageResponse(BaseModel):
    """Response for ``POST /agents/{agent_id}/message``.

    Composes the agent, the persisted inbound message, the mission created from it (if
    any), and the executed mission step (if ``run_step`` was requested).
    """

    agent: ExternalAgentRead
    message: ExternalAgentMessageRead
    mission: MissionRead | None = None
    step_response: MissionStepRunResponse | None = None


class ExternalAgentStatus(BaseModel):
    """Response for ``GET /agents/status``: a roster snapshot."""

    connected_count: int
    total_count: int
    agents: list[ExternalAgentRead]
