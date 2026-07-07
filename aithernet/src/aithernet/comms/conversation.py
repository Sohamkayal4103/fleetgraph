"""Deterministic canonical-conversation resolution (Stage 13D, Part B).

Stage 13C left a gap: a reply that omits ``conversation_id`` could form a second conversation
row even though its ``reply_to_request_id`` provably ties it to an existing request/reply chain.
This module resolves the single canonical conversation for a message using ONLY provable
correlation — never text similarity, timestamp proximity, keywords, or model reasoning — and is
shared by both the send path and the inbound-processing path so one logical request/reply chain
stays one conversation.

Resolution precedence (first match wins):

    1. an existing, valid explicit conversation_id
    2. the conversation of THIS node's original outbound request (application_request_id == r)
    3. the conversation of the inbound request message we received (application_request_id == r)
    4. the conversation linked to the reply wait for r
    5. the conversation linked through the durable InboundRequest record for r
    6. a freshly generated conversation_id

Correlation is restricted to the expected authenticated peer: a request-id collision from a
different peer (or a different authenticated payload) never merges conversations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aithernet.state.models import new_uuid
from aithernet.state.repositories import (
    AgentInboxRepository,
    AgentOutboxRepository,
    InboundRequestRepository,
    MissionReplyWaitRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def resolve_canonical_conversation(
    session: Session,
    *,
    explicit_conversation_id: str | None,
    reply_to_request_id: str | None,
    expected_peer_id: str | None,
) -> tuple[str, str]:
    """Return ``(conversation_id, source)`` for a message.

    ``explicit_conversation_id`` (when a non-empty string) always wins — existing valid ids are
    never rewritten. Otherwise, when ``reply_to_request_id`` is set, the chain is resolved by
    provable correlation, restricted (where peer identity is known) to ``expected_peer_id``.
    Falls back to a fresh id. ``source`` is one of: ``explicit``, ``outbound_request``,
    ``inbound_request_message``, ``reply_wait``, ``inbound_request_record``, ``generated``.
    """
    if explicit_conversation_id:
        return explicit_conversation_id, "explicit"

    request_id = reply_to_request_id
    if request_id:
        # 2. our own original outbound request with this id.
        out = AgentOutboxRepository(session).first_by_application_request_id(request_id)
        if out is not None and out.conversation_id and _peer_ok(out.peer_id, expected_peer_id):
            return out.conversation_id, "outbound_request"

        # 3. the inbound request message we received carrying this id.
        inbox = AgentInboxRepository(session).first_by_application_request_id(request_id)
        if (
            inbox is not None
            and inbox.conversation_id
            and inbox.application_type == "request"
            and _peer_ok(inbox.peer_id, expected_peer_id)
        ):
            return inbox.conversation_id, "inbound_request_message"

        # 4. a reply wait recorded for this request id.
        wait = MissionReplyWaitRepository(session).latest_for_request(request_id)
        if wait is not None and wait.conversation_id and _peer_ok(
            wait.expected_peer_id, expected_peer_id
        ):
            return wait.conversation_id, "reply_wait"

        # 5. the durable inbound-request record for this id.
        ir = InboundRequestRepository(session).get_by_request_id(request_id)
        if ir is not None and ir.conversation_id and _peer_ok(ir.peer_id, expected_peer_id):
            return ir.conversation_id, "inbound_request_record"

    return new_uuid(), "generated"


def _peer_ok(record_peer_id: str | None, expected_peer_id: str | None) -> bool:
    """Correlation is allowed only within the expected peer (when both ids are known).

    A missing expected peer (operator-side resolution where the peer is implicit) does not block
    correlation; a known mismatch always does — preventing a request-id collision from another
    peer from merging two distinct conversations.
    """
    if expected_peer_id is None or record_peer_id is None:
        return True
    return record_peer_id == expected_peer_id
