"""Real RX capture + 2.4 GHz survey orchestration (Stage 14C.1, Parts E/F/I).

Drives the bounded GNU Radio capture/analysis subprocess scripts in a SEPARATELY configured GNU
Radio interpreter (Aithernet's venv never imports gnuradio), imports captured IQ into the managed
content-addressed artifact store (bytes never enter SQLite), and records full physical-measurement
provenance. RX only — no transmit. Source backend + addressing are derived deterministically from
the persisted device + approved backend binding; the coordinator never supplies device arguments.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aithernet.hardware import events as ev
from aithernet.state.models import RFArtifact
from aithernet.state.repositories import (
    HardwareRFMeasurementRepository,
    SDRDeviceCapabilityRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
PROCESSING_REVISION = "14c1-gr-capture-1"


class CaptureError(Exception):
    """A real RX capture failed. Message is operator-facing and sanitized."""


class CaptureService:
    """Executes real RX captures + energy surveys and records provenance."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.node_id = runtime.config.node_id
        self.qcfg = runtime.config.hardware.qualification

    async def _emit(self, event_type: str, *, message: str, payload: dict,
                    mission_id: str | None = None) -> None:
        with contextlib.suppress(Exception):
            await self.runtime._emit_event(
                event_type=event_type, mission_id=mission_id, source="hardware",
                message=message, payload=payload,
            )

    # -- deterministic source selection ------------------------------------------

    def _capabilities(self, device_id: str) -> dict:
        with self.runtime.session_scope() as session:
            caps = SDRDeviceCapabilityRepository(session).get_for_device(device_id)
            return (caps.capabilities_json if caps else {}) or {}

    def resolve_source(self, device) -> dict:
        """Pick the real GR source backend + bounded addressing from device facts."""
        caps = self._capabilities(device.id)
        device_args = (caps.get("device_args") or {})
        uri = device_args.get("uri") or device.transport
        driver = (device.driver or "").lower()
        if driver in ("plutosdr", "pluto", "ad9361", "ad9363", "ad9363a") and uri:
            return {"source": "iio_fmcomms2", "uri": uri}
        pairs = [f"{k}={v}" for k, v in device_args.items() if k in ("driver", "serial", "uri")]
        return {"source": "soapy", "device_args": ",".join(pairs) or f"driver={driver}"}

    def _gr_python(self) -> str:
        return self.qcfg.gnuradio_python

    async def _run_script(self, script: str, args: list[str], timeout: float) -> dict:
        cmd = [self._gr_python(), str(_SCRIPTS / script), *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (TimeoutError, OSError) as exc:
            raise CaptureError(f"capture subprocess failed ({type(exc).__name__})") from exc
        text = (out or b"").decode("utf-8", "replace").strip().splitlines()
        for line in reversed(text):
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(line)
        raise CaptureError("capture subprocess produced no JSON result")

    # -- one bounded RX capture --------------------------------------------------

    async def capture(
        self, *, device, freq_hz: float, sample_rate: float, gain_db: float, duration_s: float,
        antenna: str | None = None, lease_id: str | None = None, backend_id: str | None = None,
        qualification_run_id: str | None = None, mission_id: str | None = None,
        mission_run_id: str | None = None, mission_step_id: str | None = None,
        kind: str = "capture",
    ) -> dict:
        """Run ONE bounded RX capture, import the artifact, and record provenance. RX only."""
        duration_s = min(duration_s, self.qcfg.max_duration_seconds)
        # Deterministic receive-only gate at the canonical RX execution boundary: validate
        # freq/rate/duration/gain against the effective adapter∩device envelope. Permissive when the
        # envelope is undetermined (generic SDR with no declared ranges) so working captures are not
        # regressed; gain is clamped to the device ceiling.
        from aithernet.missions import rf_constraints as _rfc
        from aithernet.rf import capabilities as _caps
        try:
            _plan = _rfc.validate_capture(
                device_id=device.id, freq_hz=freq_hz, sample_rate=sample_rate,
                duration_s=duration_s, gain_db=gain_db,
                adapter=_caps.adapter_for_driver(getattr(device, "driver", None)),
                device_caps=_caps.device_capabilities_from_contract(
                    getattr(device, "capabilities", None)))
        except _rfc.ConstraintViolation as exc:
            await self._emit(ev.EVENT_PHYSICAL_CAPTURE_FAILED,
                             message=f"capture rejected on {device.id}",
                             payload={"device_id": device.id, "constraint": exc.category},
                             mission_id=mission_id)
            raise CaptureError(f"receive-only constraint: {exc.category}") from exc
        gain_db = _plan.gain_db  # use the envelope-clamped gain
        src = self.resolve_source(device)
        await self._emit(ev.EVENT_PHYSICAL_CAPTURE_STARTED,
                         message=f"capture started on {device.id}",
                         payload={"device_id": device.id, "freq_hz": freq_hz,
                                  "sample_rate": sample_rate, "duration_s": duration_s,
                                  "source": src["source"]}, mission_id=mission_id)
        started = datetime.now(UTC)
        with tempfile.TemporaryDirectory(prefix="aithernet_cap_") as tmpd:
            out_path = os.path.join(tmpd, "capture.cf32")
            args = ["--source", src["source"], "--freq", str(freq_hz), "--rate", str(sample_rate),
                    "--gain", str(gain_db), "--duration", str(duration_s), "--output", out_path]
            if src["source"] == "iio_fmcomms2":
                args += ["--uri", src["uri"]]
            else:
                args += ["--device-args", src["device_args"]]
                if antenna:
                    args += ["--antenna", antenna]
            try:
                result = await self._run_script(
                    "gr_capture.py", args, self.qcfg.capture_timeout_seconds)
            except CaptureError as exc:
                await self._emit(ev.EVENT_PHYSICAL_CAPTURE_FAILED,
                                 message=f"capture failed on {device.id}",
                                 payload={"device_id": device.id, "error": str(exc)},
                                 mission_id=mission_id)
                raise
            if result.get("status") != "ok" or not os.path.exists(out_path) or \
                    os.path.getsize(out_path) == 0:
                await self._emit(ev.EVENT_PHYSICAL_CAPTURE_FAILED,
                                 message=f"capture produced no data on {device.id}",
                                 payload={"device_id": device.id,
                                          "error_type": result.get("error_type")},
                                 mission_id=mission_id)
                raise CaptureError("capture produced no usable IQ data")
            ended = datetime.now(UTC)
            stored = self.runtime.artifacts.store.import_file(out_path)
            size = result["bytes"]
            sample_count = result.get("sample_count", size // 8)

        artifact_id = self._index_artifact(
            device=device, backend_id=backend_id, stored=stored, size=size,
            sample_count=sample_count, freq_hz=freq_hz, mission_id=mission_id,
            mission_run_id=mission_run_id, mission_step_id=mission_step_id, kind=kind)

        measurement = self._record_measurement(
            device=device, kind=kind, freq_hz=freq_hz, sample_rate=sample_rate, gain_db=gain_db,
            antenna=antenna or src.get("antenna"), backend_id=backend_id, lease_id=lease_id,
            qualification_run_id=qualification_run_id, mission_id=mission_id,
            mission_run_id=mission_run_id, mission_step_id=mission_step_id,
            size=size, sample_count=sample_count, digest=stored.digest, artifact_id=artifact_id,
            started=started, ended=ended, result={"capture": result})

        await self._emit(ev.EVENT_PHYSICAL_CAPTURE_COMPLETED,
                         message=f"capture completed on {device.id}",
                         payload={"device_id": device.id, "measurement_id": measurement,
                                  "bytes": size, "sample_count": sample_count,
                                  "digest": stored.digest}, mission_id=mission_id)
        return {"measurement_id": measurement, "artifact_id": artifact_id, "bytes": size,
                "sample_count": sample_count, "digest": stored.digest,
                "source": src["source"], "freq_hz": freq_hz, "sample_rate": sample_rate}

    # -- 2.4 GHz energy survey ---------------------------------------------------

    async def survey(
        self, *, device, lease_id: str | None = None, backend_id: str | None = None,
        gain_db: float | None = None, qualification_run_id: str | None = None,
        mission_id: str | None = None,
    ) -> dict:
        """Sequential normalized energy survey across configured 2.4 GHz channel centers.

        An energy scan measures AGGREGATE activity per channel region; it does NOT classify each
        emitter as Wi-Fi (no decoding is performed). Results are ranked by observed power.
        """
        gain = gain_db if gain_db is not None else self.qcfg.default_gain_db
        rate = self.qcfg.default_sample_rate
        dwell = self.qcfg.survey_dwell_seconds
        src = self.resolve_source(device)
        channels: list[dict] = []
        started = datetime.now(UTC)
        with tempfile.TemporaryDirectory(prefix="aithernet_survey_") as tmpd:
            for center_mhz in self.qcfg.survey_channel_centers_mhz:
                freq = center_mhz * 1e6
                cap = os.path.join(tmpd, f"ch_{int(center_mhz)}.cf32")
                args = ["--source", src["source"], "--freq", str(freq), "--rate", str(rate),
                        "--gain", str(gain), "--duration", str(dwell), "--output", cap]
                if src["source"] == "iio_fmcomms2":
                    args += ["--uri", src["uri"]]
                else:
                    args += ["--device-args", src["device_args"]]
                with contextlib.suppress(CaptureError):
                    cap_res = await self._run_script(
                        "gr_capture.py", args, self.qcfg.capture_timeout_seconds)
                    if cap_res.get("status") != "ok" or not os.path.exists(cap):
                        continue
                    analysis = await self._run_script(
                        "rf_analyze.py",
                        ["--input", cap, "--rate", str(rate),
                         "--threshold-db", str(self.qcfg.survey_occupancy_threshold_db)],
                        self.qcfg.capture_timeout_seconds)
                    channels.append({
                        "center_mhz": center_mhz, "duration_s": dwell,
                        "sample_count": analysis.get("sample_count"),
                        "median_power_db": analysis.get("median_power_db"),
                        "p95_power_db": analysis.get("p95_power_db"),
                        "peak_power_db": analysis.get("peak_power_db"),
                        "occupancy_fraction": analysis.get("occupancy_fraction"),
                    })
            ranked = sorted(channels, key=lambda c: (c.get("median_power_db") or -999),
                            reverse=True)
            # Write bounded JSON + CSV result artifacts.
            result = {
                "kind": "energy_survey_2g4", "ranked_by": "median_power_db",
                "threshold_db": self.qcfg.survey_occupancy_threshold_db, "sample_rate": rate,
                "channels": ranked,
                "note": ("An energy scan measures aggregate activity per channel region and does "
                         "NOT identify emitters as Wi-Fi without decoding/classification."),
            }
            json_path = os.path.join(tmpd, "survey.json")
            Path(json_path).write_text(json.dumps(result, indent=2))
            csv_path = os.path.join(tmpd, "survey.csv")
            with open(csv_path, "w") as f:
                f.write("center_mhz,median_power_db,p95_power_db,peak_power_db,occupancy_fraction\n")
                for c in ranked:
                    f.write(f"{c['center_mhz']},{c.get('median_power_db')},{c.get('p95_power_db')},"
                            f"{c.get('peak_power_db')},{c.get('occupancy_fraction')}\n")
            json_size = os.path.getsize(json_path)
            csv_size = os.path.getsize(csv_path)
            json_obj = self.runtime.artifacts.store.import_file(json_path)
            csv_obj = self.runtime.artifacts.store.import_file(csv_path)
        json_aid = self._index_artifact(device=device, backend_id=backend_id, stored=json_obj,
                                        size=json_size, sample_count=0,
                                        freq_hz=2_437_000_000.0, mission_id=mission_id,
                                        kind="survey_json", media_type="application/json",
                                        display="2.4GHz survey JSON")
        csv_aid = self._index_artifact(device=device, backend_id=backend_id, stored=csv_obj,
                                       size=csv_size, sample_count=0, freq_hz=2_437_000_000.0,
                                       mission_id=mission_id, kind="survey_csv",
                                       media_type="text/csv", display="2.4GHz survey CSV")
        ended = datetime.now(UTC)
        measurement = self._record_measurement(
            device=device, kind="survey", freq_hz=2_437_000_000.0, sample_rate=rate, gain_db=gain,
            antenna=None, backend_id=backend_id, lease_id=lease_id,
            qualification_run_id=qualification_run_id, mission_id=mission_id, mission_run_id=None,
            mission_step_id=None, size=0,
                    sample_count=sum(c.get("sample_count") or 0 for c in ranked),
            digest=json_obj.digest, artifact_id=json_aid, started=started, ended=ended,
            result=result,
            analysis_params={"threshold_db": self.qcfg.survey_occupancy_threshold_db,
                             "dwell_seconds": dwell,
                                     "centers_mhz": self.qcfg.survey_channel_centers_mhz},
            confidence="medium")
        return {"measurement_id": measurement, "channels": ranked,
                "artifacts": [json_aid, csv_aid]}

    # -- persistence helpers -----------------------------------------------------

    def _index_artifact(
        self, *, device, backend_id, stored, size, sample_count, freq_hz, mission_id=None,
        mission_run_id=None, mission_step_id=None, kind="iq_capture",
        media_type="application/octet-stream", display=None,
    ) -> str:
        with self.runtime.session_scope() as session:
            artifact = RFArtifact(
                node_id=self.node_id, backend_id=backend_id or "hardware",
                relative_path=f"qualification/{stored.digest.split(':')[-1][:16]}-{kind}",
                artifact_kind=kind, media_type=media_type,
                size_bytes=size or None, content_hash=stored.digest, digest=stored.digest,
                object_ref=stored.relative_object_path, availability_state="indexed",
                display_name=display or f"{device.display_name} {kind}",
                mission_id=mission_id, mission_run_id=mission_run_id,
                mission_step_id=mission_step_id,
            )
            session.add(artifact)
            session.flush()
            aid = artifact.id
            session.commit()
        return aid

    def _record_measurement(
        self, *, device, kind, freq_hz, sample_rate, gain_db, antenna, backend_id, lease_id,
        qualification_run_id, mission_id, mission_run_id, mission_step_id, size, sample_count,
        digest, artifact_id, started, ended, result, analysis_params=None, confidence="high",
    ) -> str:
        with self.runtime.session_scope() as session:
            caps = SDRDeviceCapabilityRepository(session).get_for_device(device.id)
            cap_rev = caps.revision if caps else None
            m = HardwareRFMeasurementRepository(session).create(
                node_id=self.node_id, device_id=device.id,
                qualification_run_id=qualification_run_id, capability_revision=cap_rev,
                lease_id=lease_id, backend_id=backend_id, mission_id=mission_id,
                mission_run_id=mission_run_id, mission_step_id=mission_step_id, kind=kind,
                center_frequency_hz=freq_hz, sample_rate_sps=sample_rate, gain_db=gain_db,
                antenna=antenna, channel="0", stream_format=self.qcfg.stream_format,
                capture_bytes=size or None, sample_count=sample_count or None, sha256=digest,
                artifact_id=artifact_id, processing_revision=PROCESSING_REVISION,
                analysis_params_json=analysis_params or {}, result_json=result,
                confidence=confidence, warnings_json=[], started_at=started, ended_at=ended,
            )
            mid = m.id
            session.commit()
        return mid
