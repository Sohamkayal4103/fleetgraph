"""Stage 14E real-OS-process acceptance: node ↔ ingestion service (Part 34).

Two real processes — the Aithernet node and the standalone ingestion service — prove the durable
export contract over loopback only: consent → collect → seal batch → authenticated upload →
receipt → stop ingestion → queue another batch → SIGKILL the node → restart on the same state →
restart ingestion → pending delivery resumes with NO duplicate logical batch → consent
withdrawal blocks new exports → traceable deletion. A separate test proves local-archive export.

Process-heavy; excluded from the ordinary suite by file naming (run explicitly).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "examples" / "external_agent"))
sys.path.insert(0, str(_HERE))
from aithernet.data.crypto import generate_master_secret_b64  # noqa: E402
from aithernet.data.ingest_auth import generate_keypair  # noqa: E402

NODE_LAUNCHER = _HERE / "_node_proc.py"
#: A temporary pseudonymization master secret shared across the node's restart (stable pseudonyms).
_PSEUDO_SECRET = generate_master_secret_b64()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, deadline: float, *, key: str | None = None, val: str | None = None) -> bool:
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=3)
            if r.status_code == 200 and (key is None or r.json().get(key) == val):
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


def _kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    with __import__("contextlib").suppress(Exception):
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)


def _launch_node(state: Path, port: int, env_extra: dict, log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "NODE_ID": "dp-node", "PORT": str(port),
        "DB_PATH": str(state / "db" / "aithernet.db"),
        "IDENTITY_DIR": str(state / "identity"), "STORE_DIR": str(state / "artifacts"),
        "MISSION_EXEC": "0", "HW_ENABLED": "0", "DP_ENABLED": "1", "DP_TENANT": "tenant-a",
        # Pseudonymization master secret (generated per test; stable across restart via env).
        "DP_PSEUDO_SECRET": env_extra.get("DP_PSEUDO_SECRET", _PSEUDO_SECRET),
        **env_extra,
    })
    (state / "db").mkdir(parents=True, exist_ok=True)
    handle = log.open("w")
    return subprocess.Popen([sys.executable, str(NODE_LAUNCHER)], env=env,
                            cwd=str(state.parent), stdout=handle, stderr=subprocess.STDOUT)


def _launch_ingestion(state: Path, port: int, log: Path, admin: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "INGEST_DATABASE_URL": f"sqlite:///{state / 'ing.db'}",
        "INGEST_BLOB_ROOT": str(state / "blobs"), "INGEST_PORT": str(port),
        "INGEST_HOST": "127.0.0.1", "INGEST_ADMIN_TOKEN": admin,
    })
    handle = log.open("w")
    return subprocess.Popen([sys.executable, "-m", "services.ingestion"], env=env,
                            cwd=str(_HERE.parent), stdout=handle, stderr=subprocess.STDOUT)


def _node_post(base: str, path: str, body: dict | None = None) -> httpx.Response:
    return httpx.post(base + path, json=body or {}, timeout=10)


def _approve_all_held(base: str) -> int:
    held = [r for r in httpx.get(base + "/data/records",
                                 params={"status": "held"}, timeout=10).json()]
    for r in held:
        httpx.post(base + f"/data/records/{r['record_id']}/approve", timeout=10)
    return len(held)


def test_node_to_ingestion_process_acceptance(tmp_path):
    node_state = tmp_path / "node"
    ing_state = tmp_path / "ing"
    node_state.mkdir()
    ing_state.mkdir()
    node_port, ing_port = _free_port(), _free_port()
    base = f"http://127.0.0.1:{node_port}"
    ing_base = f"http://127.0.0.1:{ing_port}"
    admin = "admin-secret-token"
    seed, pub = generate_keypair()
    node = ing = None
    try:
        # 1) Launch ingestion + enroll tenant/node (admin).
        ing = _launch_ingestion(ing_state, ing_port, tmp_path / "ing.log", admin)
        assert _wait(f"{ing_base}/ingest/v1/readiness", time.monotonic() + 30,
                     key="status", val="ready"), "ingestion not ready"
        ah = {"authorization": f"Bearer {admin}"}
        httpx.post(f"{ing_base}/ingest/admin/tenants",
                   json={"tenant_id": "tenant-a", "name": "T"}, headers=ah, timeout=10)
        r = httpx.post(f"{ing_base}/ingest/admin/nodes", headers=ah, timeout=10,
                       json={"tenant_id": "tenant-a", "node_id": "dp-node",
                             "key_id": "default", "public_key": pub})
        assert r.status_code == 201, r.text

        # 2) Launch the node with the ingestion signing seed in its env.
        node = _launch_node(node_state, node_port,
                            {"NODE_INGEST_SEED": seed}, tmp_path / "node.log")
        assert _wait(f"{base}/health/ready", time.monotonic() + 40, key="status", val="ready")

        # 3) Consent: accept profile + grant operational export.
        _node_post(base, "/data/consent/profile",
                   {"policy_version": "p1", "required_categories": ["operational"]})
        _node_post(base, "/data/consent/grants", {"category": "operational", "export": True})

        # 4) Configure + enable the HTTP ingestion destination (credential by env ref).
        dest = _node_post(base, "/data/destinations", {
            "kind": "http_ingestion", "name": "central",
            "config": {"base_url": ing_base, "tenant_id": "tenant-a", "node_id": "dp-node",
                       "credential_ref": "NODE_INGEST_SEED"}}).json()
        did = dest["destination_id"]
        assert _node_post(base, f"/data/destinations/{did}/enable").status_code == 200

        # 5) Collect bounded operational records + approve, then build + auto-deliver a batch.
        for _ in range(3):
            _node_post(base, "/data/telemetry/sample")
        assert _approve_all_held(base) >= 1
        batch = _node_post(base, "/data/batches/build",
                           {"destination_id": did, "category": "operational"}).json()
        assert batch.get("batch_id"), batch

        # 6-7) Worker delivers; receipt verified at the node, batch indexed at ingestion.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            b = httpx.get(base + f"/data/batches/{batch['batch_id']}", timeout=10).json()
            if b["status"] == "receipt_verified":
                break
            time.sleep(0.3)
        assert b["status"] == "receipt_verified", b
        assert b["receipt"]["records_digest"] == b["records_digest"]
        ing_batches_1 = httpx.get(ing_base + "/ingest/v1/diagnostics", timeout=10).json()["batches"]
        assert ing_batches_1 == 1

        # 8-11) Stop ingestion, queue another batch, prove it persists in retry/pending.
        _kill(ing)
        ing = None
        for _ in range(2):
            _node_post(base, "/data/telemetry/sample")
        _approve_all_held(base)
        batch2 = _node_post(base, "/data/batches/build",
                            {"destination_id": did, "category": "operational"}).json()
        time.sleep(3)
        b2 = httpx.get(base + f"/data/batches/{batch2['batch_id']}", timeout=10).json()
        assert b2["status"] in ("pending", "retry_wait", "delivering"), b2

        # 12-16) SIGKILL the node, restart on the same state, restart ingestion → resume, no dup.
        _kill(node)
        node = _launch_node(node_state, node_port,
                            {"NODE_INGEST_SEED": seed}, tmp_path / "node2.log")
        assert _wait(f"{base}/health/ready", time.monotonic() + 40, key="status", val="ready")
        ing = _launch_ingestion(ing_state, ing_port, tmp_path / "ing2.log", admin)
        assert _wait(f"{ing_base}/ingest/v1/readiness", time.monotonic() + 30,
                     key="status", val="ready")
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            b2 = httpx.get(base + f"/data/batches/{batch2['batch_id']}", timeout=10).json()
            if b2["status"] == "receipt_verified":
                break
            time.sleep(0.4)
        assert b2["status"] == "receipt_verified", b2
        # No duplicate logical batch: ingestion has exactly the two distinct batches.
        ing_batches_2 = httpx.get(ing_base + "/ingest/v1/diagnostics", timeout=10).json()["batches"]
        assert ing_batches_2 == 2, ing_batches_2

        # 17-18) Withdraw consent → new operational records are no longer export-eligible.
        grants = httpx.get(base + "/data/consent", timeout=10).json()["grants"]
        gid = next(g["grant_id"] for g in grants if g["category"] == "operational")
        _node_post(base, f"/data/consent/{gid}/withdraw", {"reason": "test"})
        _node_post(base, "/data/telemetry/sample")
        # With export consent withdrawn, there are no approvable held records to export.
        held_after = [r for r in httpx.get(base + "/data/records",
                                           params={"status": "held"}, timeout=10).json()]
        assert held_after == [], held_after

        # 19-20) Traceable deletion of a delivered batch's records + lineage update.
        delr = _node_post(base, "/data/deletions",
                          {"scope": "batch", "target_id": batch["batch_id"]}).json()
        assert delr["status"] == "completed", delr
    finally:
        # 21) No orphan processes remain.
        _kill(ing)
        _kill(node)


def test_local_archive_process(tmp_path):
    """A node process exports operational records to a LOCAL archive destination (no network)."""
    node_state = tmp_path / "node"
    node_state.mkdir()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    archive = tmp_path / "archive"
    node = None
    try:
        node = _launch_node(node_state, port, {}, tmp_path / "node.log")
        assert _wait(f"{base}/health/ready", time.monotonic() + 40, key="status", val="ready")
        _node_post(base, "/data/consent/profile",
                   {"policy_version": "p1", "required_categories": ["operational"]})
        _node_post(base, "/data/consent/grants", {"category": "operational", "export": True})
        dest = _node_post(base, "/data/destinations", {
            "kind": "local_archive", "name": "local",
            "config": {"root": str(archive)}}).json()
        did = dest["destination_id"]
        _node_post(base, f"/data/destinations/{did}/enable")
        for _ in range(2):
            _node_post(base, "/data/telemetry/sample")
        _approve_all_held(base)
        batch = _node_post(base, "/data/batches/build",
                           {"destination_id": did, "category": "operational"}).json()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            b = httpx.get(base + f"/data/batches/{batch['batch_id']}", timeout=10).json()
            if b["status"] == "receipt_verified":
                break
            time.sleep(0.3)
        assert b["status"] == "receipt_verified", b
        # The sealed bundle was atomically written under the operator-configured archive root.
        objects = list((archive / "objects").rglob("*.bundle")) if (archive / "objects").exists() \
            else []
        assert objects, "expected an archived bundle on disk"
    finally:
        _kill(node)
