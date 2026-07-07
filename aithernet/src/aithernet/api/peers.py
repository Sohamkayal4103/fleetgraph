"""Operator peer-management endpoints (Stage 13A, Part M).

Peers are external-agent rows with the additive trust/identity columns. A newly created
peer is UNTRUSTED until an operator explicitly trusts its public key. No private material is
ever exposed (a peer's public key/fingerprint are public).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.transport import (
    PeerCreate,
    PeerRead,
    PeerTrustRequest,
    PeerUpdate,
)
from aithernet.transport.errors import KeyMismatchError, PeerTrustError, TransportError

router = APIRouter(prefix="/peers", tags=["peers"])


def _peer_or_404(runtime: NodeRuntime, peer_id: str) -> PeerRead:
    peer = runtime.transport.get_peer_read(peer_id)
    if peer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Peer not found")
    return peer


@router.get("", response_model=list[PeerRead])
def list_peers(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[PeerRead]:
    """List peers, newest first (trust state, endpoint, public key/fingerprint)."""
    return runtime.transport.list_peers(limit=limit, offset=offset)


@router.post("", response_model=PeerRead, status_code=status.HTTP_201_CREATED)
async def create_peer(
    payload: PeerCreate, runtime: NodeRuntime = Depends(get_runtime)
) -> PeerRead:
    """Create an UNTRUSTED peer. Trust must be granted explicitly afterwards."""
    try:
        peer_id = await runtime.transport.create_peer(payload)
    except KeyMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _peer_or_404(runtime, peer_id)


@router.get("/{peer_id}", response_model=PeerRead)
def get_peer(peer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> PeerRead:
    return _peer_or_404(runtime, peer_id)


@router.patch("/{peer_id}", response_model=PeerRead)
async def update_peer(
    peer_id: str, payload: PeerUpdate, runtime: NodeRuntime = Depends(get_runtime)
) -> PeerRead:
    """Update non-key peer fields (name/role/endpoint/tls/enabled/metadata)."""
    _peer_or_404(runtime, peer_id)
    fields = payload.model_dump(exclude_none=True)
    if "role" in fields and hasattr(fields["role"], "value"):
        fields["role"] = fields["role"].value
    if "metadata" in fields:
        fields["metadata_json"] = fields.pop("metadata")
    with runtime.session_scope() as session:
        from aithernet.state.repositories import PeerRepository

        PeerRepository(session).update(peer_id, fields=fields)
        session.commit()
    return _peer_or_404(runtime, peer_id)


@router.post("/{peer_id}/trust", response_model=PeerRead)
async def trust_peer(
    peer_id: str, payload: PeerTrustRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> PeerRead:
    """Explicitly trust a peer, pinning its public key (never silently replaces a key)."""
    _peer_or_404(runtime, peer_id)
    try:
        await runtime.transport.trust_peer(
            peer_id, public_key=payload.public_key, fingerprint=payload.fingerprint
        )
    except KeyMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PeerTrustError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _peer_or_404(runtime, peer_id)


@router.post("/{peer_id}/revoke", response_model=PeerRead)
async def revoke_peer(peer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> PeerRead:
    """Revoke a peer: it can neither deliver to nor receive from this node."""
    _peer_or_404(runtime, peer_id)
    await runtime.transport.revoke_peer(peer_id)
    return _peer_or_404(runtime, peer_id)


@router.post("/{peer_id}/enable", response_model=PeerRead)
async def enable_peer(peer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> PeerRead:
    _peer_or_404(runtime, peer_id)
    await runtime.transport.set_peer_enabled(peer_id, enabled=True)
    return _peer_or_404(runtime, peer_id)


@router.post("/{peer_id}/disable", response_model=PeerRead)
async def disable_peer(peer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> PeerRead:
    _peer_or_404(runtime, peer_id)
    await runtime.transport.set_peer_enabled(peer_id, enabled=False)
    return _peer_or_404(runtime, peer_id)


@router.post("/{peer_id}/test")
async def test_peer(peer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """Health-check a peer endpoint (records success/failure; never grants trust)."""
    _peer_or_404(runtime, peer_id)
    try:
        return await runtime.transport.test_peer(peer_id)
    except TransportError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{peer_id}/refresh-manifest")
async def refresh_manifest(peer_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """Fetch + verify a peer's signed manifest and store it (a manifest never grants trust)."""
    _peer_or_404(runtime, peer_id)
    try:
        return await runtime.transport.refresh_manifest(peer_id)
    except KeyMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except TransportError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
