"""Native `.deb` APT-install path of scripts/install.sh (beta.6 acceptance finding 1).

A fresh Ubuntu 24.04 image whose package indexes were never initialized fails
`apt install ./aithernet_*.deb` with "python3-venv ... is not installable". The installer's
`--deb` path must detect that stale/uninitialized-index condition, run an explicit visible
`apt-get update`, retry, install via APT, and NEVER suggest pip. It must fail clearly when the
repositories are genuinely missing or the update fails — with no pip fallback.

These tests are hermetic: a fake os-release + fake dpkg/apt-cache/apt-get on PATH model each
scenario, so the suite is independent of the host distribution.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_INSTALL_SH = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"


def _fixture(tmp_path: Path, *, candidate: str, update_rc: int = 0,
             update_makes_candidate: str | None = None) -> tuple[dict, Path]:
    """Build a fake apt environment.

    ``candidate``: initial `apt-cache policy` Candidate (e.g. "(none)" or "1.0").
    ``update_makes_candidate``: Candidate value after a successful `apt-get update` (simulates a
    stale index becoming valid). ``update_rc``: exit code of `apt-get update`.
    """
    bind = tmp_path / "bin"
    bind.mkdir()
    calls = tmp_path / "calls.log"
    flag = tmp_path / "updated"
    osr = tmp_path / "os-release"
    osr.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    (bind / "dpkg").write_text(
        '#!/bin/bash\n[[ "$1" == "--print-architecture" ]] && echo amd64\n')
    after = update_makes_candidate if update_makes_candidate is not None else candidate
    (bind / "apt-cache").write_text(
        f'#!/bin/bash\nif [[ -f "{flag}" ]]; then echo "  Candidate: {after}"; '
        f'else echo "  Candidate: {candidate}"; fi\n')
    (bind / "apt-get").write_text(
        f'#!/bin/bash\necho "apt-get $*" >> "{calls}"\n'
        f'case "$1" in update) [[ {update_rc} -eq 0 ]] && touch "{flag}";'
        f' exit {update_rc};; esac\n')
    for f in ("dpkg", "apt-cache", "apt-get"):
        (bind / f).chmod(0o755)

    deb = tmp_path / "aithernet_0.8.0~beta.6_amd64.deb"
    deb.write_bytes(b"fake")
    env = dict(os.environ)
    env.update(PATH=f"{bind}:{env['PATH']}", SUDO="", AITHERNET_OS_RELEASE=str(osr))
    return env, deb


def _run(env, deb, *extra):
    return subprocess.run(["bash", str(_INSTALL_SH), "--deb", str(deb), *extra],
                          capture_output=True, text=True, env=env, timeout=60)


def _calls(tmp_path):
    p = tmp_path / "calls.log"
    return p.read_text() if p.exists() else ""


def test_stale_index_then_update_then_install(tmp_path):
    # Candidate is (none) until apt-get update, after which it becomes valid.
    env, deb = _fixture(tmp_path, candidate="(none)", update_makes_candidate="1.0")
    res = _run(env, deb)
    assert res.returncode == 0, res.stderr
    calls = _calls(tmp_path)
    assert "apt-get update" in calls          # explicit visible refresh happened
    assert "apt-get install -y" in calls      # retried + installed via APT
    assert "stale/uninitialized" in res.stderr


def test_packages_available_without_update(tmp_path):
    # Candidate already present -> no update needed, installs directly.
    env, deb = _fixture(tmp_path, candidate="1.0")
    res = _run(env, deb)
    assert res.returncode == 0, res.stderr
    calls = _calls(tmp_path)
    assert "apt-get update" not in calls
    assert "apt-get install -y" in calls


def test_genuinely_missing_repositories(tmp_path):
    # Candidate stays (none) even after a successful update -> clear error, no install.
    env, deb = _fixture(tmp_path, candidate="(none)", update_makes_candidate="(none)")
    res = _run(env, deb)
    assert res.returncode != 0
    assert "repositories (main + universe) appear missing or unreachable" in res.stderr
    assert "apt-get install -y" not in _calls(tmp_path)   # never attempted the install


def test_failed_update_is_actionable(tmp_path):
    # apt-get update itself fails (no network) -> clear error, no install.
    env, deb = _fixture(tmp_path, candidate="(none)", update_rc=1)
    res = _run(env, deb)
    assert res.returncode != 0
    assert "update` failed" in res.stderr and "unreachable" in res.stderr
    assert "apt-get install -y" not in _calls(tmp_path)


def test_never_suggests_pip(tmp_path):
    # In the failure path, output must warn AGAINST pip and never instruct a pip install.
    env, deb = _fixture(tmp_path, candidate="(none)", update_makes_candidate="(none)")
    res = _run(env, deb)
    out = (res.stdout + res.stderr).lower()
    assert "do not" in out and "pip" in out          # an explicit do-NOT-use-pip warning
    assert "pip install" not in out                  # never an instruction to pip-install
    assert "pip3 install" not in out


def test_rejects_non_ubuntu_2404(tmp_path):
    env, deb = _fixture(tmp_path, candidate="1.0")
    (tmp_path / "os-release").write_text('ID=debian\nVERSION_ID="12"\n')
    res = _run(env, deb)
    assert res.returncode != 0
    assert "Ubuntu 24.04" in res.stderr
    assert "apt-get install -y" not in _calls(tmp_path)


def test_dry_run_plans_apt_without_executing(tmp_path):
    env, deb = _fixture(tmp_path, candidate="(none)", update_makes_candidate="1.0")
    res = _run(env, deb, "--dry-run")
    assert res.returncode == 0, res.stderr
    # dry-run shows the commands but runs neither update nor install.
    assert "apt-get install" not in _calls(tmp_path)
    assert "[dry-run] would install" in res.stderr


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_documents_deb_flag(flag):
    res = subprocess.run(["bash", str(_INSTALL_SH), flag],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0
    assert "--deb" in res.stdout
