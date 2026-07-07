"""Stage 14F control-plane HTTP API tests (FastAPI TestClient).

Exercises the web boundary: secure-cookie login, CSRF enforcement on state-changing requests,
invitation acceptance, signed node enrollment + heartbeat + replay rejection, fleet visibility,
download path-traversal + revoked-release handling, and platform-admin authorization.
"""

from __future__ import annotations

import json
import secrets
import time

import pytest
from services.control_plane.app import create_app
from services.control_plane.config import HostedConfig
from services.control_plane.security import generate_keypair, load_private_key, sign

from aithernet.data.ingest_auth import build_headers


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from services.control_plane.service import ControlPlaneService

    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", secrets.token_urlsafe(48))
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'hosted.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    svc = ControlPlaneService(cfg)
    svc.bootstrap_admin("admin@example.invalid", "supersecret-123")
    return TestClient(create_app(cfg, svc))


def _login(client, email, password):
    r = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"x-csrf-token": r.json()["csrf_token"]}


def test_health_ready(client):
    assert client.get("/health/ready").json()["status"] == "ready"


def test_login_csrf_and_invitation_accept(client):
    h = _login(client, "admin@example.invalid", "supersecret-123")
    # CSRF missing -> 403
    assert client.post("/v1/invitations",
                       json={"email": "a@example.invalid", "proposed_tenant_name": "Acme"}
                       ).status_code == 403
    inv = client.post("/v1/invitations",
                      json={"email": "alice@example.invalid", "proposed_tenant_name": "Acme"},
                      headers=h)
    assert inv.status_code == 200
    acc = client.post("/v1/invitations/accept",
                      json={"token": inv.json()["token"], "email": "alice@example.invalid",
                            "password": "acme-password-9"})
    assert acc.status_code == 200 and acc.json()["tenant_id"]


def test_enrollment_heartbeat_replay_and_fleet(client):
    _login(client, "admin@example.invalid", "supersecret-123")
    inv = client.post("/v1/invitations",
                      json={"email": "alice@example.invalid", "proposed_tenant_name": "Acme"},
                      headers=_login(client, "admin@example.invalid", "supersecret-123"))
    token = inv.json()["token"]
    client.post("/v1/invitations/accept",
                json={"token": token, "email": "alice@example.invalid",
                      "password": "acme-password-9"})
    ah = _login(client, "alice@example.invalid", "acme-password-9")
    tid = client.get("/v1/auth/session").json()["tenants"][0]
    code = client.post("/v1/enrollment-codes", json={"tenant_id": tid}, headers=ah).json()["code"]
    seed, pub = generate_keypair()
    chal = client.post("/v1/node/enroll/challenge",
                       json={"code": code, "public_key": pub}).json()
    sig = sign(load_private_key(seed), chal["challenge"].encode())
    en = client.post("/v1/node/enroll",
                     json={"code": code, "public_key": pub, "node_id": "node-1",
                           "signature": sig, "display_name": "node-1",
                           "software_version": "0.8.0-beta.1"})
    assert en.status_code == 200
    body = json.dumps({"sequence": 1, "software_version": "0.8.0-beta.1"}).encode()
    headers = build_headers(private=load_private_key(seed), tenant_id=tid, node_id="node-1",
                            key_id="default", method="POST", target="/v1/node/heartbeat",
                            body=body, timestamp=int(time.time()), nonce="n1")
    assert client.post("/v1/node/heartbeat", content=body, headers=headers).json()["accepted"]
    # Replay rejected.
    assert client.post("/v1/node/heartbeat", content=body, headers=headers).status_code == 401
    fleet = client.get("/v1/fleet/nodes", params={"tenant_id": tid})
    assert fleet.status_code == 200 and fleet.json()[0]["state"] == "online"


def test_download_traversal_and_revoked(client):
    h = _login(client, "admin@example.invalid", "supersecret-123")
    rel = client.post("/v1/releases", json={"version": "0.8.0-beta.1"}, headers=h).json()
    rid = rel["release_id"]
    client.post(f"/v1/releases/{rid}/artifacts", content=b"WHEEL",
                headers={**h, "x-artifact-name": "a.whl"})
    rseed, _ = generate_keypair()
    client.post(f"/v1/releases/{rid}/sign", json={"signing_seed_b64": rseed}, headers=h)
    client.post(f"/v1/releases/{rid}/publish", json={}, headers=h)
    # Package artifacts are NOT public anymore — the authenticated customer route serves them.
    assert client.get(f"/v1/portal/downloads/{rid}/a.whl").content == b"WHEEL"
    # The old public route refuses package artifacts (no hidden unauthenticated URLs).
    assert client.get(f"/v1/downloads/{rid}/a.whl").status_code == 403
    # Path traversal in the artifact name is rejected (no escape, 4xx).
    assert client.get(f"/v1/portal/downloads/{rid}/..%2f..%2fetc%2fpasswd").status_code >= 400
    client.post(f"/v1/releases/{rid}/revoke", json={}, headers=h)
    assert client.get(f"/v1/portal/downloads/{rid}/a.whl").status_code >= 400


def test_platform_admin_authorization(client):
    # Anonymous cannot read audit.
    assert client.get("/v1/admin/audit").status_code == 401
    _login(client, "admin@example.invalid", "supersecret-123")
    assert client.get("/v1/admin/audit").status_code == 200
