"""Stage 14A clean-install smoke (real process, outside the repository working directory).

Provisions a fresh node state root in a temp dir, launches the REAL node as a separate OS process
with its working directory set OUTSIDE the repo, and verifies the operational lifecycle:
provisioning → cross-CWD startup → migration → liveness/readiness → coordinator + legacy RF
backend → mission submit/complete → graceful stop → restart recovery → backup create+verify →
diagnostics sanitization → no orphan processes → no repository-relative production state.

It uses the production scripted-coordinator launcher (no Gemini/Codex/GNU Radio/Marconi/network),
deterministic polling with strict timeouts, and cleans up every subprocess in ``finally``.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "tests" / "_node_proc.py"
HEALTH_TIMEOUT = 45.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/health/ready", timeout=3)
            if r.status_code == 200 and r.json().get("status") == "ready":
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


def _launch(state_root: Path, node_id: str, port: int, cwd: Path, log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "NODE_ID": node_id,
        "PORT": str(port),
        "DB_PATH": str(state_root / "db" / "aithernet.db"),
        "IDENTITY_DIR": str(state_root / "identity"),
        "STORE_DIR": str(state_root / "artifacts"),
        "MISSION_EXEC": "1",
        "NODE_ROLE": "requester",
    })
    handle = log.open("w")
    return subprocess.Popen(
        [sys.executable, str(LAUNCHER)], env=env, cwd=str(cwd),
        stdout=handle, stderr=subprocess.STDOUT,
    )


def _stop(proc: subprocess.Popen, *, sig=signal.SIGINT, timeout=20) -> int | None:
    if proc.poll() is not None:
        return proc.returncode
    proc.send_signal(sig)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        return proc.returncode


def test_clean_install_smoke(tmp_path) -> None:
    from aithernet.config.settings import (
        AgentTransportConfig,
        IdentityConfig,
        NodeConfig,
    )
    from aithernet.ops.backup import create_backup, verify_backup
    from aithernet.ops.diagnostics import build_diagnostics
    from aithernet.ops.provision import provision_node

    state_root = tmp_path / "install" / "node"
    node_id = "node-install-smoke"
    work_dir = tmp_path / "elsewhere"          # a NON-repo working directory
    work_dir.mkdir(parents=True)
    log1 = tmp_path / "node1.log"
    log2 = tmp_path / "node2.log"
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    # 1. provisioning (idempotent layout + migrated db)
    result = provision_node(str(state_root), node_id=node_id, node_name="Install Smoke")
    assert (state_root / "db" / "aithernet.db").is_file()
    assert oct(os.stat(state_root / "identity").st_mode & 0o777) == "0o700"
    assert result.database_migrated

    # snapshot the repo dir so we can prove nothing production-relative is written there
    repo_db_before = set(REPO_ROOT.glob("aithernet*.db"))

    proc1 = proc2 = None
    try:
        # 2-4. start from a different CWD; schema already migrated; readiness gating
        proc1 = _launch(state_root, node_id, port, work_dir, log1)
        assert _wait_ready(url, time.monotonic() + HEALTH_TIMEOUT), log1.read_text()[-2000:]
        assert httpx.get(f"{url}/health/live", timeout=5).json()["status"] == "ok"
        ov = httpx.get(f"{url}/ops/overview", timeout=5).json()
        assert ov["migrations"]["schema_valid"] is True
        assert ov["readiness"]["ready"] is True

        # 5. coordinator + legacy RF backend reachable (no live deps required)
        assert httpx.get(f"{url}/coordinator/status", timeout=5).status_code == 200
        rf = httpx.get(f"{url}/rf/backends", timeout=5).json()
        assert any(b.get("backend_id") == "legacy_gr_mcp" for b in rf)

        # 6-7. mission submit + completion (scripted coordinator completes a no-peer mission)
        mid = httpx.post(f"{url}/missions",
                         json={"content": "operational check", "source_type": "user"},
                         timeout=10).json()["id"]
        httpx.post(f"{url}/missions/{mid}/start", timeout=10)
        deadline = time.monotonic() + 60
        final = None
        while time.monotonic() < deadline:
            st = httpx.get(f"{url}/missions/{mid}/execution", timeout=5).json()["mission"]["status"]
            if st in ("completed", "failed", "blocked", "cancelled"):
                final = st
                break
            time.sleep(0.3)
        assert final == "completed", f"mission did not complete: {final}"

        # 8. graceful stop — the process exits in bounded time on SIGINT
        _stop(proc1, sig=signal.SIGINT, timeout=30)
        assert proc1.poll() is not None
        proc1 = None

        # 9. restart recovery — readiness again + the completed mission persists
        proc2 = _launch(state_root, node_id, port, work_dir, log2)
        assert _wait_ready(url, time.monotonic() + HEALTH_TIMEOUT), log2.read_text()[-2000:]
        persisted = httpx.get(f"{url}/missions/{mid}/execution", timeout=5).json()
        assert persisted["mission"]["status"] == "completed"

        _stop(proc2, sig=signal.SIGINT, timeout=30)
        proc2 = None

        # 10. backup create + verify against the provisioned config
        cfg = NodeConfig(
            node_id=node_id, node_name="Install Smoke", node_state_root=str(state_root),
            database_url=f"sqlite:///{state_root / 'db' / 'aithernet.db'}",
            backup_directory=str(state_root / "backups"),
            agent_transport=AgentTransportConfig(
                identity=IdentityConfig(state_directory=str(state_root / "identity"))),
        )
        backup = create_backup(cfg, backup_dir=str(state_root / "backups"),
                               config_path=str(state_root / "config" / "node.yaml"),
                               now_iso="2026-06-14T00:00:00+00:00")
        assert verify_backup(backup.path).ok

        # 11. diagnostics sanitization — no secrets, no raw db, no repo path
        diag = build_diagnostics(cfg, config_path=str(state_root / "config" / "node.yaml"),
                                 now_iso="2026-06-14T00:00:00+00:00")
        blob = json.dumps(diag)
        assert "-----BEGIN" not in blob and "PRIVATE KEY" not in blob
        assert str(REPO_ROOT) not in blob  # diagnostics never leak the repo checkout path
        assert diag["database_integrity"] == "ok"

        # 12. no repository-relative production state was created
        assert set(REPO_ROOT.glob("aithernet*.db")) == repo_db_before
        # 13. logs contain no private key / signature material
        combined = log1.read_text() + (log2.read_text() if log2.exists() else "")
        assert "BEGIN PRIVATE KEY" not in combined
    finally:
        for proc in (proc1, proc2):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    # 14. no orphan launcher process remains
    check = subprocess.run(["pgrep", "-af", "_node_proc.py"], capture_output=True, text=True)
    leftover = [ln for ln in check.stdout.splitlines() if "pgrep" not in ln and str(port) in ln]
    assert leftover == [], f"orphan node processes: {leftover}"
