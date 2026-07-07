"""beta.3 hosted control-plane tests: node rename, enrollment-code lifecycle, hosted meshes,
fleet states, and cross-tenant isolation at the hosted layer."""

from __future__ import annotations

import secrets

import pytest
from services.control_plane import roles
from services.control_plane.config import HostedConfig
from services.control_plane.errors import ControlPlaneError
from services.control_plane.security import generate_keypair, load_private_key, sign


@pytest.fixture
def session_key(monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", secrets.token_urlsafe(48))


@pytest.fixture
def svc(tmp_path, session_key):
    from services.control_plane.service import ControlPlaneService

    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'hosted.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    return ControlPlaneService(cfg)


@pytest.fixture
def admin(svc):
    svc.bootstrap_admin("admin@example.invalid", "supersecret-123")
    sess = svc.create_session(_uid(svc, "admin@example.invalid"))
    principal, _, _ = svc.validate_session(sess["session_token"])
    return principal


def _uid(svc, email):
    from services.control_plane.models import HostedUser
    from sqlalchemy import select
    with svc._session() as s:
        return s.execute(select(HostedUser).where(HostedUser.email == email)).scalar_one().id


def _tenant(svc, admin, *, email, name):
    inv = svc.create_invitation(admin, email=email, role=roles.TENANT_ADMIN,
                                proposed_tenant_name=name)
    svc.accept_invitation(inv["token"], email=email, password="tenant-password-9")
    auth = svc.authenticate(email, "tenant-password-9")
    sess = svc.create_session(auth["user_id"])
    principal, _, _ = svc.validate_session(sess["session_token"])
    return principal, svc.list_tenants(principal)[0]["tenant_id"]


def _enroll(svc, principal, tid, node_id):
    code = svc.create_enrollment_code(principal, tenant_id=tid)
    seed, pub = generate_keypair()
    chal = svc.enroll_challenge(code=code["code"], public_key=pub)
    sig = sign(load_private_key(seed), chal["challenge"].encode())
    return svc.enroll(code=code["code"], public_key=pub, node_id=node_id, signature=sig,
                      software_version="1.0.0-beta.3", display_name=node_id)


def test_node_rename(svc, admin):
    p, tid = _tenant(svc, admin, email="a@example.invalid", name="Acme")
    node = _enroll(svc, p, tid, "n1")
    svc.rename_node(p, node["hosted_node_id"], "Edge Gateway 1")
    got = svc.get_node(p, node["hosted_node_id"])
    assert got["display_name"] == "Edge Gateway 1"


def test_enrollment_code_list_and_revoke(svc, admin):
    p, tid = _tenant(svc, admin, email="a@example.invalid", name="Acme")
    code = svc.create_enrollment_code(p, tenant_id=tid)
    listed = svc.list_enrollment_codes(p, tid)
    assert any(c["enrollment_code_id"] == code["enrollment_code_id"] for c in listed)
    # The plaintext code is never returned by the listing (digest-only storage).
    assert all("code" not in c for c in listed)
    svc.revoke_enrollment_code(p, code["enrollment_code_id"])
    after = svc.list_enrollment_codes(p, tid)
    assert next(c for c in after if c["enrollment_code_id"] == code["enrollment_code_id"])[
        "status"] == "revoked"


def test_hosted_mesh_create_add_revoke_and_node_membership(svc, admin):
    p, tid = _tenant(svc, admin, email="a@example.invalid", name="Acme")
    n1 = _enroll(svc, p, tid, "n1")
    n2 = _enroll(svc, p, tid, "n2")
    mesh = svc.create_mesh(p, tenant_id=tid, display_name="Acme Mesh")
    svc.add_mesh_member(p, mesh["mesh_id"], hosted_node_id=n1["hosted_node_id"])
    svc.add_mesh_member(p, mesh["mesh_id"], hosted_node_id=n2["hosted_node_id"])
    detail = svc.get_mesh(p, mesh["mesh_id"])
    assert detail["member_count"] == 2
    # The node listing reflects mesh membership.
    node = svc.get_node(p, n1["hosted_node_id"])
    assert mesh["mesh_id"] in node["mesh_memberships"]
    # Revoke takes effect.
    svc.revoke_mesh_member(p, mesh["mesh_id"], n1["hosted_node_id"])
    assert svc.get_mesh(p, mesh["mesh_id"])["member_count"] == 1
    assert mesh["mesh_id"] not in svc.get_node(p, n1["hosted_node_id"])["mesh_memberships"]


def test_cross_tenant_mesh_membership_rejected(svc, admin):
    pa, tida = _tenant(svc, admin, email="a@example.invalid", name="TenantA")
    pb, tidb = _tenant(svc, admin, email="b@example.invalid", name="TenantB")
    a1 = _enroll(svc, pa, tida, "a1")
    b1 = _enroll(svc, pb, tidb, "b1")
    mesh_a = svc.create_mesh(pa, tenant_id=tida, display_name="MeshA")
    # TenantA admin cannot add a TenantB node to MeshA.
    with pytest.raises(ControlPlaneError):
        svc.add_mesh_member(pa, mesh_a["mesh_id"], hosted_node_id=b1["hosted_node_id"])
    # And cannot even create a mesh in TenantB.
    with pytest.raises(ControlPlaneError):
        svc.create_mesh(pa, tenant_id=tidb, display_name="hijack")
    # B cannot see A's nodes.
    b_nodes = {n["node_id"] for n in svc.list_nodes(pb, tidb)}
    assert b_nodes == {"b1"} and "a1" not in b_nodes
    assert a1["tenant_id"] == tida and b1["tenant_id"] == tidb


def test_tenant_isolation_node_listing(svc, admin):
    pa, tida = _tenant(svc, admin, email="a@example.invalid", name="TenantA")
    pb, tidb = _tenant(svc, admin, email="b@example.invalid", name="TenantB")
    _enroll(svc, pa, tida, "a1")
    _enroll(svc, pa, tida, "a2")
    _enroll(svc, pb, tidb, "b1")
    a_nodes = {n["node_id"] for n in svc.list_nodes(pa, tida)}
    b_nodes = {n["node_id"] for n in svc.list_nodes(pb, tidb)}
    assert a_nodes == {"a1", "a2"}
    assert b_nodes == {"b1"}
    # A cannot list B's tenant.
    with pytest.raises(ControlPlaneError):
        svc.list_nodes(pa, tidb)
