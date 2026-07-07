"""beta.10 Defect 7: the coding agent is routable for code-requiring work, but not every mission.

The coordinator (an LLM) makes the route choice live; the live coordinator->coding->GNU Radio
end-to-end mission is exercised by the source-hidden qualification suite. Here we prove the
deterministic routing plumbing + that the coordinator prompt names the exact selection triggers,
and that a non-coding decision never fabricates a coding task.
"""

from __future__ import annotations

import pytest

from aithernet.coordinator.contracts import CoordinatorDecision
from aithernet.coordinator.prompts import build_system_prompt
from aithernet.orchestrator.decision_router import (
    ROUTE_CODING_AGENT,
    ROUTE_RESPOND,
    RouteValidationError,
    coding_task_payload_from_decision,
    normalize_target,
)


def test_coding_aliases_route_to_coding_agent():
    for alias in ("coding_agent", "future_coding_agent", "Coding_Agent", " coding_agent "):
        assert normalize_target(alias) == ROUTE_CODING_AGENT


def test_non_coding_targets_do_not_route_to_coding():
    for alias in ("respond", "user", "node_state", "gnuradio_mcp", "rf_mcp"):
        assert normalize_target(alias) != ROUTE_CODING_AGENT
    assert normalize_target("respond") == ROUTE_RESPOND


def _decision(**over):
    base = dict(mission_id="m1", summary="s", next_target="coding_agent", action="delegate",
                message="do it", expected_result="done")
    base.update(over)
    return CoordinatorDecision(**base)


def test_coding_decision_builds_task_with_outputs_and_tests():
    decision = _decision(
        message="Implement a throttle helper",
        structured_payload={
            "objective": "Create channel_power.py computing average power, with pytest tests",
            "expected_outputs": ["channel_power.py", "test_channel_power.py"],
            "reporting_requirements": ["test results"],
        },
    )
    task = coding_task_payload_from_decision(decision, mission_id="m1")
    assert "channel_power.py" in task.objective
    assert "test_channel_power.py" in task.expected_outputs


def test_respond_decision_never_creates_a_coding_task():
    # A direct-answer decision must not be coerced into a coding task.
    decision = _decision(next_target="respond", action="respond", message="The answer is 42.")
    assert normalize_target(decision.next_target) != ROUTE_CODING_AGENT


def test_coding_route_requires_an_objective():
    decision = _decision(message="")
    with pytest.raises(RouteValidationError):
        coding_task_payload_from_decision(decision, mission_id="m1")


def test_prompt_names_the_coding_selection_triggers():
    prompt = build_system_prompt().lower()
    # The mandate's code-requiring triggers must be described so the coordinator selects coding.
    for trigger in ("creating or modifying files", "writing an analysis program",
                    "writing tests", "repairing code", "gnu radio python"):
        assert trigger in prompt, f"prompt missing coding trigger: {trigger!r}"
    # And it must say NOT to route to coding for a directly-answerable question.
    assert "answer directly" in prompt
