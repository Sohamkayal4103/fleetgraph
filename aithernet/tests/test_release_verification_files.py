"""Complete customer release verification (beta.5 Area 1).

A published, signed release exposes its verification set as PUBLIC downloads — ``manifest.json``
(exact signed bytes), detached ``manifest.sig``, a PEM ``aithernet-release.pub``, and ``SHA256SUMS``
— so a customer can verify a download offline before installing. Package artifacts stay
authenticated; revoked releases serve nothing. ``aithernet release verify <dir>`` is the supported
verifier.
"""

from __future__ import annotations

import tempfile

import pytest
import typer
from services.control_plane.config import HostedConfig
from services.control_plane.errors import ForbiddenError
from services.control_plane.security import generate_keypair
from services.control_plane.service import ControlPlaneService, Principal

from aithernet import cli

_DEB = b"DEB-PAYLOAD-BYTES"


@pytest.fixture
def published(monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", "x" * 48)
    seed, pub = generate_keypair()
    # The control plane must serve the public key matching the signing seed.
    monkeypatch.setenv("AITHERNET_RELEASE_PUBLIC_KEY", pub)
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tempfile.mktemp()}"
    cfg.object_storage.local_root = tempfile.mkdtemp()
    svc = ControlPlaneService(cfg)
    a = svc.bootstrap_admin("admin@example.invalid", "pw-correct-horse")
    admin = Principal(user_id=a["user_id"], email=a["email"], is_platform_admin=True)
    rid = svc.create_release(admin, version="0.8.0-test")["release_id"]
    svc.add_release_artifact(admin, release_id=rid, name="aithernet_0.8.0_amd64.deb", data=_DEB)
    svc.sign_release(admin, release_id=rid, signing_seed_b64=seed)
    svc.publish_release(admin, release_id=rid)
    return svc, admin, rid


def _fetch_set(svc, rid) -> dict[str, bytes]:
    out = {}
    for name in ("manifest.json", "manifest.sig", "SHA256SUMS", "aithernet-release.pub"):
        data, _ctype, _sha = svc.download_artifact_public(release_id=rid, name=name)
        out[name] = data
    return out


def test_verification_files_are_public(published):
    svc, _admin, rid = published
    files = _fetch_set(svc, rid)
    assert files["manifest.json"].startswith(b"{")
    assert len(files["manifest.sig"]) == 64                      # raw Ed25519 signature
    assert b"BEGIN PUBLIC KEY" in files["aithernet-release.pub"]  # PEM
    assert b"aithernet_0.8.0_amd64.deb" in files["SHA256SUMS"]


def test_package_still_requires_auth(published):
    svc, _admin, rid = published
    with pytest.raises(ForbiddenError):
        svc.download_artifact_public(release_id=rid, name="aithernet_0.8.0_amd64.deb")


def test_cli_release_verify_passes_then_detects_tamper(published, tmp_path):
    svc, _admin, rid = published
    files = _fetch_set(svc, rid)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "aithernet_0.8.0_amd64.deb").write_bytes(_DEB)

    cli.release_verify(str(tmp_path))  # no raise == verified

    # Tamper with the package -> SHA256SUMS + manifest-digest check must fail.
    (tmp_path / "aithernet_0.8.0_amd64.deb").write_bytes(_DEB + b"TAMPER")
    with pytest.raises(typer.Exit) as exc:
        cli.release_verify(str(tmp_path))
    assert exc.value.exit_code == 1


def test_cli_release_verify_detects_bad_signature(published, tmp_path):
    svc, _admin, rid = published
    files = _fetch_set(svc, rid)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "aithernet_0.8.0_amd64.deb").write_bytes(_DEB)
    # Corrupt the manifest so the signature no longer matches.
    (tmp_path / "manifest.json").write_bytes(files["manifest.json"] + b" ")
    with pytest.raises(typer.Exit):
        cli.release_verify(str(tmp_path))


def test_revoked_release_serves_no_verification_files(published):
    svc, admin, rid = published
    svc.revoke_release(admin, release_id=rid)
    with pytest.raises(ForbiddenError):
        svc.download_artifact_public(release_id=rid, name="manifest.json")
