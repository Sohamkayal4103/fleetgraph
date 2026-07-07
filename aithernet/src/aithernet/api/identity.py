"""Local node-identity endpoints (Stage 13A). Public metadata only — never the private key."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.transport import IdentityStatus, PublicIdentityDocument
from aithernet.transport.errors import IdentityError

router = APIRouter(prefix="/identity", tags=["identity"])


@router.get("/status", response_model=IdentityStatus)
def identity_status(runtime: NodeRuntime = Depends(get_runtime)) -> IdentityStatus:
    """Return local identity status (initialized?, fingerprint, public key — no private key)."""
    return IdentityStatus.model_validate(runtime.transport.identity_status())


@router.post("/initialize", response_model=IdentityStatus)
async def initialize_identity(
    rotate: bool = Query(default=False, description="Rotate to a NEW key (explicit only)."),
    runtime: NodeRuntime = Depends(get_runtime),
) -> IdentityStatus:
    """Generate the node identity if absent (idempotent). Rotation requires ``rotate=true``."""
    await runtime.transport.initialize_identity(rotate=rotate)
    return IdentityStatus.model_validate(runtime.transport.identity_status())


@router.get("/public", response_model=PublicIdentityDocument)
def public_identity(runtime: NodeRuntime = Depends(get_runtime)) -> PublicIdentityDocument:
    """Return the shareable public identity document (no private material)."""
    try:
        doc = runtime.transport.public_identity_document()
    except IdentityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return PublicIdentityDocument.model_validate(doc)


@router.get("/fingerprint")
def identity_fingerprint(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """Return the node identity fingerprint (short + full)."""
    info = runtime.transport.identity_status()
    if not info.get("initialized"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Identity not initialized."
        )
    return {"fingerprint": info["fingerprint"], "fingerprint_short": info["fingerprint_short"]}
