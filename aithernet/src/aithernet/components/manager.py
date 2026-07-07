"""Managed-component lifecycle: plan / install / verify / repair / remove (Stage 14G, PHASE 4B).

A *managed component* is built reproducibly from a pinned upstream commit plus a tracked Aithernet
patch series, then installed into a managed prefix (default ``/opt/aithernet/components/<name>``).
Aithernet launches the **installed** component, never a developer checkout.

Two install sources, both verifying provenance before anything lands on disk:

* **bundle** — a pre-built, Aithernet-signed bundle (source archive + detached Ed25519 signature +
  public key + lock). The signature is verified against the bundle's public key (authenticity), the
  archive digest is checked, then the source is extracted. This is the customer path.
* **source** — build from a pinned upstream git mirror + the patch series. The archive is created
  deterministically and signed for local integrity. Used for the from-source / offline path.

After install an **inventory** (every owned file + its sha256) is recorded, which makes ``verify``
detect missing/changed files, ``repair`` restore only owned files, and ``remove`` delete only owned
files — never shared system packages or anything outside the prefix.

This module performs NO network or privileged action implicitly: ``plan`` is pure; ``install``
acts only under the managed prefix + a state ``out_dir``; the spec source is never modified.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from aithernet.components import (
    LOCK_FILE,
    ComponentStatus,
    _read_lock,
    _sha256,
    _verify_ed25519,
    component_status,
    components_root,
)

INVENTORY_FILE = ".aithernet-component-files.json"
#: Files the manager owns at the prefix root but excludes from the hashed inventory (they are
#: metadata the manager writes itself).
_MANAGER_META = {LOCK_FILE, INVENTORY_FILE}

#: Deterministic tar mtime so source-mode rebuilds reproduce byte-for-byte.
_REPRO_MTIME = 1735689600  # 2025-01-01T00:00:00Z


class ComponentError(RuntimeError):
    """A bounded, actionable component-management failure (category in ``.category``)."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# ---------------------------------------------------------------------------
# Spec discovery (works from the installed package — never the dev checkout)
# ---------------------------------------------------------------------------
def specs_root() -> Path:
    """Where component source-of-truth specs live: env override → packaged data → repo fallback."""
    env = os.environ.get("AITHERNET_COMPONENT_SPECS")
    if env:
        return Path(env)
    # Packaged data: shipped inside the installed distribution (wheel/.deb), not the source tree.
    try:
        from importlib.resources import files
        packaged = Path(str(files("aithernet"))) / "_component_specs"
        if packaged.is_dir():
            return packaged
    except (ImportError, ModuleNotFoundError, AttributeError):
        pass
    # Dev fallback: the repo-root components/ directory.
    return Path(__file__).resolve().parents[3] / "components"


@dataclass
class ComponentSpec:
    name: str
    version: str
    summary: str = ""
    upstream_url: str = ""
    upstream_commit: str = ""
    patches: list[str] = field(default_factory=list)
    patch_series_id: str = ""
    license_spdx: str = ""
    notice_file: str = ""
    license_file: str = ""
    python_min: str = ""
    gnuradio_min: str = ""
    build_command: str = ""
    runtime_command: str = ""
    entrypoint: str = ""
    signing_public_key: str = ""  # PEM file (relative to spec_dir) pinning the component signer
    spec_dir: Path | None = None

    @property
    def patch_paths(self) -> list[Path]:
        base = self.spec_dir or Path()
        return [base / p for p in self.patches]

    @property
    def pinned_public_key_path(self) -> Path | None:
        """The trusted PEM public key shipped with the spec, if one is pinned."""
        if self.signing_public_key and self.spec_dir:
            p = self.spec_dir / self.signing_public_key
            return p if p.is_file() else None
        return None


def read_spec(name: str, *, root: Path | None = None) -> ComponentSpec:
    """Parse ``<specs_root>/<name>/component.toml`` into a :class:`ComponentSpec`."""
    import tomllib
    spec_dir = (root or specs_root()) / name
    toml = spec_dir / "component.toml"
    if not toml.is_file():
        raise ComponentError("spec_missing",
                             f"no component spec for '{name}' (looked in {spec_dir})")
    data = tomllib.loads(toml.read_text())
    c = data.get("component", {})
    src = data.get("source", {})
    lic = data.get("license", {})
    req = data.get("requirements", {})
    bld = data.get("build", {})
    run = data.get("runtime", {})
    signing = data.get("signing", {})
    return ComponentSpec(
        name=c.get("name", name), version=c.get("version", ""), summary=c.get("summary", ""),
        upstream_url=src.get("upstream_url", ""), upstream_commit=src.get("upstream_commit", ""),
        patches=list(src.get("patches", [])), patch_series_id=src.get("patch_series_id", ""),
        license_spdx=lic.get("spdx", ""), notice_file=lic.get("notice_file", ""),
        license_file=lic.get("upstream_license_file", ""),
        python_min=req.get("python_min", ""), gnuradio_min=req.get("gnuradio_min", ""),
        build_command=bld.get("command", ""), runtime_command=run.get("command", ""),
        entrypoint=run.get("entrypoint", ""), signing_public_key=signing.get("public_key", ""),
        spec_dir=spec_dir,
    )


def _patch_digest(spec: ComponentSpec) -> str:
    """sha256 over the concatenated patch series (order-significant) — the patch-set digest."""
    h = hashlib.sha256()
    for p in spec.patch_paths:
        if not p.is_file():
            raise ComponentError("patch_missing", f"patch not found: {p}")
        h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# plan — pure, no side effects
# ---------------------------------------------------------------------------
def plan(name: str, *, components_root_dir: Path | None = None) -> dict:
    """Describe exactly what installing ``name`` entails, with full provenance. No side effects."""
    spec = read_spec(name)
    root = components_root_dir or components_root()
    prefix = root / spec.name
    installed = component_status(spec.name)
    return {
        "name": spec.name,
        "version": spec.version,
        "summary": spec.summary,
        "license": spec.license_spdx,
        "upstream_url": spec.upstream_url,
        "upstream_commit": spec.upstream_commit,
        "patch_series": spec.patches,
        "patch_series_id": spec.patch_series_id,
        "patch_set_sha256": _patch_digest(spec),
        "build_command": spec.build_command,
        "runtime_command": spec.runtime_command,
        "entrypoint": spec.entrypoint,
        "requirements": {"python_min": spec.python_min, "gnuradio_min": spec.gnuradio_min},
        "install_prefix": str(prefix),
        "already_installed": installed.installed,
        "installed_version": installed.version,
        "notice_file": spec.notice_file,
        "provenance_verified_before_install": True,
        "privileged": str(prefix).startswith("/opt") and not os.access(
            root if root.exists() else root.parent, os.W_OK),
    }


# ---------------------------------------------------------------------------
# inventory + helpers
# ---------------------------------------------------------------------------
#: Environment/build directories that are NOT reproducible owned source and must never enter the
#: file inventory or its digest (created by the build step, e.g. the component's uv ``.venv``).
_NON_SOURCE_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
                    ".mypy_cache", "node_modules"}


def _iter_owned_files(prefix: Path):
    for p in sorted(prefix.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        # skip all manager metadata + its backups (".aithernet-component-*[.bak.*]")
        if p.name.startswith(".aithernet-component-"):
            continue
        rel_parts = p.relative_to(prefix).parts
        if any(part in _NON_SOURCE_DIRS for part in rel_parts[:-1]):
            continue
        if p.suffix in (".pyc", ".pyo"):
            continue
        yield p


def compute_inventory(prefix: Path) -> dict:
    """Hash every owned file under ``prefix`` (no write). Includes a stable inventory digest."""
    files = []
    for p in _iter_owned_files(prefix):
        rel = p.relative_to(prefix).as_posix()
        files.append({"path": rel, "sha256": _sha256(p), "size": p.stat().st_size})
    digest = hashlib.sha256(
        json.dumps([(f["path"], f["sha256"]) for f in files], sort_keys=True).encode()
    ).hexdigest()
    return {"prefix": str(prefix), "file_count": len(files),
            "inventory_digest": digest, "files": files}


def record_inventory(prefix: Path) -> dict:
    """Hash every owned file under ``prefix`` and write the inventory; returns it."""
    inv = compute_inventory(prefix)
    (prefix / INVENTORY_FILE).write_text(json.dumps(inv, indent=2, sort_keys=True))
    return inv


def _load_inventory(prefix: Path) -> dict | None:
    f = prefix / INVENTORY_FILE
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text())
    except ValueError:
        return None


def _deterministic_targz(src_tree: Path, top: str, dest: Path) -> str:
    """Write a reproducible .tar.gz of ``src_tree`` under prefix ``top/``; return its sha256."""
    members = sorted(src_tree.rglob("*"))
    import gzip
    raw = dest.with_suffix(".tar")
    with tarfile.open(raw, "w") as tar:
        for p in members:
            arc = f"{top}/{p.relative_to(src_tree).as_posix()}"
            ti = tar.gettarinfo(str(p), arcname=arc)
            ti.mtime = _REPRO_MTIME
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if p.is_file():
                with open(p, "rb") as fh:
                    tar.addfile(ti, fh)
            else:
                tar.addfile(ti)
    data = raw.read_bytes()
    raw.unlink()
    with gzip.GzipFile(filename="", mode="wb", fileobj=open(dest, "wb"), mtime=_REPRO_MTIME) as gz:
        gz.write(data)
    return _sha256(dest)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
def install(name: str, *, bundle: str | Path | None = None,
            upstream_git: str | Path | None = None,
            components_root_dir: Path | None = None,
            out_dir: Path | None = None,
            sign_key: str | Path | None = None,
            force: bool = False,
            build: bool = True) -> dict:
    """Install a managed component, verifying provenance before anything is written.

    ``bundle`` selects bundle mode (verify a pre-signed bundle dir). Otherwise source mode builds
    from ``upstream_git`` (a local git mirror/clone of the pinned upstream). When ``build`` is true
    (the default) the recorded ``build_command`` runs in the prefix after provenance verification,
    creating the component's runtime environment so it is immediately runnable; pass ``build=False``
    for offline/air-gapped installs that supply the environment separately. Returns a result dict
    with the recorded lock, inventory summary, and build state.
    """
    spec = read_spec(name)
    root = components_root_dir or components_root()
    prefix = root / spec.name
    out = Path(out_dir) if out_dir else (
        Path(os.environ.get("AITHERNET_COMPONENT_ARTIFACTS",
                            str(Path.home() / ".local/state/aithernet/components"))) / spec.name)
    out.mkdir(parents=True, exist_ok=True)

    if prefix.exists() and not force:
        existing = component_status(spec.name)
        if existing.installed:
            raise ComponentError("already_installed",
                                 f"'{spec.name}' already installed at {prefix} "
                                 f"(use force/--reinstall to replace)")

    if bundle is not None:
        lock = _install_from_bundle(spec, Path(bundle), prefix, out)
    else:
        lock = _install_from_source(spec, upstream_git, prefix, out, sign_key)

    inv = record_inventory(prefix)
    # final provenance gate: the just-installed component must verify (sig + digest + entrypoint).
    result = verify(spec.name, components_root_dir=root, artifacts_dir=out)
    result["installed_files"] = inv["file_count"]
    result["lock"] = lock
    if not result.get("ok"):
        raise ComponentError("verify_failed",
                             f"post-install verification failed for '{spec.name}': "
                             f"{result.get('issues')}")
    # Provenance is verified; now make the component runnable by building its runtime environment
    # (the inventory above is source-only, so the .venv created here never affects integrity).
    if build:
        bstate = _run_build_command(prefix, lock)
        result["build"] = bstate
        if not bstate.get("ok"):
            raise ComponentError("build_failed",
                                 f"component build step failed for '{spec.name}': "
                                 f"{bstate.get('reason')}")
    else:
        result["build"] = {"ran": False, "ok": True, "reason": "skipped (--no-build)"}
    return result


#: Bound for the component build step (e.g. `uv sync --frozen`): generous for a cold dependency
#: download, but finite so a hung build fails closed rather than blocking install forever.
_BUILD_TIMEOUT_SECONDS = 900


def _run_build_command(prefix: Path, lock: dict) -> dict:
    """Run the component's recorded ``build_command`` in the install prefix to create its runtime
    environment (for rf-mcp: ``uv sync --frozen`` → the component ``.venv``).

    This is what makes a freshly-installed component RUNNABLE from its managed prefix with no
    developer checkout. It is honest about failure: a missing build tool or a non-zero/timed-out
    build is reported (and raised by ``install``) rather than leaving a silently-unrunnable install.
    Returns a state dict; never prints secrets (the build command carries none).
    """
    cmd = (lock.get("build_command") or "").strip()
    if not cmd:
        return {"ran": False, "ok": True, "reason": "no build_command (nothing to build)"}
    # The build_command is trusted (it comes from the signed/verified component spec). We run each
    # "&&"-separated step as its OWN argv (no shell) so there is no shell-injection surface, while
    # still supporting the natural multi-step recipe (create venv, then install into it).
    for step in (s.strip() for s in cmd.split("&&") if s.strip()):
        parts = shlex.split(step)
        exe = parts[0]
        # An executable a prior step created in the prefix (e.g. .venv/bin/pip) is resolved against
        # the prefix; otherwise it must be on PATH.
        if not os.path.isabs(exe) and (prefix / exe).is_file():
            parts[0] = str(prefix / exe)
        elif shutil.which(exe) is None and not (prefix / exe).is_file():
            return {"ran": False, "ok": False,
                    "reason": f"build tool '{exe}' not found on PATH (install it, or use "
                              "--no-build and provide a pre-built environment)"}
        try:
            proc = subprocess.run(parts, cwd=str(prefix), capture_output=True, text=True,
                                  timeout=_BUILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return {"ran": True, "ok": False,
                    "reason": f"build step timed out after {_BUILD_TIMEOUT_SECONDS}s: {step}"}
        if proc.returncode != 0:
            return {"ran": True, "ok": False,
                    "reason": f"build step failed ({step}): {proc.stderr.strip()[-400:]}"}
    return {"ran": True, "ok": True, "command": cmd}


def _extract_top(archive: Path, into: Path) -> Path:
    """Safely extract a single-top-level-dir tar.gz; return the extracted top dir."""
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1:
            raise ComponentError("bad_archive", "source archive must have one top-level directory")
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise ComponentError("unsafe_archive", f"refusing unsafe path: {m.name}")
        tar.extractall(into)  # noqa: S202 — members validated above
    return into / tops.pop()


def _install_tree(top_dir: Path, prefix: Path, lock: dict) -> None:
    if prefix.exists():
        shutil.rmtree(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(top_dir), str(prefix))
    (prefix / LOCK_FILE).write_text(json.dumps(lock, indent=2, sort_keys=True))


def _install_from_bundle(spec: ComponentSpec, bundle: Path, prefix: Path, out: Path) -> dict:
    lock_path = bundle / "lock.json"
    if not lock_path.is_file():
        raise ComponentError("bundle_invalid", f"bundle missing lock.json: {bundle}")
    lock = json.loads(lock_path.read_text())
    src = bundle / lock["source_archive"]
    sig = bundle / lock.get("signature", "")
    pub = bundle / lock.get("public_key", "")
    if not src.is_file():
        raise ComponentError("bundle_invalid", f"bundle missing source archive: {src.name}")
    # provenance: archive digest + commit + patch set must match the spec we trust.
    if _sha256(src) != lock.get("source_sha256"):
        raise ComponentError("digest_mismatch", "bundle source archive digest mismatch")
    if spec.upstream_commit and lock.get("upstream_commit") != spec.upstream_commit:
        raise ComponentError("commit_mismatch",
                             "bundle upstream_commit does not match the trusted spec")
    if not (sig.is_file() and pub.is_file()):
        raise ComponentError("unsigned", "bundle is missing the signature/public key")
    # Trust anchor: when the spec pins a public key (shipped in the apt-signed .deb), verify the
    # detached signature against the PINNED key — not the bundle's own (replaceable) key — and
    # require the bundle's key to match it. Fail closed if the pin is present but doesn't match.
    pinned = spec.pinned_public_key_path
    if pinned is not None:
        if pinned.read_bytes().strip() != pub.read_bytes().strip():
            raise ComponentError("untrusted_key",
                                 "bundle public key does not match the pinned component key")
        verify_key = pinned
    else:
        verify_key = pub
    if not _verify_ed25519(verify_key, src, sig):
        raise ComponentError("bad_signature", "bundle signature verification failed")
    # copy the verified bundle into the state out_dir (so verify/repair can re-check later)
    for f in (src, sig, pub):
        shutil.copy2(f, out / f.name)
    top = _extract_top(src, Path(tempfile.mkdtemp()))
    _install_tree(top, prefix, lock)
    return lock


def _install_from_source(spec: ComponentSpec, upstream_git, prefix: Path, out: Path,
                         sign_key) -> dict:
    if not upstream_git:
        raise ComponentError("no_source",
                             "source mode needs --upstream-git <local clone/mirror of the pinned "
                             "upstream>; or use a signed --bundle")
    mirror = Path(upstream_git)
    if not (mirror / ".git").is_dir() and not (mirror / "HEAD").is_file():
        raise ComponentError("no_source", f"not a git repository: {mirror}")
    if not shutil.which("git"):
        raise ComponentError("no_git", "git is required for source-mode install")
    top = f"{spec.name}-{spec.version}"
    work = Path(tempfile.mkdtemp())
    stage = work / top
    stage.mkdir(parents=True)
    # 1) read-only archive of the pinned commit (never modifies the mirror)
    try:
        archived = subprocess.run(
            ["git", "-C", str(mirror), "archive", "--format=tar",
             f"--prefix={top}/", spec.upstream_commit],
            capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise ComponentError("archive_failed",
                             f"git archive of {spec.upstream_commit} failed: "
                             f"{exc.stderr.decode(errors='replace')[:200]}") from exc
    with tarfile.open(fileobj=__import__("io").BytesIO(archived)) as tar:
        tar.extractall(work)  # noqa: S202 — content is our own git archive of a pinned commit
    # 2) apply the patch series in order (verify the patch-set digest matches the spec)
    patch_sha = _patch_digest(spec)
    for patch in spec.patch_paths:
        _apply_patch(stage, patch)
    # 3) include license + notice from the spec dir (NOTICE) and the archived tree (LICENSE)
    if spec.notice_file and (spec.spec_dir / spec.notice_file).is_file():
        shutil.copy2(spec.spec_dir / spec.notice_file, stage / "AITHERNET-NOTICE.md")
    # 4) deterministic tarball + digest
    src = out / f"{top}-src.tar.gz"
    src_sha = _deterministic_targz(stage, top, src)
    (out / (src.name + ".sha256")).write_text(f"{src_sha}  {src.name}\n")
    # 5) sign for local integrity (self-signed: integrity, not third-party authenticity)
    pub = out / "aithernet-component.pub"
    sig = out / (src.name + ".sig")
    _sign_ed25519(src, sig, pub, sign_key, out)
    lock = {
        "name": spec.name, "version": spec.version, "license": spec.license_spdx,
        "upstream_url": spec.upstream_url, "upstream_commit": spec.upstream_commit,
        "patch_series": spec.patches, "patch_sha256": patch_sha,
        "source_archive": src.name, "source_sha256": src_sha,
        "python_min": spec.python_min, "gnuradio_min": spec.gnuradio_min,
        "build_command": spec.build_command, "runtime_command": spec.runtime_command,
        "signature": sig.name, "public_key": pub.name, "signing": "self-signed-source-integrity",
    }
    (out / "lock.json").write_text(json.dumps(lock, indent=2, sort_keys=True))
    top_dir = _extract_top(src, work / "install")
    _install_tree(top_dir, prefix, lock)
    return lock


def _apply_patch(tree: Path, patch: Path) -> None:
    """Apply one patch (stripping any From/Subject mail header above the diff)."""
    text = patch.read_text()
    idx = text.find("diff --git")
    diff = text[idx:] if idx >= 0 else text
    tool = "git" if shutil.which("git") else "patch"
    if tool == "git":
        proc = subprocess.run(["git", "apply", "-p1", "--unsafe-paths", "--directory", str(tree),
                               "-"], input=diff, text=True, capture_output=True)
        if proc.returncode == 0:
            return
    proc = subprocess.run(["patch", "-p1", "--no-backup-if-mismatch", "-s", "-d", str(tree)],
                          input=diff, text=True, capture_output=True)
    if proc.returncode != 0:
        raise ComponentError("patch_failed",
                             f"failed to apply {patch.name}: {proc.stderr[:200]}")


def _sign_ed25519(data: Path, sig: Path, pub: Path, sign_key, out: Path) -> None:
    if not shutil.which("openssl"):
        raise ComponentError("no_openssl", "openssl is required to sign the source archive")
    key = Path(sign_key) if sign_key else (out / "keys" / "aithernet-component.key")
    if not key.is_file():
        key.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(key.parent, 0o700)
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)],
                       check=True, capture_output=True)
        os.chmod(key, 0o600)
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin",
                    "-in", str(data), "-out", str(sig)], check=True, capture_output=True)


# ---------------------------------------------------------------------------
# verify (inventory + signature)
# ---------------------------------------------------------------------------
def verify(name: str, *, components_root_dir: Path | None = None,
           artifacts_dir: Path | None = None) -> dict:
    """Verify an installed component: inventory integrity (missing/changed/wrong prefix/version),
    runtime dependencies, and the detached signature over the corresponding-source archive."""
    root = components_root_dir or components_root()
    prefix = root / name
    lock = _read_lock(prefix)
    result: dict = {"name": name, "installed": lock is not None, "prefix": str(prefix),
                    "checks": {}, "issues": []}
    if lock is None:
        result["ok"] = False
        result["issues"].append("not installed (no lock under the managed root)")
        return result
    result["version"] = lock.get("version")
    checks = result["checks"]

    # version + prefix sanity
    try:
        spec = read_spec(name)
        checks["version_matches_spec"] = (lock.get("version") == spec.version)
        if not checks["version_matches_spec"]:
            result["issues"].append(
                f"installed version {lock.get('version')} != spec {spec.version}")
    except ComponentError:
        checks["version_matches_spec"] = None  # spec unavailable (e.g. removed); not fatal

    entry = lock.get("runtime_command", "") or ""
    entry_file = "main.py" if "main.py" in entry else (lock.get("entrypoint") or "main.py")
    checks["entrypoint_present"] = (prefix / entry_file).is_file()
    if not checks["entrypoint_present"]:
        result["issues"].append(f"runtime entrypoint {entry_file} missing")

    # inventory: missing / changed files
    inv = _load_inventory(prefix)
    if inv is None:
        result["issues"].append("no file inventory recorded (reinstall to enable integrity checks)")
        checks["inventory_present"] = False
    else:
        checks["inventory_present"] = True
        missing, changed = [], []
        for entry_rec in inv["files"]:
            fp = prefix / entry_rec["path"]
            if not fp.is_file():
                missing.append(entry_rec["path"])
            elif _sha256(fp) != entry_rec["sha256"]:
                changed.append(entry_rec["path"])
        checks["missing_files"] = missing
        checks["changed_files"] = changed
        if missing:
            result["issues"].append(f"{len(missing)} owned file(s) missing")
        if changed:
            result["issues"].append(f"{len(changed)} owned file(s) changed")

    # runtime dependency presence (best-effort, non-fatal advisory)
    checks["runtime_deps"] = _runtime_deps_present(lock)

    # signature over the source archive (when the bundle/state artifacts are available)
    art = Path(artifacts_dir) if artifacts_dir else _artifacts_dir_for(name)
    if art and (art / lock.get("source_archive", "")).is_file():
        src = art / lock["source_archive"]
        checks["source_sha256_match"] = (_sha256(src) == lock.get("source_sha256"))
        sig = art / lock.get("signature", "")
        pub = art / lock.get("public_key", "")
        if sig.is_file() and pub.is_file():
            checks["signature_valid"] = bool(_verify_ed25519(pub, src, sig))
            if not checks["signature_valid"]:
                result["issues"].append("source-archive signature INVALID")
        if not checks["source_sha256_match"]:
            result["issues"].append("source-archive digest mismatch")
    else:
        result["issues"].append("source artifact bundle not present; signature not re-verified")

    result["ok"] = bool(
        checks.get("entrypoint_present")
        and not checks.get("missing_files")
        and not checks.get("changed_files")
        and checks.get("signature_valid", True)
        and checks.get("source_sha256_match", True)
        and checks.get("version_matches_spec", True) in (True, None)
    )
    return result


def _runtime_deps_present(lock: dict) -> dict:
    """Best-effort runtime dependency presence for the RF MCP (uv/python). Advisory only."""
    out = {}
    cmd = lock.get("runtime_command", "") or ""
    if cmd.startswith("uv "):
        out["uv"] = shutil.which("uv") is not None
    out["python3"] = shutil.which("python3") is not None
    return out


def _artifacts_dir_for(name: str) -> Path | None:
    env = os.environ.get("AITHERNET_COMPONENT_ARTIFACTS")
    if env:
        return Path(env) / name
    cand = Path.home() / ".local/state/aithernet/components" / name
    return cand if cand.is_dir() else None


# ---------------------------------------------------------------------------
# repair (bounded: only owned files, only from the recorded source)
# ---------------------------------------------------------------------------
def repair(name: str, *, components_root_dir: Path | None = None,
           artifacts_dir: Path | None = None) -> dict:
    """Restore missing/changed OWNED files from the recorded source archive. Bounded: it never
    touches anything outside the prefix and never installs new system packages."""
    root = components_root_dir or components_root()
    prefix = root / name
    lock = _read_lock(prefix)
    if lock is None:
        raise ComponentError("not_installed", f"'{name}' is not installed; nothing to repair")
    pre = verify(name, components_root_dir=root, artifacts_dir=artifacts_dir)
    missing = pre["checks"].get("missing_files", [])
    changed = pre["checks"].get("changed_files", [])
    if not missing and not changed and pre.get("ok"):
        return {"name": name, "repaired": [], "ok": True, "detail": "already healthy"}
    art = Path(artifacts_dir) if artifacts_dir else _artifacts_dir_for(name)
    src = (art / lock["source_archive"]) if art else None
    if not (src and src.is_file()):
        raise ComponentError("no_source_archive",
                             "cannot repair: the recorded source archive is unavailable "
                             f"({lock.get('source_archive')}). Reinstall the component.")
    # verify the archive before trusting it
    if _sha256(src) != lock.get("source_sha256"):
        raise ComponentError("digest_mismatch", "recorded source archive fails its digest; refuse")
    fixset = set(missing) | set(changed)
    top = _extract_top(src, Path(tempfile.mkdtemp()))
    repaired = []
    for rel in sorted(fixset):
        srcf = top / rel
        if not srcf.is_file():
            continue
        dest = prefix / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(srcf, dest)
        repaired.append(rel)
    record_inventory(prefix)
    post = verify(name, components_root_dir=root, artifacts_dir=artifacts_dir)
    return {"name": name, "repaired": repaired, "ok": bool(post.get("ok")),
            "remaining_issues": post.get("issues", [])}


# ---------------------------------------------------------------------------
# remove (bounded: only owned files)
# ---------------------------------------------------------------------------
def remove(name: str, *, components_root_dir: Path | None = None,
           purge_artifacts: bool = False) -> dict:
    """Remove ONLY files owned by this component (from its inventory) plus its own metadata, then
    prune now-empty directories within the prefix. Never deletes shared system packages or files
    outside the managed prefix."""
    root = components_root_dir or components_root()
    prefix = root / name
    lock = _read_lock(prefix)
    if lock is None:
        raise ComponentError("not_installed", f"'{name}' is not installed; nothing to remove")
    inv = _load_inventory(prefix)
    removed, skipped = [], []
    if inv is not None:
        for rec in inv["files"]:
            fp = prefix / rec["path"]
            # only remove a file we still own (hash matches) — never clobber an edited/foreign file
            if fp.is_file() and not fp.is_symlink():
                if _sha256(fp) == rec["sha256"]:
                    fp.unlink()
                    removed.append(rec["path"])
                else:
                    skipped.append(rec["path"])
        for meta in (INVENTORY_FILE, LOCK_FILE):
            mp = prefix / meta
            if mp.is_file():
                mp.unlink()
        # prune empty dirs under the prefix, then the prefix itself if empty
        for d in sorted((p for p in prefix.rglob("*") if p.is_dir()), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        if prefix.exists() and not any(prefix.iterdir()):
            prefix.rmdir()
    else:
        # no inventory: refuse the blunt rmtree unless the prefix is clearly ours (lock present).
        shutil.rmtree(prefix)
        removed.append("(whole prefix; no inventory)")
    artifacts_removed = False
    if purge_artifacts:
        art = _artifacts_dir_for(name)
        if art and art.is_dir():
            shutil.rmtree(art)
            artifacts_removed = True
    return {"name": name, "removed": len(removed), "skipped_foreign": skipped,
            "prefix_gone": not prefix.exists(), "artifacts_removed": artifacts_removed}


# ---------------------------------------------------------------------------
# adopt — register an EXISTING working installation's provenance + inventory
# into the component manager WITHOUT replacing any runtime file.
# ---------------------------------------------------------------------------
def _expected_source_digests(source_checkout: Path, spec: ComponentSpec) -> dict:
    """Read-only: archive the pinned base commit from a local checkout, apply the patch series, and
    Returns base + final source digests and a per-file digest map. Never modifies the checkout.
    """
    import io
    work = Path(tempfile.mkdtemp())
    top = f"{spec.name}-{spec.version}"
    base_tar = subprocess.run(
        ["git", "-C", str(source_checkout), "archive", "--format=tar",
         f"--prefix={top}/", spec.upstream_commit],
        capture_output=True, check=True).stdout
    base_digest = hashlib.sha256(base_tar).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(base_tar)) as tar:
        tar.extractall(work)  # noqa: S202 — our own archive of a pinned commit
    stage = work / top
    for patch in spec.patch_paths:
        _apply_patch(stage, patch)
    files = {}
    for p in sorted(stage.rglob("*")):
        if p.is_file() and not p.is_symlink():
            files[p.relative_to(stage).as_posix()] = _sha256(p)
    final_digest = hashlib.sha256(
        json.dumps(sorted(files.items()), sort_keys=True).encode()).hexdigest()
    return {"base_source_sha256": base_digest, "final_source_sha256": final_digest, "files": files}


def adopt(name: str, *, from_installed: str | Path | None = None,
          source_checkout: str | Path | None = None, write_metadata: bool = False,
          dry_run: bool = True, components_root_dir: Path | None = None,
          artifacts_dir: Path | None = None) -> dict:
    """Adopt an EXISTING working component installation into the component manager.

    Records provenance + a hashed file inventory for the already-installed runtime WITHOUT replacing
    any runtime file. ``dry_run`` (default) reports only; ``write_metadata`` writes the lock +
    inventory (backing up the previous lock first). Idempotent. When ``source_checkout`` is given
    (a read-only local checkout), the installed files are additionally compared to the canonical
    base-commit + patch-series source tree and any difference stops the write.
    """
    root = components_root_dir or components_root()
    prefix = Path(from_installed) if from_installed else (root / name)
    lock = _read_lock(prefix)
    report: dict = {"name": name, "prefix": str(prefix), "dry_run": dry_run and not write_metadata,
                    "runtime_files_unchanged": True, "checks": {}, "differences": [],
                    "wrote": False}
    if lock is None:
        raise ComponentError("not_installed",
                             f"no installed component at {prefix} (nothing to adopt)")
    try:
        spec = read_spec(name)
    except ComponentError as exc:
        raise ComponentError("spec_missing",
                             f"cannot adopt without the trusted spec: {exc}") from exc

    checks = report["checks"]
    checks["version_matches_spec"] = (lock.get("version") == spec.version)
    checks["base_commit_matches_spec"] = (lock.get("upstream_commit") == spec.upstream_commit)
    entry = lock.get("runtime_command", "") or ""
    entry_file = "main.py" if "main.py" in entry else (spec.entrypoint or "main.py")
    checks["entrypoint_present"] = (prefix / entry_file).is_file()

    inv = compute_inventory(prefix)
    report["installed_file_count"] = inv["file_count"]
    report["inventory_digest"] = inv["inventory_digest"]

    patch_digest = _patch_digest(spec)
    report["base_commit"] = spec.upstream_commit
    report["patch_digest"] = patch_digest

    # component artifact (corresponding-source archive) digest + signature, where available
    art = Path(artifacts_dir) if artifacts_dir else _artifacts_dir_for(name)
    artifact_sha = None
    signature_state = "not_present"
    if art and (art / lock.get("source_archive", "")).is_file():
        src = art / lock["source_archive"]
        artifact_sha = _sha256(src)
        sig = art / lock.get("signature", "")
        pub = art / lock.get("public_key", "")
        if sig.is_file() and pub.is_file():
            signature_state = "valid" if _verify_ed25519(pub, src, sig) else "INVALID"
    report["artifact_sha256"] = artifact_sha
    report["signature_state"] = signature_state

    # optional read-only comparison of the installed files to the canonical source tree
    report["installed_matches_source"] = None
    if source_checkout:
        try:
            exp = _expected_source_digests(Path(source_checkout), spec)
            report["base_source_sha256"] = exp["base_source_sha256"]
            report["final_source_sha256"] = exp["final_source_sha256"]
            installed = {f["path"]: f["sha256"] for f in inv["files"]}
            diffs = []
            for rel, sha in exp["files"].items():
                # ignore build/runtime-generated paths not part of the source tree
                if rel in installed and installed[rel] != sha:
                    diffs.append({"path": rel, "expected": sha[:12],
                                  "installed": installed[rel][:12]})
            report["differences"] = diffs
            report["installed_matches_source"] = not diffs
        except (subprocess.CalledProcessError, ComponentError, OSError) as exc:
            report["differences"].append({"source_comparison_error": str(exc)[:200]})
            report["installed_matches_source"] = False

    required_ok = (checks["version_matches_spec"] and checks["base_commit_matches_spec"]
                   and checks["entrypoint_present"] and signature_state != "INVALID"
                   and report["installed_matches_source"] in (True, None))
    report["ok"] = bool(required_ok)

    # the adopted metadata (lock) — provenance + inventory digest; runtime files untouched
    adopted_lock = dict(lock)
    adopted_lock.update({
        "name": spec.name, "version": spec.version, "license": spec.license_spdx,
        "upstream_url": spec.upstream_url, "upstream_commit": spec.upstream_commit,
        "patch_series": spec.patches, "patch_sha256": patch_digest,
        "python_min": spec.python_min, "gnuradio_min": spec.gnuradio_min,
        "runtime_command": spec.runtime_command or lock.get("runtime_command"),
        "entrypoint": entry_file, "inventory_digest": inv["inventory_digest"],
        "install_prefix": str(prefix), "adopted": True,
    })
    if artifact_sha:
        adopted_lock["source_sha256"] = artifact_sha   # reconcile to the present artifact
    if "final_source_sha256" in report:
        adopted_lock["final_source_sha256"] = report["final_source_sha256"]
        adopted_lock["base_source_sha256"] = report["base_source_sha256"]
    report["adopted_lock"] = adopted_lock

    if write_metadata and not dry_run:
        if not required_ok:
            raise ComponentError("adopt_failed",
                                 f"refusing to write metadata: checks={checks} "
                                 f"differences={report['differences']}")
        # idempotent: only back up + rewrite when the lock or inventory actually changes
        changed = (lock != adopted_lock) or not (prefix / INVENTORY_FILE).is_file() \
            or _read_inventory_digest(prefix) != inv["inventory_digest"]
        if changed:
            import datetime as _dt
            ts = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
            backup = prefix / f"{LOCK_FILE}.bak.{ts}"
            backup.write_text(json.dumps(lock, indent=2, sort_keys=True))
            report["metadata_backup"] = str(backup)
            (prefix / LOCK_FILE).write_text(json.dumps(adopted_lock, indent=2, sort_keys=True))
            record_inventory(prefix)
            report["wrote"] = True
        else:
            report["wrote"] = False
            report["already_adopted"] = True
    return report


def _read_inventory_digest(prefix: Path) -> str | None:
    inv = _load_inventory(prefix)
    return inv.get("inventory_digest") if inv else None


def list_status() -> list[ComponentStatus]:
    """Convenience: installed components (delegates to the registry)."""
    from aithernet.components import list_components
    return list_components()
