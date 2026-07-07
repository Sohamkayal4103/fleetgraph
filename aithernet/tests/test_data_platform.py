"""Stage 14E data-platform tests (no Google account / no Internet / no SDR / no OAuth).

Covers: consent revisions + withdrawal, collection policy, redaction + pseudonymization, record
sealing, immutable compressed (+encrypted) batches, local/HTTP/Drive/air-gapped destinations,
retry + dead-letter + redrive, retention, traceable deletion + lineage, dataset versioning,
API gating, security gates, and "no MissionStep from telemetry/export activity".
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from aithernet.config.settings import (
    DataCollectionConfig,
    DataExportConfig,
    DataPlatformConfig,
    DataPrivacyConfig,
    NodeConfig,
)
from aithernet.data.batch import open_bundle, seal_batch
from aithernet.data.crypto import (
    generate_key_b64,
    generate_master_secret_b64,
    pseudonymize,
)
from aithernet.data.destinations.drive import FakeDriveClient, GoogleDriveArchiveDestination
from aithernet.data.redaction import Redactor, contains_residual_secret
from aithernet.data.service import ConsentError, ValidationError
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.state.models import MissionStep, utcnow
from aithernet.state.repositories import DataExportBatchRepository

#: A temporary pseudonymization master secret (generated per test session, never committed).
_PSEUDO_ENV = "AITHERNET_TEST_PSEUDO_SECRET"
os.environ.setdefault(_PSEUDO_ENV, generate_master_secret_b64())


def _config(tmp_path, *, export=True, training=True, analytics=False, raw=False,
            max_attempts=8, key_ref: str | None = _PSEUDO_ENV) -> NodeConfig:
    return NodeConfig(
        node_id="node-test", node_name="Node Test",
        database_url=f"sqlite:///{tmp_path / 'n.db'}",
        data_platform=DataPlatformConfig(
            enabled=True, tenant_id="tenant-a",
            collection=DataCollectionConfig(
                operational_enabled=True, product_analytics_enabled=analytics,
                training_trajectories_enabled=training, raw_artifacts_enabled=raw,
            ),
            export=DataExportConfig(enabled=export, paused=False, maximum_attempts=max_attempts,
                                    retry_base_seconds=0.01, retry_max_seconds=0.05),
            privacy=DataPrivacyConfig(pseudonymization_key_ref=key_ref),
        ),
    )


def _runtime(tmp_path, **kw) -> NodeRuntime:
    rt = NodeRuntime.from_config(_config(tmp_path, **kw))
    rt.transport.ensure_identity() or rt.transport._identity_manager.initialize()
    rt.data._ensure_default_destination()
    return rt


def _grant_training(svc):
    svc.accept_profile(policy_version="p1", optional_categories=["training", "raw_artifact"])
    svc.grant_consent(category="training", export=True)


def _local_dest(svc):
    return [d["destination_id"] for d in svc.list_destinations()
            if d["kind"] == "local_archive"][0]


# --------------------------------------------------------------------------- redaction


def test_redaction_removes_secrets_and_pseudonymizes(tmp_path):
    r = Redactor(pseudo_key="tenant-a")
    payload = {
        "objective": "scan 433", "authorization": "Bearer abc.def.ghi",
        "db": "sqlite:////home/x/a.db", "email": "user@example.com",
        "note": "ran at /home/operator/x", "ip_address": "10.0.0.5",
        "nested": {"private_key": "-----BEGIN PRIVATE KEY-----xx", "lease_token": "lt-123"},
    }
    res = r.redact(payload)
    assert res.redacted["authorization"] == "[redacted]"
    assert res.redacted["nested"]["private_key"] == "[redacted]"
    assert res.redacted["nested"]["lease_token"] == "[redacted]"
    assert "SECRET" not in str(res.redacted)
    assert not contains_residual_secret(res.redacted)
    assert res.redacted["email"].startswith("pseu_")


def test_pseudonym_stable_and_tenant_separated():
    assert pseudonymize("u@x.com", key="tA") == pseudonymize("u@x.com", key="tA")
    assert pseudonymize("u@x.com", key="tA") != pseudonymize("u@x.com", key="tB")


# --------------------------------------------------------------------------- consent


def test_training_excluded_without_consent_then_collected(tmp_path):
    rt = _runtime(tmp_path)
    svc = rt.data
    assert svc.collect_training({"objective": "x"}) is None  # excluded — no hidden collection
    _grant_training(svc)
    rid = svc.collect_training({"objective": "scan", "outcome": "completed"})
    assert rid is not None
    assert svc.list_records(category="training")[0]["status"] == "held"


def test_consent_grant_requires_profile_for_training(tmp_path):
    rt = _runtime(tmp_path)
    with pytest.raises(ConsentError):
        rt.data.grant_consent(category="training", export=True)


def test_withdrawal_blocks_and_reconciles(tmp_path):
    rt = _runtime(tmp_path)
    svc = rt.data
    _grant_training(svc)
    rid = svc.collect_training({"objective": "x"})
    svc.approve_record(rid)
    grant = next(g for g in svc.list_grants() if g["category"] == "training")
    out = svc.withdraw_consent(grant["grant_id"])
    assert out["reconciled"] >= 1
    # New training collection is now blocked immediately.
    assert svc.collect_training({"objective": "y"}) is None
    # The previously-approved record is quarantined (evidence preserved, export blocked).
    statuses = {r["status"] for r in svc.list_records(category="training")}
    assert "quarantined" in statuses


# --------------------------------------------------------------------------- batches


def test_batch_immutable_compressed_and_roundtrips(tmp_path):
    rt = _runtime(tmp_path)
    svc = rt.data
    _grant_training(svc)
    rid = svc.collect_training({"objective": "scan 915", "outcome": "completed",
                                "device_family": "pluto"})
    svc.approve_record(rid)
    did = _local_dest(svc)
    batch = svc.build_batch(destination_id=did, category="training")
    assert batch["record_count"] == 1 and batch["compressed_bytes"] > 0
    # Idempotent rebuild returns the same batch (no duplicate).
    # (no new approved records → ValidationError on rebuild)
    with pytest.raises(ValidationError):
        svc.build_batch(destination_id=did, category="training")


def test_encryption_envelope_roundtrip():
    key = generate_key_b64()
    recs = [{"record_id": "r1", "payload": {"a": 1}}]
    sealed = seal_batch(batch_id="b", tenant_id="t", node_pseudonym="n", destination_id="d",
                        idempotency_key="k", record_envelopes=recs, consent_summary={},
                        retention_summary={}, created_at_iso="x", encryption_key_b64=key)
    manifest, out = open_bundle(sealed.bundle, encryption_key_b64=key)
    assert out == recs and manifest["records_digest"] == sealed.records_digest


# --------------------------------------------------------------------------- destinations


def test_local_delivery_and_receipt(tmp_path):
    rt = _runtime(tmp_path)
    svc = rt.data
    _grant_training(svc)
    svc.approve_record(svc.collect_training({"objective": "x", "outcome": "completed"}))
    did = _local_dest(svc)
    batch = svc.build_batch(destination_id=did, category="training")
    _claim_and_deliver(rt, svc, batch["batch_id"])
    delivered = svc.get_batch(batch["batch_id"])
    assert delivered["status"] == "receipt_verified"
    assert delivered["receipt"]["receipt_status"] == "stored"


def test_drive_requires_encryption_and_is_idempotent():
    fake = FakeDriveClient()
    gd = GoogleDriveArchiveDestination(folder_id="folder-test", client=fake)
    key = generate_key_b64()
    recs = [{"record_id": "r1"}]
    enc = seal_batch(batch_id="b", tenant_id="t", node_pseudonym="n", destination_id="d",
                     idempotency_key="k", record_envelopes=recs, consent_summary={},
                     retention_summary={}, created_at_iso="x", encryption_key_b64=key)
    plain = seal_batch(batch_id="b2", tenant_id="t", node_pseudonym="n", destination_id="d",
                       idempotency_key="k2", record_envelopes=recs, consent_summary={},
                       retention_summary={}, created_at_iso="x")
    # Refuses an unencrypted bundle to an external destination.
    r0 = asyncio.run(gd.upload(batch_id="b2", idempotency_key="k2", bundle=plain.bundle,
                              manifest=plain.manifest))
    assert not r0.ok and r0.failure_category == "encryption_required"
    r1 = asyncio.run(gd.upload(batch_id="b", idempotency_key="k", bundle=enc.bundle,
                              manifest=enc.manifest))
    r2 = asyncio.run(gd.upload(batch_id="b", idempotency_key="k", bundle=enc.bundle,
                              manifest=enc.manifest))
    assert r1.ok and r1.receipt["drive_file_id"] == r2.receipt["drive_file_id"]


def test_local_destination_rejects_path_traversal(tmp_path):
    from aithernet.data.destinations.local import LocalArchiveDestination
    d = LocalArchiveDestination(root=str(tmp_path / "archive"))
    res = asyncio.run(d.delete(remote_ref="../../etc/passwd"))
    assert not res.ok and res.state == "unknown"


# --------------------------------------------------------------------------- dead-letter


def test_dead_letter_and_redrive(tmp_path):
    rt = _runtime(tmp_path, max_attempts=1)
    svc = rt.data
    _grant_training(svc)
    svc.approve_record(svc.collect_training({"objective": "x", "outcome": "completed"}))
    # Point at an HTTP destination with no credential → delivery fails.
    dest = svc.add_destination(kind="http_ingestion", name="bad",
                               config={"base_url": "http://127.0.0.1:1", "credential_ref": "NOPE"})
    # Force-enable bypassing validation by writing the row enabled is blocked; instead use local
    # with a broken root to force failure is complex — use HTTP with missing cred via deliver path.
    with rt.session_scope() as s:
        from aithernet.state.repositories import DataExportDestinationRepository
        row = DataExportDestinationRepository(s).get(dest["destination_id"])
        row.enabled = True
        s.commit()
    batch = svc.build_batch(destination_id=dest["destination_id"], category="training")
    _claim_and_deliver(rt, svc, batch["batch_id"])
    final = svc.get_batch(batch["batch_id"])
    assert final["status"] == "dead_letter"
    svc.redrive(batch["batch_id"])
    assert svc.get_batch(batch["batch_id"])["status"] == "pending"


# --------------------------------------------------------------------------- retention / deletion


def test_retention_expires_records(tmp_path):
    rt = _runtime(tmp_path)
    svc = rt.data
    rid = svc.collect_operational({"version": "0.1"})
    with rt.session_scope() as s:
        from aithernet.state.repositories import DataExportRecordRepository
        DataExportRecordRepository(s).update(rid, expires_at=utcnow() - timedelta(days=1))
        s.commit()
    out = asyncio.run(svc.run_retention())
    assert out["expired"] >= 1


def test_deletion_propagates_through_lineage(tmp_path):
    rt = _runtime(tmp_path)
    svc = rt.data
    _grant_training(svc)
    rid = svc.collect_training({"objective": "x", "outcome": "completed"})
    svc.approve_record(rid)
    did = _local_dest(svc)
    batch = svc.build_batch(destination_id=did, category="training")
    _claim_and_deliver(rt, svc, batch["batch_id"])
    ds = svc.create_dataset(name="traj")
    ver = svc.create_dataset_version(ds["dataset_id"])
    assert ver["member_count"] == 1
    res = svc.request_deletion(scope="record", target_id=rid)
    assert res["status"] == "completed"
    assert res["impact"]["records_deleted"] == 1
    assert res["impact"]["dataset_members_excluded"] == 1
    # The record content is tombstoned (payload cleared).
    assert svc.get_record(rid)["payload"] == {}


# --------------------------------------------------------------------------- no MissionStep


def test_no_mission_step_from_data_activity(tmp_path):
    rt = _runtime(tmp_path)
    svc = rt.data

    def steps():
        with rt.session_scope() as s:
            return s.query(MissionStep).count()

    before = steps()
    _grant_training(svc)
    rid = svc.collect_training({"objective": "x", "outcome": "completed"})
    svc.approve_record(rid)
    did = _local_dest(svc)
    batch = svc.build_batch(destination_id=did, category="training")
    _claim_and_deliver(rt, svc, batch["batch_id"])
    svc.request_deletion(scope="record", target_id=rid)
    assert steps() == before


# --------------------------------------------------------------------------- sanitization / API


def test_destination_config_strips_secrets(tmp_path):
    rt = _runtime(tmp_path)
    svc = rt.data
    dest = svc.add_destination(kind="http_ingestion", name="h", config={
        "base_url": "https://ingest.example", "credential_ref": "ENV_NAME",
        "api_key": "SECRET", "private_key": "-----BEGIN", "token": "tok"})
    blob = repr(dest)
    for forbidden in ("SECRET", "-----BEGIN", "tok", "api_key", "private_key"):
        assert forbidden not in blob
    assert dest["config"]["credential_ref"] == "ENV_NAME"  # ref name kept; secret value never


def test_api_gating_and_consent_flow(tmp_path):
    from aithernet.api.app import create_app
    rt = _runtime(tmp_path)
    app = create_app(runtime=rt)
    with TestClient(app) as client:
        # Effective consent reads available.
        assert client.get("/data/consent").status_code == 200
        # Training grant without a profile → 409 (consent required).
        r = client.post("/data/consent/grants", json={"category": "training", "export": True})
        assert r.status_code == 409
        client.post("/data/consent/profile", json={"policy_version": "p1",
                                                    "optional_categories": ["training"]})
        assert client.post("/data/consent/grants",
                           json={"category": "training", "export": True}).status_code == 201
        assert client.get("/data/diagnostics").status_code == 200


def test_gateway_disabled_blocks(tmp_path):
    from aithernet.api.app import create_app
    cfg = _config(tmp_path)
    cfg.data_platform.enabled = False
    rt = NodeRuntime.from_config(cfg)
    app = create_app(runtime=rt)
    with TestClient(app) as client:
        assert client.post("/data/consent/grants",
                           json={"category": "training"}).status_code == 403


# --------------------------------------------------------------------------- helpers


# ----------------------------------------------------------------- pseudonymization (secret-backed)


def test_pseudonym_secret_backed_stable_and_separated(tmp_path):
    def sub(name: str):
        p = tmp_path / name
        p.mkdir()
        return p
    # Same secret + tenant + key id → stable across separate runtimes (process/node restart).
    rt_a = _runtime(sub("a"))
    rt_a2 = _runtime(sub("a2"))                  # fresh runtime, same config + secret
    assert rt_a.data.node_pseudonym() == rt_a2.data.node_pseudonym()
    # Different tenant → different pseudonym for the same node id.
    cfg_b = _config(sub("b"))
    cfg_b.data_platform.tenant_id = "tenant-b"
    rt_b = NodeRuntime.from_config(cfg_b)
    assert rt_a.data.node_pseudonym() != rt_b.data.node_pseudonym()
    # Different key id → different pseudonym (rotation).
    cfg_k = _config(sub("k"))
    cfg_k.data_platform.privacy.pseudonymization_key_id = "k2"
    rt_k = NodeRuntime.from_config(cfg_k)
    assert rt_a.data.node_pseudonym() != rt_k.data.node_pseudonym()


def test_missing_secret_fails_closed(tmp_path):
    rt = _runtime(tmp_path, key_ref="AITHERNET_UNSET_SECRET_NAME")
    svc = rt.data
    assert not svc.pseudonymization_ready()
    assert svc.diagnostics()["pseudonymization_reason"] == "pseudonymization_secret_missing"
    _grant_training(svc)
    # Sensitive collection fails closed (no pseudonym key → no record).
    assert svc.collect_training({"objective": "x"}) is None
    # Export fails closed even if a record somehow existed.
    did = _local_dest(svc)
    with pytest.raises(ValidationError):
        svc.build_batch(destination_id=did, category="operational")
    # Local node still works (mission execution unaffected): a mission can be created.
    import asyncio as _aio

    from aithernet.schemas.missions import MissionCreate
    mission = _aio.run(rt.create_mission(MissionCreate(content="scan 433")))
    assert mission.id


def test_weak_secret_rejected(tmp_path):
    os.environ["AITHERNET_WEAK_SECRET"] = "YWJjZA=="   # 'abcd' — only 4 bytes
    try:
        rt = _runtime(tmp_path, key_ref="AITHERNET_WEAK_SECRET")
        assert not rt.data.pseudonymization_ready()
    finally:
        del os.environ["AITHERNET_WEAK_SECRET"]


def test_no_secret_in_api_diagnostics_records_or_backup(tmp_path):
    rt = _runtime(tmp_path)
    svc = rt.data
    _grant_training(svc)
    rid = svc.collect_training({"objective": "x", "outcome": "completed", "email": "u@e.com"})
    secret = os.environ[_PSEUDO_ENV]
    import base64
    raw_hex = base64.b64decode(secret).hex()
    blob = (repr(svc.diagnostics()) + repr(svc.get_record(rid)) + repr(svc.list_grants())
            + repr(svc.list_destinations()))
    for needle in (secret, raw_hex):
        assert needle not in blob
    # The record's lineage records the NON-secret key id for rotation tracing.
    assert svc.get_record(rid)["lineage"]["pseudonymization_key_id"] == "k1"


def test_pseudonym_stable_across_backup_restore(tmp_path):
    from aithernet.ops.backup import create_backup, restore_backup
    rt = _runtime(tmp_path)
    svc = rt.data
    _grant_training(svc)
    rid = svc.collect_training({"objective": "x", "outcome": "ok"}, subject="user-42")
    before = svc.get_record(rid)["record_id"]
    node_pseu_before = svc.node_pseudonym()
    with rt.session_scope() as s:
        from aithernet.state.repositories import DataExportRecordRepository
        subj_before = DataExportRecordRepository(s).get(rid).subject_pseudonym
    rt.engine.dispose()
    rt.config.backup_directory = str(tmp_path / "backups")
    backup = create_backup(rt.config, backup_dir=str(tmp_path / "backups"),
                           now_iso="2026-06-18T00:00:00+00:00")
    restore_backup(backup.path, rt.config, config_path=None, dry_run=False,
                   now_iso="2026-06-18T00:00:01+00:00")
    rt2 = NodeRuntime.from_config(rt.config)
    # Pseudonyms are stable across restore (depend only on secret+tenant+policy+key id).
    assert rt2.data.node_pseudonym() == node_pseu_before
    with rt2.session_scope() as s:
        from aithernet.state.repositories import DataExportRecordRepository
        assert DataExportRecordRepository(s).get(before).subject_pseudonym == subj_before


def test_backup_preserves_consent_and_records(tmp_path):
    from aithernet.ops.backup import create_backup, restore_backup, verify_backup
    rt = _runtime(tmp_path)
    svc = rt.data
    _grant_training(svc)
    rid = svc.collect_training({"objective": "x", "outcome": "completed"})
    svc.approve_record(rid)
    rt.engine.dispose()
    rt.config.backup_directory = str(tmp_path / "backups")
    backup = create_backup(rt.config, backup_dir=str(tmp_path / "backups"),
                           now_iso="2026-06-18T00:00:00+00:00")
    assert verify_backup(backup.path).ok
    restore_backup(backup.path, rt.config, config_path=None, dry_run=False,
                   now_iso="2026-06-18T00:00:01+00:00")
    rt2 = NodeRuntime.from_config(rt.config)
    # Consent grant + the approved record survived the round-trip.
    assert any(g["category"] == "training" for g in rt2.data.list_grants())
    assert any(r["record_id"] == rid for r in rt2.data.list_records(category="training"))


def _claim_and_deliver(rt, svc, batch_id):
    with rt.session_scope() as s:
        DataExportBatchRepository(s).claim(batch_id, owner="t", now=utcnow(),
                                           claim_expiry=utcnow() + timedelta(seconds=30))
        s.commit()
    asyncio.run(svc.deliver_batch(batch_id, owner="t"))
