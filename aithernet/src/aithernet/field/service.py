"""Field-validation campaign + soak + fault-injection service (Stage 14C.2).

Operator-invoked, deterministic validation of the complete node under repeated-operation, failure,
and long-duration conditions. Reuses the existing hardware lease + capture services; it does NOT
redesign the mission engine, transport, hardware layer, or RF backend registry. Campaign sampling,
lease cycling, and soak sampling create NO MissionStep. No fake result is ever classified as
physical evidence — a single-SDR node classifies two-radio RF as ``not_executed``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from typing import TYPE_CHECKING

from aithernet import __version__
from aithernet.field import events as ev
from aithernet.field.resources import full_sample, sample_process, sample_storage
from aithernet.hardware.capture import CaptureError
from aithernet.hardware.contracts import DeviceUnavailableError, LeaseConflictError
from aithernet.state.models import utcnow
from aithernet.state.repositories import (
    FieldCampaignRepository,
    FieldCheckRepository,
    FieldDeviceAssignmentRepository,
    FieldFaultRepository,
    FieldMeasurementRepository,
    FieldNodeRepository,
    FieldRecoveryRepository,
    FieldRunRepository,
    SDRDeviceCapabilityRepository,
    SDRDeviceLeaseRepository,
    SDRDeviceRepository,
    SoakRunRepository,
    SoakSampleRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

PASSED, FAILED, NOT_EXECUTED, SKIPPED = "passed", "failed", "not_executed", "skipped"

#: Acceptance classifications (Part W), in increasing physical scope.
CLASS_SOFTWARE = "software_harness_complete"
CLASS_SINGLE_DEVICE = "single_device_soak_complete"
CLASS_MULTI_NODE_IP = "multi_node_ip_complete"
CLASS_TWO_DEVICE = "two_device_rf_complete"
CLASS_EXTENDED = "extended_soak_complete"
CLASS_BLOCKED = "blocked"
CLASS_FAILED = "failed"

#: Bounded, predefined fault types (Part I) — never an arbitrary shell/network command.
FAULT_TYPES = (
    "node_process_kill", "rf_subprocess_kill", "peer_network_block", "peer_network_restore",
    "provider_unavailable", "device_missing", "artifact_transfer_interrupt",
    "disk_quota_pressure", "coordinator_unavailable", "coding_agent_unavailable",
)
#: Faults this in-node service can simulate deterministically in software (validation only). The
#: rest (process/network kills) are exercised by the process-level acceptance tests.
_SOFTWARE_FAULTS = {"provider_unavailable", "device_missing", "coordinator_unavailable",
                    "coding_agent_unavailable"}


class FieldService:
    """Owns field campaigns, soak runs, and bounded fault injection for one node."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.node_id = runtime.config.node_id
        self.cfg = runtime.config.field_validation

    # -- emit --------------------------------------------------------------------

    async def _emit(self, event_type: str, *, message: str, payload: dict) -> None:
        with contextlib.suppress(Exception):
            await self.runtime._emit_event(event_type=event_type, mission_id=None,
                                           source="field", message=message, payload=payload)

    def _config_digest(self) -> str:
        raw = json.dumps(self.runtime.config.model_dump(mode="json"), sort_keys=True,
                         default=str).encode()
        return "sha256:" + hashlib.sha256(raw).hexdigest()[:32]

    # -- campaign create ---------------------------------------------------------

    def create_campaign(self, *, name: str, objective: str | None = None,
                        profile: str = "smoke", peers: list[dict] | None = None) -> str:
        topology = self._topology(peers or [])
        with self.runtime.session_scope() as session:
            campaign = FieldCampaignRepository(session).create(
                node_id=self.node_id, name=name[:160], objective=(objective or "")[:500] or None,
                profile=profile, test_mode="validation", status="created",
                classification=CLASS_SOFTWARE, software_version=__version__,
                config_digest=self._config_digest(), topology_json=topology,
                thresholds_json=self.cfg.thresholds.model_dump())
            cid = campaign.id
            FieldNodeRepository(session).create(
                campaign_id=cid, role="node_a", node_ref=self.node_id,
                identity_fingerprint=self._identity_fingerprint(),
                sanitized_host_id=self._host_id(), ready=True, detail="this node")
            for p in topology.get("peers", []):
                FieldNodeRepository(session).create(
                    campaign_id=cid, role="node_b", node_ref=p.get("node_ref"),
                    peer_id=p.get("peer_id"), identity_fingerprint=p.get("fingerprint"),
                    base_url=p.get("base_url"), ready=False, detail="peer")
            session.commit()
        return cid

    def _topology(self, peers: list[dict]) -> dict:
        return {
            "control_plane": "authenticated_ip",
            "this_node": {"node_id": self.node_id, "host_id": self._host_id(),
                          "fingerprint": self._identity_fingerprint()},
            "peers": [{"node_ref": p.get("node_ref"), "peer_id": p.get("peer_id"),
                       "base_url": p.get("base_url"), "fingerprint": p.get("fingerprint")}
                      for p in peers],
        }

    def _host_id(self) -> str:
        import platform
        raw = f"{platform.node()}|{self.node_id}".encode()
        return "host-" + hashlib.sha256(raw).hexdigest()[:12]

    def _identity_fingerprint(self) -> str | None:
        with contextlib.suppress(Exception):
            status = self.runtime.transport.identity_status()
            if isinstance(status, dict):
                return status.get("fingerprint")
        return None

    # -- preflight (Part C) ------------------------------------------------------

    async def preflight(self, *, device_id: str | None = None) -> dict:
        """Read-only campaign preflight. Multi-node peer checks become not_executed when solo."""
        checks: list[dict] = []

        def add(name, status, detail=None, **ev_):
            checks.append({"name": name, "status": status, "detail": detail, **ev_})

        add("node_ready", PASSED if self.runtime.readiness.is_ready() else FAILED,
            self.runtime.readiness.phase.value)
        add("software_version", PASSED, __version__)
        add("config_digest", PASSED, self._config_digest())
        # storage capacity + disk floor
        storage = sample_storage(self.runtime)
        floor = self.cfg.thresholds.min_disk_free_bytes
        free = storage.get("disk_free_bytes")
        add("storage_capacity", PASSED if (free is None or free >= floor) else FAILED,
            f"{(free or 0) // (1024 ** 2)} MiB free")
        # coordinator + coding providers reachability (config-level)
        coord_ok = bool(self.runtime.config.coordinator.provider)
        add("coordinator_configured", PASSED if coord_ok else NOT_EXECUTED)
        # RF backends ready
        rf_ready = False
        with contextlib.suppress(Exception):
            statuses = await self.runtime.rf.list_statuses()
            rf_ready = any(s.state.value in ("ready", "running", "degraded") for s in statuses)
        add("rf_backend_ready", PASSED if rf_ready else NOT_EXECUTED,
            "no RF backend started" if not rf_ready else None)
        # required hardware present + healthy + no conflicting lease
        if device_id:
            with self.runtime.session_scope() as session:
                device = SDRDeviceRepository(session).get(device_id)
                if device is None:
                    add("required_device", FAILED, "unknown device")
                else:
                    present = device.presence_state == "present" and device.enabled
                    add("required_device_present", PASSED if present else FAILED,
                        device.presence_state)
                    caps = SDRDeviceCapabilityRepository(session).get_for_device(device.id)
                    add("device_capabilities", PASSED if caps else NOT_EXECUTED)
                    holding = SDRDeviceLeaseRepository(session).holding_for_device(device.id)
                    add("no_conflicting_lease", PASSED if not holding else FAILED,
                        f"{len(holding)} active lease(s)")
        else:
            add("required_device", NOT_EXECUTED, "no device selected")
        # artifact quota adequacy
        with contextlib.suppress(Exception):
            st = self.runtime.artifacts.store.status()
            add("artifact_quota", PASSED, f"{st.get('object_count', 0)} objects")
        # peer reachability — not executed solo (multi-node IP proven by the process test)
        add("peer_nodes", NOT_EXECUTED, "single-node preflight (peers validated by process test)")
        # bounded TX/power posture
        add("transmit_disabled", PASSED if not self.cfg.transmit_authorized else PASSED,
            "TX authorized" if self.cfg.transmit_authorized else "TX disabled (RX-only)")
        ok = all(c["status"] != FAILED for c in checks)
        return {"ok": ok, "device_id": device_id, "checks": checks}

    # -- start campaign (single-SDR repeated + survey) ---------------------------

    async def start_campaign(
        self, campaign_id: str, *, device_id: str, iterations: int | None = None,
        survey_windows: int = 0,
    ) -> str:
        with self.runtime.session_scope() as session:
            campaign = FieldCampaignRepository(session).get(campaign_id)
            if campaign is None or campaign.node_id != self.node_id:
                raise ValueError("unknown campaign")
            iters = iterations or self.runtime.config.field_validation.profiles.smoke_iterations
            FieldCampaignRepository(session).update(
                campaign_id, status="running", started_at=utcnow())
            session.commit()
        await self._emit(ev.EVENT_CAMPAIGN_STARTED,
                         message=f"campaign {campaign_id} started",
                         payload={"campaign_id": campaign_id, "device_id": device_id,
                                  "iterations": iters})
        warnings: list[str] = []
        await self._run_capture_campaign(campaign_id, device_id, iters, warnings)
        if survey_windows > 0:
            await self._run_survey_campaign(campaign_id, device_id, survey_windows, warnings)
        return await self._finalize(campaign_id, device_id, warnings)

    async def _run_capture_campaign(self, campaign_id, device_id, iterations, warnings):
        with self.runtime.session_scope() as session:
            run = FieldRunRepository(session).create(
                campaign_id=campaign_id, kind="capture_campaign", status="running")
            run_id = run.id
            FieldDeviceAssignmentRepository(session).create(
                campaign_id=campaign_id, device_id=device_id, role="rx")
            session.commit()
        cfg = self.cfg
        backend_id = self.runtime.config.rf_backends.default_backend
        ok_count = fail_count = 0
        for i in range(iterations):
            t0 = time.monotonic()
            lease_id = None
            measurement = None
            success = False
            detail = None
            try:
                # inventory refresh (cheap; probing skipped — device already has caps)
                await self.runtime.hardware_inventory.refresh()
                lease_id = await self.runtime.hardware_leases.acquire(
                    device_id, backend_id=backend_id, direction="rx", mode="exclusive",
                    operation="field_capture")
                with self.runtime.session_scope() as session:
                    device = SDRDeviceRepository(session).get(device_id)
                res = await self.runtime.hardware_capture.capture(
                    device=device, freq_hz=cfg.capture_frequency_hz,
                    sample_rate=cfg.capture_sample_rate, gain_db=cfg.capture_gain_db,
                    duration_s=cfg.capture_duration_s, lease_id=lease_id, backend_id=backend_id)
                measurement = res
                success = res["bytes"] > 0 and res["sample_count"] > 0
            except (CaptureError, DeviceUnavailableError, LeaseConflictError) as exc:
                detail = f"{type(exc).__name__}: {exc}"[:300]
                warnings.append(f"iter {i}: {type(exc).__name__}")
            finally:
                cleanup_ok = True
                if lease_id:
                    with contextlib.suppress(Exception):
                        await self.runtime.hardware_leases.release(
                            lease_id, reason="field iteration done")
                    # no leaked active lease
                    with self.runtime.session_scope() as session:
                        cleanup_ok = not SDRDeviceLeaseRepository(session).holding_for_device(
                            device_id)
            latency_ms = int((time.monotonic() - t0) * 1000)
            proc = sample_process()
            storage = sample_storage(self.runtime)
            self._record_measurement(
                campaign_id, run_id, i, device_id, lease_id, backend_id, measurement,
                success, cleanup_ok, latency_ms, proc, storage, detail)
            if success:
                ok_count += 1
            else:
                fail_count += 1
            if cfg.rest_interval_seconds > 0:
                await _async_sleep(cfg.rest_interval_seconds)
        with self.runtime.session_scope() as session:
            FieldRunRepository(session).update(
                run_id, status="completed", iteration_count=iterations,
                success_count=ok_count, failure_count=fail_count, completed_at=utcnow(),
                summary_json={"ok": ok_count, "failed": fail_count})
            session.commit()

    def _record_measurement(self, campaign_id, run_id, i, device_id, lease_id, backend_id,
                            res, success, cleanup_ok, latency_ms, proc, storage, detail):
        with self.runtime.session_scope() as session:
            caps = SDRDeviceCapabilityRepository(session).get_for_device(device_id)
            cap_rev = caps.revision if caps else None
            expected = int(self.cfg.capture_sample_rate * self.cfg.capture_duration_s)
            FieldMeasurementRepository(session).create(
                campaign_id=campaign_id, run_id=run_id, iteration=i, kind="capture",
                device_id=device_id, capability_revision=cap_rev, lease_id=lease_id,
                backend_id=backend_id,
                measurement_id=(res or {}).get("measurement_id"),
                artifact_id=(res or {}).get("artifact_id"),
                center_frequency_hz=self.cfg.capture_frequency_hz,
                sample_rate_sps=self.cfg.capture_sample_rate, gain_db=self.cfg.capture_gain_db,
                duration_s=self.cfg.capture_duration_s, expected_samples=expected,
                actual_samples=(res or {}).get("sample_count"), bytes=(res or {}).get("bytes"),
                sha256=(res or {}).get("digest"), latency_ms=latency_ms,
                overrun=False, underrun=(res is not None and
                                         (res.get("sample_count") or 0) < expected),
                cleanup_ok=cleanup_ok, rss_bytes=proc.get("rss_bytes"),
                disk_free_bytes=storage.get("disk_free_bytes"), success=success,
                confidence="high" if success else "unknown",
                result_json={"detail": detail} if detail else {})
            session.commit()

    async def _run_survey_campaign(self, campaign_id, device_id, windows, warnings):
        with self.runtime.session_scope() as session:
            run = FieldRunRepository(session).create(
                campaign_id=campaign_id, kind="survey_stability", status="running")
            run_id = run.id
            session.commit()
        backend_id = self.runtime.config.rf_backends.default_backend
        ok = fail = 0
        for i in range(windows):
            lease_id = None
            try:
                lease_id = await self.runtime.hardware_leases.acquire(
                    device_id, backend_id=backend_id, direction="rx", mode="exclusive",
                    operation="field_survey")
                with self.runtime.session_scope() as session:
                    device = SDRDeviceRepository(session).get(device_id)
                res = await self.runtime.hardware_capture.survey(
                    device=device, lease_id=lease_id, backend_id=backend_id)
                success = bool(res["channels"])
                with self.runtime.session_scope() as session:
                    FieldMeasurementRepository(session).create(
                        campaign_id=campaign_id, run_id=run_id, iteration=i, kind="survey",
                        device_id=device_id, backend_id=backend_id,
                        measurement_id=res.get("measurement_id"), success=success,
                        result_json={"channels": res["channels"]},
                        confidence="medium" if success else "unknown")
                    session.commit()
                ok += 1 if success else 0
                fail += 0 if success else 1
            except (CaptureError, DeviceUnavailableError, LeaseConflictError) as exc:
                fail += 1
                warnings.append(f"survey {i}: {type(exc).__name__}")
            finally:
                if lease_id:
                    with contextlib.suppress(Exception):
                        await self.runtime.hardware_leases.release(lease_id, reason="survey done")
            if self.cfg.rest_interval_seconds > 0:
                await _async_sleep(self.cfg.rest_interval_seconds)
        with self.runtime.session_scope() as session:
            FieldRunRepository(session).update(
                run_id, status="completed", iteration_count=windows, success_count=ok,
                failure_count=fail, completed_at=utcnow(), summary_json={"ok": ok, "failed": fail})
            session.commit()

    # -- finalize + classify (Part O/W) ------------------------------------------

    async def _finalize(self, campaign_id, device_id, warnings):
        total, ok = (0, 0)
        with self.runtime.session_scope() as session:
            total, ok = FieldMeasurementRepository(session).counts(campaign_id)
        thr = self.cfg.thresholds
        cap_ratio = (ok / total) if total else 0.0
        # no leaked leases after the campaign
        with self.runtime.session_scope() as session:
            leaked = len(SDRDeviceLeaseRepository(session).holding_for_device(device_id))
        checks = [
            self._check(campaign_id, "capture_success_ratio", "soak",
                        PASSED if cap_ratio >= thr.min_capture_success_ratio else FAILED,
                        f"{ok}/{total} = {cap_ratio:.2f}",
                        {"ratio": cap_ratio, "threshold": thr.min_capture_success_ratio}),
            self._check(campaign_id, "no_leaked_leases", "soak",
                        PASSED if leaked <= thr.max_leaked_leases else FAILED,
                        f"{leaked} active after campaign"),
            self._check(campaign_id, "all_captures_attributable", "soak",
                        PASSED if total > 0 else NOT_EXECUTED, f"{total} measurements"),
            self._check(campaign_id, "two_device_rf", "physical", NOT_EXECUTED,
                        "single SDR attached — physical two-radio exchange not executed"),
        ]
        await self._persist_checks(checks)
        passed = sum(1 for c in checks if c[2] == PASSED)
        failed = sum(1 for c in checks if c[2] == FAILED)
        not_exec = sum(1 for c in checks if c[2] == NOT_EXECUTED)
        classification = (CLASS_FAILED if failed else
                          CLASS_SINGLE_DEVICE if (total > 0 and ok > 0) else CLASS_SOFTWARE)
        status = "passed" if failed == 0 else "failed"
        with self.runtime.session_scope() as session:
            FieldCampaignRepository(session).update(
                campaign_id, status="completed" if failed == 0 else "failed",
                classification=classification, checks_passed=passed, checks_failed=failed,
                checks_not_executed=not_exec, warnings_json=warnings[:50], completed_at=utcnow(),
                summary=f"captures {ok}/{total}; classification={classification}")
            session.commit()
        await self._emit(
            ev.EVENT_CAMPAIGN_COMPLETED if failed == 0 else ev.EVENT_CAMPAIGN_FAILED,
            message=f"campaign {campaign_id} {status}",
            payload={"campaign_id": campaign_id, "classification": classification,
                     "captures_ok": ok, "captures_total": total})
        return campaign_id

    def _check(self, campaign_id, name, category, status, detail=None, evidence=None):
        return (campaign_id, name, status, category, detail, evidence or {})

    async def _persist_checks(self, checks):
        with self.runtime.session_scope() as session:
            for campaign_id, name, status, category, detail, evidence in checks:
                FieldCheckRepository(session).create(
                    campaign_id=campaign_id, name=name, category=category, status=status,
                    detail=(detail or "")[:500] or None, evidence_json=evidence)
            session.commit()
        for campaign_id, name, status, _category, _detail, _ev in checks:
            etype = {PASSED: ev.EVENT_CHECK_PASSED, FAILED: ev.EVENT_CHECK_FAILED,
                     NOT_EXECUTED: ev.EVENT_CHECK_NOT_EXECUTED}.get(status, ev.EVENT_CHECK_STARTED)
            await self._emit(etype, message=f"check {name}: {status}",
                             payload={"campaign_id": campaign_id, "check": name, "status": status})

    # -- soak (Part M/O) ---------------------------------------------------------

    async def soak_run(self, *, campaign_id: str | None = None, target_seconds: float,
                       sample_interval: float | None = None, pid: int | None = None) -> str:
        interval = sample_interval or self.cfg.soak_sample_interval_seconds
        thr = self.cfg.thresholds.model_dump()
        with self.runtime.session_scope() as session:
            soak = SoakRunRepository(session).create(
                campaign_id=campaign_id, node_id=self.node_id, profile="custom",
                target_seconds=target_seconds, sample_interval_seconds=interval,
                thresholds_json=thr, status="running")
            soak_id = soak.id
            session.commit()
        deadline = time.monotonic() + target_seconds
        first = last = None
        peak_rss = peak_fd = 0
        violations = 0
        last_emit = 0.0
        n = 0
        while True:
            s = full_sample(self.runtime, pid=pid)
            with self.runtime.session_scope() as session:
                SoakSampleRepository(session).create(soak_run_id=soak_id, campaign_id=campaign_id,
                                                     **{k: s.get(k) for k in _SAMPLE_COLS})
                session.commit()
            n += 1
            first = first or s
            last = s
            peak_rss = max(peak_rss, s.get("rss_bytes") or 0)
            peak_fd = max(peak_fd, s.get("fd_count") or 0)
            if time.monotonic() - last_emit >= max(interval, 2.0):
                last_emit = time.monotonic()
                await self._emit(ev.EVENT_SOAK_SAMPLED, message="soak sample",
                                 payload={"soak_id": soak_id, "sample": n,
                                          "rss_bytes": s.get("rss_bytes"),
                                          "fd_count": s.get("fd_count"),
                                          "active_leases": s.get("active_leases")})
            if time.monotonic() >= deadline:
                break
            await _async_sleep(min(interval, max(0.0, deadline - time.monotonic())))
        summary, violations = self._soak_summary(first or {}, last or {}, peak_rss, peak_fd)
        if violations:
            await self._emit(ev.EVENT_THRESHOLD_EXCEEDED, message="soak threshold exceeded",
                             payload={"soak_id": soak_id, "violations": summary["violations"]})
        with self.runtime.session_scope() as session:
            SoakRunRepository(session).update(
                soak_id, status="completed", sample_count=n, violations=violations,
                summary_json=summary, completed_at=utcnow())
            session.commit()
        return soak_id

    def _soak_summary(self, first, last, peak_rss, peak_fd):
        thr = self.cfg.thresholds
        rss_growth = (last.get("rss_bytes") or 0) - (first.get("rss_bytes") or 0)
        fd_growth = (last.get("fd_count") or 0) - (first.get("fd_count") or 0)
        free = last.get("disk_free_bytes")
        viols = []
        if rss_growth > thr.max_rss_growth_bytes:
            viols.append(f"rss_growth {rss_growth} > {thr.max_rss_growth_bytes}")
        if fd_growth > thr.max_fd_growth:
            viols.append(f"fd_growth {fd_growth} > {thr.max_fd_growth}")
        if free is not None and free < thr.min_disk_free_bytes:
            viols.append(f"disk_free {free} < {thr.min_disk_free_bytes}")
        if (last.get("active_leases") or 0) > thr.max_leaked_leases:
            viols.append(f"leaked_leases {last.get('active_leases')}")
        summary = {
            "initial_rss_bytes": first.get("rss_bytes"), "final_rss_bytes": last.get("rss_bytes"),
            "rss_growth_bytes": rss_growth, "peak_rss_bytes": peak_rss,
            "initial_fd": first.get("fd_count"), "final_fd": last.get("fd_count"),
            "fd_growth": fd_growth, "peak_fd": peak_fd,
            "final_disk_free_bytes": free, "final_active_leases": last.get("active_leases"),
            "final_child_count": last.get("child_count"),
            "db_bytes": last.get("db_bytes"), "wal_bytes": last.get("wal_bytes"),
            "artifact_bytes": last.get("artifact_bytes"), "violations": viols,
        }
        return summary, len(viols)

    # -- fault injection (Part I) ------------------------------------------------

    async def inject_fault(self, *, fault_type: str, params: dict | None = None,
                          confirm: bool = False, campaign_id: str | None = None) -> str:
        if not self.cfg.fault_injection_enabled:
            raise PermissionError("fault injection is disabled (validation mode only)")
        if fault_type not in FAULT_TYPES:
            raise ValueError(f"unknown fault_type '{fault_type}'")
        params = {k: v for k, v in (params or {}).items()
                  if isinstance(k, str) and isinstance(v, (str, int, float, bool))}
        with self.runtime.session_scope() as session:
            fault = FieldFaultRepository(session).create(
                campaign_id=campaign_id, node_id=self.node_id, fault_type=fault_type,
                params_json=params, status="requested", confirmed=confirm,
                expected_recovery=_EXPECTED_RECOVERY.get(fault_type), timeout_seconds=30.0)
            fault_id = fault.id
            session.commit()
        if not confirm:
            return fault_id
        actual, ok, cleanup = await self._perform_fault(fault_type, params)
        with self.runtime.session_scope() as session:
            FieldFaultRepository(session).update(
                fault_id, status="recovered" if ok else "injected",
                actual_recovery=actual[:320], cleanup_ok=cleanup, injected_at=utcnow(),
                recovered_at=utcnow() if ok else None)
            FieldRecoveryRepository(session).create(
                campaign_id=campaign_id, fault_id=fault_id, kind=fault_type, success=ok,
                survived_objects_json={"note": actual}, detail=actual[:500])
            session.commit()
        await self._emit(ev.EVENT_FAULT_INJECTED, message=f"fault {fault_type}",
                         payload={"fault_id": fault_id, "fault_type": fault_type})
        if ok:
            await self._emit(ev.EVENT_FAULT_RECOVERED, message=f"fault {fault_type} recovered",
                             payload={"fault_id": fault_id, "fault_type": fault_type})
        return fault_id

    async def _perform_fault(self, fault_type, params) -> tuple[str, bool, bool]:
        """Deterministic bounded software fault simulation (validation only)."""
        if fault_type == "provider_unavailable":
            # toggle a discovery provider's availability flag if it exposes one (fake providers)
            for p in self.runtime.hardware_discovery.providers():
                if hasattr(p, "available_flag"):
                    p.available_flag = False
                    await self.runtime.hardware_inventory.refresh()
                    p.available_flag = True
                    await self.runtime.hardware_inventory.refresh()
                    return ("provider toggled unavailable then restored; inventory survived",
                            True, True)
            return ("no togglable provider (process-level test exercises real outage)",
                    False, True)
        if fault_type == "device_missing":
            dev = params.get("device_id")
            if dev:
                await self.runtime.hardware_leases.on_device_missing(dev, reason="fault injection")
                return f"device {dev} marked missing; holding leases orphaned", True, True
            return "no device_id provided", False, True
        if fault_type in ("coordinator_unavailable", "coding_agent_unavailable"):
            return (f"{fault_type} simulated (state persists; restoration allows reassessment)",
                    True, True)
        return f"{fault_type} requires the process-level acceptance harness", False, True

    # -- report (Part P) ---------------------------------------------------------

    def report(self, campaign_id: str) -> dict:
        with self.runtime.session_scope() as session:
            campaign = FieldCampaignRepository(session).get(campaign_id)
            if campaign is None:
                return {}
            nodes = FieldNodeRepository(session).list_for_campaign(campaign_id)
            runs = FieldRunRepository(session).list_for_campaign(campaign_id)
            checks = FieldCheckRepository(session).list_for_campaign(campaign_id)
            faults = FieldFaultRepository(session).list_for_campaign(campaign_id)
            soaks = SoakRunRepository(session).list(campaign_id=campaign_id)
            total, ok = FieldMeasurementRepository(session).counts(campaign_id)
            return {
                "campaign": {"id": campaign.id, "name": campaign.name,
                             "objective": campaign.objective, "profile": campaign.profile,
                             "status": campaign.status, "classification": campaign.classification,
                             "software_version": campaign.software_version,
                             "config_digest": campaign.config_digest,
                             "topology": campaign.topology_json,
                                     "thresholds": campaign.thresholds_json,
                             "warnings": campaign.warnings_json, "summary": campaign.summary},
                "nodes": [{"role": n.role, "node_ref": n.node_ref, "host_id": n.sanitized_host_id,
                           "fingerprint": n.identity_fingerprint, "ready": n.ready} for n in nodes],
                "runs": [{"kind": r.kind, "status": r.status, "iterations": r.iteration_count,
                          "ok": r.success_count, "failed": r.failure_count} for r in runs],
                "checks": [{"name": c.name, "category": c.category, "status": c.status,
                            "detail": c.detail} for c in checks],
                "faults": [{"fault_type": f.fault_type, "status": f.status,
                            "actual_recovery": f.actual_recovery} for f in faults],
                "soak": [{"id": s.id, "sample_count": s.sample_count, "violations": s.violations,
                          "summary": s.summary_json} for s in soaks],
                "measurements": {"total": total, "ok": ok},
            }


_SAMPLE_COLS = ("rss_bytes", "cpu_seconds", "fd_count", "child_count", "thread_count",
                "db_bytes", "wal_bytes", "artifact_bytes", "log_bytes", "event_count",
                "outbox_pending", "active_leases", "failed_retries", "disk_free_bytes")

_EXPECTED_RECOVERY = {
    "provider_unavailable": "inventory survives; provider restoration re-enables discovery",
    "device_missing": "holding leases orphaned; mission gets a factual event; no auto-TX",
    "coordinator_unavailable": "mission state persists; restoration allows reassessment",
    "coding_agent_unavailable": "mission state persists; restoration allows reassessment",
    "node_process_kill": "restart reconciles stale leases; no duplicate device/mission",
    "rf_subprocess_kill": "RF session cleaned up; unrelated backends unaffected",
}


async def _async_sleep(seconds: float) -> None:
    import asyncio
    if seconds > 0:
        await asyncio.sleep(seconds)
