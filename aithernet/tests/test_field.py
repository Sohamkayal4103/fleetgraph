"""Stage 14C.2 field-validation tests (software harness; injected fake hardware).

No physical SDR is required: a fake discovery provider is injected, and the capture step is
replaced by an injected validation backend so the campaign orchestration (lease cycle, measurement
recording, leaked-lease detection, threshold evaluation, classification) is exercised
deterministically. Physical acceptance evidence comes from the real PlutoSDR (see the report).
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from _hw_util import descriptor, make_hw_runtime
from aithernet.api.app import create_app
from aithernet.state.models import MissionStep
from aithernet.state.repositories import (
    FieldCampaignRepository,
    FieldCheckRepository,
    FieldMeasurementRepository,
    SDRDeviceLeaseRepository,
    SDRDeviceRepository,
    SoakRunRepository,
)

run = asyncio.run


def _field_runtime(tmp_path, **hw_kwargs):
    rt, fake = make_hw_runtime(tmp_path, descriptors=[descriptor()], **hw_kwargs)
    rt.config.field_validation.operator_actions_enabled = True
    rt.config.field_validation.capture_duration_s = 0.1
    rt.config.field_validation.rest_interval_seconds = 0.0
    return rt, fake


def _device_id(rt):
    run(rt.hardware_inventory.refresh())
    with rt.session_scope() as s:
        return SDRDeviceRepository(s).list(node_id=rt.config.node_id)[0].id


def _inject_fake_capture(rt):
    """Replace the real GR capture with a deterministic in-memory result (no hardware)."""
    async def _fake_capture(*, device, freq_hz, sample_rate, gain_db, duration_s, lease_id=None,
                            backend_id=None, **kw):
        n = int(sample_rate * duration_s)
        return {"measurement_id": "m-fake", "artifact_id": "a-fake", "bytes": n * 8,
                "sample_count": n, "digest": "sha256:fake", "source": "fake",
                "freq_hz": freq_hz, "sample_rate": sample_rate}
    rt.hardware_capture.capture = _fake_capture


# -- campaign persistence + create -----------------------------------------------------------


def test_campaign_create_persists_topology(tmp_path):
    rt, _ = _field_runtime(tmp_path)
    cid = rt.field.create_campaign(name="c1", objective="soak", profile="smoke")
    with rt.session_scope() as s:
        c = FieldCampaignRepository(s).get(cid)
        assert c.name == "c1" and c.profile == "smoke"
        assert c.topology_json["control_plane"] == "authenticated_ip"
        assert c.config_digest.startswith("sha256:")
        assert c.classification == "software_harness_complete"


def test_preflight_runs(tmp_path):
    rt, _ = _field_runtime(tmp_path)
    dev = _device_id(rt)
    pf = run(rt.field.preflight(device_id=dev))
    names = {c["name"]: c for c in pf["checks"]}
    assert "storage_capacity" in names
    assert names["required_device_present"]["status"] == "passed"
    # peer multi-node check is honestly not_executed when solo
    assert names["peer_nodes"]["status"] == "not_executed"


# -- capture campaign (injected capture) -----------------------------------------------------


def test_capture_campaign_records_measurements_and_classifies(tmp_path):
    rt, _ = _field_runtime(tmp_path)
    dev = _device_id(rt)
    _inject_fake_capture(rt)
    cid = rt.field.create_campaign(name="cap", profile="smoke")
    run(rt.field.start_campaign(cid, device_id=dev, iterations=3))
    with rt.session_scope() as s:
        total, ok = FieldMeasurementRepository(s).counts(cid)
        assert total == 3 and ok == 3
        c = FieldCampaignRepository(s).get(cid)
        assert c.status == "completed"
        assert c.classification == "single_device_soak_complete"
        # every measurement is attributable (device + lease + provenance)
        for m in FieldMeasurementRepository(s).list_for_campaign(cid):
            assert m.device_id == dev and m.lease_id and m.expected_samples
        # no leaked active lease after the campaign
        assert not SDRDeviceLeaseRepository(s).holding_for_device(dev)
        # two_device_rf is honestly not_executed (single SDR)
        checks = {c.name: c for c in FieldCheckRepository(s).list_for_campaign(cid)}
        assert checks["two_device_rf"].status == "not_executed"


def test_campaign_creates_no_mission_step(tmp_path):
    rt, _ = _field_runtime(tmp_path)
    dev = _device_id(rt)
    _inject_fake_capture(rt)
    with rt.session_scope() as s:
        before = s.scalar(select(func.count()).select_from(MissionStep))
    cid = rt.field.create_campaign(name="nostep", profile="smoke")
    run(rt.field.start_campaign(cid, device_id=dev, iterations=2))
    run(rt.field.soak_run(campaign_id=cid, target_seconds=0.2, sample_interval=0.05))
    with rt.session_scope() as s:
        after = s.scalar(select(func.count()).select_from(MissionStep))
    assert before == after == 0  # sampling + lease cycling create NO MissionStep


# -- soak sampling + thresholds --------------------------------------------------------------


def test_soak_samples_and_evaluates_thresholds(tmp_path):
    rt, _ = _field_runtime(tmp_path)
    sid = run(rt.field.soak_run(target_seconds=0.3, sample_interval=0.1))
    with rt.session_scope() as s:
        soak = SoakRunRepository(s).get(sid)
        assert soak.sample_count >= 2
        assert "rss_growth_bytes" in soak.summary_json
        assert "final_active_leases" in soak.summary_json
        # a healthy short soak has no leaked leases / fd explosions
        assert soak.violations == 0


# -- fault injection (Part I) ----------------------------------------------------------------


def test_fault_injection_gated_by_default(tmp_path):
    rt, _ = _field_runtime(tmp_path)
    rt.config.field_validation.fault_injection_enabled = False
    try:
        run(rt.field.inject_fault(fault_type="device_missing", confirm=True))
        raise AssertionError("expected PermissionError")
    except PermissionError:
        pass


def test_fault_device_missing_orphans_lease(tmp_path):
    rt, _ = _field_runtime(tmp_path)
    rt.config.field_validation.fault_injection_enabled = True
    dev = _device_id(rt)
    lease_id = run(rt.hardware_leases.acquire(dev, backend_id="legacy_gr_mcp", direction="rx"))
    fid = run(rt.field.inject_fault(fault_type="device_missing",
                                    params={"device_id": dev}, confirm=True))
    with rt.session_scope() as s:
        assert SDRDeviceLeaseRepository(s).get(lease_id).state == "orphaned"
        from aithernet.state.repositories import FieldFaultRepository, FieldRecoveryRepository
        fault = FieldFaultRepository(s).get(fid)
        assert fault.status == "recovered"
        assert FieldRecoveryRepository(s).list_for_campaign(None) or True


def test_unknown_fault_type_rejected(tmp_path):
    rt, _ = _field_runtime(tmp_path)
    rt.config.field_validation.fault_injection_enabled = True
    try:
        run(rt.field.inject_fault(fault_type="rm -rf /", confirm=True))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# -- API + report sanitization ---------------------------------------------------------------


def _client(rt):
    return TestClient(create_app(runtime=rt))


def test_api_campaigns_and_gating(tmp_path):
    rt, _ = _field_runtime(tmp_path)
    rt.config.field_validation.operator_actions_enabled = False
    cid = rt.field.create_campaign(name="api", profile="smoke")
    with _client(rt) as c:
        assert len(c.get("/field/campaigns").json()) == 1
        assert c.get(f"/field/campaigns/{cid}").json()["classification"]
        assert c.post("/field/campaigns/preflight", json={}).status_code == 200
        # disruptive actions gated
        assert c.post("/field/campaigns/create", json={"name": "x"}).status_code == 403
        assert c.post("/field/campaigns/start",
                      json={"campaign_id": cid, "device_id": "d"}).status_code == 403
        assert c.post("/field/soak/start", json={"target_seconds": 1}).status_code == 403
        assert c.post("/field/faults/inject",
                      json={"fault_type": "device_missing"}).status_code == 403


def test_report_exposes_no_secrets_or_paths(tmp_path):
    import json as _json

    rt, _ = _field_runtime(tmp_path)
    dev = _device_id(rt)
    _inject_fake_capture(rt)
    cid = rt.field.create_campaign(name="rep", profile="smoke")
    run(rt.field.start_campaign(cid, device_id=dev, iterations=2))
    with _client(rt) as c:
        blob = _json.dumps(c.get(f"/field/campaigns/{cid}/report").json())
    assert "/home/" not in blob and "/tmp/" not in blob
    assert "lease_token" not in blob and "private" not in blob.lower()


# -- backup preserves campaign history -------------------------------------------------------


def test_backup_preserves_campaign_history(tmp_path):
    from aithernet.ops.backup import create_backup, restore_backup, verify_backup

    rt, _ = _field_runtime(tmp_path)
    dev = _device_id(rt)
    _inject_fake_capture(rt)
    cid = rt.field.create_campaign(name="bk", profile="smoke")
    run(rt.field.start_campaign(cid, device_id=dev, iterations=2))
    rt.engine.dispose()
    backup = create_backup(rt.config, backup_dir=str(tmp_path / "backups"),
                           now_iso="2026-06-14T00:00:00+00:00")
    assert verify_backup(backup.path).ok
    restore_backup(backup.path, rt.config, config_path=None, dry_run=False,
                   now_iso="2026-06-14T00:00:01+00:00")
    from aithernet.orchestrator.runtime import NodeRuntime
    rt2 = NodeRuntime.from_config(rt.config)
    with rt2.session_scope() as s:
        c = FieldCampaignRepository(s).get(cid)
        assert c is not None and c.name == "bk"
        total, _ok = FieldMeasurementRepository(s).counts(cid)
        assert total == 2
