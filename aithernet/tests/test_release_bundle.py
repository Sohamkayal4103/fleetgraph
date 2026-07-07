"""Single-ZIP Ubuntu delivery-bundle tests (live acceptance defect #2).

The control plane assembles ONE authenticated ZIP server-side from the EXACT stored release bytes:
every contained file's bytes equal the original release artifact (verified against the stored
digest before inclusion); the Python wheel/sdist are excluded (the bundle is the apt path); the ZIP
is deterministic so its SHA-256 is reproducible; anonymous/unauthorized requests are rejected. The
signed manifest + contained digests remain the trust root — the ZIP is only a delivery container.
"""

from __future__ import annotations

import io
import tempfile
import zipfile

import pytest
from services.control_plane import roles
from services.control_plane.blobstore import sha256_hex
from services.control_plane.config import HostedConfig
from services.control_plane.errors import ControlPlaneError
from services.control_plane.security import generate_keypair
from services.control_plane.service import ControlPlaneService, Principal

# Distinct stored bytes per file so a mismatch can't pass by coincidence.
DEB = b"DEB-BYTES-beta5-amd64"
SRC = b"RFMCP-SOURCE-TARBALL-aithernet.2"
SIG = b"RFMCP-DETACHED-SIGNATURE"
CPUB = b"-----BEGIN COMPONENT PUBKEY-----"
LOCK = b'{"component":"rf-mcp","version":"0.1.0+aithernet.2"}'
NOTICE = b"Aithernet NOTICE"
TPN = b"third-party notices"
INSTALL = b"#!/bin/sh\necho offline install"
WHEEL = b"PYTHON-WHEEL-should-be-excluded"
SDIST = b"PYTHON-SDIST-should-be-excluded"

# Every artifact is included in the bundle (so the included SHA256SUMS — which references every
# artifact — passes `sha256sum -c` from the extracted directory).
BUNDLED = {
    "aithernet_0.8.0~beta.5_amd64.deb": DEB,
    "aithernet-0.8.0b5-py3-none-any.whl": WHEEL,
    "aithernet-0.8.0b5.tar.gz": SDIST,
    "rf-mcp-0.1.0+aithernet.2-src.tar.gz": SRC,
    "rf-mcp-0.1.0+aithernet.2-src.tar.gz.sig": SIG,
    "aithernet-component.pub": CPUB,
    "lock.json": LOCK,
    "NOTICE.md": NOTICE,
    "THIRD-PARTY-NOTICES.md": TPN,
    "aithernet-install.sh": INSTALL,
}
VERIFICATION = ("manifest.json", "manifest.sig", "SHA256SUMS", "aithernet-release.pub")


@pytest.fixture
def svc(monkeypatch):
    seed, pub = generate_keypair()
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", "x" * 48)
    # The verification key is synthesized from this env var (as in production).
    monkeypatch.setenv("AITHERNET_RELEASE_PUBLIC_KEY", pub)
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tempfile.mktemp()}"
    cfg.object_storage.local_root = tempfile.mkdtemp()
    service = ControlPlaneService(cfg)
    service._test_release_seed = seed  # matched to the public key above
    return service


@pytest.fixture
def admin(svc):
    a = svc.bootstrap_admin("admin@example.invalid", "pw-correct-horse")
    return Principal(user_id=a["user_id"], email=a["email"], is_platform_admin=True)


def _release(svc, admin):
    from services.control_plane.service_releases import _BUNDLE_CACHE
    _BUNDLE_CACHE.clear()
    rel = svc.create_release(admin, version="0.8.0-beta.5")
    rid = rel["release_id"]
    for name, data in BUNDLED.items():
        svc.add_release_artifact(admin, release_id=rid, name=name, data=data)
    svc.sign_release(admin, release_id=rid, signing_seed_b64=svc._test_release_seed)
    svc.publish_release(admin, release_id=rid)
    return rid


def _publish_policy(svc, admin):
    svc.publish_policy(admin, policy_type="terms", version="t1", title="Terms",
                       document_text="be nice")


def _customer(svc, admin, *, accept_policy=True):
    inv = svc.create_invitation(admin, email="cust@example.invalid",
                                proposed_tenant_name="Acme", role=roles.TENANT_OPERATOR)
    svc.accept_invitation(inv["token"], email="cust@example.invalid",
                          password="cust-correct-horse-9", accept_required_policies=accept_policy)
    auth = svc.authenticate("cust@example.invalid", "cust-correct-horse-9")
    sess = svc.create_session(auth["user_id"])
    principal, _, _ = svc.validate_session(sess["session_token"])
    return principal


def test_bundle_filename_and_membership(svc, admin):
    rid = _release(svc, admin)
    p = _customer(svc, admin)
    b = svc.download_release_bundle_customer(p, release_id=rid)
    assert b["filename"] == "aithernet-0.8.0-beta.5-ubuntu24.04-amd64.zip"
    zf = zipfile.ZipFile(io.BytesIO(b["zip"]))
    names = set(zf.namelist())
    for n in (*BUNDLED, *VERIFICATION):
        assert n in names, f"{n} must be in the bundle"


def test_sha256sums_command_passes_over_bundle(svc, admin):
    """The documented `sha256sum -c SHA256SUMS` must succeed from the extracted directory:
    every file the included SHA256SUMS references is present and matches its digest."""
    rid = _release(svc, admin)
    p = _customer(svc, admin)
    b = svc.download_release_bundle_customer(p, release_id=rid)
    zf = zipfile.ZipFile(io.BytesIO(b["zip"]))
    present = set(zf.namelist())
    checked = 0
    for line in zf.read("SHA256SUMS").decode().splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")  # "<hex>  <name>"
        assert name in present, f"SHA256SUMS references {name} but it is missing from the ZIP"
        assert sha256_hex(zf.read(name)).split(":", 1)[-1] == digest, f"{name} digest mismatch"
        checked += 1
    assert checked >= len(BUNDLED)  # every artifact + manifest.json is verified


def test_bundle_bytes_equal_exact_stored_artifacts(svc, admin):
    rid = _release(svc, admin)
    p = _customer(svc, admin)
    b = svc.download_release_bundle_customer(p, release_id=rid)
    zf = zipfile.ZipFile(io.BytesIO(b["zip"]))
    # Every real artifact in the ZIP is byte-identical to what was stored (no rebuild/alteration).
    for name, data in BUNDLED.items():
        assert zf.read(name) == data
    # And byte-identical to the per-file authenticated download (Advanced / All artifacts path).
    for name in zf.namelist():
        indiv, _ct, _sha = svc.download_artifact_customer(p, release_id=rid, name=name)
        assert zf.read(name) == indiv
    # SHA256SUMS inside the ZIP carries the stored digest of the .deb.
    sums = zf.read("SHA256SUMS").decode()
    assert sha256_hex(DEB).split(":", 1)[-1] in sums
    # The ZIP entries report their digests; meta lists them for display.
    by_name = {f["name"]: f["sha256"] for f in b["files"]}
    assert by_name["aithernet_0.8.0~beta.5_amd64.deb"] == sha256_hex(DEB)


def test_bundle_sha256_is_deterministic_and_self_consistent(svc, admin):
    rid = _release(svc, admin)
    p = _customer(svc, admin)
    a = svc.download_release_bundle_customer(p, release_id=rid)
    from services.control_plane.service_releases import _BUNDLE_CACHE
    _BUNDLE_CACHE.clear()  # force a genuine rebuild, not a cache hit
    b = svc.download_release_bundle_customer(p, release_id=rid)
    assert a["zip"] == b["zip"]                       # reproducible bytes
    assert a["sha256"] == b["sha256"] == sha256_hex(a["zip"])
    assert a["byte_size"] == len(a["zip"])


def test_bundle_rejects_anonymous_and_unauthorized(svc, admin):
    rid = _release(svc, admin)
    _publish_policy(svc, admin)
    # No memberships -> not a customer.
    nobody = Principal(user_id="x", email="x@x", is_platform_admin=False)
    with pytest.raises(ControlPlaneError) as e1:
        svc.download_release_bundle_customer(nobody, release_id=rid)
    assert "not_a_customer" in e1.value.code
    # Customer who has not accepted the required policy.
    p_no = _customer(svc, admin, accept_policy=False)
    with pytest.raises(ControlPlaneError) as e2:
        svc.download_release_bundle_customer(p_no, release_id=rid)
    assert "polic" in e2.value.code


def test_bundle_not_available_for_revoked_release(svc, admin):
    rid = _release(svc, admin)
    p = _customer(svc, admin)
    svc.revoke_release(admin, release_id=rid)
    with pytest.raises(ControlPlaneError) as exc:
        svc.download_release_bundle_customer(p, release_id=rid)
    assert "not_downloadable" in exc.value.code or "not_found" in exc.value.code
