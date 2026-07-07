"""Service-mode planning for the guided setup wizard (Stage 14G, PHASE 4A).

Two runtime identity models are supported (PHASE 6):

* **systemd-user** — the node runs as the desktop Linux user's *user* service, so it inherits that
  user's provider authentication. Installed without sudo under ``~/.config/systemd/user``.
* **systemd-system** — a headless/appliance service running under an explicit account, using
  service-configured credentials. Installing it is privileged; the wizard PLANS it and prints the
  exact ``sudo`` commands but never silently escalates.
* **manual** — no unit installed; the operator runs ``aithernet start`` themselves.

Unit *content* is generated deterministically here; only the user-unit install touches the
filesystem (and never with sudo).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

USER = "systemd-user"
SYSTEM = "systemd-system"
MANUAL = "manual"
SERVICE_MODES = (USER, SYSTEM, MANUAL)

UNIT_NAME = "aithernet-node.service"

#: A conservative system PATH the managed runtime env falls back to, so the service never depends on
#: an interactive login shell's PATH. Per-user provider directories (NVM/npm/distro) are PREPENDED.
_BASE_RUNTIME_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _exe() -> str:
    """Absolute path to the installed ``aithernet`` entrypoint (falls back to the bare name)."""
    return shutil.which("aithernet") or "aithernet"


def user_unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def runtime_env_file() -> Path:
    """The managed (non-secret) runtime environment file the service reads for PATH/runtime.

    Distinct from the optional 0600 ``aithernet.env`` secret file: this holds only the resolved
    PATH (and other non-secret runtime env) needed so CLI providers installed under per-user
    prefixes (NVM, npm prefix, distro) run identically from the service and the user shell — without
    relying on ``.bashrc``/``.profile`` or an interactive session, and without any manual drop-in.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "aithernet" / "runtime.env"


def _resolve_provider_executable(executable: str | None) -> str | None:
    """Resolve a CLI provider executable to an absolute path (literal file or PATH lookup)."""
    if not executable:
        return None
    candidate = Path(executable)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(executable)


#: Coordinator/coding provider keys whose runtime is a per-user CLI executable that needs its
#: install directory on PATH (so a Node-shebang script can find its own ``node`` sibling, etc.).
_CLI_PROVIDER_KEYS = frozenset({"gemini_cli", "codex_cli", "claude_code"})


def provider_executable_dirs(config) -> list[str]:
    """Directories that must be on the service PATH for the configured CLI providers.

    For each configured CLI provider (coordinator + coding) we resolve its executable to an
    absolute path and include the executable's directory — plus the directory of the real file when
    it is a symlink. For NVM/npm-prefix installs the CLI script and its ``node`` interpreter share
    that ``bin`` directory, so including it makes a ``#!/usr/bin/env node`` shim runnable under
    systemd. Returns a de-duplicated, order-preserving list; missing/unresolvable executables are
    skipped (never guessed).
    """
    dirs: list[str] = []
    seen: set[str] = set()

    def _add(path_str: str | None) -> None:
        if not path_str:
            return
        d = str(Path(path_str).parent)
        if d and d not in seen:
            seen.add(d)
            dirs.append(d)

    coord = config.coordinator
    coding = config.coding_agent
    specs = (
        (getattr(coord, "provider", None), getattr(coord, "executable", None)),
        (getattr(coding, "provider", None), getattr(coding, "executable", None)),
    )
    for provider, executable in specs:
        if provider not in _CLI_PROVIDER_KEYS:
            continue
        resolved = _resolve_provider_executable(executable)
        if resolved is None:
            continue
        _add(resolved)
        try:
            real = os.path.realpath(resolved)
        except OSError:
            real = resolved
        _add(real)
    return dirs


def compute_runtime_path(config, *, base_path: str | None = None) -> str:
    """The PATH string for the managed runtime env: provider dirs prepended to a sane base."""
    base = base_path or _BASE_RUNTIME_PATH
    provider_dirs = provider_executable_dirs(config)
    # Drop provider dirs already present in the base to keep PATH minimal and deterministic.
    base_parts = base.split(":")
    prefix = [d for d in provider_dirs if d not in base_parts]
    return ":".join([*prefix, base]) if prefix else base


def write_runtime_env(config, *, base_path: str | None = None) -> Path:
    """Write the managed (non-secret) runtime env file with the resolved provider PATH.

    Idempotent; returns the path. The systemd unit references this via ``EnvironmentFile=`` so the
    service runs CLI providers with the same PATH the user's shell would — re-run (and restart the
    service) whenever a provider is configured/connected so a newly installed CLI becomes runnable.
    """
    dest = runtime_env_file()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"PATH={compute_runtime_path(config, base_path=base_path)}\n")
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    return dest


def user_unit_text(*, state_root: Path | None = None) -> str:
    """A systemd *user* unit running the node foreground under the invoking user.

    Service correctness does NOT depend on an interactive shell: ``ExecStart`` pins the canonical
    config with an explicit ``--config`` selector (beta.9 — the env var alone proved insufficient on
    some installs), the canonical env vars are ALSO set (documented + tested), and a managed
    ``runtime.env`` supplies the provider PATH so CLI providers run as they do from the user shell.
    """
    cfg_path = (Path(state_root) / "config" / "node.yaml") if state_root else None
    # Defect 1: the supported configuration selector is part of ExecStart, not env-only.
    cfg_flag = f" --config {cfg_path}" if cfg_path else ""
    env_lines = ""
    if state_root:
        env_lines += f"Environment=AITHERNET_STATE_ROOT={state_root}\n"
        env_lines += f"Environment=AITHERNET_CONFIG={cfg_path}\n"
    return (
        "[Unit]\n"
        "Description=Aithernet node runtime (user service)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        # Managed (non-secret) runtime env: resolved provider PATH. Defect 2 — CLI providers under
        # NVM/npm/distro run from the service exactly as from the shell, with no manual drop-in.
        "EnvironmentFile=-%h/.config/aithernet/runtime.env\n"
        # Optional 0600 env file for appliance-style API keys under this user (e.g. GEMINI_API_KEY).
        # The leading '-' means "ignore if absent" — workstation CLI auth needs no env file.
        "EnvironmentFile=-%h/.config/aithernet/aithernet.env\n"
        f"{env_lines}"
        f"ExecStart={_exe()} start{cfg_flag}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def system_unit_dirs() -> list[Path]:
    """The directories a SYSTEM node unit may be installed in. Exposed as a function so tests can
    isolate detection from whatever happens to be installed on the build/development host."""
    return [Path("/etc/systemd/system"), Path("/lib/systemd/system")]


def installed_scope() -> str | None:
    """Which scope the node unit is installed under: ``user``, ``system``, or ``None``."""
    if (user_unit_dir() / UNIT_NAME).is_file():
        return "user"
    for directory in system_unit_dirs():
        if (directory / UNIT_NAME).is_file():
            return "system"
    return None


def systemctl_base(scope: str) -> list[str]:
    """The ``systemctl`` prefix for a scope (``--user`` for a user service)."""
    return ["systemctl", "--user"] if scope == "user" else ["systemctl"]


# ---------------------------------------------------------------------------
# beta.10 Defect 1: propagate a stored secret to the RUNNING managed service and
# verify the service process actually sees it — generically, for ANY secret name,
# without ever reading or printing the secret VALUE.
# ---------------------------------------------------------------------------
def restart_service(timeout: float = 20.0) -> tuple[int, str]:
    """Restart the installed managed node service so it reloads its EnvironmentFile.

    User scope restarts without sudo. System scope cannot be restarted unprivileged here;
    we report that the operator must restart it. Returns ``(returncode, detail)`` where
    rc 0 means restarted, and a negative-style sentinel detail flags an unmanaged node.
    """
    import subprocess

    scope = installed_scope()
    if scope is None:
        return 1, "no managed service installed (foreground `aithernet start`)"
    if scope == "system":
        return 2, f"system service: restart with `sudo systemctl restart {UNIT_NAME}`"
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "restart", UNIT_NAME],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.returncode, (proc.stderr or proc.stdout or "").strip()
    except FileNotFoundError:
        return 127, "systemctl not available"
    except subprocess.TimeoutExpired:
        return 124, "restart timed out"


def service_main_pid() -> int | None:
    """The MainPID of the running user service, or None if not running/unavailable."""
    import subprocess

    if installed_scope() != "user":
        return None
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", UNIT_NAME, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=5.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    raw = (proc.stdout or "").strip()
    if not raw.isdigit():
        return None
    pid = int(raw)
    return pid or None


def service_sees_env(name: str) -> bool | None:
    """Whether the RUNNING service process has env var ``name`` set to a non-empty value.

    Reads ``/proc/<pid>/environ`` of the service's MainPID. Returns True/False, or None when it
    cannot be determined (service not running, no MainPID, or environ unreadable). The secret
    VALUE is never returned, logged, or printed — only presence is reported.
    """
    pid = service_main_pid()
    if pid is None:
        return None
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (OSError, PermissionError):
        return None
    prefix = (name + "=").encode()
    for entry in raw.split(b"\x00"):
        if entry.startswith(prefix):
            return len(entry) > len(prefix)  # present AND non-empty
    return False


def propagate_secret_to_service(name: str) -> dict:
    """Make a freshly stored secret available to the running service and verify it landed.

    Restarts the managed service (so it reloads the 0600 credential EnvironmentFile) and then
    confirms the service process sees ``name``. Never touches the secret value. Returns a record
    describing scope, restart outcome, and whether the running service now sees the secret.
    """
    scope = installed_scope()
    record: dict = {"secret": name, "scope": scope, "restarted": False,
                    "service_sees_secret": None, "detail": ""}
    if scope is None:
        record["detail"] = ("no managed service — a foreground `aithernet start` will read the "
                            "credential file on next launch")
        return record
    if scope == "system":
        record["detail"] = f"system service — restart with `sudo systemctl restart {UNIT_NAME}`"
        return record
    rc, detail = restart_service()
    record["restarted"] = rc == 0
    record["detail"] = detail
    if rc == 0:
        # Give the service a brief moment to come up and load its environment.
        import time
        for _ in range(20):
            seen = service_sees_env(name)
            if seen is True:
                break
            time.sleep(0.25)
        record["service_sees_secret"] = service_sees_env(name)
    return record


def system_unit_text(*, run_user: str = "aithernet", state_root: str = "/var/lib/aithernet") -> str:
    """A systemd *system* unit for a headless appliance running under a dedicated account."""
    return (
        "[Unit]\n"
        "Description=Aithernet node runtime (system service)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={run_user}\n"
        f"Environment=AITHERNET_STATE_ROOT={state_root}\n"
        f"Environment=AITHERNET_CONFIG={state_root}/config/node.yaml\n"
        # Defect 1: ExecStart pins the canonical config explicitly (not env-only).
        f"ExecStart={_exe()} start --config {state_root}/config/node.yaml\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "NoNewPrivileges=true\n"
        "ProtectSystem=strict\n"
        f"ReadWritePaths={state_root}\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def plan(service_mode: str, *, state_root: Path | None = None) -> dict:
    """Describe what installing the chosen service mode entails (no side effects)."""
    if service_mode == USER:
        dest = user_unit_dir() / UNIT_NAME
        return {
            "service_mode": USER,
            "privileged": False,
            "unit_path": str(dest),
            "install_commands": [
                f"(write {dest})",
                "systemctl --user daemon-reload",
                f"systemctl --user enable --now {UNIT_NAME}",
            ],
            "requires_logout": False,
            "requires_lingering": True,
            "note": "Runs as your user; inherits your provider authentication. No sudo required. "
                    "For it to run while logged out: `loginctl enable-linger $USER`.",
        }
    if service_mode == SYSTEM:
        dest = Path("/etc/systemd/system") / UNIT_NAME
        return {
            "service_mode": SYSTEM,
            "privileged": True,
            "unit_path": str(dest),
            "install_commands": [
                "sudo useradd --system --home /var/lib/aithernet --shell /usr/sbin/nologin "
                "aithernet  # if absent",
                f"sudo install -m 0644 /dev/stdin {dest}  # the generated unit",
                "sudo systemctl daemon-reload",
                f"sudo systemctl enable --now {UNIT_NAME}",
            ],
            "requires_logout": False,
            "requires_lingering": False,
            "note": "Headless/appliance service under a dedicated account; needs "
                    "service-configured credentials (not a desktop session). Privileged — "
                    "review before running.",
        }
    return {
        "service_mode": MANUAL,
        "privileged": False,
        "unit_path": None,
        "install_commands": ["aithernet start  # run in the foreground yourself"],
        "requires_logout": False,
        "requires_lingering": False,
        "note": "No service unit installed; you start the node manually.",
    }


def install_user_unit(*, state_root: Path | None = None) -> Path:
    """Write (idempotently) the systemd *user* unit. No sudo. Returns the unit path.

    Writing the file does not start anything — the wizard prints the ``systemctl --user`` commands
    for the operator (or runs them only when explicitly confirmed).
    """
    dest = user_unit_dir() / UNIT_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(user_unit_text(state_root=state_root))
    return dest
