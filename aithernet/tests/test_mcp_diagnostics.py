"""MCP diagnostics: helper, API, CLI, and the standalone validation script.

No test requires a real gr-mcp install, GNU Radio, uv, Claude Code, or API keys. Tests use
the existing tiny temp MCP server pattern (a TEST double written to a pytest temp path);
the diagnostics code itself never fabricates GNU Radio behavior.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aithernet.api.app import create_app
from aithernet.cli import app as cli_app
from aithernet.config.settings import MCPServerConfig
from aithernet.mcp.diagnostics import build_diagnostics
from aithernet.mcp.runtime import MCPRuntime
from aithernet.orchestrator.runtime import NodeRuntime

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "validate_gr_mcp.py"


def _load_validation_script():
    """Import scripts/validate_gr_mcp.py as a module (it is not a package)."""
    spec = importlib.util.spec_from_file_location("validate_gr_mcp", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _stdio_runtime(script: Path, **overrides) -> MCPRuntime:
    return MCPRuntime.from_config(
        MCPServerConfig(command=sys.executable, args=[str(script)], **overrides)
    )


def _write_node_yaml(path: Path, *, command: str, args: list[str]) -> Path:
    arg_lines = "".join(f'    - "{arg}"\n' for arg in args)
    path.write_text(
        "node_id: diag-test\n"
        "node_name: diag-test\n"
        "gnuradio_mcp:\n"
        "  provider: stdio\n"
        f'  command: "{command}"\n'
        "  args:\n"
        f"{arg_lines}"
        "  timeout_seconds: 30\n"
    )
    return path


# -- build_diagnostics helper ----------------------------------------------------


def test_diagnostics_no_probe_does_not_launch(tmp_path, write_mcp_server) -> None:
    marker = tmp_path / "launched.marker"
    script = write_mcp_server(tmp_path / "server", marker_path=marker)
    runtime = _stdio_runtime(script)

    diag = asyncio.run(build_diagnostics(runtime, probe=False))

    assert not marker.exists()  # the server was never started
    assert diag.configured is True
    assert diag.command_resolves is True
    assert diag.probe.attempted is False
    assert diag.probe.succeeded is None


def test_diagnostics_probe_launches_and_lists_tools(tmp_path, write_mcp_server) -> None:
    marker = tmp_path / "launched.marker"
    script = write_mcp_server(tmp_path / "server", marker_path=marker)
    runtime = _stdio_runtime(script)

    diag = asyncio.run(build_diagnostics(runtime, probe=True))

    assert marker.exists()  # the probe actually launched the server
    assert diag.probe.attempted is True
    assert diag.probe.succeeded is True
    assert diag.probe.tool_count == 1
    assert diag.probe.tool_names == ["echo"]


def test_diagnostics_cwd_reported(tmp_path, write_mcp_server) -> None:
    script = write_mcp_server(tmp_path / "server")
    runtime = _stdio_runtime(script, cwd=str(tmp_path))
    diag = asyncio.run(build_diagnostics(runtime, probe=False))
    assert diag.cwd == str(tmp_path)
    assert diag.cwd_exists is True

    missing_runtime = _stdio_runtime(script, cwd=str(tmp_path / "nope"))
    missing_diag = asyncio.run(build_diagnostics(missing_runtime, probe=False))
    assert missing_diag.cwd_exists is False
    assert "command" not in missing_diag.missing_configuration  # command is fine
    assert "cwd" in missing_diag.missing_configuration


# -- API endpoint ----------------------------------------------------------------


def test_api_diagnostics_unconfigured_clean(client: TestClient) -> None:
    response = client.get("/mcp/diagnostics")  # default app: MCP unconfigured
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["command"] is None
    assert "command" in body["missing_configuration"]
    assert body["probe"]["attempted"] is False
    assert body["hints"]  # actionable hints are present


def test_api_diagnostics_no_probe_does_not_launch(
    config, write_mcp_server, tmp_path
) -> None:
    # With the persistent session, autostart launches gr-mcp at startup; disable it so this
    # test still proves that a NON-probe diagnostics call spawns no subprocess of its own.
    from aithernet.config.settings import MCPSessionConfig

    marker = tmp_path / "launched.marker"
    script = write_mcp_server(tmp_path / "server", marker_path=marker)
    runtime = NodeRuntime.from_config(
        config, mcp=_stdio_runtime(script, session=MCPSessionConfig(autostart=False))
    )
    with TestClient(create_app(runtime=runtime)) as api:
        response = api.get("/mcp/diagnostics")
        assert response.status_code == 200
        assert response.json()["probe"]["attempted"] is False
    assert not marker.exists()


def test_api_diagnostics_does_not_leak_env(config) -> None:
    mcp = MCPRuntime.from_config(
        MCPServerConfig(
            command="definitely-not-a-real-binary-xyz",
            args=["--token", "x"],
            env={"GR_SECRET": "sk-super-secret-diag-value"},
        )
    )
    runtime = NodeRuntime.from_config(config, mcp=mcp)
    with TestClient(create_app(runtime=runtime)) as api:
        response = api.get("/mcp/diagnostics")
        assert response.status_code == 200
        assert "sk-super-secret-diag-value" not in response.text
        assert "env" not in response.json()


def test_api_diagnostics_probe_success(mcp_client: TestClient) -> None:
    response = mcp_client.get("/mcp/diagnostics", params={"probe": "true"})
    assert response.status_code == 200
    probe = response.json()["probe"]
    assert probe["attempted"] is True
    assert probe["succeeded"] is True
    assert probe["tool_count"] == 1
    assert probe["tool_names"] == ["echo"]


def test_api_diagnostics_probe_structured_failure(failing_mcp_client: TestClient) -> None:
    response = failing_mcp_client.get("/mcp/diagnostics", params={"probe": "true"})
    assert response.status_code == 200  # failure is structured, not an HTTP error
    probe = response.json()["probe"]
    assert probe["attempted"] is True
    assert probe["succeeded"] is False
    assert probe["error_type"] == "MCPClientError"
    assert "simulated MCP failure" in probe["error"]


# -- CLI -------------------------------------------------------------------------


def test_cli_diagnostics(mcp_live_server: str) -> None:
    result = runner.invoke(cli_app, ["mcp", "diagnostics", "--url", mcp_live_server])
    assert result.exit_code == 0
    assert "MCP diagnostics" in result.stdout
    assert "stub_mcp" in result.stdout
    assert "Probe:            not run" in result.stdout


def test_cli_diagnostics_probe(mcp_live_server: str) -> None:
    result = runner.invoke(
        cli_app, ["mcp", "diagnostics", "--probe", "--url", mcp_live_server]
    )
    assert result.exit_code == 0
    assert "Probe:            ok" in result.stdout
    assert "echo" in result.stdout


# -- standalone validation script ------------------------------------------------


def test_script_non_probe_does_not_launch(tmp_path, write_mcp_server, capsys) -> None:
    marker = tmp_path / "launched.marker"
    script = write_mcp_server(tmp_path / "server", marker_path=marker)
    node_yaml = _write_node_yaml(
        tmp_path / "node.yaml", command=sys.executable, args=[str(script)]
    )

    module = _load_validation_script()
    exit_code = module.main(["--config", str(node_yaml)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "configured:       yes" in out
    assert "probe:            not run" in out
    assert not marker.exists()  # non-probe never launches the server


def test_script_lists_tools(tmp_path, write_mcp_server, capsys) -> None:
    script = write_mcp_server(tmp_path / "server")
    node_yaml = _write_node_yaml(
        tmp_path / "node.yaml", command=sys.executable, args=[str(script)]
    )

    module = _load_validation_script()
    exit_code = module.main(["--config", str(node_yaml), "--list-tools"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "probe:            ok" in out
    assert "echo" in out


def test_script_unconfigured_returns_nonzero(tmp_path, capsys, monkeypatch) -> None:
    # Deterministic isolation: point the managed-components root + state root at empty temp dirs so
    # config auto-wiring CANNOT discover whatever rf-mcp happens to be installed on the dev host.
    # A truly unconfigured install (no gnuradio_mcp, no managed component) must report unconfigured.
    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(tmp_path / "no-components"))
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path / "state"))
    node_yaml = tmp_path / "node.yaml"
    node_yaml.write_text("node_id: t\nnode_name: t\n")  # no gnuradio_mcp -> unconfigured

    module = _load_validation_script()
    exit_code = module.main(["--config", str(node_yaml)])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "configured:       no" in out


def test_script_configured_with_managed_rfmcp(tmp_path, capsys, monkeypatch) -> None:
    """The INTENDED auto-wire path: when a managed rf-mcp component is installed+ok, a bare
    node.yaml (no explicit gnuradio_mcp) resolves to a configured launch."""
    import aithernet.components as components
    from aithernet.components import MCPLaunch

    monkeypatch.setenv("AITHERNET_COMPONENTS_ROOT", str(tmp_path / "no-components"))
    monkeypatch.setattr(
        components, "installed_mcp_launch",
        lambda name: (MCPLaunch(command=sys.executable, args=["-c", "pass"], cwd=str(tmp_path))
                      if name == "rf-mcp" else None))
    node_yaml = tmp_path / "node.yaml"
    node_yaml.write_text("node_id: t\nnode_name: t\n")  # autowires from the managed component

    module = _load_validation_script()
    exit_code = module.main(["--config", str(node_yaml)])

    out = capsys.readouterr().out
    assert "configured:       yes" in out
    assert exit_code == 0
