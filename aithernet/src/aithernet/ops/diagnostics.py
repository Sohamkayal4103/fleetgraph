"""Sanitized operational diagnostics bundle (Stage 14A, area 10).

``create_diagnostics_bundle`` writes a single ``.tar.gz`` an operator can share for support. It
contains ONLY sanitized operational facts: application version, migration status, a SQLite
integrity result, component/health status, the configuration STRUCTURE with secrets removed,
peer counts (never keys), mission-state counts, RF backend status, filesystem capacity, and a
bounded tail of recent logs. It NEVER includes database contents, the private identity,
signatures, raw messages, prompts, credentials, RF captures, or environment values.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from aithernet import __version__

if TYPE_CHECKING:
    from aithernet.config.settings import NodeConfig

# Config keys whose VALUES are redacted in the sanitized config structure.
_SECRET_KEYS = {"api_key", "secret", "token", "password", "private_key", "auth", "credential",
                "environment", "env"}
_RECENT_LOG_BYTES = 64 * 1024


def sanitize_config(config: NodeConfig) -> dict:
    """Return the config as a dict with every secret value redacted (structure preserved)."""
    raw = config.model_dump(mode="json")
    return _redact(raw)


def _redact(value):
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if any(s in key.lower() for s in _SECRET_KEYS):
                out[key] = "<redacted>" if val not in (None, "", {}, []) else val
            else:
                out[key] = _redact(val)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _sqlite_integrity(config: NodeConfig) -> str:
    if not config.database_url.startswith("sqlite"):
        return "not_sqlite"
    try:
        conn = sqlite3.connect(config.database_path)
        try:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"error:{type(exc).__name__}"


def _counts(config: NodeConfig) -> dict:
    """Bounded mission/peer/transfer state counts (no row contents, no keys)."""
    from aithernet.state.db import create_db_engine, create_session_factory
    from aithernet.state.repositories import (
        ArtifactTransferRepository,
        InboundRequestRepository,
        MissionExecutionRunRepository,
        MissionRepository,
        PeerRepository,
        RemoteMissionSnapshotRepository,
        SDRDeviceLeaseRepository,
        SDRDeviceRepository,
    )

    engine = create_db_engine(config.database_url)
    try:
        session = create_session_factory(engine)()
        try:
            peers = PeerRepository(session).list(limit=10000)
            return {
                "missions_by_status": MissionRepository(session).counts_by_status(),
                "runs_by_status": MissionExecutionRunRepository(session).counts_by_status(),
                "peers_total": len(peers),
                "peers_trusted": sum(1 for p in peers if p.trust_state == "trusted"),
                "inbound_requests": InboundRequestRepository(session).count(),
                "artifact_transfers": ArtifactTransferRepository(session).counts_by_state(),
                "remote_snapshots": len(RemoteMissionSnapshotRepository(session).list(limit=10000)),
                # Stage 14B: bounded managed-hardware summary (counts/states only — no device
                # paths, serials, capabilities, or lease tokens).
                "hardware_devices_by_status": SDRDeviceRepository(session).counts_by_status(),
                "hardware_leases_by_state": SDRDeviceLeaseRepository(session).counts_by_state(),
                "hardware_providers": len(config.hardware.providers or {}),
            }
        finally:
            session.close()
    finally:
        engine.dispose()


def build_diagnostics(
    config: NodeConfig,
    *,
    config_path: str | None = None,
    health: dict | None = None,
    now_iso: str,
) -> dict:
    """Assemble the sanitized diagnostics dict (no archive). Safe to expose/serialize."""
    from aithernet.ops.preflight import run_preflight
    from aithernet.state.db import create_db_engine
    from aithernet.state.migrations import migration_status

    info: dict = {
        "generated_at": now_iso,
        "app_version": __version__,
        "node_id": config.node_id,
        "node_name": config.node_name,
        "health": health or {},
        "database_integrity": _sqlite_integrity(config),
        "config_structure": sanitize_config(config),
    }
    try:
        engine = create_db_engine(config.database_url)
        try:
            engine_status = migration_status(engine)
        finally:
            engine.dispose()
        info["migration_status"] = {
            "applied": [m for m, _ in engine_status.applied],
            "pending": engine_status.pending,
            "schema_valid": engine_status.validation.ok,
        }
    except Exception as exc:  # noqa: BLE001 — sanitized
        info["migration_status"] = {"error": type(exc).__name__}
    try:
        info["counts"] = _counts(config)
    except Exception as exc:  # noqa: BLE001
        info["counts"] = {"error": type(exc).__name__}
    try:
        info["preflight"] = run_preflight(config, config_path=config_path).to_dict()
    except Exception as exc:  # noqa: BLE001
        info["preflight"] = {"error": type(exc).__name__}
    # Filesystem capacity for the state root (or db dir).
    root = config.node_state_root or str(Path(config.database_path).parent)
    try:
        usage = shutil.disk_usage(root)
        info["filesystem"] = {
            "path": root, "total_bytes": usage.total, "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    except OSError as exc:
        info["filesystem"] = {"error": type(exc).__name__}
    return info


def create_diagnostics_bundle(
    config: NodeConfig,
    *,
    output_dir: str | Path,
    config_path: str | None = None,
    health: dict | None = None,
    now_iso: str,
    log_file: str | Path | None = None,
) -> str:
    """Write a sanitized diagnostics ``.tar.gz`` and return its path."""
    out_dir = Path(output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso.replace(":", "").replace("-", "").replace(".", "")[:15]
    archive = out_dir / f"aithernet-diagnostics-{config.node_id[:8]}-{stamp}.tar.gz"

    info = build_diagnostics(config, config_path=config_path, health=health, now_iso=now_iso)
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        (tmp / "diagnostics.json").write_text(json.dumps(info, indent=2, sort_keys=True))
        # bounded tail of recent logs (never raw payloads — application logs are sanitized).
        if log_file and Path(log_file).is_file():
            data = Path(log_file).read_bytes()[-_RECENT_LOG_BYTES:]
            (tmp / "recent.log").write_bytes(data)
        with tarfile.open(archive, "w:gz") as tar:
            for item in tmp.iterdir():
                tar.add(item, arcname=item.name)
    return str(archive)
