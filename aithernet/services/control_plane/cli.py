"""Hosted administration CLI ``aithernet-hosted`` (Stage 14F §39).

Operator commands for the hosted control plane: migrate, bootstrap the first platform admin,
create tenants/invitations, publish policies, create/sign/publish/revoke releases, diagnostics,
and logical backup/restore. Bootstrap stores only a password hash, refuses to print a password
unless it generates a one-time secret, accepts non-interactive secret input, and disables itself
after success unless explicitly re-enabled.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import typer

from services.control_plane.config import HostedConfig
from services.control_plane.service import ControlPlaneService

app = typer.Typer(name="aithernet-hosted", help="Aithernet hosted control-plane administration.",
                  no_args_is_help=True, add_completion=False)
release_app = typer.Typer(name="release", help="Release lifecycle.", no_args_is_help=True)
tenant_app = typer.Typer(name="tenant", help="Tenant administration.", no_args_is_help=True)
invitation_app = typer.Typer(name="invitation", help="Invitations.", no_args_is_help=True)
policy_app = typer.Typer(name="policy", help="Policy documents.", no_args_is_help=True)
admin_app = typer.Typer(name="admin", help="Platform admin.", no_args_is_help=True)
research_app = typer.Typer(name="research", help="Research / owner-archive operations.",
                           no_args_is_help=True)
app.add_typer(release_app, name="release")
app.add_typer(tenant_app, name="tenant")
app.add_typer(invitation_app, name="invitation")
app.add_typer(policy_app, name="policy")
app.add_typer(admin_app, name="admin")
app.add_typer(research_app, name="research")


def _service() -> ControlPlaneService:
    return ControlPlaneService(HostedConfig.from_env())


def _echo(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _admin_principal(svc: ControlPlaneService):
    """Resolve a platform-admin principal by email (operator runs as a known admin account)."""
    from sqlalchemy import select

    from services.control_plane.models import HostedUser

    email = os.environ.get("AITHERNET_HOSTED_ADMIN_EMAIL", "").strip().lower()
    with svc._session() as s:
        user = None
        if email:
            stmt = select(HostedUser).where(HostedUser.email == email)
            user = s.execute(stmt).scalar_one_or_none()
        if user is None:
            user = s.execute(
                select(HostedUser).where(HostedUser.is_platform_admin.is_(True))
            ).scalars().first()
        if user is None:
            raise typer.BadParameter("no platform admin exists; run 'admin bootstrap' first")
        return svc._principal(s, user)


@app.command("migrate")
def migrate() -> None:
    """Apply pending hosted migrations (explicit migration system; never create_all)."""
    from services.control_plane.db import create_hosted_engine
    from services.control_plane.migrations import run_migrations

    config = HostedConfig.from_env()
    report = run_migrations(create_hosted_engine(config))
    _echo({"dialect": report.dialect, "applied_now": report.applied_now,
           "already_applied": report.already_applied})


@research_app.command("status")
def research_status() -> None:
    """Show owner-archive status: package counts, quarantine, archive jobs, Drive connection."""
    svc = _service()
    _echo(svc.research_admin_status(_admin_principal(svc)))


@research_app.command("connect-drive")
def research_connect_drive(
    refresh_token_env: str = typer.Option(
        "AITHERNET_OWNER_DRIVE_REFRESH_TOKEN", "--refresh-token-env",
        help="Env var NAME holding the owner Drive OAuth refresh token (never passed inline)."),
    account_label: str = typer.Option(None, "--account-label"),
    root_folder_id: str = typer.Option(None, "--root-folder-id"),
) -> None:
    """Connect the owner Google Drive archive SERVER-SIDE. The refresh token is read from an env
    var (never a flag/log), stored server-side only, and never returned. Clients never see it."""
    token = os.environ.get(refresh_token_env, "")
    if not token:
        raise typer.BadParameter(f"env var {refresh_token_env} is empty — set the owner Drive "
                                 "refresh token there (it is never printed).")
    svc = _service()
    _echo(svc.connect_owner_drive(_admin_principal(svc), refresh_token=token,
                                  account_label=account_label, root_folder_id=root_folder_id))


@research_app.command("drive-status")
def research_drive_status() -> None:
    """Show the server-side owner Drive connection status (never the token)."""
    svc = _service()
    _echo(svc.owner_drive_status(_admin_principal(svc)))


@research_app.command("process-jobs")
def research_process_jobs(
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Run the owner-archive worker: write queued packages to Drive (queues if unconnected)."""
    svc = _service()
    _echo(svc.process_archive_jobs(_admin_principal(svc), limit=limit))


@app.command("diagnostics")
def diagnostics() -> None:
    """Show sanitized hosted diagnostics (no secrets)."""
    _echo(_service().diagnostics())


@app.command("readiness")
def readiness() -> None:
    _echo(_service().readiness())


@app.command("check")
def check() -> None:
    """Validate the hosted configuration. Exits non-zero (fail-closed) on any problem."""
    config = HostedConfig.from_env()
    problems = sorted(set(config.validate()))
    _echo({"environment": config.environment, "valid": not problems, "problems": problems})
    if problems:
        raise typer.Exit(1)


@app.command("checklist")
def checklist() -> None:
    """Generate the production-readiness checklist (fails closed; lists unresolved items)."""
    from services.control_plane.checklist import generate_checklist

    svc = _service()
    _echo(generate_checklist(svc.config, service=svc))


@app.command("email-test")
def email_test(
    to: str = typer.Option(..., "--to",
                           help="The ONLY destination the test message is sent to."),
    subject: str = typer.Option("Aithernet email readiness test", "--subject"),
) -> None:
    """Send one bounded test email to --to via the configured provider (production SMTP check).

    Prints no secrets. Reports a distinct outcome for connection / authentication / sender /
    recipient / timeout failures, and records an audit entry. Exits non-zero unless delivery was
    attempted successfully through real SMTP (the development sink reports ``ok`` but warns it did
    not deliver)."""
    svc = _service()
    result = svc.email_test(to=to, subject=subject)
    _echo(result)
    if result["provider"] != "smtp":
        typer.secho("WARNING: provider is the development sink — no real email was delivered. "
                    "Set AITHERNET_HOSTED_EMAIL=smtp + a real AITHERNET_HOSTED_SMTP_URL to verify.",
                    fg=typer.colors.YELLOW)
        raise typer.Exit(code=2)
    if not result["ok"]:
        raise typer.Exit(code=1)


@admin_app.command("bootstrap")
def admin_bootstrap(
    email: str = typer.Option(..., "--email"),
    password: str = typer.Option(None, "--password", help="Provide a password directly."),
    password_fd: int = typer.Option(None, "--password-fd",
                                    help="Read the password from this file descriptor."),
    generate_secret: bool = typer.Option(False, "--generate-secret",
                                         help="Generate and print a one-time bootstrap secret."),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Create the first platform admin. Stores only a hash; prints a password ONLY if generated."""
    svc = _service()
    printed = None
    if generate_secret:
        password = "Az9!" + secrets.token_urlsafe(18)
        printed = password
    elif password_fd is not None:
        with os.fdopen(password_fd) as handle:
            password = handle.read().strip()
    elif password is None:
        ref = os.environ.get("AITHERNET_HOSTED_ADMIN_PASSWORD")
        if not ref:
            raise typer.BadParameter("provide --password, --password-fd, --generate-secret, "
                                     "or AITHERNET_HOSTED_ADMIN_PASSWORD")
        password = ref
    try:
        result = svc.bootstrap_admin(email, password, force=force)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"bootstrap failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    out = {"user_id": result["user_id"], "email": result["email"], "bootstrapped": True}
    if printed:
        out["one_time_password"] = printed  # shown ONCE; only the hash is stored
    _echo(out)


@admin_app.command("grant")
def admin_grant(user_email: str = typer.Option(..., "--user-email"),
                role: str = typer.Option(..., "--role")) -> None:
    """Grant a platform role (release_manager / support_operator) to a user."""
    from sqlalchemy import select

    from services.control_plane.models import HostedUser

    svc = _service()
    principal = _admin_principal(svc)
    with svc._session() as s:
        user = s.execute(
            select(HostedUser).where(HostedUser.email == user_email.strip().lower())
        ).scalar_one_or_none()
        if user is None:
            raise typer.BadParameter("user not found")
        uid = user.id
    _echo(svc.grant_platform_role(principal, uid, role))


@tenant_app.command("create")
def tenant_create(name: str = typer.Option(..., "--name"),
                  tenant_id: str = typer.Option(None, "--tenant-id"),
                  kind: str = typer.Option("organization", "--kind")) -> None:
    _echo(_service().create_tenant(name, kind=kind, tenant_id=tenant_id))


@invitation_app.command("create")
def invitation_create(email: str = typer.Option(..., "--email"),
                      tenant_id: str = typer.Option(None, "--tenant-id"),
                      proposed_tenant_name: str = typer.Option(None, "--proposed-tenant-name"),
                      role: str = typer.Option("tenant_admin", "--role")) -> None:
    """Create an invitation; the one-time token is printed ONCE for out-of-band delivery."""
    svc = _service()
    principal = _admin_principal(svc)
    _echo(svc.create_invitation(principal, email=email, role=role, tenant_id=tenant_id,
                                proposed_tenant_name=proposed_tenant_name))


@policy_app.command("publish")
def policy_publish(policy_type: str = typer.Option(..., "--type"),
                   version: str = typer.Option(..., "--version"),
                   title: str = typer.Option(..., "--title"),
                   document_file: Path = typer.Option(..., "--document-file"),
                   required: str = typer.Option("", "--required",
                                                help="Comma-separated required cats.")) -> None:
    svc = _service()
    principal = _admin_principal(svc)
    text = Path(document_file).read_text()
    cats = [c.strip() for c in required.split(",") if c.strip()]
    _echo(svc.publish_policy(principal, policy_type=policy_type, version=version, title=title,
                             document_text=text, required_categories=cats))


@release_app.command("create")
def release_create(version: str = typer.Option(..., "--version"),
                   channel: str = typer.Option(None, "--channel"),
                   artifact: list[str] = typer.Option(None, "--artifact",
                                                      help="path[:name] of an artifact file."),
                   notes: str = typer.Option(None, "--notes")) -> None:
    svc = _service()
    principal = _admin_principal(svc)
    rel = svc.create_release(principal, version=version, channel=channel, notes=notes)
    for spec in artifact or []:
        path_str, _, name = spec.partition(":")
        path = Path(path_str)
        svc.add_release_artifact(principal, release_id=rel["release_id"],
                                 name=name or path.name, data=path.read_bytes())
    _echo(svc.release_manifest(rel["release_id"]) if (artifact or []) else rel)


@release_app.command("sign")
def release_sign(release_id: str = typer.Option(..., "--release-id"),
                 signing_key_file: Path = typer.Option(None, "--signing-key-file",
                                                       help="Base64 Ed25519 seed file."),
                 signing_key_id: str = typer.Option(
                     None, "--signing-key-id",
                     help="Select a configured signing-key id (default: the active key id).")
                 ) -> None:
    """Sign a release. The key comes from --signing-key-file or AITHERNET_RELEASE_SIGNING_KEY; the
    non-secret key id comes from --signing-key-id or the configured active id (unknown/revoked ids
    are rejected)."""
    svc = _service()
    principal = _admin_principal(svc)
    seed = None
    if signing_key_file is not None:
        seed = Path(signing_key_file).read_text().strip()
    _echo(svc.sign_release(principal, release_id=release_id, signing_seed_b64=seed,
                           signing_key_id=signing_key_id))


@release_app.command("publish")
def release_publish(release_id: str = typer.Option(..., "--release-id")) -> None:
    svc = _service()
    _echo(svc.publish_release(_admin_principal(svc), release_id=release_id))


@release_app.command("revoke")
def release_revoke(release_id: str = typer.Option(..., "--release-id")) -> None:
    svc = _service()
    _echo(svc.revoke_release(_admin_principal(svc), release_id=release_id))


@release_app.command("verify")
def release_verify(release_id: str = typer.Option(..., "--release-id"),
                   public_key_file: Path = typer.Option(..., "--public-key-file")) -> None:
    """Offline-style verification of a signed manifest against a public key file."""
    from services.control_plane import release_signing

    svc = _service()
    manifest = svc.release_manifest(release_id)
    pub = Path(public_key_file).read_text().strip()
    ok = release_signing.verify_manifest(pub, manifest["manifest"], manifest["manifest_signature"])
    _echo({"release_id": release_id, "verified": ok})


@app.command("backup")
def backup(output: Path = typer.Option(..., "--output")) -> None:
    """Logical backup of all hosted tables to a JSON file (dialect-independent, no secrets keys)."""
    from services.control_plane import backup as backup_mod

    svc = _service()
    path = backup_mod.backup_to_file(svc.engine, output)
    _echo({"backup": str(path)})


@app.command("restore")
def restore(input: Path = typer.Option(..., "--input")) -> None:
    """Restore hosted tables from a logical JSON backup into a migrated database."""
    from services.control_plane import backup as backup_mod

    svc = _service()
    report = backup_mod.restore_from_file(svc.engine, input)
    _echo(report)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
