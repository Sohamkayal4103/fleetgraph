"""The application-level coordinator-message payload (Stage 13B, Part B).

One versioned application object travels INSIDE the existing signed Stage 13A ``agent_message``
envelope's ``payload`` dict — so it inherits Stage 13A authentication without a new envelope
kind or a second message subsystem. Authentication establishes only the *sender identity*; the
``text`` and ``data`` are treated as UNTRUSTED content even after the signature verifies. The
payload carries NO executable semantics: parsing it never runs a tool, shell command, or code,
and never deserializes arbitrary Python objects. Parsing is strict and bounded; failures raise
a sanitized :class:`CoordinatorMessageError`.
"""

from __future__ import annotations

import json
from enum import Enum

from pydantic import BaseModel, Field, ValidationError

from aithernet.state.models import new_uuid

#: Stable application id + version carried in every coordinator-message payload.
APPLICATION = "aithernet.coordinator-message"
APPLICATION_VERSION = "1"
SUPPORTED_APPLICATION_VERSIONS = frozenset({"1"})


class CoordinatorMessageType(str, Enum):
    REQUEST = "request"
    REPLY = "reply"
    UPDATE = "update"


class CoordinatorMessageError(Exception):
    """Raised when an application payload is missing, malformed, or over a configured bound."""

    code: str = "invalid_application_message"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class CoordinatorMessage(BaseModel):
    """The bounded, authenticated application payload (no executable semantics)."""

    application: str = APPLICATION
    application_version: str = APPLICATION_VERSION
    message_type: CoordinatorMessageType = CoordinatorMessageType.REQUEST
    text: str = ""
    data: dict = Field(default_factory=dict)
    expects_reply: bool = False
    response_deadline: str | None = None
    request_id: str = Field(default_factory=new_uuid)
    reply_to_request_id: str | None = None

    def to_payload(self) -> dict:
        """The dict that becomes the signed envelope's ``payload``."""
        return self.model_dump(mode="json")


def is_coordinator_message(payload: object) -> bool:
    """Cheap detector: does an inbound envelope payload look like a coordinator-message?"""
    return isinstance(payload, dict) and payload.get("application") == APPLICATION


def _json_depth(value: object, depth: int = 0) -> int:
    if isinstance(value, dict):
        return max((_json_depth(v, depth + 1) for v in value.values()), default=depth)
    if isinstance(value, list):
        return max((_json_depth(v, depth + 1) for v in value), default=depth)
    return depth


def parse_coordinator_message(
    payload: object,
    *,
    max_text_characters: int,
    max_data_bytes: int,
    max_data_depth: int,
) -> CoordinatorMessage:
    """Strictly parse + bound an application payload. Raises :class:`CoordinatorMessageError`.

    Enforces: the application tag + a supported version; a string ``text`` within the character
    bound; an object ``data`` within the byte and nesting bounds; an explicit, known
    ``message_type``. No field carries behavior the signature does not authenticate.
    """
    if not isinstance(payload, dict):
        raise CoordinatorMessageError("Application payload must be a JSON object.")
    if payload.get("application") != APPLICATION:
        raise CoordinatorMessageError(
            f"Unsupported application '{payload.get('application')}'.", code="unknown_application"
        )
    if str(payload.get("application_version")) not in SUPPORTED_APPLICATION_VERSIONS:
        raise CoordinatorMessageError(
            f"Unsupported application_version '{payload.get('application_version')}'.",
            code="unsupported_application_version",
        )
    text = payload.get("text", "")
    if not isinstance(text, str):
        raise CoordinatorMessageError("Application 'text' must be a string.")
    if len(text) > max_text_characters:
        raise CoordinatorMessageError(
            f"Application 'text' exceeds {max_text_characters} characters.",
            code="text_too_large",
        )
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise CoordinatorMessageError("Application 'data' must be a JSON object.")
    try:
        encoded = json.dumps(data, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CoordinatorMessageError("Application 'data' is not JSON-serializable.") from exc
    if len(encoded.encode("utf-8")) > max_data_bytes:
        raise CoordinatorMessageError(
            f"Application 'data' exceeds {max_data_bytes} bytes.", code="data_too_large"
        )
    if _json_depth(data) > max_data_depth:
        raise CoordinatorMessageError(
            f"Application 'data' nesting exceeds depth {max_data_depth}.", code="data_too_deep"
        )
    try:
        return CoordinatorMessage.model_validate(payload)
    except ValidationError as exc:  # unknown message_type / wrong field types
        raise CoordinatorMessageError(
            f"Malformed coordinator message: {type(exc).__name__}."
        ) from exc
