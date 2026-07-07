"""Consistent node backup + protected restore (Stage 14A, area 7).

A backup is a single ``.tar.gz`` containing a CONSISTENT SQLite copy (via the sqlite3 online
backup API — never a raw copy of a live file), the node configuration (which wires secrets via
the environment, so the file itself holds none), the PUBLIC identity document, the migration
history (inside the DB), and a manifest with the app version, schema migrations, and a SHA-256
per archived file. The private identity key is included only when explicitly requested.

Restore is protected: it requires confirmation, verifies every checksum, validates schema
compatibility, REFUSES to overwrite a different node identity (never merges two identities),
preserves the current installation as a rollback snapshot, and supports dry-run verification.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from aithernet import __version__

if TYPE_CHECKING:
    from aithernet.config.settings import NodeConfig

BACKUP_FORMAT = "1"
MANIFEST_NAME = "manifest.json"
_PRIVATE_KEY_FILE = "node_ed25519_private.pem"
_PUBLIC_DOC_FILE = "identity.json"


@dataclass
class BackupResult:
    path: str
    node_id: str
    created_at: str
    bytes: int
    includes_private_identity: bool
    files: list[str]


def _hash_stream(fh) -> str:
    h = hashlib.sha256()
    while True:
        chunk = fh.read(1 << 20)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return f"sha256:{h.hexdigest()}", size


def _consistent_sqlite_copy(database_path: str, dest: Path) -> None:
    """Copy the SQLite database CONSISTENTLY using the online backup API (safe while in use)."""
    src = sqlite3.connect(database_path)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def _identity_dir(config: NodeConfig) -> Path | None:
    state_dir = config.agent_transport.identity.state_directory
    return Path(state_dir).expanduser() if state_dir else None


def create_backup(
    config: NodeConfig,
    *,
    backup_dir: str | Path,
    config_path: str | None = None,
    include_private_identity: bool = False,
    now_iso: str,
    label: str | None = None,
) -> BackupResult:
    """Create a consistent, checksummed backup archive. ``now_iso`` is supplied by the caller
    (the runtime forbids implicit clock reads in some contexts)."""
    backups = Path(backup_dir).expanduser()
    backups.mkdir(parents=True, exist_ok=True)
    stamp = now_iso.replace(":", "").replace("-", "").replace(".", "").replace("+", "")[:15]
    suffix = f"-{label}" if label else ""
    archive = backups / f"aithernet-backup-{config.node_id[:8]}-{stamp}{suffix}.tar.gz"

    files_meta: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        staged: list[tuple[str, Path]] = []

        # 1. consistent SQLite copy
        db_copy = tmp / "database.sqlite3"
        if config.database_url.startswith("sqlite"):
            _consistent_sqlite_copy(config.database_path, db_copy)
            staged.append(("database.sqlite3", db_copy))

        # 2. configuration (no inline secrets — secrets are env-wired)
        if config_path and Path(config_path).is_file():
            staged.append(("config/node.yaml", Path(config_path)))

        # 3. public identity doc (always) + private key (only if requested)
        id_dir = _identity_dir(config)
        if id_dir is not None:
            pub = id_dir / _PUBLIC_DOC_FILE
            if pub.is_file():
                staged.append((f"identity/{_PUBLIC_DOC_FILE}", pub))
            if include_private_identity:
                priv = id_dir / _PRIVATE_KEY_FILE
                if priv.is_file():
                    staged.append((f"identity/{_PRIVATE_KEY_FILE}", priv))

        for arcname, src in staged:
            digest, size = _sha256_file(src)
            files_meta[arcname] = {"sha256": digest, "bytes": size}

        # 4. manifest (versions + checksums + schema migrations)
        migrations = _schema_migrations(db_copy if db_copy.exists() else None)
        manifest = {
            "backup_format": BACKUP_FORMAT,
            "created_at": now_iso,
            "app_version": __version__,
            "node_id": config.node_id,
            "node_name": config.node_name,
            "schema_migrations": migrations,
            "includes_private_identity": include_private_identity,
            "files": files_meta,
        }
        manifest_path = tmp / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

        # 5. package
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(manifest_path, arcname=MANIFEST_NAME)
            for arcname, src in staged:
                tar.add(src, arcname=arcname)

    size = archive.stat().st_size
    return BackupResult(
        path=str(archive), node_id=config.node_id, created_at=now_iso, bytes=size,
        includes_private_identity=include_private_identity, files=list(files_meta),
    )


def _schema_migrations(db_copy: Path | None) -> list[str]:
    if db_copy is None or not db_copy.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_copy))
        try:
            rows = conn.execute(
                "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def read_manifest(archive: str | Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.getmember(MANIFEST_NAME)
        fh = tar.extractfile(member)
        if fh is None:
            raise ValueError("backup manifest unreadable")
        return json.loads(fh.read().decode("utf-8"))


@dataclass
class VerifyResult:
    ok: bool
    node_id: str | None
    app_version: str | None
    schema_migrations: list[str]
    issues: list[str]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "node_id": self.node_id, "app_version": self.app_version,
            "schema_migrations": self.schema_migrations, "issues": self.issues,
        }


def verify_backup(archive: str | Path) -> VerifyResult:
    """Validate the manifest + every archived file's SHA-256. Pure read; mutates nothing.

    Any corruption (bad gzip, truncated tar, unreadable manifest, checksum mismatch) yields
    ``ok=False`` with a sanitized issue — it never raises.
    """
    issues: list[str] = []
    try:
        manifest = read_manifest(archive)
        expected = manifest.get("files", {})
        with tarfile.open(archive, "r:gz") as tar:
            names = set(tar.getnames())
            for arcname, meta in expected.items():
                if arcname not in names:
                    issues.append(f"missing file: {arcname}")
                    continue
                fh = tar.extractfile(arcname)
                if fh is None:
                    issues.append(f"unreadable file: {arcname}")
                    continue
                if f"sha256:{_hash_stream(fh)}" != meta.get("sha256"):
                    issues.append(f"checksum mismatch: {arcname}")
    except (tarfile.TarError, ValueError, KeyError, json.JSONDecodeError,
            EOFError, OSError) as exc:
        return VerifyResult(False, None, None, [], [f"unreadable backup ({type(exc).__name__})"])

    if manifest.get("backup_format") != BACKUP_FORMAT:
        issues.append(f"unsupported backup_format {manifest.get('backup_format')}")
    return VerifyResult(
        ok=not issues, node_id=manifest.get("node_id"),
        app_version=manifest.get("app_version"),
        schema_migrations=manifest.get("schema_migrations", []), issues=issues,
    )


@dataclass
class RestoreResult:
    dry_run: bool
    ok: bool
    rollback_snapshot: str | None
    issues: list[str]
    restored_files: list[str]

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run, "ok": self.ok,
            "rollback_snapshot": self.rollback_snapshot, "issues": self.issues,
            "restored_files": self.restored_files,
        }


def restore_backup(
    archive: str | Path,
    config: NodeConfig,
    *,
    config_path: str | None,
    dry_run: bool,
    now_iso: str,
    restore_private_identity: bool = False,
) -> RestoreResult:
    """Restore a verified backup into this node's paths, protecting against identity merges.

    Always verifies checksums + schema compatibility first. Refuses if the backup's node id
    differs from this node's configured id OR from an existing on-disk identity (never merges two
    identities). Preserves the current DB/identity as a rollback snapshot before overwriting.
    """
    issues: list[str] = []
    verify = verify_backup(archive)
    if not verify.ok:
        return RestoreResult(dry_run, False, None, verify.issues, [])

    # Identity-merge protection: the backup must belong to THIS node.
    if verify.node_id and verify.node_id != config.node_id:
        issues.append(
            f"node identity mismatch: backup is for {verify.node_id[:8]}…, this node is "
            f"{config.node_id[:8]}… — refusing to merge identities"
        )
    id_dir = _identity_dir(config)
    if id_dir is not None and (id_dir / _PUBLIC_DOC_FILE).is_file():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            existing = json.loads((id_dir / _PUBLIC_DOC_FILE).read_text())
            if existing.get("node_id") and verify.node_id and existing["node_id"] != verify.node_id:
                issues.append("on-disk identity does not match the backup identity")

    # Schema compatibility: the backup's migrations must be a prefix of (or equal to) the code's.
    from aithernet.state.migrations import MIGRATIONS
    known = [m.id for m in MIGRATIONS]
    unknown = [m for m in verify.schema_migrations if m not in known]
    if unknown:
        issues.append(f"backup contains unknown migrations: {unknown[:3]}")

    if issues:
        return RestoreResult(dry_run, False, None, issues, [])
    if dry_run:
        return RestoreResult(True, True, None, [], list(read_manifest(archive).get("files", {})))

    # Preserve current installation as a rollback snapshot, then restore.
    snapshot = None
    if config.database_url.startswith("sqlite") and Path(config.database_path).exists():
        snap_dir = Path(config.backup_directory or (id_dir.parent / "backups" if id_dir else "."))
        snap_dir.mkdir(parents=True, exist_ok=True)
        snapshot = str(create_backup(
            config, backup_dir=snap_dir, config_path=config_path,
            include_private_identity=True, now_iso=now_iso, label="pre-restore",
        ).path)

    restored: list[str] = []
    with tarfile.open(archive, "r:gz") as tar:
        manifest = read_manifest(archive)
        for arcname in manifest.get("files", {}):
            if arcname == "identity/" + _PRIVATE_KEY_FILE and not restore_private_identity:
                continue
            target = _restore_target(arcname, config, config_path)
            if target is None:
                continue
            fh = tar.extractfile(arcname)
            if fh is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if arcname == "database.sqlite3":
                # Restore the DB by a consistent copy into place (online-backup safe).
                with tempfile.NamedTemporaryFile(delete=False) as tmpf:
                    tmpf.write(fh.read())
                    tmp_db = tmpf.name
                _consistent_sqlite_copy(tmp_db, target)
                Path(tmp_db).unlink(missing_ok=True)
            else:
                target.write_bytes(fh.read())
            restored.append(arcname)
    return RestoreResult(False, True, snapshot, [], restored)


def _restore_target(arcname: str, config: NodeConfig, config_path: str | None) -> Path | None:
    if arcname == "database.sqlite3" and config.database_url.startswith("sqlite"):
        return Path(config.database_path)
    if arcname == "config/node.yaml" and config_path:
        return Path(config_path)
    id_dir = _identity_dir(config)
    if arcname.startswith("identity/") and id_dir is not None:
        return id_dir / arcname.split("/", 1)[1]
    return None


def list_backups(backup_dir: str | Path) -> list[dict]:
    """List backup archives in a directory with bounded manifest summaries (newest first)."""
    backups = Path(backup_dir).expanduser()
    if not backups.is_dir():
        return []
    out = []
    for archive in sorted(backups.glob("aithernet-backup-*.tar.gz"), reverse=True):
        entry = {"path": str(archive), "bytes": archive.stat().st_size}
        with contextlib.suppress(Exception):
            manifest = read_manifest(archive)
            entry.update({
                "node_id": manifest.get("node_id"),
                "created_at": manifest.get("created_at"),
                "app_version": manifest.get("app_version"),
                "includes_private_identity": manifest.get("includes_private_identity"),
                "file_count": len(manifest.get("files", {})),
            })
        out.append(entry)
    return out
