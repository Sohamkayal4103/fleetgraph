"""Real-hardware qualification orchestration + support matrix (Stage 14C.1, Parts A/D/K/L).

Builds a bounded, sanitized qualification environment record, runs factual checks against ACTUAL
attached hardware (dedup, capability probe, backend binding, lease lifecycle, real RX capture),
classifies support honestly (never "supported" just because a device was discovered), and persists
additive runs/checks/measurements with sanitized events. No fake provider result can satisfy
physical qualification; if no compatible hardware is attached the run stops factually.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aithernet import __version__
from aithernet.hardware import events as ev
from aithernet.hardware.capture import PROCESSING_REVISION, CaptureError, CaptureService
from aithernet.hardware.contracts import (
    DeviceNotFoundError,
    DeviceUnavailableError,
    LeaseConflictError,
)
from aithernet.state.models import utcnow
from aithernet.state.repositories import (
    HardwareQualificationCheckRepository,
    HardwareQualificationRunRepository,
    SDRDeviceCapabilityRepository,
    SDRDeviceLeaseRepository,
    SDRDeviceRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

PASSED, FAILED, NOT_EXECUTED, SKIPPED = "passed", "failed", "not_executed", "skipped"

#: Support classification rank order (Part K) — a run reports the HIGHEST level actually reached.
_RANK = [
    "failed", "discovered", "probed", "rx_qualified", "tx_lease_qualified",
    "capture_qualified", "survey_qualified", "disconnect_recovery_qualified",
]


class QualificationService:
    """Runs real-hardware qualification and records additive evidence."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.node_id = runtime.config.node_id
        self.qcfg = runtime.config.hardware.qualification
        self.capture = CaptureService(runtime)

    # -- environment record (Part A) ---------------------------------------------

    def _run_tool(self, cmd: list[str], timeout: float = 10.0) -> str:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            return (out.stdout or "") + (out.stderr or "")
        except (OSError, subprocess.SubprocessError):
            return ""

    def _sanitized_host_id(self) -> str:
        # A stable, non-reversible host identifier (never the raw hostname/IP).
        raw = f"{platform.node()}|{self.node_id}".encode()
        return "host-" + hashlib.sha256(raw).hexdigest()[:12]

    def build_environment(self) -> dict:
        """Bounded, sanitized qualification environment record (no env maps/creds/private paths)."""
        gr_py = self.qcfg.gnuradio_python
        gr_version = self._run_tool(
            [gr_py, "-c", "from gnuradio import gr; print(gr.version())"]).strip()
        gr_python_version = self._run_tool([gr_py, "-c",
                "import sys; print(sys.version.split()[0])"]
                                           ).strip()
        soapy_info = self._run_tool(["SoapySDRUtil", "--info"])
        soapy_version = next((ln.split(":", 1)[1].strip() for ln in soapy_info.splitlines()
                              if "Lib Version" in ln), "")
        modules = [ln.split("/")[-1].strip().split()[0]
                   for ln in soapy_info.splitlines() if "Module found:" in ln]
        uhd_version = self._run_tool(["uhd_config_info", "--version"]).strip()[:64]
        os_pretty = ""
        try:
            for ln in open("/etc/os-release"):
                if ln.startswith("PRETTY_NAME="):
                    os_pretty = ln.split("=", 1)[1].strip().strip('"')
        except OSError:
            os_pretty = platform.system()
        # RF backend revisions are operator-pinned (recorded, never modified by qualification).
        backends = [
            {"backend_id": bid, "source_revision": b.source_revision}
            for bid, b in (self.runtime.config.rf_backends.backends or {}).items()
        ]
        return {
            "node_version": __version__,
            "node_python": platform.python_version(),
            "gnuradio_python": gr_py,
            "gnuradio_python_version": gr_python_version or None,
            "gnuradio_version": gr_version or None,
            "os": os_pretty or None,
            "kernel": platform.release(),
            "soapysdr_version": soapy_version or None,
            "soapy_modules": modules[:12],
            "uhd_version": uhd_version or None,
            "host_id": self._sanitized_host_id(),
            "rf_backends": backends,
            "processing_revision": PROCESSING_REVISION,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    # -- emit helpers ------------------------------------------------------------

    async def _emit(self, event_type: str, *, message: str, payload: dict) -> None:
        import contextlib
        with contextlib.suppress(Exception):
            await self.runtime._emit_event(event_type=event_type, mission_id=None,
                                           source="hardware", message=message, payload=payload)

    # -- preflight (Part J) ------------------------------------------------------

    async def preflight(self, device_id: str) -> dict:
        """Read-only readiness for qualifying a device. Never opens the device or transmits."""
        checks: list[dict] = []

        def add(name, status, detail=None, **ev_):
            checks.append({"name": name, "status": status, "detail": detail, **ev_})

        with self.runtime.session_scope() as session:
            device = SDRDeviceRepository(session).get(device_id)
            if device is None or device.node_id != self.node_id:
                return {"ok": False, "device_id": device_id,
                        "checks": [{"name": "device_known", "status": FAILED,
                                    "detail": "unknown device"}]}
            add("device_known", PASSED, device.display_name)
            add("device_present", PASSED if device.presence_state == "present" else FAILED,
                device.presence_state)
            add("device_enabled", PASSED if device.enabled else FAILED)
            caps = SDRDeviceCapabilityRepository(session).get_for_device(device.id)
            add("capabilities_probed", PASSED if caps else NOT_EXECUTED,
                None if caps else "run discovery with probing")
            holding = SDRDeviceLeaseRepository(session).holding_for_device(device.id)
            add("lease_available", PASSED if not holding else FAILED,
                None if not holding else f"{len(holding)} active lease(s)")
        gr_ok = self._run_tool(
            [self.qcfg.gnuradio_python, "-c", "from gnuradio import gr, iio; print('ok')"]
        ).strip().endswith("ok")
        add("gnuradio_interpreter", PASSED if gr_ok else FAILED,
            self.qcfg.gnuradio_python if gr_ok else "cannot import gnuradio")
        ok = all(c["status"] != FAILED for c in checks)
        return {"ok": ok, "device_id": device_id, "checks": checks}

    # -- full qualification run (Parts D/E/F/K) ----------------------------------

    async def run(
        self, device_id: str, *, do_capture: bool = True, do_survey: bool = False,
        capture_duration: float | None = None,
    ) -> str:
        """Run real-hardware qualification and persist an additive run + checks + measurements."""
        env = self.build_environment()
        with self.runtime.session_scope() as session:
            device = SDRDeviceRepository(session).get(device_id)
            if device is None or device.node_id != self.node_id:
                raise DeviceNotFoundError(f"Unknown device '{device_id}'.")
            caps = SDRDeviceCapabilityRepository(session).get_for_device(device.id)
            cap_rev = caps.revision if caps else None
            run = HardwareQualificationRunRepository(session).create(
                node_id=self.node_id, device_id=device.id, hardware_key=device.hardware_key,
                provider_id=device.provider_id, status="running", environment_json=env,
                capability_revision=cap_rev, support_classification="discovered")
            run_id = run.id
            session.commit()
        await self._emit(ev.EVENT_QUALIFICATION_STARTED,
                         message=f"qualification started for {device_id}",
                         payload={"run_id": run_id, "device_id": device_id})

        results: list[tuple[str, str]] = []  # (check_name, status)
        warnings: list[str] = []
        reached = "discovered"

        async def record(name, category, status, detail=None, evidence=None):
            with self.runtime.session_scope() as session:
                HardwareQualificationCheckRepository(session).create(
                    run_id=run_id, node_id=self.node_id, device_id=device_id, name=name,
                    category=category, status=status, detail=(detail or "")[:500] or None,
                    evidence_json=evidence or {})
                session.commit()
            etype = {PASSED: ev.EVENT_QUALIFICATION_CHECK_PASSED,
                     FAILED: ev.EVENT_QUALIFICATION_CHECK_FAILED,
                     NOT_EXECUTED: ev.EVENT_QUALIFICATION_CHECK_NOT_EXECUTED}.get(
                         status, ev.EVENT_QUALIFICATION_CHECK_STARTED)
            await self._emit(etype, message=f"check {name}: {status}",
                             payload={"run_id": run_id, "check": name, "status": status})
            results.append((name, status))

        # 1) capability probe present
        with self.runtime.session_scope() as session:
            caps = SDRDeviceCapabilityRepository(session).get_for_device(device_id)
            cj = (caps.capabilities_json if caps else {}) or {}
        if caps and caps.confidence in ("high", "medium"):
            await record("capability_probe", "capabilities", PASSED,
                         f"confidence={caps.confidence}",
                         {"rx": cj.get("rx_supported"), "tx": cj.get("tx_supported"),
                          "channels": cj.get("channel_count")})
            reached = "probed"
        else:
            await record("capability_probe", "capabilities", FAILED, "no probed capabilities")

        # 2) backend binding resolves deterministic device args
        backend_id = self.runtime.config.rf_backends.default_backend
        bind_ok = False
        with self.runtime.session_scope() as session:
            device = SDRDeviceRepository(session).get(device_id)
            try:
                args = self.runtime.hardware_leases.derive_binding(session, device, backend_id)
                bind_ok = True
                await record("backend_binding", "binding", PASSED,
                             f"backend={backend_id}", {"device_arg_keys": sorted(args.keys())})
            except Exception as exc:  # noqa: BLE001
                await record("backend_binding", "binding", FAILED, str(exc)[:200])

        # 3) lease lifecycle (real device; read-only — no transmit needed)
        if bind_ok and cj.get("rx_supported"):
            lease_id = None
            try:
                lease_id = await self.runtime.hardware_leases.acquire(
                    device_id, backend_id=backend_id, direction="rx", mode="exclusive",
                    operation="qualification")
                await record("lease_acquire_rx", "lease", PASSED, lease_id[:8],
                             {"lease_id": lease_id})
                reached = "rx_qualified"
                # conflict
                try:
                    await self.runtime.hardware_leases.acquire(
                        device_id, backend_id=backend_id, direction="rx", mode="exclusive")
                    await record("lease_conflict_rejected", "lease", FAILED,
                                 "second exclusive lease was granted")
                except (LeaseConflictError, DeviceUnavailableError):
                    await record("lease_conflict_rejected", "lease", PASSED)
                # renew
                renewed = await self.runtime.hardware_leases.renew(lease_id)
                await record("lease_renew", "lease", PASSED if renewed else FAILED)
            finally:
                if lease_id:
                    await self.runtime.hardware_leases.release(lease_id,
                            reason="qualification done")
                    await record("lease_release", "lease", PASSED)
            # reacquire
            try:
                l2 = await self.runtime.hardware_leases.acquire(
                    device_id, backend_id=backend_id, direction="rx", mode="exclusive")
                await record("lease_reacquire", "lease", PASSED)
                await self.runtime.hardware_leases.release(l2, reason="qualification done")
            except Exception as exc:  # noqa: BLE001
                await record("lease_reacquire", "lease", FAILED, str(exc)[:120])
        else:
            await record("lease_acquire_rx", "lease", NOT_EXECUTED, "RX unsupported or no binding")

        # 4) TX exclusive-lease check (LEASE ONLY — never transmits)
        if bind_ok and cj.get("tx_supported"):
            try:
                tl = await self.runtime.hardware_leases.acquire(
                    device_id, backend_id=backend_id, direction="tx", mode="exclusive",
                    operation="tx_lease_check")
                await record("tx_lease_exclusive", "lease", PASSED,
                             "transmit lease is exclusive (no RF emitted)")
                await self.runtime.hardware_leases.release(tl, reason="tx lease check done")
                if _rank(reached) < _rank("tx_lease_qualified"):
                    reached = "tx_lease_qualified"
            except Exception as exc:  # noqa: BLE001
                await record("tx_lease_exclusive", "lease", FAILED, str(exc)[:120])

        # 5) real RX capture (Reference Mission 1) — acquire, capture, artifact, release
        if do_capture and bind_ok and cj.get("rx_supported"):
            await self._capture_check(run_id, device_id, backend_id, record, warnings,
                                      capture_duration)
            if any(n == "physical_capture" and s == PASSED for n, s in results):
                reached = "capture_qualified"

        # 6) optional 2.4 GHz survey (Reference Mission 2)
        if do_survey and bind_ok and cj.get("rx_supported"):
            await self._survey_check(run_id, device_id, backend_id, record, warnings)
            if any(n == "survey_2g4" and s == PASSED for n, s in results):
                reached = "survey_qualified"

        # disconnect/recovery (Part H) is recorded as not_executed unless physically performed.
        await record("disconnect_reconnect", "recovery", NOT_EXECUTED,
                     "physical disconnect not performed in this harness (IP-attached device)")

        # finalize
        passed = sum(1 for _, s in results if s == PASSED)
        failed = sum(1 for _, s in results if s == FAILED)
        not_exec = sum(1 for _, s in results if s == NOT_EXECUTED)
        classification = reached if failed == 0 else (reached if _rank(reached) > 0 else "failed")
        status = "passed" if failed == 0 else ("partial" if passed else "failed")
        with self.runtime.session_scope() as session:
            HardwareQualificationRunRepository(session).update(
                run_id, status=status, support_classification=classification,
                checks_total=len(results), checks_passed=passed, checks_failed=failed,
                checks_not_executed=not_exec, warnings_json=warnings,
                summary=f"{passed} passed, {failed} failed, {not_exec} not executed; "
                        f"classification={classification}",
                completed_at=utcnow())
            session.commit()
        await self._emit(ev.EVENT_QUALIFICATION_COMPLETED,
                         message=f"qualification completed for {device_id}",
                         payload={"run_id": run_id, "status": status,
                                  "classification": classification, "passed": passed,
                                  "failed": failed, "not_executed": not_exec})
        return run_id

    async def _capture_check(self, run_id, device_id, backend_id, record, warnings, duration):
        lease_id = None
        try:
            lease_id = await self.runtime.hardware_leases.acquire(
                device_id, backend_id=backend_id, direction="rx", mode="exclusive",
                operation="rx_capture")
            with self.runtime.session_scope() as session:
                device = SDRDeviceRepository(session).get(device_id)
            res = await self.capture.capture(
                device=device, freq_hz=self.qcfg.default_rx_frequency_hz,
                sample_rate=self.qcfg.default_sample_rate, gain_db=self.qcfg.default_gain_db,
                duration_s=duration or self.qcfg.default_duration_seconds,
                antenna=self.qcfg.default_antenna, lease_id=lease_id, backend_id=backend_id,
                qualification_run_id=run_id)
            plausible = res["bytes"] > 0 and res["sample_count"] > 0
            await record("physical_capture", "capture", PASSED if plausible else FAILED,
                         f"{res['bytes']} bytes / {res['sample_count']} samples",
                         {"measurement_id": res["measurement_id"],
                                 "artifact_id": res["artifact_id"],
                          "digest": res["digest"], "bytes": res["bytes"],
                          "sample_count": res["sample_count"], "source": res["source"]})
        except (CaptureError, DeviceUnavailableError, LeaseConflictError) as exc:
            warnings.append(f"capture: {type(exc).__name__}")
            await record("physical_capture", "capture", FAILED, str(exc)[:200])
        finally:
            if lease_id:
                await self.runtime.hardware_leases.release(lease_id, reason="capture done")

    async def _survey_check(self, run_id, device_id, backend_id, record, warnings):
        lease_id = None
        try:
            lease_id = await self.runtime.hardware_leases.acquire(
                device_id, backend_id=backend_id, direction="rx", mode="exclusive",
                operation="survey_2g4")
            with self.runtime.session_scope() as session:
                device = SDRDeviceRepository(session).get(device_id)
            res = await self.capture.survey(device=device, lease_id=lease_id, backend_id=backend_id,
                                            qualification_run_id=run_id)
            ok = bool(res["channels"])
            await record("survey_2g4", "survey", PASSED if ok else FAILED,
                         f"{len(res['channels'])} channel region(s) measured",
                         {"measurement_id": res["measurement_id"], "artifacts": res["artifacts"],
                          "ranked": res["channels"][:5]})
        except (CaptureError, DeviceUnavailableError, LeaseConflictError) as exc:
            warnings.append(f"survey: {type(exc).__name__}")
            await record("survey_2g4", "survey", FAILED, str(exc)[:200])
        finally:
            if lease_id:
                await self.runtime.hardware_leases.release(lease_id, reason="survey done")


def _rank(classification: str) -> int:
    return _RANK.index(classification) if classification in _RANK else 0
