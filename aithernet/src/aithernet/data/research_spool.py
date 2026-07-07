"""beta.10 Part 3: the local append-only research spool — the owner node's source of truth.

When ``data_collection_mode: owner_full`` is enabled on the owner-operated node, all legitimately
observable mission data is written here under a canonical directory tree. Google Drive is the
verified archive/publication destination (Part 4), NOT the live database — the local spool is
authoritative.

Every record is secret-scanned BEFORE it lands in ``raw/``: a record that still carries a
credential after redaction is diverted to ``quarantine/`` and never normalized or uploaded. Writes
are append-only and content-addressed for deterministic dedup.

Hidden chain-of-thought is NOT captured — only provider-exposed reasoning fields and the structured
decision explanations the providers legitimately return (see DecisionRecord capture in the engine).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from aithernet.data.redaction import Redactor, contains_residual_secret

#: Canonical research-spool subdirectories (mandate Part 3).
SPOOL_SUBDIRS = (
    "raw", "normalized", "curated", "evaluation", "artifacts", "rf",
    "snapshots", "manifests", "upload-ledger", "quarantine", "schemas",
)

DATASET_SCHEMA_VERSION = "aithernet.dataset.v1"


def default_spool_root() -> Path:
    """``~/.local/share/aithernet/research`` (honoring XDG_DATA_HOME)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "aithernet" / "research"


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ResearchSpool:
    """Manage the canonical research spool tree and append-only, secret-scanned record writes."""

    def __init__(self, root: Path | None = None, *, pseudo_key: bytes | None = None) -> None:
        self.root = Path(root) if root else default_spool_root()
        # No pseudo_key -> the redactor REMOVES identifiers (fail-closed), which is correct for a
        # single-owner research node; a tenant-derived key can be supplied to pseudonymize instead.
        self._redactor = Redactor(pseudo_key=pseudo_key)

    # -- layout ----------------------------------------------------------------
    def ensure_layout(self) -> dict:
        """Create the spool root + all canonical subdirs (0700). Idempotent."""
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        created = []
        for name in SPOOL_SUBDIRS:
            d = self.root / name
            if not d.exists():
                created.append(name)
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        return {"root": str(self.root), "subdirs": list(SPOOL_SUBDIRS), "created": created}

    def subdir(self, name: str) -> Path:
        if name not in SPOOL_SUBDIRS:
            raise ValueError(f"unknown spool subdir {name!r}")
        return self.root / name

    # -- writes ----------------------------------------------------------------
    def write_raw_record(self, record: dict) -> dict:
        """Redact + secret-scan a mission record, then append it (content-addressed JSON).

        A record that still contains a residual secret after redaction is diverted to
        ``quarantine/`` and is NEVER normalized or uploaded. Returns a receipt with the digest,
        destination ('raw' or 'quarantine'), and path."""
        self.ensure_layout()
        redacted = self._redactor.redact(record).redacted
        quarantined = contains_residual_secret(redacted)
        dest = "quarantine" if quarantined else "raw"
        body = json.dumps(redacted, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(body).hexdigest()
        path = self.subdir(dest) / f"{digest}.json"
        if not path.exists():  # append-only + dedup by content
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
        self._append_ledger("raw-record", {"digest": digest, "dest": dest,
                                           "quarantined": quarantined})
        return {"digest": "sha256:" + digest, "dest": dest, "path": str(path),
                "quarantined": quarantined}

    def write_normalized_record(self, record: dict) -> dict:
        """Secret-scan + write a NORMALIZED dataset record (content-addressed) into ``normalized/``.

        Like :meth:`write_raw_record` but lands in the normalized stage the dataset builders read.
        A residual-secret record is diverted to ``quarantine/`` and never normalized/uploaded."""
        self.ensure_layout()
        redacted = self._redactor.redact(record).redacted
        quarantined = contains_residual_secret(redacted)
        dest = "quarantine" if quarantined else "normalized"
        body = json.dumps(redacted, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(body).hexdigest()
        path = self.subdir(dest) / f"{digest}.json"
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
        self._append_ledger("normalized-record",
                             {"digest": digest, "dest": dest, "quarantined": quarantined})
        return {"digest": "sha256:" + digest, "dest": dest, "path": str(path),
                "quarantined": quarantined}

    def _append_ledger(self, kind: str, detail: dict) -> None:
        """Append one line to the spool's append-only ledger (never secrets)."""
        self.ensure_layout()
        ledger = self.subdir("upload-ledger") / "spool-ledger.jsonl"
        line = json.dumps({"at": _utc_now_iso(), "kind": kind, **detail}, sort_keys=True)
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        os.chmod(ledger, 0o600)

    def _ledger_lines(self) -> list[dict]:
        ledger = self.root / "upload-ledger" / "spool-ledger.jsonl"
        if not ledger.is_file():
            return []
        out: list[dict] = []
        for line in ledger.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # -- recorder bookkeeping (beta.8) -----------------------------------------
    def mark_recorded(self, *, mission_id: str | None, run_id: str | None,
                      digest: str | None, dest: str | None) -> None:
        """Ledger a completed-mission research record (used by the automatic recorder)."""
        self._append_ledger("mission-record", {"mission_id": mission_id, "run_id": run_id,
                                                "digest": digest, "dest": dest})

    def last_recorded_at(self) -> str | None:
        """ISO timestamp of the most recent completed-mission record, or ``None``."""
        for line in reversed(self._ledger_lines()):
            if line.get("kind") == "mission-record":
                return line.get("at")
        return None

    def mission_record_count(self) -> int:
        return sum(1 for line in self._ledger_lines() if line.get("kind") == "mission-record")

    # -- collect / package (beta.8) --------------------------------------------
    def raw_record_digests(self) -> list[str]:
        """Bare hex digests (filename stems) of every record currently in ``raw/``."""
        d = self.root / "raw"
        return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []

    def read_raw_record(self, digest: str) -> dict | None:
        p = self.root / "raw" / f"{digest}.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None

    def collected_digests(self) -> set[str]:
        """Raw digests already folded into a snapshot manifest (so collect is idempotent)."""
        out: set[str] = set()
        md = self.root / "manifests"
        if md.is_dir():
            for p in md.glob("*.json"):
                try:
                    out.update(json.loads(p.read_text()).get("raw_digests", []))
                except json.JSONDecodeError:
                    continue
        return out

    def pending_for_collection(self) -> list[str]:
        collected = self.collected_digests()
        return [h for h in self.raw_record_digests() if h not in collected]

    def write_snapshot_package(self, package: dict) -> dict:
        """Write a content-addressed snapshot package (+ its manifest) for upload.

        The package is secret-scanned again as defence-in-depth; if a residual secret is present it
        is diverted to ``quarantine/`` and NO uploadable snapshot/manifest is produced."""
        self.ensure_layout()
        body = json.dumps(package, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(body).hexdigest()
        if contains_residual_secret(package):
            qpath = self.subdir("quarantine") / f"pkg-{digest}.json"
            if not qpath.exists():
                fd = os.open(qpath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(body)
            self._append_ledger("snapshot-quarantined", {"digest": digest})
            return {"digest": "sha256:" + digest, "dest": "quarantine", "path": str(qpath),
                    "quarantined": True}
        snap = self.subdir("snapshots") / f"{digest}.json"
        if not snap.exists():
            fd = os.open(snap, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
        manifest = {
            "schema": "aithernet.dataset.manifest.v1",
            "created_at": _utc_now_iso(),
            "package_name": f"{digest}.json",
            "package_sha256": "sha256:" + digest,
            "byte_size": len(body),
            "count": package.get("count"),
            "raw_digests": list(package.get("raw_digests", [])),
        }
        mpath = self.subdir("manifests") / f"{digest}.json"
        mbody = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        if not mpath.exists():
            fd = os.open(mpath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(mbody)
        self._append_ledger("collected", {"digest": digest, "count": manifest["count"],
                                          "raw_digests_n": len(manifest["raw_digests"])})
        return {"digest": "sha256:" + digest, "dest": "snapshots", "path": str(snap),
                "manifest_path": str(mpath), "count": manifest["count"], "quarantined": False}

    # -- sync / upload bookkeeping (beta.8) ------------------------------------
    def snapshot_digests(self) -> list[str]:
        d = self.root / "snapshots"
        return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []

    def read_snapshot_bytes(self, digest: str) -> bytes | None:
        p = self.root / "snapshots" / f"{digest}.json"
        return p.read_bytes() if p.is_file() else None

    def uploaded_digests(self) -> set[str]:
        return {line["digest"] for line in self._ledger_lines()
                if line.get("kind") == "uploaded" and line.get("digest")}

    def mark_uploaded(self, *, digest: str, file_id: str | None = None,
                      byte_size: int | None = None) -> None:
        self._append_ledger("uploaded", {"digest": digest, "file_id": file_id,
                                         "byte_size": byte_size})

    def pending_upload(self) -> list[str]:
        uploaded = self.uploaded_digests()
        return [h for h in self.snapshot_digests() if h not in uploaded]

    def recorder_status(self) -> dict:
        """Recorder-facing status fields for `aithernet data status` (never secrets/contents)."""
        c = self.counts() if self.root.is_dir() else {}
        return {
            "mission_records": self.mission_record_count(),
            "last_recorded_at": self.last_recorded_at(),
            "pending_collection": len(self.pending_for_collection()) if self.root.is_dir() else 0,
            "pending_upload": len(self.pending_upload()) if self.root.is_dir() else 0,
            "snapshots": c.get("snapshots", 0),
            "manifests": c.get("manifests", 0),
        }

    # -- status ----------------------------------------------------------------
    def counts(self) -> dict:
        """Per-stage record counts for `aithernet data status` (no record contents)."""
        out = {}
        for name in SPOOL_SUBDIRS:
            d = self.root / name
            out[name] = sum(1 for _ in d.glob("*.json")) if d.is_dir() else 0
        return out

    def status(self) -> dict:
        """A bounded, secret-free spool health snapshot."""
        exists = self.root.is_dir()
        c = self.counts() if exists else {}
        return {
            "root": str(self.root),
            "exists": exists,
            "healthy": exists and all((self.root / s).is_dir() for s in SPOOL_SUBDIRS),
            "counts": c,
            "pending_raw": c.get("raw", 0),
            "quarantined": c.get("quarantine", 0),
            "snapshots": c.get("snapshots", 0),
            "schema_version": DATASET_SCHEMA_VERSION,
        }
