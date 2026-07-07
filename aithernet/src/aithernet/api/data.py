"""Operator data & privacy API (Stage 14E, Part 24).

Bounded operator endpoints for consent, local inspection, the export queue, destinations,
deletion, and the dataset registry. Inspection is operator-only and never exposes a secret that
redaction removed. No endpoint accepts arbitrary SQL, a destination URL/path beyond bounded
config fields, a Drive folder path, an encryption key, or a filesystem path; disruptive actions
(consent withdrawal, deletion, destination enablement, redrive) are guarded by the gateway flag.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from aithernet.api.app import get_runtime
from aithernet.data.service import (
    ConflictError,
    ConsentError,
    DataError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from aithernet.orchestrator.runtime import NodeRuntime

router = APIRouter(prefix="/data", tags=["data"])


def _require_enabled(runtime: NodeRuntime) -> None:
    if not runtime.config.data_platform.enabled:
        raise HTTPException(status_code=403, detail="data platform disabled")


def _map(exc: DataError) -> HTTPException:
    code = {NotFoundError: 404, ForbiddenError: 403, ConflictError: 409,
            ValidationError: 400, ConsentError: 409}.get(type(exc), 400)
    return HTTPException(status_code=code, detail=str(exc))


# --- request bodies ---------------------------------------------------------


class ProfileAcceptRequest(BaseModel):
    policy_version: str = Field(min_length=1)
    document_digest: str | None = None
    required_categories: list[str] | None = None
    optional_categories: list[str] | None = None


class ConsentGrantRequest(BaseModel):
    category: str
    purpose: str | None = None
    collection: bool = True
    export: bool = False
    raw_artifact: bool = False
    destination_scope: str = "local"


class WithdrawRequest(BaseModel):
    reason: str = ""
    quarantine: bool = True


class PauseRequest(BaseModel):
    paused: bool


class PreviewRequest(BaseModel):
    category: str
    payload: dict


class DestinationCreateRequest(BaseModel):
    kind: str
    name: str
    config: dict | None = None


class BatchBuildRequest(BaseModel):
    destination_id: str
    category: str | None = None
    max_records: int | None = Field(default=None, ge=1, le=100000)


class DeletionRequest(BaseModel):
    scope: str
    target_id: str | None = None
    subject: str | None = None
    category: str | None = None
    reason: str = ""
    hold: bool = False


class DatasetCreateRequest(BaseModel):
    name: str
    purpose: str | None = None


class DatasetVersionRequest(BaseModel):
    split_label: str | None = None
    exclusions: list[str] | None = None


# --- consent ----------------------------------------------------------------


@router.get("/consent")
def get_consent(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    return {"effective": runtime.data.effective_consent(), "grants": runtime.data.list_grants()}


@router.post("/consent/profile", status_code=201)
def accept_profile(body: ProfileAcceptRequest, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.accept_profile(
            policy_version=body.policy_version, document_digest=body.document_digest,
            required_categories=body.required_categories,
            optional_categories=body.optional_categories,
        )
    except DataError as exc:
        raise _map(exc) from exc


@router.post("/consent/grants", status_code=201)
def grant_consent(body: ConsentGrantRequest, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.grant_consent(
            category=body.category, purpose=body.purpose, collection=body.collection,
            export=body.export, raw_artifact=body.raw_artifact,
            destination_scope=body.destination_scope,
        )
    except DataError as exc:
        raise _map(exc) from exc


@router.post("/consent/{grant_id}/withdraw")
def withdraw_consent(grant_id: str, body: WithdrawRequest,
                     runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.withdraw_consent(grant_id, reason=body.reason,
                                             quarantine=body.quarantine)
    except DataError as exc:
        raise _map(exc) from exc


# --- records ----------------------------------------------------------------


@router.get("/records")
def list_records(category: str | None = None, status: str | None = None,
                 runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    return runtime.data.list_records(category=category, status=status)


@router.post("/records/preview")
def preview_redaction(body: PreviewRequest, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    return runtime.data.preview_redaction(body.category, body.payload)


@router.post("/telemetry/sample", status_code=201)
def collect_telemetry_sample(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """Collect ONE server-derived operational-telemetry snapshot (no arbitrary injection)."""
    _require_enabled(runtime)
    record_id = runtime.data.collect_operational_snapshot()
    return {"record_id": record_id, "collected": record_id is not None}


@router.get("/records/{record_id}")
def get_record(record_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    try:
        return runtime.data.get_record(record_id)
    except DataError as exc:
        raise _map(exc) from exc


@router.post("/records/{record_id}/approve")
def approve_record(record_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.approve_record(record_id)
    except DataError as exc:
        raise _map(exc) from exc


@router.post("/records/{record_id}/reject")
def reject_record(record_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.reject_record(record_id)
    except DataError as exc:
        raise _map(exc) from exc


@router.delete("/records/{record_id}")
def delete_record(record_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.delete_record(record_id)
    except DataError as exc:
        raise _map(exc) from exc


# --- export controls --------------------------------------------------------


@router.post("/export/pause")
def export_pause(body: PauseRequest, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    return runtime.data.pause_export(body.paused)


@router.post("/collection/pause")
def collection_pause(body: PauseRequest, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    return runtime.data.pause_collection(body.paused)


# --- batches ----------------------------------------------------------------


@router.get("/batches")
def list_batches(status: str | None = None,
                 runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    return runtime.data.list_batches(status=status)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    try:
        return runtime.data.get_batch(batch_id)
    except DataError as exc:
        raise _map(exc) from exc


@router.post("/batches/build", status_code=201)
def build_batch(body: BatchBuildRequest, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.build_batch(destination_id=body.destination_id,
                                       category=body.category, max_records=body.max_records)
    except DataError as exc:
        raise _map(exc) from exc


@router.post("/batches/{batch_id}/redrive")
def redrive_batch(batch_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.redrive(batch_id)
    except DataError as exc:
        raise _map(exc) from exc


# --- destinations -----------------------------------------------------------


@router.get("/destinations")
def list_destinations(runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    return runtime.data.list_destinations()


@router.post("/destinations", status_code=201)
def add_destination(body: DestinationCreateRequest,
                    runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.add_destination(kind=body.kind, name=body.name, config=body.config)
    except DataError as exc:
        raise _map(exc) from exc


@router.post("/destinations/{destination_id}/enable")
def enable_destination(destination_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.set_destination_enabled(destination_id, True)
    except DataError as exc:
        raise _map(exc) from exc


@router.post("/destinations/{destination_id}/disable")
def disable_destination(destination_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.set_destination_enabled(destination_id, False)
    except DataError as exc:
        raise _map(exc) from exc


@router.post("/destinations/{destination_id}/verify")
def verify_destination(destination_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    try:
        return runtime.data.verify_destination(destination_id)
    except DataError as exc:
        raise _map(exc) from exc


# --- deletions --------------------------------------------------------------


@router.get("/deletions")
def list_deletions(runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    return runtime.data.list_deletions()


@router.post("/deletions", status_code=201)
def request_deletion(body: DeletionRequest, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.request_deletion(
            scope=body.scope, target_id=body.target_id, subject=body.subject,
            category=body.category, reason=body.reason, hold=body.hold,
        )
    except DataError as exc:
        raise _map(exc) from exc


@router.get("/deletions/{deletion_id}")
def get_deletion(deletion_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    try:
        return runtime.data.get_deletion(deletion_id)
    except DataError as exc:
        raise _map(exc) from exc


# --- datasets ---------------------------------------------------------------


@router.get("/datasets")
def list_datasets(runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    return runtime.data.list_datasets()


@router.post("/datasets", status_code=201)
def create_dataset(body: DatasetCreateRequest,
                   runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.create_dataset(name=body.name, purpose=body.purpose)
    except DataError as exc:
        raise _map(exc) from exc


@router.get("/datasets/{dataset_id}/versions")
def list_dataset_versions(dataset_id: str,
                          runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    return runtime.data.list_dataset_versions(dataset_id)


@router.post("/datasets/{dataset_id}/versions", status_code=201)
def create_dataset_version(dataset_id: str, body: DatasetVersionRequest,
                           runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_enabled(runtime)
    try:
        return runtime.data.create_dataset_version(
            dataset_id, split_label=body.split_label, exclusions=body.exclusions,
        )
    except DataError as exc:
        raise _map(exc) from exc


@router.get("/datasets/versions/{version_id}/manifest")
def dataset_manifest(version_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    try:
        return runtime.data.dataset_manifest(version_id)
    except DataError as exc:
        raise _map(exc) from exc


@router.get("/diagnostics")
def diagnostics(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    return runtime.data.diagnostics()
