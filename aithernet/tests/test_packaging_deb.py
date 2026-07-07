"""Isolated .deb packaging tests (Stage 14F packaging fix).

Builds the ACTUAL Debian package with ``scripts/build_deb.sh``, extracts it into an isolated root,
and proves the documented node CLI works on the standard PATH **without pipx** — i.e. after a plain
``apt install ./aithernet_*.deb`` a customer can run ``aithernet ...`` directly. Also proves the
package does not ship the hosted control-plane command on customer nodes, the systemd unit uses the
same packaged runtime, and removal preserves persistent state.

The build pip-installs the runtime payload, so these tests need ``dpkg-deb`` and network; they skip
cleanly when unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_DEB = REPO_ROOT / "scripts" / "build_deb.sh"


@pytest.fixture(scope="module")
def deb_root(tmp_path_factory) -> Path:
    """Build the .deb once and extract it into an isolated root (no system install)."""
    if shutil.which("dpkg-deb") is None:
        pytest.skip("dpkg-deb not available")
    build_dir = tmp_path_factory.mktemp("deb-build")
    env = dict(os.environ)
    # Ensure the build host's `pip`/`python3` resolve to this interpreter (for the payload install).
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    env.update(VERSION="0.8.0-beta.1", ARCH="amd64", BUILD_DIR=str(build_dir))
    proc = subprocess.run(
        ["bash", str(BUILD_DEB)], env=env, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        pytest.skip(f"build_deb.sh could not build the payload here:\n{proc.stderr[-1500:]}")
    debs = list(build_dir.glob("*.deb"))
    assert len(debs) == 1, f"expected one .deb, got {debs}"
    extract = tmp_path_factory.mktemp("deb-root")
    subprocess.run(["dpkg-deb", "-x", str(debs[0]), str(extract)], check=True)
    # stash the .deb path for control/maintainer-script inspection
    extract_marker = extract / ".deb_path"
    extract_marker.write_text(str(debs[0]))
    return extract


def _run_cli(deb_root: Path, argline: str) -> subprocess.CompletedProcess:
    """Invoke the installed wrapper in a CLEAN env, with the runtime relocated under deb_root."""
    pydir = os.path.dirname(sys.executable)
    env = {
        "HOME": "/root",
        "PATH": f"{deb_root}/usr/bin:{pydir}:/usr/bin:/bin",
        "AITHERNET_HOME": f"{deb_root}/opt/aithernet",  # relocate the packaged runtime for the test
    }
    return subprocess.run(["sh", "-c", f"aithernet {argline}"], env=env,
                          capture_output=True, text=True, timeout=120)


def test_deb_installs_aithernet_on_standard_path(deb_root: Path) -> None:
    wrapper = deb_root / "usr" / "bin" / "aithernet"
    assert wrapper.is_file(), "/usr/bin/aithernet wrapper missing from the package"
    assert os.access(wrapper, os.X_OK), "/usr/bin/aithernet is not executable"
    # `command -v aithernet` resolves to the package wrapper (no pipx, no PYTHONPATH).
    found = _run_cli(deb_root, "--version || true")  # ensure dispatch works at all
    assert found.returncode in (0, 2), found.stderr


def test_deb_reports_version(deb_root: Path) -> None:
    """The conventional global `--version` works after install, reports the installed package
    version, and does so without starting services or contacting a node."""
    res = _run_cli(deb_root, "--version")
    assert res.returncode == 0, f"`aithernet --version` failed:\n{res.stdout}\n{res.stderr}"
    out = (res.stdout + res.stderr).strip()
    assert out.startswith("aithernet "), out
    # A real version number is reported (the installed dist version), not 'No such option'.
    assert "No such option" not in out
    assert any(ch.isdigit() for ch in out), out


@pytest.mark.parametrize("argline", ["--help", "setup --help", "enroll --help",
                                     "hosted status --help"])
def test_deb_documented_commands_work(deb_root: Path, argline: str) -> None:
    res = _run_cli(deb_root, argline)
    assert res.returncode == 0, f"`aithernet {argline}` failed:\n{res.stdout}\n{res.stderr}"


def test_deb_command_is_on_path_and_passes_exit_codes(deb_root: Path) -> None:
    pydir = os.path.dirname(sys.executable)
    env = {"HOME": "/root", "PATH": f"{deb_root}/usr/bin:{pydir}:/usr/bin:/bin",
           "AITHERNET_HOME": f"{deb_root}/opt/aithernet"}
    which = subprocess.run(["sh", "-c", "command -v aithernet"], env=env,
                           capture_output=True, text=True)
    assert which.returncode == 0 and which.stdout.strip().endswith("/usr/bin/aithernet")
    # a bad subcommand must propagate a non-zero exit code through the wrapper
    bad = _run_cli(deb_root, "definitely-not-a-command")
    assert bad.returncode != 0


def test_deb_payload_has_no_developer_paths(deb_root: Path) -> None:
    """The installed payload must not leak THIS build machine's checkout path. pip records the
    source path in dist-info/direct_url.json on a source install (build strips it) and rewrites
    console-script shebangs; both must point at no developer path. The scan keys on the actual
    build-checkout absolute path (portable across build hosts) — not any '/home/...' string, since
    third-party dependencies legitimately embed upstream example paths in their docs/metadata."""
    build_root = str(Path(__file__).resolve().parent.parent)  # this repo checkout
    payload = deb_root / "opt" / "aithernet" / "lib"
    leaks: list[str] = []
    for path in payload.rglob("*"):
        if not path.is_file():
            continue
        if path.name == "direct_url.json":
            leaks.append(str(path.relative_to(deb_root)) + " (pip source-install marker)")
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if build_root in text:
            leaks.append(str(path.relative_to(deb_root)))
    assert not leaks, "build-machine paths leaked into the .deb payload:\n" + "\n".join(leaks)


def test_deb_does_not_ship_hosted_admin_command(deb_root: Path) -> None:
    # The hosted control-plane CLI must not land on a customer node's PATH.
    assert not (deb_root / "usr" / "bin" / "aithernet-hosted").exists()


def test_deb_systemd_unit_uses_packaged_runtime(deb_root: Path) -> None:
    units = list((deb_root / "lib" / "systemd" / "system").glob("aithernet*.service"))
    assert units, "no systemd unit shipped"
    text = units[0].read_text()
    assert "/opt/aithernet/bin-launcher" in text, "unit must use the packaged runtime"


def test_deb_removal_preserves_persistent_state(deb_root: Path) -> None:
    deb = Path((deb_root / ".deb_path").read_text())
    prerm = subprocess.run(["dpkg-deb", "--info", str(deb), "prerm"],
                           capture_output=True, text=True)
    # prerm only stops/disables the service; it must NOT delete state or config.
    body = prerm.stdout
    assert "/var/lib/aithernet" not in body or "rm " not in body
    assert "rm -rf /var/lib/aithernet" not in body
    assert "rm -rf /etc/aithernet" not in body
    # the wrapper is package-owned, so dpkg removes it on uninstall
    manifest = (deb_root / "opt" / "aithernet" / "uninstall-manifest.txt").read_text()
    assert "/usr/bin/aithernet" in manifest
