"""Coding-agent sandbox readiness preflight (1.0.0-beta.5).

A coding-agent workspace that isolates execution with **bubblewrap** (``bwrap``) needs the local
kernel to permit an unprivileged user namespace *and* — when the workspace is network-isolated —
to bring up loopback inside a fresh net namespace. On Ubuntu 24.04 the AppArmor control
``kernel.apparmor_restrict_unprivileged_userns=1`` blocks this unless bubblewrap has an AppArmor
profile granting ``userns``, producing exactly the beta.4 failures::

    bwrap: setting up uid map: Permission denied
    bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted

This module runs **non-destructive** probes to establish the truth (it actually runs ``bwrap true``
in a throwaway sandbox), reads the relevant kernel params for diagnostics, and classifies the
outcome with a precise, security-annotated remediation. It NEVER changes a sysctl and NEVER needs
sudo — it only detects and explains.

The *authoritative* signal is the live probe, not the sysctl value: a box with
``apparmor_restrict_unprivileged_userns=1`` can still pass when bubblewrap carries the Ubuntu
AppArmor profile, so we must not fail a working host on the sysctl alone.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field

#: Lower-cased substrings that identify a bubblewrap sandbox failure caused by the unprivileged
#: user-namespace / loopback restriction (the exact beta.4 signatures + close variants).
SANDBOX_SIGNATURES: tuple[str, ...] = (
    "setting up uid map: permission denied",
    "loopback: failed rtm_newaddr",
    "operation not permitted",           # RTM_NEWADDR / clone(CLONE_NEWUSER) tail
    "no permissions to create new namespace",
    "setting up uid map",
    "failed rtm_newaddr",
    "creating new namespace failed",
    "clone: operation not permitted",
    "user namespaces are not enabled",
)

#: The kernel params we read for diagnostics (dotted sysctl name -> /proc/sys path).
_SYSCTLS: tuple[str, ...] = (
    "kernel.apparmor_restrict_unprivileged_userns",
    "kernel.unprivileged_userns_clone",
    "user.max_user_namespaces",
    "user.max_net_namespaces",
)


@dataclass
class SandboxProbe:
    """Structured result of the coding-agent sandbox preflight."""

    severity: str          # ready | blocked | absent | unverified
    detail: str
    remediation: str = ""
    apparmor_restricted: bool = False
    bwrap_path: str | None = None
    bwrap_version: str | None = None
    signals: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.severity == "ready"

    def to_dict(self) -> dict:
        return {
            "severity": self.severity, "ready": self.ready, "detail": self.detail,
            "remediation": self.remediation, "apparmor_restricted": self.apparmor_restricted,
            "bwrap_path": self.bwrap_path, "bwrap_version": self.bwrap_version,
            "signals": dict(self.signals),
        }


def matches_sandbox_failure(text: str) -> bool:
    """True if ``text`` looks like a bubblewrap user-namespace / loopback sandbox failure."""
    if not text:
        return False
    low = text.lower()
    return any(sig in low for sig in SANDBOX_SIGNATURES)


def read_sysctl(name: str) -> str | None:
    """Read a sysctl value from ``/proc/sys`` (read-only). Returns None if unavailable."""
    from pathlib import Path
    path = Path("/proc/sys") / name.replace(".", "/")
    try:
        return path.read_text().strip()
    except OSError:
        return None


def apparmor_userns_restricted() -> bool:
    """True iff ``kernel.apparmor_restrict_unprivileged_userns`` is set to a non-zero value."""
    val = read_sysctl("kernel.apparmor_restrict_unprivileged_userns")
    return val is not None and val.strip() not in ("", "0")


#: The exact sysctl this repair toggles.
SYSCTL_APPARMOR_USERNS = "kernel.apparmor_restrict_unprivileged_userns"


def remediation_text() -> str:
    """The exact, security-annotated remediation for the AppArmor userns restriction."""
    return (
        "Ubuntu AppArmor user namespace restriction is blocking bubblewrap. "
        "Guided temporary repair: `aithernet coding-sandbox repair --temporary` (asks for "
        "confirmation; runs `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`). "
        "Restore after testing: `aithernet coding-sandbox restore` (or `sudo sysctl -w "
        "kernel.apparmor_restrict_unprivileged_userns=1`). WARNING: this relaxes a host hardening "
        "control — use only if acceptable on this workstation. Aithernet never changes it for you."
    )


def apparmor_restrict_value() -> str | None:
    """The current ``kernel.apparmor_restrict_unprivileged_userns`` value (``"0"``/``"1"``/None)."""
    return read_sysctl(SYSCTL_APPARMOR_USERNS)


def host_hardening_relaxed() -> bool:
    """True iff the AppArmor userns restriction is currently OFF (explicitly ``0``).

    Distinct from ``apparmor_userns_restricted()`` (which is True when it is ON): a host where the
    control is present and set to ``0`` is one where the hardening has been *relaxed* (e.g. by
    ``coding-sandbox repair --temporary``), which doctor should surface with a restore hint.
    """
    return apparmor_restrict_value() == "0"


def relax_userns_command() -> list[str]:
    """argv that temporarily relaxes the AppArmor userns restriction (NOT executed by this module).

    ``sysctl -w`` is runtime-only — it does NOT persist across a reboot — so the relaxation stays
    temporary by default.
    """
    return ["sudo", "sysctl", "-w", f"{SYSCTL_APPARMOR_USERNS}=0"]


def restore_userns_command() -> list[str]:
    """argv that restores the AppArmor userns hardening (NOT executed by this module)."""
    return ["sudo", "sysctl", "-w", f"{SYSCTL_APPARMOR_USERNS}=1"]


def _run_bwrap(bwrap: str, sandbox_args: list[str], timeout: float = 12.0) -> tuple[int, str]:
    """Run ``bwrap <sandbox_args> true`` non-destructively; return (rc, combined_output)."""
    argv = [bwrap, *sandbox_args, "true"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _bwrap_version(bwrap: str) -> str | None:
    try:
        p = subprocess.run([bwrap, "--version"], capture_output=True, text=True, timeout=8.0)
        out = (p.stdout + p.stderr).strip()
        return out.split()[-1] if out else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def preflight_sandbox(*, require_net: bool = True, timeout: float = 12.0) -> SandboxProbe:
    """Probe local bubblewrap sandbox readiness for coding-agent workspaces (non-destructive).

    Steps: locate ``bwrap`` → read kernel params → run a uid-map probe → (when ``require_net``) run
    a loopback/net-namespace probe. The live probe is authoritative; the AppArmor sysctl only
    contextualises a failure. On a host without ``bwrap`` the result is ``absent`` (advisory — the
    default coding providers don't shell out to bubblewrap, but an isolated workspace would).
    """
    signals: dict = {name: read_sysctl(name) for name in _SYSCTLS}
    restricted = apparmor_userns_restricted()
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return SandboxProbe(
            severity="absent",
            detail="bubblewrap (bwrap) is not installed — cannot verify sandboxed coding-agent "
                   "workspace isolation.",
            remediation="Install bubblewrap if your coding workspace uses it: "
                        "`sudo apt install bubblewrap`.",
            apparmor_restricted=restricted, signals=signals)

    version = _bwrap_version(bwrap)

    # 1) uid-map probe — the minimal unprivileged-userns capability.
    uid_rc, uid_out = _run_bwrap(bwrap, ["--ro-bind", "/", "/", "--unshare-user",
                                         "--uid", "0", "--gid", "0"], timeout)
    signals["uid_map_probe_rc"] = uid_rc
    if uid_rc != 0:
        blocked = matches_sandbox_failure(uid_out) or restricted
        return SandboxProbe(
            severity="blocked" if blocked else "unverified",
            detail=("bubblewrap uid-map probe failed: "
                    + _first_line(uid_out) + (" [apparmor_restrict_unprivileged_userns=1]"
                                              if restricted else "")),
            remediation=remediation_text() if restricted or matches_sandbox_failure(uid_out)
            else "Investigate bubblewrap/user-namespace support on this host "
                 "(`bwrap --unshare-user --uid 0 true`).",
            apparmor_restricted=restricted, bwrap_path=bwrap, bwrap_version=version,
            signals=signals)

    # 2) loopback / net-namespace probe (only when the workspace needs network isolation).
    if require_net:
        net_rc, net_out = _run_bwrap(bwrap, ["--ro-bind", "/", "/", "--unshare-user",
                                             "--unshare-net", "--uid", "0", "--gid", "0"], timeout)
        signals["loopback_probe_rc"] = net_rc
        if net_rc != 0:
            blocked = matches_sandbox_failure(net_out) or restricted
            return SandboxProbe(
                severity="blocked" if blocked else "unverified",
                detail=("bubblewrap loopback/net-namespace probe failed: "
                        + _first_line(net_out) + (" [apparmor_restrict_unprivileged_userns=1]"
                                                  if restricted else "")),
                remediation=remediation_text() if blocked
                else "Investigate net-namespace/loopback support "
                     "(`bwrap --unshare-net true`).",
                apparmor_restricted=restricted, bwrap_path=bwrap, bwrap_version=version,
                signals=signals)

    return SandboxProbe(
        severity="ready",
        detail=f"bubblewrap sandbox works (uid-map"
               f"{' + loopback' if require_net else ''} probe passed)"
               + (" despite apparmor_restrict_unprivileged_userns=1 (bwrap AppArmor profile grants "
                  "userns)" if restricted else ""),
        apparmor_restricted=restricted, bwrap_path=bwrap, bwrap_version=version, signals=signals)


def _first_line(text: str) -> str:
    """First non-empty line of a probe's output, bounded (for a concise, non-sensitive detail)."""
    for ln in (text or "").splitlines():
        if ln.strip():
            return ln.strip()[:200]
    return "(no output)"
