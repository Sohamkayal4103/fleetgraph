"""Node CLI onboarding commands (Stage 14F §17).

``aithernet setup`` / ``enroll`` / ``hosted *`` / ``doctor`` drive enrollment to the hosted control
plane and outbound heartbeats. They are operator-driven and deterministic: they never install
arbitrary packages without confirmation, edit shell profiles, expose secrets, enable external
export, grant consent silently, or print long-lived credentials. The node authenticates with its
own Ed25519 identity, so there is no credential to print.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import typer

from aithernet import __version__
from aithernet.hosted.client import HostedClient, HostedClientError
from aithernet.hosted.localconfig import HostedEnrollment, load_enrollment, save_enrollment
from aithernet.transport.identity import IdentityManager

hosted_app = typer.Typer(name="hosted", help="Hosted control-plane enrollment and heartbeat.",
                         no_args_is_help=True)
enrollment_app = typer.Typer(name="enrollment", help="Local enrollment state.",
                             no_args_is_help=True)


def _default_state_root() -> Path:
    return Path(os.environ.get("AITHERNET_STATE_ROOT",
                               str(Path.home() / ".local/share/aithernet")))


def _config_dir(state_root: Path) -> Path:
    return state_root / "config"


def _identity(state_root: Path, node_id: str | None = None):
    manager = IdentityManager(state_root / "identity", node_id=node_id or "aithernet-node",
                              node_name=node_id or "aithernet-node")
    if not manager.exists():
        return None, manager
    return manager.load(), manager


def _echo(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@hosted_app.command("enroll")
def hosted_enroll(
    base_url: str = typer.Option(..., "--base-url", help="Hosted control-plane base URL."),
    code: str = typer.Option(..., "--code", help="One-time enrollment code."),
    node_name: str = typer.Option(None, "--node-name", help="Optional node display name."),
    state_root: Path = typer.Option(None, "--state-root"),
    software_version: str = typer.Option(__version__, "--version"),
) -> None:
    """Enroll this node: present the Ed25519 identity, prove possession, persist hosted state."""
    root = state_root or _default_state_root()
    node_id = node_name or os.environ.get("AITHERNET_NODE_ID", "aithernet-node")
    manager = IdentityManager(root / "identity", node_id=node_id, node_name=node_id)
    identity = manager.initialize()  # idempotent: generates only if absent
    client = HostedClient(base_url)
    try:
        result = client.enroll(code=code, identity=identity, node_id=identity.node_id,
                               software_version=software_version, display_name=node_id)
    except HostedClientError as exc:
        typer.echo(f"enrollment failed: {exc.code}", err=True)
        raise typer.Exit(1) from exc
    enrollment = HostedEnrollment(
        control_plane_base_url=base_url,
        ingestion_base_url=result.get("ingestion_base_url", ""),
        hosted_node_id=result["hosted_node_id"], tenant_id=result["tenant_id"],
        release_channel=result.get("release_channel", "early-access"),
        enrollment_state="enrolled",
    )
    save_enrollment(_config_dir(root), enrollment)
    _echo({"enrolled": True, "hosted_node_id": result["hosted_node_id"],
           "tenant_id": result["tenant_id"], "idempotent": result.get("idempotent", False)})


@hosted_app.command("heartbeat")
def hosted_heartbeat(state_root: Path = typer.Option(None, "--state-root"),
                     sequence: int = typer.Option(None, "--sequence")) -> None:
    """Send one bounded, signed heartbeat to the hosted control plane."""
    root = state_root or _default_state_root()
    enrollment = load_enrollment(_config_dir(root))
    if enrollment is None or enrollment.enrollment_state != "enrolled":
        typer.echo("not enrolled", err=True)
        raise typer.Exit(1)
    identity, _ = _identity(root)
    if identity is None:
        typer.echo("no node identity", err=True)
        raise typer.Exit(1)
    client = HostedClient(enrollment.control_plane_base_url)
    payload = {"sequence": sequence if sequence is not None else int(time.time()),
               "software_version": __version__, "readiness_summary": "ready"}
    try:
        result = client.heartbeat(identity=identity, tenant_id=enrollment.tenant_id,
                                  node_id=identity.node_id, payload=payload)
    except HostedClientError as exc:
        typer.echo(f"heartbeat failed: {exc.code}", err=True)
        raise typer.Exit(1) from exc
    _echo(result)


@hosted_app.command("config")
def hosted_config(state_root: Path = typer.Option(None, "--state-root")) -> None:
    """Fetch the bounded hosted node configuration."""
    root = state_root or _default_state_root()
    enrollment = load_enrollment(_config_dir(root))
    if enrollment is None or enrollment.enrollment_state != "enrolled":
        typer.echo("not enrolled", err=True)
        raise typer.Exit(1)
    identity, _ = _identity(root)
    client = HostedClient(enrollment.control_plane_base_url)
    _echo(client.get_config(identity=identity, tenant_id=enrollment.tenant_id,
                            node_id=identity.node_id))


@hosted_app.command("status")
def hosted_status(state_root: Path = typer.Option(None, "--state-root")) -> None:
    """Show local enrollment status (non-secret)."""
    root = state_root or _default_state_root()
    enrollment = load_enrollment(_config_dir(root))
    if enrollment is None:
        _echo({"enrollment_state": "unenrolled"})
        return
    _echo(enrollment.to_public_dict())


@enrollment_app.command("status")
def enrollment_status(state_root: Path = typer.Option(None, "--state-root")) -> None:
    hosted_status(state_root=state_root)


@enrollment_app.command("revoke-local")
def enrollment_revoke_local(state_root: Path = typer.Option(None, "--state-root")) -> None:
    """Mark the local enrollment as revoked (does not contact the server)."""
    root = state_root or _default_state_root()
    enrollment = load_enrollment(_config_dir(root)) or HostedEnrollment()
    enrollment.enrollment_state = "revoked"
    save_enrollment(_config_dir(root), enrollment)
    _echo({"enrollment_state": "revoked"})


@enrollment_app.command("disconnect")
def enrollment_disconnect(state_root: Path = typer.Option(None, "--state-root")) -> None:
    """Disconnect this node from its hosted account (stops heartbeats; keeps the local identity).

    The node simply ceases to report; the account owner revokes it from the portal/fleet. The
    Ed25519 identity is preserved so the node can re-enroll later or run standalone.
    """
    root = state_root or _default_state_root()
    enrollment = load_enrollment(_config_dir(root)) or HostedEnrollment()
    enrollment.enrollment_state = "disconnected"
    save_enrollment(_config_dir(root), enrollment)
    _echo({"enrollment_state": "disconnected",
           "note": "identity preserved; the account owner may revoke this node from the portal"})


@hosted_app.command("doctor")
def doctor(state_root: Path = typer.Option(None, "--state-root")) -> None:
    """Bounded readiness check of local prerequisites for hosted operation."""
    root = state_root or _default_state_root()
    checks = {
        "state_root_exists": root.exists(),
        "identity_present": (root / "identity").exists()
        and any((root / "identity").glob("*")) if (root / "identity").exists() else False,
        "config_dir_exists": _config_dir(root).exists(),
        "enrolled": (load_enrollment(_config_dir(root)) or HostedEnrollment()).enrollment_state
        == "enrolled",
    }
    _echo({"checks": checks, "ok": all(v for k, v in checks.items() if k != "enrolled")})


def setup(
    state_root: Path = typer.Option(None, "--state-root"),
    base_url: str = typer.Option(None, "--base-url", help="Hosted base URL (to also enroll)."),
    code: str = typer.Option(None, "--code", help="Enrollment code (to also enroll)."),
    node_name: str = typer.Option(None, "--node-name"),
    create_env: bool = typer.Option(False, "--create-env",
                                    help="Create a private local .env if absent."),
) -> None:
    """Deterministic operator-driven setup: directories, identity, validation, optional enroll.

    Never installs packages, edits shell profiles, exposes secrets, enables export, or grants
    consent. Prints bounded next steps."""
    root = state_root or _default_state_root()
    node_id = node_name or os.environ.get("AITHERNET_NODE_ID", "aithernet-node")
    created: list[str] = []
    for sub, mode in (("config", 0o750), ("identity", 0o700), ("db", 0o750),
                      ("artifacts", 0o750), ("logs", 0o750)):
        path = root / sub
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
            created.append(sub)
    manager = IdentityManager(root / "identity", node_id=node_id, node_name=node_id)
    identity = manager.initialize()
    env_path = root / "config" / ".env"
    if create_env and not env_path.exists():
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, b"# Aithernet node environment (operator-managed; never commit)\n")
        os.close(fd)
        created.append("config/.env")
    result = {"state_root": str(root), "node_id": identity.node_id,
              "fingerprint": identity.fingerprint, "created": created,
              "next_steps": ["aithernet enroll --base-url <url> --code <code>",
                             "aithernet start", "aithernet dashboard open"]}
    if base_url and code:
        try:
            hosted_enroll(base_url=base_url, code=code, node_name=node_id, state_root=root,
                          software_version=__version__)
            result["enrolled"] = True
        except typer.Exit:
            result["enrolled"] = False
    _echo(result)


def account_open(state_root: Path = typer.Option(None, "--state-root")) -> None:
    """Print the customer portal URL for this node's hosted deployment (does not auto-open)."""
    root = state_root or _default_state_root()
    enrollment = load_enrollment(_config_dir(root))
    base = enrollment.control_plane_base_url if enrollment else ""
    _echo({"portal_url": base or "(not enrolled)"})


def register(app: typer.Typer) -> None:
    """Wire the hosted onboarding commands into the main ``aithernet`` Typer app."""
    app.add_typer(hosted_app, name="hosted")
    app.add_typer(enrollment_app, name="enrollment")
    # The top-level `setup` command is the guided wizard in aithernet.cli; this lower-level
    # directories+identity+enroll routine remains available as `hosted setup`.
    hosted_app.command("setup")(setup)
    app.command("enroll")(_top_enroll)
    app.command("account-open")(account_open)


def _top_enroll(
    base_url: str = typer.Option(..., "--base-url"),
    code: str = typer.Option(..., "--code"),
    node_name: str = typer.Option(None, "--node-name"),
    state_root: Path = typer.Option(None, "--state-root"),
) -> None:
    """Enroll the local node with the hosted control plane (alias of ``hosted enroll``)."""
    hosted_enroll(base_url=base_url, code=code, node_name=node_name, state_root=state_root,
                  software_version=__version__)
