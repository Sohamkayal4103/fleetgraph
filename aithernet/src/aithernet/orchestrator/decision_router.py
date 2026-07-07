"""Coordinator decision routing: normalization, validation, and payload extraction.

The coordinator *decides*; this module turns that decision into a single, well-defined
route by interpreting the decision's ``next_target``/``structured_payload`` through a
documented contract — never fragile natural-language parsing. It does NOT execute
anything: :class:`~aithernet.orchestrator.runtime.NodeRuntime` calls these pure helpers
and then dispatches to its own capability methods. Keeping execution out of this module
avoids a circular import with the runtime.

Aliases are *routing names only* — they never become mission presets or fixed workflows.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from aithernet.coordinator.contracts import CoordinatorDecision
from aithernet.schemas.coding import CodingTaskCreate
from aithernet.schemas.mcp import MCPToolCallCreate

# -- canonical route targets -----------------------------------------------------

ROUTE_RESPOND = "respond"
ROUTE_CODING_AGENT = "coding_agent"
ROUTE_MCP = "gnuradio_mcp"
ROUTE_RF_MCP = "rf_mcp"
ROUTE_RF_DEVICE = "rf_device"
ROUTE_PEER_MESSAGE = "peer_message"
ROUTE_PEER_ARTIFACT = "peer_artifact"
ROUTE_NODE_STATE = "node_state"
ROUTE_CONTINUE = "continue_reasoning"
ROUTE_UNKNOWN = "unknown"

#: Flexible coordinator strings mapped to a canonical route target. These are routing
#: names, not behaviors — the runtime decides availability and executes.
_TARGET_ALIASES: dict[str, str] = {
    "respond": ROUTE_RESPOND,
    "user": ROUTE_RESPOND,
    "mission_source": ROUTE_RESPOND,
    "coding_agent": ROUTE_CODING_AGENT,
    "future_coding_agent": ROUTE_CODING_AGENT,
    "gnuradio_mcp": ROUTE_MCP,
    "mcp": ROUTE_MCP,
    "future_gnuradio_mcp": ROUTE_MCP,
    # Stage 13A.5: the generic multi-backend RF route (backend_id is in structured_payload).
    "rf_mcp": ROUTE_RF_MCP,
    "rf": ROUTE_RF_MCP,
    # Stage 14B: atomic managed-hardware lease action (acquire/release/refresh a KNOWN device).
    "rf_device": ROUTE_RF_DEVICE,
    "hardware": ROUTE_RF_DEVICE,
    "device": ROUTE_RF_DEVICE,
    # Stage 13B: send ONE coordinator message to a trusted, authorized peer.
    "peer_message": ROUTE_PEER_MESSAGE,
    "peer": ROUTE_PEER_MESSAGE,
    # Stage 13D.2: ONE artifact-control action (offer/request/cancel) to a trusted peer.
    "peer_artifact": ROUTE_PEER_ARTIFACT,
    "artifact": ROUTE_PEER_ARTIFACT,
    "node_state": ROUTE_NODE_STATE,
    "state": ROUTE_NODE_STATE,
    "status": ROUTE_NODE_STATE,
    "continue_reasoning": ROUTE_CONTINUE,
}

#: Targets that are valid route *names* but deliberately not executable in Stage 5.
SUPPORTED_EXECUTABLE_TARGETS: tuple[str, ...] = (
    ROUTE_RESPOND,
    ROUTE_CODING_AGENT,
    ROUTE_MCP,
    ROUTE_RF_MCP,
    ROUTE_RF_DEVICE,
    ROUTE_PEER_MESSAGE,
    ROUTE_PEER_ARTIFACT,
    ROUTE_NODE_STATE,
)


def normalize_target(next_target: str | None) -> str:
    """Map a coordinator ``next_target`` to a canonical route, or ``ROUTE_UNKNOWN``."""
    key = (next_target or "").strip().lower()
    return _TARGET_ALIASES.get(key, ROUTE_UNKNOWN)


# -- routing errors --------------------------------------------------------------


class RouteValidationError(Exception):
    """Raised when a route's structured payload is missing required fields/malformed.

    The runtime records this as a ``failed`` step (the target was available, but the
    payload could not be used) with a clear message.
    """


# -- internal route result -------------------------------------------------------


class CoordinatorRouteResult(BaseModel):
    """The outcome of routing one decision, before it is persisted as a MissionStep."""

    target: str
    action: str = ""
    status: str
    result: dict = Field(default_factory=dict)
    error: str | None = None


# -- payload extraction (pure, no I/O) -------------------------------------------


def coding_task_payload_from_decision(
    decision: CoordinatorDecision, *, mission_id: str
) -> CodingTaskCreate:
    """Build a :class:`CodingTaskCreate` from a coding_agent decision's payload.

    ``objective`` comes from ``structured_payload.objective`` or falls back to the
    decision message. Raises :class:`RouteValidationError` when no objective is available
    or the payload fields are malformed.
    """
    payload = decision.structured_payload or {}
    objective = payload.get("objective") or decision.message
    if not isinstance(objective, str) or not objective.strip():
        raise RouteValidationError(
            "coding_agent route requires an objective "
            "(structured_payload.objective or a non-empty decision message)."
        )
    try:
        return CodingTaskCreate(
            mission_id=mission_id,
            objective=objective,
            context=payload.get("context", {}),
            available_tools=payload.get("available_tools", []),
            expected_outputs=payload.get("expected_outputs", []),
            reporting_requirements=payload.get("reporting_requirements", []),
        )
    except ValidationError as exc:
        raise RouteValidationError(f"Malformed coding_agent structured_payload: {exc}") from exc


def mcp_call_payload_from_decision(
    decision: CoordinatorDecision, *, mission_id: str, mission_step_id: str | None = None
) -> MCPToolCallCreate:
    """Build an :class:`MCPToolCallCreate` from a gnuradio_mcp decision's payload.

    Requires ``structured_payload.tool_name``. Raises :class:`RouteValidationError` when
    it is missing or the payload fields are malformed. The mission/step ids are attached
    from execution context, never from the coordinator model.
    """
    payload = decision.structured_payload or {}
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise RouteValidationError(
            "gnuradio_mcp route requires a non-empty structured_payload.tool_name."
        )
    try:
        return MCPToolCallCreate(
            tool_name=tool_name,
            mission_id=mission_id,
            mission_step_id=mission_step_id,
            arguments=payload.get("arguments", {}),
            caller=payload.get("caller", "coordinator"),
        )
    except ValidationError as exc:
        raise RouteValidationError(f"Malformed gnuradio_mcp structured_payload: {exc}") from exc


def rf_call_from_decision(decision: CoordinatorDecision) -> tuple[str, str, dict]:
    """Extract ``(backend_id, tool_name, arguments)`` from an ``rf_mcp`` decision payload.

    The coordinator must name BOTH the backend and the tool — selection is model-driven, never
    keyword-derived. Raises :class:`RouteValidationError` when either is missing/malformed.
    """
    payload = decision.structured_payload or {}
    backend_id = payload.get("backend_id")
    tool_name = payload.get("tool_name")
    if not isinstance(backend_id, str) or not backend_id.strip():
        raise RouteValidationError(
            "rf_mcp route requires a non-empty structured_payload.backend_id."
        )
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise RouteValidationError(
            "rf_mcp route requires a non-empty structured_payload.tool_name."
        )
    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        raise RouteValidationError("rf_mcp structured_payload.arguments must be an object.")
    return backend_id.strip(), tool_name.strip(), arguments


def rf_device_from_decision(decision: CoordinatorDecision) -> dict:
    """Extract a validated managed-hardware action from an ``rf_device`` decision (Stage 14B).

    The coordinator may name ONLY a known persisted ``device_id``, a known ``backend_id``, a
    bounded ``operation`` purpose, a direction, a lease mode, and bounded RF requirements
    (channels/frequency/sample_rate). It can NOT supply arbitrary device arguments, driver
    strings, shell fragments, filesystem paths, subprocess environment, or network endpoints —
    those keys are ignored entirely. Raises :class:`RouteValidationError` on a bad payload.
    """
    payload = decision.structured_payload or {}
    action = payload.get("action", "acquire_lease")
    if action not in ("acquire_lease", "release_lease", "refresh_inventory"):
        raise RouteValidationError(
            "rf_device action must be acquire_lease|release_lease|refresh_inventory."
        )
    if action == "refresh_inventory":
        return {"action": "refresh_inventory"}
    if action == "release_lease":
        lease_id = payload.get("lease_id")
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise RouteValidationError(
                "rf_device release_lease requires structured_payload.lease_id."
            )
        return {"action": "release_lease", "lease_id": lease_id.strip()}
    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        raise RouteValidationError("rf_device acquire_lease requires structured_payload.device_id.")
    backend_id = payload.get("backend_id")
    if not isinstance(backend_id, str) or not backend_id.strip():
        raise RouteValidationError(
            "rf_device acquire_lease requires structured_payload.backend_id."
        )
    direction = payload.get("direction", "rx")
    if direction not in ("rx", "tx", "rx_tx"):
        raise RouteValidationError("rf_device direction must be rx|tx|rx_tx.")
    mode = payload.get("lease_mode", "exclusive")
    if mode not in ("exclusive", "shared_receive"):
        raise RouteValidationError("rf_device lease_mode must be exclusive|shared_receive.")
    channels = payload.get("channels", [])
    if not isinstance(channels, list):
        raise RouteValidationError("rf_device channels must be a list.")
    freq = payload.get("frequency_hz")
    rate = payload.get("sample_rate")
    if freq is not None and not isinstance(freq, (int, float)):
        raise RouteValidationError("rf_device frequency_hz must be a number.")
    if rate is not None and not isinstance(rate, (int, float)):
        raise RouteValidationError("rf_device sample_rate must be a number.")
    purpose = payload.get("operation") or payload.get("purpose")
    return {
        "action": "acquire_lease",
        "device_id": device_id.strip(),
        "backend_id": backend_id.strip(),
        "direction": direction,
        "lease_mode": mode,
        "channels": channels,
        "frequency_hz": float(freq) if freq is not None else None,
        "sample_rate": float(rate) if rate is not None else None,
        "operation": purpose if isinstance(purpose, str) else None,
    }


def peer_message_from_decision(decision: CoordinatorDecision) -> dict:
    """Extract a validated peer-message spec from a ``peer_message`` decision payload (13B).

    The coordinator may ONLY name a trusted peer + the message content/correlation. It can NOT
    specify endpoints, keys, signatures, headers, retry policy, sender identity, or environment
    — those fields are ignored entirely. Raises :class:`RouteValidationError` on a bad payload.
    """
    payload = decision.structured_payload or {}
    peer_id = payload.get("peer_id")
    if not isinstance(peer_id, str) or not peer_id.strip():
        raise RouteValidationError(
            "peer_message route requires a non-empty structured_payload.peer_id."
        )
    message_type = payload.get("message_type", "request")
    if message_type not in ("request", "reply", "update"):
        raise RouteValidationError(
            "peer_message structured_payload.message_type must be request|reply|update."
        )
    text = payload.get("text", "")
    if not isinstance(text, str):
        raise RouteValidationError("peer_message structured_payload.text must be a string.")
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise RouteValidationError("peer_message structured_payload.data must be an object.")
    return {
        "peer_id": peer_id.strip(),
        "message_type": message_type,
        "text": text,
        "data": data,
        "expects_reply": bool(payload.get("expects_reply", False)),
        "conversation_id": payload.get("conversation_id"),
        "reply_to_message_id": payload.get("reply_to_message_id"),
        "reply_to_request_id": payload.get("reply_to_request_id"),
        "response_deadline": payload.get("response_deadline"),
    }


def peer_artifact_from_decision(decision: CoordinatorDecision) -> dict:
    """Extract a validated artifact-control spec from a ``peer_artifact`` decision (13D.2).

    The coordinator may ONLY name a trusted peer, a known local/remote artifact id, an action
    (offer|request|cancel), a bounded purpose, and an optional known conversation id. It can NOT
    specify a URL, filesystem path, object-store location, digest override, destination, HTTP
    range, retry policy, headers, or signing identity — those are ignored entirely. Raises
    :class:`RouteValidationError` on a bad payload.
    """
    payload = decision.structured_payload or {}
    action = payload.get("action", "request")
    if action not in ("offer", "request", "cancel"):
        raise RouteValidationError(
            "peer_artifact structured_payload.action must be offer|request|cancel."
        )
    if action == "cancel":
        transfer_id = payload.get("transfer_id")
        if not isinstance(transfer_id, str) or not transfer_id.strip():
            raise RouteValidationError(
                "peer_artifact cancel requires structured_payload.transfer_id."
            )
        return {"action": "cancel", "transfer_id": transfer_id.strip()}
    peer_id = payload.get("peer_id")
    if not isinstance(peer_id, str) or not peer_id.strip():
        raise RouteValidationError("peer_artifact requires a non-empty structured_payload.peer_id.")
    artifact_id = payload.get("artifact_id") or payload.get("origin_artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise RouteValidationError(
            "peer_artifact requires a known structured_payload.artifact_id (local or remote)."
        )
    purpose = payload.get("purpose")
    return {
        "action": action,
        "peer_id": peer_id.strip(),
        "artifact_id": artifact_id.strip(),
        "purpose": purpose if isinstance(purpose, str) else None,
        "conversation_id": payload.get("conversation_id"),
    }
