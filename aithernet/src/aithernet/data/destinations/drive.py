"""Google Drive archive destination (Stage 14E, Part 14).

An OPTIONAL, disabled-by-default encrypted archive sink. Google Drive is never the live
database, authoritative artifact store, mission DB, export queue, or active capture
destination. Credentials + encryption keys are loaded from env-REFERENCED secrets only, never
committed or logged; access/refresh tokens and authorization URLs are never logged.

The Drive API is reached through a small injected ``DriveClient`` so ordinary tests use a fake
(never a real account). Uploads are resumable and idempotent (stable idempotency metadata so a
retry resolves to the same logical file). Sensitive bundles must be encrypted before upload.
"""

from __future__ import annotations

import time
from typing import Protocol

from aithernet.data.crypto import sha256_hex
from aithernet.data.destinations import (
    DeletionResult,
    ExportDestination,
    Readiness,
    UploadResult,
)


class DriveError(Exception):
    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(detail or category)
        self.category = category
        self.detail = detail


class DriveClient(Protocol):
    """The minimal Drive surface the destination needs (real client or test fake)."""

    def verify_folder(self, folder_id: str) -> bool: ...

    def find_by_idempotency(self, folder_id: str, idempotency_key: str) -> dict | None: ...

    def resumable_upload(
        self, *, folder_id: str, name: str, data: bytes, idempotency_key: str, chunk_bytes: int,
    ) -> dict: ...

    def delete_file(self, file_id: str) -> bool: ...


class FakeDriveClient:
    """An in-memory Drive client for tests: resumable chunks, idempotency, fault injection."""

    def __init__(self, *, folders: set[str] | None = None) -> None:
        self._folders = folders if folders is not None else {"folder-test"}
        self._files: dict[str, dict] = {}
        self._by_idem: dict[tuple[str, str], str] = {}
        self.fail_with: str | None = None   # "quota" | "auth" | "permission" | "interrupt"
        self._interrupt_once = False
        self.upload_calls = 0

    def verify_folder(self, folder_id: str) -> bool:
        if self.fail_with == "auth":
            raise DriveError("expired_credentials", "auth")
        return folder_id in self._folders

    def find_by_idempotency(self, folder_id: str, idempotency_key: str) -> dict | None:
        fid = self._by_idem.get((folder_id, idempotency_key))
        return dict(self._files[fid]) if fid else None

    def resumable_upload(self, *, folder_id, name, data, idempotency_key, chunk_bytes) -> dict:
        self.upload_calls += 1
        if self.fail_with == "auth":
            raise DriveError("expired_credentials", "auth")
        if self.fail_with == "permission":
            raise DriveError("permission_denied", "permission")
        if self.fail_with == "quota":
            raise DriveError("quota_exceeded", "quota")
        if folder_id not in self._folders:
            raise DriveError("permission_denied", "unknown_folder")
        existing = self.find_by_idempotency(folder_id, idempotency_key)
        if existing is not None:
            return existing                       # idempotent: no duplicate logical archive
        # Simulate a resumable upload in chunks; optionally interrupt midway once.
        uploaded = 0
        for _ in range(0, max(1, len(data)), max(1, chunk_bytes)):
            if self.fail_with == "interrupt" and not self._interrupt_once and uploaded > 0:
                self._interrupt_once = True
                raise DriveError("interrupted", "interrupted")
            uploaded = min(len(data), uploaded + chunk_bytes)
        file_id = f"drive-{len(self._files) + 1}"
        meta = {
            "drive_file_id": file_id, "folder_id": folder_id, "name": name,
            "byte_size": len(data), "sha256": sha256_hex(data),
        }
        self._files[file_id] = meta
        self._by_idem[(folder_id, idempotency_key)] = file_id
        return dict(meta)

    def delete_file(self, file_id: str) -> bool:
        if file_id in self._files:
            del self._files[file_id]
            return True
        return False


class GoogleDriveArchiveDestination(ExportDestination):
    kind = "google_drive"

    def __init__(
        self, *, folder_id: str | None, client: DriveClient | None = None,
        encrypted_required: bool = True, chunk_bytes: int = 256 * 1024,
    ) -> None:
        self.folder_id = folder_id
        self._client = client
        self.encrypted_required = encrypted_required
        self.chunk_bytes = chunk_bytes

    def validate(self) -> tuple[bool, str]:
        if not self.folder_id:
            return False, "folder_id_not_configured"
        if self._client is None:
            return False, "drive_client_unavailable"
        return True, "ok"

    def readiness(self) -> Readiness:
        if not self.folder_id:
            return Readiness(ready=False, state="unconfigured", detail="folder_id_not_configured")
        if self._client is None:
            return Readiness(ready=False, state="unconfigured", detail="credentials_unavailable")
        try:
            if not self._client.verify_folder(self.folder_id):
                return Readiness(ready=False, state="degraded", detail="folder_inaccessible")
        except DriveError as exc:
            return Readiness(ready=False, state="degraded", detail=exc.category)
        return Readiness(ready=True, state="ready")

    async def upload(self, *, batch_id: str, idempotency_key: str, bundle: bytes,
                     manifest: dict) -> UploadResult:
        ok, reason = self.validate()
        if not ok:
            return UploadResult(ok=False, failure_category="unconfigured", detail=reason)
        # Defence in depth: a Drive (external) destination must receive an ENCRYPTED bundle.
        if self.encrypted_required and not _looks_encrypted(bundle):
            return UploadResult(ok=False, failure_category="encryption_required",
                                detail="bundle_not_encrypted")
        started = time.time()
        try:
            meta = self._client.resumable_upload(
                folder_id=self.folder_id, name=f"{batch_id}.bundle", data=bundle,
                idempotency_key=idempotency_key, chunk_bytes=self.chunk_bytes,
            )
        except DriveError as exc:
            return UploadResult(ok=False, failure_category=exc.category, detail=exc.detail)
        receipt = {
            "destination_kind": self.kind,
            "drive_file_id": meta.get("drive_file_id"),
            "folder_id": meta.get("folder_id"),
            "byte_size": meta.get("byte_size"),
            "sha256": meta.get("sha256"),
            "upload_started_at": started,
            "upload_completed_at": time.time(),
            "receipt_status": "uploaded",
        }
        return UploadResult(ok=True, receipt=receipt)

    async def delete(self, *, remote_ref: str) -> DeletionResult:
        if self._client is None:
            return DeletionResult(ok=False, state="unknown", detail="drive_client_unavailable")
        try:
            deleted = self._client.delete_file(remote_ref)
        except DriveError as exc:
            # Unknown remote state must NEVER be reported as a successful deletion.
            return DeletionResult(ok=False, state="unknown", detail=exc.category)
        if deleted:
            return DeletionResult(ok=True, state="deleted",
                                  receipt={"drive_file_id": remote_ref, "deleted": True})
        return DeletionResult(ok=True, state="not_found",
                              receipt={"drive_file_id": remote_ref, "deleted": False})


def _looks_encrypted(bundle: bytes) -> bool:
    return bundle[:1] == b"{" and b"aith-batch-enc" in bundle[:64]
