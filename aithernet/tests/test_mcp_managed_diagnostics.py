"""beta.9 Defect 8: MCP diagnostics describe the managed, signed RF-MCP component and never tell
a customer to clone gr-mcp or run git. Repair flows through Aithernet (components install)."""

from __future__ import annotations

from aithernet.config.settings import MCPServerConfig
from aithernet.mcp.diagnostics import MCPDiagnostics, MCPProbeResult, _build_hints

_FORBIDDEN = ("clone", "git ", "github.com", "checkout", "AITHERNET_GR_MCP_DIR",
              "AITHERNET_GNURADIO_MCP_COMMAND")


def _diag(**kw):
    base = dict(provider="stdio", configured=True, timeout_seconds=30.0)
    base.update(kw)
    return MCPDiagnostics(**base)


def _no_forbidden(hints):
    blob = " ".join(hints).lower()
    for bad in _FORBIDDEN:
        assert bad.lower() not in blob, f"customer hint leaked developer guidance: {bad!r}"


def test_managed_component_described_no_clone():
    d = _diag(managed_component=True, managed_installed=True, managed_version="0.1.0+aithernet.2",
              managed_signature_valid=True, command="uv",
              resolved_command_path="/opt/aithernet/components/rf-mcp/.venv/bin/python")
    hints = _build_hints(d, MCPServerConfig(command="uv"))
    blob = " ".join(hints)
    assert "Managed RF-MCP component rf-mcp 0.1.0+aithernet.2" in blob
    assert "signature verified" in blob
    _no_forbidden(hints)


def test_managed_invalid_signature_points_to_repair():
    d = _diag(managed_component=True, managed_installed=True, managed_version="0.1.0+aithernet.2",
              managed_signature_valid=False, command="uv")
    hints = _build_hints(d, MCPServerConfig(command="uv"))
    blob = " ".join(hints)
    assert "aithernet components repair rf-mcp" in blob
    _no_forbidden(hints)


def test_not_installed_directs_to_managed_install_not_clone():
    d = _diag(configured=False, managed_component=False, managed_installed=False, command=None)
    hints = _build_hints(d, MCPServerConfig(command=None))
    blob = " ".join(hints)
    assert "aithernet components install rf-mcp" in blob
    _no_forbidden(hints)


def test_installed_but_unwired_directs_to_restart_or_repair():
    d = _diag(configured=False, managed_component=False, managed_installed=True,
              managed_version="0.1.0+aithernet.2", command=None)
    hints = _build_hints(d, MCPServerConfig(command=None))
    blob = " ".join(hints).lower()
    assert "doctor --repair" in blob or "service restart" in blob
    _no_forbidden(hints)


def test_probe_failure_points_to_component_verify():
    d = _diag(managed_component=True, managed_installed=True, managed_version="0.1.0+aithernet.2",
              managed_signature_valid=True, command="uv",
              probe=MCPProbeResult(attempted=True, succeeded=False, error="boom"))
    hints = _build_hints(d, MCPServerConfig(command="uv"))
    assert "aithernet components verify rf-mcp" in " ".join(hints)
    _no_forbidden(hints)
