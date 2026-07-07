"""Stage 14F node-side hosted integration tests: local config, update client, version ordering."""

from __future__ import annotations

import os
import stat

import pytest
from services.control_plane import release_signing
from services.control_plane.security import generate_keypair

from aithernet.hosted.localconfig import (
    HostedEnrollment,
    hosted_config_path,
    load_enrollment,
    save_enrollment,
)
from aithernet.hosted.update import UpdateClient, UpdateError, _version_tuple


def test_local_config_roundtrip_and_permissions(tmp_path):
    enrollment = HostedEnrollment(
        control_plane_base_url="http://127.0.0.1:8100", hosted_node_id="hn-1",
        tenant_id="acme", enrollment_state="enrolled")
    path = save_enrollment(tmp_path, enrollment)
    assert path == hosted_config_path(tmp_path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600  # restrictive permissions
    loaded = load_enrollment(tmp_path)
    assert loaded.hosted_node_id == "hn-1" and loaded.enrollment_state == "enrolled"


def test_local_config_never_writes_secrets(tmp_path):
    enrollment = HostedEnrollment(control_plane_base_url="http://x", tenant_id="t")
    save_enrollment(tmp_path, enrollment)
    text = hosted_config_path(tmp_path).read_text().lower()
    for forbidden in ("password", "session", "cookie", "private_key", "secret"):
        assert forbidden not in text


def test_version_ordering_release_beats_prerelease():
    assert _version_tuple("0.8.0") > _version_tuple("0.8.0-beta.1")
    assert _version_tuple("0.9.0-beta.1") > _version_tuple("0.8.0")
    assert _version_tuple("0.8.0-beta.2") > _version_tuple("0.8.0-beta.1")


class _FakeClient:
    """Stub HostedClient returning a signed manifest + matching artifact bytes."""

    def __init__(self, manifest, signature, release_id, artifacts):
        self._manifest = manifest
        self._signature = signature
        self._release_id = release_id
        self._artifacts = artifacts

    def get_release(self, **_):
        return {"available": True, "version": self._manifest["version"],
                "release_id": self._release_id, "manifest": self._manifest,
                "manifest_signature": self._signature}

    def download(self, release_id, name, **kwargs):  # accepts node-auth kwargs (identity/tenant/node)
        data = self._artifacts[name]
        return data, "sha256:" + __import__("hashlib").sha256(data).hexdigest()


def _signed_release():
    seed, pub = generate_keypair()
    data = b"WHEELDATA"
    sha = "sha256:" + __import__("hashlib").sha256(data).hexdigest()
    manifest = release_signing.canonical_manifest(
        version="0.8.0-beta.1", channel="early-access", signing_key_id="rk1",
        artifacts=[{"name": "a.whl", "sha256": sha, "byte_size": len(data)}])
    sig = release_signing.sign_manifest(seed, manifest)
    return pub, manifest, sig, {"a.whl": data}


def test_update_download_and_verify_ok(tmp_path):
    pub, manifest, sig, artifacts = _signed_release()
    client = _FakeClient(manifest, sig, "rel-1", artifacts)
    uc = UpdateClient(client, release_public_key_b64=pub, staging_dir=tmp_path / "stage")
    plan = uc.download_and_verify(identity=None, tenant_id="t", node_id="n")
    assert plan["verified"] and plan["version"] == "0.8.0-beta.1"
    uc.record_apply("0.8.0-beta.1", previous_version="0.7.0")
    assert uc.rollback_target() == "0.7.0"


def test_update_rejects_bad_signature(tmp_path):
    _, manifest, sig, artifacts = _signed_release()
    wrong_pub, _ = generate_keypair()
    client = _FakeClient(manifest, sig, "rel-1", artifacts)
    uc = UpdateClient(client, release_public_key_b64=wrong_pub, staging_dir=tmp_path / "stage")
    with pytest.raises(UpdateError):
        uc.download_and_verify(identity=None, tenant_id="t", node_id="n")


def test_update_rejects_tampered_artifact(tmp_path):
    pub, manifest, sig, _ = _signed_release()
    client = _FakeClient(manifest, sig, "rel-1", {"a.whl": b"TAMPERED"})
    uc = UpdateClient(client, release_public_key_b64=pub, staging_dir=tmp_path / "stage")
    with pytest.raises(UpdateError):
        uc.download_and_verify(identity=None, tenant_id="t", node_id="n")
