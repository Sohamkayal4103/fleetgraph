"""Stage 13D.2 REAL process-restart artifact-resume proof (final acceptance test).

Unlike test_artifact_two_node.py (which reset state in-process), this test launches Node A and
Node B as SEPARATE operating-system processes, interrupts a running transfer by SIGKILLing the
entire Node A process mid-flight, restarts Node A as a fresh process against the SAME persistent
database + identity + artifact store + partial file, and proves that startup recovery resumes
the download from the persisted non-zero offset — not from byte zero — completing to an exact
SHA-256 + size match with one logical transfer and one content-addressed object. A second
restart after completion proves the completed transfer is not re-downloaded.

Determinism: a transfer is paced by the production ``artifacts.worker_chunk_delay_seconds`` knob
(default 0; set > 0 only here), and every wait polls PERSISTED state with strict timeouts. All
subprocesses are killed in ``finally``; logs are preserved on failure; no orphan is left.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from aithernet.config.settings import (
    AgentTransportConfig,
    ArtifactConfig,
    IdentityConfig,
    MissionExecutionConfig,
    NodeConfig,
    TransportLocalDevelopmentConfig,
)
from aithernet.orchestrator.runtime import NodeRuntime

_LAUNCHER = str(Path(__file__).with_name("_node_proc.py"))
_A_PORT, _B_PORT = 8790, 8791
_CHUNK = 64 * 1024
_DELAY = 0.15           # per-chunk download delay on A (deterministic interruption window)
_ARTIFACT_BYTES = 2 * 1024 * 1024  # ~32 chunks


def _env(node_id, port, base: Path, *, delay="0"):
    return {
        **os.environ,
        "NODE_ID": node_id, "PORT": str(port),
        "DB_PATH": str(base / "n.db"),
        "IDENTITY_DIR": str(base / "identity"),
        "STORE_DIR": str(base / "artifact-store"),
        "CHUNK_BYTES": str(_CHUNK), "CHUNK_DELAY": delay, "GRANT_TTL": "3600",
    }


def _launch(env, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "ab")
    return subprocess.Popen([sys.executable, _LAUNCHER], env=env, stdout=log, stderr=log)


def _wait_health(url, *, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    return False


def _post(url, path, body=None):
    return httpx.post(f"{url}{path}", json=body, timeout=20)


def _get(url, path):
    return httpx.get(f"{url}{path}", timeout=20)


def _patch(url, path, body):
    return httpx.patch(f"{url}{path}", json=body, timeout=20)


def _seed_index(base: Path, node_id, blob: bytes) -> dict:
    """Pre-seed Node B's persistent DB + store with one indexed artifact (in-process, disposed
    before the real B process launches against the same state)."""
    cfg = NodeConfig(
        node_id=node_id, node_name=node_id, host="127.0.0.1", port=_B_PORT,
        database_url=f"sqlite:///{base / 'n.db'}", log_level="warning",
        mission_execution=MissionExecutionConfig(enabled=False),
        agent_transport=AgentTransportConfig(
            identity=IdentityConfig(state_directory=str(base / "identity")),
            local_development=TransportLocalDevelopmentConfig(allow_insecure_http=True),
        ),
        artifacts=ArtifactConfig(enabled=True, state_directory=str(base / "artifact-store"),
                                 chunk_bytes=_CHUNK, minimum_free_bytes=0),
    )
    rt = NodeRuntime.from_config(cfg)
    src = base / "capture.bin"
    src.write_bytes(blob)
    art = rt.artifacts.index_local_artifact(source_path=str(src), backend_id="marconi",
                                            artifact_kind="capture", display_name="capture.bin")
    rt.engine.dispose()
    return art


def _await_transfer(url, tid, predicate, *, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = _get(url, f"/artifact-transfers/{tid}")
        if r.status_code == 200 and predicate(r.json()):
            return r.json()
        time.sleep(0.1)
    r = _get(url, f"/artifact-transfers/{tid}")
    return r.json() if r.status_code == 200 else {}


def _alive(proc):
    return proc.poll() is None


def _kill(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def test_real_process_restart_resumes_from_persisted_offset():
    root = Path(tempfile.mkdtemp(prefix="13d2-restart-"))
    a_dir, b_dir = root / "node-A", root / "node-B"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    blob = os.urandom(_ARTIFACT_BYTES)
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    art = _seed_index(b_dir, "node-B", blob)
    assert art["digest"] == digest and art["size_bytes"] == len(blob)

    a_url, b_url = f"http://127.0.0.1:{_A_PORT}", f"http://127.0.0.1:{_B_PORT}"
    procs: list[subprocess.Popen] = []
    a_log, b_log = root / "node-A.log", root / "node-B.log"
    try:
        # 1. Start A and B as REAL independent processes.
        a = _launch(_env("node-A", _A_PORT, a_dir, delay=str(_DELAY)), a_log)
        b = _launch(_env("node-B", _B_PORT, b_dir), b_log)
        procs += [a, b]
        assert _wait_health(a_url) and _wait_health(b_url), "nodes did not become healthy"

        # 3. Initialize identities + explicit mutual trust + artifact permissions.
        _post(a_url, "/identity/initialize")
        _post(b_url, "/identity/initialize")
        a_pub = _get(a_url, "/identity/public").json()["public_key"]
        b_pub = _get(b_url, "/identity/public").json()["public_key"]
        pb = _post(a_url, "/peers", {"name": "B", "role": "node", "endpoint_url": b_url,
                   "expected_node_id": "node-B", "public_key": b_pub}).json()["id"]
        _post(a_url, f"/peers/{pb}/trust", {"public_key": b_pub})
        pa = _post(b_url, "/peers", {"name": "A", "role": "node", "endpoint_url": a_url,
                   "expected_node_id": "node-A", "public_key": a_pub}).json()["id"]
        _post(b_url, f"/peers/{pa}/trust", {"public_key": a_pub})
        _patch(b_url, f"/peers/{pa}/artifact-permissions",
               {"may_request_artifacts": True, "may_receive_artifacts": True})

        # 3-4. Request the artifact; wait until A is transferring with a persisted partial > 0.
        req = _post(a_url, "/artifact-transfers/request",
                    {"peer_id": pb, "origin_artifact_id": art["artifact_id"],
                     "digest": digest, "size": len(blob)}).json()
        tid = req["transfer_id"]
        partial_file = a_dir / "artifact-store" / "partial" / tid
        mid = _await_transfer(
            a_url, tid,
            lambda t: t["state"] == "transferring" and t["received_bytes"] > 0
            and t["received_bytes"] < len(blob),
            timeout=30,
        )
        assert mid.get("state") == "transferring" and mid["received_bytes"] > 0, mid

        # 5. Record the persisted progress just before termination.
        pre_kill_received = mid["received_bytes"]
        pre_kill_partial = partial_file.stat().st_size if partial_file.exists() else 0
        assert pre_kill_partial > 0 and pre_kill_partial < len(blob)

        # 6-7. SIGKILL the ENTIRE Node A process (abrupt death) and confirm it is gone.
        _kill(a)
        assert not _alive(a), "Node A still running after SIGKILL"
        # 8. The persistent state (db + identity + store + partial + transfer row) is preserved
        # on disk — nothing is cleaned up here.
        assert partial_file.exists() and partial_file.stat().st_size == pre_kill_partial

        # 9. Restart Node A as a FRESH OS process against the same state.
        a2 = _launch(_env("node-A", _A_PORT, a_dir, delay=str(_DELAY)), a_log)
        procs[0] = a2
        assert _wait_health(a_url), "Node A did not restart"

        # 10-16. Startup recovery resumes the transfer to completion.
        final = _await_transfer(a_url, tid, lambda t: t["state"] in (
            "completed", "failed", "rejected", "cancelled", "expired"), timeout=40)
        assert final.get("state") == "completed", final

        # 11-12-15. The resumed attempt started at the persisted offset, NOT zero.
        attempts = final.get("attempts", [])
        assert len(attempts) >= 2, attempts
        resumed = [at for at in attempts if at["start_offset"] > 0]
        assert resumed, f"no attempt resumed from a non-zero offset: {attempts}"
        assert resumed[-1]["start_offset"] == pre_kill_partial
        assert resumed[-1]["start_offset"] >= pre_kill_received  # never below the saved progress
        assert any(at["start_offset"] == 0 for at in attempts)   # the original fresh attempt

        # 13. Exactly one logical transfer row (no second transfer created by the restart).
        rows = [t for t in _get(a_url, "/artifact-transfers").json() if t["transfer_id"] == tid]
        assert len(rows) == 1

        # 14-17. Exactly one imported artifact + one content-addressed object; exact digest/size.
        imported = [x for x in _get(a_url, "/artifacts").json()
                    if x["digest"] == digest and x["availability_state"] == "imported"]
        assert len(imported) == 1 and imported[0]["size_bytes"] == len(blob)
        obj_dir = a_dir / "artifact-store" / "objects" / "sha256" / digest.split(":")[1][:2]
        objs = [p for p in obj_dir.iterdir() if p.name.startswith("sha256_")]
        assert len(objs) == 1
        assert hashlib.sha256(objs[0].read_bytes()).hexdigest() == digest.split(":")[1]
        assert not partial_file.exists()  # no partial exposed as complete

        # 18-19. Restart A again; the completed transfer is NOT re-downloaded.
        attempts_before = len(final["attempts"])
        _kill(a2)
        assert not _alive(a2)
        a3 = _launch(_env("node-A", _A_PORT, a_dir, delay=str(_DELAY)), a_log)
        procs[0] = a3
        assert _wait_health(a_url)
        time.sleep(2.0)  # give recovery + a poll cycle a chance to (incorrectly) re-run
        still = _get(a_url, f"/artifact-transfers/{tid}").json()
        assert still["state"] == "completed"
        assert len(still.get("attempts", [])) == attempts_before  # no new attempt
        objs_after = [p for p in obj_dir.iterdir() if p.name.startswith("sha256_")]
        assert len(objs_after) == 1  # no duplicate object

        # 20. No bytes / paths / keys / signatures / grants in any API response or store status.
        blobs = " ".join([
            _get(a_url, "/artifacts").text, _get(a_url, "/artifact-transfers").text,
            _get(a_url, f"/artifact-transfers/{tid}").text,
            _get(a_url, "/artifact-store/status").text, _get(a_url, "/events").text,
        ]).lower()
        for marker in ("signature", "-----begin", "private_key", str(root).lower(),
                       "objects/sha256", "expected_sender"):
            assert marker not in blobs, marker
    finally:
        for p in procs:
            _kill(p)
        # Surface logs on failure for debugging (no secrets are logged).
        for log in (a_log, b_log):
            if log.exists() and log.stat().st_size and os.environ.get("KEEP_ARTIFACT_LOGS"):
                print(f"--- {log} ---\n{log.read_text()[-2000:]}")
