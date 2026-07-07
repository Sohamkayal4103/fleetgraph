"""beta.5: mission-engine reliability patches discovered in beta.4 testing.

* Fallback budget protection: do not START a gnuradio_mcp flowgraph build that cannot finish.
* MCP schema corrections do not consume the scarce gnuradio_mcp action budget.
* A coding-agent bubblewrap sandbox failure blocks early with actionable remediation.
* The quota block now offers the local CatGPT Gateway as a concrete alternate coordinator.
"""

from __future__ import annotations

import asyncio
import types

from aithernet.coordinator.contracts import CoordinatorProviderError
from aithernet.missions import budgets as bud
from aithernet.missions.engine import IterationOutcome, MissionEngine, _is_coding_sandbox_failure


def _run(**counters):
    base = {"mcp_action_count": 0, "coding_agent_action_count": 0, "consecutive_failures": 0}
    base.update(counters)
    return types.SimpleNamespace(**base)


# -- fallback budget protection ---------------------------------------------------------------

def test_estimate_flowgraph_actions_is_a_sane_minimum():
    assert bud.estimate_flowgraph_mcp_actions() == 5           # 2 blocks + 1 conn + validate + exec
    assert bud.MIN_FLOWGRAPH_MCP_ACTIONS == 5


def test_fallback_blocks_when_budget_too_small_to_finish():
    run = _run(mcp_action_count=0)
    reason = bud.fallback_budget_block_reason(run, {"max_mcp_actions": 3})
    assert reason is not None
    assert reason.startswith(bud.REASON_FALLBACK_BUDGET_INSUFFICIENT)
    assert "retry" in reason.lower()


def test_fallback_allowed_when_budget_sufficient():
    run = _run(mcp_action_count=0)
    assert bud.fallback_budget_block_reason(run, {"max_mcp_actions": 15}) is None


def test_fallback_not_triggered_mid_build():
    # Once MCP work has started, we never block here (the hard per-route gate still applies).
    run = _run(mcp_action_count=2)
    assert bud.fallback_budget_block_reason(run, {"max_mcp_actions": 3}) is None


# -- MCP schema correction does not burn the action budget ------------------------------------

def _after_route_engine():
    fake = types.SimpleNamespace()
    fake.run = _run(mcp_action_count=4, consecutive_failures=1)
    fake.updates = []
    fake._get_run = lambda rid: fake.run
    fake._update_run = lambda rid, **k: fake.updates.append(k)
    fake._after_route = types.MethodType(MissionEngine._after_route, fake)
    return fake


def test_schema_correction_does_not_consume_mcp_action_or_streak():
    fake = _after_route_engine()
    asyncio.run(fake._after_route("r", "m", "gnuradio_mcp", "s",
                                  succeeded=False, consume_budget=False))
    fields = fake.updates[0]
    assert "mcp_action_count" not in fields          # budget NOT consumed
    assert "consecutive_failures" not in fields       # failure streak NOT extended


def test_real_mcp_failure_does_consume_budget_and_streak():
    fake = _after_route_engine()
    asyncio.run(fake._after_route("r", "m", "gnuradio_mcp", "s",
                                  succeeded=False, consume_budget=True))
    fields = fake.updates[0]
    assert fields["mcp_action_count"] == 5            # 4 -> 5, budget consumed
    assert fields["consecutive_failures"] == 2        # streak extended


# -- coding-agent sandbox failure blocks early ------------------------------------------------

def test_is_coding_sandbox_failure_recognizes_bwrap_errors():
    assert _is_coding_sandbox_failure(
        "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted") is True
    assert _is_coding_sandbox_failure("bwrap: setting up uid map: Permission denied") is True
    assert _is_coding_sandbox_failure("codex: file not found") is False
    assert _is_coding_sandbox_failure(None) is False


def _sandbox_block_engine():
    fake = types.SimpleNamespace()
    fake.runtime = types.SimpleNamespace(
        config=object(),   # a real-looking runtime (not None)
        coding_agent=types.SimpleNamespace(config=types.SimpleNamespace(provider="codex_cli")))
    fake.persisted, fake.transitions, fake.events = [], [], []
    fake._persist_control_step = lambda *a, **k: fake.persisted.append(k)
    fake._apply_transition = lambda *a, **k: fake.transitions.append((a, k))

    async def _emit(**k):
        fake.events.append(k)

    fake.runtime._emit_event = _emit
    fake._handle_coding_sandbox_block = types.MethodType(
        MissionEngine._handle_coding_sandbox_block, fake)
    return fake


def test_sandbox_block_is_actionable_and_recoverable():
    fake = _sandbox_block_engine()
    outcome = asyncio.run(fake._handle_coding_sandbox_block(
        "run1", "m1", "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"))
    assert outcome is IterationOutcome.BLOCKED
    reason = fake.persisted[0]["reason"]
    assert "blocked_coding_sandbox" in reason
    assert "coding_sandbox_unavailable" in reason
    assert "aithernet doctor" in reason
    assert "apparmor_restrict_unprivileged_userns=0" in reason   # the exact remediation
    assert "aithernet mission retry m1" in reason
    assert fake.persisted[0]["result_extra"]["failure_category"] == "coding_sandbox_unavailable"


# -- quota block now offers CatGPT Gateway as an alternate coordinator -------------------------

def _quota_engine(fallbacks):
    fake = types.SimpleNamespace()
    fake.runtime = types.SimpleNamespace(
        config=None,   # None => the best-effort readiness write is skipped (unit-test isolation)
        coordinator=types.SimpleNamespace(
            config=types.SimpleNamespace(provider="gemini_cli", fallback_providers=fallbacks)))
    fake.persisted, fake.transitions, fake.events = [], [], []
    fake._persist_control_step = lambda *a, **k: fake.persisted.append(k)
    fake._apply_transition = lambda *a, **k: fake.transitions.append((a, k))
    fake._update_run = lambda *a, **k: None

    async def _emit(**k):
        fake.events.append(k)

    fake.runtime._emit_event = _emit
    fake._handle_quota_block = types.MethodType(MissionEngine._handle_quota_block, fake)
    return fake


def test_quota_block_offers_catgpt_gateway_when_no_fallback():
    fake = _quota_engine([])
    asyncio.run(MissionEngine._handle_coordinator_failure(
        fake, "run1", "m1", {"max_consecutive_failures": 3}, "quota exhausted",
        exc=CoordinatorProviderError("quota exhausted")))
    reason = fake.persisted[0]["reason"]
    assert "agents connect coordinator" in reason
    assert "catgpt_gateway" in reason          # concrete local alternate offered
