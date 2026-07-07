"""beta.9 servicing update — admin owner-Drive OAuth connect flow + steady-state archive worker.

The owner/admin connects Google Drive from the admin portal via a normal server-side OAuth consent
flow (no refresh-token pasting). Covers: OAuth start (admin-only, CSRF, state minting, missing
config), callback state validation + server-side code exchange (mocked Google), the refresh token
stored SERVER-SIDE only and never returned, richer status, and the automatic archive worker —
one owner Drive connection serving MANY nodes without per-node setup, plus the reconnect-required
degraded state when the token is revoked/expired. No client Google credentials anywhere.
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

REDIRECT = "https://admin.test/api/v1/admin/research/drive/oauth/callback"


@pytest.fixture
def session_key(monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", secrets.token_urlsafe(48))


@pytest.fixture
def oauth_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "server-side-oauth-secret-never-shown")


def _configure_oauth(cfg):
    cfg.google_oauth.client_id = "cid-123.apps.googleusercontent.com"
    cfg.google_oauth.redirect_uri = REDIRECT
    return cfg


@pytest.fixture
def svc(tmp_path, session_key, oauth_env):
    from services.control_plane.service import ControlPlaneService
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'hosted.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    _configure_oauth(cfg)
    return ControlPlaneService(cfg)


@pytest.fixture
def svc_no_oauth(tmp_path, session_key):
    from services.control_plane.service import ControlPlaneService
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'hosted.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    return ControlPlaneService(cfg)


@pytest.fixture
def admin(svc):
    return _admin_for(svc)


def _admin_for(svc):
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
    return seal_batch(
        batch_id="pkg", tenant_id=tenant_id, node_pseudonym="n", destination_id="research",
        idempotency_key=idem, record_envelopes=records, consent_summary={"research": "granted"},
        retention_summary={"days": 30}, created_at_iso="2026-07-05T00:00:00Z").bundle


def _ingest(svc, admin_or_op, tid, node_id, idem):
    auth = _enroll(svc, admin_or_op, tid, node_id)
    cap = svc.mint_research_capability(auth, {"consent": True})["capability_token"]
    body = _seal(tenant_id=tid, records=[{"mission": {"id": f"m-{node_id}"}}], idem=idem)
    svc.ingest_research_package(auth, body=body, capability_token=cap, idempotency_key=idem)
    return auth


# ── OAuth start ──────────────────────────────────────────────────────────────────────────────
def test_start_requires_admin(svc, admin):
    principal, _tid = _tenant(svc, admin)  # tenant admin, NOT a platform admin
    with pytest.raises(ForbiddenError):
        svc.start_owner_drive_oauth(principal)


def test_start_without_config_reports_not_configured(svc_no_oauth):
    admin = _admin_for(svc_no_oauth)
    with pytest.raises(ControlPlaneError) as ei:
        svc_no_oauth.start_owner_drive_oauth(admin)
    assert ei.value.code == "google_oauth_not_configured"


def test_start_mints_state_and_builds_authorize_url(svc, admin):
    from urllib.parse import parse_qs, urlparse

    from services.control_plane.models import HostedResearchOAuthState
    from sqlalchemy import select
    out = svc.start_owner_drive_oauth(admin, account_label="owner@example.com")
    url = out["authorize_url"]
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    assert parsed.netloc.endswith("google.com")
    assert q["client_id"] == ["cid-123.apps.googleusercontent.com"]
    assert q["redirect_uri"] == [REDIRECT]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["scope"] == ["https://www.googleapis.com/auth/drive.file"]
    state = q["state"][0]
    with svc._session() as s:
        row = s.execute(select(HostedResearchOAuthState).where(
            HostedResearchOAuthState.state == state)).scalar_one()
    assert row.user_id == admin.user_id and row.consumed is False
    # No secret is anywhere in the returned URL.
    assert "server-side-oauth-secret" not in url


# ── OAuth callback / code exchange (mocked Google) ─────────────────────────────────────────────
def _patch_google(monkeypatch, *, refresh="rt-owner-xyz", email="owner@example.com"):
    """Mock the server-side Google calls so nothing hits the network."""
    import services.control_plane.owner_drive_oauth as odo
    monkeypatch.setattr(odo, "exchange_code", lambda client, *, code, http=None: {
        "refresh_token": refresh, "access_token": "at-short-lived",
        "expires_in": 3000, "scope": client.scope})
    monkeypatch.setattr(odo, "fetch_account_email", lambda *a, **k: email)

    holder: dict = {}

    class FakeDrive:
        def __init__(self, oauth=None, **k):
            holder["drive"] = self
            self.uploads: list = []
            self.folders: list = []

        def find_or_create_folder(self, name, parent):
            fid = f"folder:{parent or 'ROOT'}/{name}"
            self.folders.append((name, parent))
            return fid

        def resumable_upload(self, *, folder_id, name, data, idempotency_key, chunk_bytes):
            self.uploads.append({"folder_id": folder_id, "name": name, "size": len(data),
                                 "idem": idempotency_key})
            return {"drive_file_id": f"file:{name}", "byte_size": len(data)}

    monkeypatch.setattr(
        "aithernet.data.destinations.google_drive_client.RealGoogleDriveClient", FakeDrive)
    return holder


def test_callback_exchanges_code_and_stores_token_server_side(svc, admin, monkeypatch):
    _patch_google(monkeypatch)
    out = svc.start_owner_drive_oauth(admin)
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(out["authorize_url"]).query)["state"][0]
    status = svc.complete_owner_drive_oauth(admin, code="auth-code-1", state=state)
    assert status["status"] == "connected" and status["connected"] is True
    assert status["account_email"] == "owner@example.com"
    assert status["connected_via"] == "oauth"
    # The refresh token is NEVER returned by any API surface.
    assert "rt-owner-xyz" not in str(status)
    st = svc.owner_drive_status(admin)
    assert "rt-owner-xyz" not in str(st) and "refresh_token" not in str(st)
    # It IS stored server-side.
    from services.control_plane.models import HostedOwnerArchiveConnection
    from sqlalchemy import select
    with svc._session() as s:
        row = s.execute(select(HostedOwnerArchiveConnection)).scalar_one()
    assert row.refresh_token == "rt-owner-xyz"


def test_callback_rejects_bad_state(svc, admin, monkeypatch):
    _patch_google(monkeypatch)
    with pytest.raises(ForbiddenError):
        svc.complete_owner_drive_oauth(admin, code="c", state="never-issued")


def test_callback_state_is_single_use(svc, admin, monkeypatch):
    _patch_google(monkeypatch)
    from urllib.parse import parse_qs, urlparse
    out = svc.start_owner_drive_oauth(admin)
    state = parse_qs(urlparse(out["authorize_url"]).query)["state"][0]
    svc.complete_owner_drive_oauth(admin, code="c1", state=state)
    with pytest.raises(ForbiddenError):
        svc.complete_owner_drive_oauth(admin, code="c2", state=state)  # replay rejected


def test_callback_rejects_state_from_a_different_admin(svc, admin, monkeypatch):
    _patch_google(monkeypatch)
    from urllib.parse import parse_qs, urlparse

    from services.control_plane.service import Principal
    out = svc.start_owner_drive_oauth(admin)
    state = parse_qs(urlparse(out["authorize_url"]).query)["state"][0]
    # a DIFFERENT platform admin passes the permission check but must not complete this admin's
    # state (login-CSRF defence): the state is bound to the admin who started it.
    other_admin = Principal(user_id="a-different-admin", email="other@x", is_platform_admin=True)
    with pytest.raises(ForbiddenError) as ei:
        svc.complete_owner_drive_oauth(other_admin, code="c", state=state)
    assert ei.value.code == "oauth_state_mismatch"


# ── status when not configured ─────────────────────────────────────────────────────────────────
def test_status_unconfigured_when_no_google_oauth(svc_no_oauth):
    admin = _admin_for(svc_no_oauth)
    st = svc_no_oauth.owner_drive_status(admin)
    assert st["oauth_configured"] is False
    assert st["status"] == "unconfigured" and st["connected"] is False


# ── steady-state archive worker: one connection, many nodes ────────────────────────────────────
def test_one_connection_archives_many_nodes_automatically(svc, admin, monkeypatch):
    holder = _patch_google(monkeypatch)
    op, tid = _tenant(svc, admin)
    _ingest(svc, op, tid, "node-a", "ia")
    _ingest(svc, op, tid, "node-b", "ib")
    # connect owner Drive ONCE via OAuth
    from urllib.parse import parse_qs, urlparse
    out = svc.start_owner_drive_oauth(admin)
    state = parse_qs(urlparse(out["authorize_url"]).query)["state"][0]
    svc.complete_owner_drive_oauth(admin, code="c", state=state)
    # the automatic worker syncs BOTH nodes' packages with NO per-node Drive setup
    result = svc.process_archive_jobs(admin)
    assert result["synced"] == 2 and result["failed"] == 0
    status = svc.research_admin_status(admin)
    assert status["archive_jobs_synced"] == 2 and status["archive_jobs_queued"] == 0
    drive = holder["drive"]
    # each package written under <root>/tenants/<tenant>/nodes/<node>/packages
    assert len(drive.uploads) == 2
    assert {n for (n, _p) in drive.folders} >= {"tenants", "nodes", "packages", "node-a", "node-b"}


def test_worker_leaves_jobs_queued_when_disconnected(svc, admin):
    op, tid = _tenant(svc, admin)
    _ingest(svc, op, tid, "node-a", "ia")
    out = svc.process_archive_jobs(admin)  # never connected
    assert out["synced"] == 0 and out["reason"] == "owner_drive_not_connected"
    assert out["queued"] == 1
    # client upload already succeeded — the package is accepted regardless
    assert svc.research_admin_status(admin)["packages_accepted"] == 1


def test_revoked_token_flags_reconnect_and_keeps_jobs_queued(svc, admin):
    op, tid = _tenant(svc, admin)
    _ingest(svc, op, tid, "node-a", "ia")
    svc.connect_owner_drive(admin, refresh_token="rt", account_label="owner")

    class _Expired(Exception):
        category = "expired_credentials"

    def _writer(_pkg):
        raise _Expired()

    out = svc.process_archive_jobs(admin, drive_writer=_writer)
    assert out["synced"] == 0 and out.get("reason") == "reconnect_required"
    st = svc.owner_drive_status(admin)
    assert st["status"] == "error" and st["error_category"] == "reconnect_required"
    # the job stays queued (retryable after reconnect) — not marked failed
    assert svc.research_admin_status(admin)["archive_jobs_queued"] == 1


def test_process_jobs_is_idempotent_after_sync(svc, admin, monkeypatch):
    _patch_google(monkeypatch)
    op, tid = _tenant(svc, admin)
    _ingest(svc, op, tid, "node-a", "ia")
    from urllib.parse import parse_qs, urlparse
    out = svc.start_owner_drive_oauth(admin)
    state = parse_qs(urlparse(out["authorize_url"]).query)["state"][0]
    svc.complete_owner_drive_oauth(admin, code="c", state=state)
    assert svc.process_archive_jobs(admin)["synced"] == 1
    # a second run has nothing queued — steady state, no duplicate work
    assert svc.process_archive_jobs(admin)["synced"] == 0


# ── HTTP wire: admin-only + CSRF + callback redirect ───────────────────────────────────────────
def test_http_admin_only_and_csrf_and_callback(tmp_path, session_key, oauth_env, monkeypatch):
    from fastapi.testclient import TestClient
    from services.control_plane.app import create_app
    from services.control_plane.service import ControlPlaneService

    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'h.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    _configure_oauth(cfg)
    cfg.urls.admin = "https://admin.test"
    svc = ControlPlaneService(cfg)
    svc.bootstrap_admin("admin@example.invalid", "supersecret-123")
    _patch_google(monkeypatch)
    tc = TestClient(create_app(cfg, svc))

    # a normal (tenant) customer must NOT reach the admin OAuth start
    a = tc.post("/v1/auth/login", json={"email": "admin@example.invalid",
                                        "password": "supersecret-123"}).json()
    ah = {"x-csrf-token": a["csrf_token"]}
    inv = tc.post("/v1/invitations", json={"email": "op@example.invalid",
                                           "proposed_tenant_name": "Acme"}, headers=ah).json()
    tc.post("/v1/invitations/accept", json={"token": inv["token"], "email": "op@example.invalid",
                                            "password": "acme-password-9"})
    op = tc.post("/v1/auth/login", json={"email": "op@example.invalid",
                                         "password": "acme-password-9"}).json()
    r = tc.post("/v1/admin/research/drive/oauth/start", json={},
                headers={"x-csrf-token": op["csrf_token"]})
    assert r.status_code == 403  # server-side authz, not route hiding

    # admin: missing CSRF header -> 403
    tc.post("/v1/auth/login", json={"email": "admin@example.invalid",
                                    "password": "supersecret-123"})
    assert tc.post("/v1/admin/research/drive/oauth/start", json={}).status_code == 403

    # admin with CSRF -> authorize url
    login = tc.post("/v1/auth/login", json={"email": "admin@example.invalid",
                                            "password": "supersecret-123"}).json()
    start = tc.post("/v1/admin/research/drive/oauth/start", json={},
                    headers={"x-csrf-token": login["csrf_token"]})
    assert start.status_code == 200
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(start.json()["authorize_url"]).query)["state"][0]

    # callback with a bad state -> 303 redirect back to the admin page with an error reason
    bad = tc.get("/v1/admin/research/drive/oauth/callback",
                 params={"code": "c", "state": "nope"}, follow_redirects=False)
    assert bad.status_code == 303 and "drive=error" in bad.headers["location"]
    assert bad.headers["location"].startswith("https://admin.test/admin/research")

    # callback with the real state -> connected redirect; token never in the response
    ok = tc.get("/v1/admin/research/drive/oauth/callback",
                params={"code": "auth-code", "state": state}, follow_redirects=False)
    assert ok.status_code == 303 and "drive=connected" in ok.headers["location"]
    assert "rt-owner-xyz" not in ok.text

    # status endpoint reflects connected + never leaks the token
    st = tc.get("/v1/admin/research/drive").json()
    assert st["status"] == "connected" and "refresh_token" not in st


def test_callback_redirect_derives_admin_origin_from_request(tmp_path, session_key, oauth_env,
                                                             monkeypatch):
    # When AITHERNET_ADMIN_BASE_URL is unset (config.urls.admin is the loopback default), the
    # callback derives the redirect target from the request's own admin Host — so it lands back on
    # the admin SPA in production without depending on that env var.
    from fastapi.testclient import TestClient
    from services.control_plane.app import create_app
    from services.control_plane.service import ControlPlaneService

    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'h.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    _configure_oauth(cfg)  # leaves cfg.urls.admin at the loopback default
    svc = ControlPlaneService(cfg)
    svc.bootstrap_admin("admin@example.invalid", "supersecret-123")
    _patch_google(monkeypatch)
    tc = TestClient(create_app(cfg, svc))
    tc.post("/v1/auth/login", json={"email": "admin@example.invalid", "password": "supersecret-123"})
    r = tc.get("/v1/admin/research/drive/oauth/callback",
               params={"code": "c", "state": "bad"},
               headers={"host": "admin.aithernet.online"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://admin.aithernet.online/admin/research")
