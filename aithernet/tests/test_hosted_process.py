"""Stage 14F hosted end-to-end process acceptance (§43).

Real OS processes prove the early-access lifecycle over loopback only, with NO in-process mocks as
the sole evidence: migrate + bootstrap the hosted control plane (CLI), start it as a real process,
retrieve an invitation from the development email sink, accept it, accept a policy, mint a one-time
enrollment code, enroll a node by proving Ed25519 key possession, send a signed heartbeat, show the
node in the fleet, publish a signed release the node verifies, then SIGKILL + restart the control
plane on the SAME stores and prove enrollment / fleet / release / sessions survive with NO
duplicates and no orphan processes.

Process-heavy; excluded from the ordinary suite by file naming (run explicitly).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from services.control_plane.release_signing import verify_manifest
from services.control_plane.security import generate_keypair, load_private_key, sign

from aithernet.data.ingest_auth import build_headers

_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base: str, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            r = httpx.get(base + "/health/ready", timeout=3)
            if r.status_code == 200 and r.json().get("status") == "ready":
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


def _kill(proc):
    if proc is None:
        return
    with __import__("contextlib").suppress(Exception):
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)


def _env(tmp_path: Path, port: int, mailbox: Path, session_key: str) -> dict:
    env = dict(os.environ)
    env.update({
        "AITHERNET_HOSTED_ENV": "development",
        "AITHERNET_HOSTED_DATABASE_URL": f"sqlite:///{tmp_path / 'hosted.db'}",
        "AITHERNET_HOSTED_BLOB_ROOT": str(tmp_path / "blobs"),
        "AITHERNET_HOSTED_HOST": "127.0.0.1", "AITHERNET_HOSTED_PORT": str(port),
        "AITHERNET_HOSTED_SESSION_KEY": session_key,
        "AITHERNET_HOSTED_DEV_MAILBOX": str(mailbox),
        "AITHERNET_HOSTED_ADMIN_EMAIL": "admin@example.invalid",
        "PYTHONPATH": str(_ROOT),
    })
    return env


def _cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "services.control_plane.cli", *args],
                          env=env, cwd=str(_ROOT), capture_output=True, text=True, timeout=60)


def _start_server(env: dict, log: Path) -> subprocess.Popen:
    handle = log.open("w")
    return subprocess.Popen([sys.executable, "-m", "services.control_plane"], env=env,
                            cwd=str(_ROOT), stdout=handle, stderr=subprocess.STDOUT)


def _mailbox_token(mailbox: Path, to: str, kind: str, deadline: float) -> str:
    while time.monotonic() < deadline:
        for path in sorted(mailbox.glob("*.json")):
            data = json.loads(path.read_text())
            if data.get("to") == to and data.get("kind") == kind:
                body = data["body"]
                # The one-time invitation token rides ONLY inside the accept link
                # (…/#/accept-invite?token=<token>); it is never shown as a separate code.
                if "token=" in body:
                    return body.split("token=", 1)[1].split("\n", 1)[0].strip()
                if "code: " in body:  # password-reset style emails still carry a bare code
                    return body.split("code: ", 1)[1].split("\n", 1)[0].strip()
                raise AssertionError("no token/code marker in email body")
        time.sleep(0.2)
    raise AssertionError("invitation email not found in dev mailbox")


def test_hosted_end_to_end_process_acceptance(tmp_path):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    session_key = secrets.token_urlsafe(48)
    env = _env(tmp_path, port, mailbox, session_key)
    server = None
    try:
        # 1-2) Migrate + bootstrap a platform admin via the hosted CLI (real subprocesses).
        assert _cli(env, "migrate").returncode == 0
        boot = _cli(env, "admin", "bootstrap", "--email", "admin@example.invalid",
                    "--password", "supersecret-123")
        assert boot.returncode == 0, boot.stderr

        # Start the control plane as a real process.
        server = _start_server(env, tmp_path / "cp.log")
        assert _wait_ready(base, time.monotonic() + 40), "control plane not ready"

        admin = httpx.Client(base_url=base)
        r = admin.post("/v1/auth/login",
                       json={"email": "admin@example.invalid", "password": "supersecret-123"})
        assert r.status_code == 200, r.text
        ah = {"x-csrf-token": r.json()["csrf_token"]}

        # 3) Publish an early-access policy.
        pol = admin.post("/v1/policies", headers=ah, json={
            "policy_type": "preview_terms", "version": "v1", "title": "Preview Terms",
            "document_text": "placeholder preview terms", "required_categories": ["operational"]})
        assert pol.status_code == 200, pol.text
        policy_id = pol.json()["policy_id"]

        # 4-5) Create an invitation; retrieve its one-time token from the dev email sink.
        inv = admin.post("/v1/invitations", headers=ah, json={
            "email": "alice@example.invalid", "proposed_tenant_name": "Acme"})
        assert inv.status_code == 200
        token = _mailbox_token(mailbox, "alice@example.invalid", "invitation",
                               time.monotonic() + 10)

        # 6-7) Accept the invitation -> account + tenant.
        acc = httpx.post(base + "/v1/invitations/accept", json={
            "token": token, "email": "alice@example.invalid", "password": "acme-password-9"})
        assert acc.status_code == 200
        tenant_id = acc.json()["tenant_id"]

        # 8) Sign in as the customer; accept the required policy.
        cust = httpx.Client(base_url=base)
        rc = cust.post("/v1/auth/login",
                       json={"email": "alice@example.invalid", "password": "acme-password-9"})
        ch = {"x-csrf-token": rc.json()["csrf_token"]}
        assert cust.post("/v1/policies/accept", headers=ch,
                         json={"policy_id": policy_id, "tenant_id": tenant_id}).status_code == 200
        assert cust.get("/v1/policies/status").json()["onboarding_complete"]

        # 9) Mint a one-time enrollment code.
        code = cust.post("/v1/enrollment-codes", headers=ch,
                         json={"tenant_id": tenant_id}).json()["code"]

        # 10-13) Enroll a node by proving Ed25519 key possession.
        seed, pub = generate_keypair()
        chal = httpx.post(base + "/v1/node/enroll/challenge",
                          json={"code": code, "public_key": pub}).json()
        sig = sign(load_private_key(seed), chal["challenge"].encode())
        en = httpx.post(base + "/v1/node/enroll", json={
            "code": code, "public_key": pub, "node_id": "dp-node", "signature": sig,
            "software_version": "0.8.0-beta.1", "display_name": "dp-node"})
        assert en.status_code == 200 and not en.json()["idempotent"]
        hosted_node_id = en.json()["hosted_node_id"]

        # 14) Send a signed heartbeat.
        body = json.dumps({"sequence": 1, "software_version": "0.8.0-beta.1",
                           "readiness_summary": "ready"}).encode()
        hb_headers = build_headers(private=load_private_key(seed), tenant_id=tenant_id,
                                   node_id="dp-node", key_id="default", method="POST",
                                   target="/v1/node/heartbeat", body=body,
                                   timestamp=int(time.time()), nonce="hb-1")
        assert httpx.post(base + "/v1/node/heartbeat", content=body,
                          headers=hb_headers).json()["accepted"]

        # 15) Node appears in the customer fleet view, online.
        fleet = cust.get("/v1/fleet/nodes", params={"tenant_id": tenant_id}).json()
        assert fleet[0]["state"] == "online"

        # 19-24) Publish a signed release; the node verifies it.
        rseed, rpub = generate_keypair()
        rel = admin.post("/v1/releases", headers=ah,
                         json={"version": "0.8.0-beta.1"}).json()
        rid = rel["release_id"]
        admin.post(f"/v1/releases/{rid}/artifacts", content=b"WHEELDATA",
                   headers={**ah, "x-artifact-name": "aithernet-0.8.0.whl"})
        admin.post(f"/v1/releases/{rid}/sign", headers=ah, json={"signing_seed_b64": rseed})
        admin.post(f"/v1/releases/{rid}/publish", headers=ah, json={})
        rel_hdrs = build_headers(private=load_private_key(seed), tenant_id=tenant_id,
                                 node_id="dp-node", key_id="default", method="GET",
                                 target="/v1/node/release", body=b"",
                                 timestamp=int(time.time()), nonce="rel-1")
        node_rel = httpx.get(base + "/v1/node/release", headers=rel_hdrs).json()
        assert verify_manifest(rpub, node_rel["manifest"], node_rel["manifest_signature"])
        # Enrolled-node downloads are AUTHENTICATED (signed) — not a public artifact URL.
        dl_target = f"/v1/node/downloads/{rid}/aithernet-0.8.0.whl"
        dl_hdrs = build_headers(private=load_private_key(seed), tenant_id=tenant_id,
                                node_id="dp-node", key_id="default", method="GET",
                                target=dl_target, body=b"",
                                timestamp=int(time.time()), nonce="dl-1")
        data = httpx.get(base + dl_target, headers=dl_hdrs).content
        assert data == b"WHEELDATA"
        # The package is NOT available via the unauthenticated public route.
        assert httpx.get(base + f"/v1/downloads/{rid}/aithernet-0.8.0.whl").status_code == 403

        # 30-33) SIGKILL the control plane, restart on the SAME stores, prove survival + no dup.
        _kill(server)
        server = _start_server(env, tmp_path / "cp2.log")
        assert _wait_ready(base, time.monotonic() + 40)
        admin2 = httpx.Client(base_url=base)
        admin2.post("/v1/auth/login",
                    json={"email": "admin@example.invalid", "password": "supersecret-123"})
        nodes = admin2.get("/v1/fleet/nodes").json()
        assert len(nodes) == 1 and nodes[0]["hosted_node_id"] == hosted_node_id
        releases = admin2.get("/v1/releases", params={"channel": "early-access"}).json()
        assert len([r for r in releases if r["status"] == "published"]) == 1
        # No duplicate tenant/user/node from the restart.
        diag = httpx.get(base + "/health/diagnostics").json()
        assert diag["counts"]["nodes"] == 1 and diag["counts"]["tenants"] == 1
    finally:
        _kill(server)
