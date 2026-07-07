"""Operator management API for external agents (Stage 14D).

These are administrative endpoints (operator-local). They provision agents, manage
credentials/permissions/endpoints/subscriptions, and inspect deliveries / dead letters. They
are gated by ``external_agents.enabled`` and never return a secret, token, private key, lease
token, or raw path. Bearer credential creation returns its plaintext exactly once.

The agent-FACING surface (signed submission, reads, WebSocket, receipts) lives in
``agent_api.py`` and authenticates as the external agent.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from aithernet.api.app import get_runtime
from aithernet.interop.service import (
    ConflictError,
    DisabledError,
    ForbiddenError,
    InteropError,
    NotFoundError,
    ValidationError,
)
from aithernet.orchestrator.runtime import NodeRuntime

router = APIRouter(prefix="/external-agents", tags=["external-agents"])


def _require_enabled(runtime: NodeRuntime) -> None:
    if not runtime.config.external_agents.enabled:
        raise HTTPException(
            status_code=403,
            detail="The external-agent gateway is disabled (external_agents.enabled=false).",
        )


def _map_error(exc: InteropError) -> HTTPException:
    code = {
        NotFoundError: 404,
        ForbiddenError: 403,
        ConflictError: 409,
        DisabledError: 403,
        ValidationError: 400,
    }.get(type(exc), 400)
    return HTTPException(status_code=code, detail=str(exc))


# --- request bodies ---------------------------------------------------------


class AgentCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=160)
    public_key: str | None = None
    key_id: str | None = None
    permissions: list[str] | None = None
    owner: str | None = None
    endpoint_policy: str = "default"
    metadata: dict | None = None


class CredentialCreateRequest(BaseModel):
    kind: str = "ed25519"
    public_key: str | None = None
    key_id: str | None = None


class PermissionsRequest(BaseModel):
    permissions: list[str]


class StatusRequest(BaseModel):
    reason: str = ""


class EndpointCreateRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class EndpointVerifyRequest(BaseModel):
    approve: bool = True


class SubscriptionCreateRequest(BaseModel):
    delivery_mode: str = "webhook"
    endpoint_id: str | None = None
    filters: dict | None = None


class SubscriptionStateRequest(BaseModel):
    reason: str = ""


# --- agents -----------------------------------------------------------------


@router.post("", status_code=201)
def create_agent(body: AgentCreateRequest, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.create_agent(
            display_name=body.display_name, public_key=body.public_key, key_id=body.key_id,
            permissions=body.permissions, owner=body.owner,
            endpoint_policy=body.endpoint_policy, metadata=body.metadata,
        )
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.get("")
def list_agents(
    status: str | None = None, runtime: NodeRuntime = Depends(get_runtime)
) -> list[dict]:
    return runtime.interop.list_agents(status=status)


@router.get("/diagnostics")
def diagnostics(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    return runtime.interop.diagnostics()


@router.get("/{agent_id}")
def get_agent(agent_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    try:
        return runtime.interop.agent_summary(agent_id)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.post("/{agent_id}/disable")
def disable_agent(
    agent_id: str, body: StatusRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.set_status(agent_id, "disabled", reason=body.reason)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.post("/{agent_id}/enable")
def enable_agent(agent_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.set_status(agent_id, "active")
    except InteropError as exc:
        raise _map_error(exc) from exc


# --- credentials ------------------------------------------------------------


@router.post("/{agent_id}/credentials", status_code=201)
def create_credential(
    agent_id: str, body: CredentialCreateRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.add_credential(
            agent_id, kind=body.kind, public_key=body.public_key, key_id=body.key_id
        )
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.get("/{agent_id}/credentials")
def list_credentials(agent_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    try:
        return runtime.interop.list_credentials(agent_id)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.post("/{agent_id}/credentials/{credential_id}/revoke")
def revoke_credential(
    agent_id: str, credential_id: str, body: StatusRequest,
    runtime: NodeRuntime = Depends(get_runtime),
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.revoke_credential(agent_id, credential_id, reason=body.reason)
    except InteropError as exc:
        raise _map_error(exc) from exc


# --- permissions ------------------------------------------------------------


@router.get("/{agent_id}/permissions")
def get_permissions(agent_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    try:
        return {"permissions": runtime.interop.agent_summary(agent_id)["permissions"]}
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.put("/{agent_id}/permissions")
def set_permissions(
    agent_id: str, body: PermissionsRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.set_permissions(agent_id, body.permissions)
    except InteropError as exc:
        raise _map_error(exc) from exc


# --- endpoints --------------------------------------------------------------


@router.post("/{agent_id}/endpoints", status_code=201)
def add_endpoint(
    agent_id: str, body: EndpointCreateRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.register_endpoint(agent_id, body.url)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.get("/{agent_id}/endpoints")
def list_endpoints(agent_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    try:
        return runtime.interop.list_endpoints(agent_id)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.post("/{agent_id}/endpoints/{endpoint_id}/verify")
def verify_endpoint(
    agent_id: str, endpoint_id: str, body: EndpointVerifyRequest,
    runtime: NodeRuntime = Depends(get_runtime),
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.verify_endpoint(agent_id, endpoint_id, approve=body.approve)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.post("/{agent_id}/endpoints/{endpoint_id}/disable")
def disable_endpoint(
    agent_id: str, endpoint_id: str, body: StatusRequest,
    runtime: NodeRuntime = Depends(get_runtime),
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.disable_endpoint(agent_id, endpoint_id, reason=body.reason)
    except InteropError as exc:
        raise _map_error(exc) from exc


# --- subscriptions ----------------------------------------------------------


@router.post("/{agent_id}/subscriptions", status_code=201)
def create_subscription(
    agent_id: str, body: SubscriptionCreateRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.create_subscription(
            agent_id, delivery_mode=body.delivery_mode, filters=body.filters,
            endpoint_id=body.endpoint_id,
        )
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.get("/{agent_id}/subscriptions")
def list_subscriptions(agent_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    try:
        return runtime.interop.list_subscriptions(agent_id)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.post("/{agent_id}/subscriptions/{subscription_id}/pause")
def pause_subscription(
    agent_id: str, subscription_id: str, body: SubscriptionStateRequest,
    runtime: NodeRuntime = Depends(get_runtime),
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.set_subscription_state(
            agent_id, subscription_id, "paused", reason=body.reason
        )
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.post("/{agent_id}/subscriptions/{subscription_id}/resume")
def resume_subscription(
    agent_id: str, subscription_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.set_subscription_state(agent_id, subscription_id, "active")
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.delete("/{agent_id}/subscriptions/{subscription_id}")
def delete_subscription(
    agent_id: str, subscription_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    _require_enabled(runtime)
    try:
        runtime.interop.delete_subscription(agent_id, subscription_id)
        return {"subscription_id": subscription_id, "status": "revoked"}
    except InteropError as exc:
        raise _map_error(exc) from exc


# --- deliveries / dead letters ----------------------------------------------


@router.get("/{agent_id}/deliveries")
def list_deliveries(
    agent_id: str, status: str | None = None, runtime: NodeRuntime = Depends(get_runtime)
) -> list[dict]:
    try:
        return runtime.interop.list_deliveries(agent_id, status=status)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.get("/{agent_id}/deliveries/{message_pk}")
def get_delivery(
    agent_id: str, message_pk: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    try:
        return runtime.interop.get_delivery(agent_id, message_pk)
    except InteropError as exc:
        raise _map_error(exc) from exc


@router.post("/{agent_id}/deliveries/{message_pk}/redrive")
def redrive_delivery(
    agent_id: str, message_pk: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.interop.redrive(agent_id, message_pk)
    except InteropError as exc:
        raise _map_error(exc) from exc
