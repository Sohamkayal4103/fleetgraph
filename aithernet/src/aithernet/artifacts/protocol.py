"""Signed artifact-control application protocol (Stage 13D.2, Part C).

One versioned control object rides INSIDE the existing signed Stage 13A ``agent_message``
envelope's ``payload`` — so it inherits transport authentication without a second message
subsystem. Control messages carry bounded METADATA only: a transfer id, the artifact's digest
and exact byte size, correlation ids, an expiry, and the expected sender/receiver node ids.
They NEVER carry file bytes, arbitrary URLs, filesystem paths, signing material, credentials,
headers, or retry settings. Parsing is strict and bounded; failures raise a sanitized error.

A grant binds ``transfer_id`` + ``digest`` + ``size`` + ``expected_sender`` + ``expected_receiver``
+ ``expiry`` so a grant issued for one peer/artifact can never be reused by another.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, ValidationError

ARTIFACT_APPLICATION = "aithernet.artifact-control"
ARTIFACT_APPLICATION_VERSION = "1"
SUPPORTED_VERSIONS = frozenset({"1"})

MAX_NAME_CHARS = 255
MAX_REASON_CHARS = 240


class ArtifactControlType(str, Enum):
    OFFER = "artifact_offer"
    REQUEST = "artifact_request"
    GRANT = "artifact_grant"
    REJECT = "artifact_reject"
    COMPLETE = "artifact_complete"
    FAILED = "artifact_failed"


class ArtifactControlError(Exception):
    """A malformed/over-bound artifact-control payload (sanitized)."""

    code: str = "invalid_artifact_control"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ArtifactControlMessage(BaseModel):
    """The bounded, authenticated artifact-control payload (no bytes, no paths, no URLs)."""

    application: str = ARTIFACT_APPLICATION
    application_version: str = ARTIFACT_APPLICATION_VERSION
    message_type: ArtifactControlType
    transfer_id: str
    artifact_id: str | None = None
    origin_artifact_id: str | None = None
    digest: str | None = None
    size: int | None = Field(default=None, ge=0)
    artifact_kind: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    conversation_id: str | None = None
    request_id: str | None = None
    expiry: str | None = None
    expected_sender: str | None = None
    expected_receiver: str | None = None
    state: str | None = Field(default=None, max_length=24)
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)

    def to_payload(self) -> dict:
        return self.model_dump(mode="json")


def is_artifact_control(payload: object) -> bool:
    """Cheap detector: does an inbound envelope payload look like an artifact-control message?"""
    return isinstance(payload, dict) and payload.get("application") == ARTIFACT_APPLICATION


def parse_artifact_control(payload: object) -> ArtifactControlMessage:
    """Strictly parse + bound an artifact-control payload. Raises :class:`ArtifactControlError`."""
    if not isinstance(payload, dict):
        raise ArtifactControlError("Artifact-control payload must be a JSON object.")
    if payload.get("application") != ARTIFACT_APPLICATION:
        raise ArtifactControlError(
            f"Unsupported application '{payload.get('application')}'.", code="unknown_application"
        )
    if str(payload.get("application_version")) not in SUPPORTED_VERSIONS:
        raise ArtifactControlError(
            f"Unsupported application_version '{payload.get('application_version')}'.",
            code="unsupported_version",
        )
    digest = payload.get("digest")
    if digest is not None and (not isinstance(digest, str) or not digest.startswith("sha256:")):
        raise ArtifactControlError("Digest must be 'sha256:<hex>'.", code="bad_digest")
    try:
        return ArtifactControlMessage.model_validate(payload)
    except ValidationError as exc:
        raise ArtifactControlError(
            f"Malformed artifact-control message: {type(exc).__name__}."
        ) from exc
