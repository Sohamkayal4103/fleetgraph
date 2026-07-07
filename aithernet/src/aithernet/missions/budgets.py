"""Infrastructure resource budgets for autonomous mission runs (Stage 12).

Runaway-prevention limits only — NOT autonomy modes. Usage is tracked on the run row and
checked before each coordinator invocation and each route execution.
"""

from __future__ import annotations

from aithernet.config.settings import MissionBudgets
from aithernet.state.models import MissionExecutionRun

#: route target -> (budget-limit key, run usage-counter attribute).
ROUTE_BUDGET: dict[str, tuple[str, str]] = {
    "node_state": ("max_node_state_actions", "node_state_action_count"),
    "coding_agent": ("max_coding_agent_actions", "coding_agent_action_count"),
    "gnuradio_mcp": ("max_mcp_actions", "mcp_action_count"),
    "respond": ("max_response_actions", "response_action_count"),
    # Stage 14B: one MissionStep per model-selected hardware-lease action (renewal/polling are
    # infrastructure and never routed, so they never increment this counter).
    "rf_device": ("max_rf_device_actions", "rf_device_action_count"),
}

#: Structured reason persisted when a budget halts the mission.
REASON_BUDGET_EXHAUSTED = "resource_budget_exhausted"

#: Structured reason persisted when a fallback build cannot plausibly complete with the budget left.
REASON_FALLBACK_BUDGET_INSUFFICIENT = "fallback_budget_insufficient"

#: beta.5: the minimum gnuradio_mcp actions a software-only flowgraph fallback needs to be viable —
#: at least two blocks (a source + a sink), one connection, one validate, and one execute. Starting
#: a fallback build with fewer remaining actions than this only spends the remainder on doomed
#: partial work (the exact beta.4 failure: "remaining gnuradio_mcp budget was insufficient for
#: remaining connections and execution"), so we block early with an actionable reason instead.
MIN_FLOWGRAPH_MCP_ACTIONS = 5


def estimate_flowgraph_mcp_actions(*, blocks: int = 2, connections: int = 1,
                                   validate: int = 1, execute: int = 1) -> int:
    """A conservative lower bound on the gnuradio_mcp actions to build+validate+execute a flowgraph.

    Counts one action per block creation, per connection, plus a validation and an execution. Used
    to decide, BEFORE falling back to gnuradio_mcp, whether the remaining budget can finish the job.
    """
    return max(1, blocks + connections + validate + execute)


def fallback_budget_block_reason(
    run: MissionExecutionRun, budgets: dict, *, route_target: str = "gnuradio_mcp",
    minimum: int = MIN_FLOWGRAPH_MCP_ACTIONS,
) -> str | None:
    """Return a reason to BLOCK a fallback that cannot plausibly complete, else None.

    Only fires at the START of the route's work (no actions of this route consumed yet): if fewer
    than ``minimum`` actions remain, a flowgraph build+validate+execute cannot finish, so we stop
    before spending the remainder on doomed partial work. Mid-build we never block here (the coord-
    inator is making real progress and the hard per-route gate still applies)."""
    mapping = ROUTE_BUDGET.get(route_target)
    if mapping is None:
        return None
    limit_key, count_attr = mapping
    limit = budgets.get(limit_key, 0)
    used = getattr(run, count_attr, 0)
    remaining_actions = max(0, limit - used)
    if used == 0 and 0 < remaining_actions < minimum:
        return (f"{REASON_FALLBACK_BUDGET_INSUFFICIENT}: a {route_target} fallback needs about "
                f"{minimum} actions to build, validate and execute a flowgraph but only "
                f"{remaining_actions} remain — retry with a fresh budget "
                f"(`aithernet mission retry <id>`) or complete this via the coding agent.")
    return None


def resolve_budgets(defaults: MissionBudgets, overrides: dict | None) -> dict:
    """Resolve effective budgets: per-run overrides clamped to the configured maximums.

    A per-run override may only *lower* a limit (it is clamped to the default maximum), so
    a run can never request more than the node's configured ceiling.
    """
    base = defaults.model_dump()
    if not overrides:
        return base
    resolved = dict(base)
    for key, value in overrides.items():
        if key not in base:
            continue
        try:
            num = type(base[key])(value)
        except (TypeError, ValueError):
            continue
        if num <= 0:
            continue
        resolved[key] = min(num, base[key])
    return resolved


def iteration_block_reason(run: MissionExecutionRun, budgets: dict) -> str | None:
    """Return a structured exhaustion reason to BLOCK before the next iteration, or None."""
    if run.iteration_count >= budgets.get("max_iterations", 0):
        return f"{REASON_BUDGET_EXHAUSTED}: max_iterations ({budgets['max_iterations']}) reached"
    if run.coordinator_call_count >= budgets.get("max_coordinator_calls", 0):
        return (
            f"{REASON_BUDGET_EXHAUSTED}: max_coordinator_calls "
            f"({budgets['max_coordinator_calls']}) reached"
        )
    max_elapsed = budgets.get("max_elapsed_seconds")
    if max_elapsed is not None and run.elapsed_seconds >= max_elapsed:
        return f"{REASON_BUDGET_EXHAUSTED}: max_elapsed_seconds ({max_elapsed}) reached"
    if run.consecutive_failures >= budgets.get("max_consecutive_failures", 1):
        return (
            f"{REASON_BUDGET_EXHAUSTED}: max_consecutive_failures "
            f"({budgets['max_consecutive_failures']}) reached"
        )
    return None


def route_block_reason(run: MissionExecutionRun, budgets: dict, route_target: str) -> str | None:
    """Return a reason to BLOCK if executing ``route_target`` would exceed its budget."""
    mapping = ROUTE_BUDGET.get(route_target)
    if mapping is None:
        return None
    limit_key, count_attr = mapping
    limit = budgets.get(limit_key, 0)
    used = getattr(run, count_attr, 0)
    if used >= limit:
        return f"{REASON_BUDGET_EXHAUSTED}: {limit_key} ({limit}) reached"
    return None


def remaining(run: MissionExecutionRun, budgets: dict) -> dict:
    """Compute non-negative remaining counts for each budget (for context + UI)."""
    out = {
        "iterations": max(0, budgets.get("max_iterations", 0) - run.iteration_count),
        "coordinator_calls": max(
            0, budgets.get("max_coordinator_calls", 0) - run.coordinator_call_count
        ),
        "node_state_actions": max(
            0, budgets.get("max_node_state_actions", 0) - run.node_state_action_count
        ),
        "coding_agent_actions": max(
            0, budgets.get("max_coding_agent_actions", 0) - run.coding_agent_action_count
        ),
        "mcp_actions": max(0, budgets.get("max_mcp_actions", 0) - run.mcp_action_count),
        "response_actions": max(
            0, budgets.get("max_response_actions", 0) - run.response_action_count
        ),
        "rf_device_actions": max(
            0, budgets.get("max_rf_device_actions", 0)
            - getattr(run, "rf_device_action_count", 0)
        ),
        "consecutive_failures_remaining": max(
            0, budgets.get("max_consecutive_failures", 1) - run.consecutive_failures
        ),
    }
    max_elapsed = budgets.get("max_elapsed_seconds")
    if max_elapsed is not None:
        out["elapsed_seconds_remaining"] = max(0.0, max_elapsed - run.elapsed_seconds)
    return out
