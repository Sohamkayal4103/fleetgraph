"""beta.10 Part 4: dedicated research Drive hierarchy + verified upload (fake client).

Proves the mandate's folder hierarchy is created under the dedicated root, an upload is verified by
remote size (and digest when exposed), a size mismatch is rejected (never reported as success), and
the setup self-test round-trips + cleans up. A live round-trip against the real Drive is exercised
separately by `aithernet data setup` (OAuth is configured on this node).
"""

from __future__ import annotations

import hashlib

import pytest

from aithernet.data.research_drive import (
    RESEARCH_FOLDERS,
    RESEARCH_ROOT,
    ResearchDriveArchive,
)


class _FakeDrive:
    def __init__(self, *, lie_about_size=False, expose_digest=True):
        self.folders = {}
        self.files = {}
        self._n = 0
        self.lie = lie_about_size
        self.expose_digest = expose_digest

    def find_or_create_folder(self, name, parent_id):
        key = (name, parent_id)
        if key not in self.folders:
            self._n += 1
            self.folders[key] = f"folder-{self._n}"
        return self.folders[key]

    def resumable_upload(self, *, folder_id, name, data, idempotency_key, chunk_bytes):
        for fid, f in self.files.items():
            if f["idem"] == idempotency_key and f["folder"] == folder_id:
                return {"drive_file_id": fid, "byte_size": len(f["data"])}
        self._n += 1
        fid = f"file-{self._n}"
        self.files[fid] = {"data": data, "folder": folder_id, "name": name,
                           "idem": idempotency_key,
                           "sha": hashlib.sha256(data).hexdigest()}
        return {"drive_file_id": fid, "byte_size": len(data)}

    def get_metadata(self, file_id):
        f = self.files.get(file_id)
        if not f:
            return None
        size = len(f["data"]) + (1 if self.lie else 0)
        meta = {"size": size}
        if self.expose_digest:
            meta["sha256Checksum"] = f["sha"]
        return meta

    def delete_file(self, file_id):
        return self.files.pop(file_id, None) is not None


def test_ensure_hierarchy_creates_root_and_all_folders():
    arc = ResearchDriveArchive(_FakeDrive())
    folders = arc.ensure_hierarchy()
    assert "_root" in folders
    for name in RESEARCH_FOLDERS:
        assert name in folders
    assert "00 Raw Mission Packages" in folders and "Manifests" in folders
    assert len(folders) == len(RESEARCH_FOLDERS) + 1


def test_upload_verified_checks_remote_size_and_digest():
    arc = ResearchDriveArchive(_FakeDrive())
    folders = arc.ensure_hierarchy()
    r = arc.upload_verified(folder_id=folders["Manifests"], name="x.json", data=b"hello world")
    assert r["remote_size_verified"] is True
    assert r["remote_digest_verified"] is True
    assert r["sha256"].startswith("sha256:")


def test_upload_rejects_size_mismatch():
    arc = ResearchDriveArchive(_FakeDrive(lie_about_size=True))
    folders = arc.ensure_hierarchy()
    with pytest.raises(ValueError, match="not verified"):
        arc.upload_verified(folder_id=folders["Manifests"], name="x.json", data=b"data")


def test_upload_is_idempotent_by_key():
    drive = _FakeDrive()
    arc = ResearchDriveArchive(drive)
    folders = arc.ensure_hierarchy()
    a = arc.upload_verified(folder_id=folders["Manifests"], name="x", data=b"d",
                            idempotency_key="k1")
    b = arc.upload_verified(folder_id=folders["Manifests"], name="x", data=b"d",
                            idempotency_key="k1")
    assert a["file_id"] == b["file_id"]


def test_setup_and_self_test_round_trips_and_cleans_up():
    arc = ResearchDriveArchive(_FakeDrive())
    report = arc.setup_and_self_test()
    assert report["root_folder_id"]
    assert RESEARCH_ROOT  # dedicated root name is defined
    st = report["self_test"]
    assert st["remote_size_verified"] is True
    assert st["cleaned_up"] is True


def test_digest_unverifiable_when_drive_hides_it():
    arc = ResearchDriveArchive(_FakeDrive(expose_digest=False))
    folders = arc.ensure_hierarchy()
    r = arc.upload_verified(folder_id=folders["Manifests"], name="x", data=b"d")
    assert r["remote_size_verified"] is True
    assert r["remote_digest_verified"] is None  # size verified, digest not exposed
