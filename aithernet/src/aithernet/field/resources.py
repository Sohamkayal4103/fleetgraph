"""Bounded resource sampling for soak validation (Stage 14C.2, Part M).

Reads process + storage + node-state metrics from ``/proc`` and the repositories — no psutil
dependency, no cloud upload. Every reader is best-effort (returns ``None`` on failure) so a soak
sample never raises. Sampling creates NO MissionStep.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _proc_status(pid: int) -> dict[str, str]:
    out: dict[str, str] = {}
    with contextlib.suppress(OSError):
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                out[key.strip()] = value.strip()
    return out


def sample_process(pid: int | None = None) -> dict:
    """RSS, FD count, thread count, child count, and CPU seconds for ``pid`` (default: self)."""
    pid = pid or os.getpid()
    status = _proc_status(pid)
    out: dict = {"rss_bytes": None, "fd_count": None, "thread_count": None,
                 "child_count": None, "cpu_seconds": None}
    if "VmRSS" in status:
        with contextlib.suppress(ValueError, IndexError):
            out["rss_bytes"] = int(status["VmRSS"].split()[0]) * 1024
    if "Threads" in status:
        with contextlib.suppress(ValueError):
            out["thread_count"] = int(status["Threads"])
    with contextlib.suppress(OSError):
        out["fd_count"] = len(os.listdir(f"/proc/{pid}/fd"))
    with contextlib.suppress(OSError, ValueError, IndexError):
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        # after the comm field: utime=fields[11], stime=fields[12] (0-based post-")")
        out["cpu_seconds"] = (int(fields[11]) + int(fields[12])) / float(_CLK_TCK)
    # direct children: scan /proc for tasks whose PPid == pid (bounded, best-effort)
    with contextlib.suppress(OSError):
        children = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            st = _proc_status(int(entry))
            if st.get("PPid") == str(pid):
                children += 1
        out["child_count"] = children
    return out


def sample_storage(runtime: NodeRuntime) -> dict:
    """Database/WAL/artifact-store/log sizes + free disk (bytes)."""
    cfg = runtime.config
    out: dict = {"db_bytes": None, "wal_bytes": None, "artifact_bytes": None,
                 "log_bytes": None, "disk_free_bytes": None}
    db = Path(cfg.database_path).expanduser()
    with contextlib.suppress(OSError):
        if db.is_file():
            out["db_bytes"] = db.stat().st_size
        wal = db.with_name(db.name + "-wal")
        out["wal_bytes"] = wal.stat().st_size if wal.is_file() else 0
    art_dir = cfg.artifacts.state_directory
    if art_dir:
        out["artifact_bytes"] = _dir_size(Path(art_dir).expanduser())
    root = cfg.node_state_root or str(db.parent)
    with contextlib.suppress(OSError):
        logs = Path(root) / "logs"
        out["log_bytes"] = _dir_size(logs) if logs.is_dir() else 0
        out["disk_free_bytes"] = shutil.disk_usage(root).free
    return out


def _dir_size(path: Path, *, cap_files: int = 50000) -> int | None:
    total = 0
    count = 0
    with contextlib.suppress(OSError):
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                count += 1
                if count > cap_files:
                    return total
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(dirpath, f))
        return total
    return None


def sample_state(runtime: NodeRuntime) -> dict:
    """Bounded node-state counts: events, pending outbox, active leases, failed retries."""
    from sqlalchemy import func, select

    from aithernet.state.models import AgentOutboxMessage, Event
    from aithernet.state.repositories import SDRDeviceLeaseRepository
    out: dict = {"event_count": None, "outbox_pending": None, "active_leases": None,
                 "failed_retries": None}
    with contextlib.suppress(Exception), runtime.session_scope() as session:
        out["event_count"] = int(session.scalar(select(func.count()).select_from(Event)) or 0)
        leases = SDRDeviceLeaseRepository(session).counts_by_state(node_id=runtime.config.node_id)
        out["active_leases"] = int(leases.get("active", 0)) + int(leases.get("pending", 0))
        rows = session.execute(
            select(AgentOutboxMessage.status, func.count())
            .group_by(AgentOutboxMessage.status)).all()
        states = {status: int(count) for status, count in rows}
        out["outbox_pending"] = int(states.get("pending", 0)) + int(states.get("in_flight", 0))
        out["failed_retries"] = int(states.get("failed", 0)) + int(states.get("dead_letter", 0))
    return out


def full_sample(runtime: NodeRuntime, *, pid: int | None = None) -> dict:
    """One complete soak sample (process + storage + state)."""
    s = sample_process(pid)
    s.update(sample_storage(runtime))
    s.update(sample_state(runtime))
    return s
