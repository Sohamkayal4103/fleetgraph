"""Stage 14F real-browser portal login acceptance.

Brings up an EPHEMERAL hosted stack (its own project, its own temporary port + volumes + a
temporary platform admin — never the persistent aithernet_local project or its admin secret),
then drives a headless Chromium (puppeteer) through the browser-facing reverse proxy to prove the
full authenticated lifecycle: unauthenticated -> sign in -> HttpOnly cookie -> authenticated portal
+ admin view + signed-in nav -> hard-refresh persistence -> CSRF-protected write (rejected without
token, accepted with) -> sign out -> unauthenticated + protected route requires sign-in.

Process + browser dependent; excluded from the ordinary suite (run explicitly).
"""

from __future__ import annotations

import json
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_PORTAL = _REPO / "portal"
_PROJECT = "aithernet_portal_bt"


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "ps"], capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or shutil.which("node") is None
    or not (_PORTAL / "node_modules" / "puppeteer").exists()
    or not _docker_ok(),
    reason="browser acceptance requires docker + node + puppeteer (npm install in portal/)",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dc(env_file: Path, port: int, *args: str, **kw) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "--env-file", str(env_file), "-p", _PROJECT,
           "-f", str(_REPO / "deploy/compose/docker-compose.yml"),
           "-f", str(_REPO / "deploy/compose/docker-compose.local.yml"), *args]
    env = {"AITHERNET_LOCAL_HTTP_PORT": str(port), "PATH": _path()}
    return subprocess.run(cmd, cwd=str(_REPO), capture_output=True, text=True, env=_full_env(env),
                          **kw)


def _full_env(extra: dict) -> dict:
    import os
    e = dict(os.environ)
    e.update(extra)
    return e


def _path() -> str:
    import os
    return os.environ.get("PATH", "/usr/bin:/bin")


def _write_env(path: Path, port: int, admin_email: str, admin_pw: str) -> None:
    base = f"http://localhost:{port}"
    path.write_text(
        "AITHERNET_HOSTED_ENV=development\n"
        "AITHERNET_HOSTED_DATABASE_URL=postgresql://aithernet:btpw@postgres:5432/aithernet\n"
        f"AITHERNET_HOSTED_SESSION_KEY={secrets.token_hex(32)}\n"
        "AITHERNET_HOSTED_COOKIE_SECURE=0\n"
        "AITHERNET_HOSTED_EMAIL=development\n"
        "AITHERNET_HOSTED_STORAGE_BACKEND=local\n"
        "AITHERNET_HOSTED_BOOTSTRAP=1\n"
        f"AITHERNET_HOSTED_ADMIN_EMAIL={admin_email}\n"
        f"AITHERNET_HOSTED_ADMIN_PASSWORD={admin_pw}\n"
        f"AITHERNET_PUBLIC_SITE_BASE_URL={base}\nAITHERNET_PORTAL_BASE_URL={base}\n"
        f"AITHERNET_CONTROL_PLANE_BASE_URL={base}\nAITHERNET_INGESTION_BASE_URL={base}\n"
        f"AITHERNET_DOWNLOAD_BASE_URL={base}\n"
        "INGEST_DATABASE_URL=postgresql://aithernet:btpw@postgres:5432/aithernet\n"
        f"INGEST_ADMIN_TOKEN={secrets.token_hex(16)}\n"
        "POSTGRES_USER=aithernet\nPOSTGRES_PASSWORD=btpw\nPOSTGRES_DB=aithernet\n"
        "MINIO_ROOT_USER=btaccess\nMINIO_ROOT_PASSWORD=btsecretpw\n")


def _wait_healthy(env_file: Path, port: int, deadline: float) -> str:
    last = ""
    while time.monotonic() < deadline:
        ps = _dc(env_file, port, "ps", "--format", "{{.Service}}={{.Health}}")
        last = ps.stdout.replace("\n", " ")
        svc = dict(p.split("=", 1) for p in ps.stdout.split() if "=" in p)
        if all(svc.get(s) == "healthy" for s in ("control-plane", "portal", "proxy", "postgres")):
            return last
        time.sleep(3)
    return last


def test_browser_portal_login_lifecycle(tmp_path):
    port = _free_port()
    admin_email = "bt-admin@local.invalid"
    admin_pw = "Bt!" + secrets.token_urlsafe(18)  # temporary admin credential
    env_file = tmp_path / "bt.env"
    _write_env(env_file, port, admin_email, admin_pw)
    try:
        _dc(env_file, port, "down", "-v")
        up = _dc(env_file, port, "up", "-d", timeout=900)
        assert up.returncode == 0, up.stderr[-2000:]
        health = _wait_healthy(env_file, port, time.monotonic() + 240)
        assert "control-plane=healthy" in health and "portal=healthy" in health, health

        assert _dc(env_file, port, "--profile", "tasks", "run", "--rm", "migrate",
                   timeout=120).returncode == 0
        boot = _dc(env_file, port, "--profile", "tasks", "run", "--rm", "bootstrap-admin",
                   timeout=120)
        assert boot.returncode == 0, boot.stderr

        driver = subprocess.run(
            ["node", "test-browser/login.mjs"], cwd=str(_PORTAL), capture_output=True, text=True,
            timeout=180,
            env=_full_env({"BASE_URL": f"http://localhost:{port}",
                           "PORTAL_ADMIN_EMAIL": admin_email,
                           "PORTAL_ADMIN_PASSWORD": admin_pw, "PATH": _path()}))
        assert driver.returncode == 0, f"driver failed:\n{driver.stdout}\n{driver.stderr}"
        # The driver prints a single pretty-printed JSON object on stdout.
        text = driver.stdout
        result = json.loads(text[text.index("{"):])
        assert result.get("ok") is True, result
        # Spot-check the key transitions the SPA bug broke.
        assert result["session_before_login"] == 401
        assert result["login_status"] == 200 and result["session_status_after_login"] == 200
        assert result["session_cookie_httponly"] is True
        assert result["nav_shows_sign_in_after_login"] is False
        assert result["portal_requires_signin"] is False
        assert result["nav_shows_sign_in_after_refresh"] is False  # refresh persistence
        assert result["csrf_write_without_token"] == 403
        assert result["csrf_write_with_token"] == 200
        assert result["session_after_logout"] == 401
        assert result["nav_shows_sign_in_after_logout"] is True
        # Functional product: admin console + a public page render real, non-placeholder content.
        assert result["admin_overview_renders"] is True
        assert result["no_placeholder_in_admin"] is True
        assert result["public_install_renders"] is True

        # ---- one-click invitation onboarding journey (same ephemeral stack) ----
        inv = subprocess.run(
            ["node", "test-browser/invite.mjs"], cwd=str(_PORTAL), capture_output=True, text=True,
            timeout=180,
            env=_full_env({"BASE_URL": f"http://localhost:{port}",
                           "PORTAL_ADMIN_EMAIL": admin_email,
                           "PORTAL_ADMIN_PASSWORD": admin_pw, "PATH": _path()}))
        assert inv.returncode == 0, f"invite driver failed:\n{inv.stdout}\n{inv.stderr}"
        itext = inv.stdout
        ires = json.loads(itext[itext.index("{"):])
        assert ires.get("ok") is True, ires
        assert ires["link_is_hash_route"] and ires["on_accept_route"] and ires["not_on_portal"]
        assert ires["no_manual_token_field"] and ires["token_scrubbed_from_url"]
        assert ires["invited_email_shown"] and ires["email_not_editable"]
        assert ires["no_token_in_storage"]
        assert ires["redirected_to_portal"] and ires["portal_renders"] and ires["nav_signed_in"]
        assert ires["accepted_policy_visible"] and ires["session_after_refresh"] == 200
        assert ires["customer_blocked_from_admin"]
        assert ires["reused_token_rejected"] and ires["altered_token_rejected"]
    finally:
        _dc(env_file, port, "down", "-v")  # ephemeral project's OWN volumes only
