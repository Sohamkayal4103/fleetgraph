"""Pinned-trust offline component bundle install (beta.5 Area 2).

A downloaded RF-MCP bundle is installed offline with ``--bundle`` and its detached signature is
verified against the PINNED component public key shipped in the trusted spec (the apt-signed .deb's
``_component_specs``) — NOT the bundle's own (replaceable) key. A bundle signed with any other key
is rejected, even though it is internally self-consistent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from aithernet.components import manager

_HAVE_OPENSSL = shutil.which("openssl") is not None
pytestmark = pytest.mark.skipif(not _HAVE_OPENSSL, reason="openssl required")

_COMMIT = "8ca731e3f9a083b32170c9a0e6ee3d1e61b4f3de"


def _keypair(d: Path, stem: str) -> tuple[Path, Path]:
    priv, pub = d / f"{stem}.key", d / f"{stem}.pub"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(priv)], check=True)
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)], check=True)
    return priv, pub


def _signed_bundle(tmp: Path, sign_priv: Path, bundle_pub: Path,
                   version="0.1.0+aithernet.2") -> Path:
    tree = tmp / f"rf-mcp-{version}"
    (tree / "src").mkdir(parents=True)
    (tree / "main.py").write_text("# entrypoint\n")
    bundle = tmp / "bundle"
    bundle.mkdir()
    archive = bundle / f"rf-mcp-{version}-src.tar.gz"
    src_sha = manager._deterministic_targz(tree, f"rf-mcp-{version}", archive)
    sig = bundle / (archive.name + ".sig")
    subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(sign_priv), "-rawin",
                    "-in", str(archive), "-out", str(sig)], check=True)
    shutil.copy2(bundle_pub, bundle / "aithernet-component.pub")
    (bundle / "lock.json").write_text(json.dumps({
        "name": "rf-mcp", "version": version, "license": "GPL-3.0-only",
        "source_archive": archive.name, "source_sha256": src_sha,
        "signature": sig.name, "public_key": "aithernet-component.pub",
        "upstream_commit": _COMMIT,
        "build_command": "python3 -c pass", "runtime_command": ".venv/bin/python main.py",
    }))
    return bundle


def _spec(spec_dir: Path, pinned_pub: Path) -> manager.ComponentSpec:
    spec_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pinned_pub, spec_dir / "aithernet-component.pub")
    return manager.ComponentSpec(
        name="rf-mcp", version="0.1.0+aithernet.2", upstream_commit=_COMMIT,
        signing_public_key="aithernet-component.pub", spec_dir=spec_dir,
        runtime_command=".venv/bin/python main.py", build_command="python3 -c pass")


def test_bundle_signed_with_pinned_key_installs(tmp_path):
    trusted_priv, trusted_pub = _keypair(tmp_path, "trusted")
    spec = _spec(tmp_path / "spec", trusted_pub)
    bundle = _signed_bundle(tmp_path, trusted_priv, trusted_pub)
    out = tmp_path / "out"
    out.mkdir()
    lock = manager._install_from_bundle(spec, bundle, tmp_path / "prefix", out)
    assert lock["version"] == "0.1.0+aithernet.2"
    assert (tmp_path / "prefix" / "main.py").is_file()  # installed


def test_bundle_signed_with_other_key_is_rejected(tmp_path):
    trusted_priv, trusted_pub = _keypair(tmp_path, "trusted")
    attacker_priv, attacker_pub = _keypair(tmp_path, "attacker")
    spec = _spec(tmp_path / "spec", trusted_pub)   # spec pins the TRUSTED key
    # A fully self-consistent bundle, but signed with (and carrying) the attacker's key.
    bundle = _signed_bundle(tmp_path, attacker_priv, attacker_pub)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(manager.ComponentError) as exc:
        manager._install_from_bundle(spec, bundle, tmp_path / "prefix", out)
    assert exc.value.category == "untrusted_key"
