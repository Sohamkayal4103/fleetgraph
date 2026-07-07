"""Bounded MCP restart: after the cap, emit ONE summarized failure and stop (no event storm)."""

from __future__ import annotations

import asyncio
import sys

from aithernet.config.settings import MCPServerConfig, MCPSessionConfig
from aithernet.mcp.session import MCPSessionManager, MCPSessionState


def test_giveup_emits_one_summary_after_cap(tmp_path):
    events: list[tuple[str, dict]] = []

    async def sink(event_type, message, payload):
        events.append((event_type, payload))

    async def body():
        broken = tmp_path / "broken.py"
        broken.write_text("import sys\nsys.exit(1)\n")  # always exits -> start always fails
        mgr = MCPSessionManager.from_config(
            MCPServerConfig(
                command=sys.executable, args=[str(broken)], timeout_seconds=10.0,
                session=MCPSessionConfig(auto_restart=True, max_restart_attempts=2,
                                         restart_initial_backoff_seconds=0.02,
                                         restart_max_backoff_seconds=0.05),
            ),
            on_event=sink,
        )
        await mgr.start()
        await asyncio.sleep(1.0)  # let the bounded recovery loop exhaust (tiny backoff)
        final = await mgr.get_status()
        assert final.state is MCPSessionState.FAILED
        assert final.restart_count <= 2
        # Exactly ONE permanent-failure summary (the give-up), not an unbounded storm.
        giveup = [p for et, p in events
                  if et == "mcp.session.failed" and p.get("max_restart_attempts") == 2]
        assert len(giveup) == 1, giveup
        await mgr.close()

    asyncio.run(body())
