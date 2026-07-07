"""Component manager lifecycle tests (Stage 14G, PHASE 4B).

Exercises plan / install (source + bundle modes) / verify / repair / remove against an ISOLATED
prefix, using a fake upstream git mirror and a fake component spec — no network, no developer
checkout, no real GNU Radio. Proves provenance verification, inventory integrity, bounded repair,
and bounded (owned-files-only) removal.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from aithernet.components import manager

_HAVE_GIT = shutil.which("git") is not None
_HAVE_OPENSSL = shutil.which("openssl") is not None
pytestmark = pytest.mark.skipif(not (_HAVE_GIT and _HAVE_OPENSSL),
                                reason="git + openssl required")


def _git(cwd, *args):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _fake_upstream(tmp_path) -> tuple[Path, str]:
    """A tiny git repo standing in for the pinned upstream (main.py + LICENSE)."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "main.py").write_text("print('rf-mcp stub')\n")
    (repo / "LICENSE").write_text("GPL-3.0 text\n")
    (repo / "lib").mkdir()
    (repo / "lib" / "core.py").write_text("X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def _fake_spec(tmp_path, commit, *, version="1.0.0+aithernet.1") -> Path:
    """A specs root containing one fake component 'demo' pinned to ``commit`` (no patches)."""
    specs = tmp_path / "specs"
    d = specs / "demo"
    d.mkdir(parents=True)
    (d / "component.toml").write_text(f'''
[component]
name = "demo"
version = "{version}"
summary = "demo component"
[source]
upstream_url = "https://example.invalid/demo"
upstream_commit = "{commit}"
patches = []
patch_series_id = "demo/none"
[license]
spdx = "GPL-3.0-only"
notice_file = "NOTICE.md"
[requirements]
python_min = "3.12"
gnuradio_min = "3.10"
[build]
command = "python3 -c pass"
[runtime]
entrypoint = "main.py"
command = ".venv/bin/python main.py"
transport = "stdio"
''')
    (d / "NOTICE.md").write_text("Aithernet demo notice\n")
    return specs


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo, commit = _fake_upstream(tmp_path)
    specs = _fake_spec(tmp_path, commit)
    root = tmp_path / "components"
    out = tmp_path / "state"
    monkeypatch.setenv("AITHERNET_COMPONENT_SPECS", str(specs))
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(root))
    monkeypatch.setenv("AITHERNET_COMPONENT_ARTIFACTS", str(out))
    return {"repo": repo, "commit": commit, "specs": specs, "root": root, "out": out,
            "prefix": root / "demo"}


# --- spec discovery + plan -------------------------------------------------
def test_specs_root_env_override(env):
    assert manager.specs_root() == env["specs"]
    spec = manager.read_spec("demo")
    assert spec.upstream_commit == env["commit"] and spec.entrypoint == "main.py"


def test_plan_is_pure(env):
    p = manager.plan("demo")
    assert p["upstream_commit"] == env["commit"]
    assert p["install_prefix"].endswith("/demo")
    assert "patch_set_sha256" in p
    assert not env["prefix"].exists()        # plan wrote nothing


# --- source-mode install + verify ------------------------------------------
def test_source_install_then_verify(env):
    res = manager.install("demo", upstream_git=env["repo"])
    assert res["ok"] is True
    assert res["installed_files"] >= 3       # main.py, LICENSE, lib/core.py, NOTICE
    assert (env["prefix"] / "main.py").is_file()
    assert (env["prefix"] / manager.LOCK_FILE).is_file()
    assert (env["prefix"] / manager.INVENTORY_FILE).is_file()
    v = manager.verify("demo")
    assert v["ok"] and v["checks"]["signature_valid"] and v["checks"]["entrypoint_present"]
    assert v["checks"]["inventory_present"] and not v["checks"]["missing_files"]


def test_install_refuses_double_install(env):
    manager.install("demo", upstream_git=env["repo"])
    with pytest.raises(manager.ComponentError) as exc:
        manager.install("demo", upstream_git=env["repo"])
    assert exc.value.category == "already_installed"
    # reinstall (force) succeeds
    res = manager.install("demo", upstream_git=env["repo"], force=True)
    assert res["ok"]


# --- verify detects tampering ----------------------------------------------
def test_verify_detects_changed_and_missing(env):
    manager.install("demo", upstream_git=env["repo"])
    # change an owned file
    (env["prefix"] / "lib" / "core.py").write_text("X = 999\n")
    v = manager.verify("demo")
    assert v["ok"] is False
    assert "lib/core.py" in v["checks"]["changed_files"]
    # remove an owned file
    (env["prefix"] / "main.py").unlink()
    v2 = manager.verify("demo")
    assert v2["ok"] is False
    assert "main.py" in v2["checks"]["missing_files"]
    assert v2["checks"]["entrypoint_present"] is False


# --- repair restores owned files -------------------------------------------
def test_repair_restores_only_owned_files(env):
    manager.install("demo", upstream_git=env["repo"])
    (env["prefix"] / "lib" / "core.py").write_text("CORRUPT\n")
    (env["prefix"] / "main.py").unlink()
    out = manager.repair("demo")
    assert out["ok"] is True
    assert set(out["repaired"]) == {"lib/core.py", "main.py"}
    assert (env["prefix"] / "main.py").read_text() == "print('rf-mcp stub')\n"
    assert manager.verify("demo")["ok"]


def test_repair_refuses_when_archive_digest_bad(env):
    manager.install("demo", upstream_git=env["repo"])
    (env["prefix"] / "main.py").unlink()
    # corrupt the recorded source archive so repair must refuse to trust it
    lock = json.loads((env["prefix"] / manager.LOCK_FILE).read_text())
    (env["out"] / "demo" / lock["source_archive"]).write_bytes(b"tampered")
    with pytest.raises(manager.ComponentError) as exc:
        manager.repair("demo")
    assert exc.value.category == "digest_mismatch"


# --- remove is bounded -----------------------------------------------------
def test_remove_only_owned_files(env):
    manager.install("demo", upstream_git=env["repo"])
    prefix = env["prefix"]
    # a foreign file that the component does NOT own must survive removal
    foreign = prefix / "foreign-do-not-touch.txt"
    foreign.write_text("operator data\n")
    res = manager.remove("demo", yes=True) if False else manager.remove("demo")
    assert res["removed"] >= 3
    assert foreign.is_file()                 # bounded: foreign file untouched
    assert not (prefix / "main.py").exists()
    assert not (prefix / manager.LOCK_FILE).exists()
    # component no longer registered
    from aithernet.components import component_status
    assert component_status("demo").installed is False


def test_remove_skips_edited_owned_file(env):
    manager.install("demo", upstream_git=env["repo"])
    prefix = env["prefix"]
    (prefix / "lib" / "core.py").write_text("operator-edited\n")  # owned but modified
    res = manager.remove("demo")
    assert "lib/core.py" in res["skipped_foreign"]   # not blindly deleted
    assert (prefix / "lib" / "core.py").is_file()


# --- bundle mode (the customer path) ---------------------------------------
def _make_bundle(env, bundle_dir: Path):
    """Build a signed bundle from a source-mode install's artifacts."""
    manager.install("demo", upstream_git=env["repo"])
    art = env["out"] / "demo"
    lock = json.loads((env["prefix"] / manager.LOCK_FILE).read_text())
    bundle_dir.mkdir(parents=True)
    for fn in (lock["source_archive"], lock["signature"], lock["public_key"]):
        shutil.copy2(art / fn, bundle_dir / fn)
    (bundle_dir / "lock.json").write_text(json.dumps(lock))
    # wipe the install so we can re-install from the bundle
    manager.remove("demo")
    return lock


def test_bundle_install_verifies_signature(env, tmp_path):
    bundle = tmp_path / "bundle"
    _make_bundle(env, bundle)
    res = manager.install("demo", bundle=bundle)
    assert res["ok"] and (env["prefix"] / "main.py").is_file()


def test_bundle_install_rejects_tampered_archive(env, tmp_path):
    bundle = tmp_path / "bundle"
    lock = _make_bundle(env, bundle)
    # tamper the archive AFTER the digest was recorded -> digest mismatch, fail closed
    (bundle / lock["source_archive"]).write_bytes(b"evil")
    with pytest.raises(manager.ComponentError) as exc:
        manager.install("demo", bundle=bundle)
    assert exc.value.category in ("digest_mismatch", "bad_signature")
    assert not env["prefix"].exists()


# --- packaging: rf-mcp spec resolves from the INSTALLED package (no repo, no checkout) ---
def test_rf_mcp_spec_ships_in_installed_package(tmp_path):
    """ACCEPTANCE #6: the managed RF-MCP spec is discoverable from an installed wheel — not the
    developer checkout / gr-mcp. Builds the wheel, installs it to a target tree, and resolves the
    spec with cwd outside the repo and NO env override."""
    import os
    import sys
    build = pytest.importorskip("build")  # noqa: F841
    repo_root = Path(__file__).resolve().parent.parent
    wheeldir = tmp_path / "wheel"
    proc = subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(wheeldir),
                           str(repo_root)], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        pytest.skip(f"wheel build unavailable: {proc.stderr[-300:]}")
    wheel = next(wheeldir.glob("*.whl"))
    target = tmp_path / "site"
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
                    "--target", str(target), str(wheel)], check=True, timeout=300)
    # resolve the spec in a clean subprocess: cwd outside the repo, no AITHERNET_COMPONENT_SPECS.
    script = ("from aithernet.components import manager;"
              "r=manager.specs_root();"
              "p=manager.plan('rf-mcp');"
              "print(str(r));print(p['upstream_commit'])")
    env = {"PYTHONPATH": str(target), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert "_component_specs" in lines[0]           # resolved from packaged data
    assert os.sep + "aithernet" + os.sep in lines[0] or "/aithernet/" in lines[0]
    assert lines[1] == "8ca731e3f9a083b32170c9a0e6ee3d1e61b4f3de"
