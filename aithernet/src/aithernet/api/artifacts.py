"""Stage 13D.2 operator artifact + transfer endpoints (Part H).

Typed, bounded, read-mostly. The two state-changing posts (offer/request) queue ONE signed
control message via the durable transport; the worker performs the actual byte transfer. No
endpoint ever returns bytes, absolute paths, signatures, keys, or grant authorization material.
The browser calls these operator endpoints — never a peer's binary artifact endpoint directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.artifacts.service import PERMANENT_CODES, ArtifactError
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.artifacts import (
    ArtifactOfferRequest,
    ArtifactPermissionUpdate,
    ArtifactRequestRequest,
    ArtifactStoreGCRequest,
)

router = APIRouter(tags=["artifacts"])

_ERROR_STATUS = {
    "unknown_peer": status.HTTP_404_NOT_FOUND,
    "peer_not_trusted": status.HTTP_403_FORBIDDEN,
    "not_authorized": status.HTTP_403_FORBIDDEN,
    "no_endpoint": status.HTTP_409_CONFLICT,
    "not_storable": status.HTTP_409_CONFLICT,
    "unknown_artifact": status.HTTP_400_BAD_REQUEST,
    "disabled": status.HTTP_409_CONFLICT,
    "too_large": status.HTTP_413_CONTENT_TOO_LARGE,
    "store_quota": status.HTTP_507_INSUFFICIENT_STORAGE,
    "low_disk": status.HTTP_507_INSUFFICIENT_STORAGE,
}


def _raise(exc: ArtifactError) -> None:
    raise HTTPException(
        status_code=_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST), detail=str(exc)
    ) from exc


@router.get("/artifacts")
def list_artifacts(
    limit: int = Query(default=100, ge=1, le=1000),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[dict]:
    return runtime.artifacts.list_artifacts(limit=limit)


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    art = runtime.artifacts.get_artifact(artifact_id)
    if art is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return art


@router.post("/artifacts/{artifact_id}/pin")
async def pin_artifact(artifact_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    art = await runtime.artifacts.set_pinned(artifact_id, True)
    if art is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return art


@router.post("/artifacts/{artifact_id}/unpin")
async def unpin_artifact(artifact_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    art = await runtime.artifacts.set_pinned(artifact_id, False)
    if art is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return art


@router.get("/remote-artifact-offers")
def list_remote_offers(
    limit: int = Query(default=100, ge=1, le=1000),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[dict]:
    return runtime.artifacts.list_remote_offers(limit=limit)


@router.get("/artifact-transfers")
def list_transfers(
    direction: str | None = Query(default=None),
    state: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[dict]:
    return runtime.artifacts.list_transfers(direction=direction, state=state, limit=limit)


@router.get("/artifact-transfers/{transfer_id}")
def get_transfer(transfer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    t = runtime.artifacts.get_transfer(transfer_id)
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")
    return t


@router.post("/artifact-transfers/offer")
async def offer_artifact(
    payload: ArtifactOfferRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    try:
        return await runtime.artifacts.offer_artifact(
            artifact_id=payload.artifact_id, peer_id=payload.peer_id,
            conversation_id=payload.conversation_id, purpose=payload.purpose,
        )
    except ArtifactError as exc:
        _raise(exc)


@router.post("/artifact-transfers/request")
async def request_artifact(
    payload: ArtifactRequestRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    try:
        return await runtime.artifacts.request_artifact(
            peer_id=payload.peer_id, origin_artifact_id=payload.origin_artifact_id,
            digest=payload.digest, size=payload.size, conversation_id=payload.conversation_id,
            purpose=payload.purpose, mission_id=payload.mission_id,
        )
    except ArtifactError as exc:
        _raise(exc)


@router.post("/artifact-transfers/{transfer_id}/cancel")
async def cancel_transfer(transfer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    cancelled = await runtime.artifacts.cancel_transfer(transfer_id)
    return {"cancelled": cancelled, "transfer_id": transfer_id}


@router.post("/artifact-transfers/{transfer_id}/retry")
async def retry_transfer(transfer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    retried = await runtime.artifacts.retry_transfer(transfer_id)
    if retried:
        runtime.schedule_artifact_worker()
    return {"retried": retried, "transfer_id": transfer_id}


@router.get("/artifact-store/status")
def store_status(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    return runtime.artifacts.store_status()


@router.post("/artifact-store/gc")
async def store_gc(
    payload: ArtifactStoreGCRequest | None = None, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    dry_run = True if payload is None else payload.dry_run
    return await runtime.artifacts.gc(dry_run=dry_run)


@router.get("/peers/{peer_id}/artifact-permissions")
def get_artifact_permissions(peer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    perms = runtime.artifacts.peer_artifact_permissions(peer_id)
    if perms is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Peer not found")
    return perms


@router.patch("/peers/{peer_id}/artifact-permissions")
async def set_artifact_permissions(
    peer_id: str, payload: ArtifactPermissionUpdate, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    fields = payload.model_dump(exclude_none=True)
    if "allowed_artifact_kinds" in fields:
        fields["allowed_artifact_kinds_json"] = fields.pop("allowed_artifact_kinds")
    perms = await runtime.artifacts.set_peer_artifact_permissions(peer_id, fields)
    if perms is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Peer not found")
    return perms


# Expose the permanent-failure code set for tests/diagnostics (no behavior).
__all__ = ["router", "PERMANENT_CODES"]
