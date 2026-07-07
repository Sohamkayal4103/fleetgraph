"""Managed content-addressed artifact store (Stage 13D, Part D).

Layout under the configured state root:

    artifact-store/
    ├── objects/sha256/<aa>/<digest>      # final, verified, content-addressed objects
    ├── partial/<transfer_id>             # in-progress receive buffers
    ├── quarantine/<name>                 # corrupt/mismatched bytes, never served
    └── manifests/                        # reserved for future sidecar manifests

Invariants: bytes never enter SQLite; final objects are committed by atomic rename ONLY after
the streamed content matches the expected SHA-256 and byte size; identical content is
deduplicated; path traversal, symlink escape, and special files (device/FIFO/socket) are
rejected; per-artifact, per-peer, total-store, and minimum-free-space limits are enforced;
pinned objects are never garbage-collected.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass

_HEX = "0123456789abcdef"


class ArtifactStoreError(Exception):
    """A sanitized artifact-store failure. ``code`` classifies it for the API/worker."""

    code: str = "artifact_store_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


@dataclass
class StoredObject:
    """The result of committing content to the store."""

    digest: str  # "sha256:<hex>"
    size_bytes: int
    relative_object_path: str  # store-relative, e.g. objects/sha256/aa/sha256_<hex>
    deduplicated: bool


def _validate_digest(digest: str) -> str:
    """Return the lowercase hex body of a ``sha256:<hex>`` digest, or raise."""
    if not digest or not digest.startswith("sha256:"):
        raise ArtifactStoreError("Digest must be 'sha256:<hex>'.", code="bad_digest")
    body = digest.split(":", 1)[1].strip().lower()
    if len(body) != 64 or any(c not in _HEX for c in body):
        raise ArtifactStoreError("Malformed sha256 digest.", code="bad_digest")
    return body


def _reject_special_file(path: str) -> None:
    """Reject device files, FIFOs, sockets, and symlinks (lstat — do not follow)."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise ArtifactStoreError(
            "Source artifact is not accessible.", code="source_missing"
        ) from exc
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        raise ArtifactStoreError("Source is a symlink; refused.", code="unsafe_source")
    if not stat.S_ISREG(mode):
        raise ArtifactStoreError(
            "Source is not a regular file (device/FIFO/socket); refused.", code="unsafe_source"
        )


class ArtifactStore:
    """Filesystem-backed, content-addressed object store. No SQLite, no network."""

    def __init__(self, root: str, *, total_quota_bytes: int, max_artifact_bytes: int,
                 minimum_free_bytes: int) -> None:
        self.root = os.path.realpath(root)
        self.total_quota_bytes = total_quota_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self.minimum_free_bytes = minimum_free_bytes
        for sub in ("objects/sha256", "partial", "quarantine", "manifests"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)

    # -- paths (all confined to the store root) ----------------------------------

    def _object_rel(self, hex_body: str) -> str:
        return f"objects/sha256/{hex_body[:2]}/sha256_{hex_body}"

    def object_abs(self, digest: str) -> str:
        return os.path.join(self.root, self._object_rel(_validate_digest(digest)))

    def _confined(self, relative: str) -> str:
        """Resolve a store-relative path and confirm it stays strictly inside the root."""
        resolved = os.path.realpath(os.path.join(self.root, relative))
        prefix = self.root.rstrip(os.sep) + os.sep
        if resolved != self.root and not resolved.startswith(prefix):
            raise ArtifactStoreError("Path escapes the artifact store.", code="path_escape")
        return resolved

    def partial_abs(self, transfer_id: str) -> str:
        if not transfer_id or "/" in transfer_id or "\\" in transfer_id or ".." in transfer_id:
            raise ArtifactStoreError("Invalid transfer id.", code="bad_transfer_id")
        return self._confined(os.path.join("partial", transfer_id))

    def has_object(self, digest: str) -> bool:
        return os.path.isfile(self.object_abs(digest))

    # -- capacity / quota --------------------------------------------------------

    def total_size_bytes(self) -> int:
        total = 0
        objects_root = os.path.join(self.root, "objects")
        for dirpath, _dirs, files in os.walk(objects_root):
            for name in files:
                with _suppress_os():
                    total += os.path.getsize(os.path.join(dirpath, name))
        return total

    def free_bytes(self) -> int:
        with _suppress_os():
            return shutil.disk_usage(self.root).free
        return 0

    def _check_capacity(self, incoming_bytes: int) -> None:
        if incoming_bytes > self.max_artifact_bytes:
            raise ArtifactStoreError(
                "Artifact exceeds the per-artifact size limit.", code="too_large"
            )
        if self.total_size_bytes() + incoming_bytes > self.total_quota_bytes:
            raise ArtifactStoreError("Total store quota exceeded.", code="store_quota")
        if self.free_bytes() - incoming_bytes < self.minimum_free_bytes:
            raise ArtifactStoreError(
                "Insufficient free disk space for this transfer.", code="low_disk"
            )

    def precheck_capacity(self, incoming_bytes: int) -> None:
        """Public capacity gate used before authorizing/starting a transfer."""
        self._check_capacity(incoming_bytes)

    # -- import (stream-copy a local source into the store) ----------------------

    def import_file(self, source_path: str, *, expected_digest: str | None = None,
                    expected_size: int | None = None) -> StoredObject:
        """Stream-copy a regular local file into the store, hashing as we go.

        Rejects symlinks/special files. If ``expected_digest``/``expected_size`` are given they
        are verified; mismatches never commit. Identical content is deduplicated. The original
        source file is left untouched (the backend's workspace copy stays where it was created).
        """
        _reject_special_file(source_path)
        size = os.path.getsize(source_path)
        if expected_size is not None and size != expected_size:
            raise ArtifactStoreError("Source size does not match expected.", code="size_mismatch")
        self._check_capacity(size)
        os.makedirs(os.path.join(self.root, "partial"), exist_ok=True)
        tmp_rel = os.path.join("partial", f"import_{os.getpid()}_{_rand_token(source_path, size)}")
        tmp_abs = self._confined(tmp_rel)
        digest = hashlib.sha256()
        written = 0
        try:
            with open(source_path, "rb") as src, open(tmp_abs, "wb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            hex_body = digest.hexdigest()
            if expected_digest is not None and _validate_digest(expected_digest) != hex_body:
                self._quarantine(tmp_abs, hex_body)
                raise ArtifactStoreError(
                    "Imported content digest mismatch.", code="digest_mismatch"
                )
            return self._commit(tmp_abs, hex_body, written)
        finally:
            with _suppress_os():
                if os.path.exists(tmp_abs):
                    os.remove(tmp_abs)

    # -- receive (write streamed chunks into a partial, then verify+commit) ------

    def partial_size(self, transfer_id: str) -> int:
        path = self.partial_abs(transfer_id)
        with _suppress_os():
            return os.path.getsize(path)
        return 0

    def append_chunk(self, transfer_id: str, *, offset: int, data: bytes,
                     max_total_bytes: int) -> int:
        """Append ``data`` at ``offset`` to the partial file. Returns the new partial size.

        Writes are idempotent for re-delivered chunks at a known offset (the same bytes land in
        the same place). Refuses to grow past ``max_total_bytes`` (the expected artifact size).
        """
        path = self.partial_abs(transfer_id)
        current = self.partial_size(transfer_id)
        if offset > current:
            raise ArtifactStoreError("Chunk offset past end of partial (gap).", code="bad_offset")
        if offset + len(data) > max_total_bytes:
            raise ArtifactStoreError("Chunk would exceed expected size.", code="size_overflow")
        flags = os.O_WRONLY | os.O_CREAT
        fd = os.open(path, flags, 0o600)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            os.write(fd, data)
        finally:
            os.close(fd)
        return self.partial_size(transfer_id)

    def verify_and_commit_partial(self, transfer_id: str, *, expected_digest: str,
                                  expected_size: int) -> StoredObject:
        """Hash the partial, verify size+digest, and atomically commit — or quarantine.

        A size or digest mismatch quarantines the bytes and raises; it NEVER publishes the
        object. On success the content-addressed object is committed by atomic rename.
        """
        want_hex = _validate_digest(expected_digest)
        path = self.partial_abs(transfer_id)
        if not os.path.isfile(path):
            raise ArtifactStoreError("No partial data to verify.", code="no_partial")
        size = os.path.getsize(path)
        if size != expected_size:
            self._quarantine(path, f"size_{transfer_id}")
            raise ArtifactStoreError("Final size mismatch; quarantined.", code="size_mismatch")
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != want_hex:
            self._quarantine(path, f"digest_{transfer_id}")
            raise ArtifactStoreError("Final digest mismatch; quarantined.", code="digest_mismatch")
        return self._commit(path, want_hex, size)

    def discard_partial(self, transfer_id: str) -> None:
        with _suppress_os():
            os.remove(self.partial_abs(transfer_id))

    # -- read (bounded byte range for the streaming endpoint) --------------------

    def read_range(self, digest: str, *, offset: int, length: int) -> bytes:
        """Read a bounded byte range from a committed object (never outside the store)."""
        path = self.object_abs(digest)
        if not os.path.isfile(path):
            raise ArtifactStoreError("Object not found.", code="not_found")
        total = os.path.getsize(path)
        if offset < 0 or length < 0 or offset > total:
            raise ArtifactStoreError("Invalid byte range.", code="bad_range")
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read(min(length, total - offset))

    def object_size(self, digest: str) -> int:
        return os.path.getsize(self.object_abs(digest))

    # -- commit / quarantine / gc ------------------------------------------------

    def _commit(self, tmp_abs: str, hex_body: str, size: int) -> StoredObject:
        rel = self._object_rel(hex_body)
        final_abs = self._confined(rel)
        os.makedirs(os.path.dirname(final_abs), exist_ok=True)
        if os.path.isfile(final_abs):  # content dedup — identical object already present
            with _suppress_os():
                os.remove(tmp_abs)
            return StoredObject(f"sha256:{hex_body}", size, rel, deduplicated=True)
        # Atomic rename within the same filesystem (store root).
        os.replace(tmp_abs, final_abs)
        os.chmod(final_abs, 0o400)
        return StoredObject(f"sha256:{hex_body}", size, rel, deduplicated=False)

    def _quarantine(self, src_abs: str, name: str) -> None:
        dest = self._confined(os.path.join("quarantine", f"{name}_{_rand_token(name, 0)}"))
        with _suppress_os():
            os.replace(src_abs, dest)

    def gc(self, *, pinned_digests: set[str], dry_run: bool) -> dict:
        """Remove committed objects whose digest is not in ``pinned_digests``.

        Pinned objects are never removed. Returns bounded counts; ``dry_run`` reports only.
        Caller passes the digests that remain referenced/pinned by live artifact records.
        """
        keep_hex = {_validate_digest(d) for d in pinned_digests if d}
        removed = 0
        reclaimed = 0
        objects_root = os.path.join(self.root, "objects")
        for dirpath, _dirs, files in os.walk(objects_root):
            for name in files:
                if not name.startswith("sha256_"):
                    continue
                hex_body = name[len("sha256_"):]
                if hex_body in keep_hex:
                    continue
                abs_path = os.path.join(dirpath, name)
                with _suppress_os():
                    sz = os.path.getsize(abs_path)
                    if not dry_run:
                        os.chmod(abs_path, 0o600)
                        os.remove(abs_path)
                    removed += 1
                    reclaimed += sz
        return {"dry_run": dry_run, "removed_objects": removed, "reclaimed_bytes": reclaimed}

    def cleanup_partials(self, *, older_than_seconds: float, now_ts: float,
                         keep_transfer_ids: set[str]) -> int:
        """Remove abandoned partial files older than the expiry, except active transfers."""
        partial_root = os.path.join(self.root, "partial")
        removed = 0
        for name in os.listdir(partial_root) if os.path.isdir(partial_root) else []:
            if name in keep_transfer_ids:
                continue
            abs_path = os.path.join(partial_root, name)
            with _suppress_os():
                if now_ts - os.path.getmtime(abs_path) >= older_than_seconds:
                    os.remove(abs_path)
                    removed += 1
        return removed

    def status(self) -> dict:
        """Sanitized capacity snapshot (no absolute paths)."""
        used = self.total_size_bytes()
        return {
            "total_quota_bytes": self.total_quota_bytes,
            "used_bytes": used,
            "available_in_quota_bytes": max(0, self.total_quota_bytes - used),
            "free_disk_bytes": self.free_bytes(),
            "minimum_free_bytes": self.minimum_free_bytes,
            "max_artifact_bytes": self.max_artifact_bytes,
        }


class _suppress_os:
    """Context manager that swallows OSError (filesystem race / missing file)."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, OSError)


def _rand_token(seed: str, n: int) -> str:
    """A unique-enough token for temp names."""
    raw = f"{seed}:{n}:{os.getpid()}:{os.urandom(8).hex()}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]
