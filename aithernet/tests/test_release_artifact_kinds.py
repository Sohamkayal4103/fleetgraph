"""Release artifact-kind correctness (beta.5 Area 4).

Artifacts must carry an accurate explicit kind end to end (service -> DB -> manifest), not be
labelled ``wheel`` by default. Also covers the PEM public-key helper (Area 1).
"""

from __future__ import annotations

import base64
import tempfile

import pytest

from services.control_plane import release_signing
from services.control_plane.config import HostedConfig
from services.control_plane.security import generate_keypair
from services.control_plane.service import ControlPlaneService, Principal


@pytest.mark.parametrize("name,kind", [
    ("aithernet_0.8.0~beta.5_amd64.deb", "deb"),
    ("aithernet-0.8.0b5-py3-none-any.whl", "wheel"),
    ("aithernet-0.8.0b5.tar.gz", "sdist"),
    ("manifest.json", "manifest"),
    ("SHA256SUMS", "checksums"),
    ("manifest.sig", "signature"),
    ("aithernet-release.pub", "pubkey"),
    ("sbom.json", "sbom"),
    ("THIRD-PARTY-NOTICES.md", "notices"),
    ("NOTICE.md", "notices"),
    ("rf-mcp-0.1.0+aithernet.2-src.tar.gz", "component-source"),
    ("aithernet-install.sh", "installer"),
])
def test_infer_kind(name, kind):
    assert release_signing.infer_kind(name) == kind


def test_public_key_pem_round_trip_verifies():
    seed, pub_b64 = generate_keypair()
    manifest = release_signing.canonical_manifest(
        version="0.8.0-test", channel="early-access", signing_key_id="k1",
        artifacts=[{"name": "a.deb", "kind": "deb", "sha256": "x", "byte_size": 1}])
    sig_b64 = release_signing.sign_manifest(seed, manifest)
    # The PEM helper yields a key openssl can use; verify the detached sig over the canonical form.
    pem = release_signing.public_key_pem(pub_b64)
    assert "BEGIN PUBLIC KEY" in pem
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    key = load_pem_public_key(pem.encode())
    key.verify(base64.b64decode(sig_b64), release_signing.manifest_bytes(manifest))  # no raise


@pytest.fixture
def admin_svc(monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", "x" * 48)
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tempfile.mktemp()}"
    cfg.object_storage.local_root = tempfile.mkdtemp()
    svc = ControlPlaneService(cfg)
    a = svc.bootstrap_admin("admin@example.invalid", "pw-correct-horse")
    return svc, Principal(user_id=a["user_id"], email=a["email"], is_platform_admin=True)


def test_manifest_kinds_are_accurate_not_all_wheel(admin_svc):
    svc, admin = admin_svc
    seed, _ = generate_keypair()
    rid = svc.create_release(admin, version="0.8.0-test")["release_id"]
    files = {
        "aithernet_0.8.0_amd64.deb": "deb",
        "aithernet-0.8.0-py3-none-any.whl": "wheel",
        "aithernet-0.8.0.tar.gz": "sdist",
        "rf-mcp-0.1.0+aithernet.2-src.tar.gz": "component-source",
        "sbom.json": "sbom",
        "NOTICE.md": "notices",
    }
    for name in files:
        svc.add_release_artifact(admin, release_id=rid, name=name, data=b"BYTES-" + name.encode())
    svc.sign_release(admin, release_id=rid, signing_seed_b64=seed)
    manifest = svc.release_manifest(release_id=rid)["manifest"]
    got = {a["name"]: a["kind"] for a in manifest["artifacts"]}
    assert got == files                      # each kind accurate
    assert set(got.values()) != {"wheel"}    # not all labelled wheel
