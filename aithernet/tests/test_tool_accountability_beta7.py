"""beta.7 — tool-accountability fields (FIX 7) and MCP/GNU-Radio requirement parsing (FIX 8)."""
from __future__ import annotations

from aithernet.missions.requirements import parse_tool_requirements


# ── FIX 8: deterministic requirement parsing ─────────────────────────────────
def test_optional_mcp_language_is_not_required():
    r = parse_tool_requirements("Build a bounded simulation; use RF-MCP where useful.")
    assert r["mentions_mcp"] is True
    assert r["mcp_required"] is False  # "where useful" keeps it optional


def test_mandatory_mcp_before_implementation_is_required():
    r = parse_tool_requirements(
        "You must use MCP before implementation to validate the flowgraph.")
    assert r["mcp_required"] is True
    assert r["mcp_before_implementation"] is True


def test_no_silent_fallback_makes_mcp_required():
    r = parse_tool_requirements(
        "Validate with GNU Radio MCP and do not silently fallback to a Python simulation.")
    assert r["mcp_required"] is True
    assert r["no_fallback"] is True


def test_no_mcp_mention_is_not_required():
    r = parse_tool_requirements(
        "Return a concise readiness summary. Do not use GNU Radio, do not use physical RF.")
    assert r["mentions_mcp"] is False
    assert r["mcp_required"] is False  # never invent a requirement


# ── FIX 7: tool-accountability record ────────────────────────────────────────
class _Cfg:
    def __init__(self, provider):
        self.provider = provider


class _Coord:
    config = _Cfg("catgpt_gateway")


class _Coding:
    config = _Cfg("codex_cli")

    def is_configured(self):
        return True


class _Call:
    def __init__(self, name):
        self.tool_name = name


class _Runtime:
    coordinator = _Coord()
    coding_agent = _Coding()

    def __init__(self, calls):
        self._calls = calls

    def list_mcp_tool_calls(self, mission_id=None, limit=None):
        return self._calls


class _Run:
    mcp_action_count = 0


def _engine(calls):
    from aithernet.missions.engine import MissionEngine
    eng = MissionEngine.__new__(MissionEngine)  # bypass __init__ for the unit under test
    eng.runtime = _Runtime(calls)
    eng._get_run = lambda rid: _Run()  # noqa: E731 — a tiny test stub
    return eng


def test_accountability_records_mcp_usage_and_providers():
    eng = _engine([_Call("build_flowgraph"), _Call("execute_flowgraph")])
    a = eng._tool_accountability("m1", "r1")
    assert a["mcp_used"] is True and a["gnuradio_mcp_used"] is True
    assert a["mcp_tools_called"] == ["build_flowgraph", "execute_flowgraph"]
    assert a["coordinator_provider"] == "catgpt_gateway"
    assert a["coding_provider"] == "codex_cli"
    assert a["fallback_used"] is False


def test_accountability_records_no_mcp_used():
    eng = _engine([])
    a = eng._tool_accountability("m2", "r2")
    assert a["mcp_used"] is False and a["gnuradio_mcp_used"] is False
    assert a["mcp_tools_called"] == []
    assert a["gnuradio_runtime_used_by_coding"] == "unknown"  # honest: not claimed used or missing
    assert a["required_tool_missing"] is False
