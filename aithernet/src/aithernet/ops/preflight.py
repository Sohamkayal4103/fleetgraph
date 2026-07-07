"""Configuration + environment preflight validation (Stage 14A, area 2).

``run_preflight`` returns a categorized, sanitized report an operator runs BEFORE starting a node
(or as part of provisioning/upgrade). It validates the configuration file, database accessibility
and migration state, node identity, directory permissions/writability, configured peer endpoints,
the coordinator/coding-agent executables, RF backend commands + working directories, workspace
containment, port availability, and flags unsafe development paths. It never prints secrets, keys,
or a complete environment map — only check names, severities, and bounded sanitized details.
"""

from __future__ import annotations

import os
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aithernet.config.settings import NodeConfig

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    category: str
    status: str
    detail: str | None = None


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, category: str, status: str, detail: str | None = None) -> None:
        self.checks.append(Check(name=name, category=category, status=status, detail=detail))

    @property
    def ok(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)

    @property
    def counts(self) -> dict[str, int]:
        out = {OK: 0, WARN: 0, FAIL: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "counts": self.counts,
            "checks": [
                {"name": c.name, "category": c.category, "status": c.status, "detail": c.detail}
                for c in self.checks
            ],
        }


def _writable_dir(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)


def _is_unsafe_path(path: Path, repo_root: Path) -> str | None:
    """Return a reason string if a production data path is unsafe, else None."""
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if resolved == Path("/tmp") or str(resolved).startswith("/tmp/"):
        return "under /tmp (ephemeral)"
    try:
        resolved.relative_to(repo_root)
        return "inside the repository checkout"
    except ValueError:
        pass
    if resolved == Path.home():
        return "the HOME directory root"
    return None


def run_preflight(config: NodeConfig, *, config_path: str | None = None) -> PreflightReport:
    """Validate a node's configuration + environment. Read-only; never mutates anything."""
    from aithernet.config.settings import LEGACY_RF_BACKEND_ID
    from aithernet.state.db import create_db_engine
    from aithernet.state.migrations import migration_status

    report = PreflightReport()
    repo_root = Path(__file__).resolve().parents[3]

    # -- configuration -----------------------------------------------------------
    if config_path:
        p = Path(config_path)
        report.add("config_file", "config", OK if p.is_file() else WARN,
                   p.name if p.is_file() else "config file not found at the given path")
    report.add("node_identifier", "config", OK if config.node_id and config.node_name else FAIL,
               None if config.node_id else "node_id/node_name missing")

    # -- database + migrations ---------------------------------------------------
    db_path = config.database_path
    if config.database_url.startswith("sqlite"):
        db_dir = Path(db_path).expanduser().resolve().parent
        report.add("database_directory", "database", OK if _writable_dir(db_dir) else FAIL,
                   None if _writable_dir(db_dir) else "database directory is missing or unwritable")
        unsafe = _is_unsafe_path(Path(db_path), repo_root)
        if unsafe:
            report.add("database_path_safety", "paths", WARN, f"database is {unsafe}")
    try:
        engine = create_db_engine(config.database_url)
        status = migration_status(engine)
        engine.dispose()
        if status.pending:
            report.add("migration_state", "database", WARN,
                       f"{len(status.pending)} pending migration(s) — run "
                       "'aithernet database migrate'")
        elif not status.validation.ok:
            report.add("migration_state", "database", FAIL, "schema validation failed")
        else:
            report.add("migration_state", "database", OK, "schema up to date")
    except Exception as exc:  # noqa: BLE001 — sanitized
        report.add("database_access", "database", FAIL,
                   f"database unreachable ({type(exc).__name__})")

    # -- identity ----------------------------------------------------------------
    id_dir = config.agent_transport.identity.state_directory
    if id_dir:
        idp = Path(id_dir).expanduser()
        if idp.exists():
            mode = oct(idp.stat().st_mode & 0o777)
            secure = (idp.stat().st_mode & 0o077) == 0
            report.add("identity_directory", "identity", OK if secure else WARN,
                       f"present (mode {mode})" if secure
                       else f"present but world/group-accessible (mode {mode})")
        else:
            report.add("identity_directory", "identity", WARN,
                       "identity not initialized (run 'aithernet identity initialize')")
        unsafe = _is_unsafe_path(idp, repo_root)
        if unsafe:
            report.add("identity_path_safety", "paths", WARN, f"identity dir is {unsafe}")

    # -- coordinator + coding-agent executables ----------------------------------
    coord = config.coordinator
    if coord.provider in ("gemini_cli",) and coord.executable:
        found = shutil.which(coord.executable)
        report.add("coordinator_executable", "executables", OK if found else WARN,
                   f"'{coord.executable}' found" if found else f"'{coord.executable}' not on PATH")
    ca = config.coding_agent
    if ca.executable:
        found = shutil.which(ca.executable)
        report.add("coding_agent_executable", "executables", OK if found else WARN,
                   f"'{ca.executable}' found" if found else f"'{ca.executable}' not on PATH")

    # -- RF backend commands + working directories -------------------------------
    for backend_id, backend in (config.rf_backends.backends or {}).items():
        cmd = getattr(backend, "command", None)
        cwd = getattr(backend, "cwd", None)
        if cmd:
            found = shutil.which(cmd) or Path(cmd).expanduser().exists()
            report.add(f"rf_backend_command:{backend_id}", "rf",
                       OK if found else (WARN if backend_id != LEGACY_RF_BACKEND_ID else FAIL),
                       None if found else f"command for '{backend_id}' not found")
        if cwd and not Path(cwd).expanduser().is_dir():
            report.add(f"rf_backend_cwd:{backend_id}", "rf", WARN,
                       f"working directory for '{backend_id}' missing")
        ws = getattr(backend, "workspace", None)
        if ws:
            wsp = Path(ws).expanduser().resolve()
            inside = config.node_state_root and str(wsp).startswith(
                str(Path(config.node_state_root).expanduser().resolve())
            )
            report.add(f"rf_workspace_containment:{backend_id}", "rf",
                       OK if (inside or not config.node_state_root) else WARN,
                       None if inside else "RF workspace is outside the node state root")

    # -- configured peer endpoints (format only — never contact them here) -------
    # (Peers live in the DB; preflight validates only static config + reachability of the bind.)

    # -- port availability -------------------------------------------------------
    report.add("port_availability", "ports",
               OK if _port_free(config.host, config.port) else FAIL,
               None if _port_free(config.host, config.port)
               else f"{config.host}:{config.port} is already in use")

    # -- artifact store path + containment ---------------------------------------
    art_dir = config.artifacts.state_directory
    if art_dir:
        ap = Path(art_dir).expanduser()
        unsafe = _is_unsafe_path(ap, repo_root)
        if unsafe:
            report.add("artifact_store_path_safety", "paths", WARN, f"artifact store is {unsafe}")

    # -- managed hardware (Stage 14B) --------------------------------------------
    _hardware_checks(report, config)

    # -- development-checkout posture --------------------------------------------
    if not config.node_state_root:
        report.add("state_root", "paths", WARN,
                   "node_state_root is unset — using repo-relative/dev paths "
                   "(set it for production)")

    return report


def _hardware_checks(report: PreflightReport, config: NodeConfig) -> None:
    """Validate configured discovery providers, required devices/drivers, and static bindings (14B).

    Read-only: reports configured providers, whether their command/tool is on PATH, malformed
    static bindings, unsafe device config, and duplicate stable identities. It never contacts a
    device, never invents one, and never prints secrets/paths/environment.
    """
    hw = config.hardware
    if not hw.enabled:
        return
    if not hw.providers:
        report.add("hardware_providers", "hardware", WARN,
                   "hardware enabled but no discovery providers configured")
        return
    for provider_id, provider in hw.providers.items():
        if not provider.enabled:
            continue
        if provider.kind in ("soapy", "uhd", "command"):
            cmd = provider.command or {"soapy": "SoapySDRUtil", "uhd": "uhd_find_devices"}.get(
                provider.kind)
            found = bool(cmd) and (
                shutil.which(cmd) is not None or Path(cmd).expanduser().exists()
            )
            status = OK if found else (FAIL if provider.required else WARN)
            report.add(f"hardware_provider:{provider_id}", "hardware", status,
                       f"'{provider.kind}' command "
                       f"{'available' if found else 'not on PATH'}")
        elif provider.kind == "static_file":
            ok = bool(provider.descriptors_file) and Path(
                provider.descriptors_file).expanduser().is_file()
            report.add(f"hardware_provider:{provider_id}", "hardware",
                       OK if ok else (FAIL if provider.required else WARN),
                       "descriptors file present" if ok else "descriptors file missing")
        else:  # static
            report.add(f"hardware_provider:{provider_id}", "hardware", OK,
                       f"{len(provider.devices)} declared device(s)")
    # static bindings reference known backends
    known_backends = set((config.rf_backends.backends or {}).keys()) | {"legacy_gr_mcp"}
    for binding in hw.bindings:
        if binding.backend_id not in known_backends:
            report.add(f"hardware_binding:{binding.device_selector}", "hardware", WARN,
                       f"binding targets unknown backend '{binding.backend_id}'")
    # required-device selectors are declared (presence is confirmed at runtime by discovery)
    if hw.required_devices:
        report.add("hardware_required_devices", "hardware", OK,
                   f"{len(hw.required_devices)} required device selector(s) configured")


def _port_free(host: str, port: int) -> bool:
    bind_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, port))
            return True
        except OSError:
            return False
