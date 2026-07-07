"""beta.9 Defect 6: ``aithernet doctor --repair`` — safe, non-destructive, customer-level repair.

Repairs the exact issues the beta.8 customer hit, WITHOUT editing YAML/systemd by hand, without
sudo, and without deleting any state or identity:

* a generated service unit that does not pin the canonical config (missing ``--config``);
* a stale generated unit;
* a missing/outdated managed runtime PATH for CLI providers (NVM/npm/distro);
* a stale setup journal (reconciled to the real on-disk reality);
* the service not restarted after configuration (so the runtime adopts the repaired config/PATH).

It reports exactly what it changed and never performs a privileged or destructive system change.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _restart_user_service(timeout: float = 20.0) -> tuple[int, str]:
    """Restart the systemd *user* service (no sudo). Returns (returncode, detail)."""
    from aithernet.provisioning import services
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "restart", services.UNIT_NAME],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.returncode, (proc.stderr or proc.stdout or "").strip()
    except FileNotFoundError:
        return 127, "systemctl not available"
    except subprocess.TimeoutExpired:
        return 124, "restart timed out"


def _verify_runtime(cfg, timeout: float = 5.0) -> bool:
    """Bounded best-effort check that the running node answers on its canonical bind address."""
    try:
        import httpx

        from aithernet.tls import verify_option
        with httpx.Client(base_url=cfg.base_url, timeout=timeout, verify=verify_option()) as c:
            r = c.get("/healthz")
        return r.is_success
    except Exception:  # noqa: BLE001 — verification is best-effort
        return False


def repair_node(state_root: Path | None = None, *, restart: bool = True,
                dry_run: bool = False) -> dict:
    """Repair customer-level node issues idempotently. Returns a record of checks + changes."""
    from aithernet.config.loader import load_config
    from aithernet.provisioning import services
    from aithernet.provisioning.state import default_state_root

    root = Path(state_root) if state_root else default_state_root()
    cfg_path = root / "config" / "node.yaml"
    record: dict = {"root": str(root), "checks": [], "changes": [], "restarted": False,
                    "runtime_verified": None, "dry_run": dry_run, "needs_setup": False}

    # 0) Canonical config must exist — without it, the right action is guided setup, not a repair.
    if not cfg_path.is_file():
        record["needs_setup"] = True
        record["checks"].append("canonical config: MISSING — run `aithernet setup` first")
        return record
    record["checks"].append(f"canonical config: present ({cfg_path})")
    cfg = load_config(str(cfg_path))

    # 1+2) The generated user unit must pin the canonical config (--config in ExecStart). Reinstall
    #      if absent or stale. Idempotent; no sudo.
    scope = services.installed_scope()
    if scope in (None, "user"):
        want_unit = services.user_unit_text(state_root=root)
        unit_path = services.user_unit_dir() / services.UNIT_NAME
        current = unit_path.read_text() if unit_path.is_file() else None
        if current != want_unit:
            why = "missing" if current is None else "stale (no/incorrect canonical --config)"
            record["changes"].append(f"reinstall user unit pinned to {cfg_path} [{why}]")
            if not dry_run:
                services.install_user_unit(state_root=root)
        else:
            record["checks"].append("user service unit: canonical (--config pinned)")

    # 3) Managed runtime PATH for CLI providers (NVM/npm/distro) so the service can run them.
    want_env = f"PATH={services.compute_runtime_path(cfg)}\n"
    env_path = services.runtime_env_file()
    cur_env = env_path.read_text() if env_path.is_file() else None
    dirs = services.provider_executable_dirs(cfg)
    if cur_env != want_env:
        record["changes"].append(
            f"write managed runtime PATH for {len(dirs)} provider dir(s) [{env_path}]")
        if not dry_run:
            services.write_runtime_env(cfg)
    else:
        record["checks"].append("managed runtime PATH: current")

    # 4) Reconcile the setup journal against on-disk reality (no fabricated statuses).
    try:
        from aithernet.provisioning import wizard
        st = wizard.load_or_new(root)
        if not dry_run:
            wizard.reconcile(st, state_root=root)
        record["changes"].append("reconcile setup journal to on-disk reality")
    except Exception as exc:  # noqa: BLE001 — reconciliation is best-effort
        record["checks"].append(f"reconcile: skipped ({type(exc).__name__})")

    # 5) Restart the service so the runtime adopts the repaired unit/PATH, then verify it answers.
    if restart and not dry_run and services.installed_scope() == "user":
        rc, detail = _restart_user_service()
        record["restarted"] = rc == 0
        record["changes"].append(
            f"restart {services.UNIT_NAME} (rc={rc}{'; ' + detail if detail else ''})")
        if rc == 0:
            record["runtime_verified"] = _verify_runtime(cfg)

    return record
