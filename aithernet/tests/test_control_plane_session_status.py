"""Cross-surface session continuity: the public-safe /v1/auth/session-status endpoint + CORS.

The marketing site is now SAME-ORIGIN with the portal + API under www.aithernet.online, so its
account-aware header reads this endpoint with a relative same-origin fetch. It must: return 200 for
both authenticated and anonymous callers, never leak tokens/session ids/internal claims, and never
mutate the session. Narrow credentialed CORS for the www origin is retained (never a
wildcard-with-credentials) as belt-and-suspenders for the migration/rollback window.
"""

from __future__ import annotations

import secrets

import pytest
from services.control_plane.app import create_app
from services.control_plane.config import HostedConfig


@pytest.fixture
def env(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from services.control_plane.service import ControlPlaneService

    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", secrets.token_urlsafe(48))
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'hosted.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    cfg.urls.public_site = "https://www.aithernet.online"
    cfg.urls.portal = "https://www.aithernet.online"
    svc = ControlPlaneService(cfg)
    svc.bootstrap_admin("admin@example.invalid", "supersecret-123")
    return cfg, svc, TestClient(create_app(cfg, svc))


def _login(client):
    r = client.post("/v1/auth/login", json={"email": "admin@example.invalid",
                                             "password": "supersecret-123"})
    assert r.status_code == 200, r.text
    return r


# -- shape / safety --------------------------------------------------------------------------


def test_anonymous_status_is_200_and_unauthenticated(env):
    _, _, client = env
    r = client.get("/v1/auth/session-status")
    assert r.status_code == 200
    body = r.json()
    assert body == {"authenticated": False, "portal_url": "https://www.aithernet.online"}


def test_authenticated_status_returns_only_safe_account_state(env):
    _, _, client = env
    _login(client)  # TestClient keeps the session cookie
    r = client.get("/v1/auth/session-status")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["portal_url"] == "https://www.aithernet.online"
    assert set(body["account"]) == {"display_name", "email"}
    assert body["account"]["email"] == "admin@example.invalid"


def test_status_never_leaks_tokens_or_session_ids(env):
    _, _, client = env
    _login(client)
    raw = client.get("/v1/auth/session-status").text.lower()
    for forbidden in ("csrf", "token", "session_id", "session_token", "is_platform_admin",
                      "tenant", "secret", "password", "refresh"):
        assert forbidden not in raw, f"session-status leaked '{forbidden}'"


def test_status_is_read_only_and_not_cached(env):
    _, _, client = env
    login = _login(client)
    session_cookie = login.cookies.get("aithernet_hosted_session")
    r = client.get("/v1/auth/session-status")
    # The status check sets no new/refreshed session cookie...
    assert "aithernet_hosted_session" not in r.cookies
    # ...and is explicitly non-cacheable (so a CDN never serves one user's state to another).
    assert r.headers.get("cache-control") == "no-store"
    # The original session still works afterwards (not invalidated by the read).
    assert client.get("/v1/auth/session").status_code == 200
    assert session_cookie  # sanity: a session cookie was issued at login


def test_expired_or_revoked_session_reads_as_anonymous(env):
    cfg, svc, client = env
    _login(client)
    assert client.get("/v1/auth/session-status").json()["authenticated"] is True
    # Revoke every session for the admin, then the status must read anonymous.
    from services.control_plane.models import HostedSession, HostedUser
    from sqlalchemy import select
    with svc._session() as s:
        uid = s.execute(select(HostedUser.id)).scalars().first()
        for row in s.execute(select(HostedSession).where(HostedSession.user_id == uid)).scalars():
            row.revoked = True
        s.commit()
    assert client.get("/v1/auth/session-status").json()["authenticated"] is False


# -- CORS ------------------------------------------------------------------------------------


def test_cors_allows_public_origin_with_credentials(env):
    _, _, client = env
    r = client.get("/v1/auth/session-status",
                   headers={"Origin": "https://www.aithernet.online"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://www.aithernet.online"
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_cors_rejects_unapproved_origin(env):
    _, _, client = env
    r = client.get("/v1/auth/session-status", headers={"Origin": "https://evil.example"})
    # Endpoint still answers (it is public), but the browser is NOT told the foreign origin is
    # allowed, so foreign-site JS cannot read the credentialed response.
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"


def test_no_wildcard_credentialed_cors(env):
    _, _, client = env
    r = client.get("/v1/auth/session-status",
                   headers={"Origin": "https://www.aithernet.online"})
    assert r.headers.get("access-control-allow-origin") != "*"
