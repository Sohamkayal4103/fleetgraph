"""Validated SDR dependency installation (Stage 14G, PHASE 5).

Modes: ``recommended`` (validated Ubuntu packages), ``source`` (pinned, hashed source builds into an
Aithernet-owned prefix), and ``offline`` (a pre-staged local package/source set). Plus ``dry-run``
planning, ``status`` and ``verify`` (runtime discovery), and bounded ``repair``/``remove``.

Dependency sets are scoped to the SELECTED profile only — never the whole Soapy/Pothosware
ecosystem. The PlutoSDR runtime path Aithernet actually uses is **GNU Radio IIO**
(``gnuradio.iio.fmcomms2_source_fc32`` over libiio, addressed by an IIO URI) for capture, plus
**SoapySDR + the SoapyPlutoSDR module** for discovery — see :mod:`aithernet.hardware.capture` and
``scripts/gr_capture.py``. The Pluto profile therefore pulls exactly libiio + gr-iio +
SoapyPlutoSDR, and nothing more.

Planning is pure (no side effects). Execution only ever runs ``apt`` (recommended) or a build into a
managed prefix (source) when explicitly confirmed; this module never silently escalates privilege.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from aithernet.hardware.setup import PROFILES, SOURCE_BUILD_PINS

RECOMMENDED = "recommended"
SOURCE = "source"
OFFLINE = "offline"
MODES = (RECOMMENDED, SOURCE, OFFLINE)

# Per-package estimates (MB) — clearly labelled estimates; apt resolves exact sizes.
_PKG_EST_MB: dict[str, tuple[float, float]] = {
    "gnuradio": (90.0, 420.0), "gr-osmosdr": (3.0, 12.0), "gr-iio": (2.0, 8.0),
    "gr-soapy": (2.0, 8.0), "soapysdr-tools": (1.0, 4.0), "soapysdr-module-all": (3.0, 12.0),
    "soapysdr-module-plutosdr": (1.0, 3.0), "soapysdr-module-uhd": (1.0, 4.0),
    "libiio-utils": (1.0, 3.0), "libiio0t64": (1.0, 3.0), "uhd-host": (40.0, 120.0),
    "libuhd-dev": (15.0, 60.0), "python3-numpy": (4.0, 30.0), "python3-packaging": (0.3, 1.0),
}
_DEFAULT_EST = (1.0, 4.0)

#: The runtime an installed profile must expose, and the bounded probe that proves it (read-only).
#: The Pluto probe is the EXACT import the capture path needs (gnuradio.iio).
_RUNTIME_PROBES: dict[str, list[list[str]]] = {
    "pluto": [["python3", "-c", "from gnuradio import gr, iio"],
              ["SoapySDRUtil", "--find"]],
    "core": [["gnuradio-config-info", "--version"]],
    "simulation": [["gnuradio-config-info", "--version"]],
    "soapy-generic": [["python3", "-c", "from gnuradio import gr, soapy"],
                      ["SoapySDRUtil", "--find"]],
    "usrp": [["uhd_find_devices"], ["python3", "-c", "from gnuradio import gr, uhd"]],
    "full": [["python3", "-c", "from gnuradio import gr, iio, soapy"]],
}


class SdrError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def _estimate(pkgs: list[str]) -> tuple[float, float]:
    dl = round(sum(_PKG_EST_MB.get(p, _DEFAULT_EST)[0] for p in pkgs), 1)
    sz = round(sum(_PKG_EST_MB.get(p, _DEFAULT_EST)[1] for p in pkgs), 1)
    return dl, sz


@dataclass
class SdrPlan:
    profile: str
    internal: str
    mode: str
    description: str = ""
    apt_packages: list[str] = field(default_factory=list)
    post_commands: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    source_components: list[dict] = field(default_factory=list)
    source_prefix: str = ""
    download_sources: list[str] = field(default_factory=list)
    est_download_mb: float = 0.0
    est_installed_mb: float = 0.0
    privileged_ops: list[str] = field(default_factory=list)
    requires_logout: bool = False
    runtime_probes: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    staged_debs: list[str] = field(default_factory=list)   # pre-staged .deb files (offline mode)
    offline_unavailable: bool = False                       # offline assets missing/incomplete

    def to_dict(self) -> dict:
        return {
            "profile": self.profile, "internal": self.internal, "mode": self.mode,
            "description": self.description, "apt_packages": self.apt_packages,
            "post_commands": self.post_commands, "groups": self.groups,
            "source_components": self.source_components, "source_prefix": self.source_prefix,
            "download_sources": self.download_sources, "est_download_mb": self.est_download_mb,
            "est_installed_mb": self.est_installed_mb, "privileged_ops": self.privileged_ops,
            "requires_logout": self.requires_logout, "runtime_probes": self.runtime_probes,
            "notes": self.notes, "staged_debs": self.staged_debs,
            "offline_unavailable": self.offline_unavailable,
        }


def _resolve_internal(profile: str) -> str:
    """Accept a customer-facing profile name OR an internal one; return the internal key."""
    try:
        from aithernet.provisioning import profiles as cprofiles
        return cprofiles.internal_name(profile)
    except Exception:  # noqa: BLE001 — fall back to direct internal lookup
        if profile in PROFILES:
            return profile
        raise SdrError("unknown_profile",
                       f"unknown profile '{profile}' (choose: {', '.join(PROFILES)})") from None


def _staged_debs(staged_dir: str | Path | None) -> tuple[Path | None, list[str]]:
    """Pre-staged OS-package payloads for OFFLINE mode: ``<staged_dir>/os-packages/*.deb`` (or
    ``<staged_dir>/*.deb``). Returns ``(dir, debs)``; ``debs`` empty means no offline payload."""
    if not staged_dir:
        return None, []
    base = Path(staged_dir)
    for d in (base / "os-packages", base):
        if d.is_dir():
            debs = sorted(str(x) for x in d.glob("*.deb"))
            if debs:
                return d, debs
    return (base / "os-packages" if (base / "os-packages").is_dir() else None), []


def plan(profile: str, mode: str = RECOMMENDED, *, staged_dir: str | Path | None = None) -> SdrPlan:
    """Build the (pure) install plan for ``profile`` in ``mode``. No side effects.

    OFFLINE never plans apt or network: it installs pre-staged ``.deb`` payloads from
    ``staged_dir`` via ``dpkg``, or — when no complete payload is staged — marks the plan
    ``offline_unavailable`` so the caller can stop with a clear message instead of falling back to
    apt."""
    if mode not in MODES:
        raise SdrError("unknown_mode", f"mode must be one of {MODES}")
    internal = _resolve_internal(profile)
    spec = PROFILES[internal]
    pkgs = list(spec.get("apt", []))
    p = SdrPlan(profile=profile, internal=internal, mode=mode, description=spec["description"],
                groups=list(spec.get("groups", [])),
                runtime_probes=_RUNTIME_PROBES.get(internal, []))

    if mode == OFFLINE:
        p.apt_packages = pkgs
        p.post_commands = list(spec.get("post", []))
        if pkgs:
            d, debs = _staged_debs(staged_dir)
            if debs:
                p.staged_debs = debs
                p.download_sources = []  # zero network/apt-index
                p.privileged_ops = [f"sudo dpkg -i {' '.join(debs)}"]
                p.notes.append(f"offline: installing {len(debs)} pre-staged package file(s) from "
                               f"{d}; zero apt/network access.")
            else:
                p.offline_unavailable = True
                p.download_sources = []
                p.notes.append(
                    "offline assets unavailable/incomplete: no pre-staged OS-package payloads "
                    + (f"(os-packages/*.deb) in {staged_dir}" if staged_dir
                       else "were provided (pass --from <release-download-dir>)")
                    + ". Use --mode recommended (validated apt repositories) or supply a complete "
                    "offline package bundle. Zero apt/network calls are planned.")
        if p.groups and not p.offline_unavailable:
            p.privileged_ops += [f"sudo usermod -aG {g} $USER" for g in p.groups]
            p.requires_logout = True
        if not p.offline_unavailable:
            for pc in p.post_commands:
                p.privileged_ops.append(pc)
    elif mode == RECOMMENDED:
        p.apt_packages = pkgs
        p.post_commands = list(spec.get("post", []))
        p.est_download_mb, p.est_installed_mb = _estimate(pkgs)
        p.download_sources = ["Ubuntu archive (apt, validated repositories)"] if pkgs else []
        p.privileged_ops = [f"sudo apt-get install -y {' '.join(pkgs)}"] if pkgs else []
        if p.groups:
            p.privileged_ops += [f"sudo usermod -aG {g} $USER" for g in p.groups]
            p.requires_logout = True
        for pc in p.post_commands:
            p.privileged_ops.append(pc)
    else:  # SOURCE
        p.source_prefix = SOURCE_BUILD_PINS["prefix"]
        needed = _source_components_for(internal)
        p.source_components = needed
        p.download_sources = [c["url"] for c in needed]
        p.privileged_ops = [
            f"build into {p.source_prefix} (pinned tags; digests recorded on fetch)"]
        p.notes.append("Source builds are long; every revision is pinned and its source digest "
                       "recorded before building. Never clones a moving main/master.")
    if internal == "pluto":
        p.notes.append("Pluto runtime path = GNU Radio IIO (gr-iio over libiio) for capture + "
                       "SoapyPlutoSDR for discovery. No full Soapy/UHD ecosystem is installed.")
    p.notes.append("Estimates are approximate; apt resolves exact sizes.")
    return p


def _source_components_for(internal: str) -> list[dict]:
    """Subset of the pinned source components a profile actually needs."""
    want = {"core": {"gnuradio"}, "simulation": {"gnuradio"},
            "pluto": {"gnuradio", "SoapySDR"}, "soapy-generic": {"gnuradio", "SoapySDR"},
            "usrp": {"gnuradio", "uhd"}, "full": {"gnuradio", "SoapySDR", "uhd"}}.get(
                internal, {"gnuradio"})
    return [c for c in SOURCE_BUILD_PINS["components"] if c["name"] in want]


# ---------------------------------------------------------------------------
# runtime verification (read-only)
# ---------------------------------------------------------------------------
def _probe(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def verify_runtime(profile: str) -> dict:
    """Run the profile's bounded runtime probes (read-only). Reports which succeeded."""
    internal = _resolve_internal(profile)
    probes = _RUNTIME_PROBES.get(internal, [])
    results = []
    ok = True
    for cmd in probes:
        # don't fail just because an optional discovery tool is absent; report honestly
        if not shutil.which(cmd[0]) and cmd[0] != "python3":
            results.append({"probe": " ".join(cmd), "status": "tool-absent"})
            ok = False
            continue
        rc, _ = _probe(cmd)
        results.append({"probe": " ".join(cmd), "status": "ok" if rc == 0 else "failed"})
        if rc != 0:
            ok = False
    return {"profile": profile, "internal": internal, "ok": ok, "probes": results}


def status(profile: str | None = None) -> dict:
    """Which RF runtimes are present on this host (read-only); plus profile runtime check."""
    present = {
        "gnuradio": _probe(["gnuradio-config-info", "--version"])[0] == 0,
        "soapysdr": shutil.which("SoapySDRUtil") is not None,
        "libiio": (shutil.which("iio_info") or shutil.which("iio_attr")) is not None,
        "uhd": _probe(["uhd_config_info", "--version"])[0] == 0,
        "gr_iio": _probe(["python3", "-c", "from gnuradio import iio"])[0] == 0,
        "gr_soapy": _probe(["python3", "-c", "from gnuradio import soapy"])[0] == 0,
    }
    out = {"present": present}
    if profile:
        out["runtime"] = verify_runtime(profile)
    return out
