"""beta.3 tenant-isolated secure-mesh tests.

Two tenants, two nodes each. Proves that mesh membership — not mere key trust or network
reachability — authorizes peer communication, and that cross-tenant traffic is rejected before
MissionEngine ingress unless an explicit shared mesh exists. Even a fully key-trusted peer in
another tenant is rejected; guessed mesh ids, overheard/copied envelopes and forged authorities do
not bypass isolation; revocation takes effect immediately.
"""

from __future__ import annotations

import asyncio

import pytest

from _transport_util import init_and_peer, make_runtime
from aithernet.mesh import AUTHORITY_TENANT, evaluate_mesh_ingress
from aithernet.state.repositories import MeshRepository
from aithernet.transport.envelope import build_envelope
from aithernet.transport.errors import MeshAuthorizationError

run = asyncio.run


def _add_mesh(runtime, *, mesh_id, authority_id, members):
    """members: list of (node_id, fingerprint). The local node row is marked is_self."""
    with runtime.session_scope() as s:
        repo = MeshRepository(s)
        if repo.get_mesh(mesh_id) is None:
            repo.create_mesh(
                mesh_id=mesh_id, display_name=mesh_id, authority_type=AUTHORITY_TENANT,
                authority_id=authority_id,
            )
        for node_id, fp in members:
            repo.upsert_member(
                mesh_id=mesh_id, node_id=node_id, fingerprint=fp,
                is_self=(node_id == runtime.config.node_id),
            )
        s.commit()


def _revoke(runtime, mesh_id, node_id):
    with runtime.session_scope() as s:
        MeshRepository(s).revoke_member(mesh_id, node_id)
        s.commit()


async def _deliver(sender_runtime, sender_identity, receiver_runtime, *, mesh_id, authority_id):
    env = build_envelope(
        identity=sender_identity,
        recipient_node_id=receiver_runtime.config.node_id,
        recipient_agent_id=None,
        kind="agent_message",
        payload={"text": "hello"},
        mesh_id=mesh_id,
        authority_id=authority_id,
    )
    return await receiver_runtime.transport.handle_inbound(env.authenticated_dict())


def test_same_mesh_delivery_succeeds(tmp_path):
    async def scenario():
        a1 = make_runtime(tmp_path, "A1", "A1")
        a2 = make_runtime(tmp_path, "A2", "A2")
        _, _ = await init_and_peer(a1, a2, trust=True)
        ia1 = await a1.transport.initialize_identity()
        ia2 = await a2.transport.initialize_identity()
        members = [("A1", ia1.fingerprint), ("A2", ia2.fingerprint)]
        _add_mesh(a1, mesh_id="meshA", authority_id="tenantA", members=members)
        _add_mesh(a2, mesh_id="meshA", authority_id="tenantA", members=members)
        ack = await _deliver(a1, ia1, a2, mesh_id="meshA", authority_id="tenantA")
        assert ack["status"] == "accepted"

    run(scenario())


def test_cross_tenant_rejected_even_when_key_trusted(tmp_path):
    async def scenario():
        a1 = make_runtime(tmp_path, "A1", "A1")
        b1 = make_runtime(tmp_path, "B1", "B1")
        # Fully key-trust each other at the transport layer (the strong case).
        await init_and_peer(a1, b1, trust=True)
        ia1 = await a1.transport.initialize_identity()
        ib1 = await b1.transport.initialize_identity()
        # A1 belongs to meshA (tenantA); B1 is NOT a member.
        _add_mesh(a1, mesh_id="meshA", authority_id="tenantA",
                  members=[("A1", ia1.fingerprint)])
        # B1 forging meshA membership / authority does not help — A1 checks its OWN store.
        with pytest.raises(MeshAuthorizationError):
            await _deliver(b1, ib1, a1, mesh_id="meshA", authority_id="tenantA")
        # B1 naming its own (foreign) mesh is also rejected (A1 has no such mesh).
        with pytest.raises(MeshAuthorizationError):
            await _deliver(b1, ib1, a1, mesh_id="meshB", authority_id="tenantB")
        # A message with NO mesh id is rejected once the node enforces membership.
        with pytest.raises(MeshAuthorizationError):
            await _deliver(b1, ib1, a1, mesh_id=None, authority_id=None)

    run(scenario())


def test_explicit_cross_tenant_mesh_authorizes_then_revocation_blocks(tmp_path):
    async def scenario():
        a1 = make_runtime(tmp_path, "A1", "A1")
        b1 = make_runtime(tmp_path, "B1", "B1")
        await init_and_peer(a1, b1, trust=True)
        ia1 = await a1.transport.initialize_identity()
        ib1 = await b1.transport.initialize_identity()
        # An administrator creates an explicit shared cross-tenant mesh on A1 with both members.
        members = [("A1", ia1.fingerprint), ("B1", ib1.fingerprint)]
        _add_mesh(a1, mesh_id="bridge", authority_id="tenantA", members=members)
        ack = await _deliver(b1, ib1, a1, mesh_id="bridge", authority_id="tenantA")
        assert ack["status"] == "accepted"
        # Revoking B1's membership blocks it immediately.
        _revoke(a1, "bridge", "B1")
        with pytest.raises(MeshAuthorizationError):
            await _deliver(b1, ib1, a1, mesh_id="bridge", authority_id="tenantA")

    run(scenario())


def test_wrong_authority_is_rejected_pure_logic():
    from aithernet.mesh import MeshMemberView, MeshView

    mesh = MeshView(
        "m", "m", AUTHORITY_TENANT, "tenantA",
        members=(MeshMemberView("A", "fpa"), MeshMemberView("B", "fpb")),
    )
    # Correct authority -> allow.
    assert evaluate_mesh_ingress(
        local_node_id="A", sender_node_id="B", sender_fingerprint="fpb",
        envelope_mesh_id="m", envelope_authority_id="tenantA", local_meshes=[mesh],
    ).allowed
    # Forged foreign authority for the same mesh id -> deny (cross-tenant).
    res = evaluate_mesh_ingress(
        local_node_id="A", sender_node_id="B", sender_fingerprint="fpb",
        envelope_mesh_id="m", envelope_authority_id="tenantB", local_meshes=[mesh],
    )
    assert not res.allowed and "cross-tenant" in res.reason


def test_revoked_sender_membership_rejected_pure_logic():
    from aithernet.mesh import MeshMemberView, MeshView

    mesh = MeshView(
        "m", "m", AUTHORITY_TENANT, "tenantA",
        members=(MeshMemberView("A", "fpa"), MeshMemberView("B", "fpb", revoked=True)),
    )
    res = evaluate_mesh_ingress(
        local_node_id="A", sender_node_id="B", sender_fingerprint="fpb",
        envelope_mesh_id="m", envelope_authority_id="tenantA", local_meshes=[mesh],
    )
    assert not res.allowed and "revoked" in res.reason


def test_key_mismatch_in_mesh_member_rejected_pure_logic():
    from aithernet.mesh import MeshMemberView, MeshView

    mesh = MeshView(
        "m", "m", AUTHORITY_TENANT, "tenantA",
        members=(MeshMemberView("A", "fpa"), MeshMemberView("B", "fpb")),
    )
    res = evaluate_mesh_ingress(
        local_node_id="A", sender_node_id="B", sender_fingerprint="ROTATED",
        envelope_mesh_id="m", envelope_authority_id="tenantA", local_meshes=[mesh],
    )
    assert not res.allowed and "pinned mesh-member key" in res.reason
