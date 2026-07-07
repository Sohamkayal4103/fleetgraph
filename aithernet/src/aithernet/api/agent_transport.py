"""Authenticated inbound transport + operator transport endpoints (Stage 13A, Parts H/M).

``/agent/v1/*`` is the authenticated peer-facing surface: it verifies identity, trust, and
signature, deduplicates by message id, and returns a SIGNED acknowledgement. The
``/agent-transport/*`` surface is the local operator's view of the durable outbox/inbox and
the place to create outbound messages. Inbound messages are stored as DATA — never executed.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

from aithernet.api.app import get_runtime
from aithernet.artifacts.service import ArtifactError
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.transport import (
    AgentMessageSendRequest,
    AgentMessageSendResponse,
    InboxMessageRead,
    OutboxMessageRead,
    TransportStatus,
)
from aithernet.transport.errors import IdentityError, PeerTrustError, TransportError

# Inbound peer-facing transport.
agent_v1_router = APIRouter(prefix="/agent/v1", tags=["agent-transport"])
# Local operator transport surface.
transport_router = APIRouter(prefix="/agent-transport", tags=["agent-transport"])

#: TransportError code -> HTTP status (sanitized; avoids over-distinguishing attack types).
_STATUS_BY_CODE = {
    "malformed_envelope": status.HTTP_400_BAD_REQUEST,
    "bad_kind": status.HTTP_400_BAD_REQUEST,
    "unsupported_protocol_version": status.HTTP_400_BAD_REQUEST,
    "payload_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
    "peer_not_trusted": status.HTTP_403_FORBIDDEN,
    "mesh_not_authorized": status.HTTP_403_FORBIDDEN,
    "key_mismatch": status.HTTP_403_FORBIDDEN,
    "invalid_signature": status.HTTP_401_UNAUTHORIZED,
    "recipient_mismatch": status.HTTP_421_MISDIRECTED_REQUEST,
    "expired_or_skewed": status.HTTP_400_BAD_REQUEST,
    "message_id_collision": status.HTTP_409_CONFLICT,
    "identity_error": status.HTTP_503_SERVICE_UNAVAILABLE,
    "identity_missing": status.HTTP_503_SERVICE_UNAVAILABLE,
}


# -- inbound peer-facing --------------------------------------------------------


@agent_v1_router.post("/messages")
async def receive_message(request: Request, runtime: NodeRuntime = Depends(get_runtime)):
    """Authenticated inbound delivery: verify, deduplicate, store, return a SIGNED ack.

    Enforces a body-size limit before parsing, then runs the full verification pipeline.
    Rejections map to sanitized structured errors; success returns the signed acknowledgement.
    """
    limit = runtime.config.agent_transport.inbound.maximum_payload_bytes
    raw = await request.body()
    if len(raw) > limit:
        return JSONResponse(
            {"error": "payload_too_large", "detail": "Request body exceeds the size limit."},
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            {"error": "malformed_envelope", "detail": "Body is not valid JSON."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        ack = await runtime.transport.handle_inbound(body)
    except TransportError as exc:
        http_status = _STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST)
        return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=http_status)
    return JSONResponse(ack, status_code=status.HTTP_200_OK)


#: Artifact-grant failure code -> HTTP status for the binary endpoint.
_ARTIFACT_STATUS = {
    "grant_not_found": status.HTTP_404_NOT_FOUND,
    "grant_expired": status.HTTP_410_GONE,
    "grant_mismatch": status.HTTP_403_FORBIDDEN,
    "not_found": status.HTTP_404_NOT_FOUND,
    "bad_range": status.HTTP_416_RANGE_NOT_SATISFIABLE,
}
# A pull request envelope is tiny; cap the body well below the message limit.
_MAX_PULL_BODY = 64 * 1024
# Cap a single served range (bounded memory; the worker requests chunk-sized ranges).
_MAX_RANGE_BYTES = 16 * 1024 * 1024


@agent_v1_router.post("/artifact")
async def serve_artifact_range(request: Request, runtime: NodeRuntime = Depends(get_runtime)):
    """Authenticated, grant-bound artifact byte-range pull (Stage 13D.2, Part E).

    The body is a SIGNED ``artifact_pull`` envelope (verified via the Stage 13A pipeline, never
    stored). The grant bound to (transfer, digest, size, exact receiver, expiry) is validated,
    then the requested range is streamed from the managed store. No filesystem path or directory
    is ever exposed; only the exact granted object is served. Browser clients never call this.
    """
    raw = await request.body()
    if len(raw) > _MAX_PULL_BODY:
        return JSONResponse({"error": "payload_too_large"},
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "malformed_envelope"},
                            status_code=status.HTTP_400_BAD_REQUEST)
    try:
        envelope, _peer = await runtime.transport.authenticate_envelope(
            body, expected_kind="artifact_pull"
        )
    except TransportError as exc:
        http = _STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST)
        return JSONResponse({"error": exc.code}, status_code=http)
    payload = envelope.payload if isinstance(envelope.payload, dict) else {}
    transfer_id = payload.get("transfer_id")
    try:
        offset = int(payload.get("offset", 0))
        length = int(payload.get("length", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_range"}, status_code=status.HTTP_400_BAD_REQUEST)
    if not transfer_id or offset < 0 or length < 0 or length > _MAX_RANGE_BYTES:
        return JSONResponse({"error": "bad_range"}, status_code=status.HTTP_400_BAD_REQUEST)
    try:
        data = runtime.artifacts.serve_range(
            sender_node_id=envelope.sender.node_id, transfer_id=transfer_id,
            offset=offset, length=length,
        )
    except ArtifactError as exc:
        http = _ARTIFACT_STATUS.get(exc.code, status.HTTP_403_FORBIDDEN)
        return JSONResponse({"error": exc.code}, status_code=http)
    # Raw bytes only — no path, no directory, no filename header.
    return Response(content=data, media_type="application/octet-stream")


@agent_v1_router.get("/manifest")
def get_manifest(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """Return this node's signed, compact capability manifest."""
    try:
        return runtime.transport.build_local_manifest()
    except IdentityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@agent_v1_router.get("/health")
def agent_health(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """Liveness + identity fingerprint for peer reachability checks (no secrets)."""
    info = runtime.transport.identity_status()
    return {
        "status": "ok",
        "protocol": "aithernet-agent",
        "protocol_version": "1",
        "node_id": runtime.config.node_id,
        "identity_initialized": bool(info.get("initialized")),
        "fingerprint": info.get("fingerprint"),
    }


# -- operator transport surface -------------------------------------------------


@transport_router.get("/status", response_model=TransportStatus)
def transport_status(runtime: NodeRuntime = Depends(get_runtime)) -> TransportStatus:
    """Combined transport status: identity + worker + outbox/inbox/peer counts."""
    return TransportStatus.model_validate(runtime.transport.transport_status())


@transport_router.get("/outbox", response_model=list[OutboxMessageRead])
def list_outbox(
    status_filter: str | None = Query(default=None, alias="status"),
    peer_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[OutboxMessageRead]:
    return runtime.transport.list_outbox(
        status=status_filter, peer_id=peer_id, limit=limit, offset=offset
    )


@transport_router.get("/inbox", response_model=list[InboxMessageRead])
def list_inbox(
    peer_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[InboxMessageRead]:
    return runtime.transport.list_inbox(peer_id=peer_id, limit=limit, offset=offset)


@transport_router.get("/messages", response_model=list[OutboxMessageRead])
def list_messages(
    status_filter: str | None = Query(default=None, alias="status"),
    peer_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[OutboxMessageRead]:
    """List outbound messages (the durable outbox)."""
    return runtime.transport.list_outbox(
        status=status_filter, peer_id=peer_id, limit=limit, offset=offset
    )


@transport_router.post("/messages", response_model=AgentMessageSendResponse)
async def send_message(
    payload: AgentMessageSendRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> AgentMessageSendResponse:
    """Create + sign an outbound message, persist the outbox record, and enqueue it.

    Returns after queueing (the delivery worker performs retries). ``send_now`` additionally
    attempts one delivery synchronously AFTER persisting — it never bypasses the durable record.
    """
    try:
        record_id = await runtime.transport.create_outbound_message(payload)
    except IdentityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except PeerTrustError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    detail = "Message queued for delivery."
    if payload.send_now:
        await runtime.transport.send_now(record_id)
        detail = "Message persisted and one delivery attempted."
    record = runtime.transport.get_outbox_read(record_id)
    if record is None:  # pragma: no cover - just created
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return AgentMessageSendResponse(message=record, queued=True, detail=detail)


@transport_router.get("/messages/{record_id}", response_model=OutboxMessageRead)
def get_message(record_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> OutboxMessageRead:
    record = runtime.transport.get_outbox_read(record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return record


@transport_router.post("/messages/{record_id}/retry", response_model=OutboxMessageRead)
async def retry_message(
    record_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> OutboxMessageRead:
    """Operator-requested retry of a failed/dead-lettered message (same immutable id)."""
    ok = await runtime.transport.retry_message(record_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    record = runtime.transport.get_outbox_read(record_id)
    if record is None:  # pragma: no cover
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return record


@transport_router.post("/messages/{record_id}/cancel", response_model=OutboxMessageRead)
async def cancel_message(
    record_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> OutboxMessageRead:
    """Cancel a not-yet-acknowledged message so it is never claimed/delivered again."""
    ok = await runtime.transport.cancel_message(record_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    record = runtime.transport.get_outbox_read(record_id)
    if record is None:  # pragma: no cover
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return record
