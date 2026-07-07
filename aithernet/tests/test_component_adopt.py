"""Component adoption tests (Stage 14G final pass, Commit B).

Adopt an EXISTING working installation into the component manager: record provenance + a hashed file
inventory WITHOUT replacing runtime files. Uses a fake upstream git + fake spec (no network); proves
dry-run, metadata-only write (runtime files byte-for-byte unchanged), backup, idempotency, drift
detection, and source-checkout comparison (match + mismatch).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from aithernet.components import LOCK_FILE, manager

_HAVE = shutil.which("git") and shutil.which("openssl")
pytestmark = pytest.mark.skipif(not _HAVE, reason="git + openssl required")


def _git(cwd, *a):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _fake_upstream(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "main.py").write_text("print('demo')\n")
    (repo / "LICENSE").write_text("GPL-3.0\n")
    (repo / "lib").mkdir()
    (repo / "lib" / "core.py").write_text("X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def _fake_spec(tmp_path, commit, version="1.0.0+aithernet.1"):
    d = tmp_path / "specs" / "demo"
    d.mkdir(parents=True)
    (d / "component.toml").write_text(f'''
[component]
name = "demo"
version = "{version}"
[source]
upstream_url = "https://example.invalid/demo"
upstream_commit = "{commit}"
patches = []
[license]
spdx = "GPL-3.0-only"
notice_file = "NOTICE.md"
[requirements]
python_min = "3.12"
gnuradio_min = "3.10"
[runtime]
entrypoint = "main.py"
command = "uv run --no-sync python main.py"
''')
    (d / "NOTICE.md").write_text("notice\n")
    return tmp_path / "specs"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo, commit = _fake_upstream(tmp_path)
    specs = _fake_spec(tmp_path, commit)
    root = tmp_path / "components"
    out = tmp_path / "state"
    monkeypatch.setenv("AITHERNET_COMPONENT_SPECS", str(specs))
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(root))
    monkeypatch.setenv("AITHERNET_COMPONENT_ARTIFACTS", str(out))
    # create a proper install, then simulate an OLD install (no inventory file)
    manager.install("demo", upstream_git=repo)
    prefix = root / "demo"
    (prefix / manager.INVENTORY_FILE).unlink()
    return {"repo": repo, "commit": commit, "root": root, "out": out, "prefix": prefix}


def _runtime_hash(prefix: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for p in sorted(prefix.rglob("*")):
        if p.is_file() and not p.name.startswith(".aithernet-component-"):
            h.update(p.relative_to(prefix).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


# --- dry-run ---------------------------------------------------------------
def test_adopt_dry_run_writes_nothing(env):
    before = _runtime_hash(env["prefix"])
    report = manager.adopt("demo", source_checkout=env["repo"])
    assert report["ok"] and report["wrote"] is False and report["dry_run"]
    assert report["installed_matches_source"] is True and report["differences"] == []
    assert report["checks"]["version_matches_spec"] and report["checks"]["base_commit_matches_spec"]
    assert not (env["prefix"] / manager.INVENTORY_FILE).is_file()   # dry-run wrote no inventory
    assert _runtime_hash(env["prefix"]) == before


# --- write metadata, runtime untouched -------------------------------------
def test_adopt_writes_metadata_without_touching_runtime(env):
    before = _runtime_hash(env["prefix"])
    report = manager.adopt("demo", source_checkout=env["repo"],
                           write_metadata=True, dry_run=False)
    assert report["ok"] and report["wrote"] is True
    # runtime files byte-for-byte unchanged
    assert _runtime_hash(env["prefix"]) == before
    # metadata written: inventory + adopted lock + backup
    assert (env["prefix"] / manager.INVENTORY_FILE).is_file()
    lock = json.loads((env["prefix"] / LOCK_FILE).read_text())
    assert lock["adopted"] is True and lock["inventory_digest"]
    assert list(env["prefix"].glob(f"{LOCK_FILE}.bak.*"))   # old lock backed up
    # component now verifies cleanly
    assert manager.verify("demo")["ok"]


# --- idempotent ------------------------------------------------------------
def test_adopt_is_idempotent(env):
    manager.adopt("demo", write_metadata=True, dry_run=False)
    backups = len(list(env["prefix"].glob(f"{LOCK_FILE}.bak.*")))
    again = manager.adopt("demo", write_metadata=True, dry_run=False)
    assert again["wrote"] is False and again.get("already_adopted") is True
    assert len(list(env["prefix"].glob(f"{LOCK_FILE}.bak.*"))) == backups   # no new backup


# --- drift detection -------------------------------------------------------
def test_verify_detects_drift_after_adopt(env):
    manager.adopt("demo", write_metadata=True, dry_run=False)
    assert manager.verify("demo")["ok"]
    (env["prefix"] / "lib" / "core.py").write_text("X = 999\n")   # drift a runtime file
    v = manager.verify("demo")
    assert v["ok"] is False and "lib/core.py" in v["checks"]["changed_files"]


# --- mismatch fails closed -------------------------------------------------
def test_adopt_blocks_on_source_mismatch(env):
    # tamper an installed runtime file so it no longer matches the canonical source tree
    (env["prefix"] / "lib" / "core.py").write_text("TAMPERED\n")
    report = manager.adopt("demo", source_checkout=env["repo"])
    assert report["installed_matches_source"] is False and report["differences"]
    assert report["ok"] is False
    with pytest.raises(manager.ComponentError):
        manager.adopt("demo", source_checkout=env["repo"], write_metadata=True, dry_run=False)


def test_adopt_blocks_on_version_mismatch(env, tmp_path, monkeypatch):
    # point the spec at a different version than the installed lock
    specs2 = _fake_spec(tmp_path / "v2", env["commit"], version="9.9.9")
    monkeypatch.setenv("AITHERNET_COMPONENT_SPECS", str(specs2))
    report = manager.adopt("demo")
    assert report["checks"]["version_matches_spec"] is False and report["ok"] is False


def test_adopt_requires_an_installation(env):
    shutil.rmtree(env["prefix"])
    with pytest.raises(manager.ComponentError):
        manager.adopt("demo")
