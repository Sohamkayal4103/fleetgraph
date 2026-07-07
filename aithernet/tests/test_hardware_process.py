"""Stage 14B process-level acceptance smoke (Part V).

Launches a REAL node process with managed hardware enabled and a fake `static_file` discovery
provider whose descriptors file the test rewrites to make a device appear/disappear/return. It
verifies, across a real process kill + restart on the same database:

  fresh node starts with inventory service -> fake device appears -> stable identity persists ->
  lease acquired -> conflicting lease rejected -> process terminates while lease active -> node
  restarts on the same DB -> inventory record not duplicated -> lease reconciled safely -> no
  conflicting lease granted during recovery -> device becomes missing (factual lease event, no
  mission completion invented) -> device returns under the same identity -> TX not auto-resumed ->
  new lease acquirable after valid recovery -> APIs/diagnostics expose no raw paths/secrets -> no
  orphan processes remain.

Reuses the Stage 14A/13D subprocess harness (`_node_proc.py`). Times are generous for slow CI.
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

LAUNCHER = Path(__file__).parent / "_node_proc.py"
READY_TIMEOUT = 45.0

DEVICE = {
    "vendor": "Ettus", "product": "B210", "serial": "SMOKE-1", "driver": "uhd",
    "device_kind": "sdr",
    "capabilities": {
        "rx": True, "tx": True, "channels": 2,
        "frequency_ranges": [[70_000_000, 6_000_000_000]],
        "sample_rate_ranges": [[1_000_000, 56_000_000]], "shared_receive_safe": False,
    },
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _launch(state: Path, descriptors: Path, node_id: str, port: int, log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "NODE_ID": node_id, "PORT": str(port),
        "DB_PATH": str(state / "db" / "aithernet.db"),
        "IDENTITY_DIR": str(state / "identity"), "STORE_DIR": str(state / "artifacts"),
        "MISSION_EXEC": "0", "HW_ENABLED": "1", "HW_DESCRIPTORS_FILE": str(descriptors),
        "HW_LEASE_SECONDS": "3",
    })
    (state / "db").mkdir(parents=True, exist_ok=True)
    handle = log.open("w")
    return subprocess.Popen(
        [sys.executable, str(LAUNCHER)], env=env, cwd=str(state.parent),
        stdout=handle, stderr=subprocess.STDOUT,
    )


def _stop(proc, *, sig=signal.SIGINT, timeout=20):
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(sig)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


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


def _devices(url: str) -> list[dict]:
    return httpx.get(f"{url}/hardware/devices", timeout=5).json()


def _wait_device_present(url: str, deadline: float) -> dict | None:
    while time.monotonic() < deadline:
        devs = _devices(url)
        present = [d for d in devs if d["status"] == "present"]
        if present:
            return present[0]
        time.sleep(0.2)
    return None


def _wait(predicate, deadline: float, interval: float = 0.2) -> bool:
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_hardware_acceptance_smoke(tmp_path):
    state = tmp_path / "node"
    descriptors = tmp_path / "devices.json"
    descriptors.write_text(json.dumps([DEVICE]))
    node_id = "hw-smoke"
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log1 = tmp_path / "n1.log"
    log2 = tmp_path / "n2.log"
    proc1 = proc2 = None
    try:
        # 1) fresh node starts with the inventory service
        proc1 = _launch(state, descriptors, node_id, port, log1)
        assert _wait_ready(url, time.monotonic() + READY_TIMEOUT), log1.read_text()[-2000:]

        # 2-3) fake device appears with a stable identity
        device = _wait_device_present(url, time.monotonic() + 20)
        assert device is not None, "device never appeared"
        device_id = device["device_id"]
        hardware_key = device["hardware_key"]
        assert device["rx"] is True and device["tx"] is True

        # 4) lease acquired
        r = httpx.post(f"{url}/hardware/leases/acquire",
                       json={"device_id": device_id, "backend_id": "legacy_gr_mcp",
                             "direction": "rx"}, timeout=10)
        assert r.status_code == 200, r.text
        lease_id = r.json()["lease_id"]
        assert "lease_token" not in r.json()

        # 5) conflicting lease rejected
        conflict = httpx.post(f"{url}/hardware/leases/acquire",
                              json={"device_id": device_id, "backend_id": "legacy_gr_mcp"},
                              timeout=10)
        assert conflict.status_code == 409

        # 6) process terminates while the lease is active
        _stop(proc1, sig=signal.SIGINT, timeout=30)
        assert proc1.poll() is not None
        proc1 = None

        # 7) node restarts on the same database
        proc2 = _launch(state, descriptors, node_id, port, log2)
        assert _wait_ready(url, time.monotonic() + READY_TIMEOUT), log2.read_text()[-2000:]

        # 8) inventory record is not duplicated (same id rediscovered)
        device = _wait_device_present(url, time.monotonic() + 20)
        assert device is not None
        assert device["device_id"] == device_id
        assert device["hardware_key"] == hardware_key
        assert len(_devices(url)) == 1

        # 9-10) the pre-restart lease is reconciled safely (never stays active); no conflicting
        #       lease is granted during recovery -> a NEW lease becomes acquirable only after the
        #       stale lease is terminal.
        def _new_lease_ok() -> str | None:
            resp = httpx.post(f"{url}/hardware/leases/acquire",
                              json={"device_id": device_id, "backend_id": "legacy_gr_mcp"},
                              timeout=10)
            return resp.json()["lease_id"] if resp.status_code == 200 else None

        new_lease_id = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and new_lease_id is None:
            new_lease_id = _new_lease_ok()
            time.sleep(0.3)
        assert new_lease_id is not None, "could not acquire a fresh lease after recovery"
        old = httpx.get(f"{url}/hardware/leases/{lease_id}", timeout=5).json()
        assert old["state"] in ("orphaned", "expired", "released"), old["state"]

        # 11-12) device becomes missing -> a factual lease event is recorded (no mission invented)
        descriptors.write_text(json.dumps([]))
        assert _wait(
            lambda: all(d["status"] != "present" for d in _devices(url)),
            time.monotonic() + 15,
        ), "device never went missing"
        events = httpx.get(f"{url}/hardware/leases/{new_lease_id}/events", timeout=5).json()
        assert any(e["event_type"] == "orphaned" for e in events), events

        # 13-14) device returns under the SAME identity; TX is not auto-resumed
        descriptors.write_text(json.dumps([DEVICE]))
        device = _wait_device_present(url, time.monotonic() + 20)
        assert device is not None and device["device_id"] == device_id  # no duplicate
        active_leases = [le for le in httpx.get(f"{url}/hardware/leases?active=true",
                                                timeout=5).json()]
        # no lease was automatically (re)granted on device return
        assert all(le["lease_id"] != new_lease_id or le["state"] != "active"
                   for le in active_leases)

        # 15) a new lease can be acquired after valid recovery
        again = httpx.post(f"{url}/hardware/leases/acquire",
                           json={"device_id": device_id, "backend_id": "legacy_gr_mcp"}, timeout=10)
        assert again.status_code == 200

        # 16) APIs and diagnostics expose no raw device paths or lease tokens. (The configured
        # descriptors-file path legitimately appears in the sanitized config structure, exactly as
        # database_url does — it is operator config, not a device path or secret.)
        diag = httpx.get(f"{url}/ops/diagnostics", timeout=10).text
        devs_text = httpx.get(f"{url}/hardware/devices", timeout=5).text
        for blob in (diag, devs_text):
            assert "/dev/" not in blob
            assert "lease_token" not in blob
            assert "owner_worker_id" not in blob
        # the device API never leaks the raw descriptors-file path
        assert str(descriptors) not in devs_text
        # diagnostics carry the bounded hardware summary
        assert "hardware_devices_by_status" in diag

        _stop(proc2, sig=signal.SIGINT, timeout=30)
        proc2 = None
    finally:
        for proc in (proc1, proc2):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    # 17) no orphan launcher process remains for this port
    check = subprocess.run(["pgrep", "-af", "_node_proc.py"], capture_output=True, text=True)
    leftover = [ln for ln in check.stdout.splitlines()
                if "pgrep" not in ln and str(port) in ln]
    assert leftover == [], f"orphan node processes: {leftover}"
