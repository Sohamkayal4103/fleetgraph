"""Top-level readiness aggregator: ``aithernet doctor`` (Stage 14G, PHASE 7).

Aggregates the WHOLE node's readiness truthfully into explicit states. It is **read-only**: it never
installs, mutates config, claims a device it cannot see, marks an executable authenticated merely
because it exists, or surfaces secrets / OAuth tokens / full private paths / raw environment data.

``--fix-plan`` derives a deterministic, NON-mutating remediation plan from the non-ready checks.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Explicit states (the only values a check may report).
READY = "ready"
OPTIONAL = "optional"
MISSING = "missing"
MISCONFIGURED = "misconfigured"
UNSUPPORTED = "unsupported"
PERMISSION_DENIED = "permission-denied"
DEVICE_ABSENT = "device-absent"
UNVERIFIED = "unverified"
DEGRADED = "degraded"

#: States that make the overall report NOT ok (block readiness).
_BLOCKING = {MISSING, MISCONFIGURED, PERMISSION_DENIED, DEGRADED}

#: Named readiness DOMAINS -> the check categories that compose them. Distinct domains let a
#: software-only node report core-ready while physical RF is unavailable, without a top-level
#: "READY" banner implying every optional capability passed.
DOMAIN_CATEGORIES: dict[str, tuple[str, ...]] = {
    "core_node": ("core", "identity", "resources", "service", "setup"),
    "providers": ("agents",),
    "mcp": ("components",),
    "coding_sandbox": ("coding",),
    "software_mission": ("mission",),
    "physical_rf": ("hardware", "rf", "permissions"),
    "hosted_enrollment": ("hosted",),
}


@dataclass
class Check:
    name: str
    category: str
    status: str
    detail: str = ""
    remediation: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "category": self.category, "status": self.status,
                "detail": self.detail, "remediation": self.remediation}


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name, category, status, detail="", remediation="") -> None:
        self.checks.append(Check(name, category, status, detail, remediation))

    @property
    def ok(self) -> bool:
        return not any(c.status in _BLOCKING for c in self.checks)

    def summary_counts(self) -> dict:
        out: dict[str, int] = {}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def domains(self) -> dict:
        """Per-domain readiness so a top-level result never implies optional capabilities passed.

        A software-only node can be core-ready while physical RF is 'unavailable'. Each domain is
        derived from its checks: any blocking check -> unavailable; an absent device ->
        unavailable; unverified -> unverified; otherwise available; no checks -> not-applicable."""
        out: dict[str, dict] = {}
        for domain, cats in DOMAIN_CATEGORIES.items():
            checks = [c for c in self.checks if c.category in cats]
            if not checks:
                out[domain] = {"status": "not-applicable", "checks": []}
                continue
            if any(c.status in _BLOCKING for c in checks):
                status = "unavailable"
            elif any(c.status == DEVICE_ABSENT for c in checks):
                status = "unavailable"
            elif any(c.status == UNVERIFIED for c in checks):
                status = "unverified"
            else:
                status = "available"
            out[domain] = {"status": status, "checks": [c.name for c in checks]}
        return out

    def to_dict(self) -> dict:
        return {"ok": self.ok, "version": _version(),
                "summary": self.summary_counts(),
                "domains": self.domains(),
                "checks": [c.to_dict() for c in self.checks]}

    def fix_plan(self) -> list[dict]:
        """Deterministic, non-mutating remediation steps for every non-ready, actionable check."""
        plan = []
        for c in self.checks:
            if c.status in (READY, OPTIONAL, DEVICE_ABSENT, UNVERIFIED) and not c.remediation:
                continue
            if c.remediation:
                plan.append({"check": c.name, "status": c.status, "action": c.remediation})
        return plan


def _version() -> str:
    from aithernet import __version__
    return __version__


# ---------------------------------------------------------------------------
# Individual check groups (each appends to the report; all read-only)
# ---------------------------------------------------------------------------
def _state_root() -> Path:
    return Path(os.environ.get("AITHERNET_STATE_ROOT",
                               str(Path.home() / ".local/share/aithernet")))


def _check_core(r: DoctorReport) -> None:
    r.add("core_version", "core", READY, f"Aithernet {_version()}")
    # install source: packaged (.deb VERSION) / installed dist / editable / source tree
    source = "unknown"
    if Path("/opt/aithernet/VERSION").is_file():
        source = "deb (/opt/aithernet)"
    else:
        try:
            from importlib.metadata import distribution
            dist = distribution("aithernet")
            direct = dist.read_text("direct_url.json") or ""
            source = "editable" if '"editable": true' in direct else "installed distribution"
        except Exception:  # noqa: BLE001
            source = "source tree"
    r.add("install_source", "core", READY, source)
    # beta.5: after a local .deb upgrade a stale shell hash can keep resolving an OLD `aithernet`
    # (e.g. a ~/.local or venv copy) instead of the packaged one. If the deb is installed but
    # `aithernet` on PATH is not /usr/bin/aithernet, note it (non-blocking) so the operator runs
    # `hash -r`. This never blocks readiness — it only explains a possible version mismatch.
    if source == "deb (/opt/aithernet)":
        resolved = shutil.which("aithernet")
        if resolved and resolved != "/usr/bin/aithernet":
            r.add("cli_path", "core", OPTIONAL,
                  f"`aithernet` on PATH resolves to {resolved}, not the packaged "
                  "/usr/bin/aithernet",
                  "run `hash -r` (or open a new shell), then `which aithernet` / "
                  "`aithernet --version`")
        else:
            r.add("cli_path", "core", READY,
                  f"aithernet on PATH: {resolved or '/usr/bin/aithernet'}")


def _check_identity_enrollment(r: DoctorReport) -> None:
    root = _state_root()
    ident = root / "identity"
    if ident.is_dir() and any(ident.iterdir()):
        r.add("node_identity", "identity", READY, "node Ed25519 identity present")
    else:
        r.add("node_identity", "identity", MISSING, "no node identity",
              "run `aithernet setup` (node identity step) or `aithernet hosted setup`")
    try:
        from aithernet.hosted.localconfig import load_enrollment
        enr = load_enrollment(root / "config")
        if enr and enr.enrollment_state == "enrolled":
            r.add("enrollment", "hosted", READY, "enrolled with the hosted control plane")
        else:
            r.add("enrollment", "hosted", OPTIONAL, "not enrolled (standalone)",
                  "enroll with `aithernet enroll --base-url <url> --code <code>` if using hosted")
    except Exception as exc:  # noqa: BLE001
        r.add("enrollment", "hosted", UNVERIFIED, f"enrollment state unavailable "
              f"({type(exc).__name__})")


def _check_setup_state(r: DoctorReport) -> None:
    from aithernet.provisioning import state as sstate
    st = sstate.load_state(_state_root())
    if st is None:
        r.add("setup", "setup", MISSING, "guided setup has not been run",
              "run `aithernet setup` (or `aithernet setup --dry-run` to preview)")
        return
    r.add("deployment_mode", "setup", READY if st.deployment_mode else UNVERIFIED,
          st.deployment_mode or "unset")
    r.add("hardware_profile", "setup", READY if st.hardware_profile else UNVERIFIED,
          st.hardware_profile or "unset")
    r.add("service_mode", "setup", READY if st.service_mode else UNVERIFIED,
          st.service_mode or "unset")
    # The ACTUAL execution identity: which Linux user the node runs as, and the chosen model.
    try:
        from aithernet.agents import providers as _prov
        ident = _prov.runtime_identity(
            _state_root() if os.environ.get("AITHERNET_STATE_ROOT") else None)
        r.add("execution_identity", "setup", READY,
              f"Linux user '{ident['linux_user']}', identity model {ident['identity_model']}")
    except Exception:  # noqa: BLE001
        pass


def _check_service(r: DoctorReport) -> None:
    from aithernet.provisioning import services
    unit_user = services.user_unit_dir() / services.UNIT_NAME
    unit_sys = Path("/etc/systemd/system") / services.UNIT_NAME
    if unit_user.is_file() or unit_sys.is_file():
        where = "user" if unit_user.is_file() else "system"
        active = _systemctl_active(where)
        if active is True:
            r.add("service_health", "service", READY, f"{where} service active")
        elif active is False:
            r.add("service_health", "service", DEGRADED,
                  f"{where} service installed but not active",
                  f"start it: `systemctl {'--user ' if where == 'user' else ''}start "
                  f"{services.UNIT_NAME}`")
        else:
            r.add("service_health", "service", UNVERIFIED, f"{where} unit present; state unknown")
    else:
        r.add("service_health", "service", OPTIONAL, "no service unit (manual start)",
              "install one via `aithernet setup` (service mode) for auto-start")


def _systemctl_active(scope: str) -> bool | None:
    if not shutil.which("systemctl"):
        return None
    from aithernet.provisioning import services
    args = ["systemctl", "is-active", "--quiet"]
    if scope == "user":
        args.insert(1, "--user")
    args.append(services.UNIT_NAME)
    try:
        import subprocess
        return subprocess.run(args, capture_output=True, timeout=5).returncode == 0
    except Exception:  # noqa: BLE001
        return None


def _probe(cmd: list[str], timeout: float = 12.0) -> tuple[int, str]:
    import subprocess
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _profile_needs(profile: str | None) -> set[str]:
    """Which RF runtimes the configured profile requires (for required-vs-optional grading)."""
    if not profile:
        return set()
    try:
        from aithernet.provisioning import profiles
        return set(profiles.resolve(profile).runtime_libs)
    except Exception:  # noqa: BLE001
        return set()


def _check_rf_stack(r: DoctorReport, needs: set[str]) -> None:
    def grade(required: bool) -> str:
        return MISSING if required else OPTIONAL

    rc, out = _probe(["gnuradio-config-info", "--version"])
    if rc == 0:
        r.add("gnuradio", "rf", READY, f"GNU Radio {out.strip()}")
    else:
        # GNU Radio is the core RF runtime — required for every profile (incl. software-only).
        r.add("gnuradio", "rf", MISSING, "GNU Radio not found",
              "`sudo aithernet hardware install --profile <profile>`")

    if shutil.which("SoapySDRUtil"):
        rc, out = _probe(["SoapySDRUtil", "--info"])
        facs = next((ln.split("...", 1)[1].strip() for ln in out.splitlines()
                     if "Available factories" in ln), "")
        r.add("soapysdr", "rf", READY, f"factories: {facs or 'none'}")
    else:
        r.add("soapysdr", "rf", grade("soapysdr" in needs), "SoapySDRUtil not found",
              "`sudo aithernet hardware install --profile generic-soapy`")

    if shutil.which("iio_info") or shutil.which("iio_attr"):
        r.add("libiio", "rf", READY, "libiio tools present")
    else:
        r.add("libiio", "rf", grade("libiio" in needs), "libiio not found (Pluto)",
              "`sudo aithernet hardware install --profile plutosdr`")

    rc, out = _probe(["uhd_config_info", "--version"])
    if rc == 0 or "UHD" in out:
        r.add("uhd", "rf", READY, "UHD present")
    else:
        r.add("uhd", "rf", grade("uhd" in needs), "UHD not found (USRP only)",
              "`sudo aithernet hardware install --profile usrp`")

    # GNU Radio IIO / Soapy python bindings (best-effort import probe in the RF python env)
    for mod, label, key in (("gnuradio.iio", "gr-iio", "gr-iio"),
                            ("gnuradio.soapy", "gr-soapy", "gr-soapy")):
        rc, _ = _probe(["python3", "-c", f"import {mod}"], timeout=15)
        if rc == 0:
            r.add(label, "rf", READY, f"{label} import OK")
        elif key in needs:
            r.add(label, "rf", MISSING, f"{label} not importable",
                  "`sudo aithernet hardware install --profile <profile>`")
        else:
            r.add(label, "rf", OPTIONAL, f"{label} not importable")


def _check_permissions_udev(r: DoctorReport, needs: set[str]) -> None:
    # USB device groups + udev rules matter only when the profile uses PHYSICAL SDR hardware.
    needs_device = bool(needs & {"libiio", "uhd", "soapysdr"})
    rc, out = _probe(["id"])
    for g in ("plugdev", "dialout"):
        if f"({g})" in out:
            r.add(f"group:{g}", "permissions", READY, f"user in '{g}'")
        elif needs_device:
            r.add(f"group:{g}", "permissions", PERMISSION_DENIED,
                  f"user not in '{g}' (USB SDR device access)",
                  f"`sudo usermod -aG {g} $USER` then log out/in")
        else:
            r.add(f"group:{g}", "permissions", OPTIONAL, f"user not in '{g}'")
    rules = list(Path("/etc/udev/rules.d").glob("*plutosdr*")) if Path(
        "/etc/udev/rules.d").is_dir() else []
    rules += list(Path("/etc/udev/rules.d").glob("*uhd*")) if Path(
        "/etc/udev/rules.d").is_dir() else []
    if rules:
        r.add("udev_rules", "permissions", READY, f"{len(rules)} SDR udev rule file(s)")
    elif "libiio" in needs or "uhd" in needs:
        r.add("udev_rules", "permissions", MISSING, "no SDR udev rules installed",
              "install the hardware profile (it adds the vendor udev rules)")
    else:
        r.add("udev_rules", "permissions", OPTIONAL, "no SDR udev rules (not needed)")


def _check_devices(r: DoctorReport, needs: set[str]) -> None:
    try:
        from aithernet.hardware import setup as hw
        sdrs = hw.discover_sdrs()
    except Exception as exc:  # noqa: BLE001
        r.add("sdr_devices", "hardware", UNVERIFIED, f"discovery error ({type(exc).__name__})")
        return
    if sdrs:
        labels = ", ".join(d.get("label", d.get("driver", "?")) for d in sdrs)
        serials = [d.get("serial") for d in sdrs if d.get("serial")]
        r.add("sdr_devices", "hardware", READY, f"{len(sdrs)} detected: {labels}")
        r.add("device_identity", "hardware",
              READY if serials else UNVERIFIED,
              f"stable serials: {', '.join(serials)}" if serials else
              "no stable serial reported by the device")
    elif needs & {"libiio", "uhd", "soapysdr"}:
        r.add("sdr_devices", "hardware", DEVICE_ABSENT,
              "no SDR detected for a hardware profile",
              "connect the SDR, or use --profile simulation for no-hardware work")
    else:
        r.add("sdr_devices", "hardware", OPTIONAL, "no SDRs (software-only/simulation)")


def _check_components(r: DoctorReport) -> None:
    try:
        from aithernet.components import manager
        v = manager.verify("rf-mcp")
    except Exception as exc:  # noqa: BLE001
        r.add("rf_mcp", "components", UNVERIFIED, f"status unavailable ({type(exc).__name__})")
        return
    if not v.get("installed"):
        r.add("rf_mcp", "components", MISSING, "RF-MCP component not installed",
              "`aithernet components plan rf-mcp` then `aithernet components install rf-mcp`")
        return
    if v.get("ok"):
        sig = v["checks"].get("signature_valid")
        prov = "signature verified" if sig else ("signature not re-checked"
                                                 if sig is None else "SIGNATURE INVALID")
        r.add("rf_mcp", "components", READY, f"rf-mcp {v.get('version')} ({prov})")
    else:
        r.add("rf_mcp", "components", MISCONFIGURED,
              f"rf-mcp installed with issues: {'; '.join(v.get('issues', []))}",
              "`aithernet components repair rf-mcp` (or reinstall)")


def _check_mcp_wiring(r: DoctorReport) -> None:
    """Whether the GNU Radio MCP server is actually launchable, without starting it.

    Read-only: loads the node config (which auto-wires the managed rf-mcp component when no
    explicit command is set) and resolves command/args/cwd only — it never spawns the server.
    This catches the case where rf-mcp is installed and verified but not reachable as a launch
    (e.g. missing ``uv`` or an unresolved command), which a clean customer install must avoid.
    """
    try:
        from aithernet.components import installed_mcp_launch
        from aithernet.config.loader import load_config
        from aithernet.mcp.clients.stdio import check_stdio_ready

        cfg = load_config()
        mcp = cfg.gnuradio_mcp
        missing = check_stdio_ready(mcp)
        launch = installed_mcp_launch("rf-mcp")
    except Exception as exc:  # noqa: BLE001
        r.add("mcp_wiring", "components", UNVERIFIED, f"unavailable ({type(exc).__name__})")
        return
    managed = bool(launch and mcp.command == launch.command and mcp.cwd == launch.cwd)
    source = "managed component" if managed else ("explicit config" if mcp.command else "none")
    if not mcp.command:
        r.add("mcp_wiring", "components", MISSING,
              "GNU Radio MCP launch not configured (no managed rf-mcp install, no command set)",
              "`aithernet components install rf-mcp` (auto-wires), or set "
              "AITHERNET_GNURADIO_MCP_COMMAND")
    elif missing:
        r.add("mcp_wiring", "components", MISCONFIGURED,
              f"GNU Radio MCP not launchable from {source} (unresolved: {', '.join(missing)})",
              "ensure `uv` is installed and the component prefix exists, or reinstall rf-mcp")
    else:
        r.add("mcp_wiring", "components", READY,
              f"GNU Radio MCP launch resolved from {source} ({mcp.command})")


def _check_providers(r: DoctorReport) -> None:
    from aithernet.agents import providers as prov
    for role in prov.ROLES:
        st = prov.status(role, state_root=_state_root() if os.environ.get(
            "AITHERNET_STATE_ROOT") else None)
        if not st["configured"]:
            r.add(f"{role}_provider", "agents", OPTIONAL, f"{role} disabled/unconfigured",
                  f"`aithernet agents configure {role} --provider <p>`")
            continue
        auth = st.get("auth_readiness", "unverified")
        # beta.9 Defect 5: providers report "live-verified" after a live pass; doctor must agree
        # (it previously only matched "ready", so a live-verified coordinator showed UNVERIFIED).
        if auth in ("ready", "live-verified"):
            r.add(f"{role}_provider", "agents", READY,
                  f"{st['display']} ready (live-verified)")
        elif auth in ("executable-missing", "no-key-reference"):
            r.add(f"{role}_provider", "agents", MISCONFIGURED,
                  f"{st['display']}: {auth}",
                  f"`aithernet agents configure {role} ...` then `aithernet agents test {role}`")
        else:
            r.add(f"{role}_provider", "agents", UNVERIFIED,
                  f"{st['display']}: {auth}",
                  f"verify with `aithernet agents test {role}`")


def _check_catgpt_gateway(r: DoctorReport) -> None:
    """beta.7 (FIX 10): if the managed CatGPT Gateway coordinator is set up, report its lifecycle
    (ready / login-required / stopped) and whether the coordinator model is persisted. Stays
    completely quiet (adds no check) when managed CatGPT is not configured — no noise for the many
    nodes that use a different coordinator."""
    try:
        from aithernet.catgpt.manager import CatGptGatewayManager
        mgr = CatGptGatewayManager()
        if not mgr.is_configured():
            return
        data = mgr.status(probe=True, timeout=3.0)
    except Exception:  # noqa: BLE001 — the managed-gateway probe must never crash the report
        return
    gw = data.get("gateway_status", "error")
    if gw == "ready":
        model = None
        try:
            from aithernet.agents import providers as _prov
            model = getattr(_prov._role_provider_config(_prov.COORDINATOR, None), "model", None)
        except Exception:  # noqa: BLE001
            pass
        if model:
            r.add("catgpt_gateway", "agents", READY,
                  f"managed CatGPT Gateway ready; coordinator model '{model}'.")
        else:
            r.add("catgpt_gateway", "agents", DEGRADED,
                  f"gateway ready (discovered '{data.get('model')}') but the coordinator model is "
                  "not persisted.",
                  "Persist it: `aithernet catgpt status` (auto-persists the discovered model).")
    elif gw == "login_required":
        r.add("catgpt_gateway", "agents", UNVERIFIED,
              "managed CatGPT Gateway is running but no web login yet.",
              f"Open noVNC and sign in: {data.get('novnc_open_url')} "
              "(password: `aithernet catgpt vnc-password`).")
    elif gw == "stopped":
        r.add("catgpt_gateway", "agents", UNVERIFIED,
              "managed CatGPT Gateway is configured but not running.",
              "Start it: `aithernet catgpt start`.")
    else:
        r.add("catgpt_gateway", "agents", UNVERIFIED,
              f"managed CatGPT Gateway status: {gw}.")


def _check_research_standalone_drive(r: DoctorReport, rec: dict) -> None:
    """[advanced standalone mode] honest client-Drive status (beta.8 path)."""
    try:
        from aithernet.data.research_sync import drive_state
        ds = drive_state()
    except Exception:  # noqa: BLE001
        return
    if ds["status"] == "unavailable":
        r.add("research_drive_sync", "data", UNVERIFIED,
              f"[standalone] client Drive unavailable: {ds.get('reason')}.", ds.get("repair"))
    elif ds["status"] == "not_authorized":
        r.add("research_drive_sync", "data", UNVERIFIED,
              "[standalone] client Drive not authorized.", "aithernet data verify --authorize")
    elif rec["pending_upload"] > 0:
        r.add("research_drive_sync", "data", UNVERIFIED,
              f"[standalone] {rec['pending_upload']} package(s) pending Drive upload.",
              "aithernet data sync")
    else:
        r.add("research_drive_sync", "data", READY,
              "[standalone] client Drive authorized; no packages pending.")


def _check_research_collection(r: DoctorReport) -> None:
    """beta.9: when owner_full research recording is enabled, verify the recorder is producing
    records and the OWNER-ARCHIVE upload path is honest (enroll + consent + capability + hosted
    ingestion). Stays completely quiet when research collection is off — no noise for the many
    nodes that do not record. The default path needs NO client Google Drive."""
    try:
        import yaml

        from aithernet.agents.providers import node_config_path
        path = node_config_path(None)
        coll = {}
        if path.is_file():
            coll = ((yaml.safe_load(path.read_text()) or {}).get("data_platform") or {}
                    ).get("collection") or {}
        mode = str(coll.get("data_collection_mode", "off"))
        if mode != "owner_full":
            return  # quiet unless the owner explicitly enabled full recording
        consent = bool(coll.get("research_consent", False))
        upload_mode = str(coll.get("research_upload_mode", "hosted_owner_archive"))
        from aithernet.data.research_spool import ResearchSpool
        spool = ResearchSpool()
        st = spool.status()
        rec = spool.recorder_status()
    except Exception:  # noqa: BLE001 — the research probe must never crash the report
        return

    if not st.get("healthy"):
        r.add("research_recording", "data", DEGRADED,
              "owner_full recording is enabled but the local research spool is not initialized.",
              "Initialize it: `aithernet data setup`.")
        return
    if rec["mission_records"] == 0:
        r.add("research_recording", "data", UNVERIFIED,
              "recording enabled but no records yet (recorder writes one per mission outcome).",
              "Run a mission. If a mission completed with no record, restart the node so a running "
              "worker picks up owner_full (`aithernet service restart`).")
    else:
        r.add("research_recording", "data", READY,
              f"{rec['mission_records']} research record(s); last at {rec['last_recorded_at']}.")
    if st.get("quarantined"):
        r.add("research_quarantine", "data", UNVERIFIED,
              f"{st['quarantined']} record(s) quarantined (a residual secret was detected and kept "
              "out of raw/uploads).",
              "Inspect locally under the spool `quarantine/`; nothing quarantined is uploaded.")

    if upload_mode == "standalone_drive":
        _check_research_standalone_drive(r, rec)
        return

    # -- default hosted owner-archive upload --------------------------------------------------
    try:
        from aithernet.data.research_upload import (
            hosted_ingestion_status,
            is_enrolled,
            load_capability,
        )
        enrolled = is_enrolled()
    except Exception:  # noqa: BLE001
        return
    if not consent:
        r.add("research_owner_archive", "data", UNVERIFIED,
              "recording enabled but consent to owner-archive upload is not granted.",
              "Grant it: `aithernet data research-consent --grant` (records stay local).")
    elif not enrolled:
        r.add("research_owner_archive", "data", UNVERIFIED,
              "consented, but owner-archive upload is pending enrollment.",
              "Enroll this node: `aithernet enroll --base-url <url> --code <CODE>`.")
    else:
        hosted = hosted_ingestion_status()
        cap = "present" if load_capability() else "pending"
        if hosted != "healthy":
            r.add("research_owner_archive", "data", UNVERIFIED,
                  f"hosted ingestion is {hosted}; packages queue locally and retry automatically.",
                  "Aithernet retries automatically; force with `aithernet data upload-now`.")
        else:
            r.add("research_owner_archive", "data", READY,
                  f"owner-archive upload ready (hosted ingestion healthy, capability {cap}). "
                  "Google Drive is managed server-side; no client Drive setup required.")


def _check_coding_sandbox(r: DoctorReport) -> None:
    """beta.5: whether the local kernel permits the bubblewrap (bwrap) sandboxing an isolated
    coding-agent workspace relies on — uid map + loopback/net namespace. The live probe is
    authoritative and non-destructive; the AppArmor sysctl only contextualises a failure. Doctor
    never changes a sysctl and never needs sudo.
    """
    try:
        from aithernet.coding_agent import sandbox_preflight as sp
        probe = sp.preflight_sandbox()
    except Exception as exc:  # noqa: BLE001 — a probe error must never crash the read-only report
        r.add("coding_sandbox", "coding", UNVERIFIED,
              f"sandbox preflight unavailable ({type(exc).__name__})")
        return
    mapping = {"ready": READY, "blocked": DEGRADED, "absent": UNVERIFIED, "unverified": UNVERIFIED}
    status = mapping.get(probe.severity, UNVERIFIED)
    detail = probe.detail
    if status == DEGRADED:
        detail = detail + " — missions requiring the coding agent may fail until this is resolved."
    r.add("coding_sandbox", "coding", status, detail, probe.remediation)
    # beta.7 (FIX 5): if the AppArmor userns hardening has been RELAXED (value 0, e.g. via
    # `coding-sandbox repair --temporary`), report it with the restore command. Non-blocking (the
    # sandbox works) — a security reminder to re-harden the host after testing.
    try:
        if sp.host_hardening_relaxed():
            r.add("host_hardening", "coding", UNVERIFIED,
                  "relaxed — kernel.apparmor_restrict_unprivileged_userns=0 (host hardening is "
                  "temporarily reduced so the coding sandbox can run; resets on reboot).",
                  "Restore after testing: `aithernet coding-sandbox restore` "
                  "(sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=1).")
    except Exception:  # noqa: BLE001 — the hardening note must never crash the read-only report
        pass


def _check_resources(r: DoctorReport) -> None:
    try:
        usage = shutil.disk_usage(str(_state_root().anchor or "/"))
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 1.0:
            r.add("disk_space", "resources", DEGRADED, f"{free_gb:.1f} GiB free",
                  "free up disk space before installing SDR dependencies")
        elif free_gb < 5.0:
            r.add("disk_space", "resources", OPTIONAL, f"{free_gb:.1f} GiB free (low for builds)")
        else:
            r.add("disk_space", "resources", READY, f"{free_gb:.1f} GiB free")
    except OSError as exc:
        r.add("disk_space", "resources", UNVERIFIED, str(exc))


def _check_hosted_connectivity(r: DoctorReport, *, online: bool) -> None:
    from aithernet.hosted.localconfig import load_enrollment
    enr = load_enrollment(_state_root() / "config")
    if not (enr and enr.enrollment_state == "enrolled"):
        r.add("hosted_connectivity", "hosted", OPTIONAL, "not enrolled; no hosted endpoint")
        return
    if not online:
        r.add("hosted_connectivity", "hosted", UNVERIFIED,
              "not checked (pass --online to probe the control plane)")
        return
    try:
        import httpx
        url = enr.control_plane_base_url.rstrip("/") + "/health/ready"
        resp = httpx.get(url, timeout=8.0)
        if resp.is_success:
            r.add("hosted_connectivity", "hosted", READY, "control plane reachable")
        else:
            r.add("hosted_connectivity", "hosted", DEGRADED,
                  f"control plane returned {resp.status_code}",
                  "check network / control-plane health")
    except Exception as exc:  # noqa: BLE001
        r.add("hosted_connectivity", "hosted", DEGRADED,
              f"control plane unreachable ({type(exc).__name__})", "check network connectivity")


def _check_mission_readiness(r: DoctorReport) -> None:
    rf = next((c for c in r.checks if c.name == "rf_mcp"), None)
    gr = next((c for c in r.checks if c.name == "gnuradio"), None)
    wiring = next((c for c in r.checks if c.name == "mcp_wiring"), None)
    blockers = []
    if rf and rf.status in _BLOCKING:
        blockers.append("RF-MCP")
    if wiring and wiring.status in _BLOCKING and "RF-MCP" not in blockers:
        # Component is installed/verified but not launchable (e.g. missing uv / unresolved).
        blockers.append("RF-MCP launch")
    if gr and gr.status in _BLOCKING:
        blockers.append("GNU Radio")
    if blockers:
        r.add("mission_subsystem", "mission", DEGRADED,
              "receive-only missions blocked: " + ", ".join(blockers),
              "resolve the RF-MCP / GNU Radio items above")
    else:
        r.add("mission_subsystem", "mission", READY,
              "receive-only mission prerequisites satisfied (no transmit)")


def run_doctor(*, online: bool = False) -> DoctorReport:
    """Build the full read-only readiness report."""
    r = DoctorReport()
    from aithernet.provisioning import state as sstate
    st = sstate.load_state(_state_root())
    needs = _profile_needs(st.hardware_profile if st else None)

    _check_core(r)
    _check_identity_enrollment(r)
    _check_setup_state(r)
    _check_service(r)
    _check_rf_stack(r, needs)
    _check_permissions_udev(r, needs)
    _check_devices(r, needs)
    _check_components(r)
    _check_mcp_wiring(r)
    _check_providers(r)
    _check_catgpt_gateway(r)
    _check_research_collection(r)
    _check_coding_sandbox(r)
    _check_resources(r)
    _check_hosted_connectivity(r, online=online)
    _check_mission_readiness(r)
    return r
