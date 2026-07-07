"""beta.10 Part 4: the dedicated "Aithernet Research Data" Drive archive + verified test upload.

Builds the mandate's research folder hierarchy under a dedicated root, performs a content-addressed
resumable upload, and verifies the remote size/digest. The Drive client is injectable so unit tests
run against a fake; ``data setup``/``data verify`` use the live OAuth client. Secrets are never
uploaded — quarantine contents stay local; only quarantine METADATA is archived.
"""

from __future__ import annotations

import hashlib

#: The dedicated research Drive root + its canonical subfolders (mandate Part 4).
RESEARCH_ROOT = "Aithernet Research Data"
RESEARCH_FOLDERS = (
    "00 Raw Mission Packages",
    "10 Normalized Records",
    "20 Coordinator Training",
    "21 Tool Calling Training",
    "22 Recovery Training",
    "23 Routing Training",
    "30 Coding Training",
    "40 RF Datasets",
    "50 Evaluation Sets",
    "60 Dataset Snapshots",
    "70 Model Runs",
    "80 Rejected",
    "90 Quarantine Metadata",
    "Manifests",
)


class ResearchDriveArchive:
    """Manage the dedicated research Drive hierarchy + verified uploads (client is injectable)."""

    def __init__(self, client) -> None:
        # ``client`` exposes find_or_create_folder, resumable_upload, get_metadata, delete_file.
        self.client = client

    def ensure_hierarchy(self) -> dict:
        """Create the research root + every canonical subfolder. Returns name -> folder id."""
        root_id = self.client.find_or_create_folder(RESEARCH_ROOT, None)
        folders = {"_root": root_id}
        for name in RESEARCH_FOLDERS:
            folders[name] = self.client.find_or_create_folder(name, root_id)
        return folders

    def upload_verified(self, *, folder_id: str, name: str, data: bytes,
                        idempotency_key: str | None = None,
                        chunk_bytes: int = 8 * 1024 * 1024) -> dict:
        """Upload bytes (content-addressed, idempotent) and verify the remote size; verify digest
        when Drive exposes it. Raises on a size mismatch — an unverified upload is never success."""
        local_sha = hashlib.sha256(data).hexdigest()
        idem = idempotency_key or local_sha
        up = self.client.resumable_upload(
            folder_id=folder_id, name=name, data=data, idempotency_key=idem,
            chunk_bytes=chunk_bytes)
        file_id = up.get("drive_file_id") or up.get("id")
        meta = self.client.get_metadata(file_id) or {}
        remote_size = int(meta.get("size")) if meta.get("size") is not None else up.get("byte_size")
        size_ok = remote_size == len(data)
        remote_sha = meta.get("sha256Checksum")
        digest_ok = (remote_sha == local_sha) if remote_sha else None
        if not size_ok:
            raise ValueError(
                f"remote size {remote_size} != local {len(data)} for {name} — upload not verified")
        return {
            "name": name, "file_id": file_id, "sha256": "sha256:" + local_sha,
            "byte_size": len(data), "remote_size_verified": size_ok,
            "remote_digest_verified": digest_ok,
        }

    def setup_and_self_test(self) -> dict:
        """Create the hierarchy and round-trip a small test object into Manifests, then verify it.

        The test object is uploaded, its remote size verified, and then deleted (leaving the
        hierarchy in place). Returns a structured, secret-free report for ``data setup``."""
        folders = self.ensure_hierarchy()
        probe = b'{"aithernet":"research-archive-self-test"}'
        receipt = self.upload_verified(
            folder_id=folders["Manifests"], name="setup-selftest.json", data=probe,
            idempotency_key="setup-selftest")
        deleted = False
        try:
            deleted = self.client.delete_file(receipt["file_id"])
        except Exception:  # noqa: BLE001 — cleanup is best-effort; the verified upload already passed
            deleted = False
        return {
            "root_folder_id": folders["_root"],
            "folders": {k: v for k, v in folders.items() if k != "_root"},
            "self_test": {**receipt, "cleaned_up": deleted},
        }
