"""GNU Radio MCP auto-wiring from the managed rf-mcp component (Phase 1a).

A clean customer install must run RF missions with NO developer checkout and NO hand-set
``AITHERNET_GNURADIO_MCP_COMMAND``. When ``gnuradio_mcp.command`` is unset and the managed
``rf-mcp`` component is installed and intact, ``load_config`` derives the launch from the
component lock's ``runtime_command`` + install prefix. Explicit env/config always wins, and an
absent or broken component leaves the launch unconfigured (no crash, no faked behavior).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from aithernet import components as comp
from aithernet.config.loader import load_config
from aithernet.mcp.clients.stdio import check_stdio_ready

_NODE_YAML = """\
node_id: "00000000-0000-4000-8000-000000000001"
node_name: "test-node"
gnuradio_mcp:
  provider: stdio
  command: env:AITHERNET_GNURADIO_MCP_COMMAND
  args:
    - "main.py"
  cwd: env:AITHERNET_GR_MCP_DIR
"""


def _install_component(root: Path, runtime_command: str, *, with_entrypoint: bool = True) -> Path:
    """Materialize a minimal managed rf-mcp install carrying ``runtime_command`` in its lock."""
    prefix = root / "rf-mcp"
    prefix.mkdir(parents=True)
    if with_entrypoint:
        (prefix / "main.py").write_text("# entrypoint\n")
    lock = {
        "name": "rf-mcp",
        "version": "0.1.0+aithernet.2",
        "runtime_command": runtime_command,
        "license": "GPL-3.0-only",
    }
    (prefix / comp.LOCK_FILE).write_text(json.dumps(lock))
    return prefix


@pytest.fixture
def node_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "node.yaml"
    p.write_text(_NODE_YAML)
    return p


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    # Never load the repo .env, and start from an unset MCP command.
    monkeypatch.delenv("AITHERNET_GNURADIO_MCP_COMMAND", raising=False)
    monkeypatch.delenv("AITHERNET_GR_MCP_DIR", raising=False)


def _load(node_yaml: Path, tmp_path: Path):
    # env_file points at a nonexistent path so the real repo .env is never consulted.
    return load_config(path=node_yaml, env_file=tmp_path / "no-such.env")


def test_autowire_from_managed_component(node_yaml, tmp_path, monkeypatch):
    root = tmp_path / "components"
    prefix = _install_component(root, "uv run --no-sync python main.py")
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(root))

    cfg = _load(node_yaml, tmp_path)

    assert cfg.gnuradio_mcp.command == "uv"
    assert cfg.gnuradio_mcp.args == ["run", "--no-sync", "python", "main.py"]
    assert cfg.gnuradio_mcp.cwd == str(prefix)


def test_explicit_env_command_wins_over_component(node_yaml, tmp_path, monkeypatch):
    root = tmp_path / "components"
    _install_component(root, "uv run --no-sync python main.py")
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(root))
    monkeypatch.setenv("AITHERNET_GNURADIO_MCP_COMMAND", "/custom/python")
    monkeypatch.setenv("AITHERNET_GR_MCP_DIR", str(tmp_path))

    cfg = _load(node_yaml, tmp_path)

    # Explicit config is untouched; the component is NOT substituted.
    assert cfg.gnuradio_mcp.command == "/custom/python"
    assert cfg.gnuradio_mcp.args == ["main.py"]
    assert cfg.gnuradio_mcp.cwd == str(tmp_path)


def test_absent_component_leaves_unconfigured(node_yaml, tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(tmp_path / "empty"))

    cfg = _load(node_yaml, tmp_path)

    assert cfg.gnuradio_mcp.command is None
    assert "command" in check_stdio_ready(cfg.gnuradio_mcp)  # reported unconfigured, no crash


def test_broken_component_not_wired(node_yaml, tmp_path, monkeypatch):
    root = tmp_path / "components"
    # Lock references main.py but the entrypoint is missing -> component_status flags an issue.
    _install_component(root, "uv run --no-sync python main.py", with_entrypoint=False)
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(root))

    assert comp.installed_mcp_launch("rf-mcp") is None
    cfg = _load(node_yaml, tmp_path)
    assert cfg.gnuradio_mcp.command is None


def test_prefix_relative_command_is_absolutized(node_yaml, tmp_path, monkeypatch):
    # The shipped rf-mcp runtime_command is ".venv/bin/python main.py" (a prefix-relative
    # interpreter). It must resolve to an absolute path under the install prefix so it launches
    # regardless of the caller's CWD.
    root = tmp_path / "components"
    prefix = _install_component(root, ".venv/bin/python main.py")
    venv_python = prefix / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    venv_python.chmod(0o755)
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(root))

    launch = comp.installed_mcp_launch("rf-mcp")
    assert launch.command == str(venv_python)  # absolute, under the prefix
    assert launch.args == ["main.py"]
    assert launch.cwd == str(prefix)

    cfg = _load(node_yaml, tmp_path)
    assert cfg.gnuradio_mcp.command == str(venv_python)


def test_wired_launch_is_resolvable(node_yaml, tmp_path, monkeypatch):
    root = tmp_path / "components"
    # Use the live interpreter so command resolution succeeds deterministically (no uv needed).
    prefix = _install_component(root, f"{sys.executable} main.py")
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(root))

    cfg = _load(node_yaml, tmp_path)

    assert cfg.gnuradio_mcp.command == sys.executable
    assert cfg.gnuradio_mcp.cwd == str(prefix)
    # command resolvable, cwd is a real dir, no unresolved args -> fully ready to launch.
    assert check_stdio_ready(cfg.gnuradio_mcp) == []
