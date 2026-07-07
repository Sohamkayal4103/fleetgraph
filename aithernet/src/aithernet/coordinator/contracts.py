"""Coordinator domain contracts: inputs, decisions, capabilities, and errors.

These models are the stable boundary between the node runtime and any coordinator
provider. A provider consumes a :class:`CoordinatorInput` and returns a
:class:`CoordinatorDecision`; it never touches FastAPI or the database.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, ValidationError

from aithernet.schemas.events import EventRead
from aithernet.schemas.missions import MissionRead
from aithernet.schemas.node import NodeStatus
from aithernet.state.models import new_uuid, utcnow

#: Capabilities the coordinator may actually rely on in Stage 2. These map to real,
#: implemented subsystems of the node.
ACTIVE_CAPABILITIES: tuple[str, ...] = (
    "node_state",
    "event_log",
    "mission_store",
    "coordinator_reasoning",
)

#: Capabilities that later stages will add. They are advertised to the coordinator as
#: context only — they are NOT callable yet, and the runtime never acts on them.
FUTURE_CAPABILITIES: tuple[str, ...] = (
    "coding_agent",
    "gnuradio_mcp",
    "peer_agent",
    "manager_agent",
    "web_ui",
)


# -- errors ----------------------------------------------------------------------


class CoordinatorError(Exception):
    """Base class for all coordinator-related failures."""


class CoordinatorConfigurationError(CoordinatorError):
    """Raised when the coordinator is asked to reason without valid configuration.

    The runtime surfaces this as a clear configuration error rather than fabricating a
    decision.
    """


class CoordinatorProviderError(CoordinatorError):
    """Raised when a provider call fails or returns an unusable response."""


class MissionNotFoundError(Exception):
    """Raised when an operation references a mission id that does not exist."""


# -- input -----------------------------------------------------------------------


class CoordinatorInput(BaseModel):
    """Everything the coordinator needs to reason about a mission.

    Built by the node runtime from persisted state via :meth:`from_node_context`. It is
    deliberately descriptive (not prescriptive): it carries facts about the mission and
    node, never a predetermined plan.
    """

    mission_id: str
    mission_content: str
    mission_source_type: str
    mission_source_id: str | None = None
    mission_status: str
    node_status_summary: dict = Field(default_factory=dict)
    recent_events: list[dict] = Field(default_factory=list)
    available_capabilities: list[str] = Field(default_factory=list)
    future_capabilities: list[str] = Field(default_factory=list)
    # Stage 11B: compact MCP session + GNU Radio workspace context (never full catalogs/raw
    # results). Descriptive context only — never a fixed instruction to pick a route.
    mcp_session: dict = Field(default_factory=dict)
    gnuradio_context: dict = Field(default_factory=dict)
    # Stage 13A.5: compact summary of every configured RF backend (state, experimental flag,
    # discovered tool names when ready, and the backend's RF context summary). Descriptive
    # only — it NEVER tells the model which backend to choose and adds no keyword routing.
    rf_backends: dict = Field(default_factory=dict)
    # Stage 13B: compact summary of trusted, communication-authorized peers (id/name/role/
    # trust/authorization/health/capabilities). Descriptive only — it never tells the model
    # which peer to choose, and peer messaging is optional. No keys/manifests/signatures.
    peers: dict = Field(default_factory=dict)
    # Stage 12: compact autonomous-run context (run status, iteration, prior decisions/step
    # summaries/results, remaining budgets, waiting/resume history). Empty for the manual
    # one-step path. Descriptive only — never a fixed plan.
    mission_run_context: dict = Field(default_factory=dict)
    current_time: datetime

    @classmethod
    def from_node_context(
        cls,
        *,
        mission: MissionRead,
        node_status: NodeStatus,
        recent_events: Sequence[EventRead],
        current_time: datetime | None = None,
        available_capabilities: Iterable[str] = ACTIVE_CAPABILITIES,
        future_capabilities: Iterable[str] = FUTURE_CAPABILITIES,
        mcp_session: dict | None = None,
        gnuradio_context: dict | None = None,
        rf_backends: dict | None = None,
        peers: dict | None = None,
        mission_run_context: dict | None = None,
    ) -> CoordinatorInput:
        """Assemble a :class:`CoordinatorInput` from loaded node state.

        Pure transformation — no I/O — so it is trivially testable. ``recent_events``
        are compacted to the fields a reasoning model needs, keeping the prompt small.
        """
        return cls(
            mission_id=mission.id,
            mission_content=mission.content,
            mission_source_type=mission.source_type,
            mission_source_id=mission.source_id,
            mission_status=str(mission.status.value),
            node_status_summary=node_status.model_dump(mode="json"),
            recent_events=[
                {
                    "event_type": event.event_type,
                    "source": event.source,
                    "message": event.message,
                    "created_at": event.created_at.isoformat(),
                }
                for event in recent_events
            ],
            available_capabilities=list(available_capabilities),
            future_capabilities=list(future_capabilities),
            mcp_session=mcp_session or {},
            gnuradio_context=gnuradio_context or {},
            rf_backends=rf_backends or {},
            peers=peers or {},
            mission_run_context=mission_run_context or {},
            current_time=current_time or utcnow(),
        )


# -- mission control (Stage 12) --------------------------------------------------


class MissionControlDisposition(str, Enum):
    """Lifecycle outcome the coordinator chooses for one autonomous iteration.

    These are lifecycle outcomes, NOT autonomy levels or fixed operating modes:
      * ``continue`` — execute exactly one route (``next_target``), then reassess.
      * ``complete`` — the objective is satisfied; provide a final response. No route.
      * ``wait``     — pause on an external dependency; provide a structured reason. No route.
      * ``blocked``  — progress needs an unavailable capability/dependency. No route.
    """

    CONTINUE = "continue"
    COMPLETE = "complete"
    WAIT = "wait"
    BLOCKED = "blocked"


class MissionWaitCondition(BaseModel):
    """A structured, durable wait condition (Stage 13B). No executable predicates.

    The only supported machine-resolvable type is ``peer_reply``: the mission resumes when an
    authenticated, correlated reply to ``outbound_message_id`` arrives (or the deadline lapses).
    """

    type: str | None = None
    outbound_message_id: str | None = None
    deadline: datetime | None = None


class MissionWaiting(BaseModel):
    """Structured waiting reason (no arbitrary executable predicates)."""

    reason: str = ""
    wait_until: datetime | None = None
    wait_for_event_type: str | None = None
    wait_for_agent_id: str | None = None
    wait_for_user_input: bool | None = None
    condition_summary: str | None = None
    # Stage 13B: optional machine-resolvable condition (e.g. a correlated peer reply).
    wait_condition: MissionWaitCondition | None = None


class MissionBlocked(BaseModel):
    """Structured blocked reason with the missing capability when known."""

    reason: str = ""
    missing_capability: str | None = None


class MissionControl(BaseModel):
    """Additive mission-control section of a coordinator decision (Stage 12).

    Defaults to ``continue`` so existing (manual one-step) decisions remain valid.
    """

    disposition: MissionControlDisposition = MissionControlDisposition.CONTINUE
    reason: str | None = None
    final_response: str | None = None
    waiting: MissionWaiting | None = None
    blocked: MissionBlocked | None = None


# -- decision --------------------------------------------------------------------

#: Fields the reasoning model is responsible for producing. Identity and timestamps are
#: assigned by the runtime, not invented by the model.
_MODEL_DECISION_FIELDS: tuple[str, ...] = (
    "summary",
    "next_target",
    "action",
    "message",
    "structured_payload",
    "expected_result",
    "confidence",
    "mission_control",
)


class CoordinatorDecision(BaseModel):
    """A structured decision returned by the coordinator for one mission step.

    ``next_target`` is intentionally a free-form string, not an enum, so future targets
    do not require schema changes. Common values (none of which trigger deterministic
    behavior in Stage 2) include:

      * ``"respond"`` — reply to the mission source.
      * ``"continue_reasoning"`` — another coordinator step is warranted.
      * ``"node_state"`` — inspect/use node state.
      * ``"future_coding_agent"`` — would delegate to the coding agent (not yet active).
      * ``"future_gnuradio_mcp"`` — would use GNU Radio MCP (not yet active).
      * ``"future_peer_agent"`` — would contact a peer node (not yet active).
      * ``"future_manager_agent"`` — would escalate to a manager (not yet active).
    """

    decision_id: str = Field(default_factory=new_uuid)
    mission_id: str
    summary: str
    next_target: str
    action: str
    message: str
    structured_payload: dict = Field(default_factory=dict)
    expected_result: str
    confidence: float | None = None
    mission_control: MissionControl = Field(default_factory=MissionControl)
    created_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def from_model_json(cls, raw: object, *, mission_id: str) -> CoordinatorDecision:
        """Build a decision from a provider's parsed JSON output.

        Only model-owned fields are taken from ``raw``; ``mission_id`` is injected and
        ``decision_id``/``created_at`` are generated. A payload that does not satisfy
        the schema raises :class:`CoordinatorProviderError` so the runtime can record a
        failure rather than emit a malformed decision.
        """
        if not isinstance(raw, dict):
            raise CoordinatorProviderError(
                f"Coordinator response must be a JSON object, got {type(raw).__name__}."
            )
        data = {key: raw[key] for key in _MODEL_DECISION_FIELDS if key in raw}
        data["mission_id"] = mission_id
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise CoordinatorProviderError(
                f"Coordinator returned a decision that did not match the schema: {exc}"
            ) from exc


# -- status ----------------------------------------------------------------------


class CoordinatorStatus(BaseModel):
    """Coordinator configuration snapshot for ``GET /coordinator/status``.

    Reports only whether configuration is present — never secret values, auth files, or
    environment. ``executable``/``cli_version`` are relevant to CLI providers (gemini_cli);
    ``base_url_configured``/``api_key_configured`` to HTTP providers (openai_compatible).
    """

    provider: str
    model: str | None = None
    configured: bool
    missing_configuration: list[str] = Field(default_factory=list)
    # CLI providers (gemini_cli):
    executable: str | None = None
    cli_version: str | None = None
    # HTTP providers (openai_compatible, anthropic):
    base_url_configured: bool = False
    api_key_configured: bool = False
    active_capabilities: list[str] = Field(default_factory=list)
    future_capabilities: list[str] = Field(default_factory=list)


def validate_mission_control(decision: CoordinatorDecision) -> str | None:
    """Validate a decision's mission_control disposition (Stage 12).

    Returns an error message for an invalid combination (rejected + persisted by the
    engine), or ``None`` when valid. Route-target validity for ``continue`` is checked
    separately by the deterministic router; this enforces the disposition contract:

      * continue -> must name a next_target;
      * complete -> must include a non-empty final_response;
      * wait     -> must include a non-empty waiting reason (no external route);
      * blocked  -> must include a non-empty blocked reason (no external route).
    """
    mc = decision.mission_control
    disp = mc.disposition
    if disp is MissionControlDisposition.CONTINUE:
        if not decision.next_target or not decision.next_target.strip():
            return "mission_control.disposition 'continue' requires a non-empty next_target."
        return None
    if disp is MissionControlDisposition.COMPLETE:
        if not (mc.final_response and mc.final_response.strip()):
            return "mission_control.disposition 'complete' requires a non-empty final_response."
        return None
    if disp is MissionControlDisposition.WAIT:
        if mc.waiting is None or not (mc.waiting.reason and mc.waiting.reason.strip()):
            return "mission_control.disposition 'wait' requires a structured waiting.reason."
        return None
    if disp is MissionControlDisposition.BLOCKED:
        if mc.blocked is None or not (mc.blocked.reason and mc.blocked.reason.strip()):
            return "mission_control.disposition 'blocked' requires a structured blocked.reason."
        return None
    return f"Unknown mission_control.disposition '{disp}'."
