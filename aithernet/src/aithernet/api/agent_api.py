"""Agent-facing API for external agents (Stage 14D).

Every request here authenticates AS the external agent via a canonical signed request (or an
optional operator bearer credential). The agent can submit idempotent missions, read/cancel
its OWN missions, read its artifact metadata + content, read/continue its conversations,
acknowledge deliveries, and hold an authenticated WebSocket with durable replay. An agent can
never act on another agent's resources, and identity always comes from authentication — never
from request data.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from fastapi.websockets import WebSocketDisconnect

from aithernet.api.app import get_runtime
from aithernet.interop import permissions as perms
from aithernet.interop.auth import AuthContext
from aithernet.interop.service import InteropError, NotFoundError
from aithernet.orchestrator.runtime import NodeRuntime

router = APIRouter(prefix="/agent-api", tags=["agent-api"])


def _map_error(exc: InteropError) -> HTTPException:
    from aithernet.interop.service import (
        ConflictError,
        DisabledError,
        ForbiddenError,
        ValidationError,
    )

    code = {
        NotFoundError: 404, ForbiddenError: 403, ConflictError: 409,
        DisabledError: 403, ValidationError: 400,
    }.get(type(exc), 400)
    return HTTPException(status_code=code, detail=str(exc))


async def _authenticate(request: Request, runtime: NodeRuntime) -> tuple[AuthContext, bytes]:
    if not runtime.config.external_agents.enabled:
        raise HTTPException(status_code=403, detail="external-agent gateway disabled")
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()}
    ctx = runtime.interop.authenticate(
        headers=headers, method=request.method, target=request.url.path, body=body
    )
    if not ctx.ok:
        raise HTTPException(status_code=401, detail=f"authentication failed: {ctx.code}")
    return ctx, body


def _require_permission(ctx: AuthContext, permission: str) -> None:
    if permission not in ctx.permissions:
        raise HTTPException(status_code=403, detail=f"missing permission: {permission}")


def _parse_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return data


# --- missions ---------------------------------------------------------------


@router.post("/missions", status_code=202)
async def submit_mission(request: Request, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    ctx, body = await _authenticate(request, runtime)
    _require_permission(ctx, perms.PERM_MISSION_SUBMIT)
    payload = _parse_json(body)
    try:
        return await runtime.interop.submit_mission(agent_id=ctx.agent_id, body=payload)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.get("/missions/{mission_id}")
async def get_mission(
    mission_id: str, request: Request, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    ctx, _ = await _authenticate(request, runtime)
    _require_permission(ctx, perms.PERM_MISSION_READ_OWN)
    try:
        return runtime.interop.get_mission_for_agent(ctx.agent_id, mission_id)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.post("/missions/{mission_id}/cancel")
async def cancel_mission(
    mission_id: str, request: Request, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    ctx, _ = await _authenticate(request, runtime)
    _require_permission(ctx, perms.PERM_MISSION_CANCEL_OWN)
    try:
        return await runtime.interop.cancel_mission_for_agent(ctx.agent_id, mission_id)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.get("/missions/{mission_id}/events")
async def mission_events(
    mission_id: str, request: Request, runtime: NodeRuntime = Depends(get_runtime)
) -> list[dict]:
    ctx, _ = await _authenticate(request, runtime)
    _require_permission(ctx, perms.PERM_MISSION_SUBSCRIBE_OWN)
    try:
        return runtime.interop.list_mission_events_for_agent(ctx.agent_id, mission_id)
    except InteropError as exc:
        raise _map_error(exc) from exc


# --- artifacts --------------------------------------------------------------


@router.get("/artifacts/{artifact_id}")
async def get_artifact(
    artifact_id: str, request: Request, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    ctx, _ = await _authenticate(request, runtime)
    _require_permission(ctx, perms.PERM_ARTIFACT_METADATA_READ_OWN)
    try:
        return runtime.interop.get_artifact_for_agent(ctx.agent_id, artifact_id)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.get("/artifacts/{artifact_id}/content")
async def get_artifact_content(
    artifact_id: str, request: Request, runtime: NodeRuntime = Depends(get_runtime)
) -> Response:
    ctx, _ = await _authenticate(request, runtime)
    _require_permission(ctx, perms.PERM_ARTIFACT_CONTENT_READ_OWN)
    try:
        meta, data = runtime.interop.read_artifact_content(ctx.agent_id, artifact_id)
    except InteropError as exc:
        raise _map_error(exc) from exc
    return Response(
        content=data,
        media_type=meta.get("media_type") or "application/octet-stream",
        headers={"x-aithernet-artifact-sha256": meta.get("sha256") or ""},
    )


# --- conversations ----------------------------------------------------------


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str, request: Request, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    ctx, _ = await _authenticate(request, runtime)
    _require_permission(ctx, perms.PERM_CONVERSATION_READ_OWN)
    try:
        return runtime.interop.get_conversation_for_agent(ctx.agent_id, conversation_id)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.post("/conversations/{conversation_id}/messages", status_code=202)
async def conversation_message(
    conversation_id: str, request: Request, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    ctx, body = await _authenticate(request, runtime)
    _require_permission(ctx, perms.PERM_MISSION_SUBMIT)
    payload = _parse_json(body)
    try:
        return await runtime.interop.follow_up_conversation(
            ctx.agent_id, conversation_id, body=payload
        )
    except InteropError as exc:
        raise _map_error(exc) from exc


# --- receipts ---------------------------------------------------------------


@router.post("/receipts")
async def post_receipt(request: Request, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    ctx, body = await _authenticate(request, runtime)
    payload = _parse_json(body)
    message_id = payload.get("message_id")
    if not message_id:
        raise HTTPException(status_code=400, detail="message_id is required")
    try:
        return runtime.interop.record_receipt(
            agent_id=ctx.agent_id, message_id=message_id,
            payload_digest=payload.get("payload_digest"), sequence=payload.get("sequence"),
            source="webhook_app",
        )
    except InteropError as exc:
        raise _map_error(exc) from exc


# --- WebSocket --------------------------------------------------------------


@router.websocket("/ws")
async def agent_websocket(websocket: WebSocket) -> None:
    """Authenticated inbound WebSocket with subscribe / event / ack / replay / heartbeat."""
    from aithernet.interop import events as ev
    from aithernet.interop.websocket import LiveConnection

    runtime: NodeRuntime = websocket.app.state.runtime
    cfg = runtime.config.external_agents.websocket
    if not (runtime.config.external_agents.enabled and cfg.enabled):
        await websocket.close(code=1008, reason="websocket disabled")
        return
    await websocket.accept()

    # 1) Authenticated handshake — the first frame is a signed `hello`.
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=cfg.idle_timeout_seconds)
    except (TimeoutError, WebSocketDisconnect, json.JSONDecodeError, ValueError):
        await websocket.close(code=1008, reason="handshake_timeout")
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await websocket.close(code=1008, reason="expected_hello")
        return

    headers = hello.get("auth") or {}
    ctx = runtime.interop.authenticate(
        headers=headers, method="GET", target="/agent-api/ws", body=b""
    )
    if not ctx.ok or perms.PERM_WEBSOCKET_CONNECT not in ctx.permissions:
        await websocket.close(code=1008, reason=f"auth_failed:{ctx.code}")
        return

    subscription_id = hello.get("subscription_id")
    if not subscription_id:
        await websocket.close(code=1008, reason="subscription_required")
        return
    # Verify the subscription belongs to the agent and is a websocket subscription.
    subs = {s["subscription_id"]: s for s in runtime.interop.list_subscriptions(ctx.agent_id)}
    sub = subs.get(subscription_id)
    if sub is None or sub["delivery_mode"] != "websocket":
        await websocket.close(code=1008, reason="invalid_subscription")
        return

    can, reason = runtime.interop.ws.can_accept(ctx.agent_id)
    if not can:
        await websocket.close(code=1013, reason=reason)
        return

    conn = LiveConnection(
        session_id="", agent_id=ctx.agent_id, subscription_id=subscription_id
    )
    runtime.interop.ws.register(conn, queue_limit=cfg.outbound_queue_limit)
    session_id = runtime.interop.open_session(ctx.agent_id, subscription_id, label="ws")
    conn.session_id = session_id
    last_acked = int(sub.get("last_acked_sequence") or 0)
    await websocket.send_json({"type": "subscribed", "subscription_id": subscription_id})
    await runtime.interop._emit(
        ev.EVENT_WS_CONNECTED, "WebSocket connected.", agent_id=ctx.agent_id
    )

    async def _sender() -> None:
        while True:
            envelope = await conn.queue.get()
            await websocket.send_json({"type": "event", "event": envelope})

    sender = asyncio.create_task(_sender())
    try:
        while True:
            try:
                frame = await asyncio.wait_for(
                    websocket.receive_json(), timeout=cfg.heartbeat_seconds
                )
            except TimeoutError:
                await websocket.send_json({"type": "heartbeat"})
                continue
            if conn.slow:
                await websocket.close(code=1011, reason="slow_consumer")
                break
            ftype = frame.get("type") if isinstance(frame, dict) else None
            if ftype == "ack":
                mid = frame.get("message_id")
                if mid:
                    with contextlib.suppress(InteropError):
                        runtime.interop.record_receipt(
                            agent_id=ctx.agent_id, message_id=mid,
                            payload_digest=frame.get("payload_digest"), source="websocket",
                        )
                    if isinstance(frame.get("sequence"), int):
                        last_acked = max(last_acked, frame["sequence"])
            elif ftype == "replay":
                after = int(frame.get("after_sequence", last_acked) or 0)
                await runtime.interop._emit(
                    ev.EVENT_WS_REPLAY_STARTED, "WebSocket replay started.", agent_id=ctx.agent_id
                )
                result = runtime.interop.replay(ctx.agent_id, subscription_id, after)
                if result.get("replay_unavailable"):
                    await websocket.send_json({
                        "type": "replay_unavailable",
                        "earliest_sequence": result.get("earliest_sequence"),
                    })
                else:
                    for envelope in result["events"]:
                        await websocket.send_json({"type": "event", "event": envelope})
                    await websocket.send_json({"type": "replay_complete"})
                    await runtime.interop._emit(
                        ev.EVENT_WS_REPLAY_COMPLETED, "WebSocket replay completed.",
                        agent_id=ctx.agent_id,
                    )
            elif ftype == "heartbeat":
                await websocket.send_json({"type": "heartbeat"})
            elif ftype == "unsubscribe" or ftype == "close":
                break
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender
        runtime.interop.ws.unregister(conn)
        runtime.interop.close_session(session_id, last_acked=last_acked)
        with contextlib.suppress(Exception):
            await runtime.interop._emit(
                ev.EVENT_WS_DISCONNECTED, "WebSocket disconnected.", agent_id=ctx.agent_id
            )
