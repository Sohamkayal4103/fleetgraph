"""Local archive destination (Stage 14E, Part 13).

A production-capable local sink: writes sealed bundles atomically under an OPERATOR-configured
root (never an API-supplied path), content-addressed by digest, with a manifest sidecar and
checksums. Enforces a minimum free-disk floor, confines all writes under the root (no path
traversal, no symlink escape), and supports receipt-backed deletion. This is the default sink
for local + air-gapped development.
"""

from __future__ import annotations

import json
import os
import shutil

from aithernet.data.crypto import sha256_hex
from aithernet.data.destinations import (
    DeletionResult,
    ExportDestination,
    Readiness,
    UploadResult,
)


class LocalArchiveDestination(ExportDestination):
    kind = "local_archive"

    def __init__(self, *, root: str, minimum_free_bytes: int = 0) -> None:
        self.root = os.path.realpath(root)
        self.minimum_free_bytes = minimum_free_bytes

    def validate(self) -> tuple[bool, str]:
        if not self.root:
            return False, "root_not_configured"
        return True, "ok"

    def readiness(self) -> Readiness:
        try:
            os.makedirs(self.root, exist_ok=True)
        except OSError:
            return Readiness(ready=False, state="degraded", detail="root_unwritable")
        if self._free_bytes() < self.minimum_free_bytes:
            return Readiness(ready=False, state="degraded", detail="insufficient_disk")
        return Readiness(ready=True, state="ready")

    def _free_bytes(self) -> int:
        try:
            return shutil.disk_usage(self.root).free
        except OSError:
            return 0

    def _confined(self, *parts: str) -> str:
        path = os.path.realpath(os.path.join(self.root, *parts))
        if path != self.root and not path.startswith(self.root + os.sep):
            raise ValueError("path escapes archive root")
        return path

    async def upload(self, *, batch_id: str, idempotency_key: str, bundle: bytes,
                     manifest: dict) -> UploadResult:
        ready = self.readiness()
        if not ready.ready:
            return UploadResult(ok=False, failure_category="destination", detail=ready.detail)
        if self._free_bytes() < self.minimum_free_bytes + len(bundle):
            return UploadResult(ok=False, failure_category="insufficient_disk",
                                detail="insufficient_disk")
        digest = sha256_hex(bundle)
        hexpart = digest.split(":", 1)[1]
        rel = os.path.join("objects", hexpart[:2], f"{hexpart}.bundle")
        try:
            final = self._confined(rel)
            os.makedirs(os.path.dirname(final), exist_ok=True)
            if not os.path.isfile(final):  # idempotent: identical content already archived
                tmp = final + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(bundle)
                os.replace(tmp, final)
            manifest_path = self._confined("manifests", f"{batch_id}.manifest.json")
            os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
            with open(manifest_path, "w") as fh:
                json.dump({"manifest": manifest, "bundle_digest": digest,
                           "relative_path": rel, "byte_size": len(bundle)}, fh)
        except (OSError, ValueError) as exc:
            return UploadResult(ok=False, failure_category="destination",
                                detail=type(exc).__name__)
        # Receipt carries a STORE-RELATIVE path + digest only (never an absolute private path).
        return UploadResult(ok=True, receipt={
            "destination_kind": self.kind, "relative_path": rel, "bundle_digest": digest,
            "byte_size": len(bundle), "receipt_status": "stored",
        })

    async def delete(self, *, remote_ref: str) -> DeletionResult:
        try:
            target = self._confined(remote_ref)
        except ValueError:
            return DeletionResult(ok=False, state="unknown", detail="bad_ref")
        if not os.path.isfile(target):
            return DeletionResult(ok=True, state="not_found",
                                  receipt={"relative_path": remote_ref, "deleted": False})
        try:
            os.remove(target)
        except OSError as exc:
            return DeletionResult(ok=False, state="unknown", detail=type(exc).__name__)
        return DeletionResult(ok=True, state="deleted",
                              receipt={"relative_path": remote_ref, "deleted": True})
