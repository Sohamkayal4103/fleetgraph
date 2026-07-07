"""``aithernet mesh`` CLI — first-class mesh management (1.0.0-beta.3).

Mesh commands operate DIRECTLY on the node's local state database and identity, so they work fully
offline / standalone — no running server, hosted account, portal or internet connection is
required (the standalone-mesh requirement). In hosted mode the same local meshes are what the
control plane synchronizes; nothing here depends on the hosted platform.

Never prints or stores a private key. ``mesh invite-node`` pins another node's PUBLIC key only.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from aithernet.config.loader import load_config
from aithernet.mesh import (
    ADMIN_ROLE,
    AUTHORITY_STANDALONE,
    AUTHORITY_TENANT,
    DEFAULT_MESH_SCOPES,
    MEMBER_ROLE,
)
from aithernet.state.db import create_db_engine, create_session_factory, init_db
from aithernet.state.repositories import MeshRepository
from aithernet.transport.identity import (
    IdentityManager,
    default_identity_dir,
    fingerprint_for_public_key,
)

mesh_app = typer.Typer(
    name="mesh",
    help="Manage meshes — the explicit trust groups that authorize peer communication.",
    no_args_is_help=True,
)


def _local(config: str):
    """Return (NodeConfig, session_factory, local_identity_or_none) for offline mesh ops."""
    cfg = load_config(config)
    engine = create_db_engine(cfg.database_url)
    init_db(engine)  # ensure mesh tables exist (additive migration 0020)
    factory = create_session_factory(engine)
    idir = cfg.agent_transport.identity.state_directory
    manager = IdentityManager(
        Path(idir) if idir else default_identity_dir(),
        node_id=cfg.node_id,
        node_name=cfg.node_name,
    )
    return cfg, factory, manager.load_or_none()


def _mesh_dict(mesh, members) -> dict:
    return {
        "mesh_id": mesh.id,
        "display_name": mesh.display_name,
        "authority_type": mesh.authority_type,
        "authority_id": mesh.authority_id,
        "owner_node_id": mesh.owner_node_id,
        "policy_version": mesh.policy_version,
        "scopes": mesh.scopes_json or list(DEFAULT_MESH_SCOPES),
        "revoked": mesh.revoked,
        "synced": mesh.synced,
        "member_count": len([m for m in members if not m.revoked]),
    }


def _member_dict(m) -> dict:
    return {
        "node_id": m.node_id,
        "display_name": m.display_name,
        "fingerprint": m.fingerprint,
        "role": m.role,
        "scopes": m.scopes_json or [],
        "is_self": m.is_self,
        "revoked": m.revoked,
    }


ConfigOpt = typer.Option("configs/node.yaml", "--config", "-c", help="Path to the node config.")


@mesh_app.command("create")
def mesh_create(
    name: str = typer.Option(..., "--name", help="Human-readable mesh name."),
    authority_type: str = typer.Option(
        AUTHORITY_STANDALONE, "--authority-type",
        help="standalone (local owner) or tenant (hosted)."),
    authority_id: str | None = typer.Option(
        None, "--authority-id",
        help="Tenant id (hosted) or owner id. Defaults to this node's fingerprint (standalone)."),
    config: str = ConfigOpt,
) -> None:
    """Create a mesh and add THIS node as its first admin member (works offline)."""
    if authority_type not in (AUTHORITY_STANDALONE, AUTHORITY_TENANT):
        raise typer.BadParameter("authority-type must be 'standalone' or 'tenant'")
    cfg, factory, identity = _local(config)
    if identity is None:
        typer.secho("This node has no identity yet — run `aithernet identity initialize` first.",
                    fg=typer.colors.RED)
        raise typer.Exit(1)
    resolved_authority = authority_id or (
        identity.fingerprint if authority_type == AUTHORITY_STANDALONE else None)
    if not resolved_authority:
        raise typer.BadParameter("a tenant mesh requires --authority-id")
    with factory() as s:
        repo = MeshRepository(s)
        mesh = repo.create_mesh(
            display_name=name, authority_type=authority_type, authority_id=resolved_authority,
            owner_node_id=cfg.node_id, scopes=list(DEFAULT_MESH_SCOPES),
        )
        repo.upsert_member(
            mesh_id=mesh.id, node_id=cfg.node_id, public_key=identity.public_key_b64,
            fingerprint=identity.fingerprint, display_name=cfg.node_name, role=ADMIN_ROLE,
            is_self=True,
        )
        s.commit()
        members = repo.list_members(mesh.id)
        typer.echo(json.dumps(_mesh_dict(mesh, members), indent=2))


@mesh_app.command("list")
def mesh_list(config: str = ConfigOpt) -> None:
    """List meshes this node belongs to."""
    _, factory, _ = _local(config)
    with factory() as s:
        repo = MeshRepository(s)
        meshes = repo.list_meshes()
        if not meshes:
            typer.echo("No meshes. Create one with `aithernet mesh create`\n"
                       "or join with `aithernet mesh join`.")
            return
        for mesh in meshes:
            typer.echo(json.dumps(_mesh_dict(mesh, repo.list_members(mesh.id)), indent=2))


@mesh_app.command("inspect")
def mesh_inspect(mesh_id: str, config: str = ConfigOpt) -> None:
    """Inspect one mesh and its members."""
    _, factory, _ = _local(config)
    with factory() as s:
        repo = MeshRepository(s)
        mesh = repo.get_mesh(mesh_id)
        if mesh is None:
            typer.secho("No such mesh.", fg=typer.colors.RED)
            raise typer.Exit(1)
        members = repo.list_members(mesh_id)
        out = _mesh_dict(mesh, members)
        out["members"] = [_member_dict(m) for m in members]
        typer.echo(json.dumps(out, indent=2))


@mesh_app.command("members")
def mesh_members(mesh_id: str, config: str = ConfigOpt) -> None:
    """List the members of a mesh."""
    _, factory, _ = _local(config)
    with factory() as s:
        repo = MeshRepository(s)
        if repo.get_mesh(mesh_id) is None:
            typer.secho("No such mesh.", fg=typer.colors.RED)
            raise typer.Exit(1)
        for m in repo.list_members(mesh_id):
            typer.echo(json.dumps(_member_dict(m), indent=2))


@mesh_app.command("join")
def mesh_join(
    mesh_id: str = typer.Option(..., "--mesh-id", help="The mesh id to join (from its owner)."),
    name: str = typer.Option(..., "--name", help="Mesh display name."),
    authority_type: str = typer.Option(AUTHORITY_STANDALONE, "--authority-type"),
    authority_id: str = typer.Option(..., "--authority-id", help="The mesh authority id."),
    owner_node_id: str | None = typer.Option(None, "--owner-node-id"),
    config: str = ConfigOpt,
) -> None:
    """Join an existing mesh locally and register THIS node as a member (offline pairing)."""
    cfg, factory, identity = _local(config)
    if identity is None:
        typer.secho("This node has no identity yet — run `aithernet identity initialize` first.",
                    fg=typer.colors.RED)
        raise typer.Exit(1)
    with factory() as s:
        repo = MeshRepository(s)
        if repo.get_mesh(mesh_id) is None:
            repo.create_mesh(
                display_name=name, authority_type=authority_type, authority_id=authority_id,
                owner_node_id=owner_node_id, scopes=list(DEFAULT_MESH_SCOPES), mesh_id=mesh_id,
            )
        repo.upsert_member(
            mesh_id=mesh_id, node_id=cfg.node_id, public_key=identity.public_key_b64,
            fingerprint=identity.fingerprint, display_name=cfg.node_name, role=MEMBER_ROLE,
            is_self=True,
        )
        s.commit()
        typer.secho(f"Joined mesh {mesh_id} as {cfg.node_id}.", fg=typer.colors.GREEN)


@mesh_app.command("leave")
def mesh_leave(mesh_id: str, config: str = ConfigOpt) -> None:
    """Leave a mesh (revoke THIS node's own membership locally)."""
    cfg, factory, _ = _local(config)
    with factory() as s:
        repo = MeshRepository(s)
        if repo.revoke_member(mesh_id, cfg.node_id) is None:
            typer.secho("Not a member of that mesh.", fg=typer.colors.RED)
            raise typer.Exit(1)
        s.commit()
        typer.secho(f"Left mesh {mesh_id}.", fg=typer.colors.GREEN)


@mesh_app.command("invite-node")
def mesh_invite_node(
    mesh_id: str,
    node_id: str = typer.Option(..., "--node-id", help="The peer node id to add."),
    public_key: str = typer.Option(
        ..., "--public-key", help="The peer's base64 Ed25519 PUBLIC key."),
    role: str = typer.Option(MEMBER_ROLE, "--role", help="member or admin."),
    name: str | None = typer.Option(None, "--name", help="Display name for the member."),
    config: str = ConfigOpt,
) -> None:
    """Add another node to a mesh by pinning its PUBLIC key (never a private key)."""
    _, factory, _ = _local(config)
    fingerprint = fingerprint_for_public_key(public_key)
    with factory() as s:
        repo = MeshRepository(s)
        if repo.get_mesh(mesh_id) is None:
            typer.secho("No such mesh.", fg=typer.colors.RED)
            raise typer.Exit(1)
        m = repo.upsert_member(
            mesh_id=mesh_id, node_id=node_id, public_key=public_key, fingerprint=fingerprint,
            display_name=name, role=role,
        )
        s.commit()
        typer.echo(json.dumps(_member_dict(m), indent=2))


@mesh_app.command("revoke-node")
def mesh_revoke_node(mesh_id: str, node_id: str, config: str = ConfigOpt) -> None:
    """Revoke a node's membership in a mesh (takes effect immediately on this node)."""
    _, factory, _ = _local(config)
    with factory() as s:
        repo = MeshRepository(s)
        if repo.revoke_member(mesh_id, node_id) is None:
            typer.secho("No such member.", fg=typer.colors.RED)
            raise typer.Exit(1)
        s.commit()
        typer.secho(f"Revoked {node_id} from mesh {mesh_id}.", fg=typer.colors.GREEN)


def register(app: typer.Typer) -> None:
    """Attach the ``mesh`` command group to the root CLI app."""
    app.add_typer(mesh_app, name="mesh")
