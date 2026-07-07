"""Typed, bounded managed-hardware API (Stage 14B, Part N).

Read endpoints are always available. Disruptive lease actions (acquire/release/revoke) and
admin enable/disable require ``hardware.operator_lease_actions_enabled`` — matching the Stage 14A
boundary that disruptive operator actions are gated, never exposed unauthenticated by default.
No endpoint accepts an arbitrary shell command, raw device string, or backend device argument; no
response returns a lease token, raw path, credential, or environment value.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from aithernet.api.app import get_runtime
from aithernet.hardware.contracts import (
    BindingNotFoundError,
    DeviceNotFoundError,
    DeviceUnavailableError,
    LeaseCompatibilityError,
    LeaseConflictError,
    LeaseNotFoundError,
)
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.hardware import (
    DeviceCaptureRequest,
    HardwareCapabilityRead,
    HardwareDeviceRead,
    HardwareHealthEventRead,
    HardwareLeaseEventRead,
    HardwareLeaseRead,
    HardwareProviderRead,
    HardwareRefreshResult,
    HardwareStatusRead,
    LeaseAcquireRequest,
    LeaseActionRequest,
)
from aithernet.state.repositories import (
    SDRDeviceBindingRepository,
    SDRDeviceCapabilityRepository,
    SDRDeviceHealthEventRepository,
    SDRDeviceLeaseEventRepository,
    SDRDeviceLeaseRepository,
    SDRDeviceRepository,
)

router = APIRouter(tags=["hardware"])


def _require_operator(runtime: NodeRuntime) -> None:
    if not runtime.config.hardware.operator_lease_actions_enabled:
        raise HTTPException(
            status_code=403,
            detail="Operator hardware lease actions are disabled "
            "(set hardware.operator_lease_actions_enabled).",
        )


def _device_dict(session, device, *, bindings_repo, lease_repo) -> dict:
    bindings = [b.backend_id for b in bindings_repo.list_for_device(device.id) if b.enabled]
    holding = lease_repo.holding_for_device(device.id)
    caps = SDRDeviceCapabilityRepository(session).get_for_device(device.id)
    cj = (caps.capabilities_json if caps else {}) or {}
    return {
        "device_id": device.id, "node_id": device.node_id, "hardware_key": device.hardware_key,
        "provider_id": device.provider_id, "device_kind": device.device_kind,
        "vendor": device.vendor, "product": device.product, "serial": device.serial,
        "driver": device.driver, "transport": device.transport, "channel": device.channel,
        "display_name": device.display_name, "identity_limited": device.identity_limited,
        "enabled": device.enabled, "administrative_state": device.administrative_state,
        "presence_state": device.presence_state, "health_state": device.health_state,
        "status": device.status, "capability_revision": device.capability_revision,
        "rx": cj.get("rx_supported"), "tx": cj.get("tx_supported"),
        "channel_count": cj.get("channel_count"),
        "metadata": device.metadata_json or {}, "compatible_backends": bindings,
        "active_lease_count": len(holding), "first_seen_at": device.first_seen_at,
        "last_seen_at": device.last_seen_at,
    }


def _lease_dict(lease) -> dict:
    # lease_token / owner_worker_id are NEVER surfaced.
    return {
        "lease_id": lease.id, "node_id": lease.node_id, "device_id": lease.device_id,
        "backend_id": lease.backend_id, "mission_id": lease.mission_id,
        "mission_run_id": lease.mission_run_id, "mission_step_id": lease.mission_step_id,
        "requested_operation": lease.requested_operation, "direction": lease.direction,
        "requested_channels": lease.requested_channels_json or [], "lease_mode": lease.lease_mode,
        "state": lease.state, "reason": lease.reason, "acquired_at": lease.acquired_at,
        "renewed_at": lease.renewed_at, "expires_at": lease.expires_at,
        "released_at": lease.released_at, "created_at": lease.created_at,
    }


def _provider_rows(runtime: NodeRuntime, availability: dict) -> list[HardwareProviderRead]:
    # The registry is the source of truth (it may hold injected providers); config supplies the
    # declared kind when present.
    cfg = runtime.config.hardware.providers or {}
    rows = []
    for pid in runtime.hardware_discovery.provider_ids():
        pcfg = cfg.get(pid)
        rows.append(HardwareProviderRead(
            provider_id=pid, kind=pcfg.kind if pcfg else "injected",
            enabled=pcfg.enabled if pcfg else True,
            required=runtime.hardware_discovery.is_required(pid),
            available=availability.get(pid, False),
        ))
    return rows


@router.get("/hardware/status", response_model=HardwareStatusRead)
async def hardware_status(runtime: NodeRuntime = Depends(get_runtime)) -> HardwareStatusRead:
    availability = await runtime.hardware_discovery.availability()
    providers = _provider_rows(runtime, availability)
    with runtime.session_scope() as session:
        devices_by_status = SDRDeviceRepository(session).counts_by_status(
            node_id=runtime.config.node_id)
        leases_by_state = SDRDeviceLeaseRepository(session).counts_by_state(
            node_id=runtime.config.node_id)
    return HardwareStatusRead(
        enabled=runtime.config.hardware.enabled,
        operator_lease_actions_enabled=runtime.config.hardware.operator_lease_actions_enabled,
        providers=providers, devices_by_status=devices_by_status, leases_by_state=leases_by_state,
    )


@router.get("/hardware/providers", response_model=list[HardwareProviderRead])
async def hardware_providers(
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[HardwareProviderRead]:
    availability = await runtime.hardware_discovery.availability()
    return _provider_rows(runtime, availability)


@router.get("/hardware/devices", response_model=list[HardwareDeviceRead])
def hardware_devices(runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    with runtime.session_scope() as session:
        device_repo = SDRDeviceRepository(session)
        bindings_repo = SDRDeviceBindingRepository(session)
        lease_repo = SDRDeviceLeaseRepository(session)
        return [
            _device_dict(session, d, bindings_repo=bindings_repo, lease_repo=lease_repo)
            for d in device_repo.list(node_id=runtime.config.node_id,
                                      limit=runtime.config.hardware.max_devices)
        ]


def _get_device_or_404(session, runtime, device_id):
    device = SDRDeviceRepository(session).get(device_id)
    if device is None or device.node_id != runtime.config.node_id:
        raise HTTPException(status_code=404, detail="Unknown device.")
    return device


@router.get("/hardware/devices/{device_id}", response_model=HardwareDeviceRead)
def hardware_device(device_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    with runtime.session_scope() as session:
        device = _get_device_or_404(session, runtime, device_id)
        return _device_dict(session, device,
                            bindings_repo=SDRDeviceBindingRepository(session),
                            lease_repo=SDRDeviceLeaseRepository(session))


@router.get("/hardware/devices/{device_id}/capabilities", response_model=HardwareCapabilityRead)
def hardware_capabilities(device_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    with runtime.session_scope() as session:
        _get_device_or_404(session, runtime, device_id)
        caps = SDRDeviceCapabilityRepository(session).get_for_device(device_id)
        if caps is None:
            raise HTTPException(status_code=404, detail="No capabilities probed yet.")
        return {
            "device_id": device_id, "revision": caps.revision,
            "rx_supported": caps.rx_supported, "tx_supported": caps.tx_supported,
            "full_duplex": caps.full_duplex, "channel_count": caps.channel_count,
            "shared_receive_safe": caps.shared_receive_safe, "source": caps.source,
            "confidence": caps.confidence, "capabilities": caps.capabilities_json or {},
            "probed_at": caps.probed_at,
        }


@router.get("/hardware/devices/{device_id}/health-events",
            response_model=list[HardwareHealthEventRead])
def hardware_health_events(device_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> list:
    with runtime.session_scope() as session:
        _get_device_or_404(session, runtime, device_id)
        events = SDRDeviceHealthEventRepository(session).list_for_device(device_id, limit=100)
        return [
            {"id": e.id, "device_id": e.device_id, "health_state": e.health_state,
             "previous_health_state": e.previous_health_state, "presence_state": e.presence_state,
             "detail": e.detail, "created_at": e.created_at}
            for e in events
        ]


@router.post("/hardware/refresh", response_model=HardwareRefreshResult)
async def hardware_refresh(runtime: NodeRuntime = Depends(get_runtime)) -> HardwareRefreshResult:
    """Operator-triggered inventory refresh (bounded; runs discovery providers, no OS command)."""
    availability = await runtime.hardware_inventory.refresh()
    with runtime.session_scope() as session:
        by_status = SDRDeviceRepository(session).counts_by_status(node_id=runtime.config.node_id)
    return HardwareRefreshResult(availability=availability, devices_by_status=by_status)


@router.get("/hardware/leases", response_model=list[HardwareLeaseRead])
def hardware_leases(
    active: bool = False, runtime: NodeRuntime = Depends(get_runtime)
) -> list[dict]:
    with runtime.session_scope() as session:
        leases = SDRDeviceLeaseRepository(session).list(
            node_id=runtime.config.node_id, active_only=active)
        return [_lease_dict(le) for le in leases]


@router.get("/hardware/leases/{lease_id}", response_model=HardwareLeaseRead)
def hardware_lease(lease_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    with runtime.session_scope() as session:
        lease = SDRDeviceLeaseRepository(session).get(lease_id)
        if lease is None or lease.node_id != runtime.config.node_id:
            raise HTTPException(status_code=404, detail="Unknown lease.")
        return _lease_dict(lease)


@router.get("/hardware/leases/{lease_id}/events", response_model=list[HardwareLeaseEventRead])
def hardware_lease_events(lease_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> list:
    with runtime.session_scope() as session:
        events = SDRDeviceLeaseEventRepository(session).list_for_lease(lease_id)
        return [
            {"id": e.id, "lease_id": e.lease_id, "device_id": e.device_id,
             "event_type": e.event_type, "state": e.state, "reason": e.reason,
             "created_at": e.created_at}
            for e in events
        ]


@router.post("/hardware/leases/acquire", response_model=HardwareLeaseRead)
async def hardware_acquire(
    body: LeaseAcquireRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    _require_operator(runtime)
    try:
        lease_id = await runtime.hardware_leases.acquire(
            body.device_id, backend_id=body.backend_id, direction=body.direction,
            mode=body.lease_mode, operation=body.operation, channels=list(body.channels),
            frequency_hz=body.frequency_hz, sample_rate=body.sample_rate,
            duration_seconds=body.duration_seconds,
        )
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (DeviceUnavailableError, LeaseConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (BindingNotFoundError, LeaseCompatibilityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with runtime.session_scope() as session:
        return _lease_dict(SDRDeviceLeaseRepository(session).get(lease_id))


@router.post("/hardware/leases/{lease_id}/release", response_model=HardwareLeaseRead)
async def hardware_release(
    lease_id: str, body: LeaseActionRequest | None = None,
    runtime: NodeRuntime = Depends(get_runtime),
) -> dict:
    _require_operator(runtime)
    reason = (body.reason if body else None) or "operator release"
    try:
        await runtime.hardware_leases.release(lease_id, reason=reason)
    except LeaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    with runtime.session_scope() as session:
        return _lease_dict(SDRDeviceLeaseRepository(session).get(lease_id))


@router.post("/hardware/leases/{lease_id}/revoke", response_model=HardwareLeaseRead)
async def hardware_revoke(
    lease_id: str, body: LeaseActionRequest | None = None,
    runtime: NodeRuntime = Depends(get_runtime),
) -> dict:
    _require_operator(runtime)
    reason = (body.reason if body else None) or "operator revoked"
    try:
        await runtime.hardware_leases.revoke(lease_id, reason=reason)
    except LeaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    with runtime.session_scope() as session:
        return _lease_dict(SDRDeviceLeaseRepository(session).get(lease_id))


@router.post("/hardware/devices/{device_id}/enable", response_model=HardwareDeviceRead)
def hardware_enable(device_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    return _set_admin(runtime, device_id, enabled=True)


@router.post("/hardware/devices/{device_id}/disable", response_model=HardwareDeviceRead)
def hardware_disable(device_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    return _set_admin(runtime, device_id, enabled=False)


def _set_admin(runtime: NodeRuntime, device_id: str, *, enabled: bool) -> dict:
    _require_operator(runtime)
    with runtime.session_scope() as session:
        device = _get_device_or_404(session, runtime, device_id)
        device.enabled = enabled
        device.administrative_state = "enabled" if enabled else "disabled"
        if not enabled:
            device.status = "disabled"
        session.commit()
        return _device_dict(session, device,
                            bindings_repo=SDRDeviceBindingRepository(session),
                            lease_repo=SDRDeviceLeaseRepository(session))


# -- Stage 14C.1 qualification surfaces (read-only; RF actions are explicit operator-gated) ---

from aithernet.schemas.hardware import (  # noqa: E402
    HardwareMeasurementRead,
    HardwareQualificationCheckRead,
    HardwareQualificationRunRead,
    HardwareSupportEntry,
    QualificationRunRequest,
)
from aithernet.state.repositories import (  # noqa: E402
    HardwareQualificationCheckRepository,
    HardwareQualificationRunRepository,
    HardwareRFMeasurementRepository,
)


def _run_dict(run) -> dict:
    return {
        "id": run.id, "node_id": run.node_id, "device_id": run.device_id,
        "hardware_key": run.hardware_key, "provider_id": run.provider_id, "status": run.status,
        "support_classification": run.support_classification,
        "capability_revision": run.capability_revision, "environment": run.environment_json or {},
        "summary": run.summary, "warnings": run.warnings_json or [],
        "checks_total": run.checks_total, "checks_passed": run.checks_passed,
        "checks_failed": run.checks_failed, "checks_not_executed": run.checks_not_executed,
        "started_at": run.started_at, "completed_at": run.completed_at,
    }


def _measurement_dict(m) -> dict:
    # Never exposes raw absolute paths — artifact bytes are referenced by id/digest only.
    return {
        "id": m.id, "device_id": m.device_id, "qualification_run_id": m.qualification_run_id,
        "capability_revision": m.capability_revision, "lease_id": m.lease_id,
        "backend_id": m.backend_id, "mission_id": m.mission_id, "kind": m.kind,
        "center_frequency_hz": m.center_frequency_hz, "sample_rate_sps": m.sample_rate_sps,
        "gain_db": m.gain_db, "antenna": m.antenna, "channel": m.channel,
        "stream_format": m.stream_format, "capture_bytes": m.capture_bytes,
        "sample_count": m.sample_count, "sha256": m.sha256, "artifact_id": m.artifact_id,
        "processing_revision": m.processing_revision,
        "analysis_params": m.analysis_params_json or {},
        "result": m.result_json or {}, "confidence": m.confidence,
        "warnings": m.warnings_json or [], "created_at": m.created_at,
    }


@router.get("/hardware/qualifications", response_model=list[HardwareQualificationRunRead])
def hardware_qualifications(
    device_id: str | None = None, runtime: NodeRuntime = Depends(get_runtime)
) -> list[dict]:
    with runtime.session_scope() as session:
        runs = HardwareQualificationRunRepository(session).list(
            node_id=runtime.config.node_id, device_id=device_id)
        return [_run_dict(r) for r in runs]


@router.get("/hardware/qualifications/{run_id}", response_model=HardwareQualificationRunRead)
def hardware_qualification(run_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    with runtime.session_scope() as session:
        run = HardwareQualificationRunRepository(session).get(run_id)
        if run is None or run.node_id != runtime.config.node_id:
            raise HTTPException(status_code=404, detail="Unknown qualification run.")
        return _run_dict(run)


@router.get("/hardware/qualifications/{run_id}/checks",
            response_model=list[HardwareQualificationCheckRead])
def hardware_qualification_checks(run_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> list:
    with runtime.session_scope() as session:
        checks = HardwareQualificationCheckRepository(session).list_for_run(run_id)
        return [
            {"id": c.id, "run_id": c.run_id, "name": c.name, "category": c.category,
             "status": c.status, "detail": c.detail, "evidence": c.evidence_json or {},
             "created_at": c.created_at}
            for c in checks
        ]


@router.get("/hardware/devices/{device_id}/qualification-status")
def hardware_device_qualification_status(
    device_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    with runtime.session_scope() as session:
        run = HardwareQualificationRunRepository(session).latest_for_device(device_id)
        if run is None:
            return {"device_id": device_id, "classification": "discovered", "qualified": False}
        return {"device_id": device_id, "classification": run.support_classification,
                "qualified": run.status == "passed", "run_id": run.id,
                "last_qualified_at": run.completed_at}


@router.get("/hardware/measurements", response_model=list[HardwareMeasurementRead])
def hardware_measurements(
    device_id: str | None = None, runtime: NodeRuntime = Depends(get_runtime)
) -> list[dict]:
    with runtime.session_scope() as session:
        rows = HardwareRFMeasurementRepository(session).list(
            node_id=runtime.config.node_id, device_id=device_id)
        return [_measurement_dict(m) for m in rows]


@router.get("/hardware/support-matrix", response_model=list[HardwareSupportEntry])
def hardware_support_matrix(runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    """Factual support classification per device from completed qualification evidence (Part K)."""
    with runtime.session_scope() as session:
        device_repo = SDRDeviceRepository(session)
        run_repo = HardwareQualificationRunRepository(session)
        out = []
        for d in device_repo.list(node_id=runtime.config.node_id):
            run = run_repo.latest_for_device(d.id)
            out.append({
                "device_id": d.id, "display_name": d.display_name, "provider_id": d.provider_id,
                "driver": d.driver, "architecture_supported": True, "discovered": True,
                "classification": run.support_classification if run else "discovered",
                "last_qualified_at": run.completed_at if run else None,
            })
        return out


@router.post("/hardware/devices/{device_id}/capture")
async def hardware_device_capture(
    device_id: str, body: DeviceCaptureRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Perform ONE bounded real RX capture (operator-gated; RX only — never transmits).

    With ``lease_id`` the capture runs under an already-held active lease (the lease stays held);
    otherwise a temporary exclusive RX lease is acquired and released around the capture.
    """
    _require_operator(runtime)
    qcfg = runtime.config.hardware.qualification
    backend_id = body.backend_id or runtime.config.rf_backends.default_backend
    with runtime.session_scope() as session:
        device = _get_device_or_404(session, runtime, device_id)
        # detach-safe: attributes are loaded (expire_on_commit=False)
        _ = (device.id, device.driver, device.transport, device.display_name)
    own_lease = False
    lease_id = body.lease_id
    if lease_id:
        with runtime.session_scope() as session:
            lease = SDRDeviceLeaseRepository(session).get(lease_id)
            if lease is None or lease.device_id != device_id or lease.state != "active":
                raise HTTPException(status_code=422, detail="lease_id is not an active lease.")
    else:
        try:
            lease_id = await runtime.hardware_leases.acquire(
                device_id, backend_id=backend_id, direction="rx", mode="exclusive",
                operation="capture")
            own_lease = True
        except (DeviceUnavailableError, LeaseConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (BindingNotFoundError, LeaseCompatibilityError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        res = await runtime.hardware_capture.capture(
            device=device, freq_hz=body.frequency_hz or qcfg.default_rx_frequency_hz,
            sample_rate=body.sample_rate or qcfg.default_sample_rate,
            gain_db=body.gain_db if body.gain_db is not None else qcfg.default_gain_db,
            duration_s=body.duration_s or qcfg.default_duration_seconds,
            lease_id=lease_id, backend_id=backend_id)
    finally:
        if own_lease:
            await runtime.hardware_leases.release(lease_id, reason="capture done")
    return res


@router.post("/hardware/qualifications/preflight")
async def hardware_qualify_preflight(
    body: QualificationRunRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Read-only qualification preflight (never opens the device or transmits)."""
    return await runtime.hardware_qualification.preflight(body.device_id)


@router.post("/hardware/qualifications/run", response_model=HardwareQualificationRunRead)
async def hardware_qualify_run(
    body: QualificationRunRequest, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Run a real-hardware qualification. RF actions require explicit operator enablement."""
    _require_operator(runtime)
    try:
        run_id = await runtime.hardware_qualification.run(
            body.device_id, do_capture=body.do_capture, do_survey=body.do_survey,
            capture_duration=body.capture_duration)
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    with runtime.session_scope() as session:
        return _run_dict(HardwareQualificationRunRepository(session).get(run_id))
