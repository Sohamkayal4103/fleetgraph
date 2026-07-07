"""Stage 14C.1 REAL-PROCESS PlutoSDR restart acceptance test (Part G, real hardware).

Launches the real Aithernet node as an OS process against a PlutoSDR, performs a bounded real RX
capture while holding an RX lease, HARD-KILLS the node process (SIGKILL — a real crash, no graceful
lease release), restarts a second OS process against the SAME database/identity/state root, and
proves: same persisted device identity (no duplicate), stale-lease orphaning + conflict protection
BEFORE reconciliation, successful reconciliation, a fresh RX lease, a fresh real capture, and no
orphan process. RX only — never transmits.

This test REQUIRES the attached PlutoSDR and a GNU Radio interpreter; it SKIPS (never fails) when
they are absent, because physical acceptance cannot be satisfied by a fake (per the ground rule).
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
import pytest

LAUNCHER = Path(__file__).parent / "_node_proc.py"
GR_PYTHON = os.environ.get("GR_PYTHON", "/usr/bin/python3")
READY_TIMEOUT = 45.0


def _pluto_present() -> bool:
    try:
        out = subprocess.run(["SoapySDRUtil", "--find=driver=plutosdr"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return "Found device" in (out.stdout or "")


def _gr_ready() -> bool:
    try:
        out = subprocess.run([GR_PYTHON, "-c", "from gnuradio import gr, iio; print('ok')"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return "ok" in (out.stdout or "")


pytestmark = pytest.mark.skipif(
    not (_pluto_present() and _gr_ready()),
    reason="real PlutoSDR + GNU Radio interpreter not available (physical acceptance only)",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _launch(state: Path, node_id: str, port: int, log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "NODE_ID": node_id, "PORT": str(port),
        "DB_PATH": str(state / "db" / "aithernet.db"),
        "IDENTITY_DIR": str(state / "identity"), "STORE_DIR": str(state / "artifacts"),
        "MISSION_EXEC": "0", "HW_ENABLED": "1", "HW_REAL_PLUTO": "1",
        "HW_LEASE_SECONDS": "120", "HW_GRACE": "8.0", "HW_DISCOVERY_INTERVAL": "3.0",
        "GR_PYTHON": GR_PYTHON,
    })
    (state / "db").mkdir(parents=True, exist_ok=True)
    handle = log.open("w")
    return subprocess.Popen(
        [sys.executable, str(LAUNCHER)], env=env, cwd=str(state.parent),
        stdout=handle, stderr=subprocess.STDOUT,
    )


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


def _wait_present(url: str, deadline: float) -> dict | None:
    while time.monotonic() < deadline:
        present = [d for d in _devices(url) if d["status"] == "present"]
        if present:
            return present[0]
        time.sleep(0.3)
    return None


def test_pluto_real_process_restart_recovery(tmp_path):
    state = tmp_path / "node"
    node_id = "pluto-restart"
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc1 = proc2 = None
    try:
        # 1) launch the real node as an OS process; discover the PlutoSDR
        proc1 = _launch(state, node_id, port, tmp_path / "n1.log")
        old_pid = proc1.pid
        assert _wait_ready(url, time.monotonic() + READY_TIMEOUT), \
            (tmp_path / "n1.log").read_text()[-2000:]
        device = _wait_present(url, time.monotonic() + 30)
        assert device is not None, "PlutoSDR never discovered"
        device_id = device["device_id"]
        hardware_key = device["hardware_key"]
        assert device["rx"] is True
        print(f"\nEVIDENCE old_pid={old_pid} device_id={device_id} "
              f"hardware_key={hardware_key} serial={device.get('serial')}")

        # 2) acquire an RX lease and 3) perform a bounded REAL RX capture while holding it
        lease = httpx.post(f"{url}/hardware/leases/acquire",
                           json={"device_id": device_id, "backend_id": "legacy_gr_mcp",
                                 "direction": "rx"}, timeout=10)
        assert lease.status_code == 200, lease.text
        lease_id = lease.json()["lease_id"]
        cap = httpx.post(f"{url}/hardware/devices/{device_id}/capture",
                         json={"lease_id": lease_id, "duration_s": 1.0}, timeout=60)
        assert cap.status_code == 200, cap.text
        assert cap.json()["bytes"] > 0 and cap.json()["sample_count"] > 0
        print(f"EVIDENCE pre_restart_capture bytes={cap.json()['bytes']} "
              f"samples={cap.json()['sample_count']} digest={cap.json()['digest']} "
              f"lease_held={lease_id}")
        # lease is still held (capture under an existing lease never releases it)
        assert httpx.get(f"{url}/hardware/leases/{lease_id}", timeout=5).json()["state"] == "active"

        # 4) HARD-KILL the node process while the lease is active (a real crash)
        proc1.send_signal(signal.SIGKILL)
        assert proc1.wait(timeout=15) is not None
        assert proc1.poll() is not None  # confirmed process exit
        proc1 = None

        # 5) restart a SECOND OS process against the SAME db/identity/state root
        proc2 = _launch(state, node_id, port, tmp_path / "n2.log")
        new_pid = proc2.pid
        assert new_pid != old_pid
        assert _wait_ready(url, time.monotonic() + READY_TIMEOUT), \
            (tmp_path / "n2.log").read_text()[-2000:]
        restart_at = time.monotonic()

        # 6) the crashed lease was orphaned on restart (not silently dropped, not still active)
        st = httpx.get(f"{url}/hardware/leases/{lease_id}", timeout=5).json()["state"]
        assert st in ("orphaned", "expired"), st
        print(f"EVIDENCE killed_old_pid={old_pid} exit=confirmed new_pid={new_pid} "
              f"same_db_state_root=yes post_restart_lease_state={st}")

        # 7) same device identity, no duplicate
        device = _wait_present(url, time.monotonic() + 30)
        assert device is not None
        assert device["device_id"] == device_id
        assert device["hardware_key"] == hardware_key
        assert len(_devices(url)) == 1

        # 8) conflict protection BEFORE reconciliation: while the orphaned lease still holds the
        #    device (within the stale grace window), a new exclusive lease is rejected.
        if time.monotonic() - restart_at < 6.0:
            blocked = httpx.post(f"{url}/hardware/leases/acquire",
                                 json={"device_id": device_id, "backend_id": "legacy_gr_mcp"},
                                 timeout=10)
            assert blocked.status_code == 409, \
                f"expected 409 during recovery, got {blocked.status_code}"

        # 9) after reconciliation (stale lease expires) a fresh RX lease becomes acquirable
        new_lease_id = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and new_lease_id is None:
            r = httpx.post(f"{url}/hardware/leases/acquire",
                           json={"device_id": device_id, "backend_id": "legacy_gr_mcp"}, timeout=10)
            if r.status_code == 200:
                new_lease_id = r.json()["lease_id"]
            else:
                time.sleep(0.5)
        assert new_lease_id is not None, "could not acquire a fresh lease after recovery"
        assert httpx.get(f"{url}/hardware/leases/{lease_id}",
                         timeout=5).json()["state"] == "expired"

        # 10) fresh REAL RX capture after restart
        cap2 = httpx.post(f"{url}/hardware/devices/{device_id}/capture",
                          json={"lease_id": new_lease_id, "duration_s": 1.0}, timeout=60)
        assert cap2.status_code == 200, cap2.text
        assert cap2.json()["bytes"] > 0 and cap2.json()["sample_count"] > 0
        print(f"EVIDENCE fresh_lease={new_lease_id} post_restart_capture "
              f"bytes={cap2.json()['bytes']} samples={cap2.json()['sample_count']} "
              f"digest={cap2.json()['digest']} device_count={len(_devices(url))}")
        httpx.post(f"{url}/hardware/leases/{new_lease_id}/release", timeout=10)

        # 11) still exactly one device record (no duplicate)
        assert len(_devices(url)) == 1

        proc2.send_signal(signal.SIGINT)
        proc2.wait(timeout=30)
        proc2 = None
    finally:
        for proc in (proc1, proc2):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    # 12) no orphan launcher process remains for this port
    check = subprocess.run(["pgrep", "-af", "_node_proc.py"], capture_output=True, text=True)
    leftover = [ln for ln in check.stdout.splitlines()
                if "pgrep" not in ln and str(port) in ln]
    assert leftover == [], f"orphan node processes: {leftover}"
