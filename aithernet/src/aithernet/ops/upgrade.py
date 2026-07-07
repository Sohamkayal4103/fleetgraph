"""Safe upgrade / rollback workflow (Stage 14A, area 8).

The workflow never self-updates code from the network — it operates on the already-installed
code and the node's persistent state:

    preflight  — capture current version + migration state, run config/env preflight, and record
                 (never modify) the external RF backend revisions.
    apply      — create a pre-upgrade backup (the rollback point), run pending migrations, then
                 health-check the result; stop and keep the backup on any failure.
    verify     — confirm the live schema validates and no migration is pending.
    rollback   — restore the most recent pre-upgrade backup snapshot.

It never automatically downgrades a migrated database; rollback is an explicit restore of the
captured snapshot.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aithernet import __version__

if TYPE_CHECKING:
    from aithernet.config.settings import NodeConfig


def _engine(config: NodeConfig):
    from aithernet.state.db import create_db_engine
    return create_db_engine(config.database_url)


def _migration_state(config: NodeConfig) -> dict:
    from aithernet.state.migrations import migration_status
    engine = _engine(config)
    try:
        status = migration_status(engine)
        return {
            "applied": [m for m, _ in status.applied],
            "pending": status.pending,
            "schema_valid": status.validation.ok,
        }
    finally:
        engine.dispose()


def _external_backend_revisions(config: NodeConfig) -> list[dict]:
    """Record (NEVER modify) external RF backend revisions for the upgrade record."""
    out = []
    for backend_id, backend in (config.rf_backends.backends or {}).items():
        cwd = getattr(backend, "cwd", None) or getattr(backend, "workspace", None)
        revision = None
        if cwd:
            pinned = Path(cwd).expanduser().parent / f"{Path(cwd).name}-pinned-commit.txt"
            if pinned.is_file():
                revision = pinned.read_text().strip()[:64]
        out.append({"backend_id": backend_id, "pinned_revision": revision, "modified": False})
    return out


#: Preflight check categories that genuinely gate a *database upgrade*. Applying an upgrade runs
#: schema migrations against the node database, so its readiness depends on the database being
#: reachable and in a known, migratable state, and on a valid node identity — NOT on runtime
#: serve-readiness checks (an upgrade is normally run against a *running* node, so its API port is
#: legitimately bound; external executables / RF backends / hardware likewise do not affect whether
#: pending migrations can be applied). Gating ``ready_to_apply`` on these categories lets a live
#: node be preflighted while a malformed/unreachable database still fails closed.
_UPGRADE_BLOCKING_CATEGORIES = frozenset({"database", "config"})


def upgrade_preflight(config: NodeConfig, *, config_path: str | None = None) -> dict:
    """Capture version + migration state, run preflight, and record external backend revisions."""
    from aithernet.ops.preflight import FAIL, run_preflight
    report = run_preflight(config, config_path=config_path)
    state = _migration_state(config)
    blocking = [
        c.name for c in report.checks
        if c.status == FAIL and c.category in _UPGRADE_BLOCKING_CATEGORIES
    ]
    return {
        "app_version": __version__,
        "migration_state": state,
        "preflight": report.to_dict(),
        "external_backends": _external_backend_revisions(config),
        # Read-only: ready iff no database/config check failed. Non-blocking runtime failures
        # (e.g. port already in use because the node is running) are still surfaced in
        # ``preflight`` for the operator but do not block applying the migration set.
        "ready_to_apply": not blocking,
        "blocking_checks": blocking,
    }


def upgrade_apply(
    config: NodeConfig, *, config_path: str | None, backup_dir: str, now_iso: str
) -> dict:
    """Back up, migrate, then health-check. Stops on any validation failure (backup retained)."""
    from aithernet.ops.backup import create_backup
    from aithernet.state.migrations import run_migrations, validate_schema

    before = _migration_state(config)
    backup = create_backup(
        config, backup_dir=backup_dir, config_path=config_path,
        include_private_identity=True, now_iso=now_iso, label="pre-upgrade",
    )

    engine = _engine(config)
    try:
        report = run_migrations(engine)
        validation = validate_schema(engine)
    except Exception as exc:  # noqa: BLE001 — sanitized; backup retained for rollback
        engine.dispose()
        return {
            "ok": False, "error_type": type(exc).__name__,
            "rollback_backup": backup.path, "applied_migrations": [],
            "message": "migration failed — restore the rollback backup with "
                       "'aithernet upgrade rollback'",
        }
    finally:
        engine.dispose()

    ok = validation.ok
    return {
        "ok": ok,
        "app_version": __version__,
        "rollback_backup": backup.path,
        "migrations_before": before["applied"],
        "applied_migrations": report.applied_now,
        "schema_valid": validation.ok,
        "message": ("upgrade applied" if ok
                    else "schema invalid after migration — consider rollback"),
    }


def upgrade_verify(config: NodeConfig) -> dict:
    """Confirm the live schema validates and no migration is pending."""
    state = _migration_state(config)
    healthy = state["schema_valid"] and not state["pending"]
    return {"ok": healthy, "app_version": __version__, "migration_state": state}


def upgrade_rollback(
    config: NodeConfig, *, backup_path: str, config_path: str | None, now_iso: str
) -> dict:
    """Restore a captured pre-upgrade backup snapshot (explicit; never an auto-downgrade)."""
    from aithernet.ops.backup import restore_backup
    result = restore_backup(
        backup_path, config, config_path=config_path, dry_run=False, now_iso=now_iso,
        restore_private_identity=True,
    )
    return {"ok": result.ok, "issues": result.issues, "restored_files": result.restored_files,
            "rollback_snapshot": result.rollback_snapshot}
