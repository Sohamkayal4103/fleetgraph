"""Signed mission-status application protocol (Stage 13D.3, Part B).

A node reports its OWN local mission lifecycle to the peer that requested the mission. One
versioned status object rides INSIDE the existing signed Stage 13A ``agent_message`` envelope's
``payload`` — inheriting transport authentication without a second transport or mission engine.

Payloads carry bounded FACTUAL fields only (origin node, remote mission reference, correlation
ids, the local mission state, an infrastructure-allocated monotonic sequence, timestamps, a
bounded progress summary, terminal category, and artifact/transfer counts). They NEVER carry
mission instructions, coordinator/coding prompts, coordinator reasoning, tool arguments or raw
results, environment, filesystem paths, signatures, keys, credentials, or artifact bytes.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, ValidationError

MISSION_STATUS_APPLICATION = "aithernet.mission-status"
MISSION_STATUS_VERSION = "1"
SUPPORTED_VERSIONS = frozenset({"1"})

MAX_SUMMARY_CHARS = 280
MAX_LINKED_IDS = 50

# The local mission states that are reportable (mirrors aithernet.schemas.missions.MissionStatus).
REPORTABLE_STATES = frozenset({
    "received", "queued", "active", "waiting", "paused", "blocked",
    "completed", "failed", "cancelled",
})
TERMINAL_STATES = frozenset({"completed", "blocked", "failed", "cancelled"})


class MissionStatusType(str, Enum):
    UPDATE = "status_update"
    QUERY = "status_query"
    RESPONSE = "status_response"


class MissionStatusError(Exception):
    """A malformed/over-bound mission-status payload (sanitized)."""

    code: str = "invalid_mission_status"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class MissionStatusMessage(BaseModel):
    """The bounded, authenticated mission-status payload (no prompts/tools/paths/secrets)."""

    application: str = MISSION_STATUS_APPLICATION
    application_version: str = MISSION_STATUS_VERSION
    message_type: MissionStatusType

    origin_node_id: str
    remote_mission_id: str | None = None  # the reporting node's own local mission id
    inbound_request_id: str | None = None  # the request that created that mission
    parent_request_id: str | None = None
    conversation_id: str | None = None

    state: str | None = Field(default=None, max_length=24)
    response_obligation: str | None = Field(default=None, max_length=24)
    sequence: int = Field(default=0, ge=0)
    mission_updated_at: str | None = None
    emitted_at: str | None = None
    progress_summary: str | None = Field(default=None, max_length=MAX_SUMMARY_CHARS)
    terminal_category: str | None = Field(default=None, max_length=24)

    artifact_ids: list[str] = Field(default_factory=list)
    artifact_count: int = Field(default=0, ge=0)
    transfer_ids: list[str] = Field(default_factory=list)
    transfer_count: int = Field(default=0, ge=0)

    def to_payload(self) -> dict:
        return self.model_dump(mode="json")


def is_mission_status(payload: object) -> bool:
    """Cheap detector for an inbound mission-status envelope payload."""
    return isinstance(payload, dict) and payload.get("application") == MISSION_STATUS_APPLICATION


def parse_mission_status(payload: object) -> MissionStatusMessage:
    """Strictly parse + bound a mission-status payload. Raises :class:`MissionStatusError`."""
    if not isinstance(payload, dict):
        raise MissionStatusError("Mission-status payload must be a JSON object.")
    if payload.get("application") != MISSION_STATUS_APPLICATION:
        raise MissionStatusError(
            f"Unsupported application '{payload.get('application')}'.", code="unknown_application"
        )
    if str(payload.get("application_version")) not in SUPPORTED_VERSIONS:
        raise MissionStatusError(
            f"Unsupported application_version '{payload.get('application_version')}'.",
            code="unsupported_version",
        )
    state = payload.get("state")
    if state is not None and state not in REPORTABLE_STATES:
        raise MissionStatusError(f"Unknown mission state '{state}'.", code="bad_state")
    for key in ("artifact_ids", "transfer_ids"):
        value = payload.get(key)
        if value is not None and (not isinstance(value, list) or len(value) > MAX_LINKED_IDS):
            raise MissionStatusError(f"'{key}' must be a bounded list.", code="bad_links")
    try:
        return MissionStatusMessage.model_validate(payload)
    except ValidationError as exc:
        raise MissionStatusError(
            f"Malformed mission-status message: {type(exc).__name__}."
        ) from exc


def classify_terminal(state: str | None) -> str | None:
    """Return the terminal category for a state, or None if it is non-terminal."""
    return state if state in TERMINAL_STATES else None
