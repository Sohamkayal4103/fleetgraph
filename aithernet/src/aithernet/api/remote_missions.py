"""Remote mission-status operator endpoints (Stage 13D.3, Part J).

Read-only, bounded, sanitized views of authenticated remote mission snapshots + their status
events, plus a bounded status-query action and per-peer mission-status permission management.
Every payload carries ids, states, sequence numbers, and counts only — never prompts, mission
instructions, tool contents, raw envelopes, signatures, keys, paths, credentials, or env.
Reads never mutate a mission, send a message, or resume anything.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.comms import MissionStatusPermissionUpdate

router = APIRouter(tags=["remote-missions"])


@router.get("/remote-missions")
def list_remote_missions(
    peer_id: str | None = Query(default=None),
    local_mission_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[dict]:
    """List authenticated remote-mission snapshots (latest state + freshness per relationship)."""
    return runtime.mission_status.list_snapshots(
        peer_id=peer_id, local_mission_id=local_mission_id, limit=limit
    )


@router.get("/remote-missions/{snapshot_id}")
def get_remote_mission(
    snapshot_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Show one remote-mission snapshot with its computed freshness classification."""
    snap = runtime.mission_status.get_snapshot(snapshot_id)
    if snap is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
    return snap


@router.get("/remote-missions/{snapshot_id}/events")
def remote_mission_events(
    snapshot_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[dict]:
    """Append-only evidence of received status messages + their dispositions for a snapshot."""
    if runtime.mission_status.get_snapshot(snapshot_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
    return runtime.mission_status.snapshot_events(snapshot_id, limit=limit)


@router.post("/remote-missions/{snapshot_id}/query")
async def query_remote_mission(
    snapshot_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Queue a bounded status query for a known snapshot (no enumeration, no arbitrary target)."""
    if runtime.mission_status.get_snapshot(snapshot_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
    queued = await runtime.mission_status.query_snapshot(snapshot_id)
    return {"queued": queued, "snapshot_id": snapshot_id}


@router.get("/missions/{mission_id}/status-publications")
def mission_status_publications(
    mission_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> list[dict]:
    """The per-peer status-publication bookkeeping (sequence + last state) for a local mission."""
    if runtime.get_mission(mission_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")
    return runtime.mission_status.list_publications(mission_id)


@router.get("/peers/{peer_id}/mission-status-permissions")
def get_status_permissions(peer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """A peer's mission-status authorization (separate from trust, message, artifact perms)."""
    perms = runtime.mission_status.peer_permissions(peer_id)
    if perms is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Peer not found")
    return perms


@router.patch("/peers/{peer_id}/mission-status-permissions")
async def set_status_permissions(
    peer_id: str, payload: MissionStatusPermissionUpdate,
    runtime: NodeRuntime = Depends(get_runtime),
) -> dict:
    """Update a peer's mission-status authorization (trust and authorization stay separate)."""
    perms = await runtime.mission_status.set_peer_permissions(
        peer_id, payload.model_dump(exclude_none=True)
    )
    if perms is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Peer not found")
    return perms
