"""Workspace-containment utilities for backend artifacts (Stage 13A.5, Part J).

A backend (e.g. Marconi) writes artifacts beneath its configured workspace and may report
paths in tool results. Aithernet NEVER trusts an arbitrary absolute path: every candidate is
resolved (following symlinks) and verified to stay strictly inside the configured workspace.
Traversal (``..``) and symlink escapes are rejected. Only a workspace-RELATIVE reference is
stored; the bytes are never copied into SQLite and unrelated files are never exposed.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from aithernet.rf.contracts import RFArtifactError

#: Only hash files up to this size (avoid reading huge captures into memory).
_MAX_HASH_BYTES = 8 * 1024 * 1024

#: Extension -> (artifact_kind, media_type).
_KIND_BY_EXT: dict[str, tuple[str, str | None]] = {
    ".png": ("plot", "image/png"),
    ".jpg": ("plot", "image/jpeg"),
    ".jpeg": ("plot", "image/jpeg"),
    ".svg": ("plot", "image/svg+xml"),
    ".cf32": ("capture", "application/octet-stream"),
    ".sigmf-data": ("capture", "application/octet-stream"),
    ".sigmf-meta": ("capture_meta", "application/json"),
    ".wav": ("capture", "audio/wav"),
    ".npy": ("data", "application/octet-stream"),
    ".json": ("data", "application/json"),
    ".grc": ("flowgraph", "application/xml"),
    ".py": ("flowgraph", "text/x-python"),
    ".csv": ("data", "text/csv"),
}


def safe_relative_path(workspace: str | None, candidate: str) -> str:
    """Resolve ``candidate`` and return its workspace-relative path, or raise.

    ``candidate`` may be absolute or relative. The resolved real path (symlinks followed)
    must lie strictly inside the resolved workspace; otherwise :class:`RFArtifactError` is
    raised (traversal / symlink escape / unrelated file). The file need not exist yet.
    """
    if not workspace:
        raise RFArtifactError("Backend has no configured workspace; cannot index artifacts.")
    if not candidate or not str(candidate).strip():
        raise RFArtifactError("Empty artifact path.")
    ws_real = os.path.realpath(workspace)
    raw = Path(candidate)
    joined = raw if raw.is_absolute() else Path(ws_real) / raw
    resolved = os.path.realpath(str(joined))
    # Containment: the resolved real path must be the workspace itself or strictly beneath it.
    ws_prefix = ws_real.rstrip(os.sep) + os.sep
    if resolved != ws_real and not resolved.startswith(ws_prefix):
        raise RFArtifactError(
            "Artifact path resolves outside the configured backend workspace.",
            code="unsafe_artifact_path",
        )
    rel = os.path.relpath(resolved, ws_real)
    if rel.startswith("..") or os.path.isabs(rel):
        raise RFArtifactError("Artifact path escapes the workspace.", code="unsafe_artifact_path")
    return rel


def classify(relative_path: str) -> tuple[str, str | None]:
    """Return ``(artifact_kind, media_type)`` inferred from the file extension."""
    ext = Path(relative_path).suffix.lower()
    # SigMF double extensions (.sigmf-data / .sigmf-meta).
    name = relative_path.lower()
    if name.endswith(".sigmf-meta"):
        return _KIND_BY_EXT[".sigmf-meta"]
    if name.endswith(".sigmf-data"):
        return _KIND_BY_EXT[".sigmf-data"]
    return _KIND_BY_EXT.get(ext, ("unknown", None))


def stat_and_hash(workspace: str, relative_path: str) -> tuple[int | None, str | None]:
    """Return ``(size_bytes, content_hash)`` for an existing artifact, bounded and safe.

    Returns ``(None, None)`` when the file does not exist yet. Only files up to a bounded
    size are hashed (large binaries are referenced, never read whole into memory).
    """
    abs_path = os.path.join(os.path.realpath(workspace), relative_path)
    if not os.path.isfile(abs_path):
        return None, None
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        return None, None
    if size > _MAX_HASH_BYTES:
        return size, None
    try:
        digest = hashlib.sha256()
        with open(abs_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return size, f"sha256:{digest.hexdigest()}"
    except OSError:
        return size, None
