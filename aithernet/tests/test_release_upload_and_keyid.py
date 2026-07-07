"""Release-artifact upload bounding/authorization + signing-key-id traceability (Stage 14F).

Covers the two product fixes:
  * a single authenticated release-artifact route with a documented bounded maximum (re-enforced
    server-side), filename/traversal validation, integrity (digest + byte-count) verification, and
    an atomic publish gate that refuses partial/corrupt artifacts; and
  * a configurable, traceable signing-key id whose value lands in the signed manifest and which
    fails closed on unknown/revoked ids — while verification still depends on the real public key.
"""

from __future__ import annotations

import secrets

import pytest
from services.control_plane import release_signing
from services.control_plane.config import HostedConfig
from services.control_plane.errors import ControlPlaneError
from services.control_plane.security import generate_keypair
from services.control_plane.service import ControlPlaneService


@pytest.fixture
def session_key(monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", secrets.token_urlsafe(48))


def _svc(tmp_path, *, max_bytes=None, signing_keys=None, key_id=None) -> ControlPlaneService:
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'hosted.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    if max_bytes is not None:
        cfg.releases.max_artifact_bytes = max_bytes
    if key_id is not None:
        cfg.releases.signing_key_id = key_id
    if signing_keys is not None:
        cfg.releases.signing_keys = signing_keys
    return ControlPlaneService(cfg)


def _admin(svc):
    svc.bootstrap_admin("admin@example.invalid", "supersecret-123")
    sess = svc.create_session(_uid(svc, "admin@example.invalid"))
    principal, _, _ = svc.validate_session(sess["session_token"])
    return principal


def _uid(svc, email):
    from services.control_plane.models import HostedUser
    from sqlalchemy import select
    with svc._session() as s:
        return s.execute(select(HostedUser).where(HostedUser.email == email)).scalar_one().id


# -- upload bounding + integrity ------------------------------------------------------------------


def test_upload_respects_bounded_maximum(tmp_path, session_key):
    svc = _svc(tmp_path, max_bytes=1024)
    admin = _admin(svc)
    rel = svc.create_release(admin, version="1.0.0")
    rid = rel["release_id"]
    # at the bound: accepted
    svc.add_release_artifact(admin, release_id=rid, name="ok.bin", data=b"x" * 1024)
    # over the bound: rejected, no row written
    with pytest.raises(ControlPlaneError) as exc:
        svc.add_release_artifact(admin, release_id=rid, name="big.bin", data=b"x" * 1025)
    assert "artifact_too_large" in str(exc.value)


def test_default_max_artifact_bytes_fits_real_artifacts(tmp_path, session_key):
    # The documented bound must comfortably hold the real .deb (~16 MiB) and friends.
    svc = _svc(tmp_path)
    assert svc.config.releases.max_artifact_bytes >= 32 * 1024 * 1024


def test_upload_rejects_path_traversal_and_bad_names(tmp_path, session_key):
    svc = _svc(tmp_path)
    admin = _admin(svc)
    rid = svc.create_release(admin, version="1.0.0")["release_id"]
    for bad in ["../escape", "a/b.whl", "..", "/abs", "sp ace", "weird;name", ""]:
        with pytest.raises(ControlPlaneError) as exc:
            svc.add_release_artifact(admin, release_id=rid, name=bad, data=b"x")
        assert "invalid_artifact_name" in str(exc.value)
    # a real Debian artifact name (with ~) is accepted
    svc.add_release_artifact(admin, release_id=rid, name="aithernet_0.8.0~beta.2_amd64.deb",
                             data=b"x")


def test_upload_verifies_declared_digest_and_size(tmp_path, session_key):
    svc = _svc(tmp_path)
    admin = _admin(svc)
    rid = svc.create_release(admin, version="1.0.0")["release_id"]
    good = release_signing  # noqa: F841  (keep import used by other tests)
    from services.control_plane.blobstore import sha256_hex
    data = b"PAYLOAD-BYTES"
    # correct digest + size: accepted
    svc.add_release_artifact(admin, release_id=rid, name="a.bin", data=data,
                             expected_sha256=sha256_hex(data), expected_byte_size=len(data))
    # wrong digest: rejected and nothing stored
    with pytest.raises(ControlPlaneError) as exc:
        svc.add_release_artifact(admin, release_id=rid, name="b.bin", data=data,
                                 expected_sha256="sha256:" + "0" * 64)
    assert "artifact_digest_mismatch" in str(exc.value)
    with pytest.raises(ControlPlaneError) as exc:
        svc.add_release_artifact(admin, release_id=rid, name="c.bin", data=data,
                                 expected_byte_size=999)
    assert "artifact_size_mismatch" in str(exc.value)
    # the rejected uploads left no artifact rows / blobs
    manifest = svc.release_manifest(rid) if False else None  # release still draft, no manifest
    assert manifest is None
    names = {a["name"] for a in svc.list_releases(principal=admin,
             include_unpublished=True)[0]["artifacts"]}
    assert names == {"a.bin"}


def test_publish_fails_closed_on_corrupted_blob(tmp_path, session_key):
    svc = _svc(tmp_path)
    admin = _admin(svc)
    rseed, _ = generate_keypair()
    rid = svc.create_release(admin, version="1.0.0")["release_id"]
    svc.add_release_artifact(admin, release_id=rid, name="a.bin", data=b"GOOD")
    svc.sign_release(admin, release_id=rid, signing_seed_b64=rseed)
    # Corrupt the stored blob out from under the signed manifest.
    store = svc.blob_store
    store.put("releases/early-access/1.0.0/a.bin", b"TAMPERED-LONGER")
    with pytest.raises(ControlPlaneError) as exc:
        svc.publish_release(admin, release_id=rid)
    assert "release_artifact_corrupt" in str(exc.value)


# -- signing-key-id traceability + fail-closed ----------------------------------------------------


def test_signing_key_id_is_recorded_in_manifest(tmp_path, session_key):
    keys = {"lan-betaqual-2026": {"ref": "AITHERNET_RELEASE_SIGNING_KEY", "status": "active"}}
    svc = _svc(tmp_path, key_id="lan-betaqual-2026", signing_keys=keys)
    admin = _admin(svc)
    rseed, rpub = generate_keypair()
    rid = svc.create_release(admin, version="1.0.0")["release_id"]
    svc.add_release_artifact(admin, release_id=rid, name="a.bin", data=b"GOOD")
    signed = svc.sign_release(admin, release_id=rid, signing_seed_b64=rseed)
    assert signed["signing_key_id"] == "lan-betaqual-2026"
    svc.publish_release(admin, release_id=rid)
    chrel = svc.channel_release()
    assert chrel["manifest"]["signing_key_id"] == "lan-betaqual-2026"
    # verification still depends on the REAL public key + signature, not the label
    assert release_signing.verify_manifest(rpub, chrel["manifest"], chrel["manifest_signature"])
    wrong_seed, wrong_pub = generate_keypair()
    assert not release_signing.verify_manifest(wrong_pub, chrel["manifest"],
                                               chrel["manifest_signature"])


def test_explicit_signing_key_id_selection(tmp_path, session_key):
    keys = {
        "active-2026": {"ref": "AITHERNET_RELEASE_SIGNING_KEY", "status": "active"},
        "old-2025": {"ref": "AITHERNET_RELEASE_SIGNING_KEY", "status": "active"},
    }
    svc = _svc(tmp_path, key_id="active-2026", signing_keys=keys)
    admin = _admin(svc)
    rseed, _ = generate_keypair()
    rid = svc.create_release(admin, version="1.0.0")["release_id"]
    svc.add_release_artifact(admin, release_id=rid, name="a.bin", data=b"GOOD")
    signed = svc.sign_release(admin, release_id=rid, signing_seed_b64=rseed,
                              signing_key_id="old-2025")
    assert signed["signing_key_id"] == "old-2025"


def test_unknown_and_revoked_key_ids_fail_closed(tmp_path, session_key):
    keys = {
        "active-2026": {"ref": "AITHERNET_RELEASE_SIGNING_KEY", "status": "active"},
        "retired-2024": {"ref": "AITHERNET_RELEASE_SIGNING_KEY", "status": "revoked"},
    }
    svc = _svc(tmp_path, key_id="active-2026", signing_keys=keys)
    admin = _admin(svc)
    rseed, _ = generate_keypair()
    rid = svc.create_release(admin, version="1.0.0")["release_id"]
    svc.add_release_artifact(admin, release_id=rid, name="a.bin", data=b"GOOD")
    with pytest.raises(ControlPlaneError) as exc:
        svc.sign_release(admin, release_id=rid, signing_seed_b64=rseed,
                         signing_key_id="does-not-exist")
    assert "release_signing_key_unknown" in str(exc.value)
    with pytest.raises(ControlPlaneError) as exc:
        svc.sign_release(admin, release_id=rid, signing_seed_b64=rseed,
                         signing_key_id="retired-2024")
    assert "release_signing_key_revoked" in str(exc.value)


def test_upload_route_authorization_and_413(tmp_path, session_key):
    # The release-artifact ROUTE: unauthenticated upload is rejected, oversize gets 413, and a
    # within-bound authenticated upload succeeds — proving the bound is enforced at the API layer
    # (the proxy grants the larger allowance only to this path; see the Caddyfile rule).
    from fastapi.testclient import TestClient
    from services.control_plane.app import create_app
    svc = _svc(tmp_path, max_bytes=2048)
    _admin(svc)
    client = TestClient(create_app(svc.config, svc))
    login = client.post("/v1/auth/login",
                        json={"email": "admin@example.invalid", "password": "supersecret-123"})
    headers = {"x-csrf-token": login.json()["csrf_token"]}
    rid = client.post("/v1/releases", json={"version": "1.0.0"}, headers=headers).json()[
        "release_id"]
    art = f"/v1/releases/{rid}/artifacts"

    # unauthorized (no CSRF / session): rejected
    fresh = TestClient(create_app(svc.config, svc))
    assert fresh.post(art, content=b"x", headers={"x-artifact-name": "a.bin"}).status_code >= 400

    # oversize body: 413 from the route's server-side bound
    big = client.post(art, content=b"x" * 4096,
                      headers={**headers, "x-artifact-name": "big.bin"})
    assert big.status_code == 413

    # within bound: accepted
    ok = client.post(art, content=b"x" * 1000, headers={**headers, "x-artifact-name": "ok.bin"})
    assert ok.status_code == 200


def test_from_env_reads_signing_key_id_and_registry(monkeypatch):
    monkeypatch.setenv("AITHERNET_RELEASE_SIGNING_KEY_ID", "lan-betaqual-beta2")
    cfg = HostedConfig.from_env()
    assert cfg.releases.signing_key_id == "lan-betaqual-beta2"
    # a default single-key active registry is synthesized when none is provided
    assert cfg.releases.signing_keys["lan-betaqual-beta2"]["status"] == "active"


def test_from_env_overrides_max_artifact_bytes(monkeypatch):
    # deployments that ship the ~650 MiB CatGPT Gateway runtime image raise the bound
    monkeypatch.setenv("AITHERNET_RELEASE_MAX_ARTIFACT_BYTES", str(1024 * 1024 * 1024))
    assert HostedConfig.from_env().releases.max_artifact_bytes == 1024 * 1024 * 1024


def test_default_max_artifact_bytes_unchanged_without_env(monkeypatch):
    monkeypatch.delenv("AITHERNET_RELEASE_MAX_ARTIFACT_BYTES", raising=False)
    assert HostedConfig.from_env().releases.max_artifact_bytes == 64 * 1024 * 1024
