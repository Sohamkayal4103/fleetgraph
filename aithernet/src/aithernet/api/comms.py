"""Coordinator-communication operator endpoints (Stage 13B, Part O).

Read-only views of mission communications, reply waits, conversations, and inbound requests,
plus a cancel-wait and a peer-permission update. Payloads are bounded and sanitized — never
signatures, keys, raw envelopes, auth headers, or full private payloads. Polling is read-only;
it never sends a message or resumes a mission.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.comms import CommunicationError
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.comms import (
    OperatorMessageRequest,
    OperatorMessageResponse,
    PeerPermissionUpdate,
    ReplyWaitCancelResponse,
)

router = APIRouter(tags=["communications"])

# Map a sanitized CommunicationError code to an HTTP status for the operator composer.
_COMM_ERROR_STATUS = {
    "unknown_peer": status.HTTP_404_NOT_FOUND,
    "peer_not_trusted": status.HTTP_403_FORBIDDEN,
    "peer_disabled": status.HTTP_403_FORBIDDEN,
    "not_authorized": status.HTTP_403_FORBIDDEN,
    "bad_message_type": status.HTTP_400_BAD_REQUEST,
    "text_too_large": status.HTTP_400_BAD_REQUEST,
    "no_endpoint": status.HTTP_409_CONFLICT,
    "identity_missing": status.HTTP_409_CONFLICT,
}


def _mission_or_404(runtime: NodeRuntime, mission_id: str) -> None:
    if runtime.get_mission(mission_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")


@router.post("/communications/send", response_model=OperatorMessageResponse)
async def operator_send_message(
    payload: OperatorMessageRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> OperatorMessageResponse:
    """Queue ONE operator-composed coordinator message via the durable Stage 13A outbox (Part G).

    Server-side validation enforces a trusted, enabled, authorized peer; the node signs and
    delivers. The message is persisted before delivery and is NOT linked to a local mission
    (``mission_id`` stays null), so it is distinguishable from coordinator-generated messages
    and never creates a mission here. Queued/acknowledged is NOT a semantic answer.
    """
    try:
        result = await runtime.comms.queue_peer_message(
            peer_id=payload.peer_id,
            message_type=payload.message_type,
            text=payload.text,
            data=payload.data or {},
            expects_reply=payload.expects_reply,
            conversation_id=payload.conversation_id,
            reply_to_message_id=None,
            reply_to_request_id=payload.reply_to_request_id,
            response_deadline=payload.response_deadline,
            mission_id=None,
            mission_run_id=None,
            mission_step_id=None,
            causation_id=None,
        )
    except CommunicationError as exc:
        raise HTTPException(
            status_code=_COMM_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
            detail=str(exc),
        ) from exc
    return OperatorMessageResponse(
        message_id=result["message_id"],
        request_id=result["request_id"],
        conversation_id=result.get("conversation_id"),
        peer_id=result["peer_id"],
        status=result.get("delivery_status", "pending"),
        expects_reply=payload.expects_reply,
        origin="operator",
    )


@router.get("/missions/{mission_id}/reply-waits")
def list_reply_waits(mission_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    """List the reply waits (pending/satisfied/timed_out/cancelled) for a mission."""
    _mission_or_404(runtime, mission_id)
    return runtime.comms.list_reply_waits(mission_id)


@router.get("/missions/{mission_id}/communications")
def mission_communications(
    mission_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Compact communication context: outbound messages, ACK vs reply, waits, obligation."""
    _mission_or_404(runtime, mission_id)
    return runtime.comms.mission_communication_context(mission_id)


@router.post(
    "/missions/{mission_id}/reply-waits/{wait_id}/cancel",
    response_model=ReplyWaitCancelResponse,
)
async def cancel_reply_wait(
    mission_id: str, wait_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> ReplyWaitCancelResponse:
    """Cancel a pending reply wait so a later correlated reply cannot resume the mission."""
    _mission_or_404(runtime, mission_id)
    cancelled = await runtime.comms.cancel_wait(mission_id, wait_id)
    if not cancelled and not any(
        w["wait_id"] == wait_id for w in runtime.comms.list_reply_waits(mission_id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wait not found")
    return ReplyWaitCancelResponse(cancelled=cancelled, wait_id=wait_id)


@router.get("/conversations")
def list_conversations(
    limit: int = Query(default=100, ge=1, le=1000),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[dict]:
    return runtime.comms.list_conversations(limit=limit)


@router.post("/conversations/repair")
async def repair_conversations(
    dry_run: bool = Query(default=True),
    runtime: NodeRuntime = Depends(get_runtime),
) -> dict:
    """Rethread provably-split reply rows onto their canonical conversation (Stage 13D, Part B).

    ``dry_run=true`` (default) only reports counts; ``dry_run=false`` applies the bounded,
    idempotent, non-destructive repair (no message is deleted; only the grouping column moves).
    """
    return await runtime.comms.repair_conversations(dry_run=dry_run)


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    messages = runtime.comms.conversation_messages(conversation_id)
    if not messages:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return {
        "conversation_id": conversation_id, "message_count": len(messages),
        "messages": messages,
    }


@router.get("/conversations/{conversation_id}/messages")
def conversation_messages(
    conversation_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> list[dict]:
    return runtime.comms.conversation_messages(conversation_id)


@router.get("/inbound-requests")
def list_inbound_requests(
    limit: int = Query(default=100, ge=1, le=1000),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[dict]:
    return runtime.comms.list_inbound_requests(limit=limit)


@router.get("/inbound-requests/{request_id}")
def get_inbound_request(
    request_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    record = runtime.comms.get_inbound_request(request_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Inbound request not found"
        )
    return record


@router.get("/peers/{peer_id}/permissions")
def get_peer_permissions(peer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    perms = runtime.comms.peer_permissions(peer_id)
    if perms is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Peer not found")
    return perms


@router.patch("/peers/{peer_id}/permissions")
async def set_peer_permissions(
    peer_id: str, payload: PeerPermissionUpdate, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Update a peer's application authorization (trust and authorization stay separate)."""
    perms = await runtime.comms.set_peer_permissions(
        peer_id, payload.model_dump(exclude_none=True)
    )
    if perms is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Peer not found")
    return perms
