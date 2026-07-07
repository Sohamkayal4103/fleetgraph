"""Managed-component registry tests (Stage 14F).

Verifies the reproducible RF-component contract without any developer checkout: an installed
component is discovered from the managed root, its provenance is read from the lock file, and a
detached Ed25519 signature over the corresponding-source archive is verified against the real
public key (tamper is rejected).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from aithernet import components as comp


def _install(root, artifacts, name="rf-mcp", version="0.1.0+aithernet.1"):
    """Materialize a managed install + artifact bundle (source archive + sig + pubkey)."""
    prefix = root / name
    prefix.mkdir(parents=True)
    (prefix / "main.py").write_text("# entrypoint\n")
    # corresponding-source archive (any bytes for the test) + Ed25519 detached signature
    src = artifacts / name / f"{name}-{version}-src.tar.gz"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"SOURCE-ARCHIVE-BYTES")
    key = artifacts / name / "key.pem"
    pub = artifacts / name / "aithernet-component.pub"
    sig = artifacts / name / f"{name}-{version}-src.tar.gz.sig"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)], check=True)
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)], check=True)
    subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin",
                    "-in", str(src), "-out", str(sig)], check=True)
    import hashlib
    lock = {
        "name": name, "version": version, "license": "GPL-3.0-only",
        "upstream_url": "https://github.com/yoelbassin/gr-mcp",
        "upstream_commit": "8ca731e3f9a083b32170c9a0e6ee3d1e61b4f3de",
        "patch_series": ["0001-python312-numpy-gnuradio-compat.patch"],
        "source_archive": src.name,
        "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "python_min": "3.12", "gnuradio_min": "3.10",
        "runtime_command": "uv run --no-sync python main.py",
        "signature": sig.name, "public_key": pub.name,
    }
    (prefix / comp.LOCK_FILE).write_text(json.dumps(lock))
    return prefix, src


def test_list_and_status_from_managed_root(tmp_path, monkeypatch):
    root = tmp_path / "components"
    artifacts = tmp_path / "artifacts"
    _install(root, artifacts)
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(root))
    monkeypatch.setenv("AITHERNET_COMPONENT_ARTIFACTS", str(artifacts))
    rows = comp.list_components()
    assert [c.name for c in rows] == ["rf-mcp"]
    st = comp.component_status("rf-mcp")
    assert st.installed and st.version == "0.1.0+aithernet.1" and st.ok
    assert st.lock["upstream_commit"].startswith("8ca731e")


def test_verify_signature_and_tamper_rejected(tmp_path, monkeypatch):
    root = tmp_path / "components"
    artifacts = tmp_path / "artifacts"
    _, src = _install(root, artifacts)
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(root))
    monkeypatch.setenv("AITHERNET_COMPONENT_ARTIFACTS", str(artifacts))

    ok = comp.verify_component("rf-mcp")
    assert ok["ok"] is True
    assert ok["checks"]["signature_valid"] is True
    assert ok["checks"]["source_sha256_match"] is True

    # tamper the source archive -> signature + digest must fail closed
    src.write_bytes(b"SOURCE-ARCHIVE-BYTES-TAMPERED")
    bad = comp.verify_component("rf-mcp")
    assert bad["ok"] is False
    assert (bad["checks"]["signature_valid"] is False
            or bad["checks"]["source_sha256_match"] is False)


def test_uninstalled_component_reports_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(tmp_path / "empty"))
    st = comp.component_status("rf-mcp")
    assert st.installed is False and not st.ok
    res = comp.verify_component("rf-mcp")
    assert res["ok"] is False and res["installed"] is False


def test_real_built_component_if_present():
    """If the real build ran on this host, its installed component verifies end-to-end."""
    import os
    from pathlib import Path
    prefix = Path.home() / ".local/share/aithernet/components/rf-mcp"
    if not (prefix / comp.LOCK_FILE).is_file():
        pytest.skip("rf-mcp not built/installed on this host")
    os.environ.setdefault("AITHERNET_COMPONENTS_ROOT", str(prefix.parent))
    os.environ.setdefault("AITHERNET_COMPONENT_ARTIFACTS",
                          str(Path.home() / ".local/state/aithernet/components"))
    res = comp.verify_component("rf-mcp")
    assert res["installed"] and res["checks"].get("entrypoint_present")
