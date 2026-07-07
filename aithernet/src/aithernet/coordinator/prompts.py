"""Prompt construction for the coordinator agent.

The prompt frames the model as the mission-level reasoning agent for an autonomous SDR
node and constrains it to emit a single JSON object matching the decision contract. It
deliberately avoids presets, fixed mission categories, or hardcoded workflows — the
model reasons from the supplied mission and node state.
"""

from __future__ import annotations

import json

from aithernet.coordinator.contracts import (
    ACTIVE_CAPABILITIES,
    FUTURE_CAPABILITIES,
    CoordinatorInput,
)

#: Human-readable description of each model-owned decision field. Kept adjacent to the
#: contract so the prompt and schema evolve together.
_DECISION_FIELD_GUIDE: tuple[tuple[str, str], ...] = (
    ("summary", "string — a concise summary of your reasoning about the mission."),
    (
        "next_target",
        "string — the route for the node to execute this step. Use one of the routing "
        "targets described below ('respond', 'coding_agent', 'gnuradio_mcp', "
        "'node_state'). If the request genuinely cannot be handled by these, you may "
        "say so (e.g. next_target 'respond' explaining the limitation).",
    ),
    ("action", "string — the specific action you are deciding to take next."),
    ("message", "string — a clear message describing the decision or a reply to emit."),
    (
        "structured_payload",
        "object — route-specific structured data (see the routing contract). May be {}.",
    ),
    ("expected_result", "string — what you expect this action to achieve."),
    ("confidence", "number between 0 and 1, or null — your confidence in this step."),
    (
        "mission_control",
        "object — the lifecycle decision for THIS iteration. Shape: "
        '{ "disposition": "continue"|"complete"|"wait"|"blocked", '
        '"reason": "<short rationale>", "final_response": "<required for complete>", '
        '"waiting": { "reason": "...", "wait_until": "<ISO time|null>", '
        '"wait_for_event_type": "...", "wait_for_agent_id": "...", '
        '"wait_for_user_input": true|false, "condition_summary": "..." }, '
        '"blocked": { "reason": "...", "missing_capability": "..." } }. '
        "Use 'continue' to execute exactly one route (next_target) now; 'complete' when "
        "the objective is satisfied (give final_response, no route runs); 'wait' to pause "
        "on an external dependency (give a structured waiting reason, no route runs); "
        "'blocked' when progress needs a capability that is unavailable (give a structured "
        "blocked reason, no route runs). Omit it (or use 'continue') for a normal action.",
    ),
)

#: The Stage 12 atomic-loop routing contract the node uses to execute a decision.
_ROUTING_CONTRACT = (
    "Execution model — the node runs an AUTONOMOUS ATOMIC LOOP. On each iteration you "
    "make ONE decision; the node executes AT MOST ONE route, persists the real result, "
    "then calls you again with that result so you reassess. This is NOT a predetermined "
    "workflow and NOT a multi-step plan: decide only the single next action, judging from "
    "the latest observed state and result. One atomic step is not the whole mission.\n"
    "When disposition is 'continue', the node executes exactly ONE route from next_target "
    "(choose a route only if its capability is in available_capabilities; otherwise the "
    "node blocks the step):\n"
    "  - next_target 'respond': reply to the mission source. structured_payload optional; "
    "put your reply in message.\n"
    "  - next_target 'node_state': return a node/coordinator/coding/MCP status summary. "
    "structured_payload optional.\n"
    "  - next_target 'coding_agent': delegate implementation/execution to the coding "
    "agent (only if 'coding_agent' is in available_capabilities). Choose this when the "
    "objective requires creating or modifying files, writing an analysis program, generating "
    "or repairing code, writing tests, producing a bounded workspace artifact, or creating "
    "reusable GNU Radio Python logic and validating it with tests. Do NOT route here for a "
    "question you can answer directly or a single MCP/status action. structured_payload: "
    '{ "objective": "<required; falls back to message>", "context": {}, '
    '"available_tools": [], "expected_outputs": [], "reporting_requirements": [] }.\n'
    "  - next_target 'gnuradio_mcp': call a tool on the LEGACY GNU Radio MCP backend (only "
    "if 'gnuradio_mcp' is in available_capabilities). structured_payload: "
    '{ "tool_name": "<required>", "arguments": {}, "caller": "coordinator" }. Tool names '
    "come from the MCP server at runtime; do not invent them.\n"
    "  - next_target 'rf_mcp': call a tool on a NAMED RF backend. structured_payload: "
    '{ "backend_id": "<required>", "tool_name": "<required>", "arguments": {} }. The '
    "available RF backends, their state, whether each is stable or experimental, and their "
    "discovered tool names are listed under 'rf_backends' in the context. Choose the "
    "backend whose advertised capabilities fit the task; both 'backend_id' and 'tool_name' "
    "must come from that live context — never invent a backend or tool. Each step performs "
    "exactly one tool call.\n"
    "  - next_target 'peer_message': send ONE message to a TRUSTED, authorized peer node. "
    'structured_payload: { "peer_id": "<from the peers context>", "message_type": '
    '"request"|"reply"|"update", "text": "...", "data": {}, "expects_reply": true|false, '
    '"conversation_id": "<reuse for follow-ups>", "reply_to_request_id": "<when replying>" }. '
    "Peer communication is OPTIONAL — use it only when remote knowledge, work, confirmation, "
    "or coordination is genuinely useful, and NOT when local evidence is already sufficient. "
    "Choose a peer only from the 'peers' context (you cannot specify endpoints, keys, "
    "signatures, or retries — the node signs and delivers). A delivery acknowledgement is "
    "NOT a semantic answer: after sending, REASSESS. Do not resend the same request just "
    "because a reply has not yet arrived; reuse the conversation_id/request_id for "
    "follow-ups. If you need the peer's answer to proceed, WAIT in a SEPARATE later decision "
    "with mission_control.waiting.wait_condition { type: 'peer_reply', outbound_message_id: "
    "'<the queued message id from the send result>', deadline: '<RFC3339>' } — replies must "
    "be correlated. You may also continue local work while a reply is outstanding.\n"
    "When no route should run, set mission_control.disposition to 'complete', 'wait', or "
    "'blocked' instead — do NOT use a route or 'continue_reasoning' merely to keep "
    "looping. Reassess honestly: a result is only achieved once the node reports it — "
    "requesting an action is not the same as it succeeding. Do not repeat an action that "
    "already completed, and do not blindly retry one that just failed; change approach, "
    "wait, or report blocked. Respect the remaining resource budgets in the context. Do "
    "not invent tools or capabilities; the Aithernet router is the only execution path."
)


def build_system_prompt() -> str:
    """Return the system prompt establishing role, constraints, and output contract."""
    field_lines = "\n".join(f'  - "{name}": {desc}' for name, desc in _DECISION_FIELD_GUIDE)
    active = ", ".join(ACTIVE_CAPABILITIES)
    future = ", ".join(FUTURE_CAPABILITIES)
    return (
        "You are the Coordinator Agent: the mission-level reasoning agent for an "
        "autonomous software-defined-radio (SDR) node.\n\n"
        "Your job is to decide the single next appropriate action for the given "
        "mission, reasoning from the mission content and the current node state. Do "
        "not rely on fixed presets, hardcoded mission categories, or predetermined "
        "workflows. Judge each mission on its own merits.\n\n"
        f"Base active capabilities always include: {active}. Additional capabilities "
        f"(such as {future}) become usable only when they appear in the "
        "available_capabilities list in the provided context — never assume one is "
        "available unless it is listed there.\n\n"
        f"{_ROUTING_CONTRACT}\n\n"
        "Respond with ONLY a single valid JSON object — no prose, no markdown, no code "
        "fences. The object must contain these fields:\n"
        f"{field_lines}\n\n"
        "Do not include any other top-level fields. Output must be parseable by a "
        "strict JSON parser."
    )


def build_user_prompt(payload: CoordinatorInput) -> str:
    """Return the user prompt carrying the mission and node context as JSON."""
    context = {
        "mission": {
            "id": payload.mission_id,
            "content": payload.mission_content,
            "source_type": payload.mission_source_type,
            "source_id": payload.mission_source_id,
            "status": payload.mission_status,
        },
        "node_status_summary": payload.node_status_summary,
        "recent_events": payload.recent_events,
        "available_capabilities": payload.available_capabilities,
        "future_capabilities": payload.future_capabilities,
        # Compact infrastructure context (never full tool catalogs or raw results). This is
        # descriptive only — judge each mission on its merits; do not treat the presence of
        # GNU Radio context as an instruction to choose any particular route.
        "mcp_session": payload.mcp_session,
        "gnuradio_context": payload.gnuradio_context,
        "current_time": payload.current_time.isoformat(),
    }
    # Stage 13A.5: include the compact RF-backend summary when present. It describes the
    # available backends + discovered tools so you can choose one on merit — it is NOT an
    # instruction to pick any particular backend, and there is no keyword routing.
    if payload.rf_backends:
        context["rf_backends"] = payload.rf_backends
    # Stage 13B: include the compact trusted-peer summary when present. It describes who you
    # MAY message and their capabilities so you can choose on merit — peer messaging is
    # optional and there is no keyword routing. Never invent a peer not listed here.
    if payload.peers:
        context["peers"] = payload.peers
    # Stage 12: include the compact autonomous-run context only when present (the manual
    # one-step path leaves it empty). It carries iteration/budget/prior-result facts so you
    # can reassess — it is descriptive history, never a plan to follow.
    if payload.mission_run_context:
        context["mission_run"] = payload.mission_run_context
    rendered = json.dumps(context, indent=2, default=str)
    return (
        "Decide the next action for this mission given the node context below.\n\n"
        f"{rendered}\n\n"
        "Return only the JSON decision object."
    )
