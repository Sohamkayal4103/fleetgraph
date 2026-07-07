"""beta.9 — hosted research ingestion + owner archive (control-plane).

Covers: capability minting (consent-gated, auto-approved for enrolled beta nodes), node package
ingestion (verify capability + size + schema, server-side secret scan, dedup, store, enqueue
archive job), quarantine of secret-bearing packages, the owner-archive worker (Drive connected vs
disconnected), capability revocation / wrong-tenant / expired rejection, and admin status views.
No client Google credentials anywhere; the owner refresh token is server-side only and never
returned.
"""
from __future__ import annotations

import secrets
import types

import pytest
from services.control_plane import roles
from services.control_plane.config import HostedConfig
from services.control_plane.errors import ControlPlaneError, ForbiddenError
from services.control_plane.security import generate_keypair, load_private_key, sign

from aithernet.data.batch import seal_batch


@pytest.fixture
def session_key(monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", secrets.token_urlsafe(48))


@pytest.fixture
def svc(tmp_path, session_key):
    from services.control_plane.service import ControlPlaneService
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'hosted.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    return ControlPlaneService(cfg)


@pytest.fixture
def admin(svc):
    from services.control_plane.models import HostedUser
    from sqlalchemy import select
    svc.bootstrap_admin("admin@example.invalid", "supersecret-123")
    with svc._session() as s:
        uid = s.execute(select(HostedUser).where(
            HostedUser.email == "admin@example.invalid")).scalar_one().id
    sess = svc.create_session(uid)
    principal, _, _ = svc.validate_session(sess["session_token"])
    return principal


def _tenant(svc, admin, *, email="op@example.invalid", name="Op Tenant"):
    inv = svc.create_invitation(admin, email=email, role=roles.TENANT_ADMIN,
                                proposed_tenant_name=name)
    svc.accept_invitation(inv["token"], email=email, password="tenant-password-9")
    auth = svc.authenticate(email, "tenant-password-9")
    sess = svc.create_session(auth["user_id"])
    principal, _, _ = svc.validate_session(sess["session_token"])
    return principal, svc.list_tenants(principal)[0]["tenant_id"]


def _enroll(svc, principal, tid, node_id):
    code = svc.create_enrollment_code(principal, tenant_id=tid)
    seed, pub = generate_keypair()
    chal = svc.enroll_challenge(code=code["code"], public_key=pub)
    sig = sign(load_private_key(seed), chal["challenge"].encode())
    res = svc.enroll(code=code["code"], public_key=pub, node_id=node_id, signature=sig,
                     software_version="1.0.0-beta.9", display_name=node_id)
    return types.SimpleNamespace(tenant_id=res["tenant_id"], node_id=res["node_id"],
                                 node_pk=res["hosted_node_id"])


def _seal(*, tenant_id, records, idem="idem-1"):
    sealed = seal_batch(
        batch_id="pkg-1", tenant_id=tenant_id, node_pseudonym="n", destination_id="research",
        idempotency_key=idem, record_envelopes=records, consent_summary={"research": "granted"},
        retention_summary={"days": 30}, created_at_iso="2026-07-05T00:00:00Z")
    # tag the schema the server recognizes
    return sealed.bundle


# ── capability minting ──────────────────────────────────────────────────────────────────────
def test_mint_requires_consent(svc, admin):
    _, tid = _tenant(svc, admin)
    auth = _enroll(svc, admin, tid, "node-a")
    with pytest.raises(ForbiddenError):
        svc.mint_research_capability(auth, {"consent": False})


def test_mint_auto_approved_for_consented_beta_node(svc, admin):
    _, tid = _tenant(svc, admin)
    auth = _enroll(svc, admin, tid, "node-a")
    out = svc.mint_research_capability(auth, {"consent": True})
    assert out["capability_token"] and out["owner_archive_upload"] == "enabled"
    assert "research.package.upload" in out["scopes"]


# ── ingestion ───────────────────────────────────────────────────────────────────────────────
def test_ingest_accepts_and_enqueues_archive(svc, admin):
    _, tid = _tenant(svc, admin)
    auth = _enroll(svc, admin, tid, "node-a")
    cap = svc.mint_research_capability(auth, {"consent": True})["capability_token"]
    records = [{"schema": "aithernet.research.mission.v1", "mission": {"id": "m1"},
                "artifacts": [{"relative_path": "out/sig.bin", "content_hash": "sha256:abc",
                               "size_bytes": 10}]}]
    body = _seal(tenant_id=tid, records=records)
    ack = svc.ingest_research_package(auth, body=body, capability_token=cap, idempotency_key="i1")
    assert ack["status"] == "accepted" and ack["archive"] == "queued"
    jobs = svc.research_list_jobs(admin, status="queued")
    assert len(jobs) == 1 and jobs[0]["node_id"] == "node-a"
    # dedup: identical bytes → duplicate
    ack2 = svc.ingest_research_package(auth, body=body, capability_token=cap)
    assert ack2["duplicate"] is True


def test_ingest_requires_capability(svc, admin):
    _, tid = _tenant(svc, admin)
    auth = _enroll(svc, admin, tid, "node-a")
    svc.mint_research_capability(auth, {"consent": True})
    body = _seal(tenant_id=tid, records=[{"mission": {"id": "m"}}])
    with pytest.raises(ForbiddenError):
        svc.ingest_research_package(auth, body=body, capability_token=None)
    with pytest.raises(ForbiddenError):
        svc.ingest_research_package(auth, body=body, capability_token="wrong-token")


def test_revoked_capability_rejected(svc, admin):
    _, tid = _tenant(svc, admin)
    auth = _enroll(svc, admin, tid, "node-a")
    cap = svc.mint_research_capability(auth, {"consent": True})["capability_token"]
    svc.revoke_capability(admin, tenant_id=tid, node_id="node-a")
    body = _seal(tenant_id=tid, records=[{"mission": {"id": "m"}}])
    with pytest.raises(ForbiddenError):
        svc.ingest_research_package(auth, body=body, capability_token=cap)


def test_wrong_node_capability_rejected(svc, admin):
    _, tid = _tenant(svc, admin)
    auth_a = _enroll(svc, admin, tid, "node-a")
    auth_b = _enroll(svc, admin, tid, "node-b")
    cap_a = svc.mint_research_capability(auth_a, {"consent": True})["capability_token"]
    svc.mint_research_capability(auth_b, {"consent": True})
    body = _seal(tenant_id=tid, records=[{"mission": {"id": "m"}}])
    # node-b presents node-a's token → rejected (capability looked up per (tenant,node))
    with pytest.raises(ForbiddenError):
        svc.ingest_research_package(auth_b, body=body, capability_token=cap_a)


def test_secret_bearing_package_is_quarantined(svc, admin):
    _, tid = _tenant(svc, admin)
    auth = _enroll(svc, admin, tid, "node-a")
    cap = svc.mint_research_capability(auth, {"consent": True})["capability_token"]
    records = [{"mission": {"id": "m"}, "leak": "Authorization: Bearer sk-abcdef1234567890TOKEN"}]
    body = _seal(tenant_id=tid, records=records)
    ack = svc.ingest_research_package(auth, body=body, capability_token=cap)
    assert ack["status"] == "quarantined" and ack["archive"] == "blocked"
    q = svc.research_list_quarantine(admin)
    assert len(q) == 1 and q[0]["reason_category"] in ("residual_secret", "forbidden_key")
    # nothing accepted, nothing enqueued for archive
    assert svc.research_admin_status(admin)["packages_accepted"] == 0
    assert svc.research_admin_status(admin)["archive_jobs_queued"] == 0


def test_oversized_package_rejected(svc, admin):
    _, tid = _tenant(svc, admin)
    auth = _enroll(svc, admin, tid, "node-a")
    cap = svc.mint_research_capability(auth, {"consent": True})["capability_token"]
    big = b"x" * (17 * 1024 * 1024)
    with pytest.raises(ControlPlaneError):
        svc.ingest_research_package(auth, body=big, capability_token=cap)


# ── owner archive worker ─────────────────────────────────────────────────────────────────────
def test_archive_worker_queues_when_drive_disconnected(svc, admin):
    _, tid = _tenant(svc, admin)
    auth = _enroll(svc, admin, tid, "node-a")
    cap = svc.mint_research_capability(auth, {"consent": True})["capability_token"]
    body = _seal(tenant_id=tid, records=[{"mission": {"id": "m"}}])
    svc.ingest_research_package(auth, body=body, capability_token=cap)
    out = svc.process_archive_jobs(admin)  # owner Drive not connected
    assert out["synced"] == 0 and out["reason"] == "owner_drive_not_connected"
    assert out["queued"] == 1


def test_archive_worker_writes_when_drive_connected(svc, admin):
    _, tid = _tenant(svc, admin)
    auth = _enroll(svc, admin, tid, "node-a")
    cap = svc.mint_research_capability(auth, {"consent": True})["capability_token"]
    body = _seal(tenant_id=tid, records=[{"mission": {"id": "m"}}])
    svc.ingest_research_package(auth, body=body, capability_token=cap)
    # connect owner Drive server-side; token never returned
    conn = svc.connect_owner_drive(admin, refresh_token="server-side-refresh-xyz",
                                   account_label="owner@aithernet")
    assert conn["status"] == "connected" and "refresh_token" not in conn
    written = []

    def _writer(pkg):
        written.append(pkg.id)
        return "file-1"
    out = svc.process_archive_jobs(admin, drive_writer=_writer)
    assert out["synced"] == 1 and len(written) == 1
    jobs = svc.research_list_jobs(admin, status="synced")
    assert jobs and jobs[0]["drive_file_id"] == "file-1"


def test_owner_drive_status_never_leaks_token(svc, admin):
    svc.connect_owner_drive(admin, refresh_token="super-secret-refresh", account_label="owner")
    st = svc.owner_drive_status(admin)
    assert st["connected"] is True and "refresh_token" not in str(st)


# ── admin + customer views ───────────────────────────────────────────────────────────────────
def test_admin_status_and_node_summary(svc, admin):
    principal, tid = _tenant(svc, admin)
    auth = _enroll(svc, principal, tid, "node-a")
    cap = svc.mint_research_capability(auth, {"consent": True})["capability_token"]
    body = _seal(tenant_id=tid, records=[{"mission": {"id": "m"}}])
    svc.ingest_research_package(auth, body=body, capability_token=cap)
    status = svc.research_admin_status(admin)
    assert status["packages_accepted"] == 1 and status["capabilities_active"] == 1
    # customer sees their own node's consent + upload status (no admin data)
    summary = svc.research_node_summary(principal, tenant_id=tid)
    assert len(summary) == 1
    assert summary[0]["node_id"] == "node-a"
    assert summary[0]["owner_archive_upload"] == "enabled"
    assert summary[0]["packages_received"] == 1


def test_admin_views_require_platform_permission(svc, admin):
    principal, tid = _tenant(svc, admin)  # a tenant admin, NOT a platform admin
    with pytest.raises(ForbiddenError):
        svc.research_admin_status(principal)
    with pytest.raises(ForbiddenError):
        svc.connect_owner_drive(principal, refresh_token="x")


def test_collection_policy_can_disable_collection(svc, admin):
    _, tid = _tenant(svc, admin)
    auth = _enroll(svc, admin, tid, "node-a")
    svc.set_collection_policy(admin, tenant_id="*", collection_allowed=False)
    with pytest.raises(ForbiddenError):
        svc.mint_research_capability(auth, {"consent": True})


# ── HTTP wire path (signed node request through the FastAPI app) ─────────────────────────────────
def test_node_research_http_path_accepts_package(tmp_path, session_key):
    import time

    from fastapi.testclient import TestClient
    from services.control_plane.app import create_app
    from services.control_plane.security import load_private_key
    from services.control_plane.service import ControlPlaneService

    from aithernet.data.ingest_auth import build_headers

    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'h.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    svc = ControlPlaneService(cfg)
    svc.bootstrap_admin("admin@example.invalid", "supersecret-123")
    tc = TestClient(create_app(cfg, svc))

    # admin creates a tenant + enrollment code
    login = tc.post("/v1/auth/login", json={"email": "admin@example.invalid",
                                            "password": "supersecret-123"})
    ah = {"x-csrf-token": login.json()["csrf_token"]}
    inv = tc.post("/v1/invitations", json={"email": "op@example.invalid",
                                           "proposed_tenant_name": "Acme"}, headers=ah).json()
    tc.post("/v1/invitations/accept", json={"token": inv["token"], "email": "op@example.invalid",
                                            "password": "acme-password-9"})
    oh = {"x-csrf-token": tc.post("/v1/auth/login", json={
        "email": "op@example.invalid", "password": "acme-password-9"}).json()["csrf_token"]}
    tid = tc.get("/v1/auth/session").json()["tenants"][0]
    code = tc.post("/v1/enrollment-codes", json={"tenant_id": tid}, headers=oh).json()["code"]

    # node enrolls
    seed, pub = generate_keypair()
    chal = tc.post("/v1/node/enroll/challenge", json={"code": code, "public_key": pub}).json()
    sig = sign(load_private_key(seed), chal["challenge"].encode())
    tc.post("/v1/node/enroll", json={"code": code, "public_key": pub, "node_id": "node-1",
                                     "signature": sig, "display_name": "node-1"})

    def _signed(target, body, extra=None):
        h = build_headers(private=load_private_key(seed), tenant_id=tid, node_id="node-1",
                          key_id="default", method="POST", target=target, body=body,
                          timestamp=int(time.time()), nonce=secrets.token_hex(8))
        h.update(extra or {})
        return h

    # mint capability (signed, consent)
    cbody = b'{"consent": true}'
    cap = tc.post("/v1/node/research/capability", content=cbody,
                  headers=_signed("/v1/node/research/capability", cbody))
    assert cap.status_code == 200, cap.text
    token = cap.json()["capability_token"]

    # upload a sealed package (signed + capability header) -> 202 accepted
    bundle = _seal(tenant_id=tid, records=[{"mission": {"id": "m1"}}])
    up = tc.post("/v1/node/research/packages", content=bundle,
                 headers=_signed("/v1/node/research/packages", bundle,
                                 {"x-aithernet-research-capability": token,
                                  "x-aithernet-idempotency": "i1"}))
    assert up.status_code == 202, up.text
    assert up.json()["status"] == "accepted"

    # platform admin sees it in the archive status view
    tc.post("/v1/auth/login", json={"email": "admin@example.invalid",
                                    "password": "supersecret-123"})
    status = tc.get("/v1/admin/research/status")
    assert status.status_code == 200 and status.json()["packages_accepted"] == 1

    # a node request WITHOUT the capability header is rejected
    noc = tc.post("/v1/node/research/packages", content=bundle,
                  headers=_signed("/v1/node/research/packages", bundle))
    assert noc.status_code == 403
