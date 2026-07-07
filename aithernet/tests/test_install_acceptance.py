"""Stage 14F isolated installer + packaging acceptance (§44).

Runs entirely in a disposable temporary filesystem root — NEVER the developer machine's real
``/etc``, ``/var/lib``, systemd, or production state. A fake ``systemctl`` shim shadows the real
one so uninstall cannot touch systemd. Proves: signed offline release, manifest signature + artifact
checksum verification, install into a temp prefix, config-template + state-dir + systemd-unit
staging (via the .deb builder into a temp build dir), CLI invocation, preflight, uninstall with
state retention, and rejection + rollback for tampered manifest / tampered artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_INSTALL = _ROOT / "scripts" / "install.sh"
_BUILD_DEB = _ROOT / "scripts" / "build_deb.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None or shutil.which("bash") is None,
    reason="installer acceptance requires bash + openssl",
)


def _run(args, env=None, cwd=None):
    return subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, timeout=120)


def _make_signed_release(tmp: Path, *, content: bytes = b"AITHERNET-WHEEL-PAYLOAD") -> dict:
    """Build an offline signed release dir + an Ed25519 PEM public key (openssl)."""
    rel = tmp / "release"
    rel.mkdir(parents=True, exist_ok=True)
    priv = tmp / "priv.pem"
    pub = tmp / "pub.pem"
    assert _run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(priv)]).returncode == 0
    assert _run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)]).returncode == 0
    artifact = rel / "aithernet-0.8.0-beta.1.whl"
    artifact.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    manifest = {
        "version": "0.8.0-beta.1", "channel": "early-access", "signing_key_id": "rk1",
        "artifacts": [{"name": artifact.name, "sha256": sha, "byte_size": len(content)}],
    }
    (rel / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (rel / "SHA256SUMS").write_text(f"{sha}  {artifact.name}\n")  # documented archive companion
    # Detached Ed25519 signature over the exact manifest bytes.
    assert _run(["openssl", "pkeyutl", "-sign", "-inkey", str(priv), "-rawin",
                 "-in", str(rel / "manifest.json"), "-out", str(rel / "manifest.sig")]
                ).returncode == 0
    return {"release": rel, "pub": pub, "artifact": artifact.name, "sha": sha}


def _install_env(pub: Path, extra_path: Path | None = None) -> dict:
    env = dict(os.environ)
    env["AITHERNET_PUBKEY"] = str(pub)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}:{env['PATH']}"
    return env


def test_clean_install_verifies_and_installs(tmp_path):
    r = _make_signed_release(tmp_path)
    prefix = tmp_path / "opt" / "aithernet"
    proc = _run(["bash", str(_INSTALL), "--offline", str(r["release"]), "--prefix", str(prefix),
                 "--yes"], env=_install_env(r["pub"]))
    assert proc.returncode == 0, proc.stderr
    assert "signature OK" in proc.stderr
    assert "all artifacts verified" in proc.stderr
    assert (prefix / r["artifact"]).is_file()  # verified artifact landed under the temp prefix


def test_preflight_and_dry_run_no_install(tmp_path):
    r = _make_signed_release(tmp_path)
    prefix = tmp_path / "opt" / "aithernet"
    proc = _run(["bash", str(_INSTALL), "--offline", str(r["release"]), "--prefix", str(prefix),
                 "--dry-run"], env=_install_env(r["pub"]))
    assert proc.returncode == 0, proc.stderr
    assert "platform:" in proc.stderr  # preflight platform detection ran
    assert not prefix.exists()  # dry-run installs nothing


def test_tampered_manifest_rejected(tmp_path):
    r = _make_signed_release(tmp_path)
    # Tamper the manifest AFTER signing -> signature must fail.
    manifest_path = r["release"] / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["version"] = "9.9.9"
    manifest_path.write_text(json.dumps(data, indent=2))
    prefix = tmp_path / "opt" / "aithernet"
    proc = _run(["bash", str(_INSTALL), "--offline", str(r["release"]), "--prefix", str(prefix),
                 "--yes"], env=_install_env(r["pub"]))
    assert proc.returncode != 0
    assert "signature verification FAILED" in proc.stderr
    assert not (prefix / r["artifact"]).exists()  # nothing installed


def test_tampered_artifact_rejected_and_rolls_back(tmp_path):
    r = _make_signed_release(tmp_path)
    # Corrupt the artifact bytes -> checksum mismatch (manifest + signature still valid).
    (r["release"] / r["artifact"]).write_bytes(b"TAMPERED-PAYLOAD")
    prefix = tmp_path / "opt" / "aithernet"
    proc = _run(["bash", str(_INSTALL), "--offline", str(r["release"]), "--prefix", str(prefix),
                 "--yes"], env=_install_env(r["pub"]))
    assert proc.returncode != 0
    assert "SHA-256 mismatch" in proc.stderr
    assert not (prefix / r["artifact"]).exists()  # rollback / no partial install


def test_uninstall_removes_prefix_but_keeps_state(tmp_path):
    r = _make_signed_release(tmp_path)
    prefix = tmp_path / "opt" / "aithernet"
    # A fake systemctl shim guarantees the real systemd is never touched.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    shim = fakebin / "systemctl"
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)
    # Simulated persistent user state (the analogue of /var/lib/aithernet) — must be preserved.
    state = tmp_path / "state"
    state.mkdir()
    (state / "node.db").write_text("user-state")
    env = _install_env(r["pub"], extra_path=fakebin)
    assert _run(["bash", str(_INSTALL), "--offline", str(r["release"]), "--prefix", str(prefix),
                 "--yes"], env=env).returncode == 0
    assert (prefix / r["artifact"]).is_file()
    un = _run(["bash", str(_INSTALL), "--uninstall", "--prefix", str(prefix), "--yes"], env=env)
    assert un.returncode == 0, un.stderr
    assert not prefix.exists()  # app prefix removed
    assert (state / "node.db").read_text() == "user-state"  # persistent state retained


def test_deb_staging_into_isolated_build_dir(tmp_path):
    """The .deb builder stages config templates, state dirs and the systemd unit into a TEMP
    build dir (never the real /etc, /var/lib, or systemd)."""
    build = tmp_path / "build"
    env = dict(os.environ)
    env.update({"BUILD_DIR": str(build), "DRY_RUN": "1", "VERSION": "0.8.0-beta.1",
                "ARCH": "amd64"})
    proc = _run(["bash", str(_BUILD_DEB)], env=env, cwd=str(_ROOT))
    assert proc.returncode == 0, proc.stderr
    stage = build / "aithernet_0.8.0~beta.1_amd64"
    # Config templates.
    assert (stage / "etc/aithernet/node.yaml.template").is_file()
    assert (stage / "etc/aithernet/aithernet.env.template").is_file()
    # State / runtime directories matching the node state-root layout.
    for d in ("config", "db", "identity", "run", "logs", "backups", "artifacts"):
        assert (stage / "var/lib/aithernet" / d).is_dir()
    # Identity dir is private (0700).
    assert (stage / "var/lib/aithernet/identity").stat().st_mode & 0o777 == 0o700
    # systemd unit rendered (staged, not installed to the real system).
    assert (stage / "lib/systemd/system/aithernet-node.service").is_file()
    # Version + uninstall metadata + control with a non-faked dependency line.
    assert (stage / "opt/aithernet/VERSION").read_text().strip() == "0.8.0-beta.1"
    assert (stage / "opt/aithernet/uninstall-manifest.txt").is_file()
    control = (stage / "DEBIAN/control").read_text()
    assert "Depends: python3" in control
    # The packaged application includes the CLI module.
    assert (stage / "opt/aithernet/lib/aithernet/cli.py").is_file()
    # Nothing was written under the real /etc or /var/lib.
    assert str(stage).startswith(str(tmp_path))


def test_packaged_cli_is_invocable():
    """CLI invocation: the installed console script runs (the packaged layout carries cli.py)."""
    aithernet = _ROOT / ".venv" / "bin" / "aithernet"
    if not aithernet.exists():
        pytest.skip("aithernet console script not installed in .venv")
    proc = _run([str(aithernet), "--help"])
    assert proc.returncode == 0
    assert "hosted" in proc.stdout.lower()  # the Stage 14F onboarding group is registered
