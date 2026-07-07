"""Bounded field-validation campaign API (Stage 14C.2, Part P).

Read endpoints are always available. Disruptive physical actions (campaign start/stop, soak runs,
fault injection) require explicit operator enablement (and confirmation for faults/TX). No endpoint
accepts an arbitrary shell command, network command, device argument, output path, or environment
value; no response returns a credential, lease token, private identity, or raw path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from aithernet.api.app import get_runtime
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.state.repositories import (
    FieldCampaignRepository,
    FieldCheckRepository,
    FieldFaultRepository,
    FieldMeasurementRepository,
    FieldNodeRepository,
)

router = APIRouter(tags=["field"])


class CampaignCreateRequest(BaseModel):
    name: str
    objective: str | None = None
    profile: str = "smoke"


class CampaignStartRequest(BaseModel):
    campaign_id: str
    device_id: str
    iterations: int | None = Field(default=None, ge=1, le=100000)
    survey_windows: int = Field(default=0, ge=0, le=10000)


class CampaignPreflightRequest(BaseModel):
    device_id: str | None = None


class SoakStartRequest(BaseModel):
    campaign_id: str | None = None
    target_seconds: float = Field(ge=1, le=86400 * 2)
    sample_interval: float | None = Field(default=None, gt=0)


class FaultInjectRequest(BaseModel):
    fault_type: str
    params: dict | None = None
    confirm: bool = False
    campaign_id: str | None = None


def _require_operator(runtime: NodeRuntime) -> None:
    if not runtime.config.field_validation.operator_actions_enabled:
        raise HTTPException(
            status_code=403,
            detail="Field operator actions are disabled "
            "(set field_validation.operator_actions_enabled).")


def _campaign_dict(c) -> dict:
    return {
        "id": c.id, "node_id": c.node_id, "name": c.name, "objective": c.objective,
        "profile": c.profile, "test_mode": c.test_mode, "status": c.status,
        "classification": c.classification, "software_version": c.software_version,
        "config_digest": c.config_digest, "topology": c.topology_json,
        "thresholds": c.thresholds_json, "warnings": c.warnings_json, "summary": c.summary,
        "checks_passed": c.checks_passed, "checks_failed": c.checks_failed,
        "checks_not_executed": c.checks_not_executed, "started_at": c.started_at,
        "completed_at": c.completed_at, "created_at": c.created_at,
    }


@router.get("/field/campaigns")
def field_campaigns(runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    with runtime.session_scope() as session:
        return [_campaign_dict(c) for c in
                FieldCampaignRepository(session).list(node_id=runtime.config.node_id)]


def _get_campaign_or_404(session, runtime, campaign_id):
    c = FieldCampaignRepository(session).get(campaign_id)
    if c is None or c.node_id != runtime.config.node_id:
        raise HTTPException(status_code=404, detail="Unknown campaign.")
    return c


@router.get("/field/campaigns/{campaign_id}")
def field_campaign(campaign_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    with runtime.session_scope() as session:
        return _campaign_dict(_get_campaign_or_404(session, runtime, campaign_id))


@router.get("/field/campaigns/{campaign_id}/nodes")
def field_campaign_nodes(campaign_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> list:
    with runtime.session_scope() as session:
        return [{"role": n.role, "node_ref": n.node_ref, "peer_id": n.peer_id,
                 "host_id": n.sanitized_host_id, "fingerprint": n.identity_fingerprint,
                 "ready": n.ready, "detail": n.detail}
                for n in FieldNodeRepository(session).list_for_campaign(campaign_id)]


@router.get("/field/campaigns/{campaign_id}/checks")
def field_campaign_checks(campaign_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> list:
    with runtime.session_scope() as session:
        return [{"name": c.name, "category": c.category, "status": c.status,
                 "classification": c.classification, "detail": c.detail,
                 "evidence": c.evidence_json or {}, "created_at": c.created_at}
                for c in FieldCheckRepository(session).list_for_campaign(campaign_id)]


@router.get("/field/campaigns/{campaign_id}/measurements")
def field_campaign_measurements(
    campaign_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> list:
    with runtime.session_scope() as session:
        rows = FieldMeasurementRepository(session).list_for_campaign(campaign_id)
        return [{"iteration": m.iteration, "kind": m.kind, "device_id": m.device_id,
                 "lease_id": m.lease_id, "backend_id": m.backend_id,
                 "center_frequency_hz": m.center_frequency_hz, "sample_rate_sps": m.sample_rate_sps,
                 "gain_db": m.gain_db, "duration_s": m.duration_s,
                 "expected_samples": m.expected_samples, "actual_samples": m.actual_samples,
                 "bytes": m.bytes, "sha256": m.sha256, "latency_ms": m.latency_ms,
                 "cleanup_ok": m.cleanup_ok, "success": m.success, "confidence": m.confidence,
                 "created_at": m.created_at}
                for m in rows]


@router.get("/field/campaigns/{campaign_id}/faults")
def field_campaign_faults(campaign_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> list:
    with runtime.session_scope() as session:
        return [{"fault_type": f.fault_type, "status": f.status, "confirmed": f.confirmed,
                 "expected_recovery": f.expected_recovery, "actual_recovery": f.actual_recovery,
                 "cleanup_ok": f.cleanup_ok, "created_at": f.created_at}
                for f in FieldFaultRepository(session).list_for_campaign(campaign_id)]


@router.get("/field/campaigns/{campaign_id}/report")
def field_campaign_report(campaign_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    with runtime.session_scope() as session:
        _get_campaign_or_404(session, runtime, campaign_id)
    return runtime.field.report(campaign_id)


@router.post("/field/campaigns/preflight")
async def field_preflight(
    body: CampaignPreflightRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    return await runtime.field.preflight(device_id=body.device_id)


@router.post("/field/campaigns/create")
def field_campaign_create(
    body: CampaignCreateRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Create a field campaign (operator-gated; no RF action until start)."""
    _require_operator(runtime)
    cid = runtime.field.create_campaign(name=body.name, objective=body.objective,
                                        profile=body.profile)
    with runtime.session_scope() as session:
        return _campaign_dict(FieldCampaignRepository(session).get(cid))


@router.post("/field/campaigns/start")
async def field_campaign_start(
    body: CampaignStartRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Run a created field campaign (real RF actions; operator-gated)."""
    _require_operator(runtime)
    try:
        await runtime.field.start_campaign(
            body.campaign_id, device_id=body.device_id, iterations=body.iterations,
            survey_windows=body.survey_windows)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with runtime.session_scope() as session:
        return _campaign_dict(FieldCampaignRepository(session).get(body.campaign_id))


@router.post("/field/campaigns/{campaign_id}/stop")
def field_campaign_stop(campaign_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    _require_operator(runtime)
    with runtime.session_scope() as session:
        c = _get_campaign_or_404(session, runtime, campaign_id)
        if c.status == "running":
            FieldCampaignRepository(session).update(campaign_id, status="stopped")
            session.commit()
        return _campaign_dict(FieldCampaignRepository(session).get(campaign_id))


@router.post("/field/soak/start")
async def field_soak_start(
    body: SoakStartRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    _require_operator(runtime)
    soak_id = await runtime.field.soak_run(
        campaign_id=body.campaign_id, target_seconds=body.target_seconds,
        sample_interval=body.sample_interval)
    from aithernet.state.repositories import SoakRunRepository
    with runtime.session_scope() as session:
        s = SoakRunRepository(session).get(soak_id)
        return {"id": s.id, "sample_count": s.sample_count, "violations": s.violations,
                "summary": s.summary_json, "status": s.status}


@router.post("/field/faults/inject")
async def field_fault_inject(
    body: FaultInjectRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Inject a bounded, predefined validation fault (gated; confirmation for disruptive action)."""
    _require_operator(runtime)
    try:
        fault_id = await runtime.field.inject_fault(
            fault_type=body.fault_type, params=body.params, confirm=body.confirm,
            campaign_id=body.campaign_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from aithernet.state.repositories import FieldFaultRepository as _R
    with runtime.session_scope() as session:
        f = _R(session).get(fault_id)
        return {"id": f.id, "fault_type": f.fault_type, "status": f.status,
                "actual_recovery": f.actual_recovery, "cleanup_ok": f.cleanup_ok}
