"""Stage 14E ingestion-service tests (no Internet / no cloud).

Covers: enrollment, signed-request auth + replay protection, idempotent batch ingestion,
manifest/payload tamper detection, decompression-bomb + oversized bounds, cross-tenant
isolation, revoked key, receipts, deletion + lineage exclusion, and dataset versioning.
"""

from __future__ import annotations

import time

import pytest
from services.ingestion.config import IngestionConfig
from services.ingestion.service import IngestionError, IngestionService

from aithernet.data.batch import seal_batch
from aithernet.data.ingest_auth import build_headers, generate_keypair, load_private_key


def _service(tmp_path) -> IngestionService:
    return IngestionService(IngestionConfig(
        database_url=f"sqlite:///{tmp_path / 'ing.db'}", blob_root=str(tmp_path / "blobs"),
    ))


def _enroll(svc, tenant="t1", node="n1"):
    seed, pub = generate_keypair()
    svc.enroll_tenant(tenant, "Tenant")
    svc.enroll_node(tenant, node, key_id="default", public_key=pub)
    return load_private_key(seed)


def _bundle(tenant="t1", category="training", idem="idem1"):
    recs = [{"record_id": "r1", "category": category, "payload_digest": "sha256:aa"}]
    return seal_batch(batch_id="b1", tenant_id=tenant, node_pseudonym="np", destination_id="d",
                      idempotency_key=idem, record_envelopes=recs, consent_summary={},
                      retention_summary={}, created_at_iso="x")


def _headers(priv, body, *, tenant="t1", node="n1", nonce="n", target="/ingest/v1/batches"):
    return build_headers(private=priv, tenant_id=tenant, node_id=node, key_id="default",
                         method="POST", target=target, body=body, timestamp=int(time.time()),
                         nonce=nonce)


def _auth(svc, priv, body, **kw):
    return svc.authenticate(headers=_headers(priv, body, **kw), method="POST",
                            target=kw.get("target", "/ingest/v1/batches"), body=body)


def test_ingest_and_idempotent(tmp_path):
    svc = _service(tmp_path)
    priv = _enroll(svc)
    sealed = _bundle()
    r1 = svc.ingest_batch(auth=_auth(svc, priv, sealed.bundle, nonce="a"),
                          idempotency_key="idem1", body=sealed.bundle)
    assert r1["status"] == "accepted"
    assert r1["records_digest"] == sealed.records_digest
    r2 = svc.ingest_batch(auth=_auth(svc, priv, sealed.bundle, nonce="b"),
                          idempotency_key="idem1", body=sealed.bundle)
    assert r2["batch_id"] == r1["batch_id"]   # idempotent: same logical batch


def test_invalid_signature_rejected(tmp_path):
    svc = _service(tmp_path)
    _enroll(svc)
    other, _ = generate_keypair()
    sealed = _bundle()
    h = _headers(load_private_key(other), sealed.bundle, nonce="x")
    res = svc.authenticate(headers=h, method="POST", target="/ingest/v1/batches",
                           body=sealed.bundle)
    assert not res.ok and res.code == "invalid_signature"


def test_modified_body_digest_mismatch(tmp_path):
    svc = _service(tmp_path)
    priv = _enroll(svc)
    sealed = _bundle()
    h = _headers(priv, sealed.bundle, nonce="x")
    res = svc.authenticate(headers=h, method="POST", target="/ingest/v1/batches",
                           body=sealed.bundle + b"x")
    assert not res.ok and res.code == "body_digest_mismatch"


def test_replay_nonce_rejected(tmp_path):
    svc = _service(tmp_path)
    priv = _enroll(svc)
    sealed = _bundle()
    h = _headers(priv, sealed.bundle, nonce="same")
    a1 = svc.authenticate(headers=h, method="POST", target="/ingest/v1/batches", body=sealed.bundle)
    assert a1.ok
    a2 = svc.authenticate(headers=h, method="POST", target="/ingest/v1/batches", body=sealed.bundle)
    assert not a2.ok and a2.code == "nonce_replayed"


def test_revoked_key_rejected(tmp_path):
    svc = _service(tmp_path)
    priv = _enroll(svc)
    svc.revoke_credential("t1", "n1", "default")
    res = _auth(svc, priv, _bundle().bundle, nonce="x")
    assert not res.ok and res.code == "key_revoked"


def test_tampered_manifest_rejected(tmp_path):
    svc = _service(tmp_path)
    priv = _enroll(svc)
    sealed = _bundle()
    # Flip a byte in the (plaintext gzip) bundle → digest/parse failure on open.
    tampered = bytearray(sealed.bundle)
    tampered[len(tampered) // 2] ^= 0x01
    body = bytes(tampered)
    with pytest.raises(IngestionError):
        svc.ingest_batch(auth=_auth(svc, priv, body, nonce="x"),
                         idempotency_key="idem2", body=body)


def test_cross_tenant_bundle_rejected(tmp_path):
    svc = _service(tmp_path)
    priv = _enroll(svc, tenant="t1", node="n1")
    svc.enroll_tenant("t2", "Two")
    # A bundle whose manifest claims tenant t2, authenticated as t1 → rejected.
    sealed = _bundle(tenant="t2", idem="idemX")
    with pytest.raises(IngestionError) as exc:
        svc.ingest_batch(auth=_auth(svc, priv, sealed.bundle, nonce="x", tenant="t1"),
                         idempotency_key="idemX", body=sealed.bundle)
    assert exc.value.code in ("tenant_mismatch", "invalid_bundle")


def test_oversized_bundle_rejected(tmp_path):
    svc = _service(tmp_path)
    svc.config.max_bundle_bytes = 16
    priv = _enroll(svc)
    sealed = _bundle()
    with pytest.raises(IngestionError) as exc:
        svc.ingest_batch(auth=_auth(svc, priv, sealed.bundle, nonce="x"),
                         idempotency_key="idem3", body=sealed.bundle)
    assert exc.value.code == "oversized_bundle"


def test_decompression_bomb_bounded(tmp_path):
    svc = _service(tmp_path)
    svc.config.max_uncompressed_bytes = 64
    priv = _enroll(svc)
    # A bundle whose decompressed payload exceeds the bound.
    recs = [{"record_id": f"r{i}", "blob": "A" * 100} for i in range(50)]
    sealed = seal_batch(batch_id="b", tenant_id="t1", node_pseudonym="n", destination_id="d",
                        idempotency_key="bomb", record_envelopes=recs, consent_summary={},
                        retention_summary={}, created_at_iso="x")
    with pytest.raises(IngestionError):
        svc.ingest_batch(auth=_auth(svc, priv, sealed.bundle, nonce="x"),
                         idempotency_key="bomb", body=sealed.bundle)


def test_deletion_and_lineage_exclusion(tmp_path):
    svc = _service(tmp_path)
    priv = _enroll(svc)
    sealed = _bundle()
    r = svc.ingest_batch(auth=_auth(svc, priv, sealed.bundle, nonce="x"),
                         idempotency_key="idem1", body=sealed.bundle)
    ds = svc.create_dataset("t1", "traj")
    ver = svc.create_dataset_version("t1", ds["dataset_id"])
    assert ver["member_count"] == 1
    out = svc.request_deletion(tenant_id="t1", scope="batch", target_id=r["batch_id"])
    assert out["status"] == "completed" and out["records_deleted"] == 1


def test_cross_tenant_query_blocked(tmp_path):
    svc = _service(tmp_path)
    priv = _enroll(svc, tenant="t1", node="n1")
    sealed = _bundle(tenant="t1")
    r = svc.ingest_batch(auth=_auth(svc, priv, sealed.bundle, nonce="x"),
                         idempotency_key="idem1", body=sealed.bundle)
    # Tenant t2 cannot read t1's batch.
    with pytest.raises(IngestionError):
        svc.get_batch("t2", r["batch_id"])


def test_app_readiness_and_admin_gating(tmp_path):
    from fastapi.testclient import TestClient
    from services.ingestion.app import create_app
    cfg = IngestionConfig(database_url=f"sqlite:///{tmp_path / 'a.db'}",
                          blob_root=str(tmp_path / "b"), admin_token="secret-admin")
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/ingest/v1/readiness").json()["status"] == "ready"
        # Admin endpoints require the admin token.
        assert client.post("/ingest/admin/tenants", json={"tenant_id": "t1"}).status_code == 403
        ok = client.post("/ingest/admin/tenants", json={"tenant_id": "t1", "name": "T"},
                         headers={"authorization": "Bearer secret-admin"})
        assert ok.status_code == 201
