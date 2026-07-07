"""The node runtime: owns configuration, state-store sessions, and the event bus.

``NodeRuntime`` is the single coordination point for node-level operations. Stage 1
implements configuration-driven persistence of missions and events plus live event
publication. Later stages extend this object with the coordinator agent, coding agent,
and GNU Radio MCP access; those are declared as explicit hooks below rather than being
faked here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from aithernet import __version__
from aithernet.artifacts.service import ArtifactService
from aithernet.artifacts.worker import ArtifactTransferWorker
from aithernet.coding_agent.contracts import (
    CodingAgentConfigurationError,
    CodingAgentProviderError,
    CodingAgentStatus,
    CodingTaskExecutionInput,
    CodingTaskExecutionResult,
    CodingTaskNotFoundError,
)
from aithernet.coding_agent.runtime import CodingAgentRuntime
from aithernet.comms.service import CommunicationService
from aithernet.comms.status_service import MissionStatusService
from aithernet.communication.contracts import (
    ExternalAgentCreate,
    ExternalAgentMessageCreate,
    ExternalAgentMessageRead,
    ExternalAgentMessageResponse,
    ExternalAgentRead,
    ExternalAgentStatus,
    ExternalAgentStatusValue,
)
from aithernet.communication.runtime import CommunicationRuntime
from aithernet.config.settings import NodeConfig
from aithernet.coordinator.contracts import (
    ACTIVE_CAPABILITIES,
    FUTURE_CAPABILITIES,
    CoordinatorDecision,
    CoordinatorError,
    CoordinatorInput,
    MissionNotFoundError,
)
from aithernet.coordinator.runtime import CoordinatorRuntime
from aithernet.data.service import DataPlatformService
from aithernet.field.service import FieldService
from aithernet.gnuradio.context import GNURadioContextService
from aithernet.hardware.inventory import InventoryService, InventoryWorker
from aithernet.hardware.leases import LeaseService
from aithernet.hardware.qualification import QualificationService
from aithernet.hardware.registry import HardwareDiscoveryRegistry
from aithernet.interop.service import InteropService
from aithernet.mcp.contracts import (
    MCPConfigurationError,
    MCPError,
    MCPSchemaValidationError,
    MCPSessionStatus,
    MCPToolInfo,
)
from aithernet.mcp.runtime import MCPRuntime
from aithernet.missions.worker import MissionWorkerManager
from aithernet.ops.readiness import ComponentState, ReadinessTracker
from aithernet.orchestrator.decision_router import (
    ROUTE_CODING_AGENT,
    ROUTE_CONTINUE,
    ROUTE_MCP,
    ROUTE_NODE_STATE,
    ROUTE_PEER_ARTIFACT,
    ROUTE_PEER_MESSAGE,
    ROUTE_RESPOND,
    ROUTE_RF_DEVICE,
    ROUTE_RF_MCP,
    CoordinatorRouteResult,
    RouteValidationError,
    coding_task_payload_from_decision,
    mcp_call_payload_from_decision,
    normalize_target,
    peer_artifact_from_decision,
    peer_message_from_decision,
    rf_call_from_decision,
    rf_device_from_decision,
)
from aithernet.orchestrator.event_bus import EventBus
from aithernet.rf.benchmark import RFBenchmarkRunner
from aithernet.rf.context import RFWorkspaceContextService
from aithernet.rf.registry import RFBackendRegistry
from aithernet.schemas.coding import (
    CodingTaskCreate,
    CodingTaskRead,
    CodingTaskResultRead,
    CodingTaskStatus,
)
from aithernet.schemas.events import EventRead
from aithernet.schemas.mcp import MCPToolCallCreate, MCPToolCallRead, MCPToolCallStatus
from aithernet.schemas.mission_runs import (
    MissionExecutionRunRead,
    MissionExecutionStatus,
    MissionTimeline,
    MissionTimelineEntry,
    MissionWorkerStatus,
)
from aithernet.schemas.mission_steps import (
    MissionStepRead,
    MissionStepRunResponse,
    MissionStepStatus,
)
from aithernet.schemas.missions import MissionCreate, MissionRead, MissionStatus
from aithernet.schemas.node import NodeStatus
from aithernet.state.db import create_db_engine, create_session_factory, init_db
from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import (
    CodingTaskRepository,
    CodingTaskResultRepository,
    EventRepository,
    MCPToolCallRepository,
    MissionExecutionRunRepository,
    MissionRepository,
    MissionStepRepository,
)
from aithernet.transport.service import TransportService

#: Capability handle the coordinator sees when the coding agent is ready.
CODING_AGENT_CAPABILITY = "coding_agent"

#: Capability handle for GNU Radio MCP, shared by coordinator/coding-agent capability views.
MCP_CAPABILITY = "gnuradio_mcp"

#: Event type emitted when a mission is accepted by the node.
EVENT_MISSION_RECEIVED = "mission.received"

#: Coordinator lifecycle event types.
EVENT_COORDINATOR_PROCESSING_STARTED = "coordinator.processing_started"
EVENT_COORDINATOR_DECISION = "coordinator.decision"
EVENT_COORDINATOR_FAILED = "coordinator.failed"

#: Coding-task lifecycle event types.
EVENT_CODING_TASK_CREATED = "coding_task.created"
EVENT_CODING_TASK_STARTED = "coding_task.started"
EVENT_CODING_TASK_COMPLETED = "coding_task.completed"
EVENT_CODING_TASK_FAILED = "coding_task.failed"

#: MCP tool-list and tool-call lifecycle event types.
EVENT_MCP_TOOL_LIST_REQUESTED = "mcp.tool_list.requested"
EVENT_MCP_TOOL_LIST_COMPLETED = "mcp.tool_list.completed"
EVENT_MCP_TOOL_LIST_FAILED = "mcp.tool_list.failed"
EVENT_MCP_TOOL_CALL_CREATED = "mcp.tool_call.created"
EVENT_MCP_TOOL_CALL_STARTED = "mcp.tool_call.started"
EVENT_MCP_TOOL_CALL_COMPLETED = "mcp.tool_call.completed"
EVENT_MCP_TOOL_CALL_FAILED = "mcp.tool_call.failed"

#: Mission-step (coordinator decision routing) lifecycle event types.
EVENT_MISSION_STEP_STARTED = "mission_step.started"
EVENT_MISSION_STEP_COMPLETED = "mission_step.completed"
EVENT_MISSION_STEP_FAILED = "mission_step.failed"
EVENT_MISSION_STEP_BLOCKED = "mission_step.blocked"

#: Mission-step terminal status -> event type.
_MISSION_STEP_EVENT_BY_STATUS = {
    MissionStepStatus.COMPLETED.value: EVENT_MISSION_STEP_COMPLETED,
    MissionStepStatus.FAILED.value: EVENT_MISSION_STEP_FAILED,
    MissionStepStatus.BLOCKED.value: EVENT_MISSION_STEP_BLOCKED,
}

#: Number of recent events fed to the coordinator as context.
_RECENT_EVENT_WINDOW = 20

#: MCP session events after which the derived GNU Radio context becomes stale.
_STALE_ON_SESSION_EVENTS = frozenset(
    {
        "mcp.session.restarting",
        "mcp.session.degraded",
        "mcp.session.failed",
        "mcp.session.stopped",
    }
)

#: How much captured task output to retain in a mission-step result preview.
_OUTPUT_PREVIEW = 2000


def _preview(text: str, limit: int = _OUTPUT_PREVIEW) -> str:
    """Trim captured text to a bounded preview for persisted step results."""
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def _elapsed_ms(started: datetime) -> int:
    """Whole milliseconds elapsed since ``started`` (for tool-call duration)."""
    return int((utcnow() - started).total_seconds() * 1000)


class NodeRuntime:
    """Owns the lifecycle and state access for a single node."""

    def __init__(
        self,
        config: NodeConfig,
        engine: Engine,
        session_factory: sessionmaker[Session],
        coordinator: CoordinatorRuntime,
        coding_agent: CodingAgentRuntime,
        mcp: MCPRuntime,
    ) -> None:
        self.config = config
        self.engine = engine
        self.session_factory = session_factory
        self.coordinator = coordinator
        self.coding_agent = coding_agent
        self.mcp = mcp
        self.communication = CommunicationRuntime(self)
        self.gnuradio = GNURadioContextService(self)
        # Stage 12: the autonomous mission worker manager (started/stopped by the API
        # lifespan). Constructed here so CLI/API share one manager per runtime.
        self.worker_manager = MissionWorkerManager(self)
        # Stage 13A: authenticated inter-node agent transport (identity, peers, durable
        # outbox/inbox, delivery worker). Started/stopped by the API lifespan.
        self.transport = TransportService(self)
        # Stage 13A.5: multi-RF-backend registry. The legacy GNU Radio backend reuses the
        # MCP session above; additional backends (Marconi) get their own subprocess. The
        # generic RF workspace context is built first because the registry's adapters use it.
        self.rf_context = RFWorkspaceContextService(self)
        self.rf = RFBackendRegistry(self)
        self.rf_benchmark = RFBenchmarkRunner(self)
        # Stage 13B: coordinator-driven peer messaging + correlated replies + mission
        # resumption (application layer on the Stage 13A signed transport).
        self.comms = CommunicationService(self)
        # Stage 13D.2: authenticated cross-node RF artifact transfer (managed store + signed
        # control protocol + dedicated streaming endpoint + background transfer worker). The
        # worker is independent of the mission and RF MCP workers.
        self.artifacts = ArtifactService(self)
        self.artifact_worker = ArtifactTransferWorker(self)
        # Stage 13D.3: authenticated remote mission-status synchronization (a node reports its
        # own mission lifecycle to the requesting peer over the SAME signed durable outbox).
        self.mission_status = MissionStatusService(self)
        # Stage 14B: managed SDR hardware inventory + leasing. The discovery registry, inventory
        # service, and lease service are INDEPENDENT of the mission worker; the inventory worker is
        # started/stopped by the API lifespan. Off by default (config.hardware.enabled=False), so a
        # node with no hardware stays fully usable for simulation/coding/coordination/artifacts.
        self.hardware_discovery = HardwareDiscoveryRegistry(self.config.hardware)
        self.hardware_leases = LeaseService(self)
        self.hardware_inventory = InventoryService(self)
        self.hardware_inventory.leases = self.hardware_leases
        self.hardware_worker = InventoryWorker(self)
        # Stage 14C.1: real-hardware qualification + RF capture (operator/qualification-invoked;
        # RF execution runs in a separately configured GNU Radio interpreter, never this venv).
        self.hardware_qualification = QualificationService(self)
        self.hardware_capture = self.hardware_qualification.capture
        # Stage 14C.2: multi-node field-operation + soak validation (operator-invoked; reuses the
        # hardware/transport/artifact subsystems; sampling/lease-cycling create no MissionStep).
        self.field = FieldService(self)
        # Stage 14D: external-agent interoperability gateway (durable signed callbacks + WS).
        # Off by default; notification fan-out creates NO MissionStep.
        self.interop = InteropService(self)
        # Stage 14E: privacy-preserving telemetry/consent/export platform. Local-first; export
        # disabled + paused by default; collection/export create NO MissionStep.
        self.data = DataPlatformService(self)
        self.event_bus = EventBus()
        self.version = __version__
        self.started_at: datetime = utcnow()
        self.runtime_status = "running"
        # Stage 14A: readiness/liveness tracker. Migrations + repositories are already done by
        # from_config (init_db ran before this runtime was constructed), so they are READY now;
        # the workers/RF are marked by the lifespan as they start. Required components gate
        # readiness; an experimental non-autostart backend never does.
        self.readiness = ReadinessTracker()
        self._register_readiness_components()
        # Wire the managed MCP session to this node's identity + sanitized event publisher
        # (done post-construction because the emitter needs the event bus + DB).
        if self.mcp.session is not None:
            self.mcp.session.node_id = self.config.node_id
            self.mcp.session.set_event_emitter(self._emit_session_event)

    # -- construction ------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: NodeConfig,
        *,
        coordinator: CoordinatorRuntime | None = None,
        coding_agent: CodingAgentRuntime | None = None,
        mcp: MCPRuntime | None = None,
    ) -> NodeRuntime:
        """Build a runtime (engine, schema, session factory, agents) from config.

        ``coordinator``, ``coding_agent``, and ``mcp`` may be supplied to inject test
        doubles; otherwise they are built from the corresponding config sections.
        """
        engine = create_db_engine(config.database_url)
        init_db(engine)
        session_factory = create_session_factory(engine)
        if coordinator is None:
            coordinator = CoordinatorRuntime.from_config(config.coordinator)
        if coding_agent is None:
            coding_agent = CodingAgentRuntime.from_config(config.coding_agent)
        if mcp is None:
            mcp = MCPRuntime.from_config(config.gnuradio_mcp)
        return cls(config, engine, session_factory, coordinator, coding_agent, mcp)

    def _register_readiness_components(self) -> None:
        """Register the components that gate readiness, from config (Stage 14A)."""
        r = self.readiness
        # Migrations + repositories already completed in from_config before this runtime existed.
        r.register("migrations", required=True)
        r.mark("migrations", ComponentState.READY)
        r.register("repositories", required=True)
        r.mark("repositories", ComponentState.READY)
        mission_enabled = self.config.mission_execution.enabled
        r.register("mission_worker", required=mission_enabled)
        if not mission_enabled:
            r.mark("mission_worker", ComponentState.DISABLED)
        transport_enabled = (
            self.config.agent_transport.enabled and self.config.agent_transport.outbound.enabled
        )
        r.register("transport_worker", required=transport_enabled)
        if not transport_enabled:
            r.mark("transport_worker", ComponentState.DISABLED)
        # The default RF backend gates readiness only when explicitly required; otherwise its
        # failure degrades (never downs) the node. An experimental backend is never required.
        r.register("rf_default_backend", required=self.config.rf_backends.require_default_ready)
        # Stage 14B: the hardware inventory service. A general node with no attached device is NOT
        # unready by default; the inventory component is required only when hardware is enabled
        # (a missing required device/provider is gated by separate required-component marks the
        # inventory worker registers at startup). When hardware is off it is DISABLED.
        hardware_enabled = self.config.hardware.enabled and bool(self.config.hardware.providers)
        r.register("hardware_inventory", required=hardware_enabled)
        if not hardware_enabled:
            r.mark("hardware_inventory", ComponentState.DISABLED)

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        """Provide a transactional session, rolling back on error."""
        session = self.session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- mission operations ------------------------------------------------------

    async def create_mission(self, payload: MissionCreate) -> MissionRead:
        """Persist a mission, record a ``mission.received`` event, and publish it.

        Mission and event are committed in a single transaction so the state store is
        never left with one without the other. The event is published to the bus only
        after a successful commit.
        """
        with self.session_scope() as session:
            missions = MissionRepository(session)
            events = EventRepository(session)

            mission = missions.create(
                content=payload.content,
                source_type=payload.source_type,
                source_id=payload.source_id,
                status=MissionStatus.RECEIVED.value,
                metadata=payload.metadata,
            )
            event = events.create(
                event_type=EVENT_MISSION_RECEIVED,
                source="runtime",
                message=f"Mission {mission.id} received from {payload.source_type}.",
                mission_id=mission.id,
                payload={"source_type": payload.source_type, "source_id": payload.source_id},
            )
            session.commit()

            mission_read = MissionRead.model_validate(mission)
            event_payload = EventRead.model_validate(event).model_dump(by_alias=True, mode="json")

        await self.event_bus.publish(event_payload)
        return mission_read

    def list_missions(self, *, limit: int = 100, offset: int = 0) -> list[MissionRead]:
        """Return persisted missions, newest first."""
        with self.session_scope() as session:
            rows = MissionRepository(session).list(limit=limit, offset=offset)
            return [MissionRead.model_validate(row) for row in rows]

    def get_mission(self, mission_id: str) -> MissionRead | None:
        """Return a single mission, or ``None`` if it does not exist."""
        with self.session_scope() as session:
            row = MissionRepository(session).get(mission_id)
            return MissionRead.model_validate(row) if row is not None else None

    # -- coordinator operations --------------------------------------------------

    def coordinator_capabilities(self) -> tuple[list[str], list[str]]:
        """Return ``(active, future)`` capability lists for coordinator context.

        ``coding_agent`` and ``gnuradio_mcp`` are promoted from future to active only when
        the respective runtime is actually configured and ready; otherwise they stay in
        the future set so the coordinator never assumes it can use them yet. Stage 4 does
        not auto-execute decisions that mention these capabilities.
        """
        active = list(ACTIVE_CAPABILITIES)
        future = list(FUTURE_CAPABILITIES)
        if self.coding_agent.is_configured():
            active.append(CODING_AGENT_CAPABILITY)
            future = [cap for cap in future if cap != CODING_AGENT_CAPABILITY]
        if self.mcp.is_configured():
            active.append(MCP_CAPABILITY)
            future = [cap for cap in future if cap != MCP_CAPABILITY]
        return active, future

    def coding_agent_status(self) -> CodingAgentStatus:
        """Coding-agent status, with GNU Radio MCP reflected as a node capability.

        The provider's own status is returned unchanged except that, when the MCP runtime
        is configured, ``gnuradio_mcp`` moves from the coding agent's *future* set into its
        *active* set — reflecting that it is now an available node capability. Per-task
        access is still gated: a task only sees MCP when its creator includes it.
        """
        status = self.coding_agent.status()
        if not self.mcp.is_configured():
            return status
        future = [cap for cap in status.future_capabilities if cap != MCP_CAPABILITY]
        active = list(status.active_capabilities)
        if MCP_CAPABILITY not in active:
            active.append(MCP_CAPABILITY)
        return status.model_copy(
            update={"active_capabilities": active, "future_capabilities": future}
        )

    async def invoke_coordinator(
        self, mission_id: str, *, extra_context: dict | None = None
    ) -> CoordinatorDecision:
        """Run one coordinator reasoning step and emit its lifecycle events only.

        Emits ``coordinator.processing_started`` then ``coordinator.decision`` (or
        ``coordinator.failed`` on a :class:`CoordinatorError`, which is re-raised). It does
        NOT mutate mission status — callers own lifecycle. ``extra_context`` carries the
        compact autonomous-run context (Stage 12); it is empty for the manual one-step path.
        """
        with self.session_scope() as session:
            mission_row = MissionRepository(session).get(mission_id)
            if mission_row is None:
                raise MissionNotFoundError(f"Mission {mission_id} not found.")
            mission = MissionRead.model_validate(mission_row)
            recent_rows = EventRepository(session).list(
                mission_id=mission_id, limit=_RECENT_EVENT_WINDOW
            )
            recent_events = [EventRead.model_validate(row) for row in recent_rows]

        node_status = self.status()

        await self._emit_event(
            event_type=EVENT_COORDINATOR_PROCESSING_STARTED,
            mission_id=mission_id,
            source="coordinator",
            message=f"Coordinator processing started for mission {mission_id}.",
            payload={"provider": self.coordinator.config.provider},
        )

        active_capabilities, future_capabilities = self.coordinator_capabilities()
        coordinator_input = CoordinatorInput.from_node_context(
            mission=mission,
            node_status=node_status,
            recent_events=recent_events,
            current_time=utcnow(),
            available_capabilities=active_capabilities,
            future_capabilities=future_capabilities,
            mcp_session=await self._coordinator_mcp_context(),
            gnuradio_context=self.gnuradio.current().compact(),
            rf_backends=await self._coordinator_rf_context(),
            peers=self._coordinator_peer_context(),
            mission_run_context=extra_context or {},
        )

        try:
            decision = await self.coordinator.decide(coordinator_input)
        except CoordinatorError as exc:
            await self._emit_event(
                event_type=EVENT_COORDINATOR_FAILED,
                mission_id=mission_id,
                source="coordinator",
                message=f"Coordinator failed for mission {mission_id}: {exc}",
                payload={
                    "provider": self.coordinator.config.provider,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise

        await self._emit_event(
            event_type=EVENT_COORDINATOR_DECISION,
            mission_id=mission_id,
            source="coordinator",
            message=decision.summary,
            payload=decision.model_dump(mode="json"),
        )
        return decision

    async def process_mission_with_coordinator(self, mission_id: str) -> CoordinatorDecision:
        """Run one coordinator reasoning step for a mission and persist the outcome.

        Records ``coordinator.processing_started`` before reasoning, then either
        ``coordinator.decision`` (advancing a ``received`` mission to ``active``) or
        ``coordinator.failed`` (marking an in-flight mission ``failed``). Coordinator
        errors are re-raised for the API/CLI to surface clearly.
        """
        with self.session_scope() as session:
            mission_row = MissionRepository(session).get(mission_id)
            if mission_row is None:
                raise MissionNotFoundError(f"Mission {mission_id} not found.")
            status_before = MissionRead.model_validate(mission_row).status

        try:
            decision = await self.invoke_coordinator(mission_id)
        except CoordinatorError:
            # Only fail a mission that was actively in flight; never override a terminal
            # status reached elsewhere.
            if status_before in (MissionStatus.RECEIVED, MissionStatus.ACTIVE):
                self._set_mission_status(mission_id, MissionStatus.FAILED)
            raise

        if status_before == MissionStatus.RECEIVED:
            self._set_mission_status(mission_id, MissionStatus.ACTIVE)

        return decision

    # -- coding-agent operations -------------------------------------------------

    async def create_coding_task(self, payload: CodingTaskCreate) -> CodingTaskRead:
        """Persist a coding task and emit a ``coding_task.created`` event."""
        with self.session_scope() as session:
            tasks = CodingTaskRepository(session)
            events = EventRepository(session)

            task = tasks.create(
                objective=payload.objective,
                mission_id=payload.mission_id,
                context=payload.context,
                available_tools=payload.available_tools,
                expected_outputs=payload.expected_outputs,
                reporting_requirements=payload.reporting_requirements,
                provider=self.coding_agent.config.provider,
                status=CodingTaskStatus.CREATED.value,
            )
            event = events.create(
                event_type=EVENT_CODING_TASK_CREATED,
                source="coding_agent",
                message=f"Coding task {task.id} created.",
                mission_id=task.mission_id,
                payload={"task_id": task.id, "provider": task.provider},
            )
            session.commit()

            task_read = CodingTaskRead.model_validate(task)
            event_payload = EventRead.model_validate(event).model_dump(by_alias=True, mode="json")

        await self.event_bus.publish(event_payload)
        return task_read

    def list_coding_tasks(self, *, limit: int = 100, offset: int = 0) -> list[CodingTaskRead]:
        """Return persisted coding tasks, newest first."""
        with self.session_scope() as session:
            rows = CodingTaskRepository(session).list(limit=limit, offset=offset)
            return [CodingTaskRead.model_validate(row) for row in rows]

    def get_coding_task(self, task_id: str) -> CodingTaskRead | None:
        """Return a single coding task, or ``None`` if it does not exist."""
        with self.session_scope() as session:
            row = CodingTaskRepository(session).get(task_id)
            return CodingTaskRead.model_validate(row) if row is not None else None

    def get_coding_task_result(self, task_id: str) -> CodingTaskResultRead | None:
        """Return the latest result for a task, or ``None`` if there is none."""
        with self.session_scope() as session:
            if CodingTaskRepository(session).get(task_id) is None:
                raise CodingTaskNotFoundError(f"Coding task {task_id} not found.")
            row = CodingTaskResultRepository(session).get_latest_for_task(task_id)
            return CodingTaskResultRead.model_validate(row) if row is not None else None

    async def run_coding_task(self, task_id: str) -> CodingTaskResultRead:
        """Execute a coding task through the coding agent and persist the outcome.

        Marks the task ``running`` and emits ``coding_task.started`` before execution,
        then persists a result and emits ``coding_task.completed`` or
        ``coding_task.failed``. Infrastructure/configuration failures are recorded as a
        failed result and re-raised for the API/CLI to surface clearly; a task that runs
        but exits non-zero is a normal ``failed`` result (no exception).
        """
        with self.session_scope() as session:
            task_row = CodingTaskRepository(session).get(task_id)
            if task_row is None:
                raise CodingTaskNotFoundError(f"Coding task {task_id} not found.")
            task = CodingTaskRead.model_validate(task_row)

        self._set_task_status(task_id, CodingTaskStatus.RUNNING, started_at=utcnow())
        await self._emit_event(
            event_type=EVENT_CODING_TASK_STARTED,
            mission_id=task.mission_id,
            source="coding_agent",
            message=f"Coding task {task_id} started.",
            payload={"task_id": task_id, "provider": self.coding_agent.config.provider},
        )

        # Each coding task runs in its OWN absolute, bounded workspace beneath the canonical coding
        # root (config.coding_agent.workspace, made absolute by setup) — per-task isolation +
        # fail-closed containment.
        from aithernet.coding_agent.runtime import bounded_task_workspace
        task_ws = bounded_task_workspace(self.coding_agent.config.workspace, task_id)
        execution_input = CodingTaskExecutionInput.from_task(
            task, workspace=task_ws, current_time=utcnow()
        )

        try:
            execution_result = await self.coding_agent.execute(execution_input)
        except Exception as exc:
            # Domain errors (CodingAgentError) AND any unexpected exception are recorded as
            # a failed result with the task marked failed, so a task never stays `running`
            # after a crash and a failure is never reported as completed.
            result = self._persist_coding_result(
                task_id,
                CodingTaskExecutionResult(
                    status=CodingTaskStatus.FAILED.value,
                    summary=str(exc),
                    payload={"error": str(exc), "error_type": type(exc).__name__},
                ),
            )
            self._set_task_status(task_id, CodingTaskStatus.FAILED, completed_at=utcnow())
            await self._emit_event(
                event_type=EVENT_CODING_TASK_FAILED,
                mission_id=task.mission_id,
                source="coding_agent",
                message=f"Coding task {task_id} failed: {exc}",
                payload={
                    "task_id": task_id,
                    "result_id": result.id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise

        result = self._persist_coding_result(task_id, execution_result)
        succeeded = execution_result.status == CodingTaskStatus.COMPLETED.value
        final_status = CodingTaskStatus.COMPLETED if succeeded else CodingTaskStatus.FAILED
        self._set_task_status(task_id, final_status, completed_at=utcnow())
        await self._emit_event(
            event_type=EVENT_CODING_TASK_COMPLETED if succeeded else EVENT_CODING_TASK_FAILED,
            mission_id=task.mission_id,
            source="coding_agent",
            message=execution_result.summary or f"Coding task {task_id} {final_status.value}.",
            payload={
                "task_id": task_id,
                "result_id": result.id,
                "status": final_status.value,
                "exit_code": execution_result.exit_code,
            },
        )
        return result

    async def create_and_run_coding_task(
        self, payload: CodingTaskCreate
    ) -> tuple[CodingTaskRead, CodingTaskResultRead]:
        """Create a coding task and immediately execute it, returning both.

        The returned task reflects its post-run state (status/timestamps), not the
        creation-time snapshot.
        """
        created = await self.create_coding_task(payload)
        result = await self.run_coding_task(created.id)
        task = self.get_coding_task(created.id) or created
        return task, result

    # -- MCP session lifecycle (Stage 11B) ---------------------------------------

    async def _emit_session_event(self, event_type: str, message: str, payload: dict) -> None:
        """Sanitized event publisher wired into the managed MCP session manager.

        A new generation or a lost session means any derived GNU Radio context is stale
        until refreshed — so a restart/degraded/failed/stopped event marks it stale.
        """
        if event_type in _STALE_ON_SESSION_EVENTS:
            try:
                self.gnuradio.mark_stale(f"mcp session event: {event_type}")
            except Exception:  # never let context bookkeeping break event emission
                pass
        await self._emit_event(
            event_type=event_type, mission_id=None, source="mcp", message=message, payload=payload
        )

    async def _coordinator_mcp_context(self) -> dict:
        """A compact MCP-session capability summary for the coordinator (no full schemas)."""
        status = await self.mcp.session_status()
        tools: list[str] = []
        if self.mcp.session is not None:
            tools = sorted(self.mcp.session.cached_tool_names())
        return {
            "state": status.state.value,
            "configured": status.configured,
            "generation": status.generation,
            "tool_count": status.tool_count,
            # Compact capability summary: discovered tool NAMES only (never input schemas).
            "tools": tools,
        }

    async def _coordinator_rf_context(self) -> dict:
        """A compact, dynamic RF-backend summary for the coordinator (Stage 13A.5, Part K).

        Lists every configured backend with its state, experimental flag, default flag,
        discovered tool NAMES (when ready, bounded), and a small RF context summary. It is
        descriptive only: it never tells the model which backend to choose, dumps no full
        schemas/results/captures/plots, and adds no keyword routing.
        """
        backends: list[dict] = []
        for status in await self.rf.list_statuses():
            entry = {
                "backend_id": status.backend_id,
                "display_name": status.display_name,
                "state": status.state.value,
                "experimental": status.experimental,
                "default": status.default,
                "tool_count": status.tool_count,
                "version": status.version,
            }
            backend = self.rf.get_backend(status.backend_id)
            if status.state.value == "ready":
                # Discovered tool names only (bounded), never input schemas.
                entry["tools"] = sorted(backend.session.cached_tool_names())[:40]
            # Compact RF context summary (counts/status only).
            if backend.is_legacy:
                entry["context"] = self.gnuradio.current().compact()
            else:
                entry["context"] = self.rf_context.compact(status.backend_id)
            backends.append(entry)
        return {
            "default_backend": self.config.rf_backends.default_backend,
            "backends": backends,
        }

    def _coordinator_peer_context(self) -> dict:
        """A compact trusted-peer summary for the coordinator (Stage 13B, Part E).

        Descriptive only: it lists trusted, communication-authorized peers with role, trust
        state, authorization flags, endpoint health, and high-level capabilities — never keys,
        manifests, signatures, or full histories. It never tells the model which peer to pick.
        """
        with suppress(Exception):
            return self.comms.coordinator_peer_context()
        return {}

    async def start_mcp_session(self) -> MCPSessionStatus:
        """Start the persistent MCP session (idempotent); never raises on failure."""
        return await self.mcp.start_session()

    async def stop_mcp_session(self) -> MCPSessionStatus:
        """Stop the persistent MCP session and clean up the subprocess."""
        return await self.mcp.stop_session()

    async def restart_mcp_session(self) -> MCPSessionStatus:
        """Restart the persistent MCP session (new generation, rediscovered tools)."""
        return await self.mcp.restart_session()

    async def mcp_session_status(self) -> MCPSessionStatus:
        """Return the managed MCP session status (secret-free)."""
        return await self.mcp.session_status()

    async def close_mcp_session(self) -> None:
        """Close the persistent MCP session on node shutdown (no orphan process)."""
        await self.mcp.close_session()

    # -- MCP operations ----------------------------------------------------------

    async def list_mcp_tools(self, *, refresh: bool = False) -> list[MCPToolInfo]:
        """List tools from the managed MCP session, emitting audit events.

        Uses the persistent session's cached catalog; ``refresh=True`` re-queries the live
        server and replaces the cache. Emits ``mcp.tool_list.requested`` then
        ``mcp.tool_list.completed`` on success or ``mcp.tool_list.failed`` on error.
        """
        await self._emit_event(
            event_type=EVENT_MCP_TOOL_LIST_REQUESTED,
            mission_id=None,
            source="mcp",
            message="MCP tool list requested.",
            payload={"provider": self.mcp.config.provider, "refresh": refresh},
        )
        try:
            tools = await self.mcp.list_tools(refresh=refresh)
        except MCPError as exc:
            await self._emit_event(
                event_type=EVENT_MCP_TOOL_LIST_FAILED,
                mission_id=None,
                source="mcp",
                message=f"MCP tool list failed: {exc}",
                payload={"error": str(exc), "error_type": type(exc).__name__},
            )
            raise

        await self._emit_event(
            event_type=EVENT_MCP_TOOL_LIST_COMPLETED,
            mission_id=None,
            source="mcp",
            message=f"MCP tool list returned {len(tools)} tool(s).",
            payload={"count": len(tools), "tools": [tool.name for tool in tools]},
        )
        return tools

    def _preflight_tool_arguments(self, tool_name: str, arguments: dict) -> dict:
        """beta.10 Defect 5: validate/canonicalize proposed args against the tool's cached schema.

        Returns the canonicalized arguments to dispatch. Raises ``MCPSchemaValidationError`` (with a
        normalized correction) when an authoritative schema rejects the call. Conservative: with no
        cached schema, returns the arguments unchanged."""
        from aithernet.mcp.contracts import MCPSchemaValidationError
        from aithernet.mcp.schema_guard import preflight_tool_call

        tool_info = None
        if self.mcp.session is not None:
            tool_info = next((t for t in getattr(self.mcp.session, "_tools", [])
                              if t.name == tool_name), None)
        guard = preflight_tool_call(tool_info, tool_name, arguments)
        if not guard.ok:
            raise MCPSchemaValidationError(
                guard.correction, tool_name=tool_name, schema_digest=guard.schema_digest)
        return guard.arguments

    async def call_mcp_tool(self, payload: MCPToolCallCreate) -> MCPToolCallRead:
        """Call an MCP tool, persisting an auditable record and lifecycle events.

        Persists a ``created`` call, marks it ``running``, invokes the configured MCP
        client, then records the result as ``completed`` (or ``failed`` if the tool itself
        reported an error). Configuration/protocol failures mark the call ``failed`` and
        re-raise for the API/CLI to surface clearly.
        """
        # Capture the session/generation executing this call (audit + recovery linkage).
        session_status = await self.mcp.session_status()
        session_id = session_status.session_id if session_status.session_id != "-" else None
        # Stage 13A.5: stamp the executing RF backend (legacy GNU Radio by default).
        legacy_version = None
        if self.mcp.session is not None:
            legacy_version = self.mcp.session.server_info().get("version")

        with self.session_scope() as session:
            calls = MCPToolCallRepository(session)
            events = EventRepository(session)
            call = calls.create(
                tool_name=payload.tool_name,
                caller=payload.caller,
                arguments=payload.arguments,
                mission_id=payload.mission_id,
                mission_step_id=payload.mission_step_id,
                task_id=payload.task_id,
                session_id=session_id,
                session_generation=session_status.generation or None,
                status=MCPToolCallStatus.CREATED.value,
                backend_id="legacy_gr_mcp",
                backend_kind="mcp_stdio",
                backend_version=legacy_version,
            )
            event = events.create(
                event_type=EVENT_MCP_TOOL_CALL_CREATED,
                source="mcp",
                message=f"MCP tool call {call.id} created for '{payload.tool_name}'.",
                mission_id=payload.mission_id,
                payload={
                    "call_id": call.id,
                    "tool_name": payload.tool_name,
                    "caller": payload.caller,
                    "session_id": session_id,
                    "session_generation": session_status.generation,
                },
            )
            session.commit()
            call_id = call.id
            created_event = EventRead.model_validate(event).model_dump(by_alias=True, mode="json")

        await self.event_bus.publish(created_event)

        started = utcnow()
        self._update_mcp_call(call_id, status=MCPToolCallStatus.RUNNING, started_at=started)
        await self._emit_event(
            event_type=EVENT_MCP_TOOL_CALL_STARTED,
            mission_id=payload.mission_id,
            source="mcp",
            message=f"MCP tool call {call_id} started for '{payload.tool_name}'.",
            payload={"call_id": call_id, "tool_name": payload.tool_name},
        )

        try:
            # beta.10 Defect 5: validate the proposed call against the tool's AUTHORITATIVE schema
            # BEFORE dispatch — canonicalize known aliases, reject unknown/missing fields. A schema
            # rejection (raised here, before any server call) is recorded as a FAILED call with
            # error_type=MCPSchemaValidationError, so a model-generated invalid call is distinct
            # from a runtime tool failure. With no schema, the call passes through unchanged.
            dispatch_args = self._preflight_tool_arguments(payload.tool_name, payload.arguments)
            result = await self.mcp.call_tool(payload.tool_name, dispatch_args)
        except Exception as exc:
            # MCPError (incl. the oversized-frame MCPClientError) AND any unexpected
            # exception mark the call failed with the error recorded, so a call never stays
            # `running`. A failed call is NEVER replayed, even if the session later recovers.
            self._update_mcp_call(
                call_id,
                status=MCPToolCallStatus.FAILED,
                error=str(exc),
                error_type=type(exc).__name__,
                duration_ms=_elapsed_ms(started),
                completed_at=utcnow(),
            )
            await self._emit_event(
                event_type=EVENT_MCP_TOOL_CALL_FAILED,
                mission_id=payload.mission_id,
                source="mcp",
                message=f"MCP tool call {call_id} failed: {exc}",
                payload={
                    "call_id": call_id,
                    "tool_name": payload.tool_name,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise

        succeeded = result.status == "completed"
        final_status = MCPToolCallStatus.COMPLETED if succeeded else MCPToolCallStatus.FAILED
        self._update_mcp_call(
            call_id,
            status=final_status,
            result=result.result,
            error=result.error,
            error_type=None if succeeded else "MCPToolError",
            tool_catalog_generation=session_status.tool_catalog_generation,
            duration_ms=_elapsed_ms(started),
            completed_at=utcnow(),
        )
        await self._emit_event(
            event_type=EVENT_MCP_TOOL_CALL_COMPLETED if succeeded else EVENT_MCP_TOOL_CALL_FAILED,
            mission_id=payload.mission_id,
            source="mcp",
            message=f"MCP tool call {call_id} {final_status.value} for '{payload.tool_name}'.",
            payload={
                "call_id": call_id,
                "tool_name": payload.tool_name,
                "status": final_status.value,
            },
        )

        persisted = self.get_mcp_tool_call(call_id)
        assert persisted is not None  # just written in this method

        # Centralized GNU Radio context interpretation: a successful, recognized tool result
        # updates the relevant compact summary (or marks context stale on a mutation). The
        # context refresh issues its own calls (caller='gnuradio_context') and updates the
        # context itself, so those are skipped here to avoid double-application.
        if payload.caller != "gnuradio_context":
            with suppress(Exception):
                await self.gnuradio.apply_tool_call(persisted)
        return persisted

    def get_mcp_tool_call(self, call_id: str) -> MCPToolCallRead | None:
        """Return a single persisted MCP tool call, or ``None`` if it does not exist."""
        with self.session_scope() as session:
            row = MCPToolCallRepository(session).get(call_id)
            return MCPToolCallRead.model_validate(row) if row is not None else None

    def list_mcp_tool_calls(
        self,
        *,
        mission_id: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MCPToolCallRead]:
        """Return persisted MCP tool calls, newest first, optionally filtered."""
        with self.session_scope() as session:
            rows = MCPToolCallRepository(session).list(
                mission_id=mission_id, task_id=task_id, limit=limit, offset=offset
            )
            return [MCPToolCallRead.model_validate(row) for row in rows]

    # -- mission-step (decision routing) operations ------------------------------

    async def run_mission_step(self, mission_id: str) -> MissionStepRunResponse:
        """Run one coordinator decision and execute its routed step.

        Reuses :meth:`process_mission_with_coordinator` for reasoning (which emits
        ``coordinator.processing_started``/``coordinator.decision``), then interprets the
        decision through the routing contract and executes exactly one supported route.
        The decision and outcome are persisted as a :class:`MissionStep`. A blocked or
        failed route is recorded honestly — never a fake success — and does not mark the
        mission failed.
        """
        with self.session_scope() as session:
            if MissionRepository(session).get(mission_id) is None:
                raise MissionNotFoundError(f"Mission {mission_id} not found.")

        await self._emit_event(
            event_type=EVENT_MISSION_STEP_STARTED,
            mission_id=mission_id,
            source="router",
            message=f"Mission step started for mission {mission_id}.",
            payload={},
        )

        try:
            decision = await self.process_mission_with_coordinator(mission_id)
        except CoordinatorError as exc:
            # Coordinator failed before producing a decision; no step record exists yet.
            await self._emit_event(
                event_type=EVENT_MISSION_STEP_FAILED,
                mission_id=mission_id,
                source="router",
                message=f"Mission step failed before routing: {exc}",
                payload={"step_id": None, "error": str(exc), "error_type": type(exc).__name__},
            )
            raise

        route_target = normalize_target(decision.next_target)
        with self.session_scope() as session:
            step_row = MissionStepRepository(session).create(
                mission_id=mission_id,
                decision_id=decision.decision_id,
                decision=decision.model_dump(mode="json"),
                route_target=route_target,
                route_action=decision.action,
                status=MissionStepStatus.RUNNING.value,
                started_at=utcnow(),
            )
            session.commit()
            step_id = step_row.id

        try:
            route = await self._route_decision(mission_id, decision, route_target, step_id)
        except Exception as exc:
            # Domain errors are turned into blocked/failed routes inside _route_decision;
            # an UNEXPECTED exception must still not strand the step in `running`. Record it
            # as failed (sanitized) and re-raise so the API surfaces a clear error.
            self._update_mission_step(
                step_id,
                status=MissionStepStatus.FAILED.value,
                error=f"Unexpected routing error [{type(exc).__name__}]: {exc}",
                completed_at=utcnow(),
            )
            await self._emit_event(
                event_type=EVENT_MISSION_STEP_FAILED,
                mission_id=mission_id,
                source="router",
                message=f"Mission step {step_id} failed with an unexpected error: {exc}",
                payload={
                    "step_id": step_id,
                    "decision_id": decision.decision_id,
                    "route_target": route_target,
                    "error_type": type(exc).__name__,
                },
            )
            raise

        self._update_mission_step(
            step_id,
            status=route.status,
            result=route.result,
            error=route.error,
            completed_at=utcnow(),
        )
        event_type = _MISSION_STEP_EVENT_BY_STATUS.get(route.status, EVENT_MISSION_STEP_COMPLETED)
        await self._emit_event(
            event_type=event_type,
            mission_id=mission_id,
            source="router",
            message=(
                f"Mission step {step_id} {route.status} "
                f"(target={route.target}, action={decision.action})."
            ),
            payload={
                "step_id": step_id,
                "decision_id": decision.decision_id,
                "route_target": route.target,
                "status": route.status,
            },
        )

        step = self.get_mission_step(step_id)
        mission = self.get_mission(mission_id)
        assert step is not None and mission is not None  # just written/loaded above
        return MissionStepRunResponse(mission=mission, decision=decision, step=step)

    def list_mission_steps(
        self, *, mission_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[MissionStepRead]:
        """Return persisted mission steps, newest first, optionally filtered by mission."""
        with self.session_scope() as session:
            rows = MissionStepRepository(session).list(
                mission_id=mission_id, limit=limit, offset=offset
            )
            return [MissionStepRead.model_validate(row) for row in rows]

    def get_mission_step(self, step_id: str) -> MissionStepRead | None:
        """Return a single mission step, or ``None`` if it does not exist."""
        with self.session_scope() as session:
            row = MissionStepRepository(session).get(step_id)
            return MissionStepRead.model_validate(row) if row is not None else None

    # -- autonomous mission execution (Stage 12) ---------------------------------
    #
    # Thin delegators to the shared worker manager (one per runtime). The manager owns
    # leasing, the atomic loop, recovery, and lifecycle operations. Reads never start work.

    async def start_mission(self, mission_id: str) -> tuple[MissionExecutionRunRead, bool]:
        """Queue a mission for autonomous execution; returns after queueing (not completion)."""
        return await self.worker_manager.enqueue_mission(mission_id)

    async def retry_mission_run(self, mission_id: str) -> tuple[MissionExecutionRunRead, bool]:
        """Retry a blocked/failed mission with a fresh run (no duplicate mission)."""
        return await self.worker_manager.retry_mission(mission_id)

    async def pause_mission_run(self, mission_id: str) -> MissionExecutionRunRead:
        """Request a pause for the mission's active run."""
        return await self.worker_manager.pause_mission(mission_id)

    async def resume_mission_run(self, mission_id: str) -> MissionExecutionRunRead:
        """Resume a paused/waiting mission run (requeue, no replay)."""
        return await self.worker_manager.resume_mission(mission_id)

    async def cancel_mission_run(self, mission_id: str) -> MissionExecutionRunRead:
        """Request cancellation for the mission's active run."""
        return await self.worker_manager.cancel_mission(mission_id)

    async def run_one_autonomous_step(self, mission_id: str) -> MissionExecutionStatus:
        """Run exactly one autonomous iteration (no background loop)."""
        return await self.worker_manager.run_one_step(mission_id)

    def mission_execution_status(self, mission_id: str) -> MissionExecutionStatus | None:
        """Return the combined mission + current run + remaining-budget view."""
        return self.worker_manager.get_execution_status(mission_id)

    def mission_worker_status(self) -> MissionWorkerStatus:
        """Return the autonomous worker manager status (sanitized; no lease tokens)."""
        return self.worker_manager.worker_status()

    def list_mission_runs(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[MissionExecutionRunRead]:
        """Return persisted execution runs, newest first (secret-free; no lease tokens)."""
        with self.session_scope() as session:
            rows = MissionExecutionRunRepository(session).list(limit=limit, offset=offset)
            return [MissionExecutionRunRead.model_validate(row) for row in rows]

    def get_mission_run(self, run_id: str) -> MissionExecutionRunRead | None:
        """Return a single execution run, or ``None`` (secret-free; no lease token)."""
        with self.session_scope() as session:
            row = MissionExecutionRunRepository(session).get(run_id)
            return MissionExecutionRunRead.model_validate(row) if row is not None else None

    def mission_timeline(self, mission_id: str, *, limit: int = 200) -> MissionTimeline | None:
        """Assemble a chronological mission timeline from events + step summaries."""
        with self.session_scope() as session:
            if MissionRepository(session).get(mission_id) is None:
                return None
        entries: list[MissionTimelineEntry] = []
        for event in self.list_events(mission_id=mission_id, limit=limit):
            payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
            entries.append(
                MissionTimelineEntry(
                    at=event.created_at,
                    kind="event",
                    event_type=event.event_type,
                    source=event.source,
                    message=event.message,
                    coding_task_id=payload.get("task_id"),  # beta.7 (FIX 9)
                )
            )
        for step in self.list_mission_steps(mission_id=mission_id, limit=limit):
            entries.append(
                MissionTimelineEntry(
                    at=step.created_at,
                    kind="step",
                    message=(step.decision or {}).get("summary", step.route_action),
                    step_id=step.id,
                    route_target=step.route_target,
                    status=step.status.value,
                )
            )
        entries.sort(key=lambda e: e.at)
        return MissionTimeline(mission_id=mission_id, entries=entries[-limit:])

    # -- routing dispatch (calls back into capability methods) -------------------

    async def _route_decision(
        self,
        mission_id: str,
        decision: CoordinatorDecision,
        target: str,
        step_id: str | None = None,
    ) -> CoordinatorRouteResult:
        """Validate and execute exactly one supported route for ``decision``."""
        if target == ROUTE_RESPOND:
            return CoordinatorRouteResult(
                target=target,
                action=decision.action,
                status=MissionStepStatus.COMPLETED.value,
                result={
                    "type": "response",
                    "summary": decision.summary,
                    "message": decision.message,
                },
            )
        if target == ROUTE_NODE_STATE:
            return CoordinatorRouteResult(
                target=target,
                action=decision.action,
                status=MissionStepStatus.COMPLETED.value,
                result=self._node_state_result(),
            )
        if target == ROUTE_CODING_AGENT:
            return await self._route_coding_agent(mission_id, decision)
        if target == ROUTE_MCP:
            return await self._route_mcp(mission_id, decision, step_id)
        if target == ROUTE_RF_MCP:
            return await self._route_rf_mcp(mission_id, decision, step_id)
        if target == ROUTE_RF_DEVICE:
            return await self._route_rf_device(mission_id, decision, step_id)
        if target == ROUTE_PEER_MESSAGE:
            return await self._route_peer_message(mission_id, decision, step_id)
        if target == ROUTE_PEER_ARTIFACT:
            return await self._route_peer_artifact(mission_id, decision, step_id)
        if target == ROUTE_CONTINUE:
            return CoordinatorRouteResult(
                target=target,
                action=decision.action,
                status=MissionStepStatus.BLOCKED.value,
                error=(
                    "Continuous/autonomous loops are not enabled in Stage 5. "
                    "Use another explicit mission step."
                ),
            )
        return CoordinatorRouteResult(
            target=target,
            action=decision.action,
            status=MissionStepStatus.BLOCKED.value,
            error=(
                f"Unsupported route target '{decision.next_target}'. Supported executable "
                "targets in Stage 5 are: respond, coding_agent, gnuradio_mcp, node_state. "
                "Peer/manager/web-UI targets are not routable yet."
            ),
        )

    async def _route_coding_agent(
        self, mission_id: str, decision: CoordinatorDecision
    ) -> CoordinatorRouteResult:
        """Route a decision to the coding agent: create and run a coding task."""
        action = decision.action
        if not self.coding_agent.is_configured():
            return CoordinatorRouteResult(
                target=ROUTE_CODING_AGENT,
                action=action,
                status=MissionStepStatus.BLOCKED.value,
                error="The coding agent is not configured/ready; cannot route to it.",
            )
        try:
            payload = coding_task_payload_from_decision(decision, mission_id=mission_id)
        except RouteValidationError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_CODING_AGENT,
                action=action,
                status=MissionStepStatus.FAILED.value,
                error=str(exc),
            )

        try:
            task, result = await self.create_and_run_coding_task(payload)
        except CodingAgentConfigurationError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_CODING_AGENT,
                action=action,
                status=MissionStepStatus.BLOCKED.value,
                error=str(exc),
            )
        except CodingAgentProviderError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_CODING_AGENT,
                action=action,
                status=MissionStepStatus.FAILED.value,
                error=str(exc),
            )

        return CoordinatorRouteResult(
            target=ROUTE_CODING_AGENT,
            action=action,
            status=MissionStepStatus.COMPLETED.value,
            result={
                "type": "coding_task",
                "task_id": task.id,
                "task_status": task.status.value,
                "result_id": result.id,
                "result_status": result.status.value,
                "summary": result.summary,
                "exit_code": result.exit_code,
                "stdout_preview": _preview(result.stdout),
                "stderr_preview": _preview(result.stderr),
            },
        )

    async def _route_mcp(
        self, mission_id: str, decision: CoordinatorDecision, step_id: str | None = None
    ) -> CoordinatorRouteResult:
        """Route a decision to GNU Radio MCP: call a tool from the decision payload.

        The resulting MCP call is automatically linked to the owning mission and step (the
        coordinator never supplies database ids) and executes through the persistent
        managed session.
        """
        action = decision.action
        if not self.mcp.is_configured():
            return CoordinatorRouteResult(
                target=ROUTE_MCP,
                action=action,
                status=MissionStepStatus.BLOCKED.value,
                error="GNU Radio MCP is not configured/ready; cannot route to it.",
            )
        try:
            payload = mcp_call_payload_from_decision(
                decision, mission_id=mission_id, mission_step_id=step_id
            )
        except RouteValidationError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_MCP,
                action=action,
                status=MissionStepStatus.FAILED.value,
                error=str(exc),
            )

        try:
            call = await self.call_mcp_tool(payload)
        except MCPConfigurationError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_MCP,
                action=action,
                status=MissionStepStatus.BLOCKED.value,
                error=str(exc),
            )
        except MCPSchemaValidationError as exc:
            # beta.5: a schema PREFLIGHT rejection (bad field names) is a correctable, pre-dispatch
            # condition — return a structured correction WITHOUT consuming the scarce gnuradio_mcp
            # action budget, so the coordinator can re-issue a valid call. The bounded iteration /
            # coordinator-call budgets still cap how long it may keep trying.
            return CoordinatorRouteResult(
                target=ROUTE_MCP,
                action=action,
                status=MissionStepStatus.FAILED.value,
                error=str(exc),
                result={
                    "type": "mcp_schema_correction",
                    "tool_name": getattr(exc, "tool_name", None),
                    "correction": getattr(exc, "correction", str(exc)),
                    "schema_rejected": True,
                    "budget_consumed": False,
                },
            )
        except MCPError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_MCP,
                action=action,
                status=MissionStepStatus.FAILED.value,
                error=str(exc),
            )

        return CoordinatorRouteResult(
            target=ROUTE_MCP,
            action=action,
            status=MissionStepStatus.COMPLETED.value,
            result={
                "type": "mcp_tool_call",
                "call_id": call.id,
                "tool_name": call.tool_name,
                "status": call.status.value,
                "result": call.result,
                "error": call.error,
            },
        )

    async def _route_rf_mcp(
        self, mission_id: str, decision: CoordinatorDecision, step_id: str | None = None
    ) -> CoordinatorRouteResult:
        """Route a decision to a NAMED RF backend: exactly one MCP tool call (Stage 13A.5).

        The coordinator names both ``backend_id`` and ``tool_name`` in structured_payload —
        selection is model-driven, never keyword-derived. Backend/tool validity and readiness
        are enforced by the registry; a failed tool call is recorded honestly and never
        replayed. The legacy backend (``legacy_gr_mcp``) routes through the existing audited
        path; ``gnuradio_mcp`` remains the backward-compatible legacy-only target.
        """
        from aithernet.rf.contracts import (
            RFBackendDisabledError,
            RFBackendError,
            RFBackendNotFoundError,
            RFBackendNotReadyError,
            RFBackendUnavailableError,
            RFCallExecutionContext,
            RFToolNotFoundError,
        )

        action = decision.action
        try:
            backend_id, tool_name, arguments = rf_call_from_decision(decision)
        except RouteValidationError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_RF_MCP, action=action,
                status=MissionStepStatus.FAILED.value, error=str(exc),
            )

        ctx = RFCallExecutionContext(
            caller="coordinator", mission_id=mission_id, mission_step_id=step_id
        )
        try:
            call = await self.rf.call_tool(
                backend_id, tool_name, arguments, execution_context=ctx
            )
        except (RFBackendDisabledError, RFBackendNotReadyError, RFBackendUnavailableError) as exc:
            # Availability problems are BLOCKED (not the coordinator's fault) — never replayed.
            return CoordinatorRouteResult(
                target=ROUTE_RF_MCP, action=action,
                status=MissionStepStatus.BLOCKED.value, error=str(exc),
            )
        except (RFBackendNotFoundError, RFToolNotFoundError, RFBackendError) as exc:
            return CoordinatorRouteResult(
                target=ROUTE_RF_MCP, action=action,
                status=MissionStepStatus.FAILED.value, error=str(exc),
            )
        except MCPConfigurationError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_RF_MCP, action=action,
                status=MissionStepStatus.BLOCKED.value, error=str(exc),
            )
        except MCPError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_RF_MCP, action=action,
                status=MissionStepStatus.FAILED.value, error=str(exc),
            )

        succeeded = call.status == MCPToolCallStatus.COMPLETED
        return CoordinatorRouteResult(
            target=ROUTE_RF_MCP,
            action=action,
            status=(
                MissionStepStatus.COMPLETED.value if succeeded else MissionStepStatus.FAILED.value
            ),
            result={
                "type": "rf_mcp_tool_call",
                "backend_id": backend_id,
                "call_id": call.id,
                "tool_name": call.tool_name,
                "status": call.status.value,
                "result": call.result,
                "error": call.error,
            },
        )

    async def _route_rf_device(
        self, mission_id: str, decision: CoordinatorDecision, step_id: str | None = None
    ) -> CoordinatorRouteResult:
        """Route a managed-hardware action: acquire/release a lease, or refresh inventory (14B).

        This is the ONE external coordinator action for the iteration and produces ONE MissionStep.
        The coordinator names only a known device/backend + bounded RF requirements; it can never
        supply device arguments, driver strings, paths, environment, or endpoints. Lease renewal
        and inventory polling are infrastructure and create no MissionStep. A capability-
        incompatible
        request fails factually and returns the device's capability info for reassessment.
        """
        from aithernet.hardware.contracts import (
            BindingNotFoundError,
            DeviceNotFoundError,
            DeviceUnavailableError,
            LeaseCompatibilityError,
            LeaseConflictError,
            LeaseNotFoundError,
        )

        action = decision.action
        try:
            spec = rf_device_from_decision(decision)
        except RouteValidationError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_RF_DEVICE, action=action,
                status=MissionStepStatus.FAILED.value, error=str(exc),
            )

        if spec["action"] == "refresh_inventory":
            await self.hardware_inventory.refresh()
            return CoordinatorRouteResult(
                target=ROUTE_RF_DEVICE, action=action,
                status=MissionStepStatus.COMPLETED.value,
                result={"type": "rf_device", "action": "refresh_inventory",
                        "devices": self.hardware_compact_inventory().get("devices", [])},
            )

        if spec["action"] == "release_lease":
            try:
                released = await self.hardware_leases.release(
                    spec["lease_id"], reason="coordinator release"
                )
            except LeaseNotFoundError as exc:
                return CoordinatorRouteResult(
                    target=ROUTE_RF_DEVICE, action=action,
                    status=MissionStepStatus.FAILED.value, error=str(exc),
                )
            return CoordinatorRouteResult(
                target=ROUTE_RF_DEVICE, action=action,
                status=MissionStepStatus.COMPLETED.value,
                result={"type": "rf_device", "action": "release_lease",
                        "lease_id": spec["lease_id"], "released": released},
            )

        # acquire_lease — deterministic RECEIVE-ONLY gate before any device use is authorized.
        from aithernet.missions import rf_constraints as _rfc
        try:
            _rfc.assert_receive_only(spec["direction"])
        except _rfc.ConstraintViolation as exc:
            return CoordinatorRouteResult(
                target=ROUTE_RF_DEVICE, action=action,
                status=MissionStepStatus.FAILED.value,
                error=f"receive-only constraint: {exc.category}",
            )
        try:
            lease_id = await self.hardware_leases.acquire(
                spec["device_id"], backend_id=spec["backend_id"], direction=spec["direction"],
                mode=spec["lease_mode"], operation=spec["operation"], channels=spec["channels"],
                frequency_hz=spec["frequency_hz"], sample_rate=spec["sample_rate"],
                mission_id=mission_id, mission_step_id=step_id,
            )
        except (DeviceUnavailableError, LeaseConflictError) as exc:
            # Availability/conflict are BLOCKED (transient; the coordinator may reassess later).
            return CoordinatorRouteResult(
                target=ROUTE_RF_DEVICE, action=action,
                status=MissionStepStatus.BLOCKED.value, error=str(exc),
                result={"type": "rf_device", "action": "acquire_lease",
                        "device_id": spec["device_id"],
                        "capabilities": self._hardware_capability_view(spec["device_id"])},
            )
        except (DeviceNotFoundError, BindingNotFoundError, LeaseCompatibilityError) as exc:
            return CoordinatorRouteResult(
                target=ROUTE_RF_DEVICE, action=action,
                status=MissionStepStatus.FAILED.value, error=str(exc),
                result={"type": "rf_device", "action": "acquire_lease",
                        "device_id": spec["device_id"],
                        "capabilities": self._hardware_capability_view(spec["device_id"])},
            )
        return CoordinatorRouteResult(
            target=ROUTE_RF_DEVICE, action=action,
            status=MissionStepStatus.COMPLETED.value,
            result={"type": "rf_device", "action": "acquire_lease",
                    "device_id": spec["device_id"], "backend_id": spec["backend_id"],
                    "lease_id": lease_id, "direction": spec["direction"],
                    "lease_mode": spec["lease_mode"]},
        )

    def _hardware_capability_view(self, device_id: str) -> dict | None:
        """Return a compact device capability summary (for incompatible-request reassessment)."""
        with self.session_scope() as session:
            from aithernet.state.repositories import SDRDeviceRepository
            device = SDRDeviceRepository(session).get(device_id)
            if device is None:
                return None
            return self.hardware_leases.capability_view(session, device)

    def hardware_compact_inventory(self) -> dict:
        """A compact, sanitized hardware inventory for the coordinator (Stage 14B, Part J).

        Exposes only known device ids, display names, provider, present/missing, enabled/disabled,
        health, RX/TX capability, channel count, concise frequency/sample-rate capability, current
        lease availability, and compatible RF backends — NEVER raw driver output, device paths,
        secrets, environment, or arbitrary provider metadata.
        """
        from aithernet.state.repositories import (
            SDRDeviceBindingRepository,
            SDRDeviceCapabilityRepository,
            SDRDeviceLeaseRepository,
            SDRDeviceRepository,
        )

        node_id = self.config.node_id
        devices: list[dict] = []
        with self.session_scope() as session:
            device_repo = SDRDeviceRepository(session)
            caps_repo = SDRDeviceCapabilityRepository(session)
            lease_repo = SDRDeviceLeaseRepository(session)
            binding_repo = SDRDeviceBindingRepository(session)
            for device in device_repo.list(node_id=node_id, limit=self.config.hardware.max_devices):
                caps = caps_repo.get_for_device(device.id)
                cj = (caps.capabilities_json if caps else {}) or {}
                holding = lease_repo.holding_for_device(device.id)
                bindings = [b.backend_id for b in binding_repo.list_for_device(device.id)
                            if b.enabled]
                devices.append({
                    "device_id": device.id,
                    "display_name": device.display_name,
                    "provider_id": device.provider_id,
                    "device_kind": device.device_kind,
                    "present": device.presence_state == "present",
                    "enabled": device.enabled,
                    "status": device.status,
                    "health": device.health_state,
                    "rx": cj.get("rx_supported"),
                    "tx": cj.get("tx_supported"),
                    "channel_count": cj.get("channel_count"),
                    "frequency_ranges": cj.get("frequency_ranges"),
                    "sample_rate_ranges": cj.get("sample_rate_ranges"),
                    "identity_limited": device.identity_limited,
                    "lease_available": not holding,
                    "active_lease_count": len(holding),
                    "compatible_backends": bindings,
                })
        return {"enabled": self.config.hardware.enabled, "devices": devices}

    async def _route_peer_message(
        self, mission_id: str, decision: CoordinatorDecision, step_id: str | None = None
    ) -> CoordinatorRouteResult:
        """Queue EXACTLY ONE durable coordinator message to a trusted, authorized peer (13B).

        The coordinator names the peer + content/correlation only; it cannot specify
        endpoints, keys, signatures, headers, retry policy, or sender identity. Validation
        failures produce NO outbox record and are never auto-replayed. Delivery + retries are
        invisible to mission-step accounting (the Stage 13A worker owns them).
        """
        from aithernet.comms.service import CommunicationError

        action = decision.action
        try:
            spec = peer_message_from_decision(decision)
        except RouteValidationError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_PEER_MESSAGE, action=action,
                status=MissionStepStatus.FAILED.value, error=str(exc),
            )
        # Link to the mission's current execution run (one active run per mission).
        run_id = None
        with self.session_scope() as session:
            run = MissionExecutionRunRepository(session).current_for_mission(mission_id)
            run_id = run.id if run is not None else None
        try:
            result = await self.comms.queue_peer_message(
                peer_id=spec["peer_id"], message_type=spec["message_type"], text=spec["text"],
                data=spec["data"], expects_reply=spec["expects_reply"],
                conversation_id=spec["conversation_id"],
                reply_to_message_id=spec["reply_to_message_id"],
                reply_to_request_id=spec["reply_to_request_id"],
                response_deadline=spec["response_deadline"],
                mission_id=mission_id, mission_run_id=run_id, mission_step_id=step_id,
                causation_id=step_id,
            )
        except CommunicationError as exc:
            # Availability/authorization problems BLOCK (reassess/pick differently); a bad
            # payload or unknown peer is a FAILED route. Neither is auto-replayed.
            permanent = exc.code in ("bad_message_type", "text_too_large", "unknown_peer")
            status = (
                MissionStepStatus.FAILED.value if permanent else MissionStepStatus.BLOCKED.value
            )
            return CoordinatorRouteResult(
                target=ROUTE_PEER_MESSAGE, action=action, status=status, error=str(exc),
            )
        return CoordinatorRouteResult(
            target=ROUTE_PEER_MESSAGE, action=action,
            status=MissionStepStatus.COMPLETED.value,
            result={"type": "peer_message", **result},
        )

    async def _route_peer_artifact(
        self, mission_id: str, decision: CoordinatorDecision, step_id: str | None = None
    ) -> CoordinatorRouteResult:
        """Execute EXACTLY ONE artifact-control action (offer/request/cancel) for a peer (13D.2).

        The coordinator names only a trusted peer + a KNOWN artifact id + an action; it cannot
        choose a URL, path, object-store location, digest override, destination, HTTP range,
        retry policy, headers, or signing identity. The actual byte transfer + retries are
        deterministic background infrastructure and never create another MissionStep.
        """
        from aithernet.artifacts.service import ArtifactError

        action = decision.action
        try:
            spec = peer_artifact_from_decision(decision)
        except RouteValidationError as exc:
            return CoordinatorRouteResult(
                target=ROUTE_PEER_ARTIFACT, action=action,
                status=MissionStepStatus.FAILED.value, error=str(exc),
            )
        try:
            if spec["action"] == "cancel":
                cancelled = await self.artifacts.cancel_transfer(spec["transfer_id"])
                result = {"action": "cancel", "transfer_id": spec["transfer_id"],
                          "cancelled": cancelled}
            elif spec["action"] == "offer":
                result = await self.artifacts.offer_artifact(
                    artifact_id=spec["artifact_id"], peer_id=spec["peer_id"],
                    conversation_id=spec["conversation_id"], purpose=spec["purpose"],
                )
            else:  # request
                result = await self.artifacts.request_artifact(
                    peer_id=spec["peer_id"], origin_artifact_id=spec["artifact_id"],
                    conversation_id=spec["conversation_id"], purpose=spec["purpose"],
                    mission_id=mission_id,
                )
        except ArtifactError as exc:
            from aithernet.artifacts.service import PERMANENT_CODES

            permanent = exc.code in PERMANENT_CODES or exc.code in (
                "not_storable", "unknown_artifact"
            )
            status_value = (
                MissionStepStatus.FAILED.value if permanent else MissionStepStatus.BLOCKED.value
            )
            return CoordinatorRouteResult(
                target=ROUTE_PEER_ARTIFACT, action=action, status=status_value, error=str(exc),
            )
        return CoordinatorRouteResult(
            target=ROUTE_PEER_ARTIFACT, action=action,
            status=MissionStepStatus.COMPLETED.value,
            result={"type": "peer_artifact", **result},
        )

    def _node_state_result(self) -> dict:
        """Assemble a node/coordinator/coding/MCP status + recent-events summary."""
        events = self.list_events(limit=_RECENT_EVENT_WINDOW)
        return {
            "type": "node_state",
            "node_status": self.status().model_dump(mode="json"),
            "coordinator_status": self.coordinator.status().model_dump(mode="json"),
            "coding_agent_status": self.coding_agent_status().model_dump(mode="json"),
            "mcp_status": self.mcp.status().model_dump(mode="json"),
            "recent_events": [
                {
                    "event_type": event.event_type,
                    "source": event.source,
                    "message": event.message,
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ],
        }

    # -- internal helpers --------------------------------------------------------

    def _update_mission_step(
        self,
        step_id: str,
        *,
        status: str | None = None,
        result: dict | None = None,
        error: str | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        """Update a persisted mission step in its own transaction."""
        with self.session_scope() as session:
            MissionStepRepository(session).update(
                step_id,
                status=status,
                result=result,
                error=error,
                completed_at=completed_at,
            )
            session.commit()

    def _update_mcp_call(
        self,
        call_id: str,
        *,
        status: MCPToolCallStatus | None = None,
        result: dict | None = None,
        error: str | None = None,
        error_type: str | None = None,
        session_generation: int | None = None,
        tool_catalog_generation: int | None = None,
        duration_ms: int | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        """Update a persisted MCP tool call in its own transaction."""
        with self.session_scope() as session:
            MCPToolCallRepository(session).update(
                call_id,
                status=status.value if status is not None else None,
                result=result,
                error=error,
                error_type=error_type,
                session_generation=session_generation,
                tool_catalog_generation=tool_catalog_generation,
                duration_ms=duration_ms,
                started_at=started_at,
                completed_at=completed_at,
            )
            session.commit()

    def _persist_coding_result(
        self, task_id: str, execution_result: CodingTaskExecutionResult
    ) -> CodingTaskResultRead:
        """Persist a coding-task result in its own transaction."""
        with self.session_scope() as session:
            result = CodingTaskResultRepository(session).create(
                task_id=task_id,
                status=execution_result.status,
                summary=execution_result.summary,
                stdout=execution_result.stdout,
                stderr=execution_result.stderr,
                exit_code=execution_result.exit_code,
                artifacts=execution_result.artifacts,
                files_changed=execution_result.files_changed,
                commands_run=execution_result.commands_run,
                payload=execution_result.payload,
            )
            session.commit()
            return CodingTaskResultRead.model_validate(result)

    def _set_task_status(
        self,
        task_id: str,
        status: CodingTaskStatus,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        """Update a coding task's status (and timestamps) in its own transaction."""
        with self.session_scope() as session:
            CodingTaskRepository(session).update_status(
                task_id, status.value, started_at=started_at, completed_at=completed_at
            )
            session.commit()

    def schedule_artifact_worker(self) -> None:
        """Wake the artifact-transfer worker (Stage 13D.2). No-op if it is not running."""
        try:
            self.artifact_worker.schedule()
        except Exception:  # noqa: BLE001 - best-effort wake
            pass

    async def _emit_event(
        self,
        *,
        event_type: str,
        mission_id: str | None,
        message: str,
        source: str = "runtime",
        payload: dict | None = None,
    ) -> EventRead:
        """Persist an event in its own transaction and publish it to the bus."""
        with self.session_scope() as session:
            event = EventRepository(session).create(
                event_type=event_type,
                source=source,
                message=message,
                mission_id=mission_id,
                payload=payload or {},
            )
            session.commit()
            event_read = EventRead.model_validate(event)

        await self.event_bus.publish(event_read.model_dump(by_alias=True, mode="json"))
        # Stage 13D.3: a committed mission transition deterministically (and idempotently) queues
        # a remote status update for reportable inbound missions. Status events use a different
        # prefix, so this never recurses; it never creates a MissionStep. Best-effort: a failed
        # publish never affects the committed transition (the durable outbox handles delivery).
        if source != "mission_status":
            await self.mission_status.maybe_publish_from_event(event_type, mission_id)
        # Stage 14D: a committed mission transition deterministically (and idempotently) queues
        # durable external-agent callbacks for any agent that owns + subscribes to this mission.
        # It uses a different event namespace, never recurses, and creates NO MissionStep.
        if source != "interop":
            with suppress(Exception):
                await self.interop.maybe_notify_from_event(event_type, mission_id)
        return event_read

    async def publish_ephemeral_event(
        self,
        *,
        event_type: str,
        message: str,
        source: str = "mission_worker",
        mission_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        """Publish operational telemetry to the live bus WITHOUT persisting it.

        Used for node-level worker lifecycle events (``worker.started``/``stopped``/
        ``degraded``) so the live stream reflects them without polluting the mission audit
        log. Mission-audit events still go through :meth:`_emit_event` (persisted).
        """
        event = EventRead(
            id=f"ephemeral-{new_uuid()}",
            mission_id=mission_id,
            event_type=event_type,
            source=source,
            message=message,
            payload_json=payload or {},
            created_at=utcnow(),
        )
        await self.event_bus.publish(event.model_dump(by_alias=True, mode="json"))

    def _set_mission_status(self, mission_id: str, status: MissionStatus) -> None:
        """Update a mission's status in its own transaction."""
        with self.session_scope() as session:
            MissionRepository(session).update_status(mission_id, status.value)
            session.commit()

    # -- external-agent (communication) operations -------------------------------
    #
    # Thin delegators to the communication runtime (Stage 7). External agents are
    # message/mission sources; the node persists connections and messages, may create a
    # mission, and may run exactly one explicit step. It never calls endpoint_url.

    async def create_external_agent(self, payload: ExternalAgentCreate) -> ExternalAgentRead:
        """Persist a connecting external agent and emit ``external_agent.connected``."""
        return await self.communication.create_external_agent(payload)

    def list_external_agents(
        self,
        *,
        status: ExternalAgentStatusValue | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExternalAgentRead]:
        """Return external agents, newest first, optionally filtered by status."""
        return self.communication.list_external_agents(status=status, limit=limit, offset=offset)

    def get_external_agent(self, agent_id: str) -> ExternalAgentRead | None:
        """Return a single external agent, or ``None`` if it does not exist."""
        return self.communication.get_external_agent(agent_id)

    async def update_external_agent_status(
        self, agent_id: str, status: ExternalAgentStatusValue
    ) -> ExternalAgentRead:
        """Update an external agent's connection status and emit a lifecycle event."""
        return await self.communication.update_external_agent_status(agent_id, status)

    async def disable_external_agent(self, agent_id: str) -> ExternalAgentRead:
        """Disable an external agent (preferred over hard deletion)."""
        return await self.communication.disable_external_agent(agent_id)

    def external_agent_status(self) -> ExternalAgentStatus:
        """Return a roster snapshot: connected count, total count, and the agents."""
        return self.communication.external_agent_status()

    async def receive_external_agent_message(
        self, agent_id: str, payload: ExternalAgentMessageCreate
    ) -> ExternalAgentMessageResponse:
        """Record an inbound message; optionally create a mission and run one step."""
        return await self.communication.receive_external_agent_message(agent_id, payload)

    def list_external_agent_messages(
        self,
        *,
        agent_id: str | None = None,
        mission_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExternalAgentMessageRead]:
        """Return external-agent messages, newest first, optionally filtered."""
        return self.communication.list_external_agent_messages(
            agent_id=agent_id, mission_id=mission_id, limit=limit, offset=offset
        )

    # -- event operations --------------------------------------------------------

    def list_events(
        self, *, mission_id: str | None = None, limit: int = 200, offset: int = 0
    ) -> list[EventRead]:
        """Return persisted events, newest first, optionally filtered by mission."""
        with self.session_scope() as session:
            rows = EventRepository(session).list(
                mission_id=mission_id, limit=limit, offset=offset
            )
            return [EventRead.model_validate(row) for row in rows]

    # -- node status -------------------------------------------------------------

    def status(self) -> NodeStatus:
        """Return an operational snapshot of the node."""
        with self.session_scope() as session:
            mission_count = MissionRepository(session).count()
            event_count = EventRepository(session).count()
        return NodeStatus(
            node_id=self.config.node_id,
            node_name=self.config.node_name,
            runtime_status=self.runtime_status,
            database_path=self.config.database_path,
            started_at=self.started_at,
            version=self.version,
            mission_count=mission_count,
            event_count=event_count,
        )
