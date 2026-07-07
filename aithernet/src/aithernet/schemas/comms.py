"""Coordinator-communication API schemas (Stage 13B + 13C). Bounded, secret-free."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OperatorMessageRequest(BaseModel):
    """A bounded operator-composed coordinator message (Stage 13C, Part G).

    The operator may only choose a TRUSTED, ENABLED, authorized peer (validated server-side),
    a message type, bounded text/data, and reply expectations. There is deliberately NO field
    for an endpoint, signing identity, HTTP header, or retry policy — the node signs and
    delivers through the existing Stage 13A durable outbox. The message is persisted before any
    delivery attempt and never creates a mission on this node.
    """

    peer_id: str
    message_type: Literal["request", "update", "reply"] = "request"
    text: str = ""
    data: dict | None = None
    expects_reply: bool = False
    response_deadline: str | None = None
    conversation_id: str | None = None
    reply_to_request_id: str | None = None


class OperatorMessageResponse(BaseModel):
    """The queued outbox record for an operator message — queued/acknowledged is NOT answered."""

    message_id: str
    request_id: str
    conversation_id: str | None = None
    peer_id: str
    status: str
    expects_reply: bool
    origin: str = "operator"


class MissionStatusPermissionUpdate(BaseModel):
    """Per-peer mission-status authorization (Stage 13D.3; separate from trust/message/artifact).

    ``may_publish`` lets the peer publish its mission status TO us; ``may_receive`` lets us send
    OUR status to the peer; ``may_query`` lets the peer query us. All default false.
    """

    may_publish_mission_status: bool | None = None
    may_query_mission_status: bool | None = None
    may_receive_mission_status: bool | None = None
    max_active_remote_snapshots: int | None = Field(default=None, ge=1)


class PeerPermissionUpdate(BaseModel):
    """Application-authorization update for a peer (separate from public-key trust)."""

    may_send_requests: bool | None = None
    may_send_replies: bool | None = None
    may_receive_messages: bool | None = None
    may_request_response: bool | None = None
    max_inbound_request_bytes: int | None = Field(default=None, ge=1)
    max_concurrent_inbound_missions: int | None = Field(default=None, ge=1)


class ReplyWaitCancelResponse(BaseModel):
    cancelled: bool
    wait_id: str
