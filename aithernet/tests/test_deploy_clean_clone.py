"""Stage 14F clean-clone deployment acceptance (§6).

Proves a clean clone — containing ONLY tracked-or-trackable files (git-ignored artifacts
excluded) — deploys end to end with NO manually created Dockerfile, prebuilt frontend, or
operator nginx file. Builds the application + frontend images from source, starts the full
PostgreSQL + MinIO + control-plane + ingestion + portal + public-site + proxy stack over loopback,
migrates, bootstraps, runs a bounded account/enrollment/heartbeat/release flow, proves restart
persistence + backup/restore, and verifies no generated file entered the source tree.

Process-heavy + Docker-dependent; excluded from the ordinary suite (run explicitly).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

_REPO = Path(__file__).resolve().parent.parent
_PROJECT = "aithernet_cleanclone"
_PORT = 8080
_BASE = f"http://localhost:{_PORT}"
_API = _BASE + "/api"


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "ps"], capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or not _docker_ok(),
    reason="clean-clone acceptance requires a reachable Docker daemon",
)


def _build_clean_tree(dest: Path) -> None:
    """Copy ONLY tracked + trackable (non-ignored) files into dest — the tracked clone state."""
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=_REPO, capture_output=True).stdout
    others = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=_REPO,
        capture_output=True).stdout
    paths = [p for p in (tracked + others).split(b"\x00") if p]
    for raw in paths:
        rel = raw.decode()
        src = _REPO / rel
        if not src.is_file():
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)


def _dc(tree: Path, env_file: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "--env-file", str(env_file), "-p", _PROJECT,
           "-f", str(tree / "deploy/compose/docker-compose.yml"),
           "-f", str(tree / "deploy/compose/docker-compose.local.yml"), *args]
    return subprocess.run(cmd, cwd=str(tree), capture_output=True, text=True, **kw)


def _write_dev_env(path: Path) -> None:
    import secrets
    path.write_text(
        "AITHERNET_HOSTED_ENV=development\n"
        "AITHERNET_HOSTED_DATABASE_URL=postgresql://aithernet:devpw@postgres:5432/aithernet\n"
        f"AITHERNET_HOSTED_SESSION_KEY={secrets.token_hex(32)}\n"
        "AITHERNET_HOSTED_COOKIE_SECURE=0\n"
        "AITHERNET_HOSTED_EMAIL=development\n"
        "AITHERNET_HOSTED_STORAGE_BACKEND=local\n"
        "AITHERNET_HOSTED_BOOTSTRAP=1\n"
        "AITHERNET_HOSTED_ADMIN_EMAIL=ops@example.invalid\n"
        "AITHERNET_HOSTED_ADMIN_PASSWORD=opsdev-password-123\n"
        "AITHERNET_PUBLIC_SITE_BASE_URL=http://localhost:8080\n"
        "AITHERNET_PORTAL_BASE_URL=http://localhost:8080\n"
        "AITHERNET_CONTROL_PLANE_BASE_URL=http://localhost:8080\n"
        "AITHERNET_INGESTION_BASE_URL=http://localhost:8080\n"
        "AITHERNET_DOWNLOAD_BASE_URL=http://localhost:8080\n"
        "INGEST_DATABASE_URL=postgresql://aithernet:devpw@postgres:5432/aithernet\n"
        f"INGEST_ADMIN_TOKEN={secrets.token_hex(16)}\n"
        "POSTGRES_USER=aithernet\nPOSTGRES_PASSWORD=devpw\nPOSTGRES_DB=aithernet\n"
        "MINIO_ROOT_USER=devaccess\nMINIO_ROOT_PASSWORD=devsecretpw\n")


def _wait_healthy(tree: Path, env_file: Path, deadline: float) -> str:
    last = ""
    while time.monotonic() < deadline:
        ps = _dc(tree, env_file, "ps", "--format", "{{.Service}}={{.Health}}")
        last = ps.stdout.replace("\n", " ")
        services = dict(p.split("=", 1) for p in ps.stdout.split() if "=" in p)
        need = ["control-plane", "ingestion", "postgres", "minio", "portal", "public-site"]
        if all(services.get(s) == "healthy" for s in need):
            return last
        time.sleep(3)
    return last


def test_clean_clone_deploys_end_to_end(tmp_path):
    tree = tmp_path / "clone"
    tree.mkdir()
    _build_clean_tree(tree)
    env_file = tmp_path / "dev.env"  # OUTSIDE the clone tree
    _write_dev_env(env_file)

    # 14.a No manual build input is required: tracked build files present, ignored ones absent.
    assert (tree / "deploy/compose/Dockerfile").is_file()
    assert (tree / "deploy/compose/Dockerfile.frontend").is_file()
    assert (tree / "deploy/compose/nginx/spa.conf.template").is_file()
    assert (tree / "deploy/compose/docker-compose.yml").is_file()
    assert (tree / "deploy/production/up.sh").is_file()
    for ignored in ("deploy/compose/.env", "deploy/static", "portal/dist", "portal/node_modules"):
        assert not (tree / ignored).exists(), f"ignored build input leaked into clone: {ignored}"

    try:
        # 3) Compose config from the clean clone.
        cfg = _dc(tree, env_file, "config", "-q")
        assert cfg.returncode == 0, cfg.stderr

        # 4) Build every image (app + frontend from source).
        _dc(tree, env_file, "down", "-v")  # clear any prior run of this project
        b = _dc(tree, env_file, "build", timeout=900)
        assert b.returncode == 0, b.stderr[-2000:]

        # 5) Start the full stack; 6) wait for health.
        u = _dc(tree, env_file, "up", "-d", timeout=600)
        assert u.returncode == 0, u.stderr[-2000:]
        health = _wait_healthy(tree, env_file, time.monotonic() + 240)
        assert "control-plane=healthy" in health and "ingestion=healthy" in health, health

        # 6) migrate (explicit) + 7) bootstrap (one-shot tasks).
        mig = _dc(tree, env_file, "--profile", "tasks", "run", "--rm", "migrate", timeout=120)
        assert mig.returncode == 0, mig.stderr
        boot = _dc(tree, env_file, "--profile", "tasks", "run", "--rm", "bootstrap-admin",
                   timeout=120)
        assert boot.returncode == 0, boot.stderr

        # 8) health endpoints + 9) proxy routes.
        assert httpx.get(_BASE + "/health/ready", timeout=10).json()["status"] == "ready"
        assert httpx.get(_BASE + "/ingest/v1/readiness", timeout=10).json()["status"] == "ready"
        assert httpx.get(_BASE + "/", timeout=10).status_code == 200  # portal SPA
        assert httpx.get(_API + "/v1/auth/session", timeout=10).status_code == 401

        # 10) bounded account/enrollment/heartbeat/release flow.
        snap_before = _run_flow()

        # 11) restart -> persistence (no duplicates).
        assert _dc(tree, env_file, "down", timeout=120).returncode == 0
        assert _dc(tree, env_file, "up", "-d", timeout=300).returncode == 0
        _wait_healthy(tree, env_file, time.monotonic() + 240)
        after = httpx.get(_API + "/health/diagnostics", timeout=10).json()["counts"]
        assert after == snap_before, (snap_before, after)

        # 12) backup + restore round-trip.
        bk = _dc(tree, env_file, "--profile", "backup", "run", "--rm", "backup", timeout=120)
        assert bk.returncode == 0, bk.stderr
        # Restore the just-written backup (counts must be unchanged).
        name = _latest_backup_name(tree, env_file)
        rs = _dc(tree, env_file, "run", "--rm", "-v", f"{_PROJECT}_backups:/b:ro",
                 "--entrypoint", "python", "control-plane",
                 "-m", "services.control_plane.cli", "restore", "--input", f"/b/{name}",
                 timeout=120)
        assert rs.returncode == 0, rs.stderr
        restored = httpx.get(_API + "/health/diagnostics", timeout=10).json()["counts"]
        assert restored == snap_before

        # 15) verify no generated file entered the clone source tree.
        assert not (tree / "deploy/compose/.env").exists()
        assert not (tree / "portal/dist").exists()
        # No generated .env / build output entered the clone source tree.
        assert not list(tree.glob("**/.env"))
        assert not list(tree.glob("portal/dist/**"))
    finally:
        _dc(tree, env_file, "down", "-v")
        subprocess.run(["docker", "rmi", "aithernet/control-plane:latest",
                        "aithernet/frontend:latest"], capture_output=True)


def _run_flow() -> dict:
    sys.path.insert(0, str(_REPO / "src"))
    sys.path.insert(0, str(_REPO))
    from services.control_plane.release_signing import verify_manifest
    from services.control_plane.security import generate_keypair, load_private_key, sign

    from aithernet.data.ingest_auth import build_headers

    admin = httpx.Client(base_url=_BASE)
    r = admin.post(_API + "/v1/auth/login",
                   json={"email": "ops@example.invalid", "password": "opsdev-password-123"})
    assert r.status_code == 200, r.text
    ah = {"x-csrf-token": r.json()["csrf_token"]}
    pol = admin.post(_API + "/v1/policies", headers=ah, json={
        "policy_type": "preview_terms", "version": "v1", "title": "T",
        "document_text": "x", "required_categories": ["operational"]})
    policy_id = pol.json()["policy_id"]
    inv = admin.post(_API + "/v1/invitations", headers=ah, json={
        "email": "alice@example.invalid", "proposed_tenant_name": "Acme"})
    token = inv.json()["token"]
    preview = admin.get(_API + "/v1/admin/email-preview").json()
    assert any(m["kind"] == "invitation" for m in preview["messages"])  # dev email sink
    acc = httpx.post(_API + "/v1/invitations/accept", json={
        "token": token, "email": "alice@example.invalid", "password": "acme-password-9"})
    tenant = acc.json()["tenant_id"]
    cust = httpx.Client(base_url=_BASE)
    ch = {"x-csrf-token": cust.post(_API + "/v1/auth/login", json={
        "email": "alice@example.invalid", "password": "acme-password-9"}).json()["csrf_token"]}
    cust.post(_API + "/v1/policies/accept", headers=ch,
              json={"policy_id": policy_id, "tenant_id": tenant})
    code = cust.post(_API + "/v1/enrollment-codes", headers=ch,
                     json={"tenant_id": tenant}).json()["code"]
    seed, pub = generate_keypair()
    chal = httpx.post(_API + "/v1/node/enroll/challenge",
                      json={"code": code, "public_key": pub}).json()
    en = httpx.post(_API + "/v1/node/enroll", json={
        "code": code, "public_key": pub, "node_id": "dp-node",
        "signature": sign(load_private_key(seed), chal["challenge"].encode()),
        "display_name": "dp-node", "software_version": "0.8.0-beta.1"})
    assert en.status_code == 200, en.text
    body = json.dumps({"sequence": 1, "readiness_summary": "ready"}).encode()
    hb = build_headers(private=load_private_key(seed), tenant_id=tenant, node_id="dp-node",
                       key_id="default", method="POST", target="/v1/node/heartbeat", body=body,
                       timestamp=int(time.time()), nonce="cc-hb-1")
    assert httpx.post(_API + "/v1/node/heartbeat", content=body, headers=hb).json()["accepted"]
    assert cust.get(_API + "/v1/fleet/nodes",
                    params={"tenant_id": tenant}).json()[0]["state"] == "online"
    rseed, rpub = generate_keypair()
    rel = admin.post(_API + "/v1/releases", headers=ah, json={"version": "0.8.0-beta.1"}).json()
    rid = rel["release_id"]
    admin.post(_API + f"/v1/releases/{rid}/artifacts", content=b"WHEEL",
               headers={**ah, "x-artifact-name": "a.whl"})
    admin.post(_API + f"/v1/releases/{rid}/sign", headers=ah, json={"signing_seed_b64": rseed})
    admin.post(_API + f"/v1/releases/{rid}/publish", headers=ah, json={})
    relh = build_headers(private=load_private_key(seed), tenant_id=tenant, node_id="dp-node",
                         key_id="default", method="GET", target="/v1/node/release", body=b"",
                         timestamp=int(time.time()), nonce="cc-rel-1")
    nr = httpx.get(_API + "/v1/node/release", headers=relh).json()
    assert verify_manifest(rpub, nr["manifest"], nr["manifest_signature"])
    # Enrolled-node downloads are authenticated (signed); the public route refuses packages.
    dlt = f"/v1/node/downloads/{rid}/a.whl"
    dlh = build_headers(private=load_private_key(seed), tenant_id=tenant, node_id="dp-node",
                        key_id="default", method="GET", target=dlt, body=b"",
                        timestamp=int(time.time()), nonce="cc-dl-1")
    assert httpx.get(_API + dlt, headers=dlh).content == b"WHEEL"
    assert httpx.get(_BASE + f"/v1/downloads/{rid}/a.whl").status_code == 403
    sc = cust.post(_API + "/v1/support/cases", headers=ch,
                   json={"tenant_id": tenant, "subject": "help"})
    assert sc.status_code == 200
    return httpx.get(_API + "/health/diagnostics", timeout=10).json()["counts"]


def _latest_backup_name(tree: Path, env_file: Path) -> str:
    out = _dc(tree, env_file, "run", "--rm", "-v", f"{_PROJECT}_backups:/b:ro",
              "--entrypoint", "sh", "control-plane",
              "-c", "ls -1t /b | head -1").stdout.strip().splitlines()
    return out[-1].strip() if out else ""
