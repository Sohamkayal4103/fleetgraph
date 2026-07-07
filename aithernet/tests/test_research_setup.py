"""beta.10 Part 6: owner-research enablement shared by `aithernet setup` and `data setup`.

Proves owner_full is written to the canonical node.yaml, the spool is initialized, the existing
Drive root is reused (not recreated), the hierarchy is verified, a bounded verified test upload runs
and is cleaned up, and the report is plain-language + secret-free.
"""

from __future__ import annotations

import hashlib

import yaml

from aithernet.data.research_setup import (
    RESEARCH_DRIVE_ROOT_ID,
    enable_owner_research,
    set_collection_mode,
)


class _FakeDrive:
    def __init__(self):
        self.folders = {}
        self.files = {}
        self._n = 0

    def find_or_create_folder(self, name, parent_id):
        if name == "Aithernet Research Data" and parent_id is None:
            return RESEARCH_DRIVE_ROOT_ID  # reuse the existing dedicated root
        key = (name, parent_id)
        if key not in self.folders:
            self._n += 1
            self.folders[key] = f"folder-{self._n}"
        return self.folders[key]

    def resumable_upload(self, *, folder_id, name, data, idempotency_key, chunk_bytes):
        self._n += 1
        fid = f"file-{self._n}"
        self.files[fid] = data
        return {"drive_file_id": fid, "byte_size": len(data)}

    def get_metadata(self, file_id):
        d = self.files.get(file_id)
        return None if d is None else {"size": len(d),
                                       "sha256Checksum": hashlib.sha256(d).hexdigest()}

    def delete_file(self, file_id):
        return self.files.pop(file_id, None) is not None


class _FakeOAuth:
    def __init__(self, authorized=True):
        self._auth = authorized
        self.refreshed = False

    def authorized(self):
        return self._auth

    def access_token(self):
        self.refreshed = True
        return "ya29.fake"


def test_set_collection_mode_writes_canonical_yaml(tmp_path):
    path = set_collection_mode("owner_full", tmp_path)
    data = yaml.safe_load(path.read_text())
    assert data["data_platform"]["collection"]["data_collection_mode"] == "owner_full"
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_enable_owner_research_default_is_hosted_not_client_drive(tmp_path, monkeypatch):
    # beta.9: the DEFAULT flow is hosted owner-archive — no client Drive involvement at all.
    monkeypatch.setattr("aithernet.data.research_spool.default_spool_root",
                        lambda: tmp_path / "research")
    monkeypatch.setattr("aithernet.data.research_upload.is_enrolled",
                        lambda state_root=None: False)
    report = enable_owner_research(state_root=tmp_path)
    assert report["collection_mode"] == "owner_full"
    assert report["mode"] == "hosted_owner_archive"
    assert report["consent"] == "granted"
    assert report["owner_archive_upload"] == "pending_enrollment"
    assert "drive" not in report                    # no client Drive
    cfg = yaml.safe_load((tmp_path / "config" / "node.yaml").read_text())
    assert cfg["data_platform"]["collection"]["data_collection_mode"] == "owner_full"


# The following exercise the ADVANCED standalone client-Drive mode (not the normal hosted path).
def test_enable_owner_research_standalone_full_flow(tmp_path, monkeypatch):
    monkeypatch.setattr("aithernet.data.research_spool.default_spool_root",
                        lambda: tmp_path / "research")
    oauth = _FakeOAuth()
    report = enable_owner_research(state_root=tmp_path, standalone_drive=True,
                                   drive_client=_FakeDrive(), oauth=oauth)
    assert report["collection_mode"] == "owner_full"
    assert oauth.refreshed is True
    assert report["drive"] == "ready"
    assert report["drive_root_id"] == RESEARCH_DRIVE_ROOT_ID
    assert report["reused_existing_root"] is True
    assert report["self_test"]["remote_size_verified"] is True
    assert report["self_test"]["cleaned_up"] is True
    # report is secret-free
    assert "ya29" not in str(report)


def test_enable_owner_research_standalone_skip_drive(tmp_path, monkeypatch):
    monkeypatch.setattr("aithernet.data.research_spool.default_spool_root",
                        lambda: tmp_path / "research")
    report = enable_owner_research(state_root=tmp_path, standalone_drive=True, skip_drive=True)
    assert report["drive"] == "skipped"
    assert (tmp_path / "research" / "raw").is_dir()


def test_enable_owner_research_standalone_unauthorized_drive(tmp_path, monkeypatch):
    monkeypatch.setattr("aithernet.data.research_spool.default_spool_root",
                        lambda: tmp_path / "research")
    report = enable_owner_research(state_root=tmp_path, standalone_drive=True,
                                   oauth=_FakeOAuth(authorized=False))
    assert report["drive"] == "not_authorized"
    assert report["collection_mode"] == "owner_full"
