"""Read-only operations endpoints (Stage 14A, area 11/12).

Bounded, sanitized operational metadata for the dashboard Operations section: version,
migration status, readiness/liveness, storage usage, backup freshness, last preflight result,
and component startup/degraded state. DESTRUCTIVE actions (restore, upgrade apply, rollback) are
deliberately NOT exposed over the API — they are operator CLI actions only. Nothing here returns
secrets, keys, signatures, prompts, database contents, or environment values.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends

from aithernet import __version__
from aithernet.api.app import get_runtime
from aithernet.orchestrator.runtime import NodeRuntime

router = APIRouter(tags=["operations"])


def _backup_dir(runtime: NodeRuntime) -> str:
    cfg = runtime.config
    if cfg.backup_directory:
        return cfg.backup_directory
    if cfg.node_state_root:
        from aithernet.ops.paths import NodePaths
        return str(NodePaths.from_root(cfg.node_state_root).backups_dir)
    return str(Path(cfg.database_path).expanduser().resolve().parent / "backups")


@router.get("/ops/overview")
def ops_overview(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """Compact Operations snapshot: version, readiness, migrations, storage, backup freshness."""
    from aithernet.ops.backup import list_backups
    from aithernet.state.migrations import migration_status

    readiness = runtime.readiness.snapshot()
    migrations: dict = {}
    try:
        engine = runtime.engine
        status = migration_status(engine)
        migrations = {"applied": len(status.applied), "pending": status.pending,
                      "schema_valid": status.validation.ok}
    except Exception as exc:  # noqa: BLE001 — sanitized
        migrations = {"error": type(exc).__name__}

    backups = list_backups(_backup_dir(runtime))
    latest = backups[0] if backups else None

    storage: dict = {}
    root = runtime.config.node_state_root or str(Path(runtime.config.database_path).parent)
    try:
        usage = shutil.disk_usage(root)
        storage = {"path": root, "total_bytes": usage.total, "used_bytes": usage.used,
                   "free_bytes": usage.free,
                   "free_pct": round(100 * usage.free / usage.total, 1) if usage.total else None}
    except OSError as exc:
        storage = {"error": type(exc).__name__}

    return {
        "version": __version__,
        "node_id": runtime.config.node_id,
        "node_name": runtime.config.node_name,
        "started_at": runtime.started_at.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "readiness": readiness,
        "migrations": migrations,
        "storage": storage,
        "backups": {
            "count": len(backups),
            "latest_created_at": latest.get("created_at") if latest else None,
            "latest_version": latest.get("app_version") if latest else None,
        },
        "state_root_configured": bool(runtime.config.node_state_root),
    }


@router.get("/ops/preflight")
def ops_preflight(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """Run configuration + environment preflight against the running node's config."""
    from aithernet.ops.preflight import run_preflight
    return run_preflight(runtime.config).to_dict()


@router.get("/ops/backups")
def ops_backups(runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    """List node backups with bounded manifest summaries (no contents, no keys)."""
    from aithernet.ops.backup import list_backups
    return list_backups(_backup_dir(runtime))


@router.get("/ops/diagnostics")
def ops_diagnostics(runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """Return the sanitized diagnostics structure (no archive written; no secrets/contents)."""
    from aithernet.ops.diagnostics import build_diagnostics
    return build_diagnostics(
        runtime.config, health=runtime.readiness.snapshot(),
        now_iso=datetime.now(UTC).isoformat(),
    )
