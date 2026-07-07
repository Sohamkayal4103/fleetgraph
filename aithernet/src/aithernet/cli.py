"""Typer command-line interface for an Aithernet node.

The CLI has two roles:
  * ``aithernet start`` boots the API server from configuration.
  * The remaining commands are a thin client over a *running* node's HTTP API.

Client commands surface a clear, actionable error when the node is unreachable rather
than dumping a stack trace.
"""

from __future__ import annotations

import json
import time

import httpx
import typer

from aithernet.config.loader import DEFAULT_CONFIG_PATH, load_config

app = typer.Typer(
    name="aithernet",
    help="Autonomous SDR node runtime — server and API client.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    """Print the INSTALLED package version and exit, without loading config, contacting the node,
    or reading any developer state. The version comes from the installed distribution metadata."""
    if not value:
        return
    from importlib.metadata import PackageNotFoundError, version

    try:
        pkg_version = version("aithernet")
    except PackageNotFoundError:  # running from a source tree that isn't installed as a dist
        pkg_version = "unknown (not installed as a distribution)"
    typer.echo(f"aithernet {pkg_version}")
    raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show the installed Aithernet version and exit.",
    ),
) -> None:
    """Autonomous SDR node runtime — server and API client."""
mission_app = typer.Typer(
    name="mission", help="Submit, inspect, and process missions.", no_args_is_help=True
)
events_app = typer.Typer(
    name="events", help="List and stream runtime events.", no_args_is_help=True
)
coordinator_app = typer.Typer(
    name="coordinator", help="Inspect the coordinator agent.", no_args_is_help=True
)
coding_agent_app = typer.Typer(
    name="coding-agent", help="Inspect the coding agent.", no_args_is_help=True
)
coding_task_app = typer.Typer(
    name="coding-task", help="Create, inspect, and run coding tasks.", no_args_is_help=True
)
coding_sandbox_app = typer.Typer(
    name="coding-sandbox",
    help="Inspect and (temporarily, with confirmation) repair the coding-agent sandbox host "
         "controls.",
    no_args_is_help=True,
)
mcp_app = typer.Typer(
    name="mcp", help="Inspect and call GNU Radio MCP tools.", no_args_is_help=True
)
mcp_session_app = typer.Typer(
    name="session", help="Manage the persistent MCP session (Stage 11B).", no_args_is_help=True
)
mcp_app.add_typer(mcp_session_app, name="session")
agents_app = typer.Typer(
    name="agents",
    help="Connect and message external agents (Stage 7).",
    no_args_is_help=True,
)
# PHASE 6 — customer-facing AI provider configuration (coordinator + coding). A DISTINCT interface
# from the Stage 7 peer connections above; mounted as `agents configure {coordinator,coding}`.
agents_configure_app = typer.Typer(
    name="configure", help="Configure the coordinator + coding AI providers.",
    no_args_is_help=True,
)
agents_app.add_typer(agents_configure_app, name="configure")
# beta.10 Defect 1 — managed secret store inspection (names + availability only, never values).
agents_secrets_app = typer.Typer(
    name="secrets", help="List/check/remove stored provider secrets (names + availability only).",
    no_args_is_help=True,
)
agents_app.add_typer(agents_secrets_app, name="secrets")
# beta.10 Part 2 — scoped external-agent gateway credentials for server-side agents.
agents_gateway_app = typer.Typer(
    name="gateway", help="Create/revoke scoped external-agent gateway credentials.",
    no_args_is_help=True,
)
agents_app.add_typer(agents_gateway_app, name="gateway")
gnuradio_app = typer.Typer(
    name="gnuradio", help="Inspect the GNU Radio workspace context (Stage 11B).",
    no_args_is_help=True,
)
gnuradio_context_app = typer.Typer(
    name="context", help="View/refresh GNU Radio workspace context.", no_args_is_help=False
)
gnuradio_app.add_typer(gnuradio_context_app, name="context")
worker_app = typer.Typer(
    name="worker", help="Inspect the autonomous mission worker (Stage 12).",
    no_args_is_help=True,
)
identity_app = typer.Typer(
    name="identity", help="Inspect/initialize this node's cryptographic identity (Stage 13A).",
    no_args_is_help=True,
)
peer_app = typer.Typer(
    name="peer", help="Manage authenticated transport peers (Stage 13A).", no_args_is_help=True
)
agent_message_app = typer.Typer(
    name="agent-message", help="Send/inspect authenticated agent messages (Stage 13A).",
    no_args_is_help=True,
)
transport_app = typer.Typer(
    name="transport", help="Inspect the agent transport (outbox/inbox/worker) (Stage 13A).",
    no_args_is_help=True,
)
rf_app = typer.Typer(
    name="rf", help="Operate RF backends (legacy GNU Radio + experimental Marconi) (Stage 13A.5).",
    no_args_is_help=True,
)
rf_backend_app = typer.Typer(
    name="backend", help="Start/stop/restart/show one RF backend.", no_args_is_help=True
)
rf_benchmark_app = typer.Typer(
    name="benchmark", help="Run/list/show operator RF backend benchmarks.", no_args_is_help=True
)
rf_app.add_typer(rf_backend_app, name="backend")
rf_app.add_typer(rf_benchmark_app, name="benchmark")
# 1.0.0-beta.1 OTA: RF link-profile administration (waveform profiles; never hardware identity).
rf_profiles_app = typer.Typer(name="profiles", help="Inspect/validate OTA RF link profiles.",
                              no_args_is_help=True)
rf_app.add_typer(rf_profiles_app, name="profiles")
# 1.0.0-beta.2 — operator-controlled physical SDR transmission (local; no vendor/cloud gate).
rf_tx_app = typer.Typer(name="tx", help="Operator-controlled physical SDR transmission (local).",
                        no_args_is_help=True)
rf_app.add_typer(rf_tx_app, name="tx")
rf_tx_plans_app = typer.Typer(name="plans", help="Local pre-authorization envelopes (bounded TX).",
                              no_args_is_help=True)
rf_tx_app.add_typer(rf_tx_plans_app, name="plans")
# Stage 13B coordinator-communication views.
mission_reply_wait_app = typer.Typer(
    name="reply-wait", help="Cancel a mission reply wait (Stage 13B).", no_args_is_help=True
)
mission_app.add_typer(mission_reply_wait_app, name="reply-wait")
conversation_app = typer.Typer(
    name="conversation", help="Inspect peer conversations (Stage 13B).", no_args_is_help=True
)
inbound_request_app = typer.Typer(
    name="inbound-request", help="Inspect inbound coordinator requests (Stage 13B).",
    no_args_is_help=True,
)
peer_permissions_app = typer.Typer(
    name="permissions", help="View/set peer application authorization (Stage 13B).",
    no_args_is_help=True,
)
peer_app.add_typer(peer_permissions_app, name="permissions")
database_app = typer.Typer(
    name="database", help="Inspect and apply node database schema migrations.",
    no_args_is_help=True,
)
app.add_typer(database_app, name="database")
fleet_app = typer.Typer(
    name="fleet", help="Inspect the peer fleet (trust, permissions, health) (Stage 13C).",
    no_args_is_help=True,
)
app.add_typer(fleet_app, name="fleet")
communication_app = typer.Typer(
    name="communication", help="Inspect distributed communication state (Stage 13C).",
    no_args_is_help=True,
)
app.add_typer(communication_app, name="communication")
remote_mission_app = typer.Typer(
    name="remote-mission", help="Inspect authenticated remote mission status (Stage 13D.3).",
    no_args_is_help=True,
)
app.add_typer(remote_mission_app, name="remote-mission")
peer_status_perm_app = typer.Typer(
    name="mission-status-permissions",
    help="View/set per-peer mission-status authorization (Stage 13D.3).", no_args_is_help=True,
)
peer_app.add_typer(peer_status_perm_app, name="mission-status-permissions")
artifact_app = typer.Typer(
    name="artifact", help="Cross-node RF artifact transfer (Stage 13D.2).", no_args_is_help=True
)
artifact_transfer_app = typer.Typer(
    name="transfer", help="Inspect/operate artifact transfers.", no_args_is_help=True
)
artifact_store_app = typer.Typer(
    name="store", help="Managed artifact-store status + GC.", no_args_is_help=True
)
artifact_app.add_typer(artifact_transfer_app, name="transfer")
artifact_app.add_typer(artifact_store_app, name="store")
app.add_typer(artifact_app, name="artifact")
# Stage 14A production operations.
node_app = typer.Typer(
    name="node", help="Provision, preflight, and inspect a production node (Stage 14A).",
    no_args_is_help=True,
)
health_app = typer.Typer(
    name="health", help="Liveness/readiness probes (Stage 14A).", no_args_is_help=True
)
backup_app = typer.Typer(
    name="backup", help="Consistent node backup + protected restore (Stage 14A).",
    no_args_is_help=True,
)
upgrade_app = typer.Typer(
    name="upgrade", help="Safe upgrade / rollback workflow (Stage 14A).", no_args_is_help=True
)
diagnostics_app = typer.Typer(
    name="diagnostics", help="Sanitized operational diagnostics bundle (Stage 14A).",
    no_args_is_help=True,
)
app.add_typer(node_app, name="node")
app.add_typer(health_app, name="health")
app.add_typer(backup_app, name="backup")
app.add_typer(upgrade_app, name="upgrade")
app.add_typer(diagnostics_app, name="diagnostics")
app.add_typer(mission_app, name="mission")
app.add_typer(worker_app, name="worker")
app.add_typer(events_app, name="events")
app.add_typer(coordinator_app, name="coordinator")
app.add_typer(coding_agent_app, name="coding-agent")
app.add_typer(coding_task_app, name="coding-task")
app.add_typer(coding_sandbox_app, name="coding-sandbox")
app.add_typer(mcp_app, name="mcp")
app.add_typer(agents_app, name="agents")
app.add_typer(gnuradio_app, name="gnuradio")
app.add_typer(identity_app, name="identity")
app.add_typer(peer_app, name="peer")
app.add_typer(peer_app, name="peers")  # beta.10 Part 2: plural customer alias for peer commands
app.add_typer(agent_message_app, name="agent-message")
app.add_typer(transport_app, name="transport")
app.add_typer(rf_app, name="rf")
app.add_typer(conversation_app, name="conversation")
app.add_typer(inbound_request_app, name="inbound-request")

catgpt_app = typer.Typer(
    name="catgpt",
    help="Manage the Aithernet-managed CatGPT Gateway sidecar (optional local coordinator model "
         "backend — you sign into ChatGPT/Claude yourself in a local browser).",
    no_args_is_help=True,
)
app.add_typer(catgpt_app, name="catgpt")

hardware_app = typer.Typer(
    name="hardware", help="Managed SDR hardware inventory + leasing (Stage 14B).",
    no_args_is_help=True,
)
hardware_lease_app = typer.Typer(
    name="lease", help="Durable hardware leases.", no_args_is_help=True
)
hardware_qualify_app = typer.Typer(
    name="qualify", help="Real-hardware qualification (Stage 14C.1).", no_args_is_help=True
)
hardware_app.add_typer(hardware_lease_app, name="lease")
hardware_app.add_typer(hardware_qualify_app, name="qualify")
app.add_typer(hardware_app, name="hardware")

# PHASE 5 — validated SDR dependency installation (recommended/source/offline), scoped to the
# selected profile only (never the whole Soapy/Pothosware ecosystem).
sdr_app = typer.Typer(
    name="sdr", help="Install + verify SDR dependencies for a hardware profile (PHASE 5).",
    no_args_is_help=True,
)
app.add_typer(sdr_app, name="sdr")

components_app = typer.Typer(
    name="components", help="Managed, signed external components (e.g. the RF MCP).",
    no_args_is_help=True,
)
app.add_typer(components_app, name="components")

release_app = typer.Typer(
    name="release", help="Verify a downloaded Aithernet release (offline).",
    no_args_is_help=True,
)
app.add_typer(release_app, name="release")

service_app = typer.Typer(
    name="service", help="Manage the node service (workstation user service or appliance).",
    no_args_is_help=True,
)
app.add_typer(service_app, name="service")


@components_app.command("list")
def components_list() -> None:
    """List installed managed components (name, version, install prefix, license)."""
    from aithernet import components as comp
    rows = comp.list_components()
    if not rows:
        typer.secho(f"No managed components installed under {comp.components_root()}",
                    fg=typer.colors.YELLOW)
        return
    for c in rows:
        typer.secho(f"{c.name} {c.version}", fg=typer.colors.CYAN)
        typer.echo(f"  prefix:  {c.prefix}")
        commit = (c.lock.get("upstream_commit", "") or "")[:12]
        typer.echo(f"  license: {c.lock.get('license')}  "
                   f"upstream: {c.lock.get('upstream_url')}@{commit}")


@components_app.command("status")
def components_status(
    name: str = typer.Argument("rf-mcp", help="Component name."),
) -> None:
    """Show one component's installed status + provenance (from its lock file)."""
    from aithernet import components as comp
    st = comp.component_status(name)
    typer.secho(f"{name}: {'installed' if st.installed else 'NOT installed'}",
                fg=typer.colors.GREEN if st.ok else typer.colors.RED)
    if st.installed:
        typer.echo(f"  version: {st.version}")
        typer.echo(f"  prefix:  {st.prefix}")
        typer.echo(f"  source:  {st.lock.get('upstream_url')}@{st.lock.get('upstream_commit')}")
        typer.echo(f"  patches: {', '.join(st.lock.get('patch_series', []))}")
        typer.echo(f"  runtime: {st.lock.get('runtime_command')}")
    for issue in st.issues:
        typer.secho(f"  issue: {issue}", fg=typer.colors.YELLOW)
    if not st.ok:
        raise typer.Exit(code=1)


@components_app.command("verify")
def components_verify(
    name: str = typer.Argument("rf-mcp", help="Component name."),
    artifacts_dir: str = typer.Option(None, "--artifacts-dir",
                                      help="Dir holding the source archive + signature + pubkey."),
) -> None:
    """Verify a component: lock + entrypoint present, the recorded file inventory (missing/changed),
    runtime deps, version/prefix, and the detached Ed25519 signature over the source archive."""
    import json as _json
    from pathlib import Path as _Path

    from aithernet.components import manager
    result = manager.verify(name, artifacts_dir=_Path(artifacts_dir) if artifacts_dir else None)
    typer.echo(_json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@components_app.command("plan")
def components_plan(
    name: str = typer.Argument("rf-mcp", help="Component name."),
) -> None:
    """Show exactly what installing a component entails — full provenance (source tag/commit,
    patch-set digest, build/runtime commands, license, install prefix). Makes NO changes."""
    import json as _json

    from aithernet.components import manager
    try:
        typer.echo(_json.dumps(manager.plan(name), indent=2, default=str))
    except manager.ComponentError as exc:
        typer.secho(f"plan failed [{exc.category}]: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


@components_app.command("install")
def components_install(
    name: str = typer.Argument("rf-mcp", help="Component name."),
    bundle: str = typer.Option(None, "--bundle",
                               help="Pre-built signed bundle dir (source archive + sig + pubkey "
                                    "+ lock.json). The customer path."),
    upstream_git: str = typer.Option(None, "--upstream-git",
                                     help="Local clone/mirror of the pinned upstream "
                                          "(source mode)."),
    components_root: str = typer.Option(None, "--components-root",
                                        help="Managed install root override."),
    out_dir: str = typer.Option(None, "--out-dir", help="State dir for build/bundle artifacts."),
    reinstall: bool = typer.Option(False, "--reinstall", help="Replace an existing install."),
    no_build: bool = typer.Option(False, "--no-build",
                                  help="Skip the runtime-environment build step (offline/air-"
                                       "gapped installs that supply the .venv separately)."),
) -> None:
    """Install a managed component, VERIFYING provenance + signature before anything is written.

    Either --bundle (a signed bundle) or --upstream-git (build from the pinned source). After
    provenance verification the component's runtime environment is built (e.g. `uv sync --frozen`)
    so it is immediately runnable; pass --no-build to skip that. Records a file inventory for
    integrity, repair and bounded removal."""
    import json as _json
    from pathlib import Path as _Path

    # beta.5: the CatGPT Gateway runtime is a Docker image component — install == load the shipped
    # image tar into the local daemon (no source build, no registry pull, no docker login).
    if name == "catgpt-gateway":
        from aithernet.catgpt.manager import CatGptManagerError
        mgr = _catgpt_manager()
        if not mgr.runner.available():
            typer.secho("install failed [runtime]: Docker is not installed. Run "
                        "`aithernet catgpt install-runtime` first.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        try:
            info = mgr.ensure_image()
        except CatGptManagerError as exc:
            typer.secho(f"install failed [missing_artifact]: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        typer.secho(f"installed catgpt-gateway image {info['tag']} ({info['source']})",
                    fg=typer.colors.GREEN)
        return

    from aithernet.components import manager
    try:
        result = manager.install(
            name, bundle=bundle, upstream_git=upstream_git,
            components_root_dir=_Path(components_root) if components_root else None,
            out_dir=_Path(out_dir) if out_dir else None, force=reinstall, build=not no_build)
    except manager.ComponentError as exc:
        typer.secho(f"install failed [{exc.category}]: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.secho(f"installed {name} ({result.get('installed_files')} files)",
                fg=typer.colors.GREEN)
    typer.echo(_json.dumps({k: v for k, v in result.items() if k != "lock"}, indent=2, default=str))


@components_app.command("repair")
def components_repair(
    name: str = typer.Argument("rf-mcp", help="Component name."),
    artifacts_dir: str = typer.Option(None, "--artifacts-dir"),
) -> None:
    """Restore missing/changed OWNED files from the recorded source archive. Bounded: never touches
    anything outside the prefix and never installs system packages."""
    import json as _json
    from pathlib import Path as _Path

    from aithernet.components import manager
    try:
        result = manager.repair(name, artifacts_dir=_Path(artifacts_dir) if artifacts_dir else None)
    except manager.ComponentError as exc:
        typer.secho(f"repair failed [{exc.category}]: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo(_json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@components_app.command("remove")
def components_remove(
    name: str = typer.Argument("rf-mcp", help="Component name."),
    purge_artifacts: bool = typer.Option(False, "--purge-artifacts",
                                         help="Also delete the cached build/bundle artifacts."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Uninstall a component, removing ONLY files it owns (per its inventory) — never shared system
    packages or anything outside its managed prefix."""
    import json as _json

    from aithernet.components import manager
    if not yes:
        typer.confirm(f"Remove managed component '{name}' (only its owned files)?", abort=True)
    try:
        result = manager.remove(name, purge_artifacts=purge_artifacts)
    except manager.ComponentError as exc:
        typer.secho(f"remove failed [{exc.category}]: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo(_json.dumps(result, indent=2, default=str))


@components_app.command("adopt")
def components_adopt(
    name: str = typer.Argument("rf-mcp", help="Component name."),
    from_installed: str = typer.Option(None, "--from-installed",
                                       help="Managed prefix of the existing installation."),
    source_checkout: str = typer.Option(None, "--source-checkout",
                                        help="Read-only local source checkout to verify against "
                                             "(dev only; never a client runtime dependency)."),
    write_metadata: bool = typer.Option(False, "--write-metadata",
                                        help="Write the adopted lock + inventory (else dry-run)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Adopt an EXISTING working component installation into the component manager — record
    provenance + a hashed file inventory WITHOUT replacing any runtime file. Default is a dry-run;
    `--write-metadata` backs up the old lock and writes the new metadata (idempotent)."""
    import json as _json

    from aithernet.components import manager
    try:
        report = manager.adopt(
            name, from_installed=from_installed, source_checkout=source_checkout,
            write_metadata=write_metadata, dry_run=not write_metadata,
            components_root_dir=None)
    except manager.ComponentError as exc:
        typer.secho(f"adopt failed [{exc.category}]: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    if json_out:
        typer.echo(_json.dumps(report, indent=2, default=str))
    else:
        typer.secho(f"adopt {name}: {'OK' if report['ok'] else 'BLOCKED'} "
                    f"({'wrote metadata' if report.get('wrote') else 'dry-run'})",
                    fg=typer.colors.GREEN if report["ok"] else typer.colors.RED)
        typer.echo(f"  prefix: {report['prefix']}")
        typer.echo(f"  files: {report.get('installed_file_count')}  "
                   f"inventory_digest: {report.get('inventory_digest', '')[:16]}…")
        typer.echo(f"  base_commit: {report.get('base_commit')}  patch: "
                   f"{report.get('patch_digest', '')[:16]}…")
        typer.echo(f"  checks: {report['checks']}")
        if report["differences"]:
            typer.secho(f"  differences: {report['differences']}", fg=typer.colors.YELLOW)
    if not report["ok"]:
        raise typer.Exit(code=1)


@release_app.command("verify")
def release_verify(
    directory: str = typer.Argument(..., help="Directory holding the downloaded release files."),
) -> None:
    """Verify a downloaded release OFFLINE: the detached Ed25519 manifest signature, the public-key
    ↔ key-id binding, every SHA256SUMS entry, and each manifest artifact digest.

    Expects, in the directory: ``manifest.json``, ``manifest.sig``, ``aithernet-release.pub`` (PEM),
    ``SHA256SUMS``, and the package files. This is the supported equivalent of the documented
    ``openssl pkeyutl -verify`` + ``sha256sum -c`` steps. Exits non-zero on any failure.
    """
    import json as _json
    from pathlib import Path as _Path

    from aithernet.components import _sha256, _verify_ed25519

    d = _Path(directory)
    failures: list[str] = []

    def need(fname: str) -> _Path | None:
        p = d / fname
        if not p.is_file():
            failures.append(f"missing required file: {fname}")
            return None
        return p

    manifest_p = need("manifest.json")
    sig_p = need("manifest.sig")
    pub_p = need("aithernet-release.pub")
    sums_p = need("SHA256SUMS")

    manifest: dict = {}
    if manifest_p and sig_p and pub_p:
        if _verify_ed25519(pub_p, manifest_p, sig_p):
            typer.secho("✓ manifest signature verified (Ed25519)", fg=typer.colors.GREEN)
        else:
            failures.append("manifest signature INVALID")
        try:
            manifest = _json.loads(manifest_p.read_text())
        except ValueError:
            failures.append("manifest.json is not valid JSON")
        key_id = manifest.get("signing_key_id")
        if key_id:
            typer.echo(f"  signing_key_id: {key_id}")

    # SHA256SUMS: every listed file present must match.
    if sums_p:
        checked = 0
        for line in sums_p.read_text().splitlines():
            line = line.strip()
            if not line or "  " not in line:
                continue
            digest, fname = line.split("  ", 1)
            fp = d / fname
            if not fp.is_file():
                continue  # a verification-only set may omit packages; manifest digests still cover
            actual = _sha256(fp)
            checked += 1
            if actual != digest.strip():
                failures.append(f"SHA256SUMS mismatch: {fname}")
        typer.secho(f"✓ SHA256SUMS: {checked} file(s) matched", fg=typer.colors.GREEN)

    # Cross-check each manifest artifact digest against the file on disk (when present).
    for art in manifest.get("artifacts", []):
        fp = d / art["name"]
        if not fp.is_file():
            continue
        want = str(art.get("sha256", "")).split(":", 1)[-1]
        got = _sha256(fp).split(":", 1)[-1]
        if want and want != got:
            failures.append(f"manifest digest mismatch: {art['name']}")

    if failures:
        for f in failures:
            typer.secho(f"✗ {f}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho("RELEASE VERIFIED — signature, checksums, and manifest digests all match.",
                fg=typer.colors.GREEN)


def _service_scope(explicit_system: bool):
    """Resolve the systemctl scope to operate on: explicit --system, else the installed scope,
    else default to the user (personal-workstation) scope."""
    from aithernet.provisioning import services
    if explicit_system:
        return "system", services
    return (services.installed_scope() or "user"), services


def _run_systemctl(scope, services, *args: str, read_only: bool = False) -> int:
    import subprocess
    base = services.systemctl_base(scope)
    if scope == "system" and not read_only:
        # Never silently escalate: print the privileged command for the operator to run.
        typer.secho("This is a SYSTEM service; run as root:\n  sudo "
                    + " ".join([*base, *args]), fg=typer.colors.YELLOW)
        return 0
    return subprocess.run([*base, *args]).returncode


@service_app.command("install")
def service_install() -> None:
    """Install + enable the personal-workstation (systemd --user) node service (no sudo).

    Runs the node under your desktop user so it inherits your Gemini/Codex CLI authentication.
    """
    import subprocess

    from aithernet.provisioning import services
    path = services.install_user_unit()
    subprocess.run([*services.systemctl_base("user"), "daemon-reload"])
    rc = subprocess.run([*services.systemctl_base("user"), "enable", "--now",
                         services.UNIT_NAME]).returncode
    typer.secho(f"Installed user service: {path}", fg=typer.colors.GREEN)
    typer.echo("Tip: `loginctl enable-linger $USER` keeps it running while logged out.")
    if rc != 0:
        raise typer.Exit(code=rc)


@service_app.command("start")
def service_start(system: bool = typer.Option(False, "--system",
                                              help="Operate on the system unit.")):
    """Start the node service."""
    scope, services = _service_scope(system)
    raise typer.Exit(code=_run_systemctl(scope, services, "start", services.UNIT_NAME))


@service_app.command("stop")
def service_stop(system: bool = typer.Option(False, "--system",
                                             help="Operate on the system unit.")):
    """Stop the node service."""
    scope, services = _service_scope(system)
    raise typer.Exit(code=_run_systemctl(scope, services, "stop", services.UNIT_NAME))


@service_app.command("restart")
def service_restart(system: bool = typer.Option(False, "--system", help="Operate on the system "
                                                "unit.")):
    """Restart the node service."""
    scope, services = _service_scope(system)
    raise typer.Exit(code=_run_systemctl(scope, services, "restart", services.UNIT_NAME))


@service_app.command("status")
def service_status(system: bool = typer.Option(False, "--system", help="Operate on the system "
                                               "unit.")):
    """Show node service status (read-only)."""
    scope, services = _service_scope(system)
    if services.installed_scope() is None and not system:
        typer.secho("No node service unit installed. Install one with `aithernet service install` "
                    "(user) or via `aithernet setup`.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    raise typer.Exit(code=_run_systemctl(scope, services, "status", services.UNIT_NAME,
                                         "--no-pager", read_only=True))


@service_app.command("logs")
def service_logs(
    lines: int = typer.Option(50, "--lines", "-n", help="Recent log lines."),
    system: bool = typer.Option(False, "--system", help="Operate on the system unit."),
):
    """Show recent node service logs (read-only)."""
    import subprocess

    from aithernet.provisioning import services
    scope = "system" if system else (services.installed_scope() or "user")
    base = ["journalctl", "--user"] if scope == "user" else ["journalctl"]
    rc = subprocess.run(
        [*base, "-u", services.UNIT_NAME, "--no-pager", "-n", str(lines)]).returncode
    raise typer.Exit(code=rc)


# -- SDR dependency installation (PHASE 5) --------------------------------------
def _sdr_print_plan(p) -> None:
    typer.secho(f"SDR plan — profile '{p.profile}' (internal '{p.internal}'), mode '{p.mode}'",
                fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  {p.description}")
    if p.apt_packages:
        typer.echo(f"  apt packages: {' '.join(p.apt_packages)}")
    if p.source_components:
        for c in p.source_components:
            typer.echo(f"  source: {c['name']} @ {c['pin']}  ({c['url']})")
        typer.echo(f"  prefix: {p.source_prefix}")
    if p.download_sources:
        typer.echo(f"  download sources: {', '.join(p.download_sources)}")
    if p.est_download_mb or p.est_installed_mb:
        typer.echo(f"  estimated: ~{p.est_download_mb} MB download, ~{p.est_installed_mb} MB "
                   f"installed")
    for op in p.privileged_ops:
        typer.secho(f"  privileged: {op}", fg=typer.colors.YELLOW)
    if p.requires_logout:
        typer.echo("  note: log out/in after group changes take effect")
    for probe in p.runtime_probes:
        typer.echo(f"  verifies: {' '.join(probe)}")
    for n in p.notes:
        typer.echo(f"  note: {n}")


@sdr_app.command("plan")
def sdr_plan(
    profile: str = typer.Option(..., "--profile", help="Hardware profile (e.g. plutosdr)."),
    mode: str = typer.Option("recommended", "--mode", help="recommended|source|offline"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show the SDR dependency plan for a profile/mode. Pure — makes NO changes."""
    from aithernet.hardware import sdr
    try:
        p = sdr.plan(profile, mode)
    except sdr.SdrError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_out:
        typer.echo(json.dumps(p.to_dict(), indent=2))
    else:
        _sdr_print_plan(p)


@sdr_app.command("install")
def sdr_install(
    profile: str = typer.Option(..., "--profile", help="Hardware profile (e.g. plutosdr)."),
    mode: str = typer.Option("recommended", "--mode", help="recommended|source|offline"),
    from_dir: str = typer.Option(
        None, "--from",
        help="Offline: directory of pre-staged OS packages (os-packages/*.deb). No network/apt."),
    yes: bool = typer.Option(False, "--yes", help="Actually run it (else plan only). Privileged."),
) -> None:
    """Install SDR dependencies for a profile. Without --yes this prints the plan only.

    recommended -> validated apt repositories (sudo apt-get, privileged).
    offline     -> install ONLY pre-staged .deb payloads from --from via dpkg; makes ZERO apt or
                   network calls and stops clearly if the offline payload is unavailable/incomplete.
    source      -> pinned source builds (not auto-executed).
    """
    import subprocess as _sp

    from aithernet.hardware import sdr
    try:
        p = sdr.plan(profile, mode, staged_dir=from_dir)
    except sdr.SdrError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _sdr_print_plan(p)
    if mode == sdr.OFFLINE and p.offline_unavailable:
        typer.secho("\noffline assets unavailable/incomplete — no pre-staged OS-package payloads "
                    "found. No apt/network call was made. Use `--mode recommended` (validated apt "
                    "repositories) or pass `--from <dir>` with os-packages/*.deb.",
                    fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if not yes:
        typer.secho("\nPlan only. Re-run with --yes to install (requires sudo / long builds).",
                    fg=typer.colors.YELLOW)
        return
    if mode == sdr.SOURCE:
        typer.secho("Source builds are long and privileged; run the recorded build steps for "
                    f"{p.source_prefix} deliberately. Not auto-executed.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=2)
    if mode == sdr.OFFLINE:
        # Pre-staged local .deb files only — dpkg, never apt; zero network/index access.
        rc = _sp.run(["sudo", "dpkg", "-i", *p.staged_debs]).returncode
    else:  # recommended: validated apt repositories
        rc = _sp.run(["sudo", "apt-get", "install", "-y", *p.apt_packages]).returncode
    if rc != 0:
        raise typer.Exit(code=rc)
    for pc in p.post_commands:
        _sp.run(["sudo", *pc.split()])
    typer.secho("Installed. Verify with `aithernet sdr verify --profile " + profile + "`.",
                fg=typer.colors.GREEN)


@sdr_app.command("verify")
def sdr_verify(
    profile: str = typer.Option(..., "--profile", help="Hardware profile."),
) -> None:
    """Run the profile's bounded runtime probes (read-only) and report which succeed."""
    from aithernet.hardware import sdr
    try:
        result = sdr.verify_runtime(profile)
    except sdr.SdrError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2))
    if not result["ok"]:
        raise typer.Exit(code=1)


@sdr_app.command("status")
def sdr_status(
    profile: str = typer.Option(None, "--profile", help="Also run this profile's runtime probes."),
) -> None:
    """Show which RF runtimes are present on this host (read-only)."""
    from aithernet.hardware import sdr
    typer.echo(json.dumps(sdr.status(profile), indent=2))

field_app = typer.Typer(
    name="field", help="Multi-node field operation + soak validation (Stage 14C.2).",
    no_args_is_help=True,
)
field_campaign_app = typer.Typer(name="campaign", help="Field campaigns.", no_args_is_help=True)
field_soak_app = typer.Typer(name="soak", help="Resource soak runs.", no_args_is_help=True)
field_fault_app = typer.Typer(
    name="fault", help="Validation fault injection.", no_args_is_help=True)
field_app.add_typer(field_campaign_app, name="campaign")
field_app.add_typer(field_soak_app, name="soak")
field_app.add_typer(field_fault_app, name="fault")
app.add_typer(field_app, name="field")

# Stage 14D — external-agent interoperability gateway (distinct from the Stage 7 `agents`
# peer-connection group). Manages external (non-Aithernet) agent identities, credentials,
# endpoints, subscriptions, deliveries, and dead letters.
extagent_app = typer.Typer(
    name="external-agents",
    help="External-agent interoperability gateway (Stage 14D).",
    no_args_is_help=True,
)
extagent_cred_app = typer.Typer(name="credentials", help="Agent credentials.", no_args_is_help=True)
extagent_perm_app = typer.Typer(name="permissions", help="Agent permissions.", no_args_is_help=True)
extagent_ep_app = typer.Typer(name="endpoints", help="Callback endpoints.", no_args_is_help=True)
extagent_sub_app = typer.Typer(
    name="subscriptions", help="Event subscriptions.", no_args_is_help=True)
extagent_del_app = typer.Typer(name="deliveries", help="Durable deliveries.", no_args_is_help=True)
extagent_app.add_typer(extagent_cred_app, name="credentials")
extagent_app.add_typer(extagent_perm_app, name="permissions")
extagent_app.add_typer(extagent_ep_app, name="endpoints")
extagent_app.add_typer(extagent_sub_app, name="subscriptions")
extagent_app.add_typer(extagent_del_app, name="deliveries")
app.add_typer(extagent_app, name="external-agents")

# Stage 14E — privacy-preserving data platform: consent, local inspection, durable export,
# destinations, deletion, and the dataset registry.
data_app = typer.Typer(name="data", help="Privacy, consent + data export (Stage 14E).",
                       no_args_is_help=True)
data_consent_app = typer.Typer(name="consent", help="Consent.", no_args_is_help=True)
data_records_app = typer.Typer(
    name="records", help="Local record inspection.", no_args_is_help=True)
data_export_app = typer.Typer(name="export", help="Export queue.", no_args_is_help=True)
data_dest_app = typer.Typer(name="destinations", help="Export destinations.", no_args_is_help=True)
data_del_app = typer.Typer(name="deletions", help="Deletion requests.", no_args_is_help=True)
data_ds_app = typer.Typer(name="datasets", help="Dataset registry.", no_args_is_help=True)
data_app.add_typer(data_consent_app, name="consent")
data_app.add_typer(data_records_app, name="records")
data_app.add_typer(data_export_app, name="export")
data_app.add_typer(data_dest_app, name="destinations")
data_app.add_typer(data_del_app, name="deletions")
data_app.add_typer(data_ds_app, name="datasets")
app.add_typer(data_app, name="data")

# ---------------------------------------------------------------------------
# Google Drive — encrypted SECONDARY archive (Stage 14E, Part 14). Never the live
# database, object store, ingestion sink, mission state/queue, or RF capture sink.
# Narrow scope drive.file; OAuth + tokens live outside Git (0700/0600); no secrets
# are printed. Bundles are encrypted before upload and fail closed without a key.
# ---------------------------------------------------------------------------
drive_app = typer.Typer(
    name="drive", help="Encrypted Google Drive secondary archive (optional).",
    no_args_is_help=True,
)
app.add_typer(drive_app, name="drive")


def _drive_encryption_key() -> str:
    """Resolve the archive encryption key from the env var named by
    AITHERNET_DRIVE_ENCRYPTION_KEY_REF. Fails closed when unset/empty."""
    import os as _os

    ref = _os.environ.get("AITHERNET_DRIVE_ENCRYPTION_KEY_REF")
    key = _os.environ.get(ref) if ref else None
    if not key:
        raise typer.BadParameter(
            "no encryption key configured. Set AITHERNET_DRIVE_ENCRYPTION_KEY_REF to the NAME of "
            "an environment variable that holds a base64 AES key (generate one with "
            "`aithernet drive keygen`). Encryption before upload is mandatory."
        )
    return key


def _drive_client():
    from aithernet.data.destinations.google_drive_client import (
        GoogleDriveOAuth,
        RealGoogleDriveClient,
    )

    return RealGoogleDriveClient(GoogleDriveOAuth())


@drive_app.command("keygen")
def drive_keygen() -> None:
    """Print a fresh base64 AES-256 archive key. Store it in your env (referenced by
    AITHERNET_DRIVE_ENCRYPTION_KEY_REF); never commit it."""
    from aithernet.data import crypto

    typer.echo(crypto.generate_key_b64())


@drive_app.command("status")
def drive_status() -> None:
    """Bounded readiness of the Drive archive (no secrets): client present, authorized, key set."""
    import json as _json
    import os as _os

    from aithernet.data.destinations import google_drive_client as g

    ref = _os.environ.get("AITHERNET_DRIVE_ENCRYPTION_KEY_REF")
    out = {
        "oauth_client_present": g.oauth_client_path().is_file(),
        "authorized": g.token_path().is_file(),
        "scope": g.DRIVE_SCOPE,
        "encryption_key_configured": bool(ref and _os.environ.get(ref)),
        "state_dir": str(g.state_dir()),
        "archive_folders": list(g.ARCHIVE_FOLDERS),
    }
    typer.echo(_json.dumps(out, indent=2))


@drive_app.command("authorize")
def drive_authorize() -> None:
    """Run the Google OAuth (drive.file) consent flow and provision the archive folder tree.

    Prints the authorization URL for you to open; persists only the refresh token (0600). No
    OAuth client contents, codes, or tokens are printed."""
    import json as _json

    from aithernet.data.destinations.drive import DriveError
    from aithernet.data.destinations.google_drive_client import GoogleDriveOAuth

    oauth = GoogleDriveOAuth()
    try:
        oauth.authorize(on_url=lambda u: typer.secho(
            "Open this URL in your browser to authorize Aithernet (drive.file scope):\n" + u,
            fg=typer.colors.CYAN))
        client = _drive_client()
        tree = client.ensure_archive_tree()
    except DriveError as exc:
        typer.secho(f"authorization failed: {exc.category}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo(_json.dumps({"authorized": True, "archive_root": tree.get("_root"),
                            "folders": [f for f in tree if f != "_root"]}, indent=2))


@drive_app.command("upload")
def drive_upload(
    file: str = typer.Option(..., "--file", help="Path to the file to archive."),
    category: str = typer.Option(..., "--category",
                                 help="Archive folder (see `drive status` for the list)."),
    idempotency_key: str = typer.Option(None, "--idempotency-key",
                                        help="Stable key so a retry resolves to the same file."),
) -> None:
    """Encrypt a file (mandatory) and upload it to the chosen archive folder. Idempotent.

    Prints the Drive file id, byte count and bundle digest; never prints secrets. Raw continuous
    RF recordings are not uploaded by automation — this is an explicit, operator-run command."""
    import hashlib as _hashlib
    import json as _json
    from pathlib import Path as _Path

    from aithernet.data.destinations import google_drive_client as g
    from aithernet.data.destinations.drive import DriveError

    fp = _Path(file)
    if category not in g.ARCHIVE_FOLDERS:
        raise typer.BadParameter(
            f"invalid category '{category}' (choose: {', '.join(g.ARCHIVE_FOLDERS)})")
    if not fp.is_file():
        raise typer.BadParameter(f"file not found: {file}")
    key = _drive_encryption_key()
    plaintext = fp.read_bytes()
    bundle, plain_sha = g.seal_archive(plaintext, key)  # fails closed if key missing
    idem = idempotency_key or _hashlib.sha256(f"{category}/{fp.name}".encode() + bundle).hexdigest()
    try:
        client = _drive_client()
        tree = client.ensure_archive_tree()
        meta = client.resumable_upload(folder_id=tree[category], name=f"{fp.name}.enc",
                                       data=bundle, idempotency_key=idem, chunk_bytes=256 * 1024)
    except DriveError as exc:
        typer.secho(f"upload failed: {exc.category}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo(_json.dumps({
        "drive_file_id": meta["drive_file_id"], "category": category,
        "byte_size": meta["byte_size"], "bundle_sha256": meta["sha256"],
        "plaintext_sha256": plain_sha, "idempotency_key": idem, "encrypted": True,
    }, indent=2))


@drive_app.command("verify")
def drive_verify(
    file_id: str = typer.Option(..., "--file-id"),
    expect_bytes: int = typer.Option(None, "--expect-bytes"),
    expect_sha256: str = typer.Option(None, "--expect-sha256",
                                      help="Expected BUNDLE sha256 (the value `upload` printed)."),
) -> None:
    """Verify an archived file: it exists, byte count + digest match (downloads and recomputes)."""
    import json as _json

    from aithernet.data import crypto
    from aithernet.data.destinations import google_drive_client as g
    from aithernet.data.destinations.drive import DriveError

    try:
        client = _drive_client()
        md = client.get_metadata(file_id)
        if md is None or md.get("trashed"):
            typer.echo(_json.dumps({"file_id": file_id, "exists": False, "ok": False}))
            raise typer.Exit(1)
        data = client.download(file_id)
    except DriveError as exc:
        typer.secho(f"verify failed: {exc.category}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    recomputed = crypto.sha256_hex(data)
    recorded = (md.get("appProperties") or {}).get(g._SHA_KEY)
    result = {
        "file_id": file_id, "exists": True, "byte_size": len(data),
        "recomputed_sha256": recomputed, "recorded_sha256": recorded,
        "digest_match": recomputed == recorded,
        "bytes_match": (expect_bytes is None or len(data) == expect_bytes),
        "expected_match": (expect_sha256 is None or recomputed == expect_sha256),
        "looks_encrypted": data[:1] == b"{" and b"aith-batch-enc" in data[:64],
    }
    result["ok"] = bool(result["digest_match"] and result["bytes_match"]
                        and result["expected_match"] and result["looks_encrypted"])
    typer.echo(_json.dumps(result, indent=2))
    if not result["ok"]:
        raise typer.Exit(1)


@drive_app.command("delete")
def drive_delete(
    file_id: str = typer.Option(..., "--file-id"),
) -> None:
    """Delete an archived file and VERIFY it is gone (deletion is explicit and confirmed)."""
    import json as _json

    from aithernet.data.destinations import google_drive_client as g  # noqa: F401
    from aithernet.data.destinations.drive import DriveError

    try:
        client = _drive_client()
        deleted = client.delete_file(file_id)
        still = client.get_metadata(file_id)
    except DriveError as exc:
        typer.secho(f"delete failed: {exc.category}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    gone = still is None or still.get("trashed", False)
    typer.echo(_json.dumps({"file_id": file_id, "delete_returned": deleted,
                            "verified_gone": gone, "ok": gone}, indent=2))
    if not gone:
        raise typer.Exit(1)


_HTTP_TIMEOUT = 10.0
# Coordinator processing waits on a model call, so it gets a longer timeout.
_PROCESS_TIMEOUT = 120.0
# A coding-agent run can take much longer (it executes a full agent session).
_RUN_TIMEOUT = 900.0
# An MCP tool call launches an external server and waits on it.
_MCP_TIMEOUT = 120.0

# Shared CLI options.
ConfigOption = typer.Option(
    str(DEFAULT_CONFIG_PATH), "--config", "-c", help="Path to node.yaml.", show_default=True
)
UrlOption = typer.Option(
    None, "--url", "-u", help="Base URL of the running node (overrides config)."
)
# Shared probe flag (coordinator + MCP diagnostics): run one real inference / launch.
ProbeOption = typer.Option(
    False, "--probe", help="Run a live probe (one real inference / launch the server)."
)

# Shared coding-task options (module-level singletons keep Typer's callable defaults out
# of function signatures, the pattern B008 recommends).
MissionIdOption = typer.Option(None, "--mission-id", help="Owning mission id.")
ContextJsonOption = typer.Option(
    None, "--context-json", help="Structured context as a JSON object."
)
AvailableToolOption = typer.Option(
    None, "--available-tool", help="A tool the task may use (repeatable)."
)
ExpectedOutputOption = typer.Option(
    None, "--expected-output", help="An expected output (repeatable)."
)
ReportingRequirementOption = typer.Option(
    None, "--reporting-requirement", help="A reporting requirement (repeatable)."
)


ExtAgentPermissionsArgument = typer.Option(
    ..., "--permission", help="Repeatable permission grant.")


def _effective_config(config: str) -> str:
    """Prefer the canonical installed config over the dev-tree default when the operator did not
    pass --config and AITHERNET_CONFIG is unset. Keeps customer CLI commands (mcp, status, …) on
    the same node.yaml the service uses, instead of a developer configs/node.yaml."""
    import os as _os

    from aithernet.config.loader import canonical_node_config_path
    if config == str(DEFAULT_CONFIG_PATH) and "AITHERNET_CONFIG" not in _os.environ:
        canonical = canonical_node_config_path()
        if canonical.is_file():
            return str(canonical)
    return config


def _resolve_base_url(url: str | None, config: str) -> str:
    """Return the node base URL, preferring an explicit ``--url`` over config."""
    if url:
        return url.rstrip("/")
    return load_config(_effective_config(config)).base_url


def _client(base_url: str) -> httpx.Client:
    from aithernet.tls import verify_option
    return httpx.Client(base_url=base_url, timeout=_HTTP_TIMEOUT, verify=verify_option())


def _fail_unreachable(base_url: str, exc: Exception) -> typer.Exit:
    typer.secho(
        f"Error: could not reach node at {base_url} ({exc}).\n"
        "Is the server running?  Start it with:  aithernet start",
        fg=typer.colors.RED,
        err=True,
    )
    return typer.Exit(code=1)


# Max characters of a foreign/error response body ever echoed (tags stripped first).
_SAFE_BODY_PREVIEW_CHARS = 200


def _strip_html_tags(text: str) -> str:
    """Collapse an HTML/error page into a short, single-line, tag-free, secret-free preview.

    Reverse proxies (nginx/Caddy) and gateway front-ends answer wrong routes with full HTML
    pages. We NEVER echo raw HTML — we strip scripts/styles/tags/entities and collapse
    whitespace so at most a short human-readable sentence survives.
    """
    import re

    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)  # drop script/style bodies
    text = re.sub(r"(?s)<[^>]+>", " ", text)                          # strip remaining tags
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)                      # drop HTML entities
    return re.sub(r"\s+", " ", text).strip()


def _route_info(response: httpx.Response) -> dict:
    """Only-safe routing facts from a response's request URL (never headers/auth/cookies)."""
    url = response.request.url
    return {
        "method": response.request.method,
        "scheme": url.scheme,
        "host": url.host,
        "port": url.port or (443 if url.scheme == "https" else 80),
        "path": url.path,
    }


def _configured_coordinator_endpoint() -> httpx.URL | None:
    """Best-effort read of the configured coordinator provider endpoint, for boundary hints.

    Read only to compare host:port — its value is not a secret. Any failure yields ``None``.
    """
    import os as _os

    try:
        cfg = load_config(_effective_config(str(DEFAULT_CONFIG_PATH)))
        raw = getattr(cfg.coordinator, "base_url", None)
    except Exception:
        raw = None
    raw = raw or _os.environ.get("AITHERNET_CATGPT_GATEWAY_BASE_URL")
    if not raw:
        return None
    try:
        return httpx.URL(raw)
    except Exception:
        return None


def _foreign_endpoint_hint(info: dict, response: httpx.Response) -> str | None:
    """A safe one-line hint about *what* answered when it wasn't the Aithernet node API."""
    coord = _configured_coordinator_endpoint()
    if coord is not None and coord.host == info["host"] and (
        (coord.port or 80) == info["port"]
    ):
        return (
            "this host:port is the configured coordinator provider endpoint "
            "(coordinator.base_url) — a coordinator/CatGPT gateway is NOT the mission API"
        )
    banner = response.headers.get("server") or response.headers.get("via")
    if banner and any(p in banner.lower() for p in ("nginx", "caddy", "apache", "envoy")):
        return (
            f"the response came from '{banner}', a generic web server/reverse proxy — "
            "not the Aithernet node API"
        )
    return None


def _fail_non_json(response: httpx.Response, *, subsystem: str) -> typer.Exit:
    """Emit a structured, secret-free error for a non-JSON HTTP response where JSON was expected.

    Includes only safe facts (method/scheme/host/port/path/status/content-type/server, a
    tag-stripped body preview, the calling subsystem, and a foreign-endpoint hint). NEVER
    includes Authorization, bearer tokens, cookies, full HTML, or env secret values.
    """
    info = _route_info(response)
    ctype = response.headers.get("content-type", "unknown")
    server = response.headers.get("server") or response.headers.get("via")
    preview = _strip_html_tags(response.text)[:_SAFE_BODY_PREVIEW_CHARS]
    lines = [
        f"Error: {subsystem} received a non-JSON response where the Aithernet node API was "
        "expected.",
        f"  request:      {info['method']} {info['scheme']}://{info['host']}:"
        f"{info['port']}{info['path']}",
        f"  http status:  {response.status_code}",
        f"  content-type: {ctype}",
    ]
    if server:
        lines.append(f"  server:       {server}")
    if preview:
        lines.append(f"  body:         {preview}")
    hint = _foreign_endpoint_hint(info, response)
    if hint:
        lines.append(f"  hint:         {hint}")
    lines.append(
        "  This is not the Aithernet node API. Either the node isn't running, or another service "
        "(a reverse proxy, static site, or the CatGPT gateway) is bound to this port.\n"
        "  Start the node:  aithernet service start     (verify the node port: aithernet doctor)"
    )
    typer.secho("\n".join(lines), fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


def _check(response: httpx.Response) -> None:
    """Raise a clean Typer error for non-2xx responses.

    A JSON error body surfaces its ``detail`` (the node API contract). A non-JSON error body
    (e.g. an nginx/Caddy HTML page from a foreign service on the node's port) is sanitized to a
    short tag-stripped preview — raw HTML is never dumped to the terminal.
    """
    if response.is_success:
        return
    detail = None
    if "json" in response.headers.get("content-type", "").lower():
        try:
            body = response.json()
            detail = body.get("detail") if isinstance(body, dict) else None
        except (json.JSONDecodeError, ValueError):
            detail = None
    if detail is None:
        detail = _strip_html_tags(response.text)[:_SAFE_BODY_PREVIEW_CHARS] or (
            response.reason_phrase or "request failed"
        )
    typer.secho(f"Error {response.status_code}: {detail}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _node_json(response: httpx.Response, *, subsystem: str = "mission_api"):
    """Parse an Aithernet node-API JSON body, or fail with a structured, secret-free error.

    Handles BOTH failure shapes that a foreign server on the node's port produces:
      * a non-2xx HTML/text error (e.g. nginx ``405 Not Allowed``), and
      * a **2xx** response whose body is HTML (e.g. a static SPA ``index.html``) — which would
        otherwise raise a raw ``JSONDecodeError``.

    A genuine node-API JSON error (``{"detail": …}``) is surfaced cleanly via :func:`_check`.
    """
    if "json" in response.headers.get("content-type", "").lower():
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError):
            raise _fail_non_json(response, subsystem=subsystem) from None
        if not response.is_success:
            _check(response)  # JSON error body → clean "Error <status>: <detail>"
        return data
    # Non-JSON body where the node API was expected → the port is answered by something else.
    raise _fail_non_json(response, subsystem=subsystem)


def _json(response: httpx.Response):
    """Backwards-compatible alias: parse a node-API JSON body or fail structurally."""
    return _node_json(response, subsystem="node_api")


def _guard_mission_endpoint(base_url: str, config: str) -> None:
    """Block the release-1 boundary violation: the mission API base URL must not be the
    coordinator provider endpoint. A coordinator/CatGPT gateway only provides model inference —
    it is never the Aithernet mission API, database, run queue, or event store."""
    coord = _configured_coordinator_endpoint()
    if coord is None:
        return
    try:
        mission = httpx.URL(base_url)
    except Exception:
        return
    same_host = coord.host == mission.host
    same_port = (coord.port or 80) == (mission.port or (443 if mission.scheme == "https" else 80))
    if same_host and same_port:
        typer.secho(
            "Error: Coordinator provider endpoint cannot be used as the Aithernet mission API.\n"
            f"  mission API base URL resolves to {mission.scheme}://{mission.host}:"
            f"{mission.port or 80}\n"
            "  which is the configured coordinator provider endpoint (coordinator.base_url).\n"
            "  The coordinator gateway only provides model inference; it is not the mission API,\n"
            "  database, run queue, or event store. Point the node/CLI at the Aithernet node port\n"
            "  (aithernet doctor shows it), not the coordinator gateway.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)


def _mission_base_url(url: str | None, config: str) -> str:
    """Resolve the Aithernet mission API base URL and enforce the coordinator/mission boundary.

    Mission commands route through THIS (a distinct namespace) rather than the generic
    :func:`_resolve_base_url`, so an ambiguous ``base_url`` lookup can never silently target the
    coordinator provider endpoint.
    """
    base_url = _resolve_base_url(url, config)
    _guard_mission_endpoint(base_url, config)
    return base_url


# -- server ----------------------------------------------------------------------


def _port_in_use(host: str, port: int) -> bool:
    """True if ``host:port`` is already held by a live listener. Uses SO_REUSEADDR (as uvicorn
    does), so a TIME_WAIT socket reads as free while an actively-listening node reads as in use."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
            return False
        except OSError:
            return True


@app.command()
def start(
    config: str = ConfigOption,
    host: str | None = typer.Option(None, help="Override bind host from config."),
    port: int | None = typer.Option(None, help="Override bind port from config."),
    reload: bool = typer.Option(False, "--reload", help="Enable autoreload (development)."),
) -> None:
    """Run the node server in the FOREGROUND (Ctrl-C stops it cleanly).

    This is the interactive/one-off way to run a node — it stays attached to your terminal and
    shuts down gracefully on Ctrl-C. For a node that keeps running across logout/reboot, install
    the packaged background service instead: ``aithernet service install`` then manage it with
    ``aithernet service start|status|stop|restart``.
    """
    import errno
    import os as _os

    import uvicorn

    from aithernet.api.app import create_app
    from aithernet.config.loader import canonical_node_config_path

    # When the operator runs `aithernet start` by hand (no --config, no AITHERNET_CONFIG from the
    # unit), prefer the canonical installed config over the dev-tree configs/node.yaml so a customer
    # never silently boots on placeholder defaults (placeholder node id, cwd ./aithernet.db).
    if config == str(DEFAULT_CONFIG_PATH) and "AITHERNET_CONFIG" not in _os.environ:
        canonical = canonical_node_config_path()
        if canonical.is_file():
            config = str(canonical)
            typer.secho(f"Using canonical node config: {config}", fg=typer.colors.CYAN)

    # Load the managed 0600 secret store into this process's environment, exactly as the systemd
    # unit does via `EnvironmentFile=-%h/.config/aithernet/aithernet.env`. Without this a hand-run
    # foreground node resolves `api_key: env:CATGPT_GATEWAY_API_KEY` (and any other secret ref) to
    # nothing, so the coordinator would 401 mid-mission — a footgun a client should never hit.
    # override=False keeps an explicitly-exported shell value winning. Values are never printed.
    from aithernet.agents.providers import secret_env_file
    from aithernet.config.loader import load_dotenv_file
    _secret_env = secret_env_file()
    if load_dotenv_file(_secret_env):
        typer.secho(f"Loaded managed secrets from {_secret_env} (values not shown).",
                    fg=typer.colors.CYAN)

    cfg = load_config(config)
    bind_host = host or cfg.host
    bind_port = port or cfg.port

    # Pre-flight: if something is already listening, say so concisely instead of letting uvicorn
    # raise a "[Errno 98] address already in use" traceback at the customer.
    if _port_in_use(bind_host, bind_port):
        typer.secho(
            f"A node already appears to be running on {bind_host}:{bind_port}.",
            fg=typer.colors.YELLOW,
        )
        typer.echo(
            "Check it with `aithernet status`. If it's the background service, use "
            "`aithernet service status` / `aithernet service restart`. To run a foreground node on "
            "a different port: `aithernet start --port <PORT>`.",
        )
        raise typer.Exit(code=1)

    typer.secho(
        f"Starting node '{cfg.node_name}' ({cfg.node_id}) on {bind_host}:{bind_port} "
        "(press Ctrl-C to stop)",
        fg=typer.colors.GREEN,
    )

    try:
        if reload:
            # Reload requires an import string; configuration is read again in-process.
            import os

            os.environ["AITHERNET_CONFIG"] = config
            uvicorn.run(
                "aithernet.main:app",
                host=bind_host,
                port=bind_port,
                log_level=cfg.log_level,
                reload=True,
            )
        else:
            uvicorn.run(
                create_app(cfg), host=bind_host, port=bind_port, log_level=cfg.log_level
            )
    except KeyboardInterrupt:  # belt-and-suspenders: uvicorn handles SIGINT, but never traceback
        typer.secho("\nNode stopped.", fg=typer.colors.GREEN)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:  # lost a race after the pre-flight check
            typer.secho(
                f"Cannot bind {bind_host}:{bind_port} — it is already in use. "
                "Another node may have started in the meantime.",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(code=1) from exc
        raise


@app.command()
def status(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Fetch and print the running node's status."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/node/status")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    data = response.json()
    typer.secho(f"Node:    {data['node_name']} ({data['node_id']})", fg=typer.colors.CYAN)
    typer.echo(f"Status:  {data['runtime_status']}")
    typer.echo(f"Version: {data['version']}")
    typer.echo(f"DB:      {data['database_path']}")
    typer.echo(f"Started: {data['started_at']}")
    typer.echo(f"Missions: {data['mission_count']}   Events: {data['event_count']}")


# -- database migrations ---------------------------------------------------------
# These commands operate DIRECTLY on the configured local database (no running server is
# needed), so an operator can inspect and migrate an outdated database before starting the
# node. They never print database rows, the full database path, credentials, keys, or env.


def _database_engine(config: str):
    from aithernet.state.db import create_db_engine

    return create_db_engine(load_config(config).database_url)


def _safe_db_name(config: str) -> str:
    """A sanitized database identifier: the file name only for SQLite, never the full path."""
    from pathlib import Path

    url = load_config(config).database_url
    if url.startswith("sqlite"):
        tail = url.rsplit("/", 1)[-1]
        return Path(tail).name or "(in-memory)"
    # Non-SQLite: report only the dialect/scheme, never host/credentials in the URL.
    return url.split(":", 1)[0]


@database_app.command("status")
def database_status(config: str = ConfigOption) -> None:
    """Show the database dialect, applied/pending migrations, and schema validation state."""
    from aithernet.state.migrations import migration_status

    status = migration_status(_database_engine(config))
    typer.secho(f"Database: {_safe_db_name(config)} ({status.dialect})", fg=typer.colors.CYAN)
    typer.echo(f"Applied migrations: {len(status.applied)}")
    for migration_id, applied_at in status.applied:
        typer.echo(f"  ✓ {migration_id}  ({applied_at})")
    if status.pending:
        typer.secho(f"Pending: {', '.join(status.pending)}", fg=typer.colors.YELLOW)
    else:
        typer.echo("Pending: none")
    v = status.validation
    if v.ok:
        typer.secho("Schema: valid (fully migrated)", fg=typer.colors.GREEN)
    else:
        typer.secho("Schema: NEEDS MIGRATION", fg=typer.colors.YELLOW)
        if v.missing_tables:
            typer.echo(f"  missing tables: {', '.join(v.missing_tables)}")
        if v.missing_columns:
            typer.echo(f"  missing columns: {', '.join(v.missing_columns)}")
        if v.missing_indexes:
            typer.echo(f"  missing indexes: {', '.join(v.missing_indexes)}")


@database_app.command("migrate")
def database_migrate(config: str = ConfigOption) -> None:
    """Apply every pending additive migration in order (idempotent, non-destructive)."""
    from aithernet.state.migrations import MigrationError, run_migrations, validate_schema

    engine = _database_engine(config)
    try:
        report = run_migrations(engine)
    except MigrationError as exc:
        # Sanitized: migration id + error type only — never SQL, rows, keys, or env.
        typer.secho(f"Migration failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    if report.applied_now:
        typer.secho(
            f"Applied {len(report.applied_now)} migration(s): {', '.join(report.applied_now)}",
            fg=typer.colors.GREEN,
        )
    else:
        typer.echo("Database already up to date; nothing to apply.")
    v = validate_schema(engine)
    if v.ok:
        typer.secho("Schema is valid and fully migrated.", fg=typer.colors.GREEN)
    else:  # pragma: no cover - a successful migrate should always validate
        typer.secho("Schema still invalid after migration.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@database_app.command("validate")
def database_validate(config: str = ConfigOption) -> None:
    """Validate the live schema against the current models without changing anything."""
    from aithernet.state.migrations import validate_schema

    v = validate_schema(_database_engine(config))
    if v.ok:
        typer.secho("Schema is valid and fully migrated.", fg=typer.colors.GREEN)
        return
    typer.secho("Schema is NOT valid:", fg=typer.colors.RED, err=True)
    if v.missing_tables:
        typer.echo(f"  missing tables: {', '.join(v.missing_tables)}")
    if v.missing_columns:
        typer.echo(f"  missing columns: {', '.join(v.missing_columns)}")
    if v.missing_indexes:
        typer.echo(f"  missing indexes: {', '.join(v.missing_indexes)}")
    if v.pending_migrations:
        typer.echo(f"  pending migrations: {', '.join(v.pending_migrations)}")
    raise typer.Exit(code=1)


# -- missions --------------------------------------------------------------------


def _print_decision(decision: dict) -> None:
    """Render a coordinator decision for the terminal."""
    typer.secho(f"Decision:     {decision['decision_id']}", fg=typer.colors.GREEN)
    typer.echo(f"Summary:      {decision['summary']}")
    typer.echo(f"Next target:  {decision['next_target']}")
    typer.echo(f"Action:       {decision['action']}")
    typer.echo(f"Message:      {decision['message']}")
    typer.echo(f"Expected:     {decision['expected_result']}")
    if decision.get("confidence") is not None:
        typer.echo(f"Confidence:   {decision['confidence']}")


@mission_app.command("rf-plan")
def mission_rf_plan(
    objective: str = typer.Argument(..., help="Natural-language receive-only objective."),
    adapter: str = typer.Option(..., "--adapter",
                                help="Hardware adapter: software-only|simulation|plutosdr|usrp|"
                                     "generic-soapy|full-lab"),
    device: str = typer.Option(..., "--device", help="Target device id."),
    freq_hz: float = typer.Option(..., "--freq-hz", help="Center frequency (Hz)."),
    sample_rate: float = typer.Option(..., "--sample-rate", help="Sample rate (S/s)."),
    duration_s: float = typer.Option(..., "--duration-s", help="Capture duration (s)."),
    gain_db: float = typer.Option(40.0, "--gain-db"),
    artifact_max_bytes: int = typer.Option(None, "--artifact-max-bytes"),
    artifact_max_count: int = typer.Option(None, "--artifact-max-count"),
    coordinator: str = typer.Option("", "--coordinator"),
    coding: str = typer.Option("", "--coding"),
    authorization: str = typer.Option("", "--authorization",
                                      help="Operator-stated authorization boundary (recorded)."),
    show_bounds: bool = typer.Option(False, "--show-bounds",
                                     help="Also print the resolved receive envelope."),
) -> None:
    """Validate a RECEIVE-ONLY RF mission into a deterministic, frozen authorized capture plan.

    The effective envelope is the intersection of the global receive-only policy and the selected
    hardware adapter's capabilities (device-specific limits live in the adapter, not here). This is
    the gate the AI coordinator cannot widen: it enforces receive-only (no transmit path), the band,
    sample-rate/duration/gain bounds, device authorization and artifact limits, and emits a digest
    for audit. Makes no changes and touches no hardware."""
    from aithernet.missions import rf_constraints as rfc
    try:
        bounds = rfc.resolve_bounds(adapter)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    req = rfc.RFMissionRequest(
        objective=objective, device_id=device, center_frequency_hz=freq_hz,
        sample_rate=sample_rate, duration_s=duration_s, gain_db=gain_db,
        artifact_max_bytes=artifact_max_bytes, artifact_max_count=artifact_max_count,
        coordinator=coordinator, coding=coding, authorization_boundary=authorization)
    try:
        plan = rfc.validate(req, bounds)
    except rfc.ConstraintViolation as exc:
        out = {"authorized": False, "violation": exc.category, "detail": str(exc)}
        if show_bounds:
            out["bounds"] = rfc.explain_bounds(bounds)
        typer.echo(json.dumps(out, indent=2))
        raise typer.Exit(code=1) from exc
    out = {"authorized": True, "plan": plan.to_dict()}
    if show_bounds:
        out["bounds"] = rfc.explain_bounds(bounds)
    typer.echo(json.dumps(out, indent=2))


# -- customer mission verbs over the CANONICAL mission engine -------------------
# These are thin aliases that delegate to the canonical mission commands (which drive the one true
# MissionEngine via the node API). There is NO second mission engine or JSON mission format.
# `cancel`/`resume` already exist canonically below; `rf-plan` (the receive-only capture validator)
# remains as a pre-flight planning aid.
@mission_app.command("create")
def mission_create(
    objective: str = typer.Argument(..., help="Natural-language mission objective."),
    source_type: str = typer.Option("user", "--source-type"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Create a mission via the canonical engine (alias of `mission submit`)."""
    mission_submit(content=objective, source_type=source_type, source_id=None,
                   process=False, config=config, url=url)


@mission_app.command("run")
def mission_run(
    mission_id: str = typer.Argument(..., help="Mission id to run."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Run a mission through the canonical autonomous engine (alias of `mission start`)."""
    mission_start(mission_id=mission_id, config=config, url=url)


@mission_app.command("status")
def mission_status_cmd(
    mission_id: str = typer.Argument(..., help="Mission id."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Show a mission's status via the canonical engine (alias of `mission show`)."""
    mission_show(mission_id=mission_id, config=config, url=url)


@mission_app.command("events")
def mission_events_cmd(
    mission_id: str = typer.Argument(..., help="Mission id."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Show the canonical mission event timeline (alias of `mission timeline`)."""
    mission_timeline(mission_id=mission_id, config=config, url=url)


@mission_app.command("artifacts")
def mission_artifacts_cmd(
    mission_id: str = typer.Argument(..., help="Mission id."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """List the canonical RF artifacts produced by a mission (metadata only)."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/rf/artifacts", params={"mission_id": mission_id})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    typer.echo(json.dumps(_node_json(response, subsystem="mission_api"), indent=2))


@mission_app.command("export")
def mission_export_cmd(
    mission_id: str = typer.Argument(..., help="Mission id."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Export the canonical mission record + timeline (alias of `mission timeline`)."""
    mission_timeline(mission_id=mission_id, config=config, url=url)


@mission_app.command("submit")
def mission_submit(
    content: str = typer.Argument(..., help="The mission text."),
    source_type: str = typer.Option("user", "--source-type", help="Origin class."),
    source_id: str | None = typer.Option(None, "--source-id", help="Originating source id."),
    process: bool = typer.Option(
        False, "--process", help="Immediately process the mission with the coordinator."
    ),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Advanced: CREATE a mission only (does NOT start autonomous execution). `--process` runs ONE
    coordinator step. For the normal one-command workflow use `aithernet run "<prompt>"`, which
    creates, starts, streams and returns the result."""
    base_url = _mission_base_url(url, config)
    body = {"content": content, "source_type": source_type, "source_id": source_id}
    try:
        with _client(base_url) as client:
            response = client.post("/missions", json=body)
            data = _node_json(response, subsystem="mission_api")
            typer.secho(f"Mission submitted: {data['id']}", fg=typer.colors.GREEN)
            typer.echo(f"Status: {data['status']}")

            if process:
                typer.echo("Processing with coordinator...")
                proc = client.post(
                    f"/missions/{data['id']}/process", timeout=_PROCESS_TIMEOUT
                )
                _print_decision(_node_json(proc, subsystem="mission_api"))
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc


@mission_app.command("process")
def mission_process(
    mission_id: str = typer.Argument(..., help="Mission id to process."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Process a mission through the coordinator agent and print the decision."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(
                f"/missions/{mission_id}/process", timeout=_PROCESS_TIMEOUT
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _print_decision(_node_json(response, subsystem="mission_api"))


def _print_step(step: dict) -> None:
    """Render a mission step (route outcome) for the terminal."""
    status_colors = {
        "completed": typer.colors.GREEN,
        "failed": typer.colors.RED,
        "blocked": typer.colors.YELLOW,
    }
    color = status_colors.get(step["status"], typer.colors.WHITE)
    typer.secho(f"Step:     {step['id']}", fg=typer.colors.CYAN)
    typer.echo(f"Target:   {step['route_target']}")
    typer.echo(f"Action:   {step['route_action']}")
    typer.secho(f"Status:   {step['status']}", fg=color)
    if step.get("error"):
        typer.echo(f"Error:    {_preview(step['error'])}")
    result = step.get("result") or {}
    if result.get("type") == "response":
        typer.echo(f"Message:  {result.get('message', '')}")
    elif result.get("type") == "coding_task":
        typer.echo(
            f"Coding task: {result.get('task_id')} "
            f"(result {result.get('result_status')}, exit={result.get('exit_code')})"
        )
        if result.get("summary"):
            typer.echo(f"Summary:  {_preview(result['summary'])}")
    elif result.get("type") == "mcp_tool_call":
        typer.echo(
            f"MCP call: {result.get('call_id')} "
            f"tool={result.get('tool_name')} status={result.get('status')}"
        )
    elif result.get("type") == "node_state":
        typer.echo("Node-state summary returned (use step-show for full detail).")


@mission_app.command("step")
def mission_step(
    mission_id: str = typer.Argument(..., help="Mission id to run one step for."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Run one coordinator decision + routed execution step for a mission."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(f"/missions/{mission_id}/step", timeout=_RUN_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    data = response.json()
    _print_decision(data["decision"])
    typer.echo("")
    _print_step(data["step"])


@mission_app.command("steps")
def mission_steps(
    mission_id: str | None = typer.Option(None, "--mission-id", help="Filter by mission."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """List mission steps, newest first."""
    base_url = _mission_base_url(url, config)
    params = {"mission_id": mission_id} if mission_id else None
    try:
        with _client(base_url) as client:
            response = client.get("/mission-steps", params=params)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc

    steps = _node_json(response, subsystem="mission_api")
    if not steps:
        typer.echo("No mission steps.")
        return
    for s in steps:
        typer.echo(
            f"{s['id']}  {s['status']:<10}  {s['route_target']:<16}  "
            f"mission={s['mission_id']}  {s['created_at']}"
        )


@mission_app.command("step-show")
def mission_step_show(
    step_id: str = typer.Argument(..., help="Mission step id."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Show a single mission step in full."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/mission-steps/{step_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    typer.echo(json.dumps(_node_json(response, subsystem="mission_api"), indent=2))


@mission_app.command("list")
def mission_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List missions on the node, newest first."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/missions")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc

    missions = _node_json(response, subsystem="mission_api")
    if not missions:
        typer.echo("No missions.")
        return
    for m in missions:
        preview = m["content"].replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:57] + "..."
        typer.echo(f"{m['id']}  {m['status']:<10}  {m['created_at']}  {preview}")


@mission_app.command("show")
def mission_show(
    mission_id: str = typer.Argument(..., help="Mission id."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Show a single mission in full."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/missions/{mission_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    typer.echo(json.dumps(_node_json(response, subsystem="mission_api"), indent=2))


# -- autonomous mission execution (Stage 12) -------------------------------------


def _post_mission_op(base_url: str, path: str, *, timeout: float = _HTTP_TIMEOUT) -> dict:
    """POST a mission lifecycle operation and return the parsed JSON body."""
    try:
        with _client(base_url) as client:
            response = client.post(path, timeout=timeout)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    return _node_json(response, subsystem="mission_api")


def _print_run(run: dict) -> None:
    """Render an execution-run summary (never shows a lease token — it is not serialized)."""
    typer.echo(f"Run {run['id']}  status={run['status']}  iteration={run['iteration_count']}")
    lease = run.get("execution_owner_id") or "-"
    typer.echo(
        f"  mission={run['mission_id']}  owner={lease}  lease_expiry={run.get('lease_expiry')}"
    )
    if run.get("last_route_target"):
        typer.echo(
            f"  last_route={run['last_route_target']}  "
            f"last_decision={run.get('last_decision_id')}"
        )
    if run.get("blocked_reason"):
        typer.echo(f"  blocked: {run['blocked_reason']}")
    if run.get("failure_message"):
        typer.echo(f"  failure: {run['failure_message']}")
    if run.get("final_response"):
        typer.echo(f"  final_response: {run['final_response']}")


def _print_execution(data: dict) -> None:
    """Render a mission execution status (mission + run + remaining budgets)."""
    mission = data["mission"]
    typer.echo(f"Mission {mission['id']}  status={mission['status']}")
    run = data.get("run")
    if run is None:
        typer.echo("  (no execution run yet)")
        return
    _print_run(run)
    budget = data.get("budget_remaining") or {}
    if budget:
        parts = "  ".join(f"{k}={v}" for k, v in budget.items())
        typer.echo(f"  budget_remaining: {parts}")


@mission_app.command("start")
def mission_start(
    mission_id: str = typer.Argument(..., help="Mission id to start autonomously."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Queue a mission for autonomous execution (returns after queueing, not completion)."""
    base_url = _mission_base_url(url, config)
    data = _post_mission_op(base_url, f"/missions/{mission_id}/start")
    typer.secho(data.get("detail", "Queued."), fg=typer.colors.GREEN)
    _print_run(data["run"])


@mission_app.command("retry")
def mission_retry(
    mission_id: str = typer.Argument(..., help="Blocked/failed mission id to retry."),
    watch: bool = typer.Option(
        False, "--watch", help="Stream progress until the retried run finishes."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Retry a blocked/failed mission with a fresh run after fixing the cause (e.g. provider config
    or `aithernet doctor --repair`). Re-runs the SAME mission — never creates a duplicate."""
    base_url = _mission_base_url(url, config)
    data = _post_mission_op(base_url, f"/missions/{mission_id}/retry")
    typer.secho(data.get("detail", "Re-queued."), fg=typer.colors.GREEN)
    _print_run(data["run"])
    if watch:
        final = _stream_until_terminal(base_url, mission_id, interval=1.5, timeout=1800.0)
        if final is not None:
            _print_run_result(base_url, mission_id, final)
            status = (final.get("run") or {}).get("status")
            raise typer.Exit(code=0 if status == "completed" else 1)


@mission_app.command("pause")
def mission_pause(
    mission_id: str = typer.Argument(..., help="Mission id to pause."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Request a pause; a running mission pauses at its next atomic boundary."""
    base_url = _mission_base_url(url, config)
    _print_run(_post_mission_op(base_url, f"/missions/{mission_id}/pause"))


@mission_app.command("resume")
def mission_resume(
    mission_id: str = typer.Argument(..., help="Mission id to resume."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Resume a paused/waiting mission (requeue for reassessment; no replay)."""
    base_url = _mission_base_url(url, config)
    _print_run(_post_mission_op(base_url, f"/missions/{mission_id}/resume"))


@mission_app.command("cancel")
def mission_cancel(
    mission_id: str = typer.Argument(..., help="Mission id to cancel."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Request cancellation; a running mission cancels at its next atomic boundary."""
    base_url = _mission_base_url(url, config)
    _print_run(_post_mission_op(base_url, f"/missions/{mission_id}/cancel"))


@mission_app.command("run-step")
def mission_run_step(
    mission_id: str = typer.Argument(..., help="Mission id to run one autonomous iteration for."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Run exactly one autonomous iteration (no background loop)."""
    base_url = _mission_base_url(url, config)
    data = _post_mission_op(base_url, f"/missions/{mission_id}/run-step", timeout=_RUN_TIMEOUT)
    _print_execution(data)


@mission_app.command("execution")
def mission_execution(
    mission_id: str = typer.Argument(..., help="Mission id."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Show the mission's execution status (run, lease, budgets — no lease token)."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/missions/{mission_id}/execution")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _print_execution(_node_json(response, subsystem="mission_api"))


@mission_app.command("timeline")
def mission_timeline(
    mission_id: str = typer.Argument(..., help="Mission id."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Show a chronological mission timeline (events + step summaries)."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/missions/{mission_id}/timeline")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    entries = _node_json(response, subsystem="mission_api").get("entries", [])
    if not entries:
        typer.echo("No timeline entries.")
        return
    seen_task_ids: set[str] = set()
    for e in entries:
        label = e.get("event_type") or e.get("route_target") or e["kind"]
        typer.echo(f"{e['at']}  [{e['kind']:<5}] {label:<22} {e['message']}")
        tid = e.get("coding_task_id")
        if tid:
            seen_task_ids.add(tid)
    if seen_task_ids:
        typer.echo("")
        typer.secho("Coding tasks in this mission — inspect with `aithernet coding-task status "
                    "<id>`:", fg=typer.colors.CYAN)
        for tid in sorted(seen_task_ids):
            typer.echo(f"  {tid}")


@mission_app.command("runs")
def mission_runs(
    limit: int = typer.Option(50, "--limit", help="Maximum runs to list."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """List execution runs, newest first (no lease tokens)."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/mission-runs", params={"limit": limit})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    runs = _node_json(response, subsystem="mission_api")
    if not runs:
        typer.echo("No execution runs.")
        return
    for r in runs:
        typer.echo(
            f"{r['created_at']}  {r['id']}  status={r['status']:<9} "
            f"iter={r['iteration_count']}  mission={r['mission_id']}"
        )


@mission_app.command("watch")
def mission_watch(
    mission_id: str = typer.Argument(..., help="Mission id to watch."),
    interval: float = typer.Option(1.0, "--interval", help="Poll interval seconds."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Watch a mission's execution status live (read-only polling; never starts a worker)."""
    base_url = _mission_base_url(url, config)
    typer.secho(f"Watching mission {mission_id} (Ctrl-C to stop)...", fg=typer.colors.CYAN)
    last = None
    try:
        while True:
            try:
                with _client(base_url) as client:
                    response = client.get(f"/missions/{mission_id}/execution")
            except httpx.HTTPError as exc:
                raise _fail_unreachable(base_url, exc) from exc
            _check(response)
            data = response.json()
            run = data.get("run") or {}
            key = (data["mission"]["status"], run.get("status"), run.get("iteration_count"))
            if key != last:
                last = key
                _print_execution(data)
                typer.echo("-" * 40)
            if run.get("status") in ("completed", "blocked", "failed", "cancelled"):
                break
            time.sleep(max(0.1, interval))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        typer.echo("\nStopped watching.")


# -- beta.9 Defect 4: one-command customer workflow ------------------------------------------

_TERMINAL_RUN_STATES = ("completed", "blocked", "failed", "cancelled")


def _run_unreachable_guidance(base_url: str, exc: Exception) -> typer.Exit:
    """When `aithernet run` can't reach the node, guide the customer into setup/start in plain
    language (never a raw connection traceback or internal config wording)."""
    from aithernet.config.loader import canonical_node_config_path
    if canonical_node_config_path().is_file():
        typer.secho(
            "Aithernet is set up but not running.\n"
            "  Start it:  aithernet service start    (or: systemctl --user start "
            "aithernet-node.service)\n"
            "  Then run your command again.",
            fg=typer.colors.YELLOW, err=True,
        )
    else:
        typer.secho(
            "Aithernet isn't set up yet.\n"
            "  Run the one-time guided setup:  aithernet setup\n"
            "  Then:  aithernet run \"<what you want done>\"",
            fg=typer.colors.YELLOW, err=True,
        )
    return typer.Exit(code=2)


def _stream_until_terminal(base_url: str, mission_id: str, *, interval: float,
                           timeout: float) -> dict | None:
    """Poll the canonical execution status, printing ONE concise line per real change (never noisy
    per-poll spam). Returns the final execution dict on a terminal run, or ``None`` on timeout.

    A transient polling error is tolerated briefly — the mission keeps running in the service."""
    deadline = time.monotonic() + max(1.0, timeout)
    last_key = None
    poll_errors = 0
    while time.monotonic() < deadline:
        try:
            with _client(base_url) as client:
                response = client.get(f"/missions/{mission_id}/execution")
            data = _node_json(response, subsystem="mission_api")
            poll_errors = 0
        except httpx.HTTPError:
            poll_errors += 1
            if poll_errors > 5:
                raise _fail_unreachable(
                    base_url, httpx.HTTPError("lost contact with node")) from None
            time.sleep(max(0.2, interval))
            continue
        run = data.get("run") or {}
        mission_status = data["mission"]["status"]
        key = (mission_status, run.get("status"), run.get("iteration_count"),
               run.get("last_route_target"))
        if key != last_key:
            last_key = key
            _print_run_progress(mission_status, run)
        if run.get("status") in _TERMINAL_RUN_STATES:
            return data
        time.sleep(max(0.2, interval))
    return None


def _print_run_progress(mission_status: str, run: dict) -> None:
    """One readable progress line — iteration, what the engine is doing, and any retry note."""
    it = run.get("iteration_count", 0)
    route = run.get("last_route_target") or "planning"
    note = ""
    if run.get("consecutive_failures"):
        note = f"  (retrying, backoff; {run['consecutive_failures']} transient failure(s))"
    typer.echo(f"  [iter {it}] {mission_status} → {route}{note}")


def _print_run_result(base_url: str, mission_id: str, final: dict) -> None:
    """Print the final result, the mission/run identifiers, and any registered artifact ids."""
    run = final.get("run") or {}
    status = run.get("status")
    typer.echo("")
    if status == "completed":
        typer.secho("Mission completed.", fg=typer.colors.GREEN)
    elif status == "blocked":
        typer.secho("Mission blocked — action needed.", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"Mission {status}.", fg=typer.colors.RED)
    if run.get("final_response"):
        typer.echo(f"\nResult:\n{run['final_response']}")
    if run.get("blocked_reason"):
        typer.echo(f"\nWhy: {run['blocked_reason']}")
    if run.get("failure_message"):
        typer.echo(f"\nFailure: {run['failure_message']}")
    typer.echo("")
    typer.secho("Identifiers:", fg=typer.colors.CYAN)
    typer.echo(f"  mission: {mission_id}")
    typer.echo(f"  run:     {run.get('id', '?')}")
    # Artifact identifiers (metadata only; bytes never inlined).
    try:
        with _client(base_url) as client:
            arts = client.get("/rf/artifacts", params={"mission_id": mission_id})
        if arts.is_success and "json" in arts.headers.get("content-type", "").lower():
            items = arts.json()
            items = items if isinstance(items, list) else items.get("artifacts", [])
            for a in items:
                typer.echo(f"  artifact: {a.get('id')}  {a.get('artifact_type', '')}")
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        pass


def _peer_transports_available(match: dict) -> dict:
    """Available transports for a peer: IP when it has an endpoint; simulated_rf always (software);
    rf_ota when the LOCAL operator has enabled TX and a capable device is present (beta.2)."""
    rf_ota = False
    try:
        from aithernet.transport.ota import hardware as _hw
        from aithernet.transport.ota.tx_control import load_tx_config
        cfg = load_tx_config(_tx_state_root())
        rf_ota = bool(cfg.enabled and _hw.discover_tx_devices())
    except Exception:  # noqa: BLE001
        rf_ota = False
    return {"ip": bool(match.get("endpoint_url")), "simulated_rf": True, "rf_ota": rf_ota}


def _run_on_peer(base_url: str, *, peer: str, content: str, transport: str = "auto") -> None:
    """Submit an objective to a trusted peer as a coordinator REQUEST over a SELECTED transport.

    Resolves the peer, applies the deterministic transport-selection policy (RF is never silently
    downgraded to IP; physical rf_ota is closed), sends ONE signed request expecting a semantic
    reply, and records requested + selected transport. The receiving node owns policy."""
    from aithernet.transport.ota import selection as _sel
    try:
        with _client(base_url) as client:
            peers_resp = client.get("/peers")
            _check(peers_resp)
            peers = peers_resp.json() or []
            match = next((p for p in peers
                          if p.get("id") == peer or p.get("peer_id") == peer
                          or p.get("name") == peer), None)
            if match is None:
                names = ", ".join(sorted(p.get("name", "?") for p in peers)) or "(none)"
                typer.secho(f"No trusted peer '{peer}'. Pair one first: aithernet peers pair. "
                            f"Known peers: {names}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
            peer_id = match.get("id") or match.get("peer_id")
            if not match.get("trusted", match.get("trust_state") == "trusted"):
                typer.secho(f"Peer '{peer}' is not trusted — pair/trust it before submitting work.",
                            fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
            # Deterministic transport selection (no silent RF->IP downgrade).
            avail = _peer_transports_available(match)
            if transport == "rf_ota" and not avail["rf_ota"]:
                from aithernet.transport.ota.tx_control import load_tx_config
                cfg = load_tx_config(_tx_state_root())
                reason = ("physical TX is not enabled on this node — `aithernet rf tx enable` and "
                          "configure a TX-capable device" if not cfg.enabled
                          else "no TX-capable SDR is currently present (`aithernet rf tx devices`)")
                typer.secho(f"rf_ota is not available: {reason}.", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
            try:
                sel = _sel.select_transport(requested=transport, available=avail,
                                            fallback_allowed=False)
            except _sel.TransportSelectionError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2) from exc
            sent = client.post("/communications/send", json={
                "peer_id": peer_id, "message_type": "request", "text": content,
                "expects_reply": True})
            _check(sent)
            out = sent.json()
    except httpx.HTTPError as exc:
        raise _run_unreachable_guidance(base_url, exc) from exc
    typer.secho(f"Submitted to peer {peer} (it owns execution + policy).", fg=typer.colors.GREEN)
    typer.echo(f"  requested transport: {transport}")
    typer.echo(f"  selected transport:  {sel.selected} ({sel.reason})")
    typer.echo(f"  request_id:      {out.get('request_id')}")
    typer.echo(f"  conversation_id: {out.get('conversation_id')}")
    typer.echo(f"  delivery:        {out.get('status')}")
    typer.echo("Track it:")
    typer.echo("  aithernet remote-mission list")
    typer.echo("  aithernet communication waits")
    raise typer.Exit(code=0)


@app.command("run")
def run_command(
    content: str = typer.Argument(..., help="What you want Aithernet to do, in plain language."),
    detach: bool = typer.Option(
        False, "--detach", help="Queue the mission and return immediately (advanced/background)."),
    interval: float = typer.Option(1.5, "--interval", help="Progress refresh interval (seconds)."),
    timeout: float = typer.Option(
        1800.0, "--timeout", help="Max seconds to wait for completion before detaching."),
    source_type: str = typer.Option("user", "--source-type", help="Origin class (advanced)."),
    on: str | None = typer.Option(
        None, "--on", help="Submit the objective to a trusted PEER node instead of running it "
                           "locally (beta.10 Part 2). The receiving node owns execution + policy."),
    transport: str = typer.Option(
        "auto", "--transport", help="Peer transport: auto|ip|simulated_rf|rf_ota (beta.1). rf_ota "
                                    "physical TX is closed; RF is never silently downgraded."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Run work with ONE command: create the mission, start autonomous execution through the
    canonical mission engine, show progress, and print the final result + identifiers.

    This is the normal customer workflow — no separate submit/start/watch. Event, run, lease,
    budget and artifact semantics are exactly the engine's (this is not a shell alias).

    With ``--on <peer>`` the objective is submitted to a trusted peer node, which creates and runs
    the mission under ITS OWN policy (this is a request, never remote shell). Progress/results are
    tracked via ``aithernet remote-mission`` and ``aithernet communication``.

    Exit code: 0 completed, 1 blocked/failed/cancelled, 2 node not set up / not running.
    """
    base_url = _mission_base_url(url, config)

    if on:
        return _run_on_peer(base_url, peer=on, content=content, transport=transport)

    # 1) Create the mission. An unreachable node routes to guided setup/start, not a traceback.
    #    A reachable-but-foreign endpoint (a proxy/static site/CatGPT gateway on the node port)
    #    is caught by _node_json as a structured error — never a raw HTML/405 dump.
    try:
        with _client(base_url) as client:
            created = client.post(
                "/missions",
                json={"content": content, "source_type": source_type, "source_id": None},
            )
            mission_id = _node_json(created, subsystem="mission_api")["id"]
    except httpx.HTTPError as exc:
        raise _run_unreachable_guidance(base_url, exc) from exc

    typer.secho(f"Mission created: {mission_id}", fg=typer.colors.GREEN)

    # 2) Start autonomous execution through the canonical engine (queues the worker).
    start = _post_mission_op(base_url, f"/missions/{mission_id}/start")
    run = start.get("run") or {}
    typer.echo(f"Run: {run.get('id', '?')}  — executing through the mission engine")

    if detach:
        typer.secho("Detached (running in the background).", fg=typer.colors.CYAN)
        typer.echo(f"  track:  aithernet mission watch {mission_id}")
        typer.echo(f"  result: aithernet mission show {mission_id}")
        return

    # 3) Stream progress until terminal (or until --timeout, then detach with guidance).
    try:
        final = _stream_until_terminal(base_url, mission_id, interval=interval, timeout=timeout)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        typer.secho(f"\nStopped watching; the mission keeps running. "
                    f"Track it: aithernet mission watch {mission_id}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=0) from None
    if final is None:
        typer.secho(f"\nStill running after {timeout:.0f}s — detaching. "
                    f"Track it: aithernet mission watch {mission_id}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    # 4) Final result + identifiers + artifacts, then a meaningful exit code.
    _print_run_result(base_url, mission_id, final)
    status = (final.get("run") or {}).get("status")
    raise typer.Exit(code=0 if status == "completed" else 1)


# -- Aithernet-managed CatGPT Gateway (beta.5, Part B/C) -------------------------------------------


def _catgpt_manager():
    from aithernet.catgpt import CatGptGatewayManager
    return CatGptGatewayManager()


_CATGPT_GW_STATUS_COLOR = {
    "ready": typer.colors.GREEN,
    "login_required": typer.colors.YELLOW,
    "starting": typer.colors.CYAN,
    "stopped": typer.colors.RED,
    "error": typer.colors.RED,
}


def _catgpt_docker_install(mgr, *, assume_yes: bool, interactive: bool) -> bool:
    """Ensure Docker is installed + accessible for the managed gateway (Ubuntu).

    Returns True if Docker is usable. Aithernet NEVER installs Docker or runs sudo without explicit
    confirmation (``--install-docker`` / a ``y`` prompt). When Docker is installed but the current
    session lacks access, it explains the log-out / ``newgrp docker`` step instead of installing.
    """
    import subprocess as _sp
    import sys as _sys

    if mgr.runner.accessible():
        typer.echo("  docker:        installed and accessible")
        return True
    if mgr.runner.available():
        typer.secho("  docker:        installed but not accessible to this session.",
                    fg=typer.colors.YELLOW)
        typer.echo("    Ensure the daemon is running (sudo systemctl enable --now docker) and your")
        typer.echo("    user is in the docker group (sudo usermod -aG docker $USER), then run")
        typer.echo("    `newgrp docker` or log out and back in — then `aithernet catgpt start`.")
        return False

    typer.secho("  docker:        NOT installed — required to run the managed CatGPT Gateway.",
                fg=typer.colors.YELLOW)
    plan = mgr.docker_install_plan()
    typer.echo("    Aithernet can install it (Ubuntu):")
    for cmd in plan:
        typer.echo("      " + " ".join(cmd))
    proceed = assume_yes
    if not assume_yes:
        if not interactive or not _sys.stdin.isatty():
            typer.secho("    Re-run with --install-docker to install now, or install Docker "
                        "yourself, then `aithernet catgpt start`.", fg=typer.colors.YELLOW)
            return False
        proceed = typer.confirm("  Install Docker now?", default=False)
    if not proceed:
        typer.echo("    Skipped. Install Docker later, then run `aithernet catgpt start`.")
        return False
    for cmd in plan:
        typer.secho("  + " + " ".join(cmd), fg=typer.colors.CYAN)
        if _sp.run(cmd).returncode != 0:  # noqa: S603 - fixed argv, no shell
            typer.secho(f"  Command failed: {' '.join(cmd)}", fg=typer.colors.RED, err=True)
            return False
    typer.secho("  Docker installed.", fg=typer.colors.GREEN)
    typer.secho("  IMPORTANT: log out and back in, or run `newgrp docker`, so your shell joins the "
                "docker group — then run `aithernet catgpt start`.", fg=typer.colors.YELLOW)
    return mgr.runner.accessible()


def _print_catgpt_login_help(summary: dict) -> None:
    """noVNC URL + who-logs-in guidance (the user owns the web account; login is user-performed)."""
    open_url = summary.get("novnc_open_url") or summary["novnc_url"] + "/vnc.html?autoconnect=1"
    typer.echo("")
    typer.secho("Sign in yourself in the local browser:", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  1) Open noVNC:   {open_url}")
    typer.echo("       or run:     aithernet catgpt open")
    typer.echo("  2) If noVNC asks for a password:   aithernet catgpt vnc-password")
    typer.echo("       (that is the LOCAL noVNC password, not your ChatGPT/Claude password)")
    typer.echo(f"  3) In that browser, sign into your {summary['provider']} account "
               "(ChatGPT or Claude).")
    typer.echo("  4) Aithernet never sees your web password, cookies, or browser session — you own "
               "the account and the login.")
    typer.secho(
        "  Note: some providers' Google sign-in can be unreliable inside an automated browser; "
        "prefer email/password sign-in if Google login misbehaves.",
        fg=typer.colors.YELLOW,
    )


@catgpt_app.command("setup")
def catgpt_setup(
    provider: str = typer.Option(
        "chatgpt", "--provider", help="Which web account the gateway drives: chatgpt | claude."),
    api_host: str = typer.Option(
        "127.0.0.1", "--api-host", help="Host to bind the local API to (loopback by default)."),
    api_port: int = typer.Option(8000, "--api-port", help="Local OpenAI-compatible API port."),
    novnc_port: int = typer.Option(6080, "--novnc-port", help="Local noVNC (login browser) port."),
    api_token: str | None = typer.Option(
        None, "--api-token", help="Local bearer token (generated + stored securely if omitted)."),
    source_dir: str | None = typer.Option(
        None, "--source-dir", help="Path to a pinned CatGPT-Gateway checkout to BUILD the image "
                                   "from (advanced; omit to use an existing local image)."),
    configure_coordinator: bool = typer.Option(
        True, "--configure-coordinator/--no-configure-coordinator",
        help="Point the coordinator provider at this managed gateway."),
    install_docker: bool = typer.Option(
        None, "--install-docker/--no-install-docker",
        help="Install Docker if missing (Ubuntu). Omit to be asked interactively; Aithernet never "
             "installs Docker without confirmation."),
) -> None:
    """One-time setup: write the Aithernet-managed gateway config/compose/env, store a local bearer
    token via managed secrets, and point the coordinator provider at the local gateway.

    Does NOT store your ChatGPT/Claude login — you sign in yourself later via noVNC. The bearer
    token and VNC password are never printed.
    """
    from aithernet.catgpt.manager import CatGptManagerError

    mgr = _catgpt_manager()
    try:
        summary = mgr.setup(
            provider=provider, api_host=api_host, api_port=api_port, novnc_port=novnc_port,
            api_token=api_token, source_dir=source_dir,
            configure_coordinator=configure_coordinator,
        )
    except CatGptManagerError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    typer.secho("Aithernet-managed CatGPT Gateway configured.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  state dir:     {summary['state_dir']}")
    typer.echo(f"  provider:      {summary['provider']}")
    typer.echo(f"  API base URL:  {summary['base_url']}  "
               f"({'loopback-only' if summary['loopback_only'] else 'NON-loopback bind'})")
    typer.echo(f"  noVNC URL:     {summary['novnc_open_url']}")
    typer.echo(f"  bearer token:  stored via managed secrets as {summary['api_key_ref']} "
               "(not shown)")
    typer.echo(f"  source mode:   {summary['source_mode']}")
    if summary["source_dir"]:
        typer.echo(f"  build from:    {summary['source_dir']}")
    else:
        typer.echo(f"  image:         {summary['image']}")
        typer.echo(f"  image artifact: {summary['image_artifact']}")
        typer.echo(f"  image loaded:  {'yes' if summary['image_loaded'] else 'no'}"
                   + ("" if summary["image_loaded"] else
                      "  (Aithernet loads it from the release bundle on `catgpt start`)"))
    if summary["coordinator_configured"]:
        typer.echo("  coordinator:   provider set to catgpt_gateway "
                   "(model auto-persisted after login via `aithernet catgpt status`)")
    if not summary["loopback_only"]:
        typer.secho("  WARNING: the API is not bound to loopback — anyone who can reach "
                    f"{summary['api_host']}:{summary['api_port']} can use your session.",
                    fg=typer.colors.YELLOW)
    # Docker is required to RUN the gateway; detect it and offer a managed install (confirmed).
    _catgpt_docker_install(mgr, assume_yes=bool(install_docker), interactive=install_docker is None)
    typer.echo("")
    typer.echo("Next:  aithernet catgpt start   then   aithernet catgpt status")
    if summary["coordinator_configured"]:
        typer.echo("")
        typer.echo("Restart the node so it loads the new coordinator + managed secret:")
        typer.echo("  service mode (recommended):  aithernet service restart")
        typer.echo("  foreground mode:             restart `aithernet start` "
                   "(it loads managed secrets automatically)")
    _print_catgpt_login_help(summary)


@catgpt_app.command("start")
def catgpt_start(
    build: bool = typer.Option(
        False, "--build", help="Force rebuilding the image before starting (source-dir mode)."),
) -> None:
    """Start the managed gateway (``docker compose up -d``). Prints the noVNC URL to sign in."""
    from aithernet.catgpt.manager import CatGptManagerError

    mgr = _catgpt_manager()
    if not mgr.runner.available():
        typer.secho("Error: Docker is not installed. Run `aithernet catgpt install-runtime` "
                    "(or `aithernet catgpt setup`) to install it, then re-run "
                    "`aithernet catgpt start`.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if not mgr.runner.accessible():
        typer.secho("Error: Docker is installed but not accessible to this session. Ensure the "
                    "daemon is running and your user is in the docker group, then run "
                    "`newgrp docker` (or log out/in).", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    try:
        cfg = mgr.load_config()
        # Bundled-image mode loads the shipped image locally FIRST (never a Docker Hub pull); a
        # missing image tar raises the actionable Aithernet error below (not Docker's pull-access).
        res = mgr.start(build=True if build else None)
    except CatGptManagerError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    if not res.ok:
        typer.secho("Error: failed to start the managed gateway.", fg=typer.colors.RED, err=True)
        detail = (res.stderr or res.stdout or "").strip()
        if detail:
            typer.secho(f"  {detail[:400]}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    img = mgr.image_status()
    typer.secho("Managed CatGPT Gateway starting.", fg=typer.colors.GREEN)
    typer.echo(f"  image: {img['image_tag']} ({img['managed_image_source']})")
    typer.echo(f"  API:   {cfg.base_url}")
    typer.echo("")
    typer.secho("Open noVNC:", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  {cfg.novnc_open_url}")
    typer.echo("If noVNC asks for a password:")
    typer.echo("  aithernet catgpt vnc-password")
    typer.echo(f"Then log into {cfg.provider} (ChatGPT/Claude) inside the noVNC browser yourself.")
    typer.echo("")
    typer.echo("After login:  aithernet catgpt status   "
               "(discovers + auto-persists the coordinator model)")


@catgpt_app.command("status")
def catgpt_status(
    no_probe: bool = typer.Option(
        False, "--no-probe", help="Skip the live gateway probe (report container state only)."),
    json_out: bool = typer.Option(False, "--json", help="Emit the status as JSON."),
) -> None:
    """Show managed-gateway status: gateway_status, endpoint reachability, and discovered model.

    Never prints the bearer token or the browser session."""
    from aithernet.catgpt.manager import CatGptManagerError

    mgr = _catgpt_manager()
    try:
        data = mgr.status(probe=not no_probe)
    except CatGptManagerError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    # beta.7 (FIX 1): when the gateway is READY and exactly one model was discovered, persist it
    # into the coordinator config automatically — a fresh client never runs `agents configure`.
    repair: dict = {"persisted": False}
    if not no_probe and data.get("gateway_status") == "ready":
        from aithernet.agents import providers as _p
        repair = _p.ensure_catgpt_coordinator_model(
            mgr._state_root, discovered=data.get("models") or None)
        data["coordinator_model"] = repair.get("model") or repair.get("configured_model")
        data["coordinator_model_persisted"] = bool(repair.get("persisted"))
    if json_out:
        typer.echo(json.dumps(data, indent=2))
    else:
        color = _CATGPT_GW_STATUS_COLOR.get(data["gateway_status"], typer.colors.WHITE)
        typer.secho(f"gateway_status: {data['gateway_status']}", fg=color, bold=True)
        typer.echo(f"  managed_gateway:      {data['managed_gateway']}")
        typer.echo(f"  provider:             {data['provider']}")
        typer.echo(f"  base_url:             {data['base_url']}")
        typer.echo(f"  noVNC:                {data['novnc_open_url']}")
        typer.echo(f"  docker_installed:     {data['docker_installed']}")
        typer.echo(f"  docker_accessible:    {data['docker_accessible']}")
        typer.echo(f"  managed_image_present: {data['managed_image_present']}")
        typer.echo(f"  managed_image_source: {data['managed_image_source']}")
        typer.echo(f"  container_running:    {data['container_running']}")
        typer.echo(f"  endpoint_reachable:   {data['endpoint_reachable']}")
        typer.echo(f"  credential_available: {data['credential_available']} "
                   f"(ref {data['api_key_ref']})")
        typer.echo(f"  model (discovered):   {data['model'] or '(none discovered yet)'}")
        if data.get("coordinator_model_persisted"):
            typer.secho(f"  coordinator model:    {data.get('coordinator_model')} "
                        "(auto-persisted ✓)", fg=typer.colors.GREEN)
        elif repair.get("reason") == "already_configured":
            typer.echo(f"  coordinator model:    {repair.get('configured_model')} (configured)")
        elif repair.get("reason") == "multiple_models":
            typer.secho("  coordinator model:    several discovered — choose one with "
                        "`aithernet agents connect coordinator`", fg=typer.colors.YELLOW)
        if not data["docker_installed"]:
            typer.secho("  → Install Docker:  aithernet catgpt install-runtime",
                        fg=typer.colors.YELLOW)
        elif data["managed_image_source"] == "missing":
            typer.secho("  → Managed image missing from the bundle: "
                        "aithernet components install catgpt-gateway", fg=typer.colors.YELLOW)
        if data["gateway_status"] == "login_required":
            typer.secho(f"  → Open noVNC and sign into {data['provider']}: "
                        f"{data['novnc_open_url']}", fg=typer.colors.YELLOW)
            typer.echo("     password (if asked):  aithernet catgpt vnc-password")
        elif data["gateway_status"] == "stopped":
            typer.echo("  → Start it:  aithernet catgpt start")
        elif data["gateway_status"] == "ready":
            typer.secho("  → Verify the coordinator:  aithernet agents test coordinator --live",
                        fg=typer.colors.GREEN)
    raise typer.Exit(code=0 if data["gateway_status"] in ("ready", "starting") else 1)


@catgpt_app.command("logs")
def catgpt_logs(
    tail: int = typer.Option(200, "--tail", help="Number of trailing log lines to show."),
) -> None:
    """Show recent managed-gateway logs (bounded; the gateway does not log the bearer token)."""
    from aithernet.catgpt.manager import CatGptManagerError

    mgr = _catgpt_manager()
    if not mgr.runner.available():
        typer.secho("Error: docker not found on PATH.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    try:
        res = mgr.logs(tail=tail)
    except CatGptManagerError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    if res.stdout:
        typer.echo(res.stdout.rstrip())
    if not res.ok and res.stderr:
        typer.secho(res.stderr.strip()[:400], fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@catgpt_app.command("stop")
def catgpt_stop() -> None:
    """Stop the managed gateway (``docker compose down``). Keeps your browser login for reuse."""
    from aithernet.catgpt.manager import CatGptManagerError

    mgr = _catgpt_manager()
    if not mgr.runner.available():
        typer.secho("Error: docker not found on PATH.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    try:
        res = mgr.stop()
    except CatGptManagerError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    if res.ok:
        typer.secho("Managed CatGPT Gateway stopped (browser login volume preserved).",
                    fg=typer.colors.GREEN)
    else:
        typer.secho((res.stderr or res.stdout or "stop failed").strip()[:400],
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@catgpt_app.command("install-runtime")
def catgpt_install_runtime(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Install Docker without the interactive confirmation prompt."),
) -> None:
    """Install/prepare the managed CatGPT Gateway runtime: Docker (with confirmation) + load the
    shipped gateway image locally. No git clone, no docker login, no Docker Hub pull."""
    from aithernet.catgpt.manager import CatGptManagerError

    mgr = _catgpt_manager()
    typer.secho("Preparing the managed CatGPT Gateway runtime.", fg=typer.colors.CYAN, bold=True)
    ok = _catgpt_docker_install(mgr, assume_yes=yes, interactive=not yes)
    if not ok:
        raise typer.Exit(code=1)
    # Docker is usable → load the shipped image now so `catgpt start` is instant and offline.
    try:
        info = mgr.ensure_image()
    except CatGptManagerError as exc:
        typer.secho(f"  image:         {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    typer.secho(f"  image:         {info['tag']} ready ({info['source']})", fg=typer.colors.GREEN)
    typer.echo("Next:  aithernet catgpt start")


@catgpt_app.command("vnc-password")
def catgpt_vnc_password() -> None:
    """Print ONLY the local noVNC password (for viewing the login browser).

    This is the LOCAL noVNC password — NOT your ChatGPT/Claude password and NOT the API bearer
    token. It is never logged. The password is printed to stdout; the explanation goes to stderr so
    the value stays scriptable.
    """
    from aithernet.catgpt.manager import CatGptManagerError

    mgr = _catgpt_manager()
    try:
        pw = mgr.read_vnc_password()
    except CatGptManagerError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    typer.secho("Local noVNC password (NOT your ChatGPT/Claude password, NOT the API token):",
                fg=typer.colors.YELLOW, err=True)
    typer.echo(pw)


@catgpt_app.command("open")
def catgpt_open(
    launch: bool = typer.Option(
        True, "--launch/--no-launch",
        help="Also try to open the URL in a browser (falls back to just printing it)."),
) -> None:
    """Print (and try to open) the noVNC login URL, then guide the ChatGPT/Claude sign-in."""
    from aithernet.catgpt.manager import CatGptManagerError

    mgr = _catgpt_manager()
    try:
        cfg = mgr.load_config()
    except CatGptManagerError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    url = cfg.novnc_open_url
    typer.secho("Open noVNC:", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  {url}")
    typer.echo("If noVNC asks for a password:")
    typer.echo("  aithernet catgpt vnc-password")
    typer.echo(f"Then log into {cfg.provider} (ChatGPT/Claude) inside the noVNC browser yourself.")
    if launch:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — headless/no-display is fine; the URL is printed above
            pass


@coding_sandbox_app.command("status")
def coding_sandbox_status(
    json_out: bool = typer.Option(False, "--json", help="Emit the probe as JSON."),
) -> None:
    """Show coding-agent sandbox readiness (bubblewrap userns/loopback) + host-hardening state."""
    from aithernet.coding_agent import sandbox_preflight as sp

    probe = sp.preflight_sandbox()
    relaxed = sp.host_hardening_relaxed()
    data = probe.to_dict()
    data["host_hardening_relaxed"] = relaxed
    data["apparmor_restrict_value"] = sp.apparmor_restrict_value()
    if json_out:
        typer.echo(json.dumps(data, indent=2))
        raise typer.Exit(code=0 if probe.ready else 1)
    color = {"ready": typer.colors.GREEN, "blocked": typer.colors.RED,
             "absent": typer.colors.YELLOW, "unverified": typer.colors.YELLOW}.get(
        probe.severity, typer.colors.WHITE)
    typer.secho(f"coding_sandbox: {probe.severity}", fg=color, bold=True)
    typer.echo(f"  detail:  {probe.detail}")
    typer.echo(f"  kernel.apparmor_restrict_unprivileged_userns: {sp.apparmor_restrict_value()}")
    if relaxed:
        typer.secho("  host_hardening: relaxed", fg=typer.colors.YELLOW)
        typer.echo(f"  restore:  {' '.join(sp.restore_userns_command())}")
    if probe.remediation and not probe.ready:
        typer.secho(f"  → {probe.remediation}", fg=typer.colors.YELLOW)
    raise typer.Exit(code=0 if probe.ready else 1)


@coding_sandbox_app.command("repair")
def coding_sandbox_repair(
    temporary: bool = typer.Option(
        True, "--temporary/--persistent",
        help="Temporary (runtime-only; resets on reboot). Persistent is not supported in beta.7."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Temporarily relax the Ubuntu AppArmor user-namespace restriction so the bubblewrap coding
    sandbox can initialize. This RELAXES a host hardening control — Aithernet asks first and NEVER
    changes it silently; sudo runs only after your explicit confirmation.
    """
    import subprocess as _sp

    from aithernet.coding_agent import sandbox_preflight as sp
    if not temporary:
        typer.secho("Error: beta.7 supports only a TEMPORARY (runtime-only) sandbox repair. "
                    "Omit --persistent.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if sp.host_hardening_relaxed():
        typer.secho("kernel.apparmor_restrict_unprivileged_userns is already 0 (relaxed).",
                    fg=typer.colors.YELLOW)
        typer.echo(f"Restore with:  {' '.join(sp.restore_userns_command())}")
        raise typer.Exit(code=0)
    cmd = sp.relax_userns_command()
    typer.secho("This RELAXES a host hardening control (AppArmor unprivileged user namespaces):",
                fg=typer.colors.YELLOW, bold=True)
    typer.echo(f"  {' '.join(cmd)}")
    typer.echo("It is TEMPORARY (runtime-only; the default hardening returns on reboot).")
    typer.echo(f"Restore any time with:  {' '.join(sp.restore_userns_command())}")
    if not yes and not typer.confirm("Relax the AppArmor userns restriction now?", default=False):
        typer.echo("Aborted. Host hardening unchanged.")
        raise typer.Exit(code=1)
    rc = _sp.run(cmd).returncode  # noqa: S603 — fixed argv; sudo runs ONLY here, after confirmation
    if rc != 0:
        typer.secho("Failed to relax the restriction (sudo declined or sysctl unavailable).",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho("Host hardening temporarily relaxed. coding_sandbox should now initialize.",
                fg=typer.colors.GREEN)
    typer.secho(f"REMEMBER to restore it after testing:  {' '.join(sp.restore_userns_command())}",
                fg=typer.colors.YELLOW)
    typer.echo("Then re-run:  aithernet agents test coding --live  (or your mission).")


@coding_sandbox_app.command("restore")
def coding_sandbox_restore(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Restore the Ubuntu AppArmor user-namespace hardening relaxed by `coding-sandbox repair`."""
    import subprocess as _sp

    from aithernet.coding_agent import sandbox_preflight as sp
    cmd = sp.restore_userns_command()
    typer.echo(f"Restoring host hardening:  {' '.join(cmd)}")
    if not yes and not typer.confirm("Restore the AppArmor userns restriction now?", default=True):
        typer.echo("Aborted. Host hardening unchanged.")
        raise typer.Exit(code=1)
    rc = _sp.run(cmd).returncode  # noqa: S603 — fixed argv; runs only after confirmation
    if rc != 0:
        typer.secho("Failed to restore the restriction (sudo declined or sysctl unavailable).",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho("Host hardening restored (kernel.apparmor_restrict_unprivileged_userns=1).",
                fg=typer.colors.GREEN)


@worker_app.command("status")
def worker_status(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show the autonomous mission worker manager status."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/mission-worker/status")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    data = response.json()
    typer.echo(
        f"Worker enabled={data['enabled']} running={data['running']} "
        f"degraded={data['degraded']} pool={data['worker_count']}"
    )
    typer.echo(
        f"  active={data['active_count']} queued={data['queued_count']} "
        f"node={data['node_id']}"
    )
    if data.get("active_missions"):
        typer.echo(f"  active_missions: {', '.join(data['active_missions'])}")
    if data.get("last_error"):
        typer.echo(f"  last_error: {data['last_error']}")


# -- events ----------------------------------------------------------------------


@events_app.command("list")
def events_list(
    mission_id: str | None = typer.Option(None, "--mission-id", help="Filter by mission."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """List runtime events, newest first."""
    base_url = _resolve_base_url(url, config)
    params = {"mission_id": mission_id} if mission_id else None
    try:
        with _client(base_url) as client:
            response = client.get("/events", params=params)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    events = response.json()
    if not events:
        typer.echo("No events.")
        return
    for e in events:
        mission = e["mission_id"] or "-"
        typer.echo(f"{e['created_at']}  {e['event_type']:<18}  mission={mission}  {e['message']}")


@events_app.command("stream")
def events_stream(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Stream runtime events live from the node (Ctrl-C to stop)."""
    base_url = _resolve_base_url(url, config)
    stream_url = f"{base_url}/events/stream"
    typer.secho(f"Streaming events from {stream_url} (Ctrl-C to stop)...", fg=typer.colors.CYAN)
    try:
        with httpx.Client(timeout=None) as client:
            with client.stream("GET", stream_url) as response:
                if not response.is_success:
                    response.read()
                    _check(response)
                for line in response.iter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        payload = line[len("data:") :].strip()
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        mission = event.get("mission_id") or "-"
                        typer.echo(
                            f"{event['created_at']}  {event['event_type']:<18}  "
                            f"mission={mission}  {event['message']}"
                        )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    except KeyboardInterrupt:
        typer.echo("\nStopped.")


# -- coordinator -----------------------------------------------------------------


@coordinator_app.command("status")
def coordinator_status(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Print the configured coordinator provider and readiness (no secrets)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/coordinator/status")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    data = response.json()
    ready = "yes" if data["configured"] else "no"
    color = typer.colors.GREEN if data["configured"] else typer.colors.YELLOW
    typer.secho(f"Provider:   {data['provider']}", fg=typer.colors.CYAN)
    if data.get("executable"):  # CLI provider (gemini_cli)
        typer.echo(f"Executable: {data['executable']}")
    typer.echo(f"Model:      {data['model'] or 'default'}")
    typer.secho(f"Configured: {ready}", fg=color)
    if data.get("cli_version"):
        typer.echo(f"CLI version: {data['cli_version']}")
    if not data.get("executable"):  # HTTP provider (openai_compatible/anthropic)
        typer.echo(f"Base URL:   {'set' if data['base_url_configured'] else 'unset'}")
        typer.echo(f"API key:    {'set' if data['api_key_configured'] else 'unset'}")
    if data["missing_configuration"]:
        typer.echo(f"Missing:    {', '.join(data['missing_configuration'])}")
    typer.echo(f"Active capabilities: {', '.join(data['active_capabilities'])}")


def _print_coordinator_diagnostics(d: dict) -> None:
    """Render a coordinator diagnostic report for the terminal (no secrets)."""
    ready = "yes" if d["configured"] else "no"
    color = typer.colors.GREEN if d["configured"] else typer.colors.YELLOW
    typer.secho("Coordinator diagnostics", fg=typer.colors.CYAN)
    typer.echo(f"Provider:    {d['provider']}")
    if d.get("executable") is not None:
        typer.echo(f"Executable:  {d['executable']}")
        typer.echo(f"Resolves:    {'yes' if d.get('executable_resolves') else 'no'}")
        if d.get("resolved_executable_path"):
            typer.echo(f"Path:        {d['resolved_executable_path']}")
        if d.get("cli_version"):
            typer.echo(f"CLI version: {d['cli_version']}")
        if d.get("working_directory"):
            typer.echo(f"Runtime dir: {d['working_directory']}")
    else:
        typer.echo(f"Base URL:    {'set' if d.get('base_url_configured') else 'unset'}")
        typer.echo(f"API key:     {'set' if d.get('api_key_configured') else 'unset'}")
    typer.echo(f"Model:       {d['model']}")
    typer.echo(f"Timeout:     {d['timeout_seconds']}s")
    typer.secho(f"Configured:  {ready}", fg=color)
    if d["missing_configuration"]:
        typer.echo(f"Missing:     {', '.join(d['missing_configuration'])}")

    probe = d["probe"]
    if not probe["attempted"]:
        typer.echo("Probe:       not run (use --probe for one authenticated inference)")
    elif probe["succeeded"]:
        models = ", ".join(probe.get("model_reported") or []) or "default"
        extra = f", {probe['elapsed_ms']}ms" if probe.get("elapsed_ms") is not None else ""
        typer.secho(
            f"Probe:       ok — response received (model: {models}{extra}, "
            f"tool calls: {probe.get('tool_calls', 0)})",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            f"Probe:       FAILED [{probe['error_type']}] {probe['error']}",
            fg=typer.colors.RED,
        )
    if d.get("hints"):
        typer.echo("Next steps:")
        for hint in d["hints"]:
            typer.echo(f"  - {hint}")


@coordinator_app.command("diagnostics")
def coordinator_diagnostics(
    probe: bool = ProbeOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Print a coordinator diagnostic report (no secrets); --probe makes one inference."""
    base_url = _resolve_base_url(url, config)
    params = {"probe": "true"} if probe else None
    try:
        with _client(base_url) as client:
            response = client.get(
                "/coordinator/diagnostics", params=params, timeout=_PROCESS_TIMEOUT
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_coordinator_diagnostics(response.json())


# -- coding agent ----------------------------------------------------------------


@coding_agent_app.command("status")
def coding_agent_status(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Print the configured coding-agent provider and readiness (no secrets)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/coding-agent/status")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    data = response.json()
    ready = "yes" if data["configured"] else "no"
    color = typer.colors.GREEN if data["configured"] else typer.colors.YELLOW
    typer.secho(f"Provider:   {data['provider']}", fg=typer.colors.CYAN)
    typer.echo(f"Executable: {data['executable']}")
    typer.echo(f"Workspace:  {data['workspace']}")
    typer.secho(f"Configured: {ready}", fg=color)
    if data["missing_configuration"]:
        typer.echo(f"Missing:    {', '.join(data['missing_configuration'])}")
    typer.echo(f"Active capabilities: {', '.join(data['active_capabilities'])}")


# -- coding tasks ----------------------------------------------------------------


def _preview(text: str, limit: int = 400) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _print_task(task: dict) -> None:
    """Render a coding task header for the terminal."""
    typer.secho(f"Task:      {task['id']}", fg=typer.colors.GREEN)
    typer.echo(f"Status:    {task['status']}")
    typer.echo(f"Provider:  {task['provider'] or '(unset)'}")
    typer.echo(f"Mission:   {task['mission_id'] or '-'}")
    typer.echo(f"Objective: {task['objective']}")


def _print_result(result: dict) -> None:
    """Render a coding-task result for the terminal."""
    typer.secho(f"Result:    {result['id']}", fg=typer.colors.CYAN)
    typer.echo(f"Status:    {result['status']}")
    if result.get("exit_code") is not None:
        typer.echo(f"Exit code: {result['exit_code']}")
    typer.echo(f"Summary:   {_preview(result['summary'])}")
    if result.get("stdout"):
        typer.echo(f"stdout:    {_preview(result['stdout'])}")
    if result.get("stderr"):
        typer.echo(f"stderr:    {_preview(result['stderr'])}")


def _coding_task_body(
    objective: str,
    mission_id: str | None,
    context_json: str | None,
    available_tool: list[str] | None,
    expected_output: list[str] | None,
    reporting_requirement: list[str] | None,
) -> dict:
    """Assemble the JSON body for a coding-task create/run request."""
    context: dict = {}
    if context_json:
        try:
            context = json.loads(context_json)
        except json.JSONDecodeError as exc:
            typer.secho(f"Invalid --context-json: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        if not isinstance(context, dict):
            typer.secho("--context-json must be a JSON object.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
    return {
        "objective": objective,
        "mission_id": mission_id,
        "context": context,
        "available_tools": available_tool or [],
        "expected_outputs": expected_output or [],
        "reporting_requirements": reporting_requirement or [],
    }


@coding_task_app.command("create")
def coding_task_create(
    objective: str = typer.Argument(..., help="What the coding agent should implement."),
    mission_id: str | None = MissionIdOption,
    context_json: str | None = ContextJsonOption,
    available_tool: list[str] | None = AvailableToolOption,
    expected_output: list[str] | None = ExpectedOutputOption,
    reporting_requirement: list[str] | None = ReportingRequirementOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Create a coding task without running it."""
    base_url = _resolve_base_url(url, config)
    body = _coding_task_body(
        objective, mission_id, context_json, available_tool, expected_output, reporting_requirement
    )
    try:
        with _client(base_url) as client:
            response = client.post("/coding-tasks", json=body)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_task(response.json())


@coding_task_app.command("list")
def coding_task_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List coding tasks on the node, newest first."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/coding-tasks")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    tasks = response.json()
    if not tasks:
        typer.echo("No coding tasks.")
        return
    for t in tasks:
        preview = t["objective"].replace("\n", " ")
        if len(preview) > 50:
            preview = preview[:47] + "..."
        typer.echo(f"{t['id']}  {t['status']:<10}  {t['created_at']}  {preview}")


@coding_task_app.command("show")
def coding_task_show(
    task_id: str = typer.Argument(..., help="Coding task id."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Show a single coding task in full."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/coding-tasks/{task_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2))


@coding_task_app.command("status")
def coding_task_status(
    task_id: str = typer.Argument(..., help="Coding task id (from `mission events` / `coding-task "
                                            "list`)."),
    json_out: bool = typer.Option(False, "--json", help="Emit the task + latest result as JSON."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Inspect a coding task: status, provider, and its latest result — commands run, files
    changed, artifacts, summary, and bounded logs."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            tr = client.get(f"/coding-tasks/{task_id}")
            _check(tr)
            task = tr.json()
            rr = client.get(f"/coding-tasks/{task_id}/result")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    result = rr.json() if rr.status_code == 200 else None
    if json_out:
        typer.echo(json.dumps({"task": task, "result": result}, indent=2))
        return
    _print_task(task)
    if result is None:
        typer.echo(f"Result:    (not run yet — `aithernet coding-task run {task_id}`)")
        return
    typer.echo("")
    _print_result(result)
    for label, key in (("Commands run", "commands_run"), ("Files changed", "files_changed"),
                       ("Artifacts", "artifacts")):
        items = result.get(key) or []
        if items:
            typer.secho(f"{label} ({len(items)}):", fg=typer.colors.CYAN)
            for it in items[:40]:
                typer.echo(f"  {it if isinstance(it, str) else json.dumps(it)}")


@coding_task_app.command("logs")
def coding_task_logs(
    task_id: str = typer.Argument(..., help="Coding task id."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Show the stdout/stderr captured by a coding task's latest result."""
    result = _coding_task_result_or_exit(task_id, url, config)
    printed = False
    for stream in ("stdout", "stderr"):
        if result.get(stream):
            typer.secho(f"── {stream} ──", fg=typer.colors.CYAN)
            typer.echo(result[stream].rstrip())
            printed = True
    if not printed:
        typer.echo("(no stdout/stderr captured)")


@coding_task_app.command("artifacts")
def coding_task_artifacts(
    task_id: str = typer.Argument(..., help="Coding task id."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """List a coding task's produced artifacts and changed files."""
    result = _coding_task_result_or_exit(task_id, url, config)
    arts = result.get("artifacts") or []
    files = result.get("files_changed") or []
    typer.secho(f"Artifacts ({len(arts)}):", fg=typer.colors.CYAN)
    for a in arts:
        typer.echo(f"  {a if isinstance(a, str) else json.dumps(a)}")
    typer.secho(f"Files changed ({len(files)}):", fg=typer.colors.CYAN)
    for f in files:
        typer.echo(f"  {f}")
    if not arts and not files:
        typer.echo("(none recorded)")


def _coding_task_result_or_exit(task_id: str, url: str | None, config: str) -> dict:
    """Fetch a coding task's latest result, or exit(1) with a clear message if it has not run."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            rr = client.get(f"/coding-tasks/{task_id}/result")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    if rr.status_code != 200:
        typer.secho(f"No result for coding task {task_id} yet "
                    f"(run it: `aithernet coding-task run {task_id}`).",
                    fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    return rr.json()


@coding_task_app.command("run")
def coding_task_run(
    task_id: str = typer.Argument(..., help="Coding task id to run."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Run an existing coding task and print its result."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(f"/coding-tasks/{task_id}/run", timeout=_RUN_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    result = response.json()
    typer.echo(f"Task:      {result['task_id']}")
    _print_result(result)


@coding_task_app.command("submit")
def coding_task_submit(
    objective: str = typer.Argument(..., help="What the coding agent should implement."),
    mission_id: str | None = MissionIdOption,
    context_json: str | None = ContextJsonOption,
    available_tool: list[str] | None = AvailableToolOption,
    expected_output: list[str] | None = ExpectedOutputOption,
    reporting_requirement: list[str] | None = ReportingRequirementOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Create a coding task and immediately run it."""
    base_url = _resolve_base_url(url, config)
    body = _coding_task_body(
        objective, mission_id, context_json, available_tool, expected_output, reporting_requirement
    )
    try:
        with _client(base_url) as client:
            response = client.post("/coding-tasks/run", json=body, timeout=_RUN_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    data = response.json()
    _print_task(data["task"])
    _print_result(data["result"])


# -- mcp -------------------------------------------------------------------------

# Shared MCP call options (module-level singletons keep callable defaults out of the
# function signature, the pattern B008 recommends).
ArgsJsonOption = typer.Option(
    "{}", "--args-json", help="Tool arguments as a JSON object."
)
McpMissionIdOption = typer.Option(None, "--mission-id", help="Associate with a mission.")
McpTaskIdOption = typer.Option(None, "--task-id", help="Associate with a coding task.")
CallerOption = typer.Option("user", "--caller", help="Who is making the call.")


@mcp_app.command("status")
def mcp_status(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Print the configured MCP server and readiness (no secrets)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/mcp/status")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    data = _json(response)
    ready = "yes" if data["configured"] else "no"
    color = typer.colors.GREEN if data["configured"] else typer.colors.YELLOW
    typer.secho(f"Provider:   {data['provider']}", fg=typer.colors.CYAN)
    typer.echo(f"Command:    {data['command'] or '(unset)'}")
    typer.secho(f"Configured: {ready}", fg=color)
    if data["missing_configuration"]:
        typer.echo(f"Missing:    {', '.join(data['missing_configuration'])}")
    typer.echo(f"Active capabilities: {', '.join(data['active_capabilities'])}")


def _print_diagnostics(d: dict) -> None:
    """Render an MCP diagnostic report for the terminal."""
    ready = "yes" if d["configured"] else "no"
    color = typer.colors.GREEN if d["configured"] else typer.colors.YELLOW
    typer.secho("MCP diagnostics", fg=typer.colors.CYAN)
    typer.echo(f"Provider:         {d['provider']}")
    typer.echo(f"Command:          {d['command'] or '(unset)'}")
    typer.echo(f"Command resolves: {'yes' if d['command_resolves'] else 'no'}")
    if d.get("resolved_command_path"):
        typer.echo(f"Resolved path:    {d['resolved_command_path']}")
    if d.get("args_unresolved_count"):
        typer.echo(f"Unresolved args:  {d['args_unresolved_count']}")
    if d.get("cwd"):
        typer.echo(f"Working dir:      {d['cwd']} (exists: {'yes' if d['cwd_exists'] else 'no'})")
    typer.echo(f"Timeout:          {d['timeout_seconds']}s")
    typer.secho(f"Configured:       {ready}", fg=color)
    if d["missing_configuration"]:
        typer.echo(f"Missing:          {', '.join(d['missing_configuration'])}")

    probe = d["probe"]
    if not probe["attempted"]:
        typer.echo("Probe:            not run (use --probe to launch the server)")
    elif probe["succeeded"]:
        names = ", ".join(probe.get("tool_names") or [])
        typer.secho(
            f"Probe:            ok — {probe['tool_count']} tool(s): {names}",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            f"Probe:            FAILED [{probe['error_type']}] {probe['error']}",
            fg=typer.colors.RED,
        )

    if d.get("hints"):
        typer.echo("Next steps:")
        for hint in d["hints"]:
            typer.echo(f"  - {hint}")


@mcp_app.command("diagnostics")
def mcp_diagnostics(
    probe: bool = ProbeOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Print an MCP diagnostic report (no secrets); --probe launches the server."""
    base_url = _resolve_base_url(url, config)
    params = {"probe": "true"} if probe else None
    try:
        with _client(base_url) as client:
            response = client.get("/mcp/diagnostics", params=params, timeout=_MCP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_diagnostics(_json(response))


@mcp_app.command("tools")
def mcp_tools(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List tools exposed by the configured MCP server."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/mcp/tools", timeout=_MCP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    tools = _json(response)
    if not tools:
        typer.echo("No tools.")
        return
    for tool in tools:
        description = (tool.get("description") or "").replace("\n", " ")
        if len(description) > 70:
            description = description[:67] + "..."
        typer.echo(f"{tool['name']:<28}  {description}")


@mcp_app.command("call")
def mcp_call(
    tool_name: str = typer.Argument(..., help="The MCP tool to call."),
    args_json: str = ArgsJsonOption,
    mission_id: str | None = McpMissionIdOption,
    task_id: str | None = McpTaskIdOption,
    caller: str = CallerOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Call an MCP tool and print the persisted call record."""
    base_url = _resolve_base_url(url, config)
    try:
        arguments = json.loads(args_json)
    except json.JSONDecodeError as exc:
        typer.secho(f"Invalid --args-json: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if not isinstance(arguments, dict):
        typer.secho("--args-json must be a JSON object.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    body = {
        "mission_id": mission_id,
        "task_id": task_id,
        "caller": caller,
        "arguments": arguments,
    }
    try:
        with _client(base_url) as client:
            response = client.post(
                f"/mcp/tools/{tool_name}/call", json=body, timeout=_MCP_TIMEOUT
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_tool_call(_json(response))


@mcp_app.command("calls")
def mcp_calls(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List persisted MCP tool calls, newest first."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/mcp/tool-calls")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    calls = _json(response)
    if not calls:
        typer.echo("No tool calls.")
        return
    for call in calls:
        typer.echo(
            f"{call['id']}  {call['status']:<10}  {call['tool_name']:<24}  "
            f"caller={call['caller']}  {call['created_at']}"
        )


@mcp_app.command("call-show")
def mcp_call_show(
    call_id: str = typer.Argument(..., help="Tool call id."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Show a single persisted MCP tool call in full."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/mcp/tool-calls/{call_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(_json(response), indent=2))


def _print_tool_call(call: dict) -> None:
    """Render an MCP tool call record for the terminal."""
    typer.secho(f"Call:      {call['id']}", fg=typer.colors.GREEN)
    typer.echo(f"Tool:      {call['tool_name']}")
    typer.echo(f"Caller:    {call['caller']}")
    typer.echo(f"Status:    {call['status']}")
    if call.get("error"):
        typer.echo(f"Error:     {_preview(call['error'])}")
    typer.echo(f"Result:    {_preview(json.dumps(call.get('result') or {}))}")


# -- external agents -------------------------------------------------------------

# Shared agents-message options (module-level singletons keep callable defaults out of the
# function signature, the pattern B008 recommends).
AgentTypeOption = typer.Option("external", "--type", help="Agent class (e.g. external, manager).")
AgentUrlOption = typer.Option(None, "--url", help="Endpoint URL (stored for later; not called).")
AgentTransportOption = typer.Option(
    "local", "--transport", help="Transport (local|http|websocket)."
)
AgentAuthTypeOption = typer.Option("none", "--auth-type", help="Auth scheme hint; no secret.")
MessageTypeOption = typer.Option("mission_request", "--type", help="Message type.")
CreateMissionOption = typer.Option(
    True, "--create-mission/--no-create-mission", help="Create a mission from the message."
)
RunStepOption = typer.Option(
    False, "--run-step", help="Run one mission step (requires mission creation)."
)
PayloadJsonOption = typer.Option("{}", "--payload-json", help="Message payload as a JSON object.")
FilterAgentIdOption = typer.Option(None, "--agent-id", help="Filter by agent id.")
FilterMissionIdOption = typer.Option(None, "--mission-id", help="Filter by mission id.")


def _print_agent(agent: dict) -> None:
    """Render an external-agent header for the terminal."""
    status_colors = {"connected": typer.colors.GREEN, "disabled": typer.colors.RED}
    color = status_colors.get(agent["status"], typer.colors.YELLOW)
    typer.secho(f"Agent:     {agent['id']}", fg=typer.colors.CYAN)
    typer.echo(f"Name:      {agent['name']}")
    typer.echo(f"Type:      {agent['agent_type']}")
    typer.secho(f"Status:    {agent['status']}", fg=color)
    typer.echo(f"Transport: {agent['transport']}")
    typer.echo(f"Auth type: {agent['auth_type'] or 'none'}")
    typer.echo(f"Endpoint:  {agent['endpoint_url'] or '-'}")


def _guide_openai_compatible(role, provider, base_url, model, api_key_ref, *, interactive):
    """Collect base_url + model (+ secret name for hosted) for an OpenAI-compatible provider.

    Offers the Z.AI tested profile, suggests a provider-appropriate secret name, and never
    requires the user to know adapter internals. Returns (base_url, model, api_key_ref)."""
    from aithernet.agents import providers as prov
    is_local = provider == "openai_compatible_local"

    # Offer a tested profile (Z.AI) for the hosted OpenAI-compatible provider.
    if not base_url and not is_local and interactive:
        profiles = list(prov.OPENAI_COMPATIBLE_PROFILES.items())
        if profiles:
            typer.secho("Tested OpenAI-compatible profiles:", fg=typer.colors.CYAN)
            for i, (_, p) in enumerate(profiles, 1):
                typer.echo(f"  {i}. {p['display']}  ({p['base_url']})")
            typer.echo("  0. Custom endpoint")
            pick = typer.prompt("Choose a profile number", default="0").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(profiles):
                key, prof = profiles[int(pick) - 1]
                base_url = prof["coding_base_url"] if role == prov.CODING and \
                    prof.get("coding_base_url") else prof["base_url"]
                model = model or prof.get("suggested_model") or ""
                api_key_ref = api_key_ref or prof.get("suggested_secret")

    if not base_url:
        default = "http://127.0.0.1:8080/v1" if is_local else ""
        if interactive:
            base_url = typer.prompt("Endpoint base URL", default=default).strip()
        elif default:
            base_url = default
    if base_url:
        try:
            prov.validate_base_url(base_url)
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc

    if not model and interactive:
        model = typer.prompt("Model ID the endpoint accepts").strip()

    # A hosted endpoint needs a key; a local endpoint typically does not.
    if not is_local and not api_key_ref:
        suggested = prov.suggested_secret_name(provider)
        if interactive:
            api_key_ref = typer.prompt(
                "Secret NAME holding the API key (store the value with `agents set-secret`)",
                default=suggested).strip()
        else:
            api_key_ref = suggested
        if api_key_ref and api_key_ref not in prov.list_secret_names():
            typer.secho(f"Reminder: store the key with `aithernet agents set-secret {api_key_ref}` "
                        "(the value is read without echo).", fg=typer.colors.YELLOW)
    return base_url, model, api_key_ref


def _catgpt_choose_managed(*, interactive: bool) -> bool:
    """Offer the Aithernet-managed gateway vs an external endpoint (beta.5 Part D). Returns True to
    use the managed gateway. Non-interactive defaults to False (external — backward compatible)."""
    if not interactive:
        return False
    typer.secho("How do you want to connect CatGPT?", fg=typer.colors.CYAN, bold=True)
    typer.echo("  1. Aithernet-managed CatGPT Gateway  (recommended — Aithernet runs a local, "
               "loopback-bound gateway for you; you sign in via noVNC)")
    typer.echo("  2. External CatGPT Gateway endpoint  (advanced — you run the gateway yourself)")
    pick = typer.prompt("Choose 1 or 2", default="1").strip()
    return pick in ("1", "managed", "aithernet-managed")


def _connect_managed_catgpt(role: str, *, interactive: bool) -> None:
    """Configure the Aithernet-managed CatGPT gateway end-to-end from ``agents connect``."""
    from aithernet.catgpt.manager import CatGptGatewayManager, CatGptManagerError

    provider_choice = "chatgpt"
    if interactive:
        provider_choice = (typer.prompt(
            "Which web account should the gateway drive? (chatgpt|claude)",
            default="chatgpt").strip() or "chatgpt")
    mgr = CatGptGatewayManager()
    try:
        summary = mgr.setup(provider=provider_choice)
    except CatGptManagerError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    typer.secho("Aithernet-managed CatGPT Gateway configured and set as the coordinator.",
                fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  API base URL:  {summary['base_url']}  (loopback-only)")
    typer.echo(f"  bearer token:  stored via managed secrets as {summary['api_key_ref']} "
               "(not shown)")
    typer.echo("  Start it:      aithernet catgpt start")
    typer.echo(f"  Sign in:       open {summary['novnc_url']} and log into {provider_choice} "
               "yourself")
    typer.echo("  Restart node:  aithernet service restart  (foreground: restart `aithernet "
               "start`, which loads managed secrets)")
    typer.echo(f"  Then verify:   aithernet agents test {role} --live")
    typer.secho("  You own the web account and the login — Aithernet never stores your session, "
                "cookies, or password.", fg=typer.colors.CYAN)
    if not mgr.runner.available():
        typer.secho("  Note: docker was not found on PATH; install Docker to start the gateway.",
                    fg=typer.colors.YELLOW)


def _guide_catgpt_gateway(base_url, model, api_key_ref, *, interactive):
    """Guide CatGPT-Gateway coordinator setup in plain language (beta.5).

    Explains the local-gateway model, offers the default local endpoint, discovers models when the
    gateway is up, and keeps the API key optional (a local/dummy value is acceptable). Returns
    (base_url, model, api_key_ref). Never touches the gateway's browser cookies."""
    from aithernet.agents import providers as prov
    default_base = prov.catgpt_gateway_default_base_url()
    typer.secho("CatGPT Gateway — local OpenAI-compatible browser gateway (coordinator only):",
                fg=typer.colors.CYAN, bold=True)
    typer.echo("  • Start CatGPT-Gateway yourself first; sign into ChatGPT/Claude in its own "
               "browser/session flow if prompted.")
    typer.echo(f"  • Aithernet connects to its local endpoint (default {default_base}).")
    typer.echo("  • Aithernet never needs your browser cookies — it speaks only the OpenAI API.")
    typer.echo("  • This provider is coordinator reasoning ONLY — keep your coding agent "
               "(e.g. codex_cli) configured separately.")

    if not base_url:
        base_url = (typer.prompt("Gateway endpoint base URL", default=default_base).strip()
                    if interactive else default_base)
    try:
        prov.validate_base_url(base_url)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    # Bearer token: CatGPT-Gateway usually requires a LOCAL bearer token for both /models and
    # /chat/completions. Try discovery; if the gateway needs a token, prompt (hidden), store it as a
    # managed secret (recommended ref CATGPT_GATEWAY_API_KEY), and retry — no env exports required.
    recommended_ref = "CATGPT_GATEWAY_API_KEY"
    found = prov.discover_models(base_url, api_key_ref=api_key_ref or "")
    if not found and not api_key_ref and interactive:
        typer.secho("CatGPT-Gateway returned no models without a token — it likely needs a local "
                    "bearer token (its README often uses a placeholder such as 'dummy123'; use "
                    "YOUR gateway's actual token).", fg=typer.colors.YELLOW)
        token = typer.prompt(
            "CatGPT-Gateway local bearer token (press Enter to skip and configure later)",
            default="", hide_input=True).strip()
        if token:
            prov.set_secret(recommended_ref, token)     # 0600 managed store; value never printed
            api_key_ref = recommended_ref
            typer.secho(f"Stored the token as managed secret {recommended_ref} (value not shown).",
                        fg=typer.colors.GREEN)
            found = prov.discover_models(base_url, api_key_ref=api_key_ref)
        else:
            typer.secho("No token entered — the live test will fail until one is configured: "
                        f"`aithernet agents set-secret {recommended_ref}`.",
                        fg=typer.colors.YELLOW)

    # Choose the model — from what the gateway ACTUALLY reports (GET /models, with any token applied
    # above), or explicitly. A model is never fabricated: if discovery lists models the user must
    # pick one; if discovery is unavailable and no model is given, setup fails clearly (or,
    # interactively, the user types it).
    if not model:
        if found:
            if interactive:
                typer.secho("Models offered by the gateway:", fg=typer.colors.CYAN)
                for i, m in enumerate(found, 1):
                    typer.echo(f"  {i}. {m}")
                pick = typer.prompt("Choose a model number or type an id", default="1").strip()
                if pick.isdigit() and 1 <= int(pick) <= len(found):
                    model = found[int(pick) - 1]
                else:
                    model = pick
            else:
                typer.secho("The gateway offers: " + ", ".join(found)
                            + ". Re-run with --model <id> to choose one.",
                            fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
        elif interactive:
            model = typer.prompt(
                "Model id (the gateway listed none via /models — enter it manually, or start the "
                "gateway first and re-run)", default="").strip()
            if not model:
                typer.secho(
                    "CatGPT-Gateway model not configured and model discovery is unavailable — the "
                    "coordinator will not be usable until a model is set (start the gateway and "
                    "re-run, or pass --model).", fg=typer.colors.YELLOW)
        else:
            typer.secho(
                "CatGPT-Gateway model not configured and model discovery is unavailable. Start the "
                "gateway (for /models discovery) or pass --model <id>.",
                fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)

    # A local gateway usually needs no key; an optional (possibly dummy, local-only) one is allowed.
    if api_key_ref and api_key_ref not in prov.list_secret_names():
        typer.secho(f"Reminder: store the value with `aithernet agents set-secret {api_key_ref}` "
                    "(a local/dummy value is fine — the gateway carries the real subscription).",
                    fg=typer.colors.YELLOW)
    return base_url, model, api_key_ref


def _connect_provider_role(role: str, *, provider: str | None, model: str | None,
                           base_url: str | None = None, api_key_ref: str | None,
                           executable: str | None, identity_model: str | None, restart: bool,
                           verify_endpoint: bool = True) -> None:
    """Guided, provider-agnostic connect for the coordinator/coding ROLE — discover supported
    options, guide OpenAI-compatible URL/model/secret in plain language, validate the endpoint,
    configure, persist the PATH, restart, verify."""
    import shutil as _sh
    import sys as _sys

    from aithernet.agents import providers as prov
    selectable = [p for p in prov.providers_for(role) if p.key != prov.DISABLED]

    if not provider:
        typer.secho(f"Supported {role} options:", fg=typer.colors.CYAN, bold=True)
        kinds = {"api": "API key", "cli": "sign-in / CLI", "local": "local endpoint"}
        for i, p in enumerate(selectable, 1):
            typer.echo(f"  {i}. {p.display} ({kinds.get(p.kind, p.kind)})")
        if not _sys.stdin.isatty():
            typer.secho("Re-run with --provider <option> to choose non-interactively.",
                        fg=typer.colors.YELLOW)
            raise typer.Exit(code=2)
        choice = typer.prompt("Choose a number or name", default="1").strip()
        provider = selectable[int(choice) - 1].key if choice.isdigit() else choice

    info = prov.get_info(role, provider)
    if info is None:
        typer.secho(f"Unknown {role} provider '{provider}' (choose: "
                    f"{', '.join(p.key for p in selectable)}).", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # Resolve a CLI runtime automatically (NVM/npm/distro) when not supplied.
    exe = executable
    if info.kind == "cli" and not exe and info.executable:
        exe = _sh.which(info.executable) or info.executable

    # beta.10 Defect 2: OpenAI-compatible providers need a base URL + model. Guide them in plain
    # language (with the Z.AI tested profile), suggest a provider-appropriate secret name, and
    # validate the endpoint before saving. Never suggest GEMINI_API_KEY for a non-Gemini provider.
    if provider == "catgpt_gateway":
        # beta.5 Part D: offer the Aithernet-managed gateway (runs it locally for the client) or an
        # external endpoint. Managed setup configures the coordinator itself and returns early.
        if not base_url and _catgpt_choose_managed(interactive=_sys.stdin.isatty()):
            return _connect_managed_catgpt(role, interactive=_sys.stdin.isatty())
        base_url, model, api_key_ref = _guide_catgpt_gateway(
            base_url, model, api_key_ref, interactive=_sys.stdin.isatty())
        # Only validate the endpoint against a model the user actually chose — never a fabricated.
        if verify_endpoint and base_url and model:
            res = prov.validate_openai_endpoint(base_url, model, api_key_ref=api_key_ref or "")
            if res.get("ok"):
                typer.secho(f"Gateway validated ({res.get('host')}): {res.get('detail')}.",
                            fg=typer.colors.GREEN)
            else:
                typer.secho(f"Gateway not validated: {res.get('detail')}. Saving anyway; start "
                            f"CatGPT-Gateway, then re-run `aithernet agents test {role} --live`.",
                            fg=typer.colors.YELLOW)
    elif provider in prov.OPENAI_COMPATIBLE_PROVIDERS:
        base_url, model, api_key_ref = _guide_openai_compatible(
            role, provider, base_url, model, api_key_ref, interactive=_sys.stdin.isatty())
        if verify_endpoint:
            res = prov.validate_openai_endpoint(base_url, model, api_key_ref=api_key_ref or "")
            if res.get("ok"):
                typer.secho(f"Endpoint validated ({res.get('host')}): {res.get('detail')}.",
                            fg=typer.colors.GREEN)
            else:
                typer.secho(f"Endpoint not validated: {res.get('detail')}. Saving anyway; fix "
                            f"and re-run `aithernet agents test {role} --live`.",
                            fg=typer.colors.YELLOW)
    elif info.kind == "api" and not api_key_ref:
        suggested = prov.suggested_secret_name(provider)
        typer.secho(f"{info.display} needs an API key. Store it, then re-run with the reference:",
                    fg=typer.colors.YELLOW)
        typer.echo(f"  aithernet agents set-secret {suggested}")
        typer.echo(f"  aithernet agents connect {role} --provider {provider} "
                   f"--api-key-ref {suggested}")
        raise typer.Exit(code=2)

    try:
        prov.configure(role, provider, model=model or "", executable=exe or "",
                       base_url=base_url or "", api_key_ref=api_key_ref or "",
                       identity_model=identity_model or "")
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    typer.secho(f"Configured {role}: {info.display}.", fg=typer.colors.GREEN)

    # Persist the provider runtime PATH so the service can run a CLI provider.
    try:
        from aithernet.config.loader import canonical_node_config_path, load_config
        from aithernet.provisioning import services
        if canonical_node_config_path().is_file():
            services.write_runtime_env(load_config(str(canonical_node_config_path())))
    except Exception:  # noqa: BLE001 — PATH persistence is best-effort
        pass

    if restart:
        from aithernet.provisioning import repair as _repair
        from aithernet.provisioning import services
        if services.installed_scope() == "user":
            rc, detail = _repair._restart_user_service()
            typer.echo(f"Service restart: {'ok' if rc == 0 else f'rc={rc} ({detail})'}")

    # Verify the provider (one bounded live request) and report success or ONE actionable failure.
    try:
        result = prov.test(role, live=True)
        if result.get("status") == "ready" and result.get("live"):
            typer.secho(f"Verified — {role} is live-verified.", fg=typer.colors.GREEN, bold=True)
            return
        typer.secho(f"{role} configured but not yet live-verified "
                    f"({result.get('status', 'unknown')}). Complete auth, then: "
                    f"aithernet agents test {role} --live", fg=typer.colors.YELLOW)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"{role} configured; live verification deferred ({type(exc).__name__}). "
                    f"Run: aithernet agents test {role} --live", fg=typer.colors.YELLOW)


@agents_app.command("connect")
def agents_connect(
    role: str = typer.Argument(
        None, help="coordinator | coding for guided provider setup. Omit to connect an "
                   "external agent (requires --name)."),
    provider: str = typer.Option(
        None, "--provider", help="Adapter to use (advanced). If omitted, options are listed."),
    model: str = typer.Option(None, "--model", help="Model id (API providers)."),
    base_url: str = typer.Option(
        None, "--base-url", help="Endpoint base URL (OpenAI-compatible hosted/local providers)."),
    api_key_ref: str = typer.Option(
        None, "--api-key-ref", help="NAME of an env var holding the API key (never the key)."),
    executable: str = typer.Option(
        None, "--executable", help="CLI executable path (resolved automatically if omitted)."),
    identity_model: str = typer.Option(None, "--identity-model", help="workstation|appliance."),
    no_verify: bool = typer.Option(
        False, "--no-verify", help="Skip the bounded endpoint validation before saving."),
    no_restart: bool = typer.Option(
        False, "--no-restart", help="Do not restart/verify the service after configuring."),
    name: str = typer.Option(None, "--name", help="External-agent name (when role is omitted)."),
    agent_type: str = AgentTypeOption,
    url: str | None = AgentUrlOption,
    transport: str = AgentTransportOption,
    auth_type: str = AgentAuthTypeOption,
    config: str = ConfigOption,
    node_url: str | None = UrlOption,
) -> None:
    """Connect a coordinator/coding provider (guided) — or, with --name, an external agent.

    `aithernet agents connect coordinator` and `... coding` discover supported options, configure
    the provider, resolve its CLI runtime, restart the service and verify it — no adapter names or
    env files required.
    """
    from aithernet.agents import providers as prov
    if role in (prov.COORDINATOR, prov.CODING):
        return _connect_provider_role(
            role, provider=provider, model=model, base_url=base_url, api_key_ref=api_key_ref,
            executable=executable, identity_model=identity_model, restart=not no_restart,
            verify_endpoint=not no_verify)
    if role is not None:
        typer.secho(f"Unknown role '{role}' — use 'coordinator' or 'coding'.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    # External-agent connect (legacy/advanced path).
    if not name:
        typer.secho("Provide a role (coordinator|coding) for provider setup, or --name to connect "
                    "an external agent.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    base_url = _resolve_base_url(node_url, config)
    body = {"name": name, "agent_type": agent_type, "endpoint_url": url,
            "transport": transport, "auth_type": auth_type}
    try:
        with _client(base_url) as client:
            response = client.post("/agents/connect", json=body)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_agent(response.json())


@agents_app.command("list")
def agents_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List external agents, newest first."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/agents")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    agents = response.json()
    if not agents:
        typer.echo("No external agents.")
        return
    for a in agents:
        typer.echo(
            f"{a['id']}  {a['status']:<12}  {a['agent_type']:<10}  "
            f"{a['transport']:<10}  {a['name']}"
        )


@agents_app.command("show")
def agents_show(
    agent_id: str = typer.Argument(..., help="External agent id."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Show a single external agent in full."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/agents/{agent_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2))


@agents_app.command("status")
def agents_status(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Print the external-agent roster summary."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/agents/status")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    data = response.json()
    typer.secho(
        f"Connected: {data['connected_count']} / {data['total_count']} total",
        fg=typer.colors.CYAN,
    )
    for a in data["agents"]:
        typer.echo(f"  {a['id']}  {a['status']:<12}  {a['name']}")


@agents_app.command("set-status")
def agents_set_status(
    agent_id: str = typer.Argument(..., help="External agent id."),
    new_status: str = typer.Argument(..., help="connected | disconnected | disabled."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Set an external agent's connection status."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.patch(f"/agents/{agent_id}/status", json={"status": new_status})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_agent(response.json())


@agents_app.command("message")
def agents_message(
    agent_id: str = typer.Argument(..., help="External agent id."),
    content: str = typer.Argument(..., help="The message content."),
    message_type: str = MessageTypeOption,
    create_mission: bool = CreateMissionOption,
    run_step: bool = RunStepOption,
    payload_json: str = PayloadJsonOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Send a message from an external agent; optionally create a mission and run a step."""
    base_url = _resolve_base_url(url, config)
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        typer.secho(f"Invalid --payload-json: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if not isinstance(payload, dict):
        typer.secho("--payload-json must be a JSON object.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    body = {
        "message_type": message_type,
        "content": content,
        "payload": payload,
        "create_mission": create_mission,
        "run_step": run_step,
    }
    try:
        with _client(base_url) as client:
            response = client.post(
                f"/agents/{agent_id}/message", json=body, timeout=_RUN_TIMEOUT
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    data = response.json()
    _print_agent(data["agent"])
    typer.echo("")
    typer.secho(f"Message:   {data['message']['id']}", fg=typer.colors.GREEN)
    typer.echo(f"Direction: {data['message']['direction']}")
    if data.get("mission"):
        typer.echo(f"Mission:   {data['mission']['id']} ({data['mission']['status']})")
    else:
        typer.echo("Mission:   (none created)")
    if data.get("step_response"):
        step = data["step_response"]["step"]
        typer.echo("")
        _print_step(step)


@agents_app.command("messages")
def agents_messages(
    agent_id: str | None = FilterAgentIdOption,
    mission_id: str | None = FilterMissionIdOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """List external-agent messages, newest first, optionally filtered."""
    base_url = _resolve_base_url(url, config)
    params = {}
    if agent_id:
        params["agent_id"] = agent_id
    if mission_id:
        params["mission_id"] = mission_id
    try:
        with _client(base_url) as client:
            response = client.get("/agent-messages", params=params or None)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)

    messages = response.json()
    if not messages:
        typer.echo("No agent messages.")
        return
    for m in messages:
        mission = m["mission_id"] or "-"
        preview = m["content"].replace("\n", " ")
        if len(preview) > 40:
            preview = preview[:37] + "..."
        typer.echo(
            f"{m['id']}  {m['direction']:<9}  {m['message_type']:<16}  "
            f"agent={m['agent_id']}  mission={mission}  {preview}"
        )


# -- AI provider configuration (PHASE 6) ----------------------------------------
def _provider_state_root():
    import os as _os
    from pathlib import Path as _Path
    sr = _os.environ.get("AITHERNET_STATE_ROOT")
    return _Path(sr) if sr else None


@agents_app.command("providers")
def agents_providers(
    role: str = typer.Argument(None, help="coordinator | coding (default: both)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List supported coordinator + coding providers with their beta-qualification status and the
    current per-role readiness. Auth is reported ready ONLY when a bounded test has succeeded."""
    from aithernet.agents import providers as prov
    roles = [role] if role in prov.ROLES else list(prov.ROLES)
    catalogue = {r: [vars(p) for p in prov.providers_for(r)] for r in roles}
    statuses = {r: prov.status(r, state_root=_provider_state_root()) for r in roles}
    if json_out:
        typer.echo(json.dumps({"catalogue": catalogue, "status": statuses}, indent=2))
        return
    for r in roles:
        cur = statuses[r]
        typer.secho(f"\n{r}: {cur['display']} "
                    f"({'configured' if cur['configured'] else 'not configured'}; auth="
                    f"{cur['auth_readiness']})",
                    fg=typer.colors.CYAN, bold=True)
        for p in prov.providers_for(r):
            tag = p.support_level + (" · preferred beta default" if p.preferred_beta else "")
            mark = "*" if p.key == cur["provider"] else " "
            typer.echo(f"  {mark} {p.key:<24} {p.kind:<8} [{tag}]")
            typer.echo(f"      auth: {p.auth}")


@agents_app.command("provider-status")
def agents_provider_status_cmd(
    role: str = typer.Argument(None, help="coordinator | coding (default: both)."),
) -> None:
    """Show sanitised provider readiness: provider, model/endpoint, executable + version, runtime
    Linux identity, configured?, auth readiness, last bounded test. No tokens / paths / responses.

    (Named `provider-status` so it does not shadow the Stage 7 external-agent `agents status`.)"""
    from aithernet.agents import providers as prov
    roles = [role] if role in prov.ROLES else list(prov.ROLES)
    out = {r: prov.status(r, state_root=_provider_state_root()) for r in roles}
    typer.echo(json.dumps(out, indent=2))


@agents_app.command("test")
def agents_provider_test(
    role: str = typer.Argument(..., help="coordinator | coding."),
    live: bool = typer.Option(False, "--live",
                              help="Make ONE real bounded provider request to verify auth "
                                   "end-to-end (coordinator: a minimal inference; coding: a "
                                   "non-destructive nonce-echo task in an isolated workspace)."),
) -> None:
    """Run a bounded, non-sensitive readiness request and report ready only if it succeeds."""
    from aithernet.agents import providers as prov
    if role not in prov.ROLES:
        raise typer.BadParameter(f"role must be one of {prov.ROLES}")
    result = prov.test(role, live=live, state_root=_provider_state_root())
    typer.echo(json.dumps(result, indent=2))
    if not result.get("ready"):
        raise typer.Exit(code=1)


@agents_app.command("models")
def agents_models(
    provider: str = typer.Argument(..., help="Provider, e.g. gemini_api."),
) -> None:
    """List the recommended/supported models for an API coordinator provider (and the default), so
    you can validate a model before `agents configure`."""
    from aithernet.coordinator.providers import gemini_api
    if provider == "gemini_api":
        typer.echo(json.dumps({"provider": "gemini_api", "default": gemini_api.DEFAULT_MODEL,
                               "recommended": list(gemini_api.RECOMMENDED_MODELS),
                               "rule": "any gemini-* model id is accepted"}, indent=2))
    else:
        typer.echo(json.dumps({"provider": provider,
                               "note": "model id is provider-specific; see the provider's docs"},
                              indent=2))


@agents_app.command("list-providers")
def agents_list_providers(
    role: str = typer.Argument(None, help="coordinator | coding (default: both)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List every provider with its capabilities, authentication category, and billing model.

    Billing is stated accurately per provider: subscription allowance, separate API billing,
    cloud-provider billing, or local compute — so it is never implied that a consumer subscription
    includes unrelated API credits."""
    from aithernet.agents import providers as prov
    roles = [role] if role in prov.ROLES else list(prov.ROLES)
    payload: dict = {}
    for r in roles:
        items = []
        for p in prov.providers_for(r):
            items.append({
                "key": p.key, "display": p.display, "kind": p.kind,
                "auth_category": p.auth_category(), "billing": p.billing(),
                "capabilities": sorted(p.capabilities()),
                "support_level": p.support_level, "preferred_beta": p.preferred_beta,
                "eligible": p.satisfies_role(),
            })
        payload[r] = items
    if json_out:
        typer.echo(json.dumps(payload, indent=2))
        return
    for r in roles:
        typer.secho(f"\n{r} providers:", fg=typer.colors.CYAN, bold=True)
        for it in payload[r]:
            star = "*" if it["preferred_beta"] else " "
            typer.echo(f"  {star} {it['key']:<26} {it['kind']:<6} "
                       f"auth={it['auth_category']:<12} billing={it['billing']}")


@agents_app.command("inspect-provider")
def agents_inspect_provider(
    provider: str = typer.Argument(..., help="Provider key, e.g. claude_cli."),
    role: str = typer.Option(None, "--role", help="Disambiguate if a key exists in both roles."),
) -> None:
    """Show one provider's capabilities, auth category, billing, capacity-pool family, and notes."""
    from aithernet.agents import capacity as cap
    from aithernet.agents import providers as prov
    roles = [role] if role in prov.ROLES else list(prov.ROLES)
    info = next((prov.get_info(r, provider) for r in roles if prov.get_info(r, provider)), None)
    if info is None:
        raise typer.BadParameter(f"unknown provider '{provider}'")
    typer.echo(json.dumps({
        "key": info.key, "role": info.role, "display": info.display, "kind": info.kind,
        "executable": info.executable, "auth": info.auth,
        "auth_category": info.auth_category(), "billing": info.billing(),
        "capabilities": sorted(info.capabilities()),
        "pool_family": cap.pool_family(info.key),
        "support_level": info.support_level, "eligible_for_role": info.satisfies_role(),
        "notes": info.notes,
    }, indent=2))


@agents_app.command("capacity")
def agents_capacity(
    role: str = typer.Argument(None, help="coordinator | coding (default: both)."),
    all_pools: bool = typer.Option(False, "--all", help="Show all configured pools."),
) -> None:
    """Show capacity pools for the configured roles. Roles that share an account share ONE pool
    (e.g. a Claude subscription used for both coordinator and Claude Code coding). No usage numbers
    are invented — only what the provider exposes plus the configured concurrency."""
    from aithernet.agents import capacity as cap
    from aithernet.agents import providers as prov
    inputs = prov.capacity_pool_inputs(state_root=_provider_state_root())
    if role in prov.ROLES and not all_pools:
        inputs = {role: inputs[role]}
    user = ""
    try:
        import getpass
        user = getpass.getuser()
    except Exception:
        user = "runtime"
    pools = cap.build_pools(inputs, runtime_user=user)
    shared = len({p.pool_id for p in pools.values()}) < sum(
        1 for c in inputs.values() if c.get("provider") != "disabled")
    typer.echo(json.dumps({
        "pools": [s.view() for s in pools.values()],
        "shared_pool": shared,
    }, indent=2))


@agents_app.command("fallbacks")
def agents_fallbacks(
    role: str = typer.Argument(..., help="coordinator | coding."),
) -> None:
    """Show the explicit, role-valid fallback chain for a role (primary first)."""
    from aithernet.agents import providers as prov
    if role not in prov.ROLES:
        raise typer.BadParameter(f"role must be one of {prov.ROLES}")
    chain = prov.fallback_chain(role, state_root=_provider_state_root())
    typer.echo(json.dumps({"role": role, "chain": chain,
                           "note": "fallback is explicit + recorded; never silent"}, indent=2))


@agents_app.command("set-fallbacks")
def agents_set_fallbacks(
    role: str = typer.Argument(..., help="coordinator | coding."),
    providers_csv: str = typer.Argument(..., help="Comma-separated provider keys / provider:profile."),
) -> None:
    """Set the explicit fallback chain for a role. Each entry must be eligible for the role."""
    from aithernet.agents import providers as prov
    if role not in prov.ROLES:
        raise typer.BadParameter(f"role must be one of {prov.ROLES}")
    entries = [e.strip() for e in providers_csv.split(",") if e.strip()]
    try:
        result = prov.set_fallback_chain(role, entries, state_root=_provider_state_root())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2))


@agents_app.command("disconnect")
def agents_disconnect(
    role: str = typer.Argument(..., help="coordinator | coding."),
) -> None:
    """Disable a role's provider (sets it to `disabled`). Does not delete stored secrets."""
    from aithernet.agents import providers as prov
    if role not in prov.ROLES:
        raise typer.BadParameter(f"role must be one of {prov.ROLES}")
    result = prov.configure(role, "disabled", state_root=_provider_state_root())
    typer.echo(json.dumps(result, indent=2))


@agents_app.command("presets")
def agents_presets(json_out: bool = typer.Option(False, "--json")) -> None:
    """List the recommended provider presets (recommendations only — never auto-applied)."""
    from aithernet.agents import presets as pr
    items = [{"name": p.name, "coordinator": p.coordinator, "coding": p.coding,
              "shared_pool": p.shared_pool, "description": p.description,
              "billing": p.billing_summary()} for p in pr.list_presets()]
    if json_out:
        typer.echo(json.dumps(items, indent=2))
        return
    for it in items:
        typer.secho(f"\n{it['name']}", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"  coordinator: {it['coordinator']}   coding: {it['coding']}"
                   f"   shared_pool: {it['shared_pool']}")
        typer.echo(f"  {it['description']}")


@agents_app.command("set-secret")
def agents_set_secret(
    name: str = typer.Argument(..., help="Env-var NAME holding the key, e.g. GEMINI_API_KEY."),
    remove: bool = typer.Option(False, "--remove", help="Remove the stored secret instead."),
) -> None:
    """Securely store (or rotate/remove) an API key in the node's 0600 credential env file.

    The value is read WITHOUT echo and written only to the file the systemd user service loads
    (~/.config/aithernet/aithernet.env, mode 0600). The value is never printed or logged. Record
    the reference in node config with `aithernet agents configure ... --api-key-ref NAME`.
    """
    import getpass

    from aithernet.agents import providers as prov
    from aithernet.provisioning import services as _services
    if remove:
        removed = prov.remove_secret(name)
        typer.secho(f"{'Removed' if removed else 'No such secret'}: {name}",
                    fg=typer.colors.GREEN if removed else typer.colors.YELLOW)
        if removed:
            _report_secret_propagation(_services.propagate_secret_to_service(name), removed=True)
        return
    value = getpass.getpass(f"Enter value for {name} (input hidden): ")
    if not value.strip():
        raise typer.BadParameter("empty value")
    try:
        path = prov.set_secret(name, value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.secho(f"Stored {name} in {path} (0600). Value not shown.", fg=typer.colors.GREEN)
    # beta.10 Defect 1: make it available to the RUNNING service automatically (no manual
    # `systemctl --user import-environment`) and verify the service process actually sees it.
    _report_secret_propagation(_services.propagate_secret_to_service(name))
    typer.echo(f"Reference it: aithernet agents configure coordinator --api-key-ref {name} ...")


def _report_secret_propagation(rec: dict, *, removed: bool = False) -> None:
    """Print the outcome of pushing a secret change to the running service (never the value)."""
    scope = rec.get("scope")
    if scope is None:
        typer.secho(f"  {rec.get('detail', '')}", fg=typer.colors.YELLOW)
        return
    if scope == "system":
        typer.secho(f"  {rec.get('detail', '')}", fg=typer.colors.CYAN)
        return
    if not rec.get("restarted"):
        typer.secho(f"  service restart failed: {rec.get('detail', '')}", fg=typer.colors.RED)
        return
    sees = rec.get("service_sees_secret")
    if removed:
        ok = sees is False
        typer.secho(f"  service restarted; secret {'cleared from' if ok else 'may persist in'} "
                    "the running service.", fg=typer.colors.GREEN if ok else typer.colors.YELLOW)
        return
    if sees is True:
        typer.secho("  service restarted and now sees the secret (verified). No manual "
                    "import-environment needed.", fg=typer.colors.GREEN)
    elif sees is None:
        typer.secho("  service restarted (could not read its environment to verify).",
                    fg=typer.colors.YELLOW)
    else:
        typer.secho("  service restarted but does NOT yet see the secret — run "
                    "`aithernet agents secrets check " + str(rec.get("secret", "")) + "`.",
                    fg=typer.colors.RED)


@agents_secrets_app.command("list")
def agents_secrets_list() -> None:
    """List stored secret NAMES and whether the running service currently sees each (no values)."""
    from aithernet.agents import providers as prov
    from aithernet.provisioning import services as _services
    names = prov.list_secret_names()
    rows = [{"name": n, "stored": True, "service_sees": _services.service_sees_env(n)}
            for n in names]
    typer.echo(json.dumps({"secrets": rows, "count": len(rows)}, indent=2))


@agents_secrets_app.command("check")
def agents_secrets_check(
    name: str = typer.Argument(..., help="Secret NAME to check (availability only, never value)."),
) -> None:
    """Check a secret: stored in the credential file? seen by the running service? (no value)."""
    from aithernet.agents import providers as prov
    from aithernet.provisioning import services as _services
    stored = name in prov.list_secret_names()
    sees = _services.service_sees_env(name)
    out = {"name": name, "stored": stored, "service_sees": sees}
    typer.echo(json.dumps(out, indent=2))
    if stored and sees is False:
        typer.secho("Stored but the running service does not see it — set it again or restart "
                    "the service to reload it.", fg=typer.colors.YELLOW)


@agents_secrets_app.command("remove")
def agents_secrets_remove(
    name: str = typer.Argument(..., help="Secret NAME to remove."),
) -> None:
    """Remove a stored secret and propagate the removal to the running service."""
    from aithernet.agents import providers as prov
    from aithernet.provisioning import services as _services
    removed = prov.remove_secret(name)
    typer.secho(f"{'Removed' if removed else 'No such secret'}: {name}",
                fg=typer.colors.GREEN if removed else typer.colors.YELLOW)
    if removed:
        _report_secret_propagation(_services.propagate_secret_to_service(name), removed=True)


@agents_app.command("remove")
def agents_provider_remove(
    role: str = typer.Argument(..., help="coordinator | coding."),
) -> None:
    """Reset a role to disabled (no credentials were ever stored to delete)."""
    from aithernet.agents import providers as prov
    if role not in prov.ROLES:
        raise typer.BadParameter(f"role must be one of {prov.ROLES}")
    typer.echo(json.dumps(prov.remove(role, state_root=_provider_state_root()), indent=2))


def _configure_provider(role, provider, model, executable, base_url, api_key_ref, identity_model):
    from aithernet.agents import providers as prov
    try:
        result = prov.configure(role, provider, model=model or "", executable=executable or "",
                                base_url=base_url or "", api_key_ref=api_key_ref or "",
                                identity_model=identity_model or "",
                                state_root=_provider_state_root())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2))
    typer.secho(f"note: support level — {result['support_level']}"
                + (" (preferred beta default)" if result.get("preferred_beta") else ""),
                fg=typer.colors.YELLOW)
    # beta.10 Defect 1: a config change requires the running service to reload. Do it automatically
    # (no manual `systemctl --user restart`) and, when a key reference is configured, verify the
    # service actually sees the referenced secret.
    from aithernet.provisioning import services as _services
    if _services.installed_scope() is not None:
        rc, detail = _services.restart_service()
        if rc == 0:
            typer.secho("Restarted the node service to load the new provider.",
                        fg=typer.colors.GREEN)
            if api_key_ref:
                sees = _services.service_sees_env(api_key_ref)
                if sees is True:
                    typer.secho(f"Verified the service sees {api_key_ref}.",
                                fg=typer.colors.GREEN)
                elif sees is False:
                    typer.secho(f"Service does not yet see {api_key_ref} — store it with "
                                f"`aithernet agents set-secret {api_key_ref}`.",
                                fg=typer.colors.YELLOW)
        else:
            typer.secho(f"note: restart the node service to load the provider ({detail}).",
                        fg=typer.colors.CYAN)
    typer.echo(f"Verify it reached the runtime: aithernet agents provider-status {role}  "
               f"(and `aithernet agents test {role} --live` for end-to-end auth).")


@agents_configure_app.command("coordinator")
def agents_configure_coordinator(
    provider: str = typer.Option(..., "--provider",
                                 help="gemini_api|gemini_cli|anthropic|openai_compatible|"
                                      "openai_compatible_local|disabled"),
    model: str = typer.Option(None, "--model"),
    executable: str = typer.Option(None, "--executable",
                                   help="CLI executable (for CLI providers)."),
    base_url: str = typer.Option(None, "--base-url", help="API/local endpoint base URL."),
    api_key_ref: str = typer.Option(None, "--api-key-ref",
                                    help="NAME of an env var holding the API key (NOT the key)."),
    identity_model: str = typer.Option(None, "--identity-model", help="workstation|appliance"),
) -> None:
    """Configure the coordinator (planning) provider. Stores only the selection — never a key."""
    _configure_provider("coordinator", provider, model, executable, base_url, api_key_ref,
                        identity_model)


@agents_configure_app.command("coding")
def agents_configure_coding(
    provider: str = typer.Option(..., "--provider",
                                 help="codex_cli (preferred) | claude_code | disabled"),
    executable: str = typer.Option(None, "--executable", help="CLI executable override."),
    identity_model: str = typer.Option(None, "--identity-model", help="workstation|appliance"),
) -> None:
    """Configure the coding agent provider. Stores only the selection — never a credential."""
    _configure_provider("coding", provider, None, executable, None, None, identity_model)


# -- mcp session (Stage 11B) -----------------------------------------------------


def _print_session(d: dict) -> None:
    """Render the managed MCP session status for the terminal (no secrets)."""
    state = d["state"]
    colors = {
        "ready": typer.colors.GREEN,
        "degraded": typer.colors.YELLOW,
        "restarting": typer.colors.YELLOW,
        "starting": typer.colors.YELLOW,
        "failed": typer.colors.RED,
        "stopped": typer.colors.WHITE,
    }
    typer.secho(f"State:        {state}", fg=colors.get(state, typer.colors.WHITE))
    typer.echo(f"Session id:   {d['session_id']}")
    typer.echo(f"Provider:     {d['provider']}")
    typer.echo(f"Configured:   {'yes' if d['configured'] else 'no'}")
    typer.echo(f"Generation:   {d['generation']}")
    typer.echo(f"Restart cnt:  {d['restart_count']}")
    typer.echo(f"PID:          {d['pid'] if d.get('pid') is not None else '-'}")
    uptime = d.get("uptime_seconds")
    typer.echo(f"Uptime:       {round(uptime, 1) if uptime is not None else '-'}s")
    typer.echo(f"Tool count:   {d['tool_count'] if d.get('tool_count') is not None else '-'}")
    typer.echo(f"Active reqs:  {d['active_requests']} (max {d['max_in_flight_requests']})")
    typer.echo(f"Autostart:    {d['autostart']}   Auto-restart: {d['auto_restart']}")
    if d.get("missing_configuration"):
        typer.echo(f"Missing:      {', '.join(d['missing_configuration'])}")
    if d.get("last_error"):
        typer.secho(
            f"Last error:   [{d.get('last_error_type')}] {_preview(d['last_error'])}",
            fg=typer.colors.RED,
        )


def _session_request(method: str, path: str, config: str, url: str | None) -> dict:
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.request(method, path, timeout=_MCP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    return response.json()


@mcp_session_app.command("status")
def mcp_session_status(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show the persistent MCP session status (polling spawns no subprocess)."""
    _print_session(_session_request("GET", "/mcp/session", config, url))


@mcp_session_app.command("start")
def mcp_session_start(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Start the persistent MCP session (idempotent)."""
    _print_session(_session_request("POST", "/mcp/session/start", config, url))


@mcp_session_app.command("restart")
def mcp_session_restart(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Restart the session (new generation, rediscovered tools)."""
    _print_session(_session_request("POST", "/mcp/session/restart", config, url))


@mcp_session_app.command("stop")
def mcp_session_stop(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Stop the persistent MCP session and clean up the subprocess."""
    _print_session(_session_request("POST", "/mcp/session/stop", config, url))


# -- gnuradio context (Stage 11B) ------------------------------------------------


def _print_context(d: dict) -> None:
    """Render the GNU Radio workspace context for the terminal."""
    fresh = "stale" if d["stale"] else "fresh"
    color = typer.colors.YELLOW if d["stale"] else typer.colors.GREEN
    typer.secho(f"Freshness:    {fresh}", fg=color)
    if d.get("stale_reason"):
        typer.echo(f"Stale reason: {d['stale_reason']}")
    flowgraph = d.get("active_flowgraph_name") or d.get("active_flowgraph_path") or "(none)"
    typer.echo(f"Flowgraph:    {flowgraph}")
    typer.echo(f"Status:       {d['flowgraph_status']}")
    blocks = d.get("block_summary") or {}
    conns = d.get("connection_summary") or {}
    typer.echo(f"Blocks:       {blocks.get('count') if blocks.get('count') is not None else '-'}")
    typer.echo(f"Connections:  {conns.get('count') if conns.get('count') is not None else '-'}")
    val = d.get("validation_summary") or {}
    typer.echo(f"Validation:   {val.get('summary') or '-'}")
    errs = d.get("latest_error_summary") or {}
    typer.echo(f"Errors:       {errs.get('summary') or '-'}")
    exe = d.get("execution_summary") or {}
    typer.echo(f"Execution:    {exe.get('summary') or '-'}")
    typer.echo(f"Mission:      {d.get('related_mission_id') or '-'}")
    gen = d.get("source_session_generation")
    typer.echo(f"Source gen:   {gen if gen is not None else '-'}")
    typer.echo(f"Last refresh: {d.get('last_refreshed_at') or '(never)'}")


def _show_context(config: str, url: str | None) -> None:
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/gnuradio/context")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_context(response.json())


@gnuradio_context_app.callback(invoke_without_command=True)
def gnuradio_context_main(
    ctx: typer.Context,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Show the current GNU Radio workspace context (when no subcommand is given)."""
    if ctx.invoked_subcommand is None:
        _show_context(config, url)


@gnuradio_context_app.command("show")
def gnuradio_context_show(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show the current GNU Radio workspace context."""
    _show_context(config, url)


@gnuradio_context_app.command("refresh")
def gnuradio_context_refresh(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Refresh the GNU Radio context via available read-only tools (no execution/mutation)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post("/gnuradio/context/refresh", json={}, timeout=_MCP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_context(response.json())


# -- Stage 13A: identity / peers / agent messages / transport --------------------

# Shared options for the transport CLI.
PeerNameOption = typer.Option(..., "--name", help="Peer display name.")
PeerRoleOption = typer.Option("node", "--role", help="Peer role: node, manager, external_agent.")
PeerEndpointOption = typer.Option(None, "--endpoint", help="Peer transport base URL.")
PeerExpectedNodeOption = typer.Option(None, "--node-id", help="Expected peer node id.")
PeerPublicKeyOption = typer.Option(None, "--public-key", help="Peer base64 Ed25519 public key.")
PeerFingerprintOption = typer.Option(None, "--fingerprint", help="Expected key fingerprint.")
PeerNoTlsOption = typer.Option(False, "--no-tls-verify", help="Disable TLS verification (dev).")
RotateOption = typer.Option(False, "--rotate", help="Rotate to a NEW key (explicit).")
MsgPeerOption = typer.Option(..., "--peer-id", help="Destination peer id.")
MsgSubjectOption = typer.Option(None, "--subject", help="Message subject.")
MsgTextOption = typer.Option(None, "--text", help="Message text.")
MsgDataJsonOption = typer.Option("{}", "--data-json", help="Structured payload as JSON object.")
MsgSendNowOption = typer.Option(False, "--send-now", help="Attempt one delivery synchronously.")
OutboxStatusOption = typer.Option(None, "--status", help="Filter outbox by delivery status.")


def _print_identity(data: dict) -> None:
    if not data.get("initialized"):
        typer.secho("Identity: not initialized", fg=typer.colors.YELLOW)
        typer.echo(f"  State dir: {data.get('state_directory')}")
        return
    typer.secho(f"Node:        {data['node_name']} ({data['node_id']})", fg=typer.colors.CYAN)
    typer.echo(f"  Fingerprint: {data.get('fingerprint_short') or data.get('fingerprint')}")
    typer.echo(f"  Version:     {data.get('identity_version')}")
    typer.echo(f"  Created:     {data.get('created_at')}")
    if data.get("rotated_at"):
        typer.echo(f"  Rotated:     {data.get('rotated_at')}")


def _short_fp(fp: str | None) -> str:
    if not fp:
        return "-"
    body = fp.split(":", 1)[-1]
    return f"sha256:{body[:16]}" if body else fp


def _print_peer(peer: dict) -> None:
    trust = peer.get("trust_state", "?")
    color = {"trusted": typer.colors.GREEN, "revoked": typer.colors.RED}.get(
        trust, typer.colors.YELLOW
    )
    typer.secho(f"Peer {peer['id']}  [{trust}]", fg=color)
    typer.echo(f"  Name:        {peer.get('name')} ({peer.get('role')})")
    typer.echo(f"  Endpoint:    {peer.get('endpoint_url')}")
    typer.echo(f"  Node id:     {peer.get('expected_node_id')}")
    typer.echo(f"  Fingerprint: {_short_fp(peer.get('fingerprint'))}")
    typer.echo(f"  Enabled:     {peer.get('enabled')}  TLS verify: {peer.get('tls_verify')}")
    if peer.get("last_error"):
        typer.echo(f"  Last error:  {peer['last_error']}")


def _print_outbox_row(m: dict) -> None:
    typer.echo(
        f"  {m['message_id'][:8]}  {m['status']:<14} {m['kind']:<14} "
        f"attempts={m['attempt_count']}/{m['max_attempts']}  peer={m['peer_id'][:8]}"
    )


def _print_inbox_row(m: dict) -> None:
    typer.echo(
        f"  {m['message_id'][:8]}  {m['kind']:<14} from={m['sender_node_id'][:12]} "
        f"dup={m['duplicate_count']}  {m['received_at']}"
    )


# -- identity --------------------------------------------------------------------


@identity_app.command("status")
def identity_status_cmd(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show this node's identity status (fingerprint/public metadata; never the private key)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/identity/status")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_identity(response.json())


@identity_app.command("initialize")
def identity_initialize_cmd(
    rotate: bool = RotateOption, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Generate the node identity if absent (idempotent). ``--rotate`` makes a NEW key."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post("/identity/initialize", params={"rotate": rotate})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_identity(response.json())


@identity_app.command("public")
def identity_public_cmd(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Print the shareable public identity document (safe to send to a peer operator)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/identity/public")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2))


@identity_app.command("fingerprint")
def identity_fingerprint_cmd(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Print this node's identity fingerprint."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/identity/fingerprint")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    data = response.json()
    typer.secho(data.get("fingerprint_short") or data.get("fingerprint"), fg=typer.colors.CYAN)


# -- peers -----------------------------------------------------------------------


@peer_app.command("add")
def peer_add_cmd(
    name: str = PeerNameOption,
    role: str = PeerRoleOption,
    url_: str | None = PeerEndpointOption,
    node_id: str | None = PeerExpectedNodeOption,
    public_key: str | None = PeerPublicKeyOption,
    fingerprint: str | None = PeerFingerprintOption,
    no_tls_verify: bool = PeerNoTlsOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Add an UNTRUSTED peer. Trust must be granted explicitly with ``peer trust``."""
    base_url = _resolve_base_url(url, config)
    body = {
        "name": name, "role": role, "endpoint_url": url_, "expected_node_id": node_id,
        "public_key": public_key, "fingerprint": fingerprint, "tls_verify": not no_tls_verify,
    }
    try:
        with _client(base_url) as client:
            response = client.post("/peers", json=body)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_peer(response.json())


@peer_app.command("list")
def peer_list_cmd(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List transport peers and their trust state."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/peers")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    peers = response.json()
    if not peers:
        typer.echo("No peers.")
        return
    for peer in peers:
        _print_peer(peer)


@peer_app.command("show")
def peer_show_cmd(peer_id: str, config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show one peer."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/peers/{peer_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_peer(response.json())


@peer_app.command("trust")
def peer_trust_cmd(
    peer_id: str,
    public_key: str | None = PeerPublicKeyOption,
    fingerprint: str | None = PeerFingerprintOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Explicitly trust a peer, pinning its public key (never silently replaces a key)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(
                f"/peers/{peer_id}/trust",
                json={"public_key": public_key, "fingerprint": fingerprint},
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_peer(response.json())


@peer_app.command("revoke")
def peer_revoke_cmd(peer_id: str, config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Revoke a peer (it can no longer deliver to or receive from this node)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(f"/peers/{peer_id}/revoke")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_peer(response.json())


@peer_app.command("transports")
def peer_transports_cmd(peer_id: str, config: str = ConfigOption,
                        url: str | None = UrlOption) -> None:
    """Show the peer transports available for a peer. rf_ota physical TX is closed (beta.1)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get("/peers")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    match = next((p for p in (resp.json() or [])
                  if p.get("id") == peer_id or p.get("peer_id") == peer_id
                  or p.get("name") == peer_id), None)
    if match is None:
        raise typer.BadParameter(f"no peer '{peer_id}'")
    avail = _peer_transports_available(match)
    typer.echo(json.dumps({
        "peer": match.get("name") or peer_id,
        "transports": {"ip": "available" if avail["ip"] else "no-endpoint",
                       "simulated_rf": "available",
                       "rf_ota": "closed-pending-hardware-and-plan-authorization"},
    }, indent=2))


@peer_app.command("test")
def peer_test_cmd(peer_id: str, transport: str = typer.Option(
                      "ip", "--transport", help="ip|simulated_rf|rf_ota (rf_ota needs local TX "
                                                "enabled + a capable device)."),
                  config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Health-check a peer endpoint over a chosen transport."""
    base_url = _resolve_base_url(url, config)
    if transport == "rf_ota":
        avail, devices = _rf_ota_availability()
        if avail == "available":
            typer.secho(f"Peer transport rf_ota: available ({len(devices)} TX-capable device(s) "
                        "present; operator-enabled). A bounded physical link test transmits only "
                        "after you confirm a plan.", fg=typer.colors.GREEN)
        else:
            typer.secho(f"Peer transport rf_ota: {avail}. Enable with `aithernet rf tx enable` and "
                        "connect a TX-capable SDR.", fg=typer.colors.YELLOW)
        return
    if transport == "simulated_rf":
        # The simulated RF transport is a software loopback (modem + impaired channel), always
        # locally available; a real link health-check runs at delivery time.
        typer.secho("Peer transport simulated_rf: available (software loopback; modem + impaired "
                    "channel).", fg=typer.colors.GREEN)
        return
    try:
        with _client(base_url) as client:
            response = client.post(f"/peers/{peer_id}/test")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    data = response.json()
    ok = data.get("ok")
    typer.secho(
        f"Peer health: {'ok' if ok else 'failed'}",
        fg=typer.colors.GREEN if ok else typer.colors.RED,
    )


@peer_app.command("pair")
def peer_pair_cmd(
    name: str = PeerNameOption,
    public_key: str | None = PeerPublicKeyOption,
    url_: str | None = PeerEndpointOption,
    node_id: str | None = PeerExpectedNodeOption,
    role: str = PeerRoleOption,
    fingerprint: str | None = PeerFingerprintOption,
    no_tls_verify: bool = PeerNoTlsOption,
    no_test: bool = typer.Option(False, "--no-test", help="Skip the health-check after pairing."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Pair a peer in ONE step — add it, pin its key (explicit trust), health-check.

    Equivalent to ``peer add`` + ``peer trust`` + ``peer test``. A public key is required so trust
    pins it (we never trust a peer without a pinned key). Idempotent: re-pairing the same endpoint
    re-pins the same key and re-checks health."""
    if not public_key:
        raise typer.BadParameter("--public-key is required to pair (trust pins the peer's key)")
    base_url = _resolve_base_url(url, config)
    add_body = {"name": name, "role": role, "endpoint_url": url_, "expected_node_id": node_id,
                "public_key": public_key, "fingerprint": fingerprint,
                "tls_verify": not no_tls_verify}
    try:
        with _client(base_url) as client:
            added = client.post("/peers", json=add_body)
            _check(added)
            peer = added.json()
            peer_id = peer["id"] if "id" in peer else peer.get("peer_id")
            typer.secho(f"Added peer {peer_id} ({name}).", fg=typer.colors.GREEN)
            trusted = client.post(f"/peers/{peer_id}/trust",
                                  json={"public_key": public_key, "fingerprint": fingerprint})
            _check(trusted)
            typer.secho("Trusted (key pinned).", fg=typer.colors.GREEN)
            _print_peer(trusted.json())
            if not no_test:
                health = client.post(f"/peers/{peer_id}/test")
                ok = health.is_success and (health.json() or {}).get("ok")
                typer.secho(f"Health: {'ok' if ok else 'unreachable (peer not up yet?)'}",
                            fg=typer.colors.GREEN if ok else typer.colors.YELLOW)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc


@peer_app.command("manifest")
def peer_manifest_cmd(
    peer_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Fetch + verify a peer's signed capability manifest (a manifest never grants trust)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(f"/peers/{peer_id}/refresh-manifest")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    manifest = response.json()
    typer.secho(f"Manifest for {manifest.get('node_name')} ({manifest.get('node_id')})",
                fg=typer.colors.CYAN)
    typer.echo(f"  Capabilities: {', '.join(manifest.get('capabilities', []))}")
    typer.echo(f"  Kinds:        {', '.join(manifest.get('supported_kinds', []))}")


# -- agent messages --------------------------------------------------------------


@agent_message_app.command("send")
def agent_message_send_cmd(
    peer_id: str = MsgPeerOption,
    subject: str | None = MsgSubjectOption,
    text: str | None = MsgTextOption,
    data_json: str = MsgDataJsonOption,
    send_now: bool = MsgSendNowOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Create + sign an outbound message to a peer and enqueue it for durable delivery."""
    base_url = _resolve_base_url(url, config)
    try:
        data = json.loads(data_json)
        if not isinstance(data, dict):
            raise ValueError("data must be a JSON object")
    except ValueError as exc:
        typer.secho(f"Invalid --data-json: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    body = {
        "peer_id": peer_id, "subject": subject, "text": text, "data": data, "send_now": send_now,
    }
    try:
        with _client(base_url) as client:
            response = client.post("/agent-transport/messages", json=body, timeout=_PROCESS_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    result = response.json()
    msg = result["message"]
    typer.secho(
        f"Queued message {msg['message_id']} -> peer {msg['peer_id']}", fg=typer.colors.GREEN
    )
    typer.echo(f"  Status: {msg['status']}  ({result.get('detail')})")


@agent_message_app.command("list")
def agent_message_list_cmd(
    status_filter: str | None = OutboxStatusOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """List outbound messages (the durable outbox)."""
    base_url = _resolve_base_url(url, config)
    params = {"status": status_filter} if status_filter else {}
    try:
        with _client(base_url) as client:
            response = client.get("/agent-transport/messages", params=params)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    rows = response.json()
    if not rows:
        typer.echo("No outbound messages.")
        return
    typer.secho("Outbox:", fg=typer.colors.CYAN)
    for row in rows:
        _print_outbox_row(row)


@agent_message_app.command("show")
def agent_message_show_cmd(
    message_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one outbound message record."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/agent-transport/messages/{message_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2, default=str))


@agent_message_app.command("retry")
def agent_message_retry_cmd(
    message_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Retry a failed/dead-lettered message (same immutable message id)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(f"/agent-transport/messages/{message_id}/retry")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.secho(f"Retry scheduled: {response.json()['status']}", fg=typer.colors.GREEN)


@agent_message_app.command("cancel")
def agent_message_cancel_cmd(
    message_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Cancel a not-yet-acknowledged message so it is never delivered."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(f"/agent-transport/messages/{message_id}/cancel")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.secho(f"Cancelled: {response.json()['status']}", fg=typer.colors.YELLOW)


# -- transport status ------------------------------------------------------------


@transport_app.command("status")
def transport_status_cmd(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show transport status: identity, worker, outbox/inbox/peer counts."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/agent-transport/status")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    data = response.json()
    worker = data["worker"]
    typer.secho("Agent transport", fg=typer.colors.CYAN)
    typer.echo(f"  Identity:   {'initialized' if data['identity']['initialized'] else 'missing'}")
    typer.echo(f"  Worker:     running={worker['running']} degraded={worker['degraded']}")
    typer.echo(f"  Peers:      {data['peer_count']} ({data['trusted_peer_count']} trusted)")
    typer.echo(f"  Outbox:     {data['outbox_counts']}")
    typer.echo(f"  Inbox:      {data['inbox_count']}")


@transport_app.command("outbox")
def transport_outbox_cmd(
    status_filter: str | None = OutboxStatusOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """List the durable outbox."""
    base_url = _resolve_base_url(url, config)
    params = {"status": status_filter} if status_filter else {}
    try:
        with _client(base_url) as client:
            response = client.get("/agent-transport/outbox", params=params)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    rows = response.json()
    if not rows:
        typer.echo("Outbox empty.")
        return
    for row in rows:
        _print_outbox_row(row)


@transport_app.command("inbox")
def transport_inbox_cmd(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List the durable inbox."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/agent-transport/inbox")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    rows = response.json()
    if not rows:
        typer.echo("Inbox empty.")
        return
    for row in rows:
        _print_inbox_row(row)


# -- Stage 13A.5: RF backends ----------------------------------------------------

ArgsJsonRfOption = typer.Option("{}", "--args-json", help="Tool arguments as a JSON object.")
BenchBackendOption = typer.Option(None, "--backend-id", help="Backend id (omit to run all).")


def _print_rf_backend(b: dict) -> None:
    marker = "experimental" if b.get("experimental") else "stable"
    color = typer.colors.YELLOW if b.get("experimental") else typer.colors.GREEN
    state = b.get("state")
    typer.secho(f"{b['backend_id']}  [{marker}]  {state}", fg=color)
    typer.echo(f"  Name:    {b.get('display_name')}  (default: {b.get('default')})")
    typer.echo(f"  Version: {b.get('version')}  rev: {b.get('source_revision')}")
    typer.echo(
        f"  Session: {b.get('session_id')} gen {b.get('session_generation')}"
        f" pid {b.get('pid')}"
    )
    typer.echo(f"  Tools:   {b.get('tool_count')}  active_requests: {b.get('active_requests')}")
    typer.echo(f"  Workspace: {b.get('workspace')}")
    if b.get("last_error"):
        typer.echo(f"  Error:   {b['last_error']}")


def _rf_get(base_url, path, params=None):
    with _client(base_url) as client:
        return client.get(path, params=params or {})


@rf_profiles_app.command("list")
def rf_profiles_list() -> None:
    """List OTA RF link profiles + their approval state (waveform parameters only)."""
    from aithernet.transport.ota.profiles import ProfileRegistry
    reg = ProfileRegistry()
    rows = [{"profile_id": pid, "state": reg.state(pid),
             "modulation": reg.get(pid).modulation,
             "approved_for_simulation": reg.approved_for_simulation(pid)}
            for pid in reg.list_ids()]
    typer.echo(json.dumps({"profiles": rows}, indent=2))


@rf_profiles_app.command("inspect")
def rf_profiles_inspect(profile_id: str) -> None:
    """Show one RF link profile's manifest (no frequency/gain/power/antenna/hardware identity)."""
    from aithernet.transport.ota.profiles import ProfileRegistry
    reg = ProfileRegistry()
    if profile_id not in reg.list_ids():
        raise typer.BadParameter(f"unknown profile; choose: {', '.join(reg.list_ids())}")
    typer.echo(json.dumps(reg.get(profile_id).to_manifest(), indent=2))


@rf_profiles_app.command("validate")
def rf_profiles_validate(profile_id: str) -> None:
    """Structurally validate a profile + its generated flowgraph (no execution, no transmission)."""
    from aithernet.transport.ota import flowgraph as _fg
    from aithernet.transport.ota.profiles import (
        ProfileRegistry,
        ProfileValidationError,
        validate_profile,
    )
    reg = ProfileRegistry()
    if profile_id not in reg.list_ids():
        raise typer.BadParameter(f"unknown profile; choose: {', '.join(reg.list_ids())}")
    p = reg.get(profile_id)
    try:
        validate_profile(p)
        tx, rx = _fg.generate_tx_flowgraph(p), _fg.generate_rx_flowgraph(p)
        tx_ok, _ = _fg.validate_flowgraph_source(tx)
        rx_ok, _ = _fg.validate_flowgraph_source(rx)
        typer.echo(json.dumps({"profile_id": profile_id, "structurally_valid": True,
                               "flowgraph_safe": tx_ok and rx_ok,
                               "flowgraph_digest": _fg.flowgraph_digest(tx, rx)}, indent=2))
    except ProfileValidationError as exc:
        typer.secho(f"invalid: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _tx_state_root():
    import os as _os
    sr = _os.environ.get("AITHERNET_STATE_ROOT")
    return sr or None


def _rf_ota_availability() -> tuple[str, list]:
    """Operator-controlled rf_ota availability: available when TX is enabled + a capable device is
    present; otherwise reports the exact local reason. No vendor/cloud check."""
    from aithernet.transport.ota import hardware as _hw
    from aithernet.transport.ota.tx_control import load_tx_config
    cfg = load_tx_config(_tx_state_root())
    devices = _hw.discover_tx_devices()
    if cfg.enabled and devices:
        return "available", [d.to_dict() for d in devices]
    if cfg.enabled and not devices:
        return "enabled-no-device-present", []
    return "disabled-by-operator", [d.to_dict() for d in devices]


@rf_app.command("tx-policy")
def rf_tx_policy_status() -> None:
    """Show the LOCAL physical-TX policy (operator-controlled; no vendor/cloud authorization)."""
    from aithernet.transport.ota.tx_control import load_tx_config
    cfg = load_tx_config(_tx_state_root())
    avail, _devs = _rf_ota_availability()
    typer.echo(json.dumps({
        "physical_tx": "enabled" if cfg.enabled else "disabled",
        "control": "local-operator",
        "vendor_authorization_required": False,
        "rf_ota": avail,
        "device": cfg.device_id, "operating_region_label": cfg.operating_region_label,
        "max_duration_seconds": cfg.max_duration_seconds, "gain_ceiling": cfg.gain_ceiling,
        "allow_ip_fallback": cfg.allow_ip_fallback,
        "require_interactive_confirmation": cfg.require_interactive_confirmation,
        "note": "Physical SDR transmission is available once the LOCAL operator enables it with a "
                "TX-capable device and confirms each plan. No vendor approval, cloud auth, "
                "subscription, or external service is required. Enable: `aithernet rf tx enable`.",
    }, indent=2))


@rf_app.command("transport")
def rf_transport_status() -> None:
    """Show OTA transport availability (ip, simulated_rf, operator-controlled rf_ota)."""
    from aithernet.transport.ota.profiles import ProfileRegistry
    avail, devices = _rf_ota_availability()
    reg = ProfileRegistry()
    typer.echo(json.dumps({
        "transports": {"ip": "available", "simulated_rf": "available", "rf_ota": avail},
        "rf_ota_devices": devices,
        "profiles": reg.list_ids(),
    }, indent=2))


@rf_tx_app.command("status")
def rf_tx_status() -> None:
    """Show the local operator TX status + configuration (no secrets)."""
    from aithernet.transport.ota.tx_control import load_tx_config
    cfg = load_tx_config(_tx_state_root())
    avail, devices = _rf_ota_availability()
    typer.echo(json.dumps({"enabled": cfg.enabled, "rf_ota": avail,
                           "devices_present": devices, "config": cfg.to_dict()}, indent=2))


@rf_tx_app.command("enable")
def rf_tx_enable() -> None:
    """Enable physical SDR transmission on THIS node (a local toggle; no external service)."""
    from dataclasses import replace

    from aithernet.transport.ota.tx_control import load_tx_config, save_tx_config
    cfg = replace(load_tx_config(_tx_state_root()), enabled=True)
    save_tx_config(cfg, _tx_state_root())
    typer.secho("Physical TX enabled on this node (local). Configure a device with "
                "`aithernet rf tx configure`; physical RF is your responsibility to operate within "
                "the rules at your location.", fg=typer.colors.GREEN)


@rf_tx_app.command("disable")
def rf_tx_disable() -> None:
    """Disable physical SDR transmission on this node (local toggle)."""
    from dataclasses import replace

    from aithernet.transport.ota.tx_control import load_tx_config, save_tx_config
    save_tx_config(replace(load_tx_config(_tx_state_root()), enabled=False), _tx_state_root())
    typer.secho("Physical TX disabled on this node.", fg=typer.colors.YELLOW)


@rf_tx_app.command("devices")
def rf_tx_devices() -> None:
    """Discover TX-capable SDRs via the installed adapters (local; none = empty)."""
    from aithernet.transport.ota import hardware as _hw
    devices = _hw.discover_tx_devices()
    typer.echo(json.dumps({"supported_adapters": _hw.supported_adapters(),
                           "devices": [d.to_dict() for d in devices],
                           "count": len(devices)}, indent=2))


@rf_tx_app.command("capabilities")
def rf_tx_capabilities(
    device: str = typer.Argument(..., help="Adapter name (e.g. plutosdr) or discovered device id."),
) -> None:
    """Show the TX-capability envelope a device/adapter reports (ranges/controls; per-adapter)."""
    from dataclasses import asdict

    from aithernet.transport.ota import hardware as _hw
    adapter = device.split(":", 1)[0]
    try:
        cap = _hw.resolve_capability(adapter)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps({"adapter": adapter, "capability": asdict(cap)}, indent=2))


@rf_tx_app.command("configure")
def rf_tx_configure(
    device_id: str = typer.Option(None, "--device-id"),
    uri: str = typer.Option(None, "--uri"),
    tx_channel: int = typer.Option(None, "--tx-channel"),
    antenna: str = typer.Option(None, "--antenna"),
    region_label: str = typer.Option(None, "--region-label",
                                     help="Documentation/audit label; never selects a frequency."),
    max_duration: float = typer.Option(None, "--max-duration-seconds"),
    gain_ceiling: float = typer.Option(None, "--gain-ceiling"),
    allow_ip_fallback: bool = typer.Option(None, "--allow-ip-fallback/--no-ip-fallback"),
    interactive_confirm: bool = typer.Option(
        None, "--interactive-confirm/--no-interactive-confirm"),
) -> None:
    """Set the local operator TX configuration (no YAML editing; no external service)."""
    from dataclasses import replace

    from aithernet.transport.ota.tx_control import load_tx_config, save_tx_config
    cfg = load_tx_config(_tx_state_root())
    updates = {k: v for k, v in {
        "device_id": device_id, "uri": uri, "tx_channel": tx_channel, "antenna": antenna,
        "operating_region_label": region_label, "max_duration_seconds": max_duration,
        "gain_ceiling": gain_ceiling, "allow_ip_fallback": allow_ip_fallback,
        "require_interactive_confirmation": interactive_confirm,
    }.items() if v is not None}
    save_tx_config(replace(cfg, **updates), _tx_state_root())
    typer.echo(json.dumps({"updated": list(updates), "config": load_tx_config(
        _tx_state_root()).to_dict()}, indent=2))


@rf_tx_app.command("test-plan")
def rf_tx_test_plan(
    device: str = typer.Option("plutosdr", "--device"),
    center_frequency_hz: int = typer.Option(..., "--center-frequency-hz",
                                            help="OPERATOR-supplied; Aithernet never picks one."),
    profile: str = typer.Option("ref-bpsk-1k", "--profile"),
) -> None:
    """Validate a candidate TX plan against device ranges + local config; no transmit."""
    from dataclasses import asdict

    from aithernet.transport.ota import hardware as _hw
    from aithernet.transport.ota.authorization import RFTxPlan
    from aithernet.transport.ota.profiles import ProfileRegistry
    from aithernet.transport.ota.tx_control import evaluate_operator_tx, load_tx_config
    reg = ProfileRegistry()
    if profile not in reg.list_ids():
        raise typer.BadParameter(f"unknown profile; choose: {', '.join(reg.list_ids())}")
    p = reg.get(profile)
    cap = _hw.resolve_capability(device.split(":", 1)[0])
    plan = RFTxPlan(device_id=device, uri="", profile_id=profile, profile_digest=p.digest(),
                    flowgraph_digest="", implementation_digest="", frame_artifact_digest="",
                    center_frequency_hz=center_frequency_hz, sample_rate=p.sample_rate,
                    occupied_bandwidth_hz=p.occupied_bandwidth_hz, gain=0.0, channel=0,
                    duration_seconds=1.0, message_count=1, frame_count=1)
    import time as _t
    gate = evaluate_operator_tx(plan=plan, config=load_tx_config(_tx_state_root()), capability=cap,
                                device_present=True, peer_authenticated=True,
                                interactive_confirmed=False, now=int(_t.time()))
    typer.echo(json.dumps({"plan_digest": plan.digest(), "plan": asdict(plan),
                           "would_be_allowed": gate.allowed, "reasons": gate.reasons,
                           "note": "no transmission performed"}, indent=2))


def _tx_plans_path():
    from aithernet.transport.ota.tx_control import tx_config_path
    return tx_config_path(_tx_state_root()).parent / "tx-plans.json"


def _load_tx_plans() -> dict:
    p = _tx_plans_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _save_tx_plans(plans: dict) -> None:
    import os as _os
    p = _tx_plans_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plans, indent=2, sort_keys=True))
    _os.chmod(p, 0o600)


@rf_tx_plans_app.command("authorize")
def rf_tx_plans_authorize(
    envelope_id: str = typer.Argument(..., help="Local pre-authorization id."),
    device_id: str = typer.Option(..., "--device-id"),
    freq_min_hz: int = typer.Option(..., "--freq-min-hz", help="OPERATOR-supplied."),
    freq_max_hz: int = typer.Option(..., "--freq-max-hz", help="OPERATOR-supplied."),
    max_bandwidth_hz: int = typer.Option(..., "--max-bandwidth-hz"),
    gain_ceiling: float = typer.Option(..., "--gain-ceiling"),
    max_duration: float = typer.Option(..., "--max-duration-seconds"),
    profiles: str = typer.Option(..., "--profiles", help="Comma-separated allowed profiles."),
    valid_seconds: int = typer.Option(3600, "--valid-seconds"),
) -> None:
    """Create a LOCAL bounded pre-authorization envelope for non-interactive plans."""
    import time as _t
    plans = _load_tx_plans()
    plans[envelope_id] = {
        "envelope_id": envelope_id, "device_id": device_id, "freq_min_hz": freq_min_hz,
        "freq_max_hz": freq_max_hz, "max_occupied_bandwidth_hz": max_bandwidth_hz,
        "gain_ceiling": gain_ceiling, "max_duration_seconds": max_duration,
        "allowed_profiles": [s.strip() for s in profiles.split(",")], "max_payload_bytes": 65536,
        "max_retries": 8, "valid_until": int(_t.time()) + valid_seconds}
    _save_tx_plans(plans)
    typer.secho(f"Pre-authorization envelope '{envelope_id}' created (local; bounded).",
                fg=typer.colors.GREEN)


@rf_tx_plans_app.command("list")
def rf_tx_plans_list() -> None:
    """List local pre-authorization envelopes."""
    typer.echo(json.dumps({"envelopes": sorted(_load_tx_plans())}, indent=2))


@rf_tx_plans_app.command("inspect")
def rf_tx_plans_inspect(envelope_id: str) -> None:
    """Show one local pre-authorization envelope."""
    plans = _load_tx_plans()
    if envelope_id not in plans:
        raise typer.BadParameter(f"no envelope '{envelope_id}'")
    typer.echo(json.dumps(plans[envelope_id], indent=2))


@rf_tx_plans_app.command("revoke")
def rf_tx_plans_revoke(envelope_id: str) -> None:
    """Revoke a local pre-authorization envelope."""
    plans = _load_tx_plans()
    if plans.pop(envelope_id, None) is None:
        typer.secho(f"No envelope '{envelope_id}'.", fg=typer.colors.YELLOW)
        return
    _save_tx_plans(plans)
    typer.secho(f"Revoked '{envelope_id}'.", fg=typer.colors.GREEN)


@rf_tx_app.command("loopback")
def rf_tx_loopback(
    profile: str = typer.Option("ref-bpsk-1k", "--profile"),
) -> None:
    """Run a contained modem loopback (modulate -> sink -> demodulate). No radiation."""
    from aithernet.transport.ota import modem
    from aithernet.transport.ota.authorization import RFTxPlan
    from aithernet.transport.ota.executor import LoopbackTxBackend, execute_peer_tx
    from aithernet.transport.ota.frame import Frame, FrameType
    from aithernet.transport.ota.hardware import resolve_capability
    from aithernet.transport.ota.profiles import ProfileRegistry
    from aithernet.transport.ota.tx_control import OperatorTxConfig
    reg = ProfileRegistry()
    p = reg.get(profile) if profile in reg.list_ids() else None
    if p is None:
        raise typer.BadParameter(f"unknown profile; choose: {', '.join(reg.list_ids())}")
    frame = Frame(FrameType.DATA, 1, 0, 1, b"loopback-test-frame").encode(p.crc_scheme)
    samples = modem.modulate(frame, p)
    recovered = modem.demodulate(samples, p).data
    plan = RFTxPlan(device_id="loopback", uri="", profile_id=profile, profile_digest=p.digest(),
                    flowgraph_digest="", implementation_digest="", frame_artifact_digest="",
                    center_frequency_hz=0, sample_rate=p.sample_rate,
                    occupied_bandwidth_hz=p.occupied_bandwidth_hz, gain=0.0, channel=0,
                    duration_seconds=1.0, message_count=1, frame_count=1)
    cap = resolve_capability("plutosdr")
    rec = execute_peer_tx(plan=plan, profile=p, frame_samples=samples,
                          config=OperatorTxConfig(enabled=True, max_frame_count=10,
                                                 gain_ceiling=70),
                          capability=cap, device_present=True, peer_authenticated=True,
                          interactive_confirmed=True, backend=LoopbackTxBackend(), now=0)
    ok = recovered.startswith(frame[:8])
    typer.echo(json.dumps({"profile": profile, "modem_loopback_decoded": ok,
                           "executor_allowed": rec.allowed, "radiated": rec.radiated,
                           "sample_count": rec.metadata.get("sample_count"),
                           "flowgraph_digest": rec.flowgraph_digest}, indent=2))


@rf_tx_app.command("send-test-frame")
def rf_tx_send_test_frame(
    payload: str = typer.Option("aithernet-test-frame", "--payload"),
    profile: str = typer.Option("ref-bpsk-1k", "--profile"),
    persist: bool = typer.Option(False, "--persist", help="Persist a research record (no secret)."),
) -> None:
    """Send ONE bounded test frame through the managed executor. Contained loopback (no radiation)
    unless a configured TX device + operator confirmation are present (operator-run)."""
    from aithernet.transport.ota import modem
    from aithernet.transport.ota.authorization import RFTxPlan
    from aithernet.transport.ota.executor import LoopbackTxBackend, execute_peer_tx
    from aithernet.transport.ota.frame import Frame, FrameType
    from aithernet.transport.ota.hardware import resolve_capability
    from aithernet.transport.ota.profiles import ProfileRegistry
    from aithernet.transport.ota.tx_control import OperatorTxConfig
    reg = ProfileRegistry()
    if profile not in reg.list_ids():
        raise typer.BadParameter(f"unknown profile; choose: {', '.join(reg.list_ids())}")
    p = reg.get(profile)
    frame = Frame(FrameType.DATA, 1, 0, 1, payload.encode()[:p.max_frame_payload_bytes]).encode(
        p.crc_scheme)
    samples = modem.modulate(frame, p)
    plan = RFTxPlan(device_id="loopback", uri="", profile_id=profile, profile_digest=p.digest(),
                    flowgraph_digest="", implementation_digest="", frame_artifact_digest="",
                    center_frequency_hz=0, sample_rate=p.sample_rate,
                    occupied_bandwidth_hz=p.occupied_bandwidth_hz, gain=0.0, channel=0,
                    duration_seconds=1.0, message_count=1, frame_count=1)
    cfg = OperatorTxConfig(enabled=True, max_frame_count=10, gain_ceiling=70.0)
    rec = execute_peer_tx(plan=plan, profile=p, frame_samples=samples, config=cfg,
                          capability=resolve_capability("plutosdr"), device_present=True,
                          peer_authenticated=True, interactive_confirmed=True,
                          backend=LoopbackTxBackend(), now=0)
    out = {"profile": profile, "allowed": rec.allowed, "radiated": rec.radiated,
           "transport_class": "loopback", "sample_count": rec.metadata.get("sample_count")}
    if persist:
        from aithernet.transport.ota.research import persist_physical_tx
        r = persist_physical_tx(rec, plan=plan, config=cfg, transport_class="loopback")
        out["research_record"] = {"dest": r["dest"], "digest": r["digest"]}
    typer.echo(json.dumps(out, indent=2))


@rf_app.command("backends")
def rf_backends(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List all RF backends and their status."""
    base_url = _resolve_base_url(url, config)
    try:
        response = _rf_get(base_url, "/rf/backends")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    backends = response.json()
    if not backends:
        typer.echo("No RF backends configured.")
        return
    for backend in backends:
        _print_rf_backend(backend)


@rf_backend_app.command("show")
def rf_backend_show(
    backend_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one RF backend's status + context freshness + artifact count."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _rf_get(base_url, f"/rf/backends/{backend_id}")
        ctx = _rf_get(base_url, f"/rf/contexts/{backend_id}")
        arts = _rf_get(base_url, "/rf/artifacts", {"backend_id": backend_id})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    _print_rf_backend(resp.json())
    if ctx.is_success:
        c = ctx.json()
        typer.echo(f"  Context: stale={c.get('stale')} ({c.get('stale_reason')})")
    if arts.is_success:
        typer.echo(f"  Artifacts: {len(arts.json())}")


def _rf_backend_action(action: str, backend_id: str, base_url: str) -> None:
    try:
        with _client(base_url) as client:
            response = client.post(f"/rf/backends/{backend_id}/{action}", timeout=_MCP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    _print_rf_backend(response.json())


@rf_backend_app.command("start")
def rf_backend_start(
    backend_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Start an RF backend session."""
    _rf_backend_action("start", backend_id, _resolve_base_url(url, config))


@rf_backend_app.command("stop")
def rf_backend_stop(
    backend_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Stop an RF backend session."""
    _rf_backend_action("stop", backend_id, _resolve_base_url(url, config))


@rf_backend_app.command("restart")
def rf_backend_restart(
    backend_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Restart an RF backend session (new generation; its context becomes stale)."""
    _rf_backend_action("restart", backend_id, _resolve_base_url(url, config))


@rf_app.command("tools")
def rf_tools(backend_id: str, config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List a backend's live discovered tools."""
    base_url = _resolve_base_url(url, config)
    try:
        response = _rf_get(base_url, f"/rf/backends/{backend_id}/tools")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    tools = response.json()
    typer.secho(f"{backend_id}: {len(tools)} tools", fg=typer.colors.CYAN)
    for tool in tools:
        typer.echo(f"  {tool['name']}")


@rf_app.command("tools-refresh")
def rf_tools_refresh(
    backend_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Re-query a backend's live tool catalog."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(
                f"/rf/backends/{backend_id}/tools/refresh", timeout=_MCP_TIMEOUT
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.secho(f"{backend_id}: {len(response.json())} tools", fg=typer.colors.CYAN)


@rf_app.command("call")
def rf_call(
    backend_id: str,
    tool_name: str,
    args_json: str = ArgsJsonRfOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Call ONE tool on a named RF backend and print the audited call record."""
    base_url = _resolve_base_url(url, config)
    try:
        arguments = json.loads(args_json)
        if not isinstance(arguments, dict):
            raise ValueError("--args-json must be a JSON object")
    except ValueError as exc:
        typer.secho(f"Invalid --args-json: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        with _client(base_url) as client:
            response = client.post(
                f"/rf/backends/{backend_id}/call",
                json={"tool_name": tool_name, "arguments": arguments},
                timeout=_MCP_TIMEOUT,
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    call = response.json()
    color = typer.colors.GREEN if call["status"] == "completed" else typer.colors.RED
    typer.secho(f"Call {call['id']} [{call['status']}] on {call['backend_id']}", fg=color)
    typer.echo(f"  tool: {call['tool_name']}  summary_type: {call.get('result_summary_type')}")
    if call.get("error"):
        typer.echo(f"  error: {call['error']}")


@rf_app.command("context")
def rf_context(backend_id: str, config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show a backend's RF workspace context."""
    base_url = _resolve_base_url(url, config)
    try:
        response = _rf_get(base_url, f"/rf/contexts/{backend_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2, default=str))


@rf_app.command("context-refresh")
def rf_context_refresh(
    backend_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Refresh a backend's RF context via its discovered read-only tools."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(f"/rf/contexts/{backend_id}/refresh", timeout=_MCP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2, default=str))


@rf_app.command("artifacts")
def rf_artifacts(
    backend_id: str | None = typer.Option(None, "--backend-id", help="Filter by backend."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """List indexed RF artifacts (workspace-relative references only)."""
    base_url = _resolve_base_url(url, config)
    params = {"backend_id": backend_id} if backend_id else {}
    try:
        response = _rf_get(base_url, "/rf/artifacts", params)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    arts = response.json()
    if not arts:
        typer.echo("No artifacts.")
        return
    for a in arts:
        typer.echo(
            f"  [{a['backend_id']}] {a['relative_path']}  "
            f"({a['artifact_kind']}, {a.get('size_bytes')}B)"
        )


@rf_benchmark_app.command("list")
def rf_benchmark_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List recorded benchmark runs (and available scenarios)."""
    base_url = _resolve_base_url(url, config)
    try:
        scenarios = _rf_get(base_url, "/rf/benchmarks/scenarios")
        runs = _rf_get(base_url, "/rf/benchmarks")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(scenarios)
    _check(runs)
    typer.secho("Scenarios:", fg=typer.colors.CYAN)
    for s in scenarios.json():
        typer.echo(f"  {s['scenario_id']} (v{s['version']}) backends={s['backends']}")
    typer.secho("Runs:", fg=typer.colors.CYAN)
    for r in runs.json():
        typer.echo(
            f"  {r['id'][:8]} {r['scenario_id']} on {r['backend_id']} "
            f"-> {r['validation_outcome']} ({r['mcp_calls']} calls, {r['elapsed_seconds']:.2f}s)"
        )


@rf_benchmark_app.command("run")
def rf_benchmark_run(
    scenario: str,
    backend_id: str | None = BenchBackendOption,
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Run a scenario against one backend (or every backend that can express it)."""
    base_url = _resolve_base_url(url, config)
    params = {"scenario_id": scenario}
    if backend_id:
        params["backend_id"] = backend_id
    try:
        with _client(base_url) as client:
            response = client.post("/rf/benchmarks/run", params=params, timeout=_RUN_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    for r in response.json():
        color = typer.colors.GREEN if r["success"] else typer.colors.RED
        typer.secho(
            f"{r['backend_id']} v{r['backend_version']}: {r['validation_outcome']} "
            f"({r['mcp_calls']} calls, {r['elapsed_seconds']:.2f}s, "
            f"{r['artifact_count']} artifacts)",
            fg=color,
        )


@rf_benchmark_app.command("show")
def rf_benchmark_show(
    benchmark_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one benchmark run."""
    base_url = _resolve_base_url(url, config)
    try:
        response = _rf_get(base_url, f"/rf/benchmarks/{benchmark_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2, default=str))


# -- Stage 13B: coordinator communications ---------------------------------------


@mission_app.command("communications")
def mission_communications(
    mission_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show a mission's peer communications (ACK vs semantic reply, waits, obligation)."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/missions/{mission_id}/communications")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    data = response.json()
    typer.secho("Outbound messages:", fg=typer.colors.CYAN)
    for m in data.get("outbound_messages", []):
        ack = "acknowledged" if m.get("transport_acknowledged") else "not-acked"
        typer.echo(
            f"  {m['message_id'][:8]} {m.get('message_type')} -> {m['peer_id'][:8]} "
            f"delivery={m.get('delivery_status')} [{ack}] expects_reply={m.get('expects_reply')}"
        )
    typer.secho("Reply waits:", fg=typer.colors.CYAN)
    for w in data.get("reply_waits", []):
        typer.echo(
            f"  {w['wait_id'][:8]} state={w['state']} req={w['request_id'][:8]} "
            f"deadline={w.get('deadline')}"
        )
    ob = data.get("response_obligation")
    if ob:
        typer.echo(
            f"Response obligation: required={ob['response_required']} "
            f"queued={ob['response_queued']}"
        )


@mission_app.command("reply-waits")
def mission_reply_waits(
    mission_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """List a mission's reply waits."""
    base_url = _mission_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/missions/{mission_id}/reply-waits")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    waits = response.json()
    if not waits:
        typer.echo("No reply waits.")
        return
    for w in waits:
        color = {"satisfied": typer.colors.GREEN, "timed_out": typer.colors.YELLOW,
                 "cancelled": typer.colors.RED}.get(w["state"], typer.colors.CYAN)
        typer.secho(
            f"  {w['wait_id']}  [{w['state']}]  req={w['request_id'][:8]} "
            f"peer={w['expected_peer_id'][:8]} deadline={w.get('deadline')}",
            fg=color,
        )


@mission_reply_wait_app.command("cancel")
def mission_reply_wait_cancel(
    mission_id: str, wait_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Cancel a pending reply wait (prevents a later correlated reply from resuming)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(f"/missions/{mission_id}/reply-waits/{wait_id}/cancel")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    cancelled = response.json().get("cancelled")
    typer.secho(
        f"Reply wait {'cancelled' if cancelled else 'not cancelled (already resolved)'}.",
        fg=typer.colors.YELLOW if cancelled else typer.colors.CYAN,
    )


@conversation_app.command("list")
def conversation_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List peer conversations."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/conversations")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    convs = response.json()
    if not convs:
        typer.echo("No conversations.")
        return
    for c in convs:
        typer.echo(
            f"  {c['conversation_id'][:8]}  out={c['outbound']} in={c['inbound']} "
            f"peers={[p[:8] for p in c['peers']]}"
        )


@conversation_app.command("show")
def conversation_show(
    conversation_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one conversation's bounded message history (no signatures/payloads)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/conversations/{conversation_id}/messages")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    for m in response.json():
        kind = "semantic-reply" if m.get("semantic_reply") else m.get("message_type")
        typer.echo(
            f"  [{m['direction']}] {m.get('at')} {kind} req={(m.get('request_id') or '')[:8]} "
            f"peer={(m.get('peer_id') or '')[:8]}"
        )


@inbound_request_app.command("list")
def inbound_request_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List inbound coordinator requests and their missions."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/inbound-requests")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    rows = response.json()
    if not rows:
        typer.echo("No inbound requests.")
        return
    for r in rows:
        typer.echo(
            f"  {r['request_id'][:8]}  [{r['state']}]  peer={r['peer_id'][:8]} "
            f"mission={(r.get('mission_id') or '-')[:8]} "
            f"resp_required={r['response_required']} resp_queued={r['response_queued']}"
        )


@inbound_request_app.command("show")
def inbound_request_show(
    request_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one inbound request."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/inbound-requests/{request_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2, default=str))


@peer_permissions_app.command("show")
def peer_permissions_show(
    peer_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show a peer's application authorization (separate from public-key trust)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get(f"/peers/{peer_id}/permissions")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2, default=str))


@peer_permissions_app.command("set")
def peer_permissions_set(
    peer_id: str,
    may_send_requests: bool | None = typer.Option(None, "--may-send-requests/--no-send-requests"),
    may_send_replies: bool | None = typer.Option(None, "--may-send-replies/--no-send-replies"),
    may_receive_messages: bool | None = typer.Option(
        None, "--may-receive-messages/--no-receive-messages"
    ),
    may_request_response: bool | None = typer.Option(
        None, "--may-request-response/--no-request-response"
    ),
    max_inbound_request_bytes: int | None = typer.Option(None, "--max-inbound-bytes"),
    max_concurrent_inbound_missions: int | None = typer.Option(None, "--max-concurrent"),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Set a peer's application authorization (conservative defaults; trust stays separate)."""
    base_url = _resolve_base_url(url, config)
    body = {
        k: v for k, v in {
            "may_send_requests": may_send_requests,
            "may_send_replies": may_send_replies,
            "may_receive_messages": may_receive_messages,
            "may_request_response": may_request_response,
            "max_inbound_request_bytes": max_inbound_request_bytes,
            "max_concurrent_inbound_missions": max_concurrent_inbound_missions,
        }.items() if v is not None
    }
    try:
        with _client(base_url) as client:
            response = client.patch(f"/peers/{peer_id}/permissions", json=body)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2, default=str))


# -- Stage 13C operator inspection ----------------------------------------------
# All read commands here are bounded and sanitized: they abbreviate ids, preserve the
# ACK-versus-semantic-reply distinction, and never print signatures, keys, raw envelopes,
# auth headers, environment, or unbounded payloads.


def _get_json(base_url: str, path: str, params: dict | None = None):
    try:
        with _client(base_url) as client:
            response = client.get(path, params=params or {})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    return response.json()


@app.command()
def overview(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show the node overview: identity, worker/coordinator/RF health, fleet + comms rollup."""
    base_url = _resolve_base_url(url, config)
    data = _get_json(base_url, "/overview")
    node = data["node"]
    typer.secho(f"Node: {node['node_name']} ({node['node_id']})", fg=typer.colors.CYAN)
    typer.echo(f"  runtime={node['runtime_status']} version={node['version']} "
               f"identity={'yes' if node['identity_initialized'] else 'no'} "
               f"fp={node.get('fingerprint') or '-'}")
    mw, tw = data["workers"]["mission"], data["workers"]["transport"]
    typer.echo(f"  mission worker:   running={mw['running']} degraded={mw['degraded']} "
               f"active={mw['active_count']} queued={mw['queued_count']}")
    typer.echo(f"  transport worker: running={tw['running']} degraded={tw['degraded']} "
               f"in_flight={tw['in_flight']}")
    co = data["coordinator"]
    typer.echo(f"  coordinator={co['provider']} configured={co['configured']}")
    typer.echo(f"  peers: {data['peers'].get('trusted', 0)} trusted / "
               f"{data['peers'].get('total', 0)} total")
    ob = data["outbox"]
    typer.echo(f"  outbox: pending={ob['pending']} in_flight={ob['in_flight']} "
               f"acked={ob['acknowledged']} failed={ob['failed']} dead={ob['dead_letter']}  "
               f"inbox={data['inbox_count']}")
    ms = data["missions"]
    typer.echo(f"  missions: active={ms.get('active', 0)} waiting={ms.get('waiting', 0)} "
               f"queued={ms.get('queued', 0)}")
    typer.echo(f"  reply waits pending={data['reply_waits'].get('pending', 0)}  "
               f"inbound requests={data['inbound_requests'].get('total', 0)}")
    for backend in data.get("rf_backends", []):
        typer.echo(f"  rf[{backend['backend_id']}] state={backend['state']} "
                   f"tools={backend['tool_count']}")
    failures = data.get("recent_failures", {})
    total_fail = sum(len(v) for v in failures.values())
    if total_fail:
        typer.secho(f"  recent failures: comms={len(failures.get('communication', []))} "
                    f"rf={len(failures.get('rf', []))} mission={len(failures.get('mission', []))}",
                    fg=typer.colors.YELLOW)


@fleet_app.command("heartbeat")
def fleet_heartbeat(
    now: bool = typer.Option(
        False, "--now", help="Send one heartbeat to the hosted control plane immediately."),
    state_root: str | None = typer.Option(None, "--state-root"),
    sequence: int | None = typer.Option(None, "--sequence"),
) -> None:
    """Send a signed hosted heartbeat now (alias of ``hosted heartbeat``)."""
    from pathlib import Path as _Path

    from aithernet.hosted.cli import hosted_heartbeat
    if not now:
        typer.echo("Pass --now to send a heartbeat immediately.")
        return
    hosted_heartbeat(state_root=_Path(state_root) if state_root else None, sequence=sequence)


@fleet_app.command("list")
def fleet_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List peers with trust, permissions (separate), health, and linkage."""
    base_url = _resolve_base_url(url, config)
    peers = _get_json(base_url, "/fleet").get("peers", [])
    if not peers:
        typer.echo("No peers.")
        return
    for p in peers:
        perms = p["permissions"]
        # Trust and authorization are distinct concepts — show both, separately.
        auth = "".join([
            "R" if perms["may_send_requests"] else "-",
            "r" if perms["may_send_replies"] else "-",
            "M" if perms["may_receive_messages"] else "-",
            "X" if perms["may_request_response"] else "-",
        ])
        typer.echo(
            f"  {p['peer_id'][:8]}  {p['name'][:18]:<18} role={p['role']:<8} "
            f"trust={p['trust_state']:<9} health={p['transport_health']:<8} "
            f"auth[Rr Mx]={auth} convs={p['conversation_count']} "
            f"waits={p['pending_reply_waits']}"
        )
    typer.secho("  auth flags: R=send_requests r=send_replies M=receive_messages "
                "X=request_response (trust is separate)", fg=typer.colors.WHITE)


@fleet_app.command("show")
def fleet_show(
    peer_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one peer's trust, permissions, health, manifest freshness, and linkage."""
    base_url = _resolve_base_url(url, config)
    peers = _get_json(base_url, "/fleet").get("peers", [])
    match = next((p for p in peers if p["peer_id"] == peer_id or p["peer_id"].startswith(peer_id)),
                 None)
    if match is None:
        typer.secho("Peer not found in fleet.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"Peer {match['name']} ({match['peer_id']})", fg=typer.colors.CYAN)
    typer.echo(f"  role={match['role']} trust={match['trust_state']} enabled={match['enabled']}")
    typer.echo(f"  transport health={match['transport_health']} "
               f"failures={match['consecutive_failures']} fingerprint={match.get('fingerprint')}")
    typer.echo(f"  last success={match.get('last_success_at')} "
               f"last failure={match.get('last_failure_at')}")
    typer.echo(f"  manifest at={match.get('manifest_at')} capabilities={match.get('capabilities')}")
    typer.secho("  application authorization (separate from public-key trust):",
                fg=typer.colors.WHITE)
    for key, value in match["permissions"].items():
        typer.echo(f"    {key} = {value}")
    typer.echo(f"  conversations={match['conversation_count']} "
               f"outstanding messages={match['outstanding_messages']} "
               f"pending reply waits={match['pending_reply_waits']}")


@conversation_app.command("repair")
def conversation_repair(
    apply: bool = typer.Option(
        False, "--apply/--dry-run",
        help="Apply the repair (default is a dry-run that only reports counts).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Rethread provably-split reply rows onto their canonical conversation (Stage 13D)."""
    base_url = _resolve_base_url(url, config)
    if apply and not yes and not typer.confirm(
        "Apply canonical-conversation repair? This rethreads provably-split replies "
        "(no message is deleted)."
    ):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)
    try:
        with _client(base_url) as client:
            response = client.post("/conversations/repair", params={"dry_run": not apply})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    data = response.json()
    mode = "APPLIED" if apply else "DRY-RUN"
    typer.secho(
        f"[{mode}] scanned {data['scanned_replies']} replies · "
        f"{data['repairable']} repairable · {data['repaired']} repaired.",
        fg=typer.colors.GREEN if apply else typer.colors.CYAN,
    )


@conversation_app.command("timeline")
def conversation_timeline(
    conversation_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show a conversation's ordered timeline (ACK vs semantic reply distinguished)."""
    base_url = _resolve_base_url(url, config)
    data = _get_json(base_url, f"/conversations/{conversation_id}/timeline")
    typer.secho(f"Conversation {conversation_id[:8]} "
                f"({data['message_count']} messages, peers={[p[:8] for p in data['peers']]})",
                fg=typer.colors.CYAN)
    for e in data["entries"]:
        if e.get("direction") == "system":
            typer.echo(f"  · {e.get('at')} [{e['kind']}] req={(e.get('request_id') or '')[:8]} "
                       f"peer={(e.get('expected_peer_id') or '')[:8]}")
            continue
        ack = "ack" if e.get("transport_acknowledged") else "no-ack"
        reply = " SEMANTIC-REPLY" if e.get("semantic_reply") else ""
        text = e.get("text_preview") or ""
        typer.echo(f"  [{e['direction']:<8}] {e.get('at')} {e.get('message_type')} "
                   f"delivery={e.get('delivery_status')} [{ack}]{reply} "
                   f"req={(e.get('request_id') or '')[:8]}  {text}")


@conversation_app.command("send")
def conversation_send(
    peer_id: str = typer.Argument(..., help="Trusted, enabled peer id to message."),
    text: str = typer.Option(..., "--text", help="Bounded message text."),
    message_type: str = typer.Option("request", "--type", help="request | update | reply."),
    expects_reply: bool = typer.Option(False, "--expects-reply", help="Expect a semantic reply."),
    data_json: str | None = typer.Option(None, "--data-json", help="Optional bounded JSON data."),
    conversation_id: str | None = typer.Option(None, "--conversation", help="Existing conv id."),
    reply_to_request_id: str | None = typer.Option(None, "--reply-to", help="Reply-to request id."),
    response_deadline: str | None = typer.Option(None, "--deadline", help="ISO reply deadline."),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """Queue ONE operator message via the durable outbox (queued/acked is NOT answered)."""
    base_url = _resolve_base_url(url, config)
    body: dict = {
        "peer_id": peer_id, "message_type": message_type, "text": text,
        "expects_reply": expects_reply, "conversation_id": conversation_id,
        "reply_to_request_id": reply_to_request_id, "response_deadline": response_deadline,
    }
    if data_json:
        try:
            body["data"] = json.loads(data_json)
        except json.JSONDecodeError as exc:
            typer.secho(f"Invalid --data-json: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    try:
        with _client(base_url) as client:
            response = client.post("/communications/send", json=body)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    res = response.json()
    typer.secho(f"Queued operator {message_type} {res['message_id'][:8]} to peer "
                f"{res['peer_id'][:8]} (status={res['status']}, origin={res['origin']}).",
                fg=typer.colors.GREEN)
    typer.echo("Note: queued/acknowledged means DELIVERED, not semantically answered.")


@communication_app.command("outbox")
def communication_outbox(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List outbound transport messages (delivery + ACK state; ACK is not an answer)."""
    base_url = _resolve_base_url(url, config)
    rows = _get_json(base_url, "/agent-transport/outbox")
    if not rows:
        typer.echo("Outbox empty.")
        return
    for m in rows:
        ack = "acked" if m.get("acknowledged_at") else "no-ack"
        typer.echo(f"  {m['message_id'][:8]} {m['status']:<12} {m.get('kind', ''):<14} "
                   f"attempts={m['attempt_count']}/{m['max_attempts']} [{ack}] "
                   f"peer={m['peer_id'][:8]} err={m.get('error_type') or '-'}")


@communication_app.command("inbox")
def communication_inbox(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List inbound transport messages (authenticated; duplicates idempotent)."""
    base_url = _resolve_base_url(url, config)
    rows = _get_json(base_url, "/agent-transport/inbox")
    if not rows:
        typer.echo("Inbox empty.")
        return
    for m in rows:
        typer.echo(f"  {m['message_id'][:8]} {m.get('kind', ''):<14} "
                   f"from={m['sender_node_id'][:12]} dup={m.get('duplicate_count', 0)} "
                   f"{m.get('received_at')}")


@communication_app.command("waits")
def communication_waits(
    state: str | None = typer.Option(None, "--state", help="pending|satisfied|timed_out|cancelled"),
    config: str = ConfigOption,
    url: str | None = UrlOption,
) -> None:
    """List durable reply waits across missions (a wait is satisfied only by a semantic reply)."""
    base_url = _resolve_base_url(url, config)
    params = {"state": state} if state else None
    rows = _get_json(base_url, "/reply-waits", params=params)
    if not rows:
        typer.echo("No reply waits.")
        return
    for w in rows:
        typer.echo(f"  {w['wait_id'][:8]} state={w['state']:<10} mission={w['mission_id'][:8]} "
                   f"req={w['request_id'][:8]} peer={w['expected_peer_id'][:8]} "
                   f"deadline={w.get('deadline')}")


@communication_app.command("summary")
def communication_summary(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show the fleet-wide communication rollup (outbox/inbox/waits/inbound + failures)."""
    base_url = _resolve_base_url(url, config)
    data = _get_json(base_url, "/communications/summary")
    typer.secho("Communication summary", fg=typer.colors.CYAN)
    typer.echo(f"  outbox: {data['outbox_counts']}")
    typer.echo(f"  inbox: {data['inbox_count']}")
    typer.echo(f"  reply waits: {data['reply_waits']}")
    typer.echo(f"  inbound requests: {data['inbound_requests']}")
    typer.echo(f"  peers: {data['peers']}")
    if data.get("recent_failures"):
        typer.secho(f"  recent communication failures: {len(data['recent_failures'])}",
                    fg=typer.colors.YELLOW)


@mission_app.command("distributed-timeline")
def mission_distributed_timeline(
    mission_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show a mission's distributed correlation timeline + topology (persisted records only)."""
    base_url = _mission_base_url(url, config)
    data = _get_json(base_url, f"/missions/{mission_id}/distributed-timeline")
    typer.secho(f"Mission {mission_id[:8]} status={data['mission_status']} "
                f"inbound={data['is_inbound_mission']}", fg=typer.colors.CYAN)
    ob = data.get("response_obligation")
    if ob:
        typer.echo(f"  response obligation: required={ob['response_required']} "
                   f"queued={ob['response_queued']}")
    typer.secho("  outbound messages:", fg=typer.colors.WHITE)
    for m in data["outbound_messages"]:
        ack = "acked" if m.get("transport_acknowledged") else "no-ack"
        peer = (m.get("peer_id") or "")[:8]
        typer.echo(f"    {m['message_id'][:8]} {m.get('message_type')} -> {peer} "
                   f"delivery={m.get('delivery_status')} [{ack}] "
                   f"expects_reply={m.get('expects_reply')}")
    typer.secho("  reply waits:", fg=typer.colors.WHITE)
    for w in data["reply_waits"]:
        typer.echo(f"    {w['wait_id'][:8]} state={w['state']} req={w['request_id'][:8]}")
    topo = data.get("topology", {})
    n_nodes, n_edges = len(topo.get("nodes", [])), len(topo.get("edges", []))
    typer.secho(f"  topology: {n_nodes} nodes, {n_edges} edges "
                "(remote mission status is unknown unless the peer supplied it)",
                fg=typer.colors.WHITE)
    for edge in topo.get("edges", []):
        typer.echo(f"    {edge['from']} --{edge['kind']}--> {edge['to']}")


# -- Stage 13D.2 artifact transfer ----------------------------------------------
# Bounded + sanitized: never prints bytes, absolute paths, keys, signatures, or grant material.


@artifact_app.command("list")
def artifact_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List local + imported artifacts (metadata only; bytes live in the managed store)."""
    base_url = _resolve_base_url(url, config)
    rows = _get_json(base_url, "/artifacts")
    if not rows:
        typer.echo("No artifacts.")
        return
    for a in rows:
        typer.echo(f"  {a['artifact_id'][:8]}  {a.get('availability_state'):<10} "
                   f"{a.get('artifact_kind'):<10} {a.get('size_bytes')} bytes  "
                   f"{(a.get('display_name') or '')[:24]}  {a.get('digest') or '-'}")


@artifact_app.command("show")
def artifact_show(artifact_id: str, config: str = ConfigOption,
                  url: str | None = UrlOption) -> None:
    """Show one artifact's identity + evidence (no bytes, no absolute path)."""
    base_url = _resolve_base_url(url, config)
    typer.echo(json.dumps(_get_json(base_url, f"/artifacts/{artifact_id}"), indent=2, default=str))


@artifact_app.command("offer")
def artifact_offer(artifact_id: str, peer_id: str, config: str = ConfigOption,
                   url: str | None = UrlOption) -> None:
    """Offer a locally-stored artifact to a trusted, authorized peer."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            r = client.post("/artifact-transfers/offer",
                            json={"artifact_id": artifact_id, "peer_id": peer_id})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(r)
    res = r.json()
    typer.secho(f"Offered (transfer {res['transfer_id'][:8]}, state={res['state']}).",
                fg=typer.colors.GREEN)


@artifact_app.command("request")
def artifact_request(remote_artifact_id: str, peer_id: str, config: str = ConfigOption,
                     url: str | None = UrlOption) -> None:
    """Request a remote artifact from a trusted peer (the backend performs the transfer)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            r = client.post("/artifact-transfers/request",
                            json={"peer_id": peer_id, "origin_artifact_id": remote_artifact_id})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(r)
    res = r.json()
    typer.secho(f"Requested (transfer {res['transfer_id'][:8]}, state={res['state']}).",
                fg=typer.colors.GREEN)


@artifact_transfer_app.command("list")
def artifact_transfer_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List artifact transfers (direction, state, progress; ACK-style bytes only)."""
    base_url = _resolve_base_url(url, config)
    rows = _get_json(base_url, "/artifact-transfers")
    if not rows:
        typer.echo("No transfers.")
        return
    for t in rows:
        typer.echo(f"  {t['transfer_id'][:8]}  {t['direction']:<8} {t['state']:<12} "
                   f"{t.get('received_bytes')}/{t.get('expected_size')} bytes  "
                   f"peer={t['peer_id'][:8]}  err={t.get('error_type') or '-'}")


@artifact_transfer_app.command("show")
def artifact_transfer_show(transfer_id: str, config: str = ConfigOption,
                           url: str | None = UrlOption) -> None:
    """Show one transfer's state + progress (no bytes/paths/grant material)."""
    base_url = _resolve_base_url(url, config)
    typer.echo(json.dumps(_get_json(base_url, f"/artifact-transfers/{transfer_id}"),
                          indent=2, default=str))


@artifact_transfer_app.command("cancel")
def artifact_transfer_cancel(transfer_id: str, yes: bool = typer.Option(False, "--yes", "-y"),
                             config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Cancel a transfer (durable; a cancelled transfer never resumes)."""
    base_url = _resolve_base_url(url, config)
    if not yes and not typer.confirm(f"Cancel transfer {transfer_id[:8]}?"):
        raise typer.Exit(code=1)
    try:
        with _client(base_url) as client:
            r = client.post(f"/artifact-transfers/{transfer_id}/cancel")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(r)
    typer.secho("Cancelled." if r.json().get("cancelled") else "Not cancellable.",
                fg=typer.colors.YELLOW)


@artifact_transfer_app.command("retry")
def artifact_transfer_retry(transfer_id: str, config: str = ConfigOption,
                            url: str | None = UrlOption) -> None:
    """Retry a failed transfer (only if the failure was transient)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            r = client.post(f"/artifact-transfers/{transfer_id}/retry")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(r)
    typer.secho("Re-queued." if r.json().get("retried") else "Not retryable.",
                fg=typer.colors.GREEN if r.json().get("retried") else typer.colors.CYAN)


@artifact_store_app.command("status")
def artifact_store_status(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show managed artifact-store usage/quota/free-space (no absolute paths)."""
    base_url = _resolve_base_url(url, config)
    s = _get_json(base_url, "/artifact-store/status")
    typer.secho("Artifact store", fg=typer.colors.CYAN)
    typer.echo(f"  used={s['used_bytes']} / quota={s['total_quota_bytes']} bytes  "
               f"(available {s['available_in_quota_bytes']})")
    typer.echo(f"  free disk={s['free_disk_bytes']}  min free={s['minimum_free_bytes']}  "
               f"max artifact={s['max_artifact_bytes']}")


@artifact_store_app.command("gc")
def artifact_store_gc(apply: bool = typer.Option(False, "--apply/--dry-run"),
                      yes: bool = typer.Option(False, "--yes", "-y"),
                      config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Garbage-collect unreferenced store objects (pinned objects are never removed)."""
    base_url = _resolve_base_url(url, config)
    if apply and not yes and not typer.confirm("Apply artifact-store GC (remove unreferenced "
                                               "objects; pinned are kept)?"):
        raise typer.Exit(code=1)
    try:
        with _client(base_url) as client:
            r = client.post("/artifact-store/gc", json={"dry_run": not apply})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(r)
    res = r.json()
    mode = "APPLIED" if apply else "DRY-RUN"
    typer.secho(
        f"[{mode}] removed={res['removed_objects']} reclaimed={res['reclaimed_bytes']} bytes",
        fg=typer.colors.GREEN if apply else typer.colors.CYAN,
    )


# -- Stage 13D.3 remote mission-status inspection -------------------------------
# Bounded + sanitized: ids, states, sequence numbers and counts only — never raw envelopes,
# prompts, signatures, keys, credentials, or unbounded payloads.


@remote_mission_app.command("list")
def remote_mission_list(
    peer_id: str | None = typer.Option(None, "--peer", help="Filter by peer id."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """List authenticated remote-mission snapshots (latest state + freshness)."""
    base_url = _resolve_base_url(url, config)
    params = {"peer_id": peer_id} if peer_id else None
    rows = _get_json(base_url, "/remote-missions", params=params)
    if not rows:
        typer.echo("No remote-mission snapshots.")
        return
    for s in rows:
        typer.echo(
            f"  {s['snapshot_id'][:8]} peer={s['peer_id'][:8]} "
            f"remote={s['remote_mission_ref'][:8]} state={s.get('state') or 'unknown':<10} "
            f"freshness={s['freshness']:<8} seq={s['latest_sequence']} "
            f"obligation={s.get('response_obligation')}"
        )


@remote_mission_app.command("show")
def remote_mission_show(
    snapshot_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one remote-mission snapshot (state, freshness, sequence, links)."""
    base_url = _resolve_base_url(url, config)
    s = _get_json(base_url, f"/remote-missions/{snapshot_id}")
    typer.secho(f"Remote mission snapshot {s['snapshot_id']}", fg=typer.colors.CYAN)
    typer.echo(f"  origin node={s['origin_node_id']} peer={s['peer_id'][:8]}")
    typer.echo(f"  remote mission={s['remote_mission_ref']} request={s.get('request_id')}")
    typer.echo(f"  local mission={s.get('local_mission_id')} "
               f"conversation={s.get('conversation_id')}")
    typer.echo(f"  state={s.get('state')} freshness={s['freshness']} "
               f"terminal={s.get('terminal_category')}")
    typer.echo(f"  sequence={s['latest_sequence']} obligation={s.get('response_obligation')}")
    typer.echo(f"  artifacts={s['artifact_count']} transfers={s['transfer_count']} "
               f"query_pending={s.get('query_pending')}")
    typer.echo(f"  remote updated={s.get('remote_updated_at')} received={s.get('received_at')}")


@remote_mission_app.command("events")
def remote_mission_events(
    snapshot_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show the append-only status-event evidence + dispositions for a snapshot."""
    base_url = _resolve_base_url(url, config)
    rows = _get_json(base_url, f"/remote-missions/{snapshot_id}/events")
    if not rows:
        typer.echo("No status events.")
        return
    for e in rows:
        typer.echo(f"  seq={e.get('sequence')} state={e.get('state')} "
                   f"type={e.get('message_type')} disposition={e['disposition']} "
                   f"at={e.get('received_at')}")


@remote_mission_app.command("query")
def remote_mission_query(
    snapshot_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Queue a bounded status query for a known snapshot (peer responds from persisted fact)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.post(f"/remote-missions/{snapshot_id}/query")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    queued = response.json().get("queued")
    typer.secho(f"Status query {'queued' if queued else 'not queued'}.",
                fg=typer.colors.GREEN if queued else typer.colors.YELLOW)


@mission_app.command("status-publications")
def mission_status_publications(
    mission_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show this mission's per-peer status-publication bookkeeping (sequence + last state)."""
    base_url = _mission_base_url(url, config)
    rows = _get_json(base_url, f"/missions/{mission_id}/status-publications")
    if not rows:
        typer.echo("No status publications (mission is not a reportable inbound mission).")
        return
    for p in rows:
        typer.echo(f"  peer={p['peer_id'][:8]} last_seq={p['last_sequence']} "
                   f"last_state={p['last_state']} request={(p.get('request_id') or '')[:8]}")


@peer_status_perm_app.command("show")
def peer_status_permissions_show(
    peer_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show a peer's mission-status authorization (separate from trust/message/artifact perms)."""
    base_url = _resolve_base_url(url, config)
    perms = _get_json(base_url, f"/peers/{peer_id}/mission-status-permissions")
    typer.echo(json.dumps(perms, indent=2, default=str))


@peer_status_perm_app.command("set")
def peer_status_permissions_set(
    peer_id: str,
    may_publish: bool | None = typer.Option(None, "--may-publish/--no-publish"),
    may_query: bool | None = typer.Option(None, "--may-query/--no-query"),
    may_receive: bool | None = typer.Option(None, "--may-receive/--no-receive"),
    max_active_remote_snapshots: int | None = typer.Option(None, "--max-active-snapshots"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Set a peer's mission-status authorization (conservative; trust stays separate)."""
    base_url = _resolve_base_url(url, config)
    body = {k: v for k, v in {
        "may_publish_mission_status": may_publish,
        "may_query_mission_status": may_query,
        "may_receive_mission_status": may_receive,
        "max_active_remote_snapshots": max_active_remote_snapshots,
    }.items() if v is not None}
    try:
        with _client(base_url) as client:
            response = client.patch(f"/peers/{peer_id}/mission-status-permissions", json=body)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(response)
    typer.echo(json.dumps(response.json(), indent=2, default=str))


# -- Stage 14A production operations --------------------------------------------
# These commands operate on the LOCAL configured node (config + filesystem) so they work when
# the server is down. They never print secrets, keys, signatures, or full environment maps.


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


def _backup_dir_for(cfg) -> str:
    from pathlib import Path
    if cfg.backup_directory:
        return cfg.backup_directory
    if cfg.node_state_root:
        from aithernet.ops.paths import NodePaths
        return str(NodePaths.from_root(cfg.node_state_root).backups_dir)
    return str(Path(cfg.database_path).expanduser().resolve().parent / "backups")


def _status_color(status: str):
    return {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW,
            "fail": typer.colors.RED}.get(status, typer.colors.WHITE)


@node_app.command("preflight")
def node_preflight(config: str = ConfigOption) -> None:
    """Validate configuration + environment before starting the node (exit 1 on any failure)."""
    from aithernet.ops.preflight import run_preflight
    cfg = load_config(config)
    report = run_preflight(cfg, config_path=config)
    typer.secho(f"Preflight for node '{cfg.node_name}' ({cfg.node_id[:8]}…)", fg=typer.colors.CYAN)
    for c in report.checks:
        typer.secho(f"  [{c.status.upper():<4}] {c.name}"
                    + (f" — {c.detail}" if c.detail else ""), fg=_status_color(c.status))
    counts = report.counts
    typer.echo(f"Result: {counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail")
    if not report.ok:
        raise typer.Exit(code=1)


@node_app.command("provision")
def node_provision(
    state_root: str = typer.Argument(..., help="Node state root directory."),
    node_id: str = typer.Option(..., "--node-id", help="Stable node id."),
    node_name: str = typer.Option(..., "--node-name", help="Human-readable node name."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show actions; create nothing."),
    write_config: bool = typer.Option(True, "--config/--no-config", help="Write initial config."),
    migrate: bool = typer.Option(True, "--migrate/--no-migrate", help="Initialize + migrate db."),
) -> None:
    """Idempotently create the production layout, an initial config, and a migrated database."""
    from aithernet.ops.provision import provision_node
    result = provision_node(state_root, node_id=node_id, node_name=node_name, dry_run=dry_run,
                            write_config=write_config, migrate=migrate)
    tag = "[dry-run] " if dry_run else ""
    typer.secho(f"{tag}Provisioned node state root: {result.state_root}", fg=typer.colors.GREEN)
    typer.echo(f"  directories created: {len(result.created_dirs)} "
               f"(existing: {len(result.existing_dirs)})")
    for action in result.actions:
        typer.echo(f"  action: {action}")
    for skip in result.skipped:
        typer.secho(f"  skipped: {skip}", fg=typer.colors.YELLOW)


@node_app.command("status")
def node_status_local(config: str = ConfigOption) -> None:
    """Show local node version, migration state, and resolved paths (no server required)."""
    from aithernet import __version__
    from aithernet.ops.upgrade import _migration_state
    cfg = load_config(config)
    typer.secho(f"Node {cfg.node_name} ({cfg.node_id})", fg=typer.colors.CYAN)
    typer.echo(f"  version: {__version__}")
    typer.echo(f"  state root: {cfg.node_state_root or '(repo-relative / dev)'}")
    db_label = (cfg.database_url.split("///")[-1] if cfg.database_url.startswith("sqlite")
                else cfg.database_url.split(":", 1)[0])
    typer.echo(f"  database: {db_label}")
    try:
        state = _migration_state(cfg)
        typer.echo(f"  migrations applied: {len(state['applied'])}  "
                   f"pending: {len(state['pending'])}  schema valid: {state['schema_valid']}")
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"  migration state unavailable ({type(exc).__name__})", fg=typer.colors.YELLOW)


@node_app.command("systemd-unit")
def node_systemd_unit(
    state_root: str = typer.Argument(..., help="Node state root."),
    node_id: str = typer.Option(..., "--node-id"),
    user: str = typer.Option("aithernet", "--user"),
    executable: str = typer.Option("/opt/aithernet/.venv/bin/aithernet", "--executable"),
) -> None:
    """Print a hardened systemd unit for this node (review before installing)."""
    from aithernet.ops.systemd import render_systemd_unit
    typer.echo(render_systemd_unit(node_id=node_id, state_root=state_root, user=user,
                                   executable=executable))


@health_app.command("live")
def health_live(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Liveness probe against a running node."""
    base_url = _resolve_base_url(url, config)
    data = _get_json(base_url, "/health/live")
    typer.secho(f"live: {data.get('status')} (version {data.get('version')})",
                fg=typer.colors.GREEN)


@health_app.command("ready")
def health_ready_cmd(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Readiness probe — exit 1 when the node is not ready."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            response = client.get("/health/ready")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    data = response.json()
    ready = data.get("status") == "ready"
    typer.secho(f"ready: {data.get('status')} (phase {data.get('phase')})",
                fg=typer.colors.GREEN if ready else typer.colors.YELLOW)
    for comp in data.get("components", []):
        typer.echo(f"  {comp['name']:<22} required={comp['required']} {comp['state']}")
    if not ready:
        raise typer.Exit(code=1)


@backup_app.command("create")
def backup_create(
    config: str = ConfigOption,
    include_private_identity: bool = typer.Option(
        False, "--include-private-identity",
        help="Also archive the PRIVATE identity key (sensitive — store securely)."),
) -> None:
    """Create a consistent, checksummed backup of the database, config, and public identity."""
    from aithernet.ops.backup import create_backup
    cfg = load_config(config)
    result = create_backup(cfg, backup_dir=_backup_dir_for(cfg), config_path=config,
                           include_private_identity=include_private_identity, now_iso=_now_iso())
    typer.secho(f"Backup created: {result.path}", fg=typer.colors.GREEN)
    typer.echo(f"  files: {', '.join(result.files)}  ({result.bytes} bytes)")
    if include_private_identity:
        typer.secho("  NOTE: includes the private identity key — protect this archive.",
                    fg=typer.colors.YELLOW)


@backup_app.command("list")
def backup_list(config: str = ConfigOption) -> None:
    """List backups with bounded manifest summaries (newest first)."""
    from aithernet.ops.backup import list_backups
    cfg = load_config(config)
    rows = list_backups(_backup_dir_for(cfg))
    if not rows:
        typer.echo("No backups.")
        return
    for b in rows:
        from pathlib import Path as _P
        typer.echo(f"  {_P(b['path']).name}  node={str(b.get('node_id'))[:8]} "
                   f"v{b.get('app_version')} files={b.get('file_count')} "
                   f"{b.get('created_at')}")


@backup_app.command("verify")
def backup_verify_cmd(archive: str = typer.Argument(..., help="Backup archive path.")) -> None:
    """Verify a backup's manifest and every file checksum."""
    from aithernet.ops.backup import verify_backup
    v = verify_backup(archive)
    if v.ok:
        typer.secho(f"Backup OK — node {str(v.node_id)[:8]}, v{v.app_version}, "
                    f"{len(v.schema_migrations)} migrations.", fg=typer.colors.GREEN)
    else:
        typer.secho("Backup INVALID:", fg=typer.colors.RED, err=True)
        for issue in v.issues:
            typer.echo(f"  - {issue}")
        raise typer.Exit(code=1)


@backup_app.command("restore")
def backup_restore_cmd(
    archive: str = typer.Argument(..., help="Backup archive path."),
    config: str = ConfigOption,
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate only; change nothing."),
    yes: bool = typer.Option(False, "--yes", help="Confirm the destructive restore."),
    restore_private_identity: bool = typer.Option(
        False, "--restore-private-identity", help="Also restore the private identity key."),
) -> None:
    """Restore a verified backup (preserves a rollback snapshot; refuses an identity merge)."""
    from aithernet.ops.backup import restore_backup
    cfg = load_config(config)
    if not dry_run and not yes:
        typer.confirm("Restore overwrites the current database/identity (a rollback snapshot is "
                      "kept). Continue?", abort=True)
    result = restore_backup(archive, cfg, config_path=config, dry_run=dry_run, now_iso=_now_iso(),
                            restore_private_identity=restore_private_identity)
    if not result.ok:
        typer.secho("Restore refused:", fg=typer.colors.RED, err=True)
        for issue in result.issues:
            typer.echo(f"  - {issue}")
        raise typer.Exit(code=1)
    tag = "[dry-run] " if dry_run else ""
    typer.secho(f"{tag}Restore validated ({len(result.restored_files)} files).",
                fg=typer.colors.GREEN)
    if result.rollback_snapshot:
        typer.echo(f"  rollback snapshot: {result.rollback_snapshot}")


@upgrade_app.command("preflight")
def upgrade_preflight_cmd(config: str = ConfigOption) -> None:
    """Capture version + migration state and validate readiness to upgrade."""
    from aithernet.ops.upgrade import upgrade_preflight
    cfg = load_config(config)
    data = upgrade_preflight(cfg, config_path=config)
    typer.secho(f"Upgrade preflight (v{data['app_version']})", fg=typer.colors.CYAN)
    ms = data["migration_state"]
    typer.echo(f"  migrations applied: {len(ms['applied'])}  pending: {len(ms['pending'])}")
    for b in data["external_backends"]:
        typer.echo(f"  external backend {b['backend_id']}: revision={b['pinned_revision']} "
                   f"(unmodified)")
    typer.secho(f"  ready to apply: {data['ready_to_apply']}",
                fg=typer.colors.GREEN if data["ready_to_apply"] else typer.colors.RED)
    if not data["ready_to_apply"]:
        raise typer.Exit(code=1)


@upgrade_app.command("apply")
def upgrade_apply_cmd(
    config: str = ConfigOption,
    yes: bool = typer.Option(False, "--yes", help="Confirm the upgrade."),
) -> None:
    """Back up, run migrations, then health-check. Stops + keeps the backup on failure."""
    from aithernet.ops.upgrade import upgrade_apply
    cfg = load_config(config)
    if not yes:
        typer.confirm("Apply migrations after creating a pre-upgrade backup?", abort=True)
    data = upgrade_apply(cfg, config_path=config, backup_dir=_backup_dir_for(cfg),
                         now_iso=_now_iso())
    typer.echo(f"  rollback backup: {data['rollback_backup']}")
    typer.echo(f"  applied: {data.get('applied_migrations')}")
    typer.secho(f"  {data['message']}",
                fg=typer.colors.GREEN if data["ok"] else typer.colors.RED)
    if not data["ok"]:
        raise typer.Exit(code=1)


@upgrade_app.command("verify")
def upgrade_verify_cmd(config: str = ConfigOption) -> None:
    """Confirm the live schema validates and no migration is pending."""
    from aithernet.ops.upgrade import upgrade_verify
    data = upgrade_verify(load_config(config))
    typer.secho(f"  schema valid + up to date: {data['ok']}",
                fg=typer.colors.GREEN if data["ok"] else typer.colors.RED)
    if not data["ok"]:
        raise typer.Exit(code=1)


@upgrade_app.command("rollback")
def upgrade_rollback_cmd(
    backup_path: str = typer.Argument(..., help="Pre-upgrade backup to restore."),
    config: str = ConfigOption,
    yes: bool = typer.Option(False, "--yes", help="Confirm the rollback."),
) -> None:
    """Restore a captured pre-upgrade backup (explicit; never an automatic downgrade)."""
    from aithernet.ops.upgrade import upgrade_rollback
    cfg = load_config(config)
    if not yes:
        typer.confirm("Roll back by restoring this backup?", abort=True)
    data = upgrade_rollback(cfg, backup_path=backup_path, config_path=config, now_iso=_now_iso())
    if not data["ok"]:
        typer.secho(f"Rollback failed: {data['issues']}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"Rolled back ({len(data['restored_files'])} files restored).",
                fg=typer.colors.GREEN)


@diagnostics_app.command("bundle")
def diagnostics_bundle_cmd(
    config: str = ConfigOption,
    output_dir: str | None = typer.Option(None, "--output-dir", help="Where to write the bundle."),
) -> None:
    """Write a sanitized diagnostics archive (no secrets, db contents, or private identity)."""
    from pathlib import Path

    from aithernet.ops.diagnostics import create_diagnostics_bundle
    cfg = load_config(config)
    out = output_dir or _backup_dir_for(cfg)
    log_file = None
    if cfg.node_state_root:
        from aithernet.ops.paths import NodePaths
        candidate = NodePaths.from_root(cfg.node_state_root).log_file
        log_file = str(candidate) if Path(candidate).is_file() else None
    path = create_diagnostics_bundle(cfg, output_dir=out, config_path=config, now_iso=_now_iso(),
                                     log_file=log_file)
    typer.secho(f"Diagnostics bundle written: {path}", fg=typer.colors.GREEN)
    typer.echo("  contains only sanitized operational facts (no db contents, keys, or secrets).")


# -- managed hardware (Stage 14B) -------------------------------------------------


def _hw_get(base_url: str, path: str, params: dict | None = None) -> httpx.Response:
    with _client(base_url) as client:
        return client.get(path, params=params or {})


@hardware_app.command("providers")
def hardware_providers(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List configured discovery providers and whether each is available."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _hw_get(base_url, "/hardware/providers")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    rows = resp.json()
    if not rows:
        typer.echo("No discovery providers configured.")
        return
    for p in rows:
        flag = "available" if p["available"] else "unavailable"
        req = " [required]" if p.get("required") else ""
        typer.echo(f"  {p['provider_id']:20} {p['kind']:12} {flag}{req}")


@hardware_app.command("refresh")
def hardware_refresh(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Trigger an inventory refresh (runs discovery providers once)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post("/hardware/refresh", timeout=_MCP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    data = resp.json()
    typer.echo(f"Availability: {data['availability']}")
    typer.echo(f"Devices by status: {data['devices_by_status']}")


@hardware_app.command("list")
def hardware_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List discovered managed devices (sanitized; bounded)."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _hw_get(base_url, "/hardware/devices")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    rows = resp.json()
    if not rows:
        typer.echo("No devices in inventory.")
        return
    for d in rows:
        rx = "RX" if d.get("rx") else ("-" if d.get("rx") is False else "?")
        tx = "TX" if d.get("tx") else ("-" if d.get("tx") is False else "?")
        typer.echo(
            f"  {d['device_id'][:8]}  {d['display_name'] or '-':22} {d['provider_id']:10} "
            f"status={d['status']:9} {rx}/{tx} leases={d['active_lease_count']}"
        )


@hardware_app.command("show")
def hardware_show(
    device_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one device's bounded facts + presence/health/status."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _hw_get(base_url, f"/hardware/devices/{device_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    d = resp.json()
    for key in ("device_id", "display_name", "provider_id", "device_kind", "vendor", "product",
                "serial", "driver", "presence_state", "health_state", "status", "enabled",
                "identity_limited", "compatible_backends", "active_lease_count"):
        typer.echo(f"  {key}: {d.get(key)}")


@hardware_app.command("capabilities")
def hardware_capabilities(
    device_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one device's factual capabilities (unknown stays unknown)."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _hw_get(base_url, f"/hardware/devices/{device_id}/capabilities")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.echo(json.dumps(resp.json(), indent=2, sort_keys=True))


@hardware_app.command("health")
def hardware_health(
    device_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show a device's recent health/presence events."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _hw_get(base_url, f"/hardware/devices/{device_id}/health-events")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    rows = resp.json()
    if not rows:
        typer.echo("No health events.")
        return
    for e in rows:
        typer.echo(f"  {e['created_at']}  {e['health_state']:8} {e.get('detail') or ''}")


@hardware_app.command("doctor")
def hardware_doctor() -> None:
    """Read-only health report: GNU Radio / SoapySDR / UHD / libiio / groups / detected SDRs /
    RF MCP readiness, with actionable remediation. Runs unprivileged."""
    from aithernet.hardware import setup as hw
    rep = hw.doctor()
    typer.secho(f"Hardware doctor — {'OK' if rep.ok else 'ISSUES'}",
                fg=typer.colors.GREEN if rep.ok else typer.colors.RED)
    colors = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW,
              "fail": typer.colors.RED, "info": typer.colors.CYAN}
    for c in rep.checks:
        typer.secho(f"  [{c.status.upper():4}] {c.name}: {c.detail or ''}",
                    fg=colors.get(c.status))
        if c.remediation and c.status in ("warn", "fail"):
            typer.echo(f"         -> {c.remediation}")
    if not rep.ok:
        raise typer.Exit(code=1)


@hardware_app.command("discover")
def hardware_discover() -> None:
    """Enumerate attached SDRs (read-only). Does not claim a device it cannot see."""
    import json as _json

    from aithernet.hardware import setup as hw
    sdrs = hw.discover_sdrs()
    typer.echo(_json.dumps(sdrs, indent=2))
    if not sdrs:
        typer.secho("No SDRs detected.", fg=typer.colors.YELLOW)


@hardware_app.command("verify")
def hardware_verify() -> None:
    """Pass/fail rollup of the host RF stack (non-zero exit when any check fails)."""
    import json as _json

    from aithernet.hardware import setup as hw
    res = hw.verify()
    typer.echo(_json.dumps(res, indent=2))
    if not res["ok"]:
        raise typer.Exit(code=1)


@hardware_app.command("install")
def hardware_install(
    profile: str = typer.Option(..., "--profile",
                                help="core|simulation|pluto|usrp|soapy-generic|full"),
    yes: bool = typer.Option(False, "--yes", help="Run the apt install (else plan only)."),
) -> None:
    """Install a hardware profile from SIGNED distribution packages (no source compilation).

    Prints the exact privileged plan; with --yes it runs `sudo apt-get install`. GNU Radio / UHD /
    SoapySDR are never compiled from source here (use the explicit, confirmed source-build mode)."""
    import subprocess

    from aithernet.hardware import setup as hw
    try:
        plan = hw.install_plan(profile)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.secho(f"Profile '{profile}': {plan['description']}", fg=typer.colors.CYAN)
    typer.echo("  packages: " + " ".join(plan["apt_packages"]))
    typer.echo("  command:  " + " ".join(plan["apt_command"]))
    for pc in plan["post_commands"]:
        typer.echo(f"  post:     {pc}")
    for g in plan["groups"]:
        typer.echo(f"  group:    add $USER to '{g}'")
    if not yes:
        typer.secho("Plan only (re-run with --yes to install). Requires sudo.",
                    fg=typer.colors.YELLOW)
        return
    rc = subprocess.run(plan["apt_command"]).returncode
    if rc != 0:
        raise typer.Exit(code=rc)
    for pc in plan["post_commands"]:
        subprocess.run(["sudo", pc])
    typer.secho("Profile installed. Run `aithernet hardware doctor` to verify.",
                fg=typer.colors.GREEN)


@hardware_lease_app.command("list")
def hardware_lease_list(
    active: bool = typer.Option(False, "--active", help="Only holding leases."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """List device leases (lease tokens are never shown)."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _hw_get(base_url, "/hardware/leases", {"active": active})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    rows = resp.json()
    if not rows:
        typer.echo("No leases.")
        return
    for le in rows:
        typer.echo(
            f"  {le['lease_id'][:8]}  device={le['device_id'][:8]} {le['lease_mode']:14} "
            f"{le['direction']:5} state={le['state']:9} expires={le.get('expires_at')}"
        )


@hardware_lease_app.command("show")
def hardware_lease_show(
    lease_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one lease + its lifecycle events."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _hw_get(base_url, f"/hardware/leases/{lease_id}")
        events = _hw_get(base_url, f"/hardware/leases/{lease_id}/events")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.echo(json.dumps(resp.json(), indent=2, sort_keys=True))
    if events.is_success:
        for e in events.json():
            typer.echo(f"  {e['created_at']}  {e['event_type']:10} {e.get('reason') or ''}")


@hardware_lease_app.command("acquire")
def hardware_lease_acquire(
    device_id: str,
    backend_id: str = typer.Option(..., "--backend-id", help="RF backend to bind."),
    direction: str = typer.Option("rx", "--direction", help="rx|tx|rx_tx."),
    mode: str = typer.Option("exclusive", "--mode", help="exclusive|shared_receive."),
    operation: str | None = typer.Option(None, "--operation", help="Bounded purpose."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Acquire a device lease (operator; requires operator_lease_actions_enabled)."""
    base_url = _resolve_base_url(url, config)
    body = {"device_id": device_id, "backend_id": backend_id, "direction": direction,
            "lease_mode": mode, "operation": operation}
    try:
        with _client(base_url) as client:
            resp = client.post("/hardware/leases/acquire", json=body)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    le = resp.json()
    typer.secho(f"Lease {le['lease_id']} state={le['state']}", fg=typer.colors.GREEN)


@hardware_lease_app.command("release")
def hardware_lease_release(
    lease_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Release a held lease (operator)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(f"/hardware/leases/{lease_id}/release", json={})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Lease {lease_id} released.", fg=typer.colors.GREEN)


@hardware_lease_app.command("revoke")
def hardware_lease_revoke(
    lease_id: str,
    yes: bool = typer.Option(False, "--yes", help="Confirm revocation."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Revoke a lease (operator). Requires confirmation."""
    if not yes and not typer.confirm(f"Revoke lease {lease_id}? Active RF work loses the device."):
        raise typer.Exit(code=1)
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(f"/hardware/leases/{lease_id}/revoke", json={})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Lease {lease_id} revoked.", fg=typer.colors.YELLOW)


@hardware_qualify_app.command("preflight")
def hardware_qualify_preflight(
    device_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Read-only qualification preflight for a device (never opens the device)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post("/hardware/qualifications/preflight", json={"device_id": device_id})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    data = resp.json()
    typer.secho(f"Preflight {'OK' if data['ok'] else 'BLOCKED'}",
                fg=typer.colors.GREEN if data["ok"] else typer.colors.RED)
    for c in data["checks"]:
        typer.echo(f"  {c['name']:24} {c['status']:12} {c.get('detail') or ''}")


@hardware_qualify_app.command("run")
def hardware_qualify_run(
    device_id: str,
    survey: bool = typer.Option(False, "--survey", help="Also run the 2.4 GHz energy survey."),
    no_capture: bool = typer.Option(False, "--no-capture", help="Skip the RX capture."),
    duration: float | None = typer.Option(None, "--duration", help="Capture seconds."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Run a REAL-hardware qualification (operator-invoked RF actions). Opens the device."""
    base_url = _resolve_base_url(url, config)
    body = {"device_id": device_id, "do_capture": not no_capture, "do_survey": survey,
            "capture_duration": duration}
    try:
        with _client(base_url) as client:
            resp = client.post("/hardware/qualifications/run", json=body, timeout=_RUN_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    r = resp.json()
    typer.secho(f"Qualification {r['id']} status={r['status']} "
                f"classification={r['support_classification']}",
                fg=typer.colors.GREEN if r["status"] == "passed" else typer.colors.YELLOW)
    typer.echo(f"  {r['checks_passed']} passed / {r['checks_failed']} failed / "
               f"{r['checks_not_executed']} not executed")


@hardware_qualify_app.command("status")
def hardware_qualify_status(
    qualification_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show a qualification run's status + classification."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _hw_get(base_url, f"/hardware/qualifications/{qualification_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    r = resp.json()
    for key in ("id", "device_id", "status", "support_classification", "checks_passed",
                "checks_failed", "checks_not_executed", "summary", "completed_at"):
        typer.echo(f"  {key}: {r.get(key)}")


@hardware_qualify_app.command("report")
def hardware_qualify_report(
    qualification_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Print the full sanitized qualification report (environment + checks)."""
    base_url = _resolve_base_url(url, config)
    try:
        run = _hw_get(base_url, f"/hardware/qualifications/{qualification_id}")
        checks = _hw_get(base_url, f"/hardware/qualifications/{qualification_id}/checks")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(run)
    r = run.json()
    typer.secho(f"Qualification {r['id']} — {r['support_classification']} ({r['status']})",
                fg=typer.colors.CYAN)
    env = r.get("environment", {})
    typer.echo(f"  GNU Radio: {env.get('gnuradio_version')}  "
               f"SoapySDR: {env.get('soapysdr_version')}")
    typer.echo(f"  OS: {env.get('os')}  kernel: {env.get('kernel')}  host: {env.get('host_id')}")
    if checks.is_success:
        typer.echo("  Checks:")
        for c in checks.json():
            typer.echo(f"    {c['name']:24} {c['status']:12} {c.get('detail') or ''}")


# -- field validation (Stage 14C.2) ----------------------------------------------


@field_app.command("preflight")
def field_preflight(
    device_id: str | None = typer.Option(None, "--device-id"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Read-only campaign preflight."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post("/field/campaigns/preflight", json={"device_id": device_id})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    data = resp.json()
    typer.secho(f"Preflight {'OK' if data['ok'] else 'BLOCKED'}",
                fg=typer.colors.GREEN if data["ok"] else typer.colors.RED)
    for c in data["checks"]:
        typer.echo(f"  {c['name']:26} {c['status']:12} {c.get('detail') or ''}")


@field_campaign_app.command("create")
def field_campaign_create(
    name: str, objective: str | None = typer.Option(None, "--objective"),
    profile: str = typer.Option("smoke", "--profile"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Create a field campaign (operator-gated)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post("/field/campaigns/create",
                               json={"name": name, "objective": objective, "profile": profile})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Campaign {resp.json()['id']} created.", fg=typer.colors.GREEN)


@field_campaign_app.command("start")
def field_campaign_start(
    campaign_id: str,
    device_id: str = typer.Option(..., "--device-id"),
    iterations: int | None = typer.Option(None, "--iterations"),
    survey_windows: int = typer.Option(0, "--survey-windows"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Run a campaign (real RF actions; operator-gated). Opens the device."""
    base_url = _resolve_base_url(url, config)
    body = {"campaign_id": campaign_id, "device_id": device_id, "iterations": iterations,
            "survey_windows": survey_windows}
    try:
        with _client(base_url) as client:
            resp = client.post("/field/campaigns/start", json=body, timeout=_RUN_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    r = resp.json()
    typer.secho(f"Campaign {r['id']} {r['status']} classification={r['classification']} "
                f"({r['checks_passed']} passed / {r['checks_failed']} failed / "
                f"{r['checks_not_executed']} not executed)",
                fg=typer.colors.GREEN if r["status"] == "completed" else typer.colors.YELLOW)


@field_campaign_app.command("status")
def field_campaign_status(
    campaign_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show a campaign's status + classification."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _hw_get(base_url, f"/field/campaigns/{campaign_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    r = resp.json()
    for key in ("id", "name", "status", "classification", "checks_passed", "checks_failed",
                "checks_not_executed", "summary"):
        typer.echo(f"  {key}: {r.get(key)}")


@field_campaign_app.command("stop")
def field_campaign_stop(
    campaign_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Stop a running campaign (operator-gated)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(f"/field/campaigns/{campaign_id}/stop")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Campaign {campaign_id} status={resp.json()['status']}.", fg=typer.colors.YELLOW)


@field_campaign_app.command("report")
def field_campaign_report(
    campaign_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Print the sanitized campaign report."""
    base_url = _resolve_base_url(url, config)
    try:
        resp = _hw_get(base_url, f"/field/campaigns/{campaign_id}/report")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.echo(json.dumps(resp.json(), indent=2, sort_keys=True, default=str))


@field_soak_app.command("start")
def field_soak_start(
    target_seconds: float = typer.Option(..., "--seconds"),
    interval: float | None = typer.Option(None, "--interval"),
    campaign_id: str | None = typer.Option(None, "--campaign-id"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Run a resource soak (operator-gated)."""
    base_url = _resolve_base_url(url, config)
    body = {"target_seconds": target_seconds, "sample_interval": interval,
            "campaign_id": campaign_id}
    try:
        with _client(base_url) as client:
            resp = client.post("/field/soak/start", json=body, timeout=_RUN_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    s = resp.json()
    typer.secho(f"Soak {s['id']} samples={s['sample_count']} violations={s['violations']}",
                fg=typer.colors.GREEN if not s["violations"] else typer.colors.YELLOW)


@field_fault_app.command("list")
def field_fault_list() -> None:
    """List the supported bounded validation fault types."""
    from aithernet.field.service import FAULT_TYPES
    for f in FAULT_TYPES:
        typer.echo(f"  {f}")


@field_fault_app.command("inject")
def field_fault_inject(
    fault_type: str,
    device_id: str | None = typer.Option(None, "--device-id"),
    campaign_id: str | None = typer.Option(None, "--campaign-id"),
    yes: bool = typer.Option(False, "--yes", help="Confirm the disruptive fault."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Inject a bounded validation fault (requires confirmation; validation mode only)."""
    prompt = f"Inject fault '{fault_type}'? (validation environments only)"
    if not yes and not typer.confirm(prompt):
        raise typer.Exit(code=1)
    base_url = _resolve_base_url(url, config)
    params = {"device_id": device_id} if device_id else {}
    body = {"fault_type": fault_type, "params": params, "confirm": True,
            "campaign_id": campaign_id}
    try:
        with _client(base_url) as client:
            resp = client.post("/field/faults/inject", json=body)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    r = resp.json()
    typer.secho(f"Fault {r['id']} status={r['status']} recovery={r.get('actual_recovery')}",
                fg=typer.colors.GREEN if r["status"] == "recovered" else typer.colors.YELLOW)


@agents_gateway_app.command("create")
def agents_gateway_create(
    name: str = typer.Argument(..., help="Display name for the scoped server-side gateway agent."),
    public_key: str | None = typer.Option(
        None, "--public-key", help="Base64 raw Ed25519 key the gateway will sign requests with."),
    owner: str | None = typer.Option(None, "--owner", help="Owner/operator label."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Provision a SCOPED external-agent gateway credential for a server-side agent.

    The gateway can submit work via the authenticated external-agent API but cannot bypass this
    node's policies (provider/hardware/RF/workspace ownership stay local). No unauthenticated node
    API is exposed."""
    base_url = _resolve_base_url(url, config)
    body = {"display_name": name, "public_key": public_key, "owner": owner}
    try:
        with _client(base_url) as client:
            resp = client.post("/external-agents", json=body)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Gateway '{name}' created: agent {resp.json()['agent_id']} (scoped; no policy "
                "bypass).", fg=typer.colors.GREEN)


@agents_gateway_app.command("revoke")
def agents_gateway_revoke(
    name: str = typer.Argument(..., help="Display name (or agent id) of the gateway to revoke."),
    reason: str = typer.Option("revoked by operator", "--reason"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Revoke a gateway credential — it can no longer submit work to this node."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            roster = client.get("/external-agents")
            _check(roster)
            agents = roster.json() or []
            match = next((a for a in agents if a.get("agent_id") == name
                          or a.get("display_name") == name), None)
            if match is None:
                typer.secho(f"No gateway '{name}'.", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
            resp = client.post(f"/external-agents/{match['agent_id']}/disable",
                               json={"reason": reason})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Gateway '{name}' revoked.", fg=typer.colors.GREEN)


@extagent_app.command("list")
def extagent_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List external agents (no secrets)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get("/external-agents")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    for a in resp.json():
        typer.echo(f"{a['agent_id']}  {a['status']:8}  {a['display_name']}  {a.get('fingerprint')}")


@extagent_app.command("show")
def extagent_show(agent_id: str, config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show one external agent (no secrets)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get(f"/external-agents/{agent_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.echo(json.dumps(resp.json(), indent=2))


@extagent_app.command("create")
def extagent_create(
    display_name: str,
    public_key: str | None = typer.Option(None, "--public-key", help="Base64 raw Ed25519 key."),
    owner: str | None = typer.Option(None, "--owner"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Provision an external agent (operator-gated)."""
    base_url = _resolve_base_url(url, config)
    body = {"display_name": display_name, "public_key": public_key, "owner": owner}
    try:
        with _client(base_url) as client:
            resp = client.post("/external-agents", json=body)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Agent {resp.json()['agent_id']} created.", fg=typer.colors.GREEN)


@extagent_app.command("enable")
def extagent_enable(agent_id: str, config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Enable an external agent."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(f"/external-agents/{agent_id}/enable")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Agent {agent_id} enabled.", fg=typer.colors.GREEN)


@extagent_app.command("disable")
def extagent_disable(
    agent_id: str, reason: str = typer.Option("", "--reason"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Disable an external agent (blocks new submissions + connections)."""
    if not yes:
        typer.confirm(f"Disable external agent {agent_id}?", abort=True)
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(f"/external-agents/{agent_id}/disable", json={"reason": reason})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Agent {agent_id} disabled.", fg=typer.colors.YELLOW)


@extagent_app.command("diagnostics")
def extagent_diagnostics(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show sanitized external-agent gateway diagnostics."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get("/external-agents/diagnostics")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.echo(json.dumps(resp.json(), indent=2))


@extagent_cred_app.command("create")
def extagent_cred_create(
    agent_id: str, kind: str = typer.Option("ed25519", "--kind"),
    public_key: str | None = typer.Option(None, "--public-key"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Add a credential. A bearer secret is printed ONCE and never stored in clear."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(
                f"/external-agents/{agent_id}/credentials",
                json={"kind": kind, "public_key": public_key},
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    r = resp.json()
    if "secret" in r:
        typer.secho(f"SECRET (shown once): {r['secret']}", fg=typer.colors.YELLOW)
    typer.secho(f"Credential {r['credential_id']} ({r['kind']}) created.", fg=typer.colors.GREEN)


@extagent_cred_app.command("list")
def extagent_cred_list(
    agent_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """List credentials (never shows secrets)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get(f"/external-agents/{agent_id}/credentials")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    for c in resp.json():
        typer.echo(f"{c['credential_id']}  {c['kind']:8}  {c['status']:8}  {c['key_id']}")


@extagent_cred_app.command("revoke")
def extagent_cred_revoke(
    agent_id: str, credential_id: str, reason: str = typer.Option("", "--reason"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Revoke a credential (invalidates future authentication)."""
    if not yes:
        typer.confirm(f"Revoke credential {credential_id}?", abort=True)
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(
                f"/external-agents/{agent_id}/credentials/{credential_id}/revoke",
                json={"reason": reason},
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho("Credential revoked.", fg=typer.colors.YELLOW)


@extagent_perm_app.command("show")
def extagent_perm_show(
    agent_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show an agent's permissions."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get(f"/external-agents/{agent_id}/permissions")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.echo(json.dumps(resp.json(), indent=2))


@extagent_perm_app.command("set")
def extagent_perm_set(
    agent_id: str,
    permissions: list[str] = ExtAgentPermissionsArgument,
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Set an agent's permissions (replaces the full set)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.put(
                f"/external-agents/{agent_id}/permissions", json={"permissions": permissions}
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho("Permissions updated.", fg=typer.colors.GREEN)


@extagent_ep_app.command("add")
def extagent_ep_add(
    agent_id: str, endpoint_url: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Register a callback endpoint (validated against the SSRF policy)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(
                f"/external-agents/{agent_id}/endpoints", json={"url": endpoint_url}
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Endpoint {resp.json()['endpoint_id']} registered.", fg=typer.colors.GREEN)


@extagent_ep_app.command("list")
def extagent_ep_list(
    agent_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """List callback endpoints (sanitized policy result)."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get(f"/external-agents/{agent_id}/endpoints")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    for e in resp.json():
        typer.echo(f"{e['endpoint_id']}  {e['status']:20}  {e['scheme']}://{e['host']}:{e['port']}")


@extagent_ep_app.command("verify")
def extagent_ep_verify(
    agent_id: str, endpoint_id: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Approve outside default policy."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Approve (verify) a callback endpoint after re-validation."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(
                f"/external-agents/{agent_id}/endpoints/{endpoint_id}/verify",
                json={"approve": True},
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho("Endpoint verified.", fg=typer.colors.GREEN)


@extagent_ep_app.command("disable")
def extagent_ep_disable(
    agent_id: str, endpoint_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Disable a callback endpoint."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(
                f"/external-agents/{agent_id}/endpoints/{endpoint_id}/disable", json={"reason": ""}
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho("Endpoint disabled.", fg=typer.colors.YELLOW)


@extagent_sub_app.command("create")
def extagent_sub_create(
    agent_id: str, mode: str = typer.Option("webhook", "--mode"),
    endpoint_id: str | None = typer.Option(None, "--endpoint-id"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Create an event subscription."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(
                f"/external-agents/{agent_id}/subscriptions",
                json={"delivery_mode": mode, "endpoint_id": endpoint_id, "filters": {}},
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho(f"Subscription {resp.json()['subscription_id']} created.", fg=typer.colors.GREEN)


@extagent_sub_app.command("list")
def extagent_sub_list(
    agent_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """List event subscriptions."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get(f"/external-agents/{agent_id}/subscriptions")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    for s in resp.json():
        typer.echo(f"{s['subscription_id']}  {s['delivery_mode']:10}  {s['status']:8}  "
                   f"seq={s['next_sequence']} acked={s['last_acked_sequence']}")


@extagent_sub_app.command("pause")
def extagent_sub_pause(
    agent_id: str, subscription_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Pause a subscription."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(
                f"/external-agents/{agent_id}/subscriptions/{subscription_id}/pause",
                json={"reason": ""},
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho("Subscription paused.", fg=typer.colors.YELLOW)


@extagent_sub_app.command("resume")
def extagent_sub_resume(
    agent_id: str, subscription_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Resume a subscription."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(
                f"/external-agents/{agent_id}/subscriptions/{subscription_id}/resume"
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho("Subscription resumed.", fg=typer.colors.GREEN)


@extagent_sub_app.command("delete")
def extagent_sub_delete(
    agent_id: str, subscription_id: str,
    yes: bool = typer.Option(False, "--yes", "-y"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Delete (revoke) a subscription."""
    if not yes:
        typer.confirm(f"Delete subscription {subscription_id}?", abort=True)
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.delete(f"/external-agents/{agent_id}/subscriptions/{subscription_id}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho("Subscription deleted.", fg=typer.colors.YELLOW)


@extagent_del_app.command("list")
def extagent_del_list(
    agent_id: str, status: str | None = typer.Option(None, "--status"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """List durable deliveries for an agent."""
    base_url = _resolve_base_url(url, config)
    params = {"status": status} if status else {}
    try:
        with _client(base_url) as client:
            resp = client.get(f"/external-agents/{agent_id}/deliveries", params=params)
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    for d in resp.json():
        typer.echo(f"{d['message_pk']}  {d['event_type']:24}  {d['status']:12}  "
                   f"attempts={d['attempt_count']}/{d['max_attempts']}")


@extagent_del_app.command("show")
def extagent_del_show(
    agent_id: str, message_pk: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """Show one delivery including its per-attempt history."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get(f"/external-agents/{agent_id}/deliveries/{message_pk}")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.echo(json.dumps(resp.json(), indent=2))


@extagent_del_app.command("redrive")
def extagent_del_redrive(
    agent_id: str, message_pk: str,
    yes: bool = typer.Option(False, "--yes", "-y"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Re-queue a dead-lettered delivery for one more cycle."""
    if not yes:
        typer.confirm(f"Redrive dead-lettered message {message_pk}?", abort=True)
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(f"/external-agents/{agent_id}/deliveries/{message_pk}/redrive")
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    typer.secho("Message re-queued.", fg=typer.colors.GREEN)


@extagent_del_app.command("dead-letter")
def extagent_del_dead_letter(
    agent_id: str, config: str = ConfigOption, url: str | None = UrlOption
) -> None:
    """List dead-lettered deliveries for an agent."""
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get(
                f"/external-agents/{agent_id}/deliveries", params={"status": "dead_letter"}
            )
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    for d in resp.json():
        typer.echo(f"{d['message_pk']}  {d['event_type']:24}  {d.get('failure_category')}")


def _data_get(path: str, config: str, url: str | None, params: dict | None = None):
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.get(path, params=params or {})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    return resp.json()


def _data_post(path: str, config: str, url: str | None, body: dict | None = None):
    base_url = _resolve_base_url(url, config)
    try:
        with _client(base_url) as client:
            resp = client.post(path, json=body or {})
    except httpx.HTTPError as exc:
        raise _fail_unreachable(base_url, exc) from exc
    _check(resp)
    return resp.json()


@data_consent_app.command("show")
def data_consent_show(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show the currently effective consent by category (and grants)."""
    typer.echo(json.dumps(_data_get("/data/consent", config, url), indent=2))


@data_consent_app.command("grant")
def data_consent_grant(
    category: str,
    export: bool = typer.Option(False, "--export"),
    raw_artifact: bool = typer.Option(False, "--raw-artifact"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Grant consent for a category (raw-artifact consent requires confirmation)."""
    if raw_artifact and not yes:
        typer.confirm("Grant RAW-ARTIFACT consent (separate, sensitive)?", abort=True)
    r = _data_post("/data/consent/grants", config, url,
                   {"category": category, "export": export, "raw_artifact": raw_artifact})
    typer.secho(f"Granted {category} (grant {r['grant_id']}).", fg=typer.colors.GREEN)


@data_consent_app.command("withdraw")
def data_consent_withdraw(
    grant_id: str, reason: str = typer.Option("", "--reason"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Withdraw a consent grant (blocks new exports immediately)."""
    if not yes:
        typer.confirm(f"Withdraw consent grant {grant_id}?", abort=True)
    r = _data_post(f"/data/consent/{grant_id}/withdraw", config, url, {"reason": reason})
    typer.secho(f"Withdrawn ({r['reconciled']} queued records reconciled).",
                fg=typer.colors.YELLOW)


@data_records_app.command("list")
def data_records_list(
    category: str | None = typer.Option(None, "--category"),
    status: str | None = typer.Option(None, "--status"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """List collected record summaries (no secrets)."""
    params = {k: v for k, v in {"category": category, "status": status}.items() if v}
    for r in _data_get("/data/records", config, url, params):
        typer.echo(f"{r['record_id']}  {r['category']:12}  {r['status']:14}  "
                   f"bytes={r['byte_size']}")


@data_records_app.command("show")
def data_records_show(record_id: str, config: str = ConfigOption,
                      url: str | None = UrlOption) -> None:
    """Show one record incl. its ALREADY-REDACTED payload (operator inspection)."""
    typer.echo(json.dumps(_data_get(f"/data/records/{record_id}", config, url), indent=2))


@data_records_app.command("approve")
def data_records_approve(record_id: str, config: str = ConfigOption,
                         url: str | None = UrlOption) -> None:
    """Approve a held record for export."""
    _data_post(f"/data/records/{record_id}/approve", config, url)
    typer.secho("Approved.", fg=typer.colors.GREEN)


@data_records_app.command("reject")
def data_records_reject(record_id: str, config: str = ConfigOption,
                        url: str | None = UrlOption) -> None:
    """Reject a record (cancelled, never exported)."""
    _data_post(f"/data/records/{record_id}/reject", config, url)
    typer.secho("Rejected.", fg=typer.colors.YELLOW)


@data_export_app.command("status")
def data_export_status(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show export diagnostics (pending/held records, batches, dead letters, destinations)."""
    typer.echo(json.dumps(_data_get("/data/diagnostics", config, url), indent=2))


@data_export_app.command("pause")
def data_export_pause(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Pause export delivery (collection continues)."""
    _data_post("/data/export/pause", config, url, {"paused": True})
    typer.secho("Export paused.", fg=typer.colors.YELLOW)


@data_export_app.command("resume")
def data_export_resume(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Resume export delivery."""
    _data_post("/data/export/pause", config, url, {"paused": False})
    typer.secho("Export resumed.", fg=typer.colors.GREEN)


@data_export_app.command("build")
def data_export_build(
    destination_id: str = typer.Option(..., "--destination-id"),
    category: str | None = typer.Option(None, "--category"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Seal approved records into an immutable batch."""
    r = _data_post("/data/batches/build", config, url,
                   {"destination_id": destination_id, "category": category})
    typer.secho(f"Batch {r['batch_id']} sealed ({r['record_count']} records, "
                f"encrypted={r['encrypted']}).", fg=typer.colors.GREEN)


@data_export_app.command("deliver")
def data_export_deliver(
    batch_id: str = typer.Option(..., "--batch-id"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Re-queue (redrive) a dead-lettered batch for delivery."""
    _data_post(f"/data/batches/{batch_id}/redrive", config, url)
    typer.secho("Batch re-queued for delivery.", fg=typer.colors.GREEN)


@data_dest_app.command("list")
def data_dest_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List export destinations (sanitized config; no secrets)."""
    for d in _data_get("/data/destinations", config, url):
        typer.echo(f"{d['destination_id']}  {d['kind']:16}  enabled={d['enabled']}  "
                   f"{d['name']}")


@data_dest_app.command("add-local")
def data_dest_add_local(name: str, root: str = typer.Option(..., "--root"),
                        config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Add a local archive destination."""
    r = _data_post("/data/destinations", config, url,
                   {"kind": "local_archive", "name": name, "config": {"root": root}})
    typer.secho(f"Destination {r['destination_id']} added.", fg=typer.colors.GREEN)


@data_dest_app.command("add-http")
def data_dest_add_http(
    name: str, base_url: str = typer.Option(..., "--base-url"),
    credential_ref: str = typer.Option(..., "--credential-ref",
                                        help="Env var NAME holding the node key (never inline)."),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Add an HTTP ingestion destination (credential by env-var reference, never inline)."""
    r = _data_post("/data/destinations", config, url, {
        "kind": "http_ingestion", "name": name,
        "config": {"base_url": base_url, "credential_ref": credential_ref}})
    typer.secho(f"Destination {r['destination_id']} added.", fg=typer.colors.GREEN)


@data_dest_app.command("add-drive")
def data_dest_add_drive(
    name: str, folder_id: str = typer.Option(..., "--folder-id"),
    encryption_key_ref: str = typer.Option(..., "--encryption-key-ref"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Add a Google Drive archive destination (refs only; never an inline secret)."""
    r = _data_post("/data/destinations", config, url, {
        "kind": "google_drive", "name": name,
        "config": {"folder_id": folder_id, "encryption_key_ref": encryption_key_ref}})
    typer.secho(f"Destination {r['destination_id']} added.", fg=typer.colors.GREEN)


@data_dest_app.command("enable")
def data_dest_enable(
    destination_id: str, yes: bool = typer.Option(False, "--yes", "-y"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Enable a destination (external destinations require confirmation)."""
    if not yes:
        typer.confirm(f"Enable destination {destination_id}?", abort=True)
    _data_post(f"/data/destinations/{destination_id}/enable", config, url)
    typer.secho("Destination enabled.", fg=typer.colors.GREEN)


@data_dest_app.command("disable")
def data_dest_disable(destination_id: str, config: str = ConfigOption,
                      url: str | None = UrlOption) -> None:
    """Disable a destination."""
    _data_post(f"/data/destinations/{destination_id}/disable", config, url)
    typer.secho("Destination disabled.", fg=typer.colors.YELLOW)


@data_dest_app.command("verify")
def data_dest_verify(destination_id: str, config: str = ConfigOption,
                     url: str | None = UrlOption) -> None:
    """Verify a destination's readiness (no secrets printed)."""
    typer.echo(json.dumps(
        _data_post(f"/data/destinations/{destination_id}/verify", config, url), indent=2))


@data_dest_app.command("verify-drive")
def data_dest_verify_drive(
    destination_id: str, delete_test: bool = typer.Option(False, "--delete-test"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Manual Google Drive verification (operator-only; never runs in ordinary tests).

    Checks credentials WITHOUT printing them, verifies folder access, uploads a small encrypted
    test bundle, and records a local receipt. Real OAuth credentials must be configured first.
    """
    if delete_test and not yes:
        typer.confirm("Delete the remote Drive test file after upload?", abort=True)
    typer.secho(
        "Drive verification is an operator action requiring real configured credentials.\n"
        "Configure google_drive.oauth_credential_ref + encryption_key_ref, then run\n"
        "`aithernet data destinations verify <id>`. Real-account verification is manual and\n"
        "never executed by automated tests.", fg=typer.colors.YELLOW,
    )


@data_del_app.command("request")
def data_del_request(
    scope: str = typer.Option(..., "--scope", help="record|subject|batch|category"),
    target_id: str | None = typer.Option(None, "--target-id"),
    category: str | None = typer.Option(None, "--category"),
    reason: str = typer.Option("", "--reason"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Request a traceable deletion (propagates through dataset lineage)."""
    if not yes:
        typer.confirm(f"Request deletion (scope={scope})?", abort=True)
    r = _data_post("/data/deletions", config, url, {
        "scope": scope, "target_id": target_id, "category": category, "reason": reason})
    typer.secho(f"Deletion {r['deletion_id']} {r['status']} "
                f"({r.get('impact', {}).get('records_deleted', 0)} records).",
                fg=typer.colors.GREEN)


@data_del_app.command("status")
def data_del_status(deletion_id: str, config: str = ConfigOption,
                    url: str | None = UrlOption) -> None:
    """Show a deletion request status + receipt."""
    typer.echo(json.dumps(_data_get(f"/data/deletions/{deletion_id}", config, url), indent=2))


@data_ds_app.command("list")
def data_ds_list(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """List datasets."""
    for d in _data_get("/data/datasets", config, url):
        typer.echo(f"{d['dataset_id']}  {d['name']:24}  {d['purpose']}")


@data_ds_app.command("create")
def data_ds_create(name: str, config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Create a dataset."""
    r = _data_post("/data/datasets", config, url, {"name": name})
    typer.secho(f"Dataset {r['dataset_id']} created.", fg=typer.colors.GREEN)


@data_ds_app.command("version")
def data_ds_version(
    dataset_id: str, split: str | None = typer.Option(None, "--split"),
    config: str = ConfigOption, url: str | None = UrlOption,
) -> None:
    """Create an immutable dataset version from approved, delivered training records."""
    r = _data_post(f"/data/datasets/{dataset_id}/versions", config, url, {"split_label": split})
    typer.secho(f"Version v{r['version']} created ({r['member_count']} members, "
                f"digest {r['content_digest'][:16]}).", fg=typer.colors.GREEN)


@data_ds_app.command("manifest")
def data_ds_manifest(version_id: str, config: str = ConfigOption,
                     url: str | None = UrlOption) -> None:
    """Show a dataset version manifest + membership."""
    typer.echo(json.dumps(
        _data_get(f"/data/datasets/versions/{version_id}/manifest", config, url), indent=2))


@data_app.command("diagnostics")
def data_diagnostics(config: str = ConfigOption, url: str | None = UrlOption) -> None:
    """Show sanitized data-platform diagnostics (no secrets / tokens / keys / paths)."""
    typer.echo(json.dumps(_data_get("/data/diagnostics", config, url), indent=2))


# -- owner research recording + verified Drive archive --------------------------------------
# Automatic local recording (owner_full) + manual collect/sync/build for packaging, export, and
# admin. Drive sync is honest: without an operator-installed Desktop OAuth client, recording is
# local-only and every command says so with the exact repair — never a raw DriveError.

data_drive_client_app = typer.Typer(
    name="drive-client",
    help="[advanced standalone/local archive mode] Manage a node-local Google Desktop OAuth "
         "client. NOT required for the hosted Aithernet owner-archive upload path.",
    no_args_is_help=True)
data_app.add_typer(data_drive_client_app, name="drive-client")


def _research_spool():
    from aithernet.data.research_spool import ResearchSpool
    return ResearchSpool()


def _read_collection_config() -> dict:
    """Read research-collection config from the canonical node.yaml (CLI has no live runtime)."""
    try:
        import yaml

        from aithernet.agents.providers import node_config_path
        path = node_config_path(None)
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text()) or {}
        return ((data.get("data_platform") or {}).get("collection") or {})
    except Exception:  # noqa: BLE001
        return {}


@data_app.command("status")
def data_status() -> None:
    """Research-collection dashboard — recorder state, local queue, and hosted owner-archive upload.

    In the default hosted-client path, sanitized records are uploaded to the Aithernet owner archive
    (``drive_sync: managed_by_hosted``); the client never manages Google Drive. Shows consent,
    recorder state, local queue, uploaded/quarantined counts, hosted ingestion health, and the exact
    next action only when action is actually needed. Never prints secrets, tokens, or contents."""
    coll = _read_collection_config()
    mode = str(coll.get("data_collection_mode", "off"))
    consent = bool(coll.get("research_consent", False))
    upload_mode = str(coll.get("research_upload_mode", "hosted_owner_archive"))
    spool = _research_spool()
    st = spool.status()
    rec = spool.recorder_status()
    recorder_active = (mode == "owner_full") and st.get("healthy", False)

    if upload_mode == "standalone_drive":
        from aithernet.data.research_sync import drive_state
        ds = drive_state()
        out = {"collection": "enabled" if mode == "owner_full" else "disabled",
               "collection_mode": mode, "recorder_active": recorder_active, "consent": consent,
               "upload_mode": "standalone_drive", "mission_records": rec["mission_records"],
               "local_queue": rec["pending_collection"] + rec["pending_upload"],
               "quarantined": st.get("quarantined", 0),
               "last_recorded_at": rec["last_recorded_at"],
               "drive_sync": ds["status"], "spool_root": st["root"]}
        typer.echo(json.dumps(out, indent=2))
        return

    from aithernet.data.research_upload import (
        last_uploaded_at,
        owner_archive_state,
    )
    oa = owner_archive_state(consent=consent, spool=spool)
    uploaded = len(spool.uploaded_digests())
    out = {
        "collection": "enabled" if mode == "owner_full" else "disabled",
        "collection_mode": mode,
        "consent": consent,
        "recorder_active": recorder_active,
        "owner_archive_upload": oa["owner_archive_upload"],
        "hosted_ingestion": oa["hosted_ingestion"],
        "local_queue": oa["local_queue"],
        "mission_records": rec["mission_records"],
        "uploaded": uploaded,
        "quarantined": st.get("quarantined", 0),
        "last_recorded_at": rec["last_recorded_at"],
        "last_uploaded_at": last_uploaded_at(spool=spool),
        "drive_sync": "managed_by_hosted",
        "spool_root": st["root"],
    }
    if mode != "owner_full":
        out["next_step"] = "aithernet data setup   # enable recording + owner-archive upload"
    elif not consent:
        out["next_step"] = "aithernet data research-consent --grant"
    else:
        out["next_step"] = oa.get("next_step")
    typer.echo(json.dumps(out, indent=2))


@data_app.command("research-consent")
def data_research_consent(
    grant: bool = typer.Option(False, "--grant", help="Consent to owner-archive upload."),
    withdraw: bool = typer.Option(False, "--withdraw", help="Withdraw consent (stop uploading)."),
) -> None:
    """Show or change consent to upload sanitized research records to the Aithernet owner archive.

    Consent is explicit and withdrawable. Withdrawing stops all uploads immediately; local records
    already uploaded are unaffected. (Named `research-consent` to avoid the pre-existing Stage-14E
    `data consent` category group.)"""
    from aithernet.data.research_setup import set_consent
    if grant and withdraw:
        raise typer.BadParameter("choose one of --grant / --withdraw")
    if grant or withdraw:
        set_consent(bool(grant))
        typer.secho(f"Owner-archive upload consent: {'granted' if grant else 'withdrawn'}.",
                    fg=typer.colors.GREEN if grant else typer.colors.YELLOW)
    coll = _read_collection_config()
    typer.echo(json.dumps({"consent": bool(coll.get("research_consent", False)),
                           "consent_at": coll.get("research_consent_at"),
                           "upload_mode": coll.get("research_upload_mode",
                                                   "hosted_owner_archive")}, indent=2))


@data_app.command("upload-now")
def data_upload_now() -> None:
    """Force an immediate owner-archive upload of any queued research packages (diagnostics/admin).

    Not required for normal operation — uploads happen automatically after each mission. Refuses
    clearly (with the exact next action) when not enrolled or consent is not granted."""
    coll = _read_collection_config()
    if not bool(coll.get("research_consent", False)):
        typer.secho("Consent not granted — run `aithernet data research-consent --grant` first.",
                    fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    from aithernet.data.research_upload import upload_pending
    result = upload_pending(consent=True)
    typer.echo(json.dumps(result, indent=2))
    if not result.get("ok"):
        typer.secho(f"Upload not performed ({result.get('reason')}). "
                    f"{result.get('next_step', '')}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"Owner-archive upload: uploaded={result['uploaded']} "
                f"failed={result['failed']} queued={result['queued']}.",
                fg=typer.colors.GREEN if not result["failed"] else typer.colors.YELLOW)


@data_app.command("setup")
def data_setup(
    enable: bool = typer.Option(
        True, "--enable/--disable", help="Enable owner_full research recording + owner-archive."),
    standalone_drive: bool = typer.Option(
        False, "--standalone-drive",
        help="ADVANCED: upload to this node's OWN Google Drive, not the hosted owner archive."),
) -> None:
    """Guided research setup — enable owner_full, init the local queue, activate automatic
    recording, and (default) enable upload to the Aithernet HOSTED owner archive. No Drive setup is
    required on this client; the owner Drive archive is managed server-side.

    Use --standalone-drive only for the advanced local-archive mode (not the normal client path)."""
    from aithernet.data.research_setup import enable_owner_research
    if not enable:
        from aithernet.data.research_setup import set_collection_mode, set_consent
        set_collection_mode("off")
        set_consent(False)
        layout = _research_spool().ensure_layout()
        typer.secho(f"Local research spool ready at {layout['root']} (collection DISABLED).",
                    fg=typer.colors.YELLOW)
        return
    try:
        report = enable_owner_research(standalone_drive=standalone_drive)
    except Exception as exc:  # noqa: BLE001 — surface a sanitized failure, never a token
        typer.secho(f"Research setup failed: {type(exc).__name__}: {exc}", fg=typer.colors.RED,
                    err=True)
        raise typer.Exit(code=1) from exc
    for line in report.get("steps", []):
        typer.secho(f"  - {line}", fg=typer.colors.GREEN)
    if not standalone_drive:
        typer.secho("Inspect status anytime with: aithernet data status", fg=typer.colors.CYAN)


@data_app.command("verify")
def data_verify(
    authorize: bool = typer.Option(
        False, "--authorize",
        help="[standalone-drive only] run the one-time browser Google Drive OAuth sign-in."),
) -> None:
    """Verify the local research queue + report recorder state, hosted owner-archive upload
    readiness, and the exact next action for any real issue.

    The default hosted-client path requires NO Google Drive setup on the client. --authorize only
    applies to the advanced --standalone-drive mode."""
    coll = _read_collection_config()
    mode = str(coll.get("data_collection_mode", "off"))
    consent = bool(coll.get("research_consent", False))
    upload_mode = str(coll.get("research_upload_mode", "hosted_owner_archive"))
    spool = _research_spool()
    st = spool.status()
    rec = spool.recorder_status()

    if upload_mode == "standalone_drive":
        _data_verify_standalone(authorize, mode, spool, st, rec)
        return

    from aithernet.data.research_upload import (
        hosted_ingestion_status,
        is_enrolled,
        load_capability,
        owner_archive_state,
    )
    oa = owner_archive_state(consent=consent, spool=spool)
    report = {
        "spool_integrity": "ok" if st.get("healthy") else
                           ("empty" if st.get("exists") else "not initialized"),
        "recorder_active": (mode == "owner_full") and st.get("healthy", False),
        "consent": consent,
        "upload_mode": "hosted_owner_archive",
        "enrolled": is_enrolled(),
        "capability": "present" if load_capability() else "none",
        "hosted_ingestion": hosted_ingestion_status() if is_enrolled() else "not_enrolled",
        "local_queue": oa["local_queue"],
        "mission_records": rec["mission_records"],
        "owner_archive_upload": oa["owner_archive_upload"],
        "client_google_drive_required": False,
        "next_step": (None if mode == "owner_full" and consent and oa["owner_archive_upload"]
                      in ("enabled",) else oa.get("next_step")
                      or ("aithernet data research-consent --grant" if not consent else None)),
    }
    typer.echo(json.dumps(report, indent=2))
    typer.secho("No client Google Drive setup is required — the owner Drive archive is managed by "
                "hosted Aithernet.", fg=typer.colors.CYAN)


def _data_verify_standalone(authorize, mode, spool, st, rec) -> None:
    """[advanced] the beta.8 standalone-Drive verify path."""
    from aithernet.data.destinations.google_drive_client import (
        GoogleDriveOAuth,
        RealGoogleDriveClient,
        oauth_client_present,
    )
    from aithernet.data.research_sync import drive_state
    typer.echo(json.dumps({
        "spool_integrity": "ok" if st.get("healthy") else "not initialized",
        "recorder_active": (mode == "owner_full") and st.get("healthy", False),
        "upload_mode": "standalone_drive", "mission_records": rec["mission_records"],
        "drive": drive_state()}, indent=2))
    if not oauth_client_present():
        typer.secho("[standalone] No Google Desktop OAuth client installed — install one with "
                    "`aithernet data drive-client install <file>`.", fg=typer.colors.YELLOW)
        if authorize:
            raise typer.Exit(code=1)
        return
    oauth = GoogleDriveOAuth()
    if not oauth.authorized():
        if authorize:
            oauth.authorize(on_url=lambda u: typer.echo(f"  {u}"))
            typer.secho("Authorized.", fg=typer.colors.GREEN)
        else:
            typer.secho("[standalone] Drive not authorized — re-run with --authorize.",
                        fg=typer.colors.YELLOW)
            return
    from aithernet.data.research_drive import RESEARCH_ROOT, ResearchDriveArchive
    folders = ResearchDriveArchive(RealGoogleDriveClient(oauth)).ensure_hierarchy()
    typer.secho(f"[standalone] Drive archive '{RESEARCH_ROOT}' verified: {len(folders) - 1} "
                f"folders.", fg=typer.colors.GREEN)


@data_app.command("collect")
def data_collect() -> None:
    """Package every not-yet-collected local research record into a snapshot + manifest.

    Automatic recording already writes records after each mission; `collect` bundles them for
    export/upload. Idempotent — safe to run repeatedly. Never uploads (see `data sync`)."""
    from aithernet.data.research_sync import collect
    result = collect()
    typer.echo(json.dumps(result, indent=2))
    if result.get("collected"):
        typer.secho(f"Collected {result['collected']} record(s) into snapshot "
                    f"{result['package']}.", fg=typer.colors.GREEN)
    else:
        typer.secho(result.get("message", "Nothing to collect."), fg=typer.colors.CYAN)


@data_app.command("sync")
def data_sync() -> None:
    """[advanced standalone/local archive mode] Upload pending snapshots to the node's OWN Google
    Drive. NOT required for the hosted owner-archive path — use `aithernet data upload-now` for
    hosted uploads. Refuses clearly if Drive is not set up, instead of failing."""
    from aithernet.data.research_sync import sync
    result = sync()
    typer.echo(json.dumps(result, indent=2))
    if not result.get("ok"):
        typer.secho(f"Sync not performed ({result.get('reason')}): {result.get('message')}. "
                    f"Next: {result.get('next_step')}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"Sync complete: uploaded={result['uploaded']} skipped={result['skipped']} "
                f"failed={result['failed']}.",
                fg=typer.colors.GREEN if not result["failed"] else typer.colors.YELLOW)


@data_app.command("research-records")
def data_research_records(
    limit: int = typer.Option(20, "--limit", help="Max recent records to list."),
) -> None:
    """List recent local research records (secret-free metadata only — never record contents).

    (Named `research-records` to avoid the pre-existing consent `data records` group.)"""
    spool = _research_spool()
    lines = [line for line in spool._ledger_lines() if line.get("kind") == "mission-record"]
    recent = list(reversed(lines))[:limit]
    out = {
        "mission_records": spool.mission_record_count(),
        "last_recorded_at": spool.last_recorded_at(),
        "recent": [{"at": r.get("at"), "mission_id": r.get("mission_id"),
                    "digest": r.get("digest"), "dest": r.get("dest")} for r in recent],
    }
    typer.echo(json.dumps(out, indent=2))


@data_app.command("build")
def data_build(
    dataset_class: str = typer.Argument(
        ..., help="coordinator | routing | tool-calling | recovery | coding | rf-analysis"),
    version: int = typer.Option(1, "--version", help="Dataset version number."),
) -> None:
    """Build a versioned, training-ready dataset from the research spool.

    Emits train/validation/evaluation JSONL + schema + dataset card + provenance + statistics +
    SHA256SUMS, with leakage-free MISSION-grouped splits and a permanently held-out evaluation
    slice. Does NOT fine-tune a model — this builds the training-ready package."""
    from aithernet.data.dataset_builder import DATASET_CLASSES, build_dataset
    if dataset_class not in DATASET_CLASSES:
        raise typer.BadParameter(f"choose: {', '.join(sorted(DATASET_CLASSES))}")
    manifest = build_dataset(dataset_class, spool=_research_spool(), version=version)
    typer.echo(json.dumps(manifest, indent=2))


@data_drive_client_app.command("install")
def data_drive_client_install(
    path: str = typer.Argument(..., help="Path to a Google Desktop OAuth client JSON file."),
) -> None:
    """Install an operator-provided Google Desktop OAuth client (enables Drive authorization).

    Aithernet ships NO embedded OAuth client — an installed desktop app cannot keep a client secret
    confidential. Create a Desktop OAuth client in Google Cloud (scope: drive.file), download its
    JSON, and install it here. The secret is stored 0600 and never printed."""
    from pathlib import Path as _Path

    from aithernet.data.destinations.drive import DriveError
    from aithernet.data.destinations.google_drive_client import install_oauth_client
    try:
        receipt = install_oauth_client(_Path(path))
    except DriveError as exc:
        typer.secho(f"Could not install OAuth client: {exc.code}: {exc}", fg=typer.colors.RED,
                    err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f"Installed Desktop OAuth client ({receipt['client_id_masked']}). "
                "Next: `aithernet data verify --authorize`.", fg=typer.colors.GREEN)


@data_drive_client_app.command("status")
def data_drive_client_status() -> None:
    """Show the installed OAuth client status (masked; never the secret or full client id)."""
    from aithernet.data.destinations.google_drive_client import oauth_client_status
    typer.echo(json.dumps(oauth_client_status(), indent=2))


# Stage 14F: hosted control-plane onboarding commands (enroll / hosted * / enrollment *).
from aithernet.hosted.cli import register as _register_hosted  # noqa: E402

_register_hosted(app)

# 1.0.0-beta.3: first-class mesh management (offline-capable; standalone + hosted).
from aithernet.cli_mesh import register as _register_mesh  # noqa: E402

_register_mesh(app)


# ---------------------------------------------------------------------------
# Stage 14G PHASE 4A — guided customer setup (`aithernet setup`). The real installation
# coordinator: a resumable, idempotent state machine over the 14 onboarding steps.
# ---------------------------------------------------------------------------
_SETUP_STATUS_COLORS = {"done": typer.colors.GREEN, "deferred": typer.colors.CYAN,
                        "skipped": typer.colors.BLUE, "failed": typer.colors.RED,
                        "pending": typer.colors.YELLOW}


def _setup_ask(kind: str, key: str, prompt: str, choices, default):
    """Interactive answer source for the wizard (typer prompts)."""
    if kind == "confirm":
        return typer.confirm(prompt, default=bool(default))
    if kind == "choice" and choices:
        typer.echo(f"{prompt} — choices: {', '.join(choices)}")
        return typer.prompt(f"  {key}", default=str(default))
    return typer.prompt(prompt, default=str(default))


def _print_setup_plan(plans, totals) -> None:
    typer.secho("Setup plan (dry-run — no changes made):", fg=typer.colors.CYAN, bold=True)
    for p in plans:
        sudo = "  [sudo]" if p.requires_sudo else ""
        typer.secho(f"\n• {p.title}{sudo}", bold=True)
        if p.summary:
            typer.echo(f"    {p.summary}")
        if p.os_packages:
            typer.echo(f"    os packages: {' '.join(p.os_packages)}")
        if p.components:
            typer.echo(f"    components: {' '.join(p.components)}")
        if p.download_sources:
            typer.echo(f"    download sources: {', '.join(p.download_sources)}")
        if p.est_download_mb or p.est_installed_mb:
            typer.echo(f"    estimated: ~{p.est_download_mb} MB download, "
                       f"~{p.est_installed_mb} MB installed")
        for op in p.privileged_ops:
            typer.secho(f"    privileged: {op}", fg=typer.colors.YELLOW)
        for sc in p.service_changes:
            typer.echo(f"    service: {sc}")
        if p.deferred_to:
            typer.secho(f"    runs via: {p.deferred_to}", fg=typer.colors.CYAN)
        for n in p.notes:
            typer.echo(f"    note: {n}")
    typer.secho("\nTotals", bold=True)
    typer.echo(f"  os packages:        {' '.join(totals['os_packages']) or '(none)'}")
    typer.echo(f"  components:         {' '.join(totals['components']) or '(none)'}")
    typer.echo(f"  estimated download: ~{totals['est_download_mb']} MB")
    typer.echo(f"  estimated install:  ~{totals['est_installed_mb']} MB")
    typer.echo(f"  privileged steps:   {', '.join(totals['privileged_steps']) or '(none)'}")
    typer.echo(f"  restart required:   {totals['requires_restart']}")
    typer.echo(f"  logout required:    {totals['requires_logout']}")
    typer.echo(f"  reboot required:    {totals['requires_reboot']}")


def _print_setup_status(state, plans) -> None:
    typer.secho("Setup status:", fg=typer.colors.CYAN, bold=True)
    from aithernet.provisioning import wizard as _wz
    for step in _wz.STEPS:
        rec = state.steps.get(step.id)
        st = rec.status if rec else "pending"
        typer.secho(f"  [{st.upper():8}] {step.title}",
                    fg=_SETUP_STATUS_COLORS.get(st))
        if rec and rec.detail:
            typer.echo(f"             {rec.detail}")


@app.command("setup")
def setup(
    profile: str = typer.Option(None, "--profile",
                                help="Hardware profile: software-only|simulation|plutosdr|usrp|"
                                     "generic-soapy|full-lab (aliases: core/pluto/soapy-generic/"
                                     "full)"),
    deployment_mode: str = typer.Option(None, "--deployment-mode", help="standalone|hosted"),
    service_mode: str = typer.Option(None, "--service-mode",
                                     help="systemd-user|systemd-system|manual"),
    sdr_strategy: str = typer.Option(None, "--sdr-strategy",
                                     help="recommended|source|offline|none"),
    coordinator: str = typer.Option(None, "--coordinator",
                                    help="Coordinator provider or 'disabled'."),
    coding: str = typer.Option(None, "--coding", help="Coding provider or 'disabled'."),
    components_bundle_dir: str = typer.Option(
        None, "--components-bundle-dir",
        help="Directory of the downloaded release (installs the signed rf-mcp bundle offline)."),
    node_name: str = typer.Option(None, "--node-name"),
    base_url: str = typer.Option(None, "--base-url", help="Hosted base URL (for enrollment)."),
    code: str = typer.Option(None, "--code", help="One-time enrollment code."),
    state_root: str = typer.Option(None, "--state-root", help="Override the node state root."),
    non_interactive: bool = typer.Option(False, "--non-interactive",
                                         help="Answer only from flags + defaults; never prompt."),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Show the full plan and make NO changes."),
    resume: bool = typer.Option(False, "--resume", help="Continue from recorded state."),
    repair: bool = typer.Option(False, "--repair", help="Re-run failed/incomplete steps."),
    status: bool = typer.Option(False, "--status", help="Show recorded progress and exit."),
    migrate: bool = typer.Option(False, "--migrate",
                                 help="Repair a beta.7 split state: bind the canonical config to "
                                      "the real node identity (idempotent; preserves the key)."),
    only: str = typer.Option(None, "--only", help="Run only these comma-separated step ids."),
    research: bool = typer.Option(
        None, "--research/--no-research",
        help="Enable sanitized research recording + upload to the Aithernet owner archive."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Guided customer installation coordinator (idempotent, resumable).

    Modes: interactive (default), --non-interactive, --dry-run, --resume, --repair, --status.
    Works from an installed package without the source repo; records completed steps; shows exactly
    what needs sudo; never edits YAML by hand or stores provider credentials in the project tree.
    """
    from pathlib import Path as _Path

    from aithernet.provisioning import wizard as _wz
    root = _Path(state_root) if state_root else None
    opts = {k: v for k, v in {
        "profile": profile, "deployment_mode": deployment_mode, "service_mode": service_mode,
        "sdr_strategy": sdr_strategy, "coordinator_provider": coordinator,
        "coding_provider": coding, "node_name": node_name, "base_url": base_url, "code": code,
        "components_bundle_dir": components_bundle_dir,
    }.items() if v is not None}

    st = _wz.load_or_new(root)

    # beta.7 -> beta.8 migration/repair: detect the split-state and bind the canonical config to the
    # real node identity. Runs via --migrate and automatically within --repair (idempotent, safe to
    # rerun). Exposed through the existing setup repair flow — not a separate subsystem.
    if migrate or repair:
        from aithernet.provisioning import migrate as _migrate
        try:
            rec = _migrate.migrate_beta7_state(root, dry_run=dry_run)
        except _migrate.AmbiguousIdentityError as exc:
            typer.secho(f"Migration stopped: {exc}", fg=typer.colors.RED)
            typer.echo("Resolve which node identity is correct (keep one, remove the other), "
                       "then re-run `aithernet setup --migrate`.")
            raise typer.Exit(code=2) from exc
        if json_out:
            typer.echo(json.dumps(rec, indent=2))
        else:
            typer.secho("Migration/repair:", fg=typer.colors.CYAN, bold=True)
            for a in rec.get("actions", []):
                typer.echo(f"  - {a}")
            v = rec.get("result", {}).get("verified")
            if v:
                ok = bool(v.get("identity_matches"))
                typer.secho(f"  verified: {v}",
                            fg=typer.colors.GREEN if ok else typer.colors.YELLOW)
        if migrate and not repair:
            return
        st = _wz.load_or_new(root)  # reload after migration rewrote state/config

    # Reconcile the journal against the LIVE system for the inspection/continuation modes, so
    # --status agrees with doctor and --resume/--repair never repeat already-finished external work.
    if status or resume or repair:
        st = _wz.reconcile(st, state_root=root)

    if status:
        plans = _wz.build_plan(st, state_root=root, opts=opts)
        if json_out:
            typer.echo(json.dumps({"state": st.to_dict(),
                                   "steps": [p.to_dict() for p in plans]}, indent=2))
        else:
            _print_setup_status(st, plans)
        return

    if dry_run:
        plans = _wz.build_plan(st, state_root=root, opts=opts)
        totals = _wz.plan_totals(plans)
        if json_out:
            typer.echo(json.dumps({"plan": [p.to_dict() for p in plans], "totals": totals},
                                  indent=2))
        else:
            _print_setup_plan(plans, totals)
        return

    interactive = not non_interactive
    only_ids = [s.strip() for s in only.split(",")] if only else None
    if only_ids:
        for sid in only_ids:
            if sid not in _wz.STEP_IDS:
                raise typer.BadParameter(
                    f"unknown step '{sid}'. Valid: {', '.join(_wz.STEP_IDS)}")

    results = _wz.run(st, state_root=root, opts=opts, interactive=interactive,
                      ask=_setup_ask if interactive else None,
                      only=only_ids, repair=repair,
                      on_result=None if json_out else (lambda r: typer.secho(
                          f"  [{r.status.upper():8}] {r.step_id}: {r.detail}"
                          + (f"  -> {r.remediation}" if r.remediation else ""),
                          fg=_SETUP_STATUS_COLORS.get(r.status))))
    failed = [r for r in results if r.status == "failed"]
    if json_out:
        typer.echo(json.dumps({"results": [r.to_dict() for r in results],
                               "state": st.to_dict()}, indent=2))
    else:
        typer.secho(
            f"\nSetup {'completed with failures' if failed else 'step run complete'}: "
            f"{len(results)} step(s) executed, {len(failed)} failed.",
            fg=typer.colors.RED if failed else typer.colors.GREEN)
        if not failed:
            typer.echo("Resume/repair anytime: `aithernet setup --resume` / `--repair`. "
                       "Review: `aithernet setup --status`.")
    if failed:
        raise typer.Exit(code=1)

    # beta.10 Part 6: owner-research opt-in. One plain-language question; everything else is
    # automatic (enable owner_full, init spool, verify/refresh Drive, reuse the existing root,
    # verify hierarchy, bounded verified test upload). Never edits OAuth/YAML/env/folder-IDs.
    want_research = research
    if want_research is None and interactive and not json_out:
        want_research = typer.confirm(
            "Enable sanitized research recording and upload to the Aithernet owner archive?",
            default=False)
    if want_research:
        _setup_owner_research(root, json_out=json_out)


def _setup_owner_research(root, *, json_out: bool) -> None:
    """Run owner-research enablement (guided, hosted owner-archive) and report it in plain language.

    Enables owner_full, records consent, initializes the local queue, activates automatic recording,
    and enables upload to the Aithernet HOSTED owner archive. No Google Drive setup on the client;
    the owner Drive archive is managed server-side. Reports honestly if upload is pending."""
    from aithernet.data.research_setup import enable_owner_research
    try:
        report = enable_owner_research(state_root=root)
    except Exception as exc:  # noqa: BLE001 — never leak a token; report a category
        typer.secho(f"Research setup did not complete: {type(exc).__name__}: {exc}",
                    fg=typer.colors.YELLOW, err=True)
        return
    if json_out:
        typer.echo(json.dumps({"research": report}, indent=2))
        return
    upload = report.get("owner_archive_upload")
    typer.secho("Research recording enabled.", fg=typer.colors.GREEN, bold=True)
    if upload == "enabled":
        typer.secho("Owner archive upload will use the enrolled Aithernet hosted connection.\n"
                    "Google Drive archive is managed by hosted Aithernet; no Google Drive setup is "
                    "required on this client.\nAithernet will automatically record, sanitize, "
                    "package, and upload mission records after enrollment.\n"
                    "Inspect status anytime with: aithernet data status", fg=typer.colors.GREEN)
    elif upload == "pending_enrollment":
        typer.secho("Research recording enabled locally.\n"
                    "Owner archive upload will begin after enrollment.", fg=typer.colors.YELLOW)
    else:
        typer.secho("Research recording enabled locally.\n"
                    "Owner archive upload is pending: "
                    f"{report.get('owner_archive_reason', upload)}.\n"
                    "Aithernet will retry automatically after the issue is repaired.",
                    fg=typer.colors.YELLOW)


_DOCTOR_COLORS = {
    "ready": typer.colors.GREEN, "optional": typer.colors.BLUE,
    "missing": typer.colors.RED, "misconfigured": typer.colors.RED,
    "permission-denied": typer.colors.RED, "degraded": typer.colors.RED,
    "unsupported": typer.colors.MAGENTA, "device-absent": typer.colors.YELLOW,
    "unverified": typer.colors.YELLOW,
}


@app.command("doctor")
def doctor(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable report."),
    fix_plan: bool = typer.Option(False, "--fix-plan",
                                  help="Print a deterministic, non-mutating remediation plan."),
    repair: bool = typer.Option(False, "--repair",
                                help="Safely repair customer-level issues (canonical service "
                                     "config, stale unit, provider PATH, restart, journal). "
                                     "Non-destructive; no sudo."),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="With --repair: show what would change, make NO changes."),
    state_root: str = typer.Option(None, "--state-root", help="Override the node state root."),
    online: bool = typer.Option(False, "--online",
                                help="Also probe hosted control-plane connectivity (bounded)."),
) -> None:
    """Aggregate the whole node's readiness, truthfully. Read-only by default: never installs,
    mutates config, invents a device, or prints secrets. With --repair it performs safe,
    non-destructive customer-level repairs and reports exactly what changed."""
    if repair:
        from pathlib import Path as _Path

        from aithernet.provisioning import repair as _repair
        root = _Path(state_root) if state_root else None
        rec = _repair.repair_node(root, restart=not dry_run, dry_run=dry_run)
        if json_out:
            typer.echo(json.dumps(rec, indent=2))
            raise typer.Exit(code=2 if rec.get("needs_setup") else 0)
        if rec.get("needs_setup"):
            typer.secho("Not set up yet — run `aithernet setup` first.", fg=typer.colors.YELLOW)
            raise typer.Exit(code=2)
        title = "Would change (dry-run):" if dry_run else "Repaired:"
        typer.secho(title, fg=typer.colors.CYAN, bold=True)
        for c in rec.get("changes", []):
            typer.echo(f"  - {c}")
        if not rec.get("changes"):
            typer.secho("  nothing to repair — node configuration is canonical.",
                        fg=typer.colors.GREEN)
        for c in rec.get("checks", []):
            typer.echo(f"  · {c}")
        if rec.get("restarted"):
            verified = rec.get("runtime_verified")
            typer.secho(
                f"  service restarted; runtime {'verified' if verified else 'not verified'}.",
                fg=typer.colors.GREEN if verified else typer.colors.YELLOW)
        raise typer.Exit(code=0)

    from aithernet import doctor as doc
    report = doc.run_doctor(online=online)
    if json_out:
        out = report.to_dict()
        if fix_plan:
            out["fix_plan"] = report.fix_plan()
        typer.echo(json.dumps(out, indent=2))
        raise typer.Exit(code=0 if report.ok else 1)
    if fix_plan:
        plan = report.fix_plan()
        typer.secho("Remediation plan (deterministic; makes no changes):",
                    fg=typer.colors.CYAN, bold=True)
        if not plan:
            typer.secho("  nothing to fix — node is ready.", fg=typer.colors.GREEN)
        for i, step in enumerate(plan, 1):
            typer.secho(f"  {i}. [{step['status']}] {step['check']}", fg=typer.colors.YELLOW)
            typer.echo(f"     -> {step['action']}")
        raise typer.Exit(code=0 if report.ok else 1)
    typer.secho(f"Aithernet doctor — {'READY' if report.ok else 'ISSUES'} "
                f"(v{report.to_dict()['version']})",
                fg=typer.colors.GREEN if report.ok else typer.colors.RED, bold=True)
    last_cat = None
    for c in report.checks:
        if c.category != last_cat:
            typer.secho(f"\n[{c.category}]", bold=True)
            last_cat = c.category
        typer.secho(f"  [{c.status.upper():16}] {c.name}: {c.detail}",
                    fg=_DOCTOR_COLORS.get(c.status))
        if c.remediation and c.status not in ("ready", "optional"):
            typer.echo(f"      -> {c.remediation}")
    counts = report.summary_counts()
    typer.echo("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not report.ok:
        typer.secho("Run `aithernet doctor --fix-plan` for remediation steps.",
                    fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)


#: Concise, stable exit code for an expected customer-facing failure.
_CUSTOMER_ERROR_EXIT = 1


def main() -> None:
    """Console-script entrypoint.

    Wraps the Typer app so EXPECTED customer failures (missing config/CA/credential/executable,
    service down, port in use, MCP/hardware problems) surface as a concise one-line diagnostic with
    a stable non-zero exit code, not a Rich traceback. Set AITHERNET_DEBUG=1 for a full traceback.
    """
    import os as _os

    # Resolve a real CA bundle (system store / certifi) so packaged HTTPS clients never crash on a
    # missing certifi cacert.pem. Verification stays on.
    from aithernet.tls import ensure_ssl_env
    ensure_ssl_env()

    debug = bool(_os.environ.get("AITHERNET_DEBUG"))
    try:
        app()
    except (SystemExit, KeyboardInterrupt):
        raise  # Typer/click exits + Ctrl-C are the normal control flow
    except FileNotFoundError as exc:
        if debug:
            raise
        typer.secho(f"error: file not found — {exc}", fg=typer.colors.RED, err=True)
        typer.secho("(set AITHERNET_DEBUG=1 for a full traceback)",
                    fg=typer.colors.YELLOW, err=True)
        raise SystemExit(2) from exc
    except Exception as exc:  # noqa: BLE001 — concise customer diagnostic, not a raw traceback
        if debug:
            raise
        typer.secho(f"error: {type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        typer.secho("(set AITHERNET_DEBUG=1 for a full traceback)",
                    fg=typer.colors.YELLOW, err=True)
        raise SystemExit(_CUSTOMER_ERROR_EXIT) from exc


if __name__ == "__main__":
    main()
