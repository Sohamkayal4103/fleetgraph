"""SQLAlchemy ORM models for the node state store."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_uuid() -> str:
    """Generate a string UUID used as a primary key."""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all node state models."""


class Mission(Base):
    """A unit of work submitted to the node.

    Stage 1 persists and reports missions; later stages attach the coordinator and
    coding agents that drive a mission through its lifecycle. The ``status`` values
    are defined by :class:`aithernet.schemas.missions.MissionStatus`.
    """

    __tablename__ = "missions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_type: Mapped[str] = mapped_column(String(64), default="user", nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="received", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Event(Base):
    """A runtime event recorded in the node's append-only event log.

    Events are the audit trail and the payload streamed over the event bus. They may
    be associated with a mission (``mission_id``) or be node-level (``mission_id`` is
    ``None``).
    """

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(128), default="runtime", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class CodingTask(Base):
    """A unit of implementation/execution work for the coding agent.

    Stage 3 persists, runs, and reports coding tasks. A task may belong to a mission
    (``mission_id``) or stand alone. The ``status`` values are defined by
    :class:`aithernet.schemas.coding.CodingTaskStatus`. The coding agent that executes a
    task is real (Claude Code via its CLI); GNU Radio MCP and automatic coordinator
    routing are later stages.
    """

    __tablename__ = "coding_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    available_tools_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    expected_outputs_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    reporting_requirements_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CodingTaskResult(Base):
    """The recorded outcome of executing a :class:`CodingTask`.

    One task may produce several results over time (e.g. on re-runs); the latest is the
    one the API surfaces. Output streams are captured verbatim from the coding agent and
    never fabricated.
    """

    __tablename__ = "coding_task_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("coding_tasks.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stdout: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stderr: Mapped[str] = mapped_column(Text, default="", nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifacts_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    files_changed_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    commands_run_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class MissionStep(Base):
    """The auditable record of executing one coordinator decision for a mission.

    Stage 5 turns a single :class:`CoordinatorDecision` into one routed execution step.
    The decision is interpreted through a documented routing contract (see
    :mod:`aithernet.orchestrator.decision_router`) — never fragile natural-language
    parsing — and the chosen route is validated and executed only if its target/action/
    payload are clear and available. ``status`` values are defined by
    :class:`aithernet.schemas.mission_steps.MissionStepStatus`.
    """

    __tablename__ = "mission_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("missions.id"), nullable=False, index=True
    )
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    decision_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    route_target: Mapped[str] = mapped_column(String(64), nullable=False)
    route_action: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExternalAgent(Base):
    """An external agent connected to this node as a message/mission source.

    Stage 7 lets an outside party (a user's private agent, a manager agent, another
    node's coordinator, a lab orchestrator, or a local/cloud process) connect, be listed,
    and send messages that the node persists. An external agent is a *source*, not an
    internal reasoning provider — the node never calls ``endpoint_url`` automatically in
    Stage 7 (it is stored as connection info for a later stage). ``status`` values are
    defined by :class:`aithernet.communication.contracts.ExternalAgentStatusValue`.

    No raw secret/token is ever stored: the connection records only an ``auth_type`` hint
    (e.g. "none", "bearer", "custom"); secure secret storage is a later stage.
    """

    __tablename__ = "external_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_type: Mapped[str] = mapped_column(String(64), default="external", nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    transport: Mapped[str] = mapped_column(String(32), default="local", nullable=False)
    auth_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="connected", nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- Stage 13A peer-trust / transport identity (additive; never holds private keys) --
    # An external-agent record doubles as a transport PEER record. These columns are all
    # nullable so existing Stage 7 rows remain valid. `trust_state` gates real delivery; a
    # newly added peer is `untrusted` until an operator explicitly trusts its public key.
    role: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    expected_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    trust_state: Mapped[str] = mapped_column(String(16), default="untrusted", nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    tls_verify: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_manifest_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    capability_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    capability_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Stage 13B application authorization (additive; SEPARATE from public-key trust) --
    # Public-key trust authenticates identity; these flags authorize coordinator-level
    # communication. Defaults are conservative: trust alone never authorizes a peer to create
    # an inbound mission here. Existing trusted peers remain valid transport peers.
    may_send_requests: Mapped[bool] = mapped_column(default=False, nullable=False)
    may_send_replies: Mapped[bool] = mapped_column(default=True, nullable=False)
    may_receive_messages: Mapped[bool] = mapped_column(default=True, nullable=False)
    may_request_response: Mapped[bool] = mapped_column(default=False, nullable=False)
    max_inbound_request_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_concurrent_inbound_missions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- Stage 13D.2 artifact authorization (additive; SEPARATE from trust AND from message
    # authorization). Transport trust never authorizes an artifact transfer; all default false.
    may_offer_artifacts: Mapped[bool] = mapped_column(default=False, nullable=False)
    may_request_artifacts: Mapped[bool] = mapped_column(default=False, nullable=False)
    may_receive_artifacts: Mapped[bool] = mapped_column(default=False, nullable=False)
    max_artifact_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_concurrent_transfers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allowed_artifact_kinds_json: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # -- Stage 13D.3 mission-status authorization (additive; SEPARATE from trust, message AND
    # artifact permissions). All default false: trust never authorizes status exchange.
    # `may_publish` = this peer may publish its mission status TO us (we accept its updates);
    # `may_receive` = we may send OUR mission status to this peer; `may_query` = peer may query us.
    may_publish_mission_status: Mapped[bool] = mapped_column(default=False, nullable=False)
    may_query_mission_status: Mapped[bool] = mapped_column(default=False, nullable=False)
    may_receive_mission_status: Mapped[bool] = mapped_column(default=False, nullable=False)
    max_active_remote_snapshots: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ExternalAgentMessage(Base):
    """A message exchanged with an :class:`ExternalAgent`, in either direction.

    Stage 7 persists every inbound message a connected agent sends (and the outbound reply
    the node records summarising what happened). An inbound ``mission_request`` may create
    a :class:`Mission` (``mission_id`` links the two). Direction values are
    ``inbound``/``outbound``; the node never fabricates a delivery it did not make — an
    outbound row is a recorded local summary, not proof of a network callback.
    """

    __tablename__ = "external_agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("external_agents.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), default="mission_request", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class MissionExecutionRun(Base):
    """A persistent autonomous-execution run for a mission (Stage 12).

    Holds the autonomous runtime metadata (lease, iteration/action counts, budgets, usage,
    lifecycle reasons, final summary) so it is not scattered onto the mission. A mission has
    at most one non-terminal run; a new run is created only by an explicit user operation.
    Lease tokens live here but are NEVER exposed by the API/events/UI.
    """

    __tablename__ = "mission_execution_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    mission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("missions.id"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)

    # -- lease (DB-backed, atomic) --
    execution_owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_renewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- counters / usage --
    iteration_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coordinator_call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    node_state_action_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coding_agent_action_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mcp_action_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    response_action_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Stage 14B: one MissionStep per model-selected rf_device lease action (renewal/polling
    # create no step). Additive; existing rows default to 0.
    rf_device_action_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    elapsed_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    budgets_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # -- linkage / lifecycle --
    last_mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_route_target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    waiting_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_response: Mapped[str | None] = mapped_column(Text, nullable=True)

    pause_requested: Mapped[bool] = mapped_column(default=False, nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recovery_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # -- timestamps --
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    waiting_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GNURadioContextRecord(Base):
    """The node's current structured GNU Radio workspace context (Stage 11B).

    One current record per node (keyed by ``node_id``). It stores *compact* structured
    summaries derived only from successful, parsed MCP tool results — large raw results stay
    in :class:`MCPToolCall` and are referenced by ``source_mcp_call_ids_json``. Values are
    never fabricated: unavailable/unknown fields stay ``null`` rather than being invented.
    """

    __tablename__ = "gnuradio_context"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    active_flowgraph_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    active_flowgraph_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active_flowgraph_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    flowgraph_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    block_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    connection_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    validation_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latest_error_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    related_mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    related_mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_mcp_call_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_session_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stale: Mapped[bool] = mapped_column(default=True, nullable=False)
    stale_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    context_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class MCPToolCall(Base):
    """An audited call to a tool exposed by an external MCP server.

    Stage 4 records every GNU Radio MCP tool invocation — its caller, tool name,
    arguments, status, and structured result — so MCP usage is fully auditable. Tool
    names are discovered at runtime via ``tools/list`` and never hardcoded. A call may be
    associated with a mission and/or a coding task, or stand alone.
    """

    __tablename__ = "mcp_tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    caller: Mapped[str] = mapped_column(String(64), default="user", nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    arguments_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Stage 11B: which managed session/generation executed this call (audit + recovery).
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    session_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_catalog_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Stage 13A.5: which RF backend executed this call (additive; existing rows default to
    # the legacy GNU Radio backend). backend_id is a stable id, never a filesystem path.
    backend_id: Mapped[str] = mapped_column(
        String(64), default="legacy_gr_mcp", nullable=False, index=True
    )
    backend_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    backend_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backend_source_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_summary_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RFArtifact(Base):
    """A safe reference to an artifact a backend wrote beneath its configured workspace (13A.5).

    Only the workspace-RELATIVE path is stored; absolute paths, traversal, and symlink escapes
    are rejected before insertion. The artifact bytes are NEVER copied into SQLite — this is a
    bounded, audited reference linked to the originating MCP call + mission/run/step.
    """

    __tablename__ = "rf_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    backend_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(64), default="unknown", nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    mcp_call_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mission_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # -- Stage 13D.2 transferable identity + evidence (additive; existing rows stay readable) --
    # origin_* identify where a transferred artifact came from; digest/object_ref tie it to the
    # managed content-addressed store (bytes live ONLY on disk, never in this row).
    origin_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    backend_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backend_source_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    digest: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    object_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    availability_state: Mapped[str] = mapped_column(
        String(24), default="indexed", nullable=False, index=True
    )
    pinned: Mapped[bool] = mapped_column(default=False, nullable=False)
    retention_state: Mapped[str] = mapped_column(String(24), default="default", nullable=False)


class RFWorkspaceContextRecord(Base):
    """Per-(node, backend) persistent RF workspace context (Stage 13A.5).

    One row per node+backend. The legacy backend keeps its own dedicated GNU Radio context
    table; this generic record carries the higher-level RF summaries (devices/captures/signals/
    measurements/scenes/pipelines/runs/artifacts) for backends like Marconi. Summaries are
    compact — never full captures, plots, schemas, or raw MCP results.
    """

    __tablename__ = "rf_workspace_context"
    __table_args__ = (UniqueConstraint("node_id", "backend_id", name="uq_rf_context_node_backend"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    backend_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    backend_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backend_source_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    stale: Mapped[bool] = mapped_column(default=True, nullable=False)
    stale_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    devices_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    captures_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    signals_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    measurements_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    active_scene_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    active_pipeline_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pipeline_validation_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recent_runs_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    artifacts_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    exported_flowgraphs_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latest_errors_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    related_mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    related_mission_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    related_mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_mcp_call_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_session_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class RFBenchmarkRun(Base):
    """One operator-run comparative benchmark of a backend against a scenario (Stage 13A.5).

    A benchmark is evidence, NOT an automatic routing rule: it never changes the default
    backend. Each scenario step is one atomic MCP call (no hidden multi-tool workflow). Failed
    scenarios are persisted honestly.
    """

    __tablename__ = "rf_benchmark_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scenario_version: Mapped[str] = mapped_column(String(16), default="1", nullable=False)
    backend_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    backend_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backend_source_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    success: Mapped[bool] = mapped_column(default=False, nullable=False)
    mission_iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coordinator_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mcp_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    elapsed_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    validation_outcome: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    artifact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    artifact_types_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repeatability_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    steps_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class MissionReplyWait(Base):
    """A durable record that a mission is waiting for a correlated peer reply (Stage 13B).

    Created when the coordinator chooses ``mission_control=wait`` with a ``peer_reply``
    condition. Exactly one *pending* wait may exist per outbound ``request_id``; a single
    correlated reply from the expected peer satisfies it exactly once (atomic compare-and-set),
    which requeues the mission for reassessment. A delivery ACK is never a satisfying reply.
    """

    __tablename__ = "mission_reply_waits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    mission_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    mission_run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    outbound_message_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    expected_peer_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    satisfied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timed_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    satisfying_inbox_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    satisfying_transport_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resume_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resume_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class InboundRequest(Base):
    """A durable record of an authenticated, authorized inbound coordinator request (13B).

    Guarantees one inbound request id from one peer creates at most one mission. A second
    delivery (same id + same content) is a duplicate; a same-id/different-content or
    different-peer arrival is rejected/isolated. ``response_message_id`` records the queued
    correlated reply that satisfies a response obligation.
    """

    __tablename__ = "inbound_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    peer_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    sender_node_id: Mapped[str] = mapped_column(String(36), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    inbox_message_id: Mapped[str] = mapped_column(String(36), nullable=False)
    envelope_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    response_required: Mapped[bool] = mapped_column(default=False, nullable=False)
    response_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="received", nullable=False, index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class AgentOutboxMessage(Base):
    """A durable outbound transport message awaiting authenticated delivery (Stage 13A).

    The ``message_id`` is immutable across every retry — the receiver deduplicates on it, so
    a transport retry never produces a new logical message. A DB-backed claim lease lets one
    delivery worker own a record at a time. ``delivered`` means the peer accepted the HTTP
    request; ``acknowledged`` means a valid SIGNED ack from the expected peer was verified —
    HTTP 200 alone is never acknowledgement. Private keys/signatures of our own messages are
    fine to store (they are ours); peer auth material/headers are never persisted here.
    """

    __tablename__ = "agent_outbox_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    message_id: Mapped[str] = mapped_column(
        String(36), default=new_uuid, nullable=False, unique=True, index=True
    )
    peer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("external_agents.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(16), default="outbound", nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="agent_message", nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(8), default="1", nullable=False)
    envelope_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    envelope_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reply_to_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mission_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Stage 13B application-level correlation (additive; the application payload lives in
    # envelope_json.payload). These are denormalized for fast reply correlation.
    application_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    application_request_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    reply_to_request_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    expects_reply: Mapped[bool] = mapped_column(default=False, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    first_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_ack_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dead_letter_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # DB-backed delivery claim (one worker owns a record at a time).
    claim_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class AgentInboxMessage(Base):
    """A durable inbound transport message, deduplicated by authenticated message id (13A).

    Exactly ONE logical row exists per authenticated ``message_id``. A repeat delivery with
    the SAME canonical ``envelope_hash`` increments ``duplicate_count`` and returns the same
    acknowledgement outcome; a repeat with a DIFFERENT hash is rejected as a collision and
    never overwrites the original. Inbound payloads are stored as DATA only — Stage 13A never
    executes a mission/route/tool/shell op from an inbound message.
    """

    __tablename__ = "agent_inbox_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    message_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    peer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("external_agents.id"), nullable=True, index=True
    )
    sender_node_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sender_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(8), default="1", nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="agent_message", nullable=False)
    envelope_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    envelope_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reply_to_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mission_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Stage 13B application-level correlation (additive; parsed from the application payload).
    application_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    application_request_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    reply_to_request_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    application_state: Mapped[str] = mapped_column(String(24), default="none", nullable=False)

    processing_state: Mapped[str] = mapped_column(String(20), default="received", nullable=False)
    ack_status: Mapped[str] = mapped_column(String(16), default="accepted", nullable=False)
    ack_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    authenticated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ArtifactTransfer(Base):
    """A durable cross-node RF-artifact transfer (Stage 13D.2).

    Direction ``inbound`` = this node downloads bytes from a peer; ``outbound`` = this node
    serves bytes to a peer under a bound grant. Binary bytes live ONLY in the managed store on
    disk — this row holds bounded metadata, the expected SHA-256/size to verify against, the
    received-byte progress (for resume), and the grant binding (exact sender/receiver/digest/
    size/expiry). A transport/HTTP retry never creates a MissionStep.
    """

    __tablename__ = "artifact_transfers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    transfer_id: Mapped[str] = mapped_column(
        String(36), default=new_uuid, nullable=False, unique=True, index=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    peer_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    local_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    origin_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="offered", nullable=False, index=True)

    expected_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    expected_size: Mapped[int] = mapped_column(Integer, nullable=False)
    received_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    partial_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    object_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)

    artifact_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mission_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Grant binding (a grant for one peer/artifact cannot be reused by another).
    expected_sender_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_receiver_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grant_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    claim_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArtifactTransferAttempt(Base):
    """One audited attempt of an :class:`ArtifactTransfer` (Stage 13D.2)."""

    __tablename__ = "artifact_transfer_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    transfer_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # The byte offset this attempt STARTED from — 0 for a fresh attempt, > 0 when an attempt
    # resumes a partially-downloaded transfer after a process restart (Stage 13D.2 restart proof).
    start_offset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_transferred: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), default="started", nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RemoteArtifactReference(Base):
    """An artifact a trusted peer OFFERED to this node (Stage 13D.2).

    Metadata only — no bytes. Lets an operator/coordinator see what is on offer before
    requesting a download. One ``(peer_id, origin_artifact_id)`` is one logical offer.
    """

    __tablename__ = "remote_artifact_references"
    __table_args__ = (
        UniqueConstraint("peer_id", "origin_artifact_id", name="uq_remote_artifact_peer_origin"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    peer_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    origin_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    digest: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="offered", nullable=False, index=True)
    local_transfer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    local_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    offered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class LocalMissionStatusPublication(Base):
    """Per-(local mission, requesting peer) status publication bookkeeping (Stage 13D.3).

    Holds the monotonically increasing sequence and the last state this node has published to a
    peer about one of its OWN local missions. Sequence allocation is infrastructure-owned and
    survives restart; re-publishing the same state is idempotent (no new sequence). This row is
    NOT the mission — it never gates or mutates mission execution.
    """

    __tablename__ = "local_mission_status_publications"
    __table_args__ = (
        UniqueConstraint("mission_id", "peer_id", name="uq_local_status_mission_peer"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    mission_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    peer_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    last_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class RemoteMissionSnapshot(Base):
    """The latest AUTHENTICATED status this node holds for a remote peer mission (Stage 13D.3).

    One current snapshot per factual remote relationship — keyed by (peer, remote mission ref).
    Updated only by authenticated, in-order status messages; never by inference. A remote update
    NEVER touches the local mission table.
    """

    __tablename__ = "remote_mission_snapshots"
    __table_args__ = (
        UniqueConstraint("peer_id", "remote_mission_ref", name="uq_remote_snapshot_peer_mission"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    origin_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    peer_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    remote_mission_ref: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    parent_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    local_mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    latest_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latest_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    response_obligation: Mapped[str | None] = mapped_column(String(24), nullable=True)
    progress_summary: Mapped[str | None] = mapped_column(String(320), nullable=True)
    terminal_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    artifact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transfer_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    artifact_ids_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    transfer_ids_json: Mapped[list | None] = mapped_column(JSON, nullable=True)

    remote_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    freshness_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Persisted freshness classification: unknown | fresh | stale | terminal. Recomputed from
    # timestamps on read; the stored value lets a stale transition be notified at most once.
    freshness: Mapped[str] = mapped_column(String(16), default="fresh", nullable=False)
    stale_notified: Mapped[bool] = mapped_column(default=False, nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(8), default="1", nullable=False)
    query_pending: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class RemoteMissionStatusEvent(Base):
    """Append-only evidence of every received remote status message + its disposition (13D.3)."""

    __tablename__ = "remote_mission_status_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    peer_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    remote_mission_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    message_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # accepted | duplicate | older_sequence_ignored | invalid_transition_rejected |
    # unauthorized_rejected | unknown_mission_rejected | malformed_rejected
    disposition: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    inbox_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class SDRDevice(Base):
    """A stable, persisted managed-hardware device record (Stage 14B, Part C/D).

    Identity is the deterministic ``hardware_key`` (provider + driver + vendor + product +
    serial + channel) — NOT a transient enumeration index — so a record survives node restart
    and ordinary USB re-enumeration. A disappeared device is kept as a historical record (its
    presence becomes ``missing``); rediscovery updates this row rather than duplicating it.
    ``device_kind`` is an extensible string so an optical/replay/custom-IQ frontend is
    representable without redesigning the table. No raw USB path / environment / credential is
    ever stored here — only bounded sanitized facts.
    """

    __tablename__ = "sdr_devices"
    __table_args__ = (
        UniqueConstraint("node_id", "hardware_key", name="uq_sdr_device_node_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    hardware_key: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_kind: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    product: Mapped[str | None] = mapped_column(String(128), nullable=True)
    serial: Mapped[str | None] = mapped_column(String(128), nullable=True)
    driver: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transport: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    identity_limited: Mapped[bool] = mapped_column(default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    administrative_state: Mapped[str] = mapped_column(
        String(16), default="enabled", nullable=False
    )
    presence_state: Mapped[str] = mapped_column(String(16), default="present", nullable=False)
    health_state: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="present", nullable=False, index=True)
    capability_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class SDRDeviceCapability(Base):
    """The current normalized, source-attributed capabilities for one device (Part E).

    One current row per device (keyed by ``device_id``). Capabilities are FACTUAL: unknown stays
    null/empty and is never inferred from a vendor/product name. Scalar summary columns make the
    common compatibility checks queryable; the full normalized snapshot rides in JSON
    (bounded, never raw command output).
    """

    __tablename__ = "sdr_device_capabilities"
    __table_args__ = (
        UniqueConstraint("device_id", name="uq_sdr_capability_device"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sdr_devices.id"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    rx_supported: Mapped[bool | None] = mapped_column(nullable=True)
    tx_supported: Mapped[bool | None] = mapped_column(nullable=True)
    full_duplex: Mapped[bool | None] = mapped_column(nullable=True)
    channel_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shared_receive_safe: Mapped[bool] = mapped_column(default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    capabilities_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class SDRDeviceHealthEvent(Base):
    """Append-only factual health/presence history for a device (Part F/L).

    Repeated identical readings are rate-limited by the inventory service, so a flapping device
    cannot flood this table. Carries only states + a bounded sanitized detail.
    """

    __tablename__ = "sdr_device_health_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    health_state: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_health_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    presence_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class SDRDeviceBackendBinding(Base):
    """An administrator-approved mapping from a managed device to an RF backend (Part I).

    A deterministic binding adapter derives the backend-specific arguments from THIS row plus the
    persisted device — the coordinator never supplies device arguments, driver strings, paths,
    environment, or endpoints. ``device_args_json`` is bounded, sanitized, operator-approved.
    """

    __tablename__ = "sdr_device_backend_bindings"
    __table_args__ = (
        UniqueConstraint("device_id", "backend_id", name="uq_sdr_binding_device_backend"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    backend_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="config", nullable=False)
    device_args_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class SDRDeviceLease(Base):
    """A durable lease that authorizes managed physical-device use (Part G).

    Only an ACTIVE valid lease authorizes a device; acquisition is atomic (a conditional UPDATE
    cannot grant two conflicting exclusive leases). ``lease_token``/``owner_worker_id`` back the
    claim and are NEVER exposed by the API/events/UI. A lease is released on explicit release,
    cancellation, mission terminal state, expiry, revocation, device disappearance, or restart
    reconciliation. History is retained — a terminal lease row is never deleted.
    """

    __tablename__ = "sdr_device_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    backend_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mission_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    requested_operation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direction: Mapped[str] = mapped_column(String(8), default="rx", nullable=False)
    requested_channels_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    lease_mode: Mapped[str] = mapped_column(String(20), default="exclusive", nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    owner_worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rf_operation_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class HardwareQualificationRun(Base):
    """One real-hardware qualification run for a managed device (Stage 14C.1).

    Holds the bounded qualification environment record (Part A — software/OS/GNU Radio/Python/
    provider versions, sanitized host id; never env maps/credentials/private paths) and the
    overall outcome + support classification derived from the executed checks. Runs are additive
    (a device may be qualified repeatedly); evidence is never overwritten.
    """

    __tablename__ = "hardware_qualification_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    hardware_key: Mapped[str | None] = mapped_column(String(96), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="run", nullable=False)
    support_classification: Mapped[str] = mapped_column(
        String(32), default="discovered", nullable=False
    )
    capability_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    environment_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    checks_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checks_passed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checks_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checks_not_executed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HardwareQualificationCheck(Base):
    """One factual qualification check within a run (Stage 14C.1).

    ``status`` is ``passed``/``failed``/``not_executed``/``skipped``. ``evidence_json`` is bounded
    and sanitized (ids, counts, parameters, digests) — never raw probe output, paths, or secrets.
    """

    __tablename__ = "hardware_qualification_checks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="general", nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class HardwareRFMeasurement(Base):
    """Provenance for one physical RF result (Stage 14C.1, Part I).

    Full provenance for an actual capture/survey: device + capability revision + lease + backend
    + mission correlation, RF parameters, capture size + SHA-256, processing/analysis parameters,
    confidence and warnings. Large sample bytes live ONLY in the artifact store (referenced by
    ``artifact_id``/``digest``) — never in SQLite. ``result_json`` is a bounded reduced result.
    """

    __tablename__ = "hardware_rf_measurements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    qualification_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    capability_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    backend_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backend_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mission_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), default="capture", nullable=False)
    center_frequency_hz: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_rate_sps: Mapped[float | None] = mapped_column(Float, nullable=True)
    gain_db: Mapped[float | None] = mapped_column(Float, nullable=True)
    antenna: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stream_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    capture_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(80), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    processing_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    analysis_params_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class FieldCampaign(Base):
    """A multi-node physical RF field-validation / soak campaign (Stage 14C.2, Part A).

    Records a bounded, sanitized topology (participating node identities, sanitized host ids, SDR
    device ids, backend revisions), the test mode, configured thresholds, status, and an honest
    acceptance classification. Never stores credentials, private keys, environment maps, full
    network interfaces, raw private paths, or unrestricted command output.
    """

    __tablename__ = "field_campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    objective: Mapped[str | None] = mapped_column(String(500), nullable=True)
    profile: Mapped[str] = mapped_column(String(24), default="smoke", nullable=False)
    test_mode: Mapped[str] = mapped_column(String(24), default="validation", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="created", nullable=False, index=True)
    classification: Mapped[str] = mapped_column(
        String(40), default="software_harness_complete", nullable=False
    )
    software_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    config_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    topology_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    thresholds_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    checks_passed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checks_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checks_not_executed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class FieldNode(Base):
    """A participating node in a field campaign (this node, a peer, or the ground station)."""

    __tablename__ = "field_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    campaign_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(24), default="node", nullable=False)
    node_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    peer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    identity_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sanitized_host_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ready: Mapped[bool] = mapped_column(default=False, nullable=False)
    detail: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class FieldDeviceAssignment(Base):
    """A managed SDR assigned to a campaign node + role (rx/tx/survey)."""

    __tablename__ = "field_device_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    campaign_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    field_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False)
    hardware_key: Mapped[str | None] = mapped_column(String(96), nullable=True)
    backend_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="rx", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class FieldRun(Base):
    """One run within a campaign (capture / survey / ip_coordination / payload_exchange)."""

    __tablename__ = "field_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    campaign_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    iteration_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FieldCheck(Base):
    """One factual campaign check (passed/failed/not_executed/skipped) with bounded evidence."""

    __tablename__ = "field_checks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    campaign_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="general", nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    classification: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class FieldMeasurement(Base):
    """A per-iteration field measurement (capture/survey/payload) with full provenance (Part E)."""

    __tablename__ = "field_measurements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    campaign_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    iteration: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="capture", nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    capability_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    backend_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mission_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    measurement_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    center_frequency_hz: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_rate_sps: Mapped[float | None] = mapped_column(Float, nullable=True)
    gain_db: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_samples: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_samples: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(80), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overrun: Mapped[bool | None] = mapped_column(nullable=True)
    underrun: Mapped[bool | None] = mapped_column(nullable=True)
    cleanup_ok: Mapped[bool | None] = mapped_column(nullable=True)
    rss_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disk_free_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(default=False, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class FieldFaultInjection(Base):
    """A deterministic, operator-approved fault injection (validation mode only) (Part I)."""

    __tablename__ = "field_fault_injections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fault_type: Mapped[str] = mapped_column(String(40), nullable=False)
    params_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="requested", nullable=False, index=True)
    confirmed: Mapped[bool] = mapped_column(default=False, nullable=False)
    expected_recovery: Mapped[str | None] = mapped_column(String(320), nullable=True)
    actual_recovery: Mapped[str | None] = mapped_column(String(320), nullable=True)
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    cleanup_ok: Mapped[bool | None] = mapped_column(nullable=True)
    detail: Mapped[str | None] = mapped_column(String(320), nullable=True)
    injected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class FieldRecoveryResult(Base):
    """The result of a recovery after a fault/restart (identifies persisted objects, Part J)."""

    __tablename__ = "field_recovery_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    fault_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    success: Mapped[bool] = mapped_column(default=False, nullable=False)
    survived_objects_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class SoakRun(Base):
    """A long-duration resource soak run with bounded periodic sampling (Part M)."""

    __tablename__ = "soak_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[str] = mapped_column(String(24), default="smoke", nullable=False)
    target_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    sample_interval_seconds: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False, index=True)
    thresholds_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    violations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SoakSample(Base):
    """One bounded resource sample within a soak run (creates NO MissionStep) (Part M)."""

    __tablename__ = "soak_samples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    soak_run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rss_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    fd_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    child_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thread_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    db_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wal_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outbox_pending: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_leases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed_retries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disk_free_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class SDRDeviceLeaseEvent(Base):
    """Append-only audit of every lease lifecycle transition (Part G/Q)."""

    __tablename__ = "sdr_device_lease_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    lease_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# Stage 14D — External-agent interoperability (the "interop" namespace).
#
# These tables are for EXTERNAL programs / non-Aithernet agents (orchestration
# systems, robotics, ground-station apps, local scripts) that submit missions
# over an authenticated gateway and receive durable signed callbacks. They are
# deliberately SEPARATE from the Stage 7/13 ``external_agents`` (peer) table,
# which models Aithernet node-to-node peers. Nothing here duplicates the peer
# transport; it builds on the same Ed25519 identity + canonical-signing helpers.
# ---------------------------------------------------------------------------


class InteropAgent(Base):
    """A first-class external (non-Aithernet) agent identity (Stage 14D)."""

    __tablename__ = "interop_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    identity_type: Mapped[str] = mapped_column(String(24), nullable=False, default="ed25519")
    fingerprint: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    provisioning_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    permissions_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    endpoint_policy: Mapped[str] = mapped_column(String(24), nullable=False, default="default")
    rate_limit_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_authenticated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    disabled_reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class InteropCredential(Base):
    """An agent authentication credential: an Ed25519 key, or a hashed bearer token."""

    __tablename__ = "interop_credentials"
    __table_args__ = (UniqueConstraint("agent_id", "key_id", name="uq_interop_credential_keyid"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("interop_agents.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="ed25519")
    key_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    public_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scopes_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class InteropEndpoint(Base):
    """A registered callback (webhook) endpoint with a sanitized policy result."""

    __tablename__ = "interop_endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("interop_agents.id"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    scheme: Mapped[str] = mapped_column(String(8), nullable=False, default="https")
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending_verification", index=True
    )
    policy_result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verification_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signing_key_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class InteropSubscription(Base):
    """A bounded event subscription owning a monotonic per-subscription sequence."""

    __tablename__ = "interop_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("interop_agents.id"), nullable=False, index=True
    )
    endpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("interop_endpoints.id"), nullable=True, index=True
    )
    delivery_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="webhook")
    filters_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    next_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_acked_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retention_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=604800)
    reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class InteropSession(Base):
    """An authenticated WebSocket session bound to one agent (Stage 14D)."""

    __tablename__ = "interop_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("interop_agents.id"), nullable=False, index=True
    )
    subscription_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    remote_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_acked_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    close_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)


class InteropMessage(Base):
    """A canonical external-agent event envelope + its durable outbox/delivery state."""

    __tablename__ = "interop_messages"
    __table_args__ = (
        UniqueConstraint("subscription_id", "sequence", name="uq_interop_message_seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("interop_agents.id"), nullable=False, index=True
    )
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("interop_subscriptions.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    delivery_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="webhook")
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    # Durable outbox / delivery state.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    claim_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(48), nullable=True)
    dead_letter_reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    first_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class InteropDelivery(Base):
    """Per-attempt audit record for one webhook/WebSocket delivery attempt."""

    __tablename__ = "interop_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    message_pk: Mapped[str] = mapped_column(
        ForeignKey("interop_messages.id"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="webhook")
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(48), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(320), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class InteropReceipt(Base):
    """An idempotent application acknowledgement of a delivered message."""

    __tablename__ = "interop_receipts"
    __table_args__ = (
        UniqueConstraint("agent_id", "message_id", name="uq_interop_receipt_agent_message"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subscription_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="webhook_app")
    accepted: Mapped[bool] = mapped_column(default=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class InteropNonce(Base):
    """A bounded replay-protection cache of authenticated request nonces."""

    __tablename__ = "interop_nonces"
    __table_args__ = (UniqueConstraint("agent_id", "nonce", name="uq_interop_nonce"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    nonce: Mapped[str] = mapped_column(String(120), nullable=False)
    key_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    request_target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class InteropSubmission(Base):
    """An idempotent mission-submission acceptance record (agent_id + idempotency_key)."""

    __tablename__ = "interop_submissions"
    __table_args__ = (
        UniqueConstraint("agent_id", "idempotency_key", name="uq_interop_submission_idem"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted")
    receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class InteropAudit(Base):
    """A sanitized audit record of external-agent administrative + security events."""

    __tablename__ = "interop_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    detail: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# Stage 14E — privacy-preserving telemetry, consent, export, and dataset lineage.
#
# Node-side ("data platform") tables. The product is fully functional without these.
# Export is OFF + paused by default; training + raw-artifact collection require explicit
# recorded consent. Export records are immutable after sealing; corrections supersede.
# Nothing here creates a MissionStep, and no table ever stores a secret/token/key/raw path.
# ---------------------------------------------------------------------------


class DataConsentProfile(Base):
    """An affirmatively-accepted versioned participation/research-preview policy."""

    __tablename__ = "data_consent_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject: Mapped[str | None] = mapped_column(String(160), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    document_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    required_categories_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    optional_categories_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    effective_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class DataConsentGrant(Base):
    """The current effective per-category consent grant; history lives in revisions."""

    __tablename__ = "data_consent_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject: Mapped[str | None] = mapped_column(String(160), nullable=True)
    category: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    destination_scope: Mapped[str] = mapped_column(String(24), nullable=False, default="local")
    collection_scope: Mapped[str] = mapped_column(String(24), nullable=False, default="enabled")
    export_scope: Mapped[str] = mapped_column(String(24), nullable=False, default="disabled")
    raw_artifact_scope: Mapped[str] = mapped_column(String(24), nullable=False, default="disabled")
    policy_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    terms_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", index=True
    )
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    current_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class DataConsentRevision(Base):
    """Append-only consent history (grant/modify/withdraw). Never mutated or deleted."""

    __tablename__ = "data_consent_revisions"
    __table_args__ = (
        UniqueConstraint("grant_id", "revision_no", name="uq_data_consent_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    grant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    scopes_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class DataConsentAudit(Base):
    """Sanitized append-only audit of consent + data-platform security events."""

    __tablename__ = "data_consent_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class DataCategoryPolicy(Base):
    """Operator-tunable per-category collection/export policy (typed, never an expression)."""

    __tablename__ = "data_category_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "node_id", "category", name="uq_data_category_policy"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    collect: Mapped[bool] = mapped_column(default=True, nullable=False)
    export_allowed: Mapped[bool] = mapped_column(default=False, nullable=False)
    require_approval: Mapped[bool] = mapped_column(default=False, nullable=False)
    redaction_policy_version: Mapped[str] = mapped_column(String(16), nullable=False, default="r1")
    retention_class: Mapped[str] = mapped_column(String(24), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class DataExportRecord(Base):
    """The canonical export record envelope + its durable outbox state (Part 6/10)."""

    __tablename__ = "data_export_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    category: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_pseudonym: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_pseudonym: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    consent_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    redaction_policy_version: Mapped[str] = mapped_column(String(16), nullable=False, default="r1")
    retention_class: Mapped[str] = mapped_column(String(24), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lineage_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    sealed: Mapped[bool] = mapped_column(default=False, nullable=False)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Outbox / delivery state.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="collected", index=True)
    held_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    destination_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_category: Mapped[str | None] = mapped_column(String(48), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class DataExportBatch(Base):
    """An immutable sealed export batch + its durable delivery state (Part 11)."""

    __tablename__ = "data_export_batches"
    __table_args__ = (
        UniqueConstraint("destination_id", "idempotency_key", name="uq_data_batch_idem"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_pseudonym: Mapped[str] = mapped_column(String(80), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uncompressed_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    compressed_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    records_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    consent_summary_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    retention_summary_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    encryption_metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    relative_object_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    claim_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_category: Mapped[str | None] = mapped_column(String(48), nullable=True)
    dead_letter_reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class DataExportDestination(Base):
    """A configured export destination (sanitized config; NEVER a secret/token/key/raw path)."""

    __tablename__ = "data_export_destinations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="configured")
    config_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    readiness: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    last_receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class DataDeliveryAttempt(Base):
    """Per-attempt audit for one batch delivery attempt."""

    __tablename__ = "data_delivery_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    destination_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_category: Mapped[str | None] = mapped_column(String(48), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(320), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class DataDeletionRequest(Base):
    """A traceable deletion request + its execution status / receipt (Part 21)."""

    __tablename__ = "data_deletion_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    subject_pseudonym: Mapped[str | None] = mapped_column(String(80), nullable=True)
    category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="requested", index=True)
    hold: Mapped[bool] = mapped_column(default=False, nullable=False)
    local_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remote_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    impact_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class DataDataset(Base):
    """A registered dataset for consented training data (node-side registry)."""

    __tablename__ = "data_datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    tenant_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class DataDatasetVersion(Base):
    """An immutable dataset version with reproducible membership + lineage (Part 18)."""

    __tablename__ = "data_dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version", name="uq_data_dataset_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    tenant_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_policy_version: Mapped[str] = mapped_column(String(40), nullable=False, default="s1")
    consent_policy_versions_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    redaction_policy_version: Mapped[str] = mapped_column(String(16), nullable=False, default="r1")
    source_batch_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    category_counts_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    date_range_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    hardware_families_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    outcome_distribution_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    content_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    manifest_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    exclusions_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    split_label: Mapped[str | None] = mapped_column(String(24), nullable=True)
    deletion_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class DataDatasetMember(Base):
    """A bounded reproducible membership row linking a dataset version to a source record."""

    __tablename__ = "data_dataset_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    version_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    record_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    excluded: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Mesh(Base):
    """A first-class mesh: the explicit trust group that authorizes peer communication (beta.3).

    Public-key trust authenticates a peer's identity; mesh membership authorizes whether two
    authenticated nodes may exchange messages at all. A mesh is bound to exactly one authority:
    a hosted ``tenant`` (``authority_id`` = tenant id) or a ``standalone`` owner (``authority_id``
    = the owner node's fingerprint). The authority makes cross-tenant spoofing impossible — a
    foreign tenant/owner id never matches a local mesh. Created locally (works fully offline) and,
    in hosted mode, synchronized through the control plane. Never stores a private key.
    """

    __tablename__ = "meshes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    authority_type: Mapped[str] = mapped_column(String(16), default="standalone", nullable=False)
    authority_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    owner_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scopes_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    revoked: Mapped[bool] = mapped_column(default=False, nullable=False)
    synced: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class MeshMember(Base):
    """One node's membership in a :class:`Mesh` (additive; never holds a private key).

    The local node is itself a member row. A node is reachable inside a mesh only while it has a
    non-revoked member row with a pinned public key. ``scopes_json`` (when non-empty) narrows the
    member below the mesh default scopes.
    """

    __tablename__ = "mesh_members"
    __table_args__ = (UniqueConstraint("mesh_id", "node_id", name="uq_mesh_member"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    mesh_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default="member", nullable=False)
    scopes_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_self: Mapped[bool] = mapped_column(default=False, nullable=False)
    revoked: Mapped[bool] = mapped_column(default=False, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
