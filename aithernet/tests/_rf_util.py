"""Shared helpers for Stage 13A.5 RF-backend tests (fake MCP subprocesses; no real backends).

Provides two fake stdio MCP servers — a GNU Radio-like legacy server and a Marconi-like
server (FastMCP ``structuredContent`` result shape) — plus a builder for a NodeRuntime with a
multi-backend ``rf_backends`` config. No real gr-mcp/Marconi/GNU Radio/Gemini/Codex/network.
"""

from __future__ import annotations

import sys
from pathlib import Path

from aithernet.config.settings import (
    MCPServerConfig,
    MCPSessionConfig,
    NodeConfig,
    RFBackendConfig,
    RFBackendsConfig,
)
from aithernet.orchestrator.runtime import NodeRuntime

# A GNU Radio-like legacy stdio server: get_blocks/get_connections/validate_flowgraph/
# get_all_errors/get_all_available_blocks/make_block + a crash tool.
_FAKE_LEGACY = r'''
import sys, json, os
def send(o): sys.stdout.write(json.dumps(o)+"\n"); sys.stdout.flush()
def text(mid, payload, is_error=False):
    send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps(payload)}],"isError":is_error}})
TOOLS=[{"name":n,"description":n,"inputSchema":{"type":"object","properties":{}}} for n in
 ["get_blocks","get_connections","validate_flowgraph","get_all_errors","get_all_available_blocks","make_block","crash"]]
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    m=json.loads(line); meth=m.get("method"); mid=m.get("id")
    if meth=="initialize":
        info={"name":"GNU Radio MCP","version":"1.0-fake-legacy"}
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
              "capabilities":{"tools":{}},"serverInfo":info}})
    elif meth=="notifications/initialized": pass
    elif meth=="tools/list": send({"jsonrpc":"2.0","id":mid,"result":{"tools":TOOLS}})
    elif meth=="tools/call":
        p=m.get("params") or {}; name=p.get("name"); a=p.get("arguments") or {}
        if name=="crash": os._exit(7)
        elif name in ("get_blocks","get_connections"): text(mid, a.get("items", []))
        elif name=="validate_flowgraph": text(mid, {"valid": True})
        elif name=="get_all_errors": text(mid, [])
        elif name=="get_all_available_blocks": text(mid, ["blk_a","blk_b"])
        elif name=="make_block": text(mid, {"ok": True})
        else: text(mid, {}, is_error=True)
    elif meth=="exit": break
'''

# A Marconi-like stdio server with FastMCP structuredContent results + a crash tool.
_FAKE_MARCONI = r'''
import sys, json, os
WS=os.environ.get("MARCONI_WORKSPACE",".")
def send(o): sys.stdout.write(json.dumps(o)+"\n"); sys.stdout.flush()
def wrap(mid, payload, is_error=False):
    send({"jsonrpc":"2.0","id":mid,"result":{"content":[],
          "structuredContent":{"result":payload},"isError":is_error}})
def _schema(n):
    props={"device_id":{"type":"string"}} if n=="capture" else {}
    req=["device_id"] if n=="capture" else []
    return {"type":"object","properties":props,"required":req}
TOOLS=[{"name":n,"description":n,"inputSchema":_schema(n)} for n in
 ["list_devices","list_runs","list_blocks","capture","psd_plot","run_pipeline","crash"]]
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    m=json.loads(line); meth=m.get("method"); mid=m.get("id")
    if meth=="initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"marconi","version":"9.9.9-fake"}}})
    elif meth=="notifications/initialized": pass
    elif meth=="tools/list": send({"jsonrpc":"2.0","id":mid,"result":{"tools":TOOLS}})
    elif meth=="tools/call":
        p=m.get("params") or {}; name=p.get("name"); a=p.get("arguments") or {}
        if name=="crash": os._exit(7)
        elif name=="list_devices": wrap(mid, [{"id":"sim0","kind":"sim"}])
        elif name=="list_runs": wrap(mid, [])
        elif name=="list_blocks": wrap(mid, ["tone","noise"])
        elif name=="capture":
            os.makedirs(os.path.join(WS,"captures"),exist_ok=True)
            open(os.path.join(WS,"captures","c1.cf32"),"wb").write(b"\x00"*128)
            wrap(mid, {"path":"captures/c1.cf32"})
        elif name=="psd_plot":
            os.makedirs(os.path.join(WS,"plots"),exist_ok=True)
            open(os.path.join(WS,"plots","p.png"),"wb").write(b"PNG"*4)
            wrap(mid, {"plot_path":"plots/p.png","escape":"/etc/passwd"})
        elif name=="run_pipeline": wrap(mid, {"run_id":"r1"})
        else: wrap(mid, {}, is_error=True)
    elif meth=="exit": break
'''


def write_fake_legacy(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "legacy_server.py"
    path.write_text(_FAKE_LEGACY)
    return path


def write_fake_marconi(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "marconi_server.py"
    path.write_text(_FAKE_MARCONI)
    return path


def make_rf_runtime(
    tmp_path: Path,
    *,
    legacy: bool = False,
    marconi: bool = True,
    marconi_autostart: bool = False,
    marconi_enabled: bool = True,
    marconi_command: str | None = "__default__",
    default_backend: str = "legacy_gr_mcp",
    node_id: str = "rf-node",
) -> NodeRuntime:
    """Build a NodeRuntime with fake legacy + Marconi backends (no autostart unless asked)."""
    base = tmp_path / node_id
    base.mkdir(parents=True, exist_ok=True)
    # Legacy backend via gnuradio_mcp config (the registry reuses runtime.mcp.session).
    if legacy:
        legacy_script = write_fake_legacy(base / "legacy")
        gnuradio_mcp = MCPServerConfig(
            provider="stdio", command=sys.executable, args=[str(legacy_script)],
            cwd=str(base / "legacy"), session=MCPSessionConfig(autostart=False),
        )
    else:
        gnuradio_mcp = MCPServerConfig()  # unconfigured -> legacy disabled

    backends: dict[str, RFBackendConfig] = {}
    if marconi:
        ws = base / "marconi_ws"
        ws.mkdir(parents=True, exist_ok=True)
        script = write_fake_marconi(base / "marconi")
        command = sys.executable if marconi_command == "__default__" else marconi_command
        backends["marconi"] = RFBackendConfig(
            display_name="Marconi", experimental=True, enabled=marconi_enabled,
            autostart=marconi_autostart, command=command, args=[str(script)],
            cwd=str(base / "marconi"), workspace=str(ws),
            environment={"MARCONI_WORKSPACE": str(ws)}, source_revision="testrev",
        )
    config = NodeConfig(
        node_id=node_id, node_name="rf-test",
        database_url=f"sqlite:///{base / 'node.db'}",
        gnuradio_mcp=gnuradio_mcp,
        rf_backends=RFBackendsConfig(default_backend=default_backend, backends=backends),
    )
    return NodeRuntime.from_config(config)
