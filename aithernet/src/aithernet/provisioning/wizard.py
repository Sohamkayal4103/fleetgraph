"""Guided setup state machine (Stage 14G, PHASE 4A).

``aithernet setup`` is the customer installation coordinator. It runs a fixed, ordered list of
:class:`Step` objects, each of which can be **planned** (no side effects — used by ``--dry-run`` and
``--status``) and **applied**. Progress and selections are persisted via
:mod:`aithernet.provisioning.state` so a run can be interrupted and resumed, re-run idempotently, or
repaired. The machine itself is UI-agnostic: the CLI supplies an ``ask`` callback for interactive
prompts; tests supply a scripted one; non-interactive runs answer purely from flags + defaults.

No step here ever edits YAML by hand, prints a secret, or silently escalates privilege. Steps whose
execution belongs to a dedicated command (SDR install, component install, provider configuration)
PLAN fully and mark themselves ``deferred`` with the exact follow-up command.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aithernet.provisioning import profiles, services
from aithernet.provisioning.state import (
    CONFIGURED,
    DEFERRED,
    DONE,
    FAILED,
    SKIPPED,
    SetupState,
    load_state,
    save_state,
)

# Rough per-package download/installed-size estimates (MB), clearly labelled as estimates in
# output. Real figures depend on apt's resolver; unknown packages fall back to a small default.
_PKG_EST_MB: dict[str, tuple[float, float]] = {
    "gnuradio": (90.0, 420.0),
    "gr-osmosdr": (3.0, 12.0),
    "gr-iio": (2.0, 8.0),
    "gr-soapy": (2.0, 8.0),
    "soapysdr-tools": (1.0, 4.0),
    "soapysdr-module-all": (3.0, 12.0),
    "soapysdr-module-plutosdr": (1.0, 3.0),
    "soapysdr-module-uhd": (1.0, 4.0),
    "libiio-utils": (1.0, 3.0),
    "libiio0t64": (1.0, 3.0),
    "uhd-host": (40.0, 120.0),
    "libuhd-dev": (15.0, 60.0),
    "python3-numpy": (4.0, 30.0),
    "python3-packaging": (0.3, 1.0),
}
_DEFAULT_PKG_EST = (1.0, 4.0)


def _estimate(pkgs: list[str]) -> tuple[float, float]:
    dl = sum(_PKG_EST_MB.get(p, _DEFAULT_PKG_EST)[0] for p in pkgs)
    sz = sum(_PKG_EST_MB.get(p, _DEFAULT_PKG_EST)[1] for p in pkgs)
    return round(dl, 1), round(sz, 1)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _resolved(ctx: SetupContext, key: str, default: str) -> str:
    """Resolve a selection for PLAN rendering the same way apply will: explicit opt → recorded
    state → default. This keeps ``--dry-run`` honest about the modes the operator actually chose
    (so e.g. ``--deployment-mode standalone`` is reflected in the plan, not just at execution)."""
    val = ctx.opts.get(key)
    if val in (None, ""):
        val = getattr(ctx.state, key, None)
    return str(val) if val not in (None, "") else default


def _bundle_dir(ctx: SetupContext) -> str | None:
    import os
    return (ctx.opts.get("components_bundle_dir")
            or os.environ.get("AITHERNET_COMPONENT_BUNDLE_DIR"))


def _offline_os_payload(ctx: SetupContext) -> tuple[str | None, list[str]]:
    """Locate pre-staged OS-package payloads for an OFFLINE install.

    Convention: ``<components-bundle-dir>/os-packages/*.deb``. Returns ``(dir, debs)``; ``debs`` is
    empty when no complete offline OS payload is staged (offline must then stop clearly rather than
    silently fall back to apt/network)."""
    bundle = _bundle_dir(ctx)
    if not bundle:
        return None, []
    d = Path(bundle) / "os-packages"
    debs = sorted(str(p) for p in d.glob("*.deb")) if d.is_dir() else []
    return (str(d) if d.is_dir() else None), debs


@dataclass
class StepPlan:
    """A non-mutating description of what a step would do (the unit of ``--dry-run`` output)."""

    step_id: str
    title: str
    summary: str = ""
    requires_sudo: bool = False
    os_packages: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    download_sources: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    est_download_mb: float = 0.0
    est_installed_mb: float = 0.0
    privileged_ops: list[str] = field(default_factory=list)
    service_changes: list[str] = field(default_factory=list)
    requires_restart: bool = False
    requires_logout: bool = False
    requires_reboot: bool = False
    deferred_to: str = ""        # follow-up command that performs the real work, if any
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id, "title": self.title, "summary": self.summary,
            "requires_sudo": self.requires_sudo, "os_packages": self.os_packages,
            "components": self.components, "download_sources": self.download_sources,
            "versions": self.versions, "est_download_mb": self.est_download_mb,
            "est_installed_mb": self.est_installed_mb, "privileged_ops": self.privileged_ops,
            "service_changes": self.service_changes, "requires_restart": self.requires_restart,
            "requires_logout": self.requires_logout, "requires_reboot": self.requires_reboot,
            "deferred_to": self.deferred_to, "notes": self.notes,
        }


@dataclass
class StepResult:
    step_id: str
    status: str
    detail: str = ""
    remediation: str = ""

    def to_dict(self) -> dict:
        return {"step_id": self.step_id, "status": self.status, "detail": self.detail,
                "remediation": self.remediation}


class SetupContext:
    """Carries selections + answer sources through a run.

    ``opts`` are explicit flag values (non-interactive). ``ask`` is an optional callback
    ``ask(kind, key, prompt, choices, default) -> str|bool`` used only in interactive mode. Answers
    resolve as: explicit opt → interactive prompt → default. Every resolved answer is stored back on
    the state so ``--resume`` and ``--status`` reflect it.
    """

    def __init__(self, state: SetupState, *, state_root: Path | None,
                 opts: dict | None = None, interactive: bool = False,
                 ask: Callable | None = None) -> None:
        self.state = state
        self.state_root = state_root
        self.opts = opts or {}
        self.interactive = interactive
        self._ask = ask

    def _resolve(self, kind: str, key: str, prompt: str, choices, default):
        val = self.opts.get(key)
        if val is None or val == "":
            if self.interactive and self._ask is not None:
                val = self._ask(kind, key, prompt, choices, default)
            else:
                val = default
        return val

    def choose(self, key: str, prompt: str, choices: list[str], default: str) -> str:
        val = str(self._resolve("choice", key, prompt, choices, default))
        if choices and val not in choices:
            raise ValueError(f"{key}: '{val}' is not one of {choices}")
        return val

    def text(self, key: str, prompt: str, default: str) -> str:
        return str(self._resolve("text", key, prompt, None, default))

    def confirm(self, key: str, prompt: str, default: bool) -> bool:
        val = self._resolve("confirm", key, prompt, None, default)
        if isinstance(val, str):
            val = val.strip().lower() in ("1", "true", "yes", "y", "on")
        return bool(val)


@dataclass
class Step:
    id: str
    title: str
    category: str
    plan: Callable[[SetupContext], StepPlan]
    apply: Callable[[SetupContext], StepResult]


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------
def _p(step_id: str, title: str, **kw) -> StepPlan:
    return StepPlan(step_id=step_id, title=title, **kw)


# 1 — deployment mode -------------------------------------------------------
def _plan_deployment(ctx: SetupContext) -> StepPlan:
    return _p("deployment_mode", "Deployment mode",
              summary="Choose 'standalone' (local only) or 'hosted' (enroll with the control "
                      "plane for fleet management + updates).")


def _apply_deployment(ctx: SetupContext) -> StepResult:
    mode = ctx.choose("deployment_mode", "Deployment mode", ["standalone", "hosted"],
                      ctx.state.deployment_mode or "standalone")
    ctx.state.deployment_mode = mode
    return StepResult("deployment_mode", DONE, f"deployment_mode={mode}")


# 2 — node identity ---------------------------------------------------------
def _plan_identity(ctx: SetupContext) -> StepPlan:
    return _p("node_identity", "Node identity",
              summary="Generate (or reuse) this node's Ed25519 identity. Idempotent; the private "
                      "key never leaves the node and is never printed.")


def _apply_identity(ctx: SetupContext) -> StepResult:
    from aithernet.provisioning.state import default_state_root
    from aithernet.transport.identity import IdentityManager
    root = ctx.state_root or default_state_root()
    name = ctx.text("node_name", "Node name", ctx.state.node_name or "aithernet-node")
    ctx.state.node_name = name
    mgr = IdentityManager(root / "identity", node_id=name, node_name=name)
    identity = mgr.initialize()  # idempotent
    return StepResult("node_identity", DONE, f"identity {identity.fingerprint[:16]}… ({name})")


# 3 — directories + permissions --------------------------------------------
_DIRS = (("config", 0o750), ("identity", 0o700), ("db", 0o750),
         ("artifacts", 0o750), ("logs", 0o750), ("setup", 0o700))


def _plan_directories(ctx: SetupContext) -> StepPlan:
    from aithernet.provisioning.state import default_state_root
    root = ctx.state_root or default_state_root()
    return _p("directories", "Directories and permissions",
              summary=f"Create the node state tree under {root} with restrictive permissions, and "
                      "bind the canonical node.yaml (real node id + absolute paths).",
              notes=[f"{root}/{d} ({oct(m)})" for d, m in _DIRS])


def _canonical_node_id(ctx: SetupContext) -> str:
    """A stable, real node id (uuid5 over name+created_at), generated once and persisted in setup
    state — never the placeholder ``00000000-0000-4000-8000-000000000001`` the loader falls back
    to when no config is present."""
    import uuid
    if not ctx.state.node_id:
        seed = f"{ctx.state.node_name or 'aithernet-node'}:{ctx.state.created_at or ''}"
        ctx.state.node_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"aithernet-node:{seed}"))
    return ctx.state.node_id


def write_canonical_config(ctx: SetupContext) -> dict:
    """Write/merge the ONE canonical ``node.yaml`` binding the runtime to absolute, state-rooted
    paths + a real node id, WITHOUT clobbering provider sections written by ``agents configure``.

    The service loads exactly this file (the systemd unit pins AITHERNET_CONFIG to it). It fixes the
    beta.7 split where the service silently used a placeholder node id, a cwd-relative
    ``./aithernet.db`` and a different identity directory than setup created.
    """
    from aithernet.agents import providers as prov
    from aithernet.ops.paths import NodePaths
    from aithernet.provisioning.state import default_state_root

    root = ctx.state_root or default_state_root()
    paths = NodePaths.from_root(root)
    node_id = _canonical_node_id(ctx)
    name = ctx.state.node_name or "aithernet-node"

    data = prov._read_node_yaml(ctx.state_root)  # preserve coordinator/coding sections if present
    data["node_id"] = node_id
    data["node_name"] = name
    data["node_state_root"] = str(paths.root)
    data["database_url"] = paths.database_url
    transport = data.setdefault("agent_transport", {})
    transport.setdefault("enabled", True)
    identity = transport.setdefault("identity", {})
    identity["state_directory"] = str(paths.identity_dir)  # same dir setup creates the key in
    artifacts = data.setdefault("artifacts", {})
    artifacts.setdefault("enabled", True)
    artifacts["state_directory"] = str(paths.artifacts_dir)
    coding = data.setdefault("coding_agent", {})
    coding["workspace"] = str(paths.coding_dir)            # absolute, bounded coding workspace
    prov._write_node_yaml(data, ctx.state_root)
    return {"config": str(prov.node_config_path(ctx.state_root)), "node_id": node_id,
            "database_url": paths.database_url, "identity_dir": str(paths.identity_dir)}


def _apply_directories(ctx: SetupContext) -> StepResult:
    import os

    from aithernet.provisioning.state import default_state_root
    root = ctx.state_root or default_state_root()
    created = []
    for sub, mode in _DIRS:
        path = root / sub
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(sub)
        os.chmod(path, mode)
    # Bind the ONE canonical config now (real node id + absolute db/identity/workspace) so the
    # service never falls back to a placeholder id, a cwd database, or a divergent identity dir.
    info = write_canonical_config(ctx)
    return StepResult("directories", DONE,
                      f"ensured {len(_DIRS)} dirs ({len(created)} created) under {root}; "
                      f"canonical config bound (node_id={info['node_id'][:8]}…, absolute paths)")


# 4 — hosted enrollment -----------------------------------------------------
def _plan_enrollment(ctx: SetupContext) -> StepPlan:
    # Reflect the SELECTED deployment mode: standalone omits hosted enrollment entirely (no base
    # URL, no code, zero control-plane requests). Only hosted mode plans enrollment.
    if _resolved(ctx, "deployment_mode", "standalone") != "hosted":
        return _p("hosted_enrollment", "Hosted enrollment",
                  summary="Standalone deployment: hosted enrollment is skipped — the node makes "
                          "zero control-plane requests and needs no base URL or enrollment code.",
                  notes=["skipped (standalone deployment)"])
    return _p("hosted_enrollment", "Hosted enrollment",
              summary="Enroll the node with the hosted control plane using a one-time code.",
              download_sources=["hosted control plane (HTTPS)"],
              deferred_to="aithernet enroll --base-url <url> --code <code>")


def _apply_enrollment(ctx: SetupContext) -> StepResult:
    if ctx.state.deployment_mode != "hosted":
        return StepResult("hosted_enrollment", SKIPPED, "standalone deployment; no enrollment")
    base_url = ctx.text("base_url", "Hosted base URL", ctx.opts.get("base_url", "") or "")
    code = ctx.text("code", "Enrollment code", ctx.opts.get("code", "") or "")
    if not (base_url and code):
        return StepResult("hosted_enrollment", DEFERRED,
                          "no base URL/code supplied",
                          "run `aithernet enroll --base-url <url> --code <code>` when you have a "
                          "code from your customer portal")
    try:
        from aithernet.hosted.cli import _top_enroll
        _top_enroll(base_url=base_url, code=code, node_name=ctx.state.node_name or None,
                    state_root=ctx.state_root)
    except SystemExit as exc:  # typer.Exit
        if getattr(exc, "code", 0) not in (0, None):
            return StepResult("hosted_enrollment", FAILED, "enrollment failed",
                              "verify the base URL and that the code is unused/unexpired")
    return StepResult("hosted_enrollment", DONE, f"enrolled with {base_url}")


# 5 — service mode (selection) ---------------------------------------------
def _plan_service_mode(ctx: SetupContext) -> StepPlan:
    return _p("service_mode", "Service mode",
              summary="Choose how the node runs: systemd-user (inherits your provider auth), "
                      "systemd-system (headless appliance), or manual.")


def _apply_service_mode(ctx: SetupContext) -> StepResult:
    default = ctx.state.service_mode or services.USER
    mode = ctx.choose("service_mode", "Service mode", list(services.SERVICE_MODES), default)
    ctx.state.service_mode = mode
    # identity model is implied by service mode but can be chosen explicitly.
    idm = "appliance" if mode == services.SYSTEM else "workstation"
    ctx.state.identity_model = ctx.choose("identity_model", "Runtime identity model",
                                          ["workstation", "appliance"],
                                          ctx.state.identity_model or idm)
    return StepResult("service_mode", DONE,
                      f"service_mode={mode}, identity_model={ctx.state.identity_model}")


# 6 — hardware profile ------------------------------------------------------
def _plan_hardware_profile(ctx: SetupContext) -> StepPlan:
    name = ctx.opts.get("profile") or ctx.state.hardware_profile or "software-only"
    try:
        prof = profiles.resolve(name)
        notes = [f"{prof.name} -> internal '{prof.internal}': {prof.summary}"]
    except KeyError as exc:
        notes = [str(exc)]
    return _p("hardware_profile", "Hardware profile",
              summary="Select the customer hardware profile (drives SDR dependencies + missions).",
              notes=notes)


def _apply_hardware_profile(ctx: SetupContext) -> StepResult:
    default = ctx.state.hardware_profile or "software-only"
    name = ctx.choose("profile", "Hardware profile", profiles.profile_names(), default)
    prof = profiles.resolve(name)
    ctx.state.hardware_profile = prof.name
    return StepResult("hardware_profile", DONE, f"profile={prof.name} (internal '{prof.internal}')")


# 7 — SDR dependency strategy ----------------------------------------------
_SDR_STRATEGIES = ("recommended", "source", "offline", "none")


def _profile_apt(ctx: SetupContext) -> list[str]:
    from aithernet.hardware import setup as hw
    # Resolve the SELECTED profile (opt → state → default) so the plan and apply compute the same
    # package set — `--profile X --dry-run` must reflect X, not the recorded/default profile.
    name = ctx.opts.get("profile") or ctx.state.hardware_profile or "software-only"
    try:
        internal = profiles.internal_name(name)
        return list(hw.PROFILES.get(internal, {}).get("apt", []))
    except KeyError:
        return []


def _resolved_profile(ctx: SetupContext) -> str:
    return ctx.opts.get("profile") or ctx.state.hardware_profile or "software-only"


def _plan_sdr_strategy(ctx: SetupContext) -> StepPlan:
    """Plan the SDR dependency step honestly for the SELECTED strategy. Each strategy renders the
    steps it (and only it) would actually run — offline never shows apt/network."""
    strat = _resolved(ctx, "sdr_strategy", "recommended")
    pkgs = _profile_apt(ctx)
    summary = ("How to provide GNU Radio / SoapySDR / drivers: recommended (validated apt), "
               "source (pinned builds), offline (pre-staged), or none.")
    if strat == "none":
        return _p("sdr_strategy", "SDR dependency strategy", summary=summary,
                  notes=["strategy=none: SDR dependency installation is skipped (no packages)."])
    if not pkgs:
        return _p("sdr_strategy", "SDR dependency strategy", summary=summary,
                  notes=[f"strategy={strat}: this profile needs no vendor SDR packages."])
    if strat == "offline":
        d, debs = _offline_os_payload(ctx)
        if debs:
            return _p("sdr_strategy", "SDR dependency strategy", summary=summary,
                      requires_sudo=True, os_packages=pkgs, download_sources=[],
                      privileged_ops=[f"sudo dpkg -i {d}/*.deb"],
                      deferred_to=f"aithernet sdr install --mode offline --from {d}",
                      notes=[f"strategy=offline: install {len(debs)} pre-staged package file(s) "
                             f"from {d}. Zero apt/network calls."])
        return _p("sdr_strategy", "SDR dependency strategy", summary=summary,
                  os_packages=pkgs, download_sources=[], privileged_ops=[],
                  notes=["strategy=offline: OFFLINE ASSETS UNAVAILABLE/INCOMPLETE — no pre-staged "
                         f"OS-package payloads for {', '.join(pkgs)} were found"
                         + (f" in {d}" if d else " (no --components-bundle-dir/os-packages given)")
                         + ". This release does not ship a full offline GNU Radio payload. Use "
                         "--sdr-strategy recommended (validated apt repositories) or supply a "
                         "complete offline package bundle. Zero apt/network calls are planned."])
    if strat == "source":
        return _p("sdr_strategy", "SDR dependency strategy", summary=summary,
                  download_sources=["pinned upstream source (no apt)"],
                  deferred_to=f"aithernet sdr install --mode source --profile "
                              f"{_resolved_profile(ctx)}",
                  notes=["strategy=source: long pinned builds; every revision is pinned and its "
                         "source digest recorded before building."])
    # recommended (default): validated apt repositories.
    dl, sz = _estimate(pkgs)
    return _p("sdr_strategy", "SDR dependency strategy", summary=summary,
              requires_sudo=True, os_packages=pkgs,
              download_sources=["Ubuntu archive (apt, validated repositories)"],
              est_download_mb=dl, est_installed_mb=sz,
              privileged_ops=[f"sudo apt-get install -y {' '.join(pkgs)}"],
              deferred_to=f"aithernet sdr install --mode recommended --profile "
                          f"{_resolved_profile(ctx)}",
              notes=["strategy=recommended: validated apt repositories.",
                     "Estimates are approximate (apt resolves exact sizes)."])


def _apply_sdr_strategy(ctx: SetupContext) -> StepResult:
    default = ctx.state.sdr_strategy or "recommended"
    strat = ctx.choose("sdr_strategy", "SDR dependency strategy", list(_SDR_STRATEGIES), default)
    ctx.state.sdr_strategy = strat
    if strat == "none":
        return StepResult("sdr_strategy", DONE,
                          "strategy=none; no SDR dependencies will be installed")
    pkgs = _profile_apt(ctx)
    if not pkgs:
        return StepResult("sdr_strategy", DONE,
                          f"strategy={strat}; profile needs no vendor SDR packages")
    if strat == "offline":
        # Offline NEVER calls apt or the network. Install from pre-staged assets, or stop clearly.
        d, debs = _offline_os_payload(ctx)
        if not debs:
            return StepResult(
                "sdr_strategy", FAILED,
                "offline assets unavailable/incomplete: no pre-staged OS-package payloads for "
                f"{len(pkgs)} package(s) were found"
                + (f" in {d}" if d else " (no --components-bundle-dir/os-packages given)"),
                "supply a complete offline package bundle (os-packages/*.deb) or use "
                "`--sdr-strategy recommended`. No apt/network call was made.")
        return StepResult("sdr_strategy", DEFERRED,
                          f"strategy=offline; {len(debs)} pre-staged package file(s) ready in {d}",
                          f"run `aithernet sdr install --mode offline --from {d}` "
                          "(local dpkg, zero network)")
    # recommended / source -> the privileged installer (validated apt / pinned builds).
    return StepResult("sdr_strategy", DEFERRED,
                      f"strategy={strat}; {len(pkgs)} package(s) planned",
                      f"run `aithernet sdr install --mode {strat} "
                      f"--profile {_resolved_profile(ctx)}` (privileged)")


# 8 — managed components ----------------------------------------------------
def _plan_components(ctx: SetupContext) -> StepPlan:
    bundle = _bundle_dir(ctx)
    notes = ["Provenance is verified before anything is written: the detached Ed25519 signature is "
             "checked against the PINNED component public key, the source-archive digest and "
             "upstream commit must match the trusted spec. No Git clone, no developer checkout, no "
             "source-tree dependency, no network fallback.",
             "Once installed, rf-mcp auto-wires into the GNU Radio MCP launch at startup — no "
             "developer checkout or AITHERNET_GNURADIO_MCP_COMMAND needed."]
    if bundle:
        notes.insert(0, f"Install source: the signed bundle in {bundle} (its public key must equal "
                        "the pinned component key).")
    return _p("managed_components", "Managed components",
              summary="Install the signed, reproducible managed RF-MCP component from its bundle.",
              components=["rf-mcp"],
              deferred_to=(f"aithernet components install rf-mcp --bundle {bundle}" if bundle
                           else "aithernet components install rf-mcp --bundle <release-dir>"),
              notes=notes)


def _apply_components(ctx: SetupContext) -> StepResult:
    if not ctx.state.components:
        ctx.state.components = ["rf-mcp"]
    try:
        from aithernet import components as comp
        st = comp.component_status("rf-mcp")
    except Exception as exc:  # noqa: BLE001
        return StepResult("managed_components", DEFERRED,
                          f"status unavailable ({type(exc).__name__})",
                          "run `aithernet components install rf-mcp`")
    if st.installed and st.ok:
        return StepResult("managed_components", DONE,
                          f"rf-mcp {st.version} installed at {st.prefix}")
    # Install the downloaded, signed component bundle if one was provided (the supported clean-
    # client path: no upstream Git clone). The bundle dir is the release-download directory.
    import os
    bundle_dir = (ctx.opts.get("components_bundle_dir")
                  or os.environ.get("AITHERNET_COMPONENT_BUNDLE_DIR"))
    if bundle_dir:
        from pathlib import Path
        b = Path(bundle_dir)
        if (b / "lock.json").is_file():
            try:
                from aithernet.components import manager
                res = manager.install("rf-mcp", bundle=b, force=True)
                return StepResult("managed_components", DONE,
                                  f"rf-mcp {res.get('version')} installed from signed bundle "
                                  f"(verified against the pinned component key)")
            except Exception as exc:  # noqa: BLE001
                return StepResult("managed_components", FAILED,
                                  f"bundle install failed: {type(exc).__name__}: {exc}",
                                  "verify the downloaded bundle, then retry "
                                  "`aithernet components install rf-mcp --bundle <dir>`")
        return StepResult("managed_components", DEFERRED,
                          f"no component bundle (lock.json) found in {bundle_dir}",
                          "point --components-bundle-dir at the release download directory")
    return StepResult("managed_components", DEFERRED, "rf-mcp not installed",
                      "install the downloaded signed bundle: "
                      "`aithernet components install rf-mcp --bundle <release-download-dir>`")


# 9 — coordinator provider --------------------------------------------------
def _plan_coordinator(ctx: SetupContext) -> StepPlan:
    return _p("coordinator_provider", "Coordinator provider",
              summary="Select + configure the AI coordinator provider (or 'disabled').",
              deferred_to="aithernet agents configure coordinator",
              notes=["Credentials are stored in protected config, never the project tree, never "
                     "printed."])


def _apply_coordinator(ctx: SetupContext) -> StepResult:
    default = ctx.state.coordinator_provider or "disabled"
    val = ctx.text("coordinator_provider", "Coordinator provider", default)
    ctx.state.coordinator_provider = val
    return _configure_provider_step(ctx, "coordinator", "coordinator_provider", val)


def _configure_provider_step(ctx, role, step_id, provider) -> StepResult:
    """Write the selection through the canonical NodeConfig via the provider layer (single source
    of truth). Verifying readiness still needs `aithernet agents test`."""
    if provider == "disabled":
        from aithernet.agents import providers as prov
        prov.remove(role, state_root=ctx.state_root)
        return StepResult(step_id, DONE, f"{role} disabled")
    try:
        from aithernet.agents import providers as prov
        prov.configure(role, provider, state_root=ctx.state_root)
    except ValueError as exc:
        return StepResult(step_id, FAILED, str(exc),
                          f"choose a valid {role} provider (see `aithernet agents providers`)")
    return StepResult(step_id, DONE, f"{role} provider '{provider}' written to node config",
                      f"verify with `aithernet agents test {role}`")


# 10 — coding provider ------------------------------------------------------
def _plan_coding(ctx: SetupContext) -> StepPlan:
    return _p("coding_provider", "Coding provider",
              summary="Select + configure the coding agent provider (or 'disabled').",
              deferred_to="aithernet agents configure coding",
              notes=["Credentials are stored in protected config, never the project tree."])


def _apply_coding(ctx: SetupContext) -> StepResult:
    default = ctx.state.coding_provider or "disabled"
    val = ctx.text("coding_provider", "Coding provider", default)
    ctx.state.coding_provider = val
    return _configure_provider_step(ctx, "coding", "coding_provider", val)


# 11 — service installation/start ------------------------------------------
def _plan_service_install(ctx: SetupContext) -> StepPlan:
    from aithernet.provisioning.state import default_state_root
    mode = ctx.state.service_mode or services.USER
    sp = services.plan(mode, state_root=ctx.state_root or default_state_root())
    return _p("service_install", "Service installation / start",
              summary=f"Install + enable the node service ({mode}).",
              requires_sudo=bool(sp["privileged"]),
              service_changes=list(sp["install_commands"]),
              privileged_ops=[c for c in sp["install_commands"] if c.strip().startswith("sudo")],
              requires_logout=bool(sp.get("requires_logout")),
              notes=[sp["note"]])


def _apply_service_install(ctx: SetupContext) -> StepResult:
    mode = ctx.state.service_mode or services.USER
    if mode == services.MANUAL:
        return StepResult("service_install", DONE, "manual mode; start with `aithernet start`")
    if mode == services.SYSTEM:
        return StepResult("service_install", DEFERRED, "system service install is privileged",
                          "review and run the printed sudo commands (`aithernet setup --dry-run` "
                          "shows them)")
    # systemd-user: write the unit (no sudo); enabling/starting is the operator's explicit action.
    # Pass the RESOLVED canonical root (never None) so the unit pins AITHERNET_CONFIG +
    # AITHERNET_STATE_ROOT to the canonical node.yaml — the beta.7 split was caused by passing None
    # here, which produced a unit with no environment so the service loaded placeholder defaults.
    from aithernet.provisioning.state import default_state_root
    root = ctx.state_root or default_state_root()
    path = services.install_user_unit(state_root=root)
    # Defect 2: persist the resolved provider PATH into the managed runtime env so CLI providers
    # (NVM/npm/distro) run under the service exactly as from the user shell — no manual drop-in.
    path_note = ""
    try:
        from aithernet.config.loader import load_config
        cfg = load_config(str(root / "config" / "node.yaml"))
        services.write_runtime_env(cfg)
        dirs = services.provider_executable_dirs(cfg)
        if dirs:
            path_note = f"; runtime PATH pinned for {len(dirs)} provider dir(s)"
    except Exception:  # noqa: BLE001 — config may not be loadable mid-setup; PATH is best-effort here
        path_note = ""
    return StepResult("service_install", DONE,
                      f"user unit written: {path} (pinned to {root}/config/node.yaml){path_note}",
                      f"enable it with `systemctl --user enable --now {services.UNIT_NAME}`")


# 12 — doctor ---------------------------------------------------------------
def _plan_doctor(ctx: SetupContext) -> StepPlan:
    return _p("doctor", "Doctor (readiness)",
              summary="Run the read-only readiness report and surface any blocking issues.")


def _apply_doctor(ctx: SetupContext) -> StepResult:
    try:
        from aithernet.hardware import setup as hw
        rep = hw.doctor()
        fails = [c.name for c in rep.checks if c.status == "fail"]
    except Exception as exc:  # noqa: BLE001
        return StepResult("doctor", FAILED, f"doctor error ({type(exc).__name__})",
                          "run `aithernet doctor` directly to see details")
    if fails:
        return StepResult("doctor", DONE, f"readiness checked; blocking: {', '.join(fails)}",
                          "run `aithernet doctor --fix-plan` for remediation")
    return StepResult("doctor", DONE, "readiness checked; no blocking failures")


# 13 — hardware discovery ---------------------------------------------------
def _plan_discovery(ctx: SetupContext) -> StepPlan:
    return _p("hardware_discovery", "Hardware discovery",
              summary="Enumerate attached SDRs (read-only). Never claims a device it cannot see.")


def _apply_discovery(ctx: SetupContext) -> StepResult:
    try:
        from aithernet.hardware import setup as hw
        sdrs = hw.discover_sdrs()
    except Exception as exc:  # noqa: BLE001
        return StepResult("hardware_discovery", FAILED, f"discovery error ({type(exc).__name__})")
    if sdrs:
        labels = ", ".join(d.get("label", d.get("driver", "?")) for d in sdrs)
        return StepResult("hardware_discovery", DONE, f"{len(sdrs)} SDR(s): {labels}")
    return StepResult("hardware_discovery", DONE,
                      "no SDRs detected (expected for software-only/simulation)")


# 14 — first receive-only qualification readiness --------------------------
def _plan_qualification(ctx: SetupContext) -> StepPlan:
    return _p("qualification_readiness", "First receive-only qualification readiness",
              summary="Assess whether a first bounded RECEIVE-ONLY mission could run. No transmit, "
                      "ever, from setup.")


def _apply_qualification(ctx: SetupContext) -> StepResult:
    prof_name = ctx.state.hardware_profile or "software-only"
    try:
        prof = profiles.resolve(prof_name)
    except KeyError:
        return StepResult("qualification_readiness", FAILED, f"unknown profile '{prof_name}'")
    reasons: list[str] = []
    if prof.needs_hardware:
        try:
            from aithernet.hardware import setup as hw
            if not hw.discover_sdrs():
                reasons.append("no SDR detected for a hardware profile")
        except Exception:  # noqa: BLE001
            reasons.append("SDR discovery unavailable")
    if ctx.state.status_of("managed_components") not in (DONE,):
        reasons.append("managed RF-MCP not yet installed")
    if reasons:
        return StepResult("qualification_readiness", DEFERRED,
                          "not yet ready: " + "; ".join(reasons),
                          "address the items above, then run a bounded receive-only mission "
                          "(`aithernet run \"<prompt>\"`)")
    # beta.9 Defect 5: do NOT claim mission readiness unless the coordinator can actually run.
    # RF-MCP readiness alone is not mission readiness — a mission needs an executable, live-verified
    # coordinator (or an explicitly disabled one, which runs without AI planning).
    coord_note = _coordinator_readiness_note(ctx)
    if coord_note is None:  # coordinator disabled — software-only, no AI planning
        return StepResult("qualification_readiness", DONE,
                          "RF-MCP ready; coordinator disabled (missions run without AI planning)")
    ready, detail, remediation = coord_note
    if not ready:
        return StepResult("qualification_readiness", CONFIGURED,
                          f"RF-MCP ready; {detail}", remediation)
    return StepResult("qualification_readiness", DONE,
                      f"ready for a first bounded receive-only mission ({detail})")


def _coordinator_readiness_note(ctx: SetupContext):
    """Return ``None`` if the coordinator is disabled, else ``(ready, detail, remediation)``.

    ``ready`` is true only when the coordinator is live-verified (selected == executed proven) — a
    mere executable/credential present is not mission-ready (beta.9 Defect 5)."""
    try:
        from aithernet.agents import providers as prov
        st = prov.status(prov.COORDINATOR, state_root=ctx.state_root)
    except Exception as exc:  # noqa: BLE001
        return (False, f"coordinator readiness unavailable ({type(exc).__name__})",
                "run `aithernet agents provider-status coordinator`")
    provider = st.get("provider") or "?"
    if provider == prov.DISABLED:
        return None
    r = st.get("readiness", {})
    if r.get("live_verified") or r.get("mission_ready"):
        return (True, f"coordinator '{provider}' live-verified", "")
    return (False,
            f"coordinator '{provider}' configured but not yet verified for full planning",
            "run `aithernet agents test coordinator --live` (or `aithernet agents connect "
            "coordinator`), then retry")


STEPS: tuple[Step, ...] = (
    Step("deployment_mode", "Deployment mode", "config", _plan_deployment, _apply_deployment),
    Step("node_identity", "Node identity", "identity", _plan_identity, _apply_identity),
    Step("directories", "Directories and permissions", "config",
         _plan_directories, _apply_directories),
    Step("hosted_enrollment", "Hosted enrollment", "hosted", _plan_enrollment, _apply_enrollment),
    Step("service_mode", "Service mode", "service", _plan_service_mode, _apply_service_mode),
    Step("hardware_profile", "Hardware profile", "hardware",
         _plan_hardware_profile, _apply_hardware_profile),
    Step("sdr_strategy", "SDR dependency strategy", "hardware",
         _plan_sdr_strategy, _apply_sdr_strategy),
    Step("managed_components", "Managed components", "components",
         _plan_components, _apply_components),
    Step("coordinator_provider", "Coordinator provider", "agents",
         _plan_coordinator, _apply_coordinator),
    Step("coding_provider", "Coding provider", "agents", _plan_coding, _apply_coding),
    Step("service_install", "Service installation / start", "service",
         _plan_service_install, _apply_service_install),
    Step("doctor", "Doctor (readiness)", "doctor", _plan_doctor, _apply_doctor),
    Step("hardware_discovery", "Hardware discovery", "hardware",
         _plan_discovery, _apply_discovery),
    Step("qualification_readiness", "First receive-only qualification readiness", "mission",
         _plan_qualification, _apply_qualification),
)

STEP_IDS: tuple[str, ...] = tuple(s.id for s in STEPS)


def get_step(step_id: str) -> Step:
    for s in STEPS:
        if s.id == step_id:
            return s
    raise KeyError(step_id)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def build_plan(state: SetupState, *, state_root: Path | None = None,
               opts: dict | None = None) -> list[StepPlan]:
    """Full non-mutating plan for every step (``--dry-run`` / ``--status`` use this)."""
    ctx = SetupContext(state, state_root=state_root, opts=opts, interactive=False)
    return [s.plan(ctx) for s in STEPS]


def plan_totals(plans: list[StepPlan]) -> dict:
    """Aggregate the dry-run plan: package set, totals, privileged-op count, restart flags."""
    pkgs: list[str] = []
    for p in plans:
        for pk in p.os_packages:
            if pk not in pkgs:
                pkgs.append(pk)
    return {
        "os_packages": pkgs,
        "components": sorted({c for p in plans for c in p.components}),
        "est_download_mb": round(sum(p.est_download_mb for p in plans), 1),
        "est_installed_mb": round(sum(p.est_installed_mb for p in plans), 1),
        "privileged_steps": [p.step_id for p in plans if p.requires_sudo],
        "privileged_ops": [op for p in plans for op in p.privileged_ops],
        "requires_restart": any(p.requires_restart for p in plans),
        "requires_logout": any(p.requires_logout for p in plans),
        "requires_reboot": any(p.requires_reboot for p in plans),
        "deferred": {p.step_id: p.deferred_to for p in plans if p.deferred_to},
    }


def run(state: SetupState, *, state_root: Path | None = None, opts: dict | None = None,
        interactive: bool = False, ask: Callable | None = None,
        only: list[str] | None = None, repair: bool = False,
        on_result: Callable[[StepResult], None] | None = None) -> list[StepResult]:
    """Execute steps in order, persisting state after each.

    * ``only`` restricts execution to specific step ids.
    * ``repair`` re-runs steps whose recorded status is not a success (``failed``/``pending``);
      already-``done`` steps are skipped unless explicitly named in ``only``.
    * Resume is implicit: any step already ``done``/``skipped``/``deferred`` is not re-run unless
      ``repair``/``only`` selects it.
    """
    ctx = SetupContext(state, state_root=state_root, opts=opts, interactive=interactive, ask=ask)
    if not state.created_at:
        state.created_at = _now()
    results: list[StepResult] = []
    for step in STEPS:
        if only is not None and step.id not in only:
            continue
        if only is None:
            if repair:
                if state.status_of(step.id) in (DONE, SKIPPED):
                    continue
            elif state.is_complete(step.id):
                continue
        try:
            res = step.apply(ctx)
        except Exception as exc:  # noqa: BLE001 — a failed step must not abort the whole run
            res = StepResult(step.id, FAILED, f"{type(exc).__name__}: {exc}",
                             "fix the error above and re-run `aithernet setup --repair`")
        state.record(step.id, res.status, res.detail, now=_now())
        save_state(state, state_root)
        results.append(res)
        if on_result is not None:
            on_result(res)
    return results


def load_or_new(state_root: Path | None = None) -> SetupState:
    return load_state(state_root) or SetupState(created_at=_now())


def reconcile(state: SetupState, *, state_root: Path | None = None) -> SetupState:
    """The ONE canonical setup-state reconciliation: derive the real status of externally-completed
    steps from the LIVE system and record it, so ``setup --status`` agrees with ``doctor`` and
    ``--resume``/``--repair`` never repeat already-finished work. Idempotent; persists state.

    External commands (``aithernet sdr install`` / ``components install`` / ``agents configure``)
    complete setup responsibilities; rather than each writing back separately, reconcile reads the
    ground truth (is GNU Radio importable? is rf-mcp installed+verified? is a provider configured?
    is the service unit installed?) — robust even if a command crashed mid-way.
    """
    from aithernet.provisioning import state as S
    now = _now()

    # SDR dependency strategy.
    strat = state.sdr_strategy or _resolved(SetupContext(state, state_root=state_root),
                                            "sdr_strategy", "")
    if strat == "none":
        state.record("sdr_strategy", S.SKIPPED, "strategy=none; no SDR deps", now=now)
    else:
        import importlib.util
        try:
            gnuradio_ok = importlib.util.find_spec("gnuradio") is not None
        except Exception:  # noqa: BLE001
            gnuradio_ok = False
        if gnuradio_ok:
            state.record("sdr_strategy", S.VERIFIED, "GNU Radio importable", now=now)

    # Managed RF-MCP component.
    try:
        from aithernet import components as comp
        cs = comp.component_status("rf-mcp")
        if cs.installed and cs.ok:
            state.record("managed_components", S.VERIFIED, f"rf-mcp {cs.version}", now=now)
        elif cs.installed:
            state.record("managed_components", S.FAILED, "rf-mcp not verified", now=now)
    except Exception:  # noqa: BLE001
        pass

    # Coordinator / coding provider selections (read the canonical NodeConfig the runtime loads).
    from aithernet.agents import providers as prov
    for role, step_id in (("coordinator", "coordinator_provider"), ("coding", "coding_provider")):
        try:
            st = prov.status(role, state_root=state_root)
        except Exception:  # noqa: BLE001
            continue
        selected = st.get("provider")
        if selected and selected != prov.DISABLED:
            verb = S.VERIFIED if st.get("readiness", {}).get("live_verified") else S.CONFIGURED
            state.record(step_id, verb, f"provider '{selected}'", now=now)
            if step_id == "coordinator_provider":
                state.coordinator_provider = selected
            else:
                state.coding_provider = selected
        elif selected == prov.DISABLED:
            state.record(step_id, S.DONE, "disabled", now=now)

    # Service installation.
    try:
        from aithernet.provisioning import services
        scope = services.installed_scope()
        if scope is not None:
            state.record("service_install", S.INSTALLED, f"{scope} unit installed", now=now)
    except Exception:  # noqa: BLE001
        pass

    save_state(state, state_root)
    return state
