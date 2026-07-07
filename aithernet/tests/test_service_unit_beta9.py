"""beta.9 Defect 1 + 2: the generated systemd unit pins the canonical config in ExecStart and a
managed runtime.env supplies the provider PATH so CLI providers (NVM/npm/distro) run under the
service without an interactive shell or any manual drop-in."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aithernet.provisioning import services


def _cfg(coordinator=("disabled", None), coding=("disabled", None)):
    return SimpleNamespace(
        coordinator=SimpleNamespace(provider=coordinator[0], executable=coordinator[1]),
        coding_agent=SimpleNamespace(provider=coding[0], executable=coding[1]),
    )


def _fake_node_cli(tmp_path: Path, name: str) -> Path:
    """A Node-shebang CLI under an NVM-style bin dir, alongside its node sibling."""
    binp = tmp_path / ".nvm" / "versions" / "node" / "v20.11.0" / "bin"
    binp.mkdir(parents=True)
    (binp / "node").write_text("#!/bin/sh\n")
    (binp / "node").chmod(0o755)
    exe = binp / name
    exe.write_text("#!/usr/bin/env node\n")
    exe.chmod(0o755)
    return exe


# -- Defect 1: ExecStart pins the canonical config explicitly --------------------------------

def test_user_unit_execstart_includes_canonical_config(tmp_path):
    root = tmp_path / "state"
    text = services.user_unit_text(state_root=root)
    cfg = root / "config" / "node.yaml"
    assert f"ExecStart={services._exe()} start --config {cfg}\n" in text
    # env vars are ALSO set (documented + tested) but correctness no longer depends on them alone
    assert f"Environment=AITHERNET_CONFIG={cfg}" in text
    assert f"Environment=AITHERNET_STATE_ROOT={root}" in text


def test_user_unit_references_managed_runtime_env():
    text = services.user_unit_text(state_root=Path("/x"))
    assert "EnvironmentFile=-%h/.config/aithernet/runtime.env" in text
    assert "EnvironmentFile=-%h/.config/aithernet/aithernet.env" in text


def test_system_unit_execstart_includes_canonical_config():
    text = services.system_unit_text(state_root="/var/lib/aithernet")
    assert "ExecStart=" in text
    assert "start --config /var/lib/aithernet/config/node.yaml\n" in text


# -- Defect 2: provider PATH resolution ------------------------------------------------------

def test_provider_dirs_resolve_nvm_cli(tmp_path):
    gem = _fake_node_cli(tmp_path, "gemini")
    cfg = _cfg(coordinator=("gemini_cli", str(gem)))
    dirs = services.provider_executable_dirs(cfg)
    assert str(gem.parent) in dirs  # the bin dir holding gemini AND its node sibling


def test_provider_dirs_empty_for_api_providers():
    cfg = _cfg(coordinator=("gemini_api", None), coding=("disabled", None))
    assert services.provider_executable_dirs(cfg) == []


def test_provider_dirs_skip_unresolvable_executable():
    cfg = _cfg(coordinator=("gemini_cli", "/nonexistent/path/gemini"))
    assert services.provider_executable_dirs(cfg) == []


def test_compute_runtime_path_prepends_provider_dir(tmp_path):
    gem = _fake_node_cli(tmp_path, "gemini")
    cfg = _cfg(coordinator=("gemini_cli", str(gem)))
    path = services.compute_runtime_path(cfg)
    assert path.startswith(str(gem.parent) + ":")
    assert "/usr/bin" in path  # the sane base is always present


def test_write_runtime_env_writes_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    gem = _fake_node_cli(tmp_path, "gemini")
    codex = _fake_node_cli(tmp_path / "codexhome", "codex")
    cfg = _cfg(coordinator=("gemini_cli", str(gem)), coding=("codex_cli", str(codex)))
    dest = services.write_runtime_env(cfg)
    assert dest == Path(tmp_path / "cfg") / "aithernet" / "runtime.env"
    content = dest.read_text()
    assert content.startswith("PATH=")
    assert str(gem.parent) in content
    assert str(codex.parent) in content
    # the file is not world-readable
    assert (dest.stat().st_mode & 0o077) == 0


def test_runtime_env_file_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    assert services.runtime_env_file() == tmp_path / "c" / "aithernet" / "runtime.env"
