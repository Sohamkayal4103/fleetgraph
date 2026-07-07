"""MCP integration diagnostics.

Stage 6 authenticity checkpoint: these helpers report whether Aithernet is genuinely
configured to reach a real external MCP server (the intended target being the ready-made
``yoelbassin/gr-mcp`` GNU Radio MCP server), and — only when explicitly asked to ``probe``
— actually launch it and run ``tools/list`` through the real MCP client path.

This module never fakes GNU Radio behavior, never invents tool names, and never treats an
unconfigured server as success. Config-level diagnostics do not launch any subprocess; a
probe runs the real :class:`~aithernet.mcp.runtime.MCPRuntime` path. The report excludes
the subprocess ``env`` map, so secrets are never exposed.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from aithernet.config.settings import MCPServerConfig
from aithernet.mcp.clients.stdio import resolve_command
from aithernet.mcp.contracts import MCPError
from aithernet.mcp.runtime import MCPRuntime

#: The intended/default external GNU Radio MCP server (external; never vendored here).
GR_MCP_URL = "https://github.com/yoelbassin/gr-mcp"


class MCPProbeResult(BaseModel):
    """Outcome of (optionally) launching the server and running ``tools/list``."""

    attempted: bool = False
    succeeded: bool | None = None
    tool_count: int | None = None
    tool_names: list[str] | None = None
    error_type: str | None = None
    error: str | None = None


class MCPDiagnostics(BaseModel):
    """A secret-free diagnostic report for the configured MCP server connection."""

    provider: str
    configured: bool
    command: str | None = None
    command_resolves: bool = False
    resolved_command_path: str | None = None
    args_unresolved_count: int = 0
    missing_configuration: list[str] = Field(default_factory=list)
    cwd: str | None = None
    cwd_exists: bool | None = None
    timeout_seconds: float
    # beta.9 Defect 8: managed RF-MCP component facts so diagnostics describe the installed,
    # signed component a customer actually has — never a developer gr-mcp checkout.
    managed_component: bool = False
    managed_installed: bool = False
    managed_version: str | None = None
    managed_signature_valid: bool | None = None
    probe: MCPProbeResult = Field(default_factory=MCPProbeResult)
    hints: list[str] = Field(default_factory=list)


def _managed_rf_mcp_info(config: MCPServerConfig) -> dict:
    """Facts about the managed, signed RF-MCP component (never a gr-mcp checkout).

    ``managed`` is true when the configured launch IS the installed managed component (command +
    cwd match what ``installed_mcp_launch`` resolves). Signature state is read from the component
    verification; everything is best-effort and never raises."""
    info = {"managed": False, "installed": False, "version": None, "signature_valid": None}
    try:
        from aithernet.components import component_status, installed_mcp_launch, verify_component
        st = component_status("rf-mcp")
        info["installed"] = bool(st.installed)
        info["version"] = st.version
        launch = installed_mcp_launch("rf-mcp")
        info["managed"] = bool(
            launch and config.command == launch.command and config.cwd == launch.cwd
        )
        if st.installed:
            try:
                checks = verify_component("rf-mcp").get("checks", {})
                info["signature_valid"] = checks.get("signature_valid")
            except Exception:  # noqa: BLE001 — verification is best-effort in diagnostics
                pass
    except Exception:  # noqa: BLE001 — component layer absent/uninstalled is a normal state
        pass
    return info


async def _run_probe(mcp_runtime: MCPRuntime) -> MCPProbeResult:
    """Actually attempt ``tools/list`` through the real MCP client (may launch a server)."""
    try:
        tools = await mcp_runtime.list_tools()
    except MCPError as exc:
        return MCPProbeResult(
            attempted=True,
            succeeded=False,
            error_type=type(exc).__name__,
            error=str(exc),
        )
    return MCPProbeResult(
        attempted=True,
        succeeded=True,
        tool_count=len(tools),
        tool_names=[tool.name for tool in tools],
    )


def _build_hints(diagnostics: MCPDiagnostics, config: MCPServerConfig) -> list[str]:
    """Build actionable hints for the customer. When the managed, signed RF-MCP component is
    installed and wired, hints describe THAT component and its Aithernet repair action — never a
    developer gr-mcp checkout or a git clone (beta.9 Defect 8)."""
    hints: list[str] = []

    # -- managed RF-MCP component path (the customer install) --------------------------------
    if diagnostics.managed_component:
        sig = diagnostics.managed_signature_valid
        sig_word = ("signature verified" if sig else
                    "SIGNATURE INVALID — run `aithernet components repair rf-mcp`"
                    if sig is False else "signature not re-checked")
        hints.append(
            f"Managed RF-MCP component rf-mcp {diagnostics.managed_version or '?'} "
            f"({sig_word}); launched as: {diagnostics.resolved_command_path or config.command}."
        )
        if diagnostics.managed_signature_valid is False:
            hints.append("Repair the managed component with: aithernet components repair rf-mcp")
        if diagnostics.configured and not diagnostics.probe.attempted:
            hints.append("Run `aithernet mcp diagnostics --probe` to launch it and list its tools.")
        if diagnostics.probe.attempted and diagnostics.probe.succeeded:
            hints.append("Connected. `aithernet mcp tools` lists the tools it exposes "
                         "(discovered at runtime, never assumed).")
        if diagnostics.probe.attempted and diagnostics.probe.succeeded is False:
            hints.append("The probe failed. Re-verify the component: "
                         "aithernet components verify rf-mcp  (then re-run the probe).")
        return hints

    # -- managed component installed but NOT wired, or not installed -------------------------
    if diagnostics.managed_installed and not config.command:
        hints.append(
            f"The managed RF-MCP component (rf-mcp {diagnostics.managed_version or '?'}) is "
            "installed but not wired. It auto-wires on node start; restart the service "
            "(`aithernet service restart`) or run `aithernet doctor --repair`."
        )
        return hints
    if not config.command:
        hints.append(
            "No GNU Radio MCP component is configured. Install the managed, signed component: "
            "aithernet components install rf-mcp"
        )
    elif not diagnostics.command_resolves:
        hints.append(
            f"Command '{config.command}' was not found on PATH. Reinstall the managed component: "
            "aithernet components install rf-mcp  (or `aithernet doctor --repair`)."
        )
    if diagnostics.args_unresolved_count:
        hints.append(
            f"{diagnostics.args_unresolved_count} launch argument(s) are unresolved. The managed "
            "component supplies these automatically: aithernet components install rf-mcp"
        )
    if config.cwd is not None and diagnostics.cwd_exists is False:
        hints.append(f"Working directory '{config.cwd}' does not exist; create it or unset cwd.")
    if diagnostics.configured and not diagnostics.probe.attempted:
        hints.append("Run `aithernet mcp diagnostics --probe` to launch the server and list tools.")
    if diagnostics.probe.attempted and diagnostics.probe.succeeded:
        hints.append("Connected. Use `aithernet mcp tools` to see the tools it exposes.")
    if diagnostics.probe.attempted and diagnostics.probe.succeeded is False:
        hints.append("The probe failed. Verify the managed component: "
                     "aithernet components verify rf-mcp")
    return hints


async def build_diagnostics(mcp_runtime: MCPRuntime, *, probe: bool = False) -> MCPDiagnostics:
    """Build an MCP diagnostic report.

    When ``probe`` is ``False`` (the default), only configuration/readiness is inspected
    and no server is launched. When ``probe`` is ``True``, the real MCP client path is
    exercised (``tools/list``), capturing success (tool count/names) or a structured
    failure (error type/message).
    """
    config = mcp_runtime.config
    status = mcp_runtime.status()  # secret-free: provider/configured/command/missing
    resolved = resolve_command(config.command)
    cwd_exists = Path(config.cwd).is_dir() if config.cwd is not None else None
    probe_result = await _run_probe(mcp_runtime) if probe else MCPProbeResult(attempted=False)
    managed = _managed_rf_mcp_info(config)

    diagnostics = MCPDiagnostics(
        provider=status.provider,
        configured=status.configured,
        command=status.command,
        command_resolves=resolved is not None,
        resolved_command_path=resolved,
        args_unresolved_count=sum(1 for arg in config.args if arg is None),
        missing_configuration=status.missing_configuration,
        cwd=config.cwd,
        cwd_exists=cwd_exists,
        timeout_seconds=config.timeout_seconds,
        managed_component=managed["managed"],
        managed_installed=managed["installed"],
        managed_version=managed["version"],
        managed_signature_valid=managed["signature_valid"],
        probe=probe_result,
    )
    diagnostics.hints = _build_hints(diagnostics, config)
    return diagnostics
