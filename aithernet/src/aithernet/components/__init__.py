"""Managed external components (Stage 14F).

Aithernet launches *installed*, signed, reproducible components from a managed root
(default ``/opt/aithernet/components``) — never a developer checkout. Each installed
component carries ``.aithernet-component-lock.json`` (provenance + hashes + the
build/runtime commands) and is verifiable against its detached Ed25519 signature and
public key. This package reads that managed root; it never modifies external repos.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Default managed install root; overridable so non-root/dev installs work without /opt.
DEFAULT_COMPONENTS_ROOT = "/opt/aithernet/components"
_FALLBACK_ROOT = str(Path.home() / ".local/share/aithernet/components")
LOCK_FILE = ".aithernet-component-lock.json"


def components_root() -> Path:
    """Resolve the managed components root (env override, then /opt, then a user dir)."""
    env = os.environ.get("AITHERNET_COMPONENTS_ROOT")
    if env:
        return Path(env)
    opt = Path(DEFAULT_COMPONENTS_ROOT)
    if opt.is_dir():
        return opt
    return Path(_FALLBACK_ROOT)


@dataclass
class ComponentStatus:
    name: str
    installed: bool
    version: str | None = None
    prefix: str | None = None
    lock: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.installed and not self.issues


def _read_lock(prefix: Path) -> dict | None:
    f = prefix / LOCK_FILE
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text())
    except ValueError:
        return None


def list_components() -> list[ComponentStatus]:
    """Every installed managed component (a subdir of the root carrying a lock file)."""
    root = components_root()
    out: list[ComponentStatus] = []
    if not root.is_dir():
        return out
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        lock = _read_lock(child)
        if lock is None:
            continue
        out.append(ComponentStatus(name=lock.get("name", child.name), installed=True,
                                    version=lock.get("version"), prefix=str(child), lock=lock))
    return out


def component_status(name: str) -> ComponentStatus:
    prefix = components_root() / name
    lock = _read_lock(prefix)
    if lock is None:
        return ComponentStatus(name=name, installed=False,
                               issues=["not installed (no lock file under the managed root)"])
    st = ComponentStatus(name=lock.get("name", name), installed=True,
                         version=lock.get("version"), prefix=str(prefix), lock=lock)
    entry = lock.get("runtime_command", "")
    if "main.py" in entry and not (prefix / "main.py").is_file():
        st.issues.append("runtime entrypoint main.py missing from the install prefix")
    return st


@dataclass
class MCPLaunch:
    """A runnable stdio MCP launch resolved from an installed managed component.

    ``command`` + ``args`` are the parsed ``runtime_command`` from the component lock;
    ``cwd`` is the installed prefix. This is how the runtime launches the managed RF-MCP
    server with no developer checkout and no hand-set environment variables.
    """

    command: str
    args: list[str]
    cwd: str


def installed_mcp_launch(name: str) -> MCPLaunch | None:
    """Resolve a runnable stdio MCP launch from an installed managed component, or ``None``.

    Returns ``None`` when the component is not installed, has integrity issues, or carries
    no usable ``runtime_command`` — so callers fall back to explicit configuration rather
    than launching a broken server. Reads only the managed install prefix; never a checkout.
    """
    st = component_status(name)
    if not st.ok or not st.prefix:
        return None
    parts = shlex.split((st.lock.get("runtime_command") or "").strip())
    if not parts:
        return None
    command = parts[0]
    # A prefix-relative interpreter (e.g. ".venv/bin/python") is resolved against the install
    # prefix so it launches regardless of the caller's CWD; a bare name (e.g. "uv") is left for
    # PATH lookup.
    if not os.path.isabs(command) and (Path(st.prefix) / command).is_file():
        command = str(Path(st.prefix) / command)
    return MCPLaunch(command=command, args=parts[1:], cwd=st.prefix)


def verify_component(name: str, *, artifacts_dir: str | Path | None = None) -> dict:
    """Verify an installed component: lock present, runtime entrypoint present, and — when the
    artifact bundle is available — the detached Ed25519 signature over the source archive.

    Verification depends on the real public key + signature, never just the recorded labels.
    """
    st = component_status(name)
    result: dict = {"name": name, "installed": st.installed, "version": st.version,
                    "prefix": st.prefix, "checks": {}, "issues": list(st.issues)}
    if not st.installed:
        result["ok"] = False
        return result
    checks = result["checks"]
    checks["lock_present"] = True
    checks["entrypoint_present"] = (Path(st.prefix) / "main.py").is_file()

    art = Path(artifacts_dir) if artifacts_dir else _artifacts_dir_for(st)
    sig_ok: bool | None = None
    if art and (art / st.lock.get("source_archive", "")).is_file():
        src = art / st.lock["source_archive"]
        # recorded source hash matches the shipped archive
        digest = _sha256(src)
        checks["source_sha256_match"] = (digest == st.lock.get("source_sha256"))
        sig = art / st.lock.get("signature", "")
        pub = art / st.lock.get("public_key", "")
        if sig.is_file() and pub.is_file():
            sig_ok = _verify_ed25519(pub, src, sig)
            checks["signature_valid"] = bool(sig_ok)
    else:
        result["issues"].append("source artifact bundle not present; signature not re-verified")
    result["ok"] = bool(
        checks.get("entrypoint_present")
        and checks.get("signature_valid", True)
        and checks.get("source_sha256_match", True)
        and not st.issues
    )
    return result


def _artifacts_dir_for(st: ComponentStatus) -> Path | None:
    env = os.environ.get("AITHERNET_COMPONENT_ARTIFACTS")
    if env:
        return Path(env) / st.name
    cand = Path.home() / ".local/state/aithernet/components" / st.name
    return cand if cand.is_dir() else None


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_ed25519(pub_pem: Path, data: Path, sig: Path) -> bool:
    """Verify a detached Ed25519 signature over ``data`` using the PEM public key."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        key = load_pem_public_key(pub_pem.read_bytes())
        try:
            key.verify(sig.read_bytes(), data.read_bytes())
            return True
        except InvalidSignature:
            return False
    except Exception:  # noqa: BLE001 — fall back to openssl if cryptography is unavailable
        proc = subprocess.run(
            ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(pub_pem),
             "-rawin", "-in", str(data), "-sigfile", str(sig)],
            capture_output=True,
        )
        return proc.returncode == 0
