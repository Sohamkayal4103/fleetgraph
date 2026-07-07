#!/usr/bin/env python3
"""Validate Aithernet's connection to a real external gr-mcp / GNU Radio MCP server.

This is a Stage 6 authenticity tool. It uses the *same* config loader and ``MCPRuntime``
as the running node, so what it reports is exactly the production MCP path:

    Aithernet config  →  MCPRuntime  →  Aithernet stdio MCP client
                       →  external gr-mcp server launched by the configured command
                       →  GNU Radio

It does NOT require the Aithernet API server to be running, and it never fakes GNU Radio
behavior: tool names are discovered from the real server, an unconfigured server is
reported as such, and a failed probe/call is reported with its real error.

Usage:
    python scripts/validate_gr_mcp.py --config configs/node.yaml
    python scripts/validate_gr_mcp.py --config configs/node.yaml --list-tools
    python scripts/validate_gr_mcp.py --config configs/node.yaml --call-tool TOOL --args-json '{}'

Configure the external server first (see the README "Real gr-mcp integration" section):
    export AITHERNET_GNURADIO_MCP_COMMAND=uv
    export AITHERNET_GR_MCP_DIR=/absolute/path/to/gr-mcp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from aithernet.config.loader import DEFAULT_CONFIG_PATH, load_config
from aithernet.mcp.contracts import MCPError
from aithernet.mcp.diagnostics import MCPDiagnostics, build_diagnostics
from aithernet.mcp.runtime import MCPRuntime


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="validate_gr_mcp",
        description="Validate Aithernet's connection to an external gr-mcp/GNU Radio MCP server.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to node.yaml (default: configs/node.yaml).",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Probe: actually launch the server and run tools/list.",
    )
    parser.add_argument(
        "--call-tool",
        metavar="TOOL",
        default=None,
        help="Probe: call this tool on the server (use --args-json for its arguments).",
    )
    parser.add_argument(
        "--args-json",
        default="{}",
        help="Arguments for --call-tool, as a JSON object (default: {}).",
    )
    return parser.parse_args(argv)


def _print_diagnostics(diag: MCPDiagnostics) -> None:
    print("MCP diagnostics")
    print(f"  provider:         {diag.provider}")
    print(f"  command:          {diag.command or '(unset)'}")
    print(f"  command resolves: {'yes' if diag.command_resolves else 'no'}")
    if diag.resolved_command_path:
        print(f"  resolved path:    {diag.resolved_command_path}")
    if diag.args_unresolved_count:
        print(f"  unresolved args:  {diag.args_unresolved_count}")
    if diag.cwd is not None:
        print(f"  working dir:      {diag.cwd} (exists: {'yes' if diag.cwd_exists else 'no'})")
    print(f"  timeout:          {diag.timeout_seconds}s")
    print(f"  configured:       {'yes' if diag.configured else 'no'}")
    if diag.missing_configuration:
        print(f"  missing:          {', '.join(diag.missing_configuration)}")
    if diag.probe.attempted:
        if diag.probe.succeeded:
            names = ", ".join(diag.probe.tool_names or [])
            print(f"  probe:            ok — {diag.probe.tool_count} tool(s): {names}")
        else:
            print(f"  probe:            FAILED [{diag.probe.error_type}] {diag.probe.error}")
    else:
        print("  probe:            not run (pass --list-tools to launch the server)")
    if diag.hints:
        print("  next steps:")
        for hint in diag.hints:
            print(f"    - {hint}")


async def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    mcp = MCPRuntime.from_config(config.gnuradio_mcp)

    # A probe (real server launch) happens only when the user asks to list/call tools.
    diagnostics = await build_diagnostics(mcp, probe=args.list_tools)
    _print_diagnostics(diagnostics)

    exit_code = 0
    if not diagnostics.configured:
        exit_code = 1
    if args.list_tools and not diagnostics.probe.succeeded:
        exit_code = 1

    if args.call_tool:
        try:
            arguments = json.loads(args.args_json)
        except json.JSONDecodeError as exc:
            print(f"Invalid --args-json: {exc}", file=sys.stderr)
            return 2
        if not isinstance(arguments, dict):
            print("--args-json must be a JSON object.", file=sys.stderr)
            return 2

        print(f"\nCalling tool '{args.call_tool}'...")
        try:
            result = await mcp.call_tool(args.call_tool, arguments)
        except MCPError as exc:
            print(f"Tool call failed [{type(exc).__name__}]: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result.model_dump(), indent=2, default=str))
        if result.status != "completed":
            exit_code = 1

    return exit_code


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
