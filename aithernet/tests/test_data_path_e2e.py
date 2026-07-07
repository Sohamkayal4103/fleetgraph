"""LOCAL data-path fixture proof (Stage 14G, PHASE 8 #6 / corrected COMMIT 4).

This proves ONLY the local legs (B/F/H/K) with NON-SENSITIVE fixtures built from the CANONICAL
training-trajectory schemas (aithernet.data.trajectories):

    canonical trajectory records
    → immutable sealed + encrypted batch (dataset version)
    → durable destination spool (LocalArchiveDestination — the operator-side archive stand-in)
    → byte/digest verification
    → isolated restore (open the bundle fresh; records + manifest digest verified)

It does NOT cross the authenticated hosted ingestion, real PostgreSQL/MinIO, or real Google Drive
boundaries — see docs/data-pipeline-status.md for the honest per-layer (A–K) status. It also asserts
the node↔operator-Drive separation: a customer node carries no operator Drive OAuth credentials and
never uploads directly to Drive (Drive destination disabled by default; credentials by env-ref).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from aithernet.data import batch, crypto, trajectories
from aithernet.data.destinations.local import LocalArchiveDestination


def _mission_records(tmp_path) -> list[dict]:
    """Build export record envelopes from the CANONICAL training-trajectory schemas
    (aithernet.data.trajectories) — the canonical event/lineage source, not a duplicate engine."""
    records = [
        trajectories.mission_trajectory(
            objective="sim survey", chosen_route="rf_device",
            action={"target": "rf_device", "action": "acquire_lease", "direction": "rx"},
            result={"status": "completed"}, outcome="completed",
            artifact_metadata=[{"name": "capture.cf32", "sha256": "0" * 64, "size": 512}]),
        trajectories.coordinator_decision(
            objective="sim survey", route_target="rf_device", action="acquire_lease",
            structured_action={"direction": "rx", "frequency_hz": 2.437e9}),
        trajectories.outcome(mission_outcome="completed", success=True),
    ]
    return records


def _seal(records, key):
    return batch.seal_batch(
        batch_id="batch-1", tenant_id="tenant-sim", node_pseudonym="node-sim",
        destination_id="local-archive", idempotency_key="idem-1",
        record_envelopes=records, consent_summary={"operational": True},
        retention_summary={"days": 30}, created_at_iso="2026-01-01T00:00:00Z",
        encryption_key_b64=key)


def test_full_data_path_seal_spool_verify_restore(tmp_path):
    records = _mission_records(tmp_path)
    assert records and records[0]["kind"] == "mission_trajectory"
    key = crypto.generate_key_b64()

    # 1) immutable sealed batch (encrypted) — the dataset version
    sealed = _seal(records, key)
    assert sealed.bundle and sealed.manifest_digest
    assert sealed.manifest["record_count"] == len(records)
    assert batch.verify_manifest_digest(sealed.manifest, sealed.manifest_digest)

    # 2) durable destination spool (operator-side archive stand-in; no real OAuth)
    dest = LocalArchiveDestination(root=str(tmp_path / "archive"))
    result = asyncio.run(dest.upload(batch_id="batch-1", idempotency_key="idem-1",
                                     bundle=sealed.bundle, manifest=sealed.manifest))
    assert result.ok, result.detail

    # 3) byte/digest verification: the spooled bytes match what we sealed
    spooled = next((tmp_path / "archive").rglob("*.bundle"))
    assert spooled.read_bytes() == sealed.bundle

    # 4) isolated restore — open the bundle fresh with only the key; records + digest verified
    manifest, restored = batch.open_bundle(spooled.read_bytes(), encryption_key_b64=key)
    assert restored == records
    assert manifest["records_digest"] == sealed.records_digest
    assert batch.verify_manifest_digest(manifest, sealed.manifest_digest)


def test_bundle_is_encrypted_and_tamper_fails_closed(tmp_path):
    key = crypto.generate_key_b64()
    sealed = _seal(_mission_records(tmp_path), key)
    # encrypted: the plaintext objective must not appear in the bundle bytes
    assert b"sim survey" not in sealed.bundle
    # wrong key fails closed
    with pytest.raises(batch.BatchError):
        batch.open_bundle(sealed.bundle, encryption_key_b64=crypto.generate_key_b64())
    # tampered ciphertext fails closed
    obj = json.loads(sealed.bundle)
    obj["ciphertext_b64"] = obj["ciphertext_b64"][:-4] + "AAAA"
    with pytest.raises(batch.BatchError):
        batch.open_bundle(json.dumps(obj).encode(), encryption_key_b64=key)


def test_node_does_not_carry_operator_drive_credentials():
    from aithernet.config.settings import DataDestinationsConfig
    dests = DataDestinationsConfig()
    # a node node uploads to the hosted ingestion, NOT directly to Drive:
    assert dests.google_drive.enabled is False
    # credentials are referenced by env-var NAME only — never stored inline in node config
    assert dests.google_drive.oauth_credential_ref is None
    assert dests.google_drive.encryption_key_ref is None


def test_idempotent_spool_upload(tmp_path):
    key = crypto.generate_key_b64()
    sealed = _seal(_mission_records(tmp_path), key)
    dest = LocalArchiveDestination(root=str(tmp_path / "archive"))
    r1 = asyncio.run(dest.upload(batch_id="b", idempotency_key="k", bundle=sealed.bundle,
                                 manifest=sealed.manifest))
    r2 = asyncio.run(dest.upload(batch_id="b", idempotency_key="k", bundle=sealed.bundle,
                                 manifest=sealed.manifest))
    assert r1.ok and r2.ok
    # content-addressed: the same bundle lands once
    assert len(list((tmp_path / "archive").rglob("*.bundle"))) == 1
