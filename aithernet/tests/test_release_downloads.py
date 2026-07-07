"""Download-authorization tests (Stage 14F productization).

Closes the hidden-unauthenticated-URL hole: the public route serves only the verification key;
package artifacts require an authenticated customer session (+ membership + accepted required
policies) or a signed enrolled-node request. Revoked releases are never downloadable. Public
metadata exposes release notes + the verification key but no package bytes.
"""

from __future__ import annotations

import tempfile

import pytest

from services.control_plane import roles
from services.control_plane.config import HostedConfig
from services.control_plane.errors import ControlPlaneError
from services.control_plane.security import generate_keypair
from services.control_plane.service import ControlPlaneService, Principal


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", "x" * 48)
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tempfile.mktemp()}"
    cfg.object_storage.local_root = tempfile.mkdtemp()
    return ControlPlaneService(cfg)


@pytest.fixture
def admin(svc):
    a = svc.bootstrap_admin("admin@example.invalid", "pw-correct-horse")
    return Principal(user_id=a["user_id"], email=a["email"], is_platform_admin=True)


def _published_release(svc, admin):
    rseed, _ = generate_keypair()
    rel = svc.create_release(admin, version="0.8.0-test")
    rid = rel["release_id"]
    svc.add_release_artifact(admin, release_id=rid, name="aithernet_0.8.0_amd64.deb", data=b"DEB-BYTES")
    svc.add_release_artifact(admin, release_id=rid, name="aithernet-release.pub", data=b"-----PUBKEY-----")
    svc.sign_release(admin, release_id=rid, signing_seed_b64=rseed)
    svc.publish_release(admin, release_id=rid)
    return rid


def _publish_policy(svc, admin):
    svc.publish_policy(admin, policy_type="terms", version="t1", title="Terms",
                       document_text="be nice")


def _customer(svc, admin, *, accept_policy=True):
    # Onboard a customer (operator role) via invitation; optionally accept required policies.
    inv = svc.create_invitation(admin, email="cust@example.invalid",
                                proposed_tenant_name="Acme", role=roles.TENANT_OPERATOR)
    acc = svc.accept_invitation(inv["token"], email="cust@example.invalid", password="cust-correct-horse-9",
                                accept_required_policies=accept_policy)
    auth = svc.authenticate("cust@example.invalid", "cust-correct-horse-9")
    sess = svc.create_session(auth["user_id"])
    principal, _, _ = svc.validate_session(sess["session_token"])
    return principal, acc["tenant_id"]


def test_public_download_serves_only_verification_key(svc, admin):
    rid = _published_release(svc, admin)
    data, _, _ = svc.download_artifact_public(release_id=rid, name="aithernet-release.pub")
    assert data == b"-----PUBKEY-----"
    with pytest.raises(ControlPlaneError) as exc:  # the .deb is NOT public
        svc.download_artifact_public(release_id=rid, name="aithernet_0.8.0_amd64.deb")
    assert "authentication" in exc.value.code


def test_customer_download_requires_accepted_policy_and_membership(svc, admin):
    rid = _published_release(svc, admin)
    _publish_policy(svc, admin)
    # a customer who has NOT accepted the required policy cannot download the package
    p_no, _ = _customer(svc, admin, accept_policy=False)
    with pytest.raises(ControlPlaneError) as exc:
        svc.download_artifact_customer(p_no, release_id=rid, name="aithernet_0.8.0_amd64.deb")
    assert "polic" in exc.value.code
    # a non-customer principal (no memberships) cannot download
    nobody = Principal(user_id="x", email="x@x", is_platform_admin=False)
    with pytest.raises(ControlPlaneError):
        svc.download_artifact_customer(nobody, release_id=rid, name="aithernet_0.8.0_amd64.deb")


def test_customer_with_policy_can_download(svc, admin):
    rid = _published_release(svc, admin)
    _publish_policy(svc, admin)
    p_ok, _ = _customer(svc, admin, accept_policy=True)
    data, _, _ = svc.download_artifact_customer(p_ok, release_id=rid, name="aithernet_0.8.0_amd64.deb")
    assert data == b"DEB-BYTES"


def test_revoked_release_not_downloadable_anywhere(svc, admin):
    rid = _published_release(svc, admin)
    p_ok, _ = _customer(svc, admin, accept_policy=True)
    svc.revoke_release(admin, release_id=rid)
    for call in (
        lambda: svc.download_artifact_public(release_id=rid, name="aithernet-release.pub"),
        lambda: svc.download_artifact_customer(p_ok, release_id=rid, name="aithernet_0.8.0_amd64.deb"),
        lambda: svc.download_artifact_node(release_id=rid, name="aithernet_0.8.0_amd64.deb"),
    ):
        with pytest.raises(ControlPlaneError) as exc:
            call()
        assert "not_downloadable" in exc.value.code or "not_found" in exc.value.code


def test_public_releases_exposes_metadata_and_key_not_bytes(svc, admin):
    rid = _published_release(svc, admin)
    out = svc.public_releases()
    assert "verification_public_key" in out
    rel = next(r for r in out["releases"] if r["release_id"] == rid)
    names = {a["name"]: a for a in rel["artifacts"]}
    assert names["aithernet-release.pub"]["public"] is True
    assert names["aithernet_0.8.0_amd64.deb"]["public"] is False
    # metadata only — no artifact bytes are present in the public listing
    assert "data" not in names["aithernet_0.8.0_amd64.deb"]
    assert names["aithernet_0.8.0_amd64.deb"]["sha256"]
