"""Hardware installation profiles, doctor, discovery and verification (Stage 14F).

Customer-facing host RF setup for the Ubuntu 24.04 LTS amd64 baseline. ``doctor``/``discover``/
``verify`` are **read-only** and run without privilege — they report the real toolchain + attached
SDRs and give actionable remediation. ``install`` plans/executes a *profile* of signed distribution
packages via ``apt`` (privileged; printed and confirmed, never compiling GNU Radio/UHD/SoapySDR by
default). An explicit, confirmed source-build mode pins+hashes every revision into an
Aithernet-owned prefix (never ``/usr/local``) with a recorded bill of materials.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field

#: Maintained installation profiles → the apt packages they pull (signed distro packages).
PROFILES: dict[str, dict] = {
    "core": {
        "description": "RF MCP runtime + GNU Radio, no vendor SDR drivers.",
        "apt": ["gnuradio", "python3-numpy", "python3-packaging"],
    },
    "simulation": {
        "description": "Core + file/null sources for no-hardware development.",
        "apt": ["gnuradio", "gr-osmosdr"],
    },
    "pluto": {
        "description": "ADALM-PLUTO: libiio + Soapy Pluto + GNU Radio IIO.",
        "apt": ["gnuradio", "libiio-utils", "libiio0t64", "soapysdr-module-plutosdr",
                "soapysdr-tools", "gr-iio"],
    },
    "usrp": {
        "description": "Ettus USRP: UHD runtime, utilities and device images.",
        "apt": ["gnuradio", "uhd-host", "libuhd-dev", "soapysdr-module-uhd", "soapysdr-tools"],
        "post": ["uhd_images_downloader"],
    },
    "soapy-generic": {
        "description": "Generic SoapySDR runtime + tools + GNU Radio Soapy integration.",
        "apt": ["gnuradio", "soapysdr-tools", "gr-soapy", "soapysdr-module-all"],
    },
    "full": {
        "description": "Everything: Pluto + USRP + generic Soapy + GNU Radio integrations.",
        "apt": ["gnuradio", "gr-iio", "gr-soapy", "soapysdr-tools", "soapysdr-module-all",
                "libiio-utils", "uhd-host", "libuhd-dev"],
        "post": ["uhd_images_downloader"],
        "groups": ["plugdev", "dialout"],
    },
}

#: Source-build mode is OFF by default; every revision is pinned + hashed when enabled.
SOURCE_BUILD_PINS = {
    "prefix": "/opt/aithernet/rf-stack",   # Aithernet-owned; never /usr/local
    "components": [
        # name, git url, pinned tag/commit, expected source sha256 (filled by the operator run)
        {"name": "SoapySDR", "url": "https://github.com/pothosware/SoapySDR",
         "pin": "soapy-sdr-0.8.1", "sha256": "<record-on-fetch>"},
        {"name": "gnuradio", "url": "https://github.com/gnuradio/gnuradio",
         "pin": "v3.10.9.2", "sha256": "<record-on-fetch>"},
        {"name": "uhd", "url": "https://github.com/EttusResearch/uhd",
         "pin": "v4.6.0.0", "sha256": "<record-on-fetch>"},
    ],
}


def _cmd(*args: str, timeout: float = 20.0) -> tuple[int, str]:
    try:
        p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str | None = None
    remediation: str | None = None


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    sdrs: list[dict] = field(default_factory=list)

    def add(self, *a, **k) -> None:
        self.checks.append(Check(*a, **k))

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [vars(c) for c in self.checks],
            "detected_sdrs": self.sdrs,
        }


def discover_sdrs() -> list[dict]:
    """Enumerate attached SDRs (read-only) via SoapySDR + a USB fallback. Never claims a device."""
    found: list[dict] = []
    if shutil.which("SoapySDRUtil"):
        rc, out = _cmd("SoapySDRUtil", "--find", timeout=30)
        dev: dict = {}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Found device"):
                if dev:
                    found.append(dev)
                dev = {}
            elif "=" in line and dev is not None:
                k, _, v = line.partition("=")
                dev[k.strip()] = v.strip()
        if dev:
            found.append(dev)
    # de-duplicate by (driver,label), excluding non-RF Soapy factories (audio/null/file/...) via
    # the SAME canonical classifier every surface uses — a WSL audio endpoint is never an SDR.
    from aithernet.hardware.probing import is_rf_driver
    seen = set()
    uniq = []
    for d in found:
        if not is_rf_driver(d.get("driver")):
            continue
        key = (d.get("driver"), d.get("label"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(d)
    return uniq


def doctor() -> DoctorReport:
    """Read-only health report of the host RF stack + attached SDRs + RF MCP readiness."""
    r = DoctorReport()

    rc, out = _cmd("gnuradio-config-info", "--version")
    if rc == 0:
        r.add("gnuradio", "ok", f"GNU Radio {out.strip()}")
    else:
        r.add("gnuradio", "fail", "GNU Radio not found",
              "install a profile: `sudo aithernet hardware install --profile core`")

    if shutil.which("SoapySDRUtil"):
        rc, out = _cmd("SoapySDRUtil", "--info", timeout=20)
        mods = [ln.split("/")[-1].split()[0] for ln in out.splitlines() if "Module found" in ln]
        facs = next((ln.split("...", 1)[1].strip() for ln in out.splitlines()
                     if "Available factories" in ln), "")
        r.add("soapysdr", "ok", f"factories: {facs or 'none'}; modules: {len(mods)}")
    else:
        r.add("soapysdr", "warn", "SoapySDRUtil not found",
              "`sudo aithernet hardware install --profile soapy-generic`")

    rc, out = _cmd("uhd_config_info", "--version")
    if rc != 0:
        rc, out = _cmd("uhd_find_devices", "--help")
    if rc == 0 or "UHD" in out:
        img = "present" if shutil.which("uhd_images_downloader") else "downloader missing"
        r.add("uhd", "ok", f"UHD present; images: {img}")
    else:
        r.add("uhd", "warn", "UHD not found (only needed for Ettus USRP)",
              "`sudo aithernet hardware install --profile usrp`")

    if shutil.which("iio_info"):
        r.add("libiio", "ok", "libiio tools present (Pluto support)")
    else:
        r.add("libiio", "warn", "libiio not found (needed for ADALM-PLUTO)",
              "`sudo aithernet hardware install --profile pluto`")

    rc, out = _cmd("id")
    groups = out
    for g in ("plugdev", "dialout"):
        if f"({g})" in groups:
            r.add(f"group:{g}", "ok", f"user is in '{g}'")
        else:
            r.add(f"group:{g}", "warn", f"user not in '{g}' (USB SDR permissions)",
                  f"`sudo usermod -aG {g} $USER` then re-login")

    r.sdrs = discover_sdrs()
    if r.sdrs:
        labels = ", ".join(d.get("label", d.get("driver", "?")) for d in r.sdrs)
        r.add("sdr_devices", "ok", f"{len(r.sdrs)} detected: {labels}")
    else:
        r.add("sdr_devices", "info",
              "no SDRs detected (connect a device or use --profile simulation)")

    # RF MCP component readiness (managed, signed; never a dev checkout)
    try:
        from aithernet import components as comp
        st = comp.component_status("rf-mcp")
        if st.installed and st.ok:
            r.add("rf_mcp", "ok", f"rf-mcp {st.version} installed at {st.prefix}")
        elif st.installed:
            r.add("rf_mcp", "warn", "rf-mcp installed with issues",
                  "`aithernet components verify rf-mcp`")
        else:
            r.add("rf_mcp", "warn", "rf-mcp component not installed",
                  "install the signed RF MCP component (see the setup wizard)")
    except Exception as exc:  # noqa: BLE001
        r.add("rf_mcp", "warn", f"rf-mcp status unavailable ({type(exc).__name__})")
    return r


def verify() -> dict:
    """A pass/fail rollup of doctor() suitable for scripts (non-zero exit when any check fails)."""
    rep = doctor()
    return {"ok": rep.ok, "summary": {c.name: c.status for c in rep.checks},
            "detected_sdrs": len(rep.sdrs)}


def install_plan(profile: str) -> dict:
    """Return the privileged apt plan for a profile (the CLI prints + confirms before running)."""
    if profile not in PROFILES:
        raise KeyError(f"unknown profile '{profile}' (choose: {', '.join(PROFILES)})")
    spec = PROFILES[profile]
    apt = spec["apt"]
    return {
        "profile": profile,
        "description": spec["description"],
        "apt_packages": apt,
        "apt_command": ["sudo", "apt-get", "install", "-y", *apt],
        "post_commands": spec.get("post", []),
        "groups": spec.get("groups", []),
        "note": "Standard mode uses signed distribution packages only; no source compilation.",
    }
