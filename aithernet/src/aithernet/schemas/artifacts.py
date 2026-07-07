"""Stage 13D.2 artifact API request/response schemas. Bounded, secret-free."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ArtifactOfferRequest(BaseModel):
    artifact_id: str
    peer_id: str
    conversation_id: str | None = None
    purpose: str | None = Field(default=None, max_length=240)


class ArtifactRequestRequest(BaseModel):
    peer_id: str
    origin_artifact_id: str
    digest: str | None = None
    size: int | None = Field(default=None, ge=0)
    conversation_id: str | None = None
    purpose: str | None = Field(default=None, max_length=240)
    mission_id: str | None = None


class ArtifactPermissionUpdate(BaseModel):
    """Per-peer artifact authorization (separate from key trust AND message authorization)."""

    may_offer_artifacts: bool | None = None
    may_request_artifacts: bool | None = None
    may_receive_artifacts: bool | None = None
    max_artifact_bytes: int | None = Field(default=None, ge=1)
    max_concurrent_transfers: int | None = Field(default=None, ge=1)
    allowed_artifact_kinds: list[str] | None = None


class ArtifactStoreGCRequest(BaseModel):
    dry_run: bool = True
