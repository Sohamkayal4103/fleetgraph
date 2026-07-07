"""Repository classes encapsulating all database access for node state.

Route handlers and the runtime use these repositories rather than issuing queries
directly, keeping persistence logic in one place and easy to evolve in later stages.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from aithernet.state.models import (
    AgentInboxMessage,
    AgentOutboxMessage,
    ArtifactTransfer,
    ArtifactTransferAttempt,
    CodingTask,
    CodingTaskResult,
    DataCategoryPolicy,
    DataConsentAudit,
    DataConsentGrant,
    DataConsentProfile,
    DataConsentRevision,
    DataDataset,
    DataDatasetMember,
    DataDatasetVersion,
    DataDeletionRequest,
    DataDeliveryAttempt,
    DataExportBatch,
    DataExportDestination,
    DataExportRecord,
    Event,
    ExternalAgent,
    ExternalAgentMessage,
    FieldCampaign,
    FieldCheck,
    FieldDeviceAssignment,
    FieldFaultInjection,
    FieldMeasurement,
    FieldNode,
    FieldRecoveryResult,
    FieldRun,
    HardwareQualificationCheck,
    HardwareQualificationRun,
    HardwareRFMeasurement,
    InboundRequest,
    InteropAgent,
    InteropAudit,
    InteropCredential,
    InteropDelivery,
    InteropEndpoint,
    InteropMessage,
    InteropNonce,
    InteropReceipt,
    InteropSession,
    InteropSubmission,
    InteropSubscription,
    LocalMissionStatusPublication,
    MCPToolCall,
    Mesh,
    MeshMember,
    Mission,
    MissionExecutionRun,
    MissionReplyWait,
    MissionStep,
    RemoteArtifactReference,
    RemoteMissionSnapshot,
    RemoteMissionStatusEvent,
    RFArtifact,
    RFBenchmarkRun,
    RFWorkspaceContextRecord,
    SDRDevice,
    SDRDeviceBackendBinding,
    SDRDeviceCapability,
    SDRDeviceHealthEvent,
    SDRDeviceLease,
    SDRDeviceLeaseEvent,
    SoakRun,
    SoakSample,
    utcnow,
)


class MissionRepository:
    """CRUD and query operations for :class:`Mission` rows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        content: str,
        source_type: str = "user",
        source_id: str | None = None,
        status: str = "received",
        metadata: dict | None = None,
    ) -> Mission:
        """Insert a new mission and flush so its generated id is available."""
        mission = Mission(
            content=content,
            source_type=source_type,
            source_id=source_id,
            status=status,
            metadata_json=metadata or {},
        )
        self.session.add(mission)
        self.session.flush()
        return mission

    def get(self, mission_id: str) -> Mission | None:
        """Return a mission by id, or ``None`` if it does not exist."""
        return self.session.get(Mission, mission_id)

    def list(self, *, limit: int = 100, offset: int = 0) -> list[Mission]:
        """Return missions, newest first."""
        stmt = (
            select(Mission)
            .order_by(Mission.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(stmt))

    def update_status(self, mission_id: str, status: str) -> Mission | None:
        """Update a mission's status, returning the mission or ``None`` if absent."""
        mission = self.get(mission_id)
        if mission is None:
            return None
        mission.status = status
        self.session.flush()
        return mission

    def count(self) -> int:
        """Return the total number of missions."""
        return int(self.session.scalar(select(func.count()).select_from(Mission)) or 0)

    def counts_by_status(self) -> dict[str, int]:
        """Return ``{status: count}`` over all missions in a single grouped query."""
        stmt = select(Mission.status, func.count()).group_by(Mission.status)
        return {status: int(count) for status, count in self.session.execute(stmt)}


class EventRepository:
    """Append and query operations for the :class:`Event` log."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        event_type: str,
        source: str = "runtime",
        message: str = "",
        mission_id: str | None = None,
        payload: dict | None = None,
    ) -> Event:
        """Append a new event and flush so its generated id is available."""
        event = Event(
            event_type=event_type,
            source=source,
            message=message,
            mission_id=mission_id,
            payload_json=payload or {},
        )
        self.session.add(event)
        self.session.flush()
        return event

    def list(
        self,
        *,
        mission_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Event]:
        """Return events, newest first, optionally filtered by mission."""
        stmt = select(Event).order_by(Event.created_at.desc())
        if mission_id is not None:
            stmt = stmt.where(Event.mission_id == mission_id)
        stmt = stmt.limit(limit).offset(offset)
        return list(self.session.scalars(stmt))

    def count(self) -> int:
        """Return the total number of events."""
        return int(self.session.scalar(select(func.count()).select_from(Event)) or 0)


class CodingTaskRepository:
    """CRUD and query operations for :class:`CodingTask` rows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        objective: str,
        mission_id: str | None = None,
        context: dict | None = None,
        available_tools: list[str] | None = None,
        expected_outputs: list[str] | None = None,
        reporting_requirements: list[str] | None = None,
        provider: str | None = None,
        status: str = "created",
    ) -> CodingTask:
        """Insert a new coding task and flush so its generated id is available."""
        task = CodingTask(
            objective=objective,
            mission_id=mission_id,
            context_json=context or {},
            available_tools_json=available_tools or [],
            expected_outputs_json=expected_outputs or [],
            reporting_requirements_json=reporting_requirements or [],
            provider=provider,
            status=status,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def get(self, task_id: str) -> CodingTask | None:
        """Return a coding task by id, or ``None`` if it does not exist."""
        return self.session.get(CodingTask, task_id)

    def list(self, *, limit: int = 100, offset: int = 0) -> list[CodingTask]:
        """Return coding tasks, newest first."""
        stmt = (
            select(CodingTask)
            .order_by(CodingTask.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(stmt))

    def update_status(
        self,
        task_id: str,
        status: str,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> CodingTask | None:
        """Update a task's status (and optional timestamps); return it or ``None``."""
        task = self.get(task_id)
        if task is None:
            return None
        task.status = status
        if started_at is not None:
            task.started_at = started_at
        if completed_at is not None:
            task.completed_at = completed_at
        self.session.flush()
        return task

    def count(self) -> int:
        """Return the total number of coding tasks."""
        return int(self.session.scalar(select(func.count()).select_from(CodingTask)) or 0)


class CodingTaskResultRepository:
    """Append and query operations for :class:`CodingTaskResult` rows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        task_id: str,
        status: str,
        summary: str = "",
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        artifacts: list | None = None,
        files_changed: list | None = None,
        commands_run: list | None = None,
        payload: dict | None = None,
    ) -> CodingTaskResult:
        """Append a new task result and flush so its generated id is available."""
        result = CodingTaskResult(
            task_id=task_id,
            status=status,
            summary=summary,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            artifacts_json=artifacts or [],
            files_changed_json=files_changed or [],
            commands_run_json=commands_run or [],
            payload_json=payload or {},
        )
        self.session.add(result)
        self.session.flush()
        return result

    def get_latest_for_task(self, task_id: str) -> CodingTaskResult | None:
        """Return the most recent result for a task, or ``None`` if none exists."""
        stmt = (
            select(CodingTaskResult)
            .where(CodingTaskResult.task_id == task_id)
            .order_by(CodingTaskResult.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()


class MCPToolCallRepository:
    """CRUD and query operations for :class:`MCPToolCall` rows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        tool_name: str,
        caller: str = "user",
        arguments: dict | None = None,
        mission_id: str | None = None,
        mission_step_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        session_generation: int | None = None,
        status: str = "created",
        backend_id: str = "legacy_gr_mcp",
        backend_kind: str | None = None,
        backend_version: str | None = None,
        backend_source_revision: str | None = None,
    ) -> MCPToolCall:
        """Insert a new tool-call record and flush so its generated id is available."""
        call = MCPToolCall(
            tool_name=tool_name,
            caller=caller,
            arguments_json=arguments or {},
            mission_id=mission_id,
            mission_step_id=mission_step_id,
            task_id=task_id,
            session_id=session_id,
            session_generation=session_generation,
            status=status,
            backend_id=backend_id,
            backend_kind=backend_kind,
            backend_version=backend_version,
            backend_source_revision=backend_source_revision,
        )
        self.session.add(call)
        self.session.flush()
        return call

    def get(self, call_id: str) -> MCPToolCall | None:
        """Return a tool call by id, or ``None`` if it does not exist."""
        return self.session.get(MCPToolCall, call_id)

    def list(
        self,
        *,
        mission_id: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MCPToolCall]:
        """Return tool calls, newest first, optionally filtered by mission/task."""
        stmt = select(MCPToolCall).order_by(MCPToolCall.created_at.desc())
        if mission_id is not None:
            stmt = stmt.where(MCPToolCall.mission_id == mission_id)
        if task_id is not None:
            stmt = stmt.where(MCPToolCall.task_id == task_id)
        stmt = stmt.limit(limit).offset(offset)
        return list(self.session.scalars(stmt))

    def update(
        self,
        call_id: str,
        *,
        status: str | None = None,
        result: dict | None = None,
        error: str | None = None,
        error_type: str | None = None,
        session_generation: int | None = None,
        tool_catalog_generation: int | None = None,
        duration_ms: int | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        result_summary_type: str | None = None,
    ) -> MCPToolCall | None:
        """Update a tool call's status/result/timestamps; return it or ``None``."""
        call = self.get(call_id)
        if call is None:
            return None
        if status is not None:
            call.status = status
        if result is not None:
            call.result_json = result
        if error is not None:
            call.error = error
        if error_type is not None:
            call.error_type = error_type
        if session_generation is not None:
            call.session_generation = session_generation
        if tool_catalog_generation is not None:
            call.tool_catalog_generation = tool_catalog_generation
        if duration_ms is not None:
            call.duration_ms = duration_ms
        if started_at is not None:
            call.started_at = started_at
        if completed_at is not None:
            call.completed_at = completed_at
        if result_summary_type is not None:
            call.result_summary_type = result_summary_type
        self.session.flush()
        return call

    def count(self) -> int:
        """Return the total number of tool calls."""
        return int(self.session.scalar(select(func.count()).select_from(MCPToolCall)) or 0)


class ExternalAgentRepository:
    """CRUD and query operations for :class:`ExternalAgent` rows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        name: str,
        agent_type: str = "external",
        endpoint_url: str | None = None,
        transport: str = "local",
        auth_type: str | None = None,
        status: str = "connected",
        metadata: dict | None = None,
        last_seen_at: datetime | None = None,
    ) -> ExternalAgent:
        """Insert a new external agent and flush so its generated id is available."""
        agent = ExternalAgent(
            name=name,
            agent_type=agent_type,
            endpoint_url=endpoint_url,
            transport=transport,
            auth_type=auth_type,
            status=status,
            metadata_json=metadata or {},
            last_seen_at=last_seen_at,
        )
        self.session.add(agent)
        self.session.flush()
        return agent

    def get(self, agent_id: str) -> ExternalAgent | None:
        """Return an external agent by id, or ``None`` if it does not exist."""
        return self.session.get(ExternalAgent, agent_id)

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExternalAgent]:
        """Return external agents, newest first, optionally filtered by status."""
        stmt = select(ExternalAgent).order_by(ExternalAgent.created_at.desc())
        if status is not None:
            stmt = stmt.where(ExternalAgent.status == status)
        stmt = stmt.limit(limit).offset(offset)
        return list(self.session.scalars(stmt))

    def update(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        last_seen_at: datetime | None = None,
    ) -> ExternalAgent | None:
        """Update an agent's status and/or last-seen timestamp; return it or ``None``."""
        agent = self.get(agent_id)
        if agent is None:
            return None
        if status is not None:
            agent.status = status
        if last_seen_at is not None:
            agent.last_seen_at = last_seen_at
        self.session.flush()
        return agent

    def count(self, *, status: str | None = None) -> int:
        """Return the number of external agents, optionally filtered by status."""
        stmt = select(func.count()).select_from(ExternalAgent)
        if status is not None:
            stmt = stmt.where(ExternalAgent.status == status)
        return int(self.session.scalar(stmt) or 0)


class ExternalAgentMessageRepository:
    """Append and query operations for :class:`ExternalAgentMessage` rows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        agent_id: str,
        direction: str,
        message_type: str = "mission_request",
        content: str = "",
        payload: dict | None = None,
        mission_id: str | None = None,
    ) -> ExternalAgentMessage:
        """Append a new agent message and flush so its generated id is available."""
        message = ExternalAgentMessage(
            agent_id=agent_id,
            direction=direction,
            message_type=message_type,
            content=content,
            payload_json=payload or {},
            mission_id=mission_id,
        )
        self.session.add(message)
        self.session.flush()
        return message

    def get(self, message_id: str) -> ExternalAgentMessage | None:
        """Return an agent message by id, or ``None`` if it does not exist."""
        return self.session.get(ExternalAgentMessage, message_id)

    def set_mission(self, message_id: str, mission_id: str) -> ExternalAgentMessage | None:
        """Link a persisted message to a mission; return it or ``None`` if absent."""
        message = self.get(message_id)
        if message is None:
            return None
        message.mission_id = mission_id
        self.session.flush()
        return message

    def list(
        self,
        *,
        agent_id: str | None = None,
        mission_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExternalAgentMessage]:
        """Return agent messages, newest first, optionally filtered by agent/mission."""
        stmt = select(ExternalAgentMessage).order_by(ExternalAgentMessage.created_at.desc())
        if agent_id is not None:
            stmt = stmt.where(ExternalAgentMessage.agent_id == agent_id)
        if mission_id is not None:
            stmt = stmt.where(ExternalAgentMessage.mission_id == mission_id)
        stmt = stmt.limit(limit).offset(offset)
        return list(self.session.scalars(stmt))


class MissionStepRepository:
    """CRUD and query operations for :class:`MissionStep` rows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        mission_id: str,
        decision_id: str,
        decision: dict,
        route_target: str,
        route_action: str = "",
        status: str = "created",
        started_at: datetime | None = None,
    ) -> MissionStep:
        """Insert a new mission-step record and flush so its generated id is available."""
        step = MissionStep(
            mission_id=mission_id,
            decision_id=decision_id,
            decision_json=decision,
            route_target=route_target,
            route_action=route_action,
            status=status,
            started_at=started_at,
        )
        self.session.add(step)
        self.session.flush()
        return step

    def get(self, step_id: str) -> MissionStep | None:
        """Return a mission step by id, or ``None`` if it does not exist."""
        return self.session.get(MissionStep, step_id)

    def list(
        self,
        *,
        mission_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MissionStep]:
        """Return mission steps, newest first, optionally filtered by mission."""
        stmt = select(MissionStep).order_by(MissionStep.created_at.desc())
        if mission_id is not None:
            stmt = stmt.where(MissionStep.mission_id == mission_id)
        stmt = stmt.limit(limit).offset(offset)
        return list(self.session.scalars(stmt))

    def update(
        self,
        step_id: str,
        *,
        status: str | None = None,
        result: dict | None = None,
        error: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> MissionStep | None:
        """Update a mission step's status/result/timestamps; return it or ``None``."""
        step = self.get(step_id)
        if step is None:
            return None
        if status is not None:
            step.status = status
        if result is not None:
            step.result_json = result
        if error is not None:
            step.error = error
        if started_at is not None:
            step.started_at = started_at
        if completed_at is not None:
            step.completed_at = completed_at
        self.session.flush()
        return step

    def count(self) -> int:
        """Return the total number of mission steps."""
        return int(self.session.scalar(select(func.count()).select_from(MissionStep)) or 0)


#: Mission/run statuses considered terminal (never re-leased or reopened).
_TERMINAL_RUN_STATES = ("completed", "blocked", "failed", "cancelled")


class MissionExecutionRunRepository:
    """CRUD + atomic DB-backed leasing for :class:`MissionExecutionRun` rows (Stage 12)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self, *, mission_id: str, node_id: str, budgets: dict, status: str = "queued"
    ) -> MissionExecutionRun:
        """Insert a new execution run (queued) and flush so its id is available."""
        run = MissionExecutionRun(
            mission_id=mission_id,
            node_id=node_id,
            status=status,
            budgets_json=budgets or {},
            queued_at=utcnow() if status == "queued" else None,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def get(self, run_id: str) -> MissionExecutionRun | None:
        return self.session.get(MissionExecutionRun, run_id)

    def current_for_mission(self, mission_id: str) -> MissionExecutionRun | None:
        """Return the newest non-terminal run for a mission, or ``None``."""
        stmt = (
            select(MissionExecutionRun)
            .where(MissionExecutionRun.mission_id == mission_id)
            .where(MissionExecutionRun.status.not_in(_TERMINAL_RUN_STATES))
            .order_by(MissionExecutionRun.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def latest_for_mission(self, mission_id: str) -> MissionExecutionRun | None:
        stmt = (
            select(MissionExecutionRun)
            .where(MissionExecutionRun.mission_id == mission_id)
            .order_by(MissionExecutionRun.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list(self, *, limit: int = 100, offset: int = 0) -> list[MissionExecutionRun]:
        stmt = (
            select(MissionExecutionRun)
            .order_by(MissionExecutionRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(stmt))

    def counts_by_status(self) -> dict[str, int]:
        """Return ``{status: count}`` over all execution runs in a single grouped query."""
        stmt = select(MissionExecutionRun.status, func.count()).group_by(
            MissionExecutionRun.status
        )
        return {status: int(count) for status, count in self.session.execute(stmt)}

    def queued_run_ids(self, node_id: str, *, limit: int = 50) -> list[str]:
        """Return ids of queued runs (oldest first) awaiting a worker lease."""
        stmt = (
            select(MissionExecutionRun.id)
            .where(MissionExecutionRun.node_id == node_id)
            .where(MissionExecutionRun.status == "queued")
            .order_by(MissionExecutionRun.queued_at.asc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def recoverable_active_ids(self, node_id: str, now: datetime, *, limit: int = 50) -> list[str]:
        """Return ids of active runs whose lease has expired (recoverable after a crash)."""
        stmt = (
            select(MissionExecutionRun.id)
            .where(MissionExecutionRun.node_id == node_id)
            .where(MissionExecutionRun.status == "active")
            .where(
                or_(
                    MissionExecutionRun.lease_expiry.is_(None),
                    MissionExecutionRun.lease_expiry < now,
                )
            )
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    # -- atomic lease operations (single conditional UPDATEs) --------------------

    def acquire_lease(
        self, run_id: str, *, owner_id: str, token: str, now: datetime, expiry: datetime
    ) -> int:
        """Atomically claim a queued run (or an active run with an expired lease).

        Returns the number of rows updated (1 = acquired, 0 = lost the race / not eligible).
        The caller commits; SQLite serializes the conditional UPDATE so two workers cannot
        both win.
        """
        stmt = (
            update(MissionExecutionRun)
            .where(MissionExecutionRun.id == run_id)
            .where(
                or_(
                    MissionExecutionRun.status == "queued",
                    (MissionExecutionRun.status == "active")
                    & (
                        MissionExecutionRun.lease_expiry.is_(None)
                        | (MissionExecutionRun.lease_expiry < now)
                    ),
                )
            )
            .values(
                status="active",
                execution_owner_id=owner_id,
                lease_token=token,
                lease_acquired_at=now,
                lease_renewed_at=now,
                lease_expiry=expiry,
                started_at=func.coalesce(MissionExecutionRun.started_at, now),
                last_activity_at=now,
            )
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def renew_lease(
        self, run_id: str, *, owner_id: str, token: str, now: datetime, expiry: datetime
    ) -> int:
        """Renew a lease only when the caller still holds the matching token."""
        stmt = (
            update(MissionExecutionRun)
            .where(MissionExecutionRun.id == run_id)
            .where(MissionExecutionRun.execution_owner_id == owner_id)
            .where(MissionExecutionRun.lease_token == token)
            .where(MissionExecutionRun.status == "active")
            .values(lease_renewed_at=now, lease_expiry=expiry, last_activity_at=now)
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def release_lease(self, run_id: str, *, token: str) -> int:
        """Clear the lease only when the caller still holds the matching token."""
        stmt = (
            update(MissionExecutionRun)
            .where(MissionExecutionRun.id == run_id)
            .where(MissionExecutionRun.lease_token == token)
            .values(
                lease_token=None,
                execution_owner_id=None,
                lease_acquired_at=None,
                lease_renewed_at=None,
                lease_expiry=None,
            )
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def update(
        self, run_id: str, *, fields: dict, bump_version: bool = True
    ) -> MissionExecutionRun | None:
        """Apply ``fields`` to a run, bumping the optimistic-concurrency version."""
        run = self.get(run_id)
        if run is None:
            return None
        for key, value in fields.items():
            setattr(run, key, value)
        if bump_version:
            run.version = (run.version or 0) + 1
        run.last_activity_at = utcnow()
        self.session.flush()
        return run


# -- Stage 13A: peer trust + durable outbox/inbox --------------------------------

#: Outbox delivery states considered terminal (never claimed or retried automatically).
_TERMINAL_OUTBOX_STATES = ("acknowledged", "failed", "dead_letter", "cancelled")
#: Outbox states eligible for a delivery claim.
_CLAIMABLE_OUTBOX_STATES = ("pending", "retry_scheduled")


class PeerRepository:
    """Trust + transport operations over :class:`ExternalAgent` rows (Stage 13A peers).

    A peer IS an external-agent row with the additive Stage 13A trust/identity columns
    populated, so existing Stage 7 records and APIs stay valid.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        name: str,
        role: str = "unknown",
        endpoint_url: str | None = None,
        expected_node_id: str | None = None,
        public_key: str | None = None,
        fingerprint: str | None = None,
        transport: str = "http",
        tls_verify: bool = True,
        trust_state: str = "untrusted",
        metadata: dict | None = None,
    ) -> ExternalAgent:
        """Insert a new peer record (untrusted by default)."""
        peer = ExternalAgent(
            name=name,
            agent_type=role,
            role=role,
            endpoint_url=endpoint_url,
            expected_node_id=expected_node_id,
            public_key=public_key,
            fingerprint=fingerprint,
            transport=transport,
            tls_verify=tls_verify,
            trust_state=trust_state,
            status="connected",
            enabled=True,
            metadata_json=metadata or {},
        )
        self.session.add(peer)
        self.session.flush()
        return peer

    def get(self, peer_id: str) -> ExternalAgent | None:
        return self.session.get(ExternalAgent, peer_id)

    def get_by_node_id(self, node_id: str) -> ExternalAgent | None:
        stmt = (
            select(ExternalAgent)
            .where(ExternalAgent.expected_node_id == node_id)
            .order_by(ExternalAgent.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def get_by_fingerprint(self, fingerprint: str) -> ExternalAgent | None:
        stmt = (
            select(ExternalAgent)
            .where(ExternalAgent.fingerprint == fingerprint)
            .order_by(ExternalAgent.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list(self, *, limit: int = 100, offset: int = 0) -> list[ExternalAgent]:
        stmt = (
            select(ExternalAgent)
            .order_by(ExternalAgent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(stmt))

    def update(self, peer_id: str, *, fields: dict) -> ExternalAgent | None:
        peer = self.get(peer_id)
        if peer is None:
            return None
        for key, value in fields.items():
            setattr(peer, key, value)
        peer.updated_at = utcnow()
        self.session.flush()
        return peer

    def record_success(self, peer_id: str) -> None:
        peer = self.get(peer_id)
        if peer is None:
            return
        peer.last_success_at = utcnow()
        peer.consecutive_failures = 0
        peer.last_error = None
        self.session.flush()

    def record_failure(self, peer_id: str, *, error: str | None) -> None:
        peer = self.get(peer_id)
        if peer is None:
            return
        peer.last_failure_at = utcnow()
        peer.consecutive_failures = (peer.consecutive_failures or 0) + 1
        peer.last_error = error
        self.session.flush()


class MeshRepository:
    """CRUD over :class:`Mesh` / :class:`MeshMember` rows (beta.3 first-class mesh model).

    Holds the local node's own truth about which meshes it belongs to and who else is a member.
    The transport ingress reads :meth:`mesh_views` to decide whether an authenticated peer
    message is authorized; the sender never supplies this data.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- meshes -----------------------------------------------------------------

    def create_mesh(
        self,
        *,
        display_name: str,
        authority_type: str,
        authority_id: str,
        owner_node_id: str | None = None,
        scopes: list[str] | None = None,
        mesh_id: str | None = None,
        synced: bool = False,
    ) -> Mesh:
        mesh = Mesh(
            display_name=display_name,
            authority_type=authority_type,
            authority_id=authority_id,
            owner_node_id=owner_node_id,
            scopes_json=list(scopes or []),
            synced=synced,
        )
        if mesh_id:
            mesh.id = mesh_id
        self.session.add(mesh)
        self.session.flush()
        return mesh

    def get_mesh(self, mesh_id: str) -> Mesh | None:
        return self.session.get(Mesh, mesh_id)

    def list_meshes(self, *, include_revoked: bool = True) -> list[Mesh]:
        stmt = select(Mesh).order_by(Mesh.created_at.asc())
        if not include_revoked:
            stmt = stmt.where(Mesh.revoked == False)  # noqa: E712
        return list(self.session.scalars(stmt))

    def revoke_mesh(self, mesh_id: str) -> Mesh | None:
        mesh = self.get_mesh(mesh_id)
        if mesh is None:
            return None
        mesh.revoked = True
        mesh.updated_at = utcnow()
        self.session.flush()
        return mesh

    def update_mesh(self, mesh_id: str, *, fields: dict) -> Mesh | None:
        mesh = self.get_mesh(mesh_id)
        if mesh is None:
            return None
        for key, value in fields.items():
            setattr(mesh, key, value)
        mesh.updated_at = utcnow()
        self.session.flush()
        return mesh

    # -- members ----------------------------------------------------------------

    def upsert_member(
        self,
        *,
        mesh_id: str,
        node_id: str,
        public_key: str | None = None,
        fingerprint: str | None = None,
        display_name: str | None = None,
        role: str = "member",
        scopes: list[str] | None = None,
        is_self: bool = False,
    ) -> MeshMember:
        member = self.get_member(mesh_id, node_id)
        if member is None:
            member = MeshMember(mesh_id=mesh_id, node_id=node_id, is_self=is_self)
            self.session.add(member)
        member.public_key = public_key if public_key is not None else member.public_key
        member.fingerprint = fingerprint if fingerprint is not None else member.fingerprint
        if display_name is not None:
            member.display_name = display_name
        member.role = role
        if scopes is not None:
            member.scopes_json = list(scopes)
        if is_self:
            member.is_self = True
        member.revoked = False
        member.updated_at = utcnow()
        self.session.flush()
        return member

    def get_member(self, mesh_id: str, node_id: str) -> MeshMember | None:
        stmt = (
            select(MeshMember)
            .where(MeshMember.mesh_id == mesh_id, MeshMember.node_id == node_id)
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list_members(self, mesh_id: str, *, include_revoked: bool = True) -> list[MeshMember]:
        stmt = select(MeshMember).where(MeshMember.mesh_id == mesh_id)
        if not include_revoked:
            stmt = stmt.where(MeshMember.revoked == False)  # noqa: E712
        return list(self.session.scalars(stmt.order_by(MeshMember.added_at.asc())))

    def revoke_member(self, mesh_id: str, node_id: str) -> MeshMember | None:
        member = self.get_member(mesh_id, node_id)
        if member is None:
            return None
        member.revoked = True
        member.updated_at = utcnow()
        self.session.flush()
        return member

    # -- views (for the transport mesh guard) -----------------------------------

    def mesh_views(self) -> list:
        """Return secret-free :class:`aithernet.mesh.MeshView` objects for every non-revoked mesh.

        This is the local node's authoritative membership truth used by the ingress mesh guard.
        """
        from aithernet.mesh import DEFAULT_MESH_SCOPES, MeshMemberView, MeshView

        views: list = []
        for mesh in self.list_meshes(include_revoked=False):
            members = tuple(
                MeshMemberView(
                    node_id=m.node_id,
                    fingerprint=m.fingerprint,
                    role=m.role,
                    scopes=tuple(m.scopes_json or ()),
                    revoked=bool(m.revoked),
                )
                for m in self.list_members(mesh.id)
            )
            views.append(
                MeshView(
                    mesh_id=mesh.id,
                    display_name=mesh.display_name,
                    authority_type=mesh.authority_type,
                    authority_id=mesh.authority_id,
                    policy_version=mesh.policy_version,
                    scopes=tuple(mesh.scopes_json) if mesh.scopes_json else DEFAULT_MESH_SCOPES,
                    revoked=bool(mesh.revoked),
                    members=members,
                )
            )
        return views


class AgentOutboxRepository:
    """CRUD + atomic delivery-claim operations for :class:`AgentOutboxMessage` (Stage 13A)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        message_id: str,
        peer_id: str,
        kind: str,
        envelope: dict,
        envelope_hash: str,
        protocol_version: str = "1",
        conversation_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        reply_to_message_id: str | None = None,
        mission_id: str | None = None,
        mission_run_id: str | None = None,
        mission_step_id: str | None = None,
        application_type: str | None = None,
        application_request_id: str | None = None,
        reply_to_request_id: str | None = None,
        expects_reply: bool = False,
        expires_at: datetime | None = None,
        max_attempts: int = 5,
        next_attempt_at: datetime | None = None,
    ) -> AgentOutboxMessage:
        record = AgentOutboxMessage(
            message_id=message_id,
            peer_id=peer_id,
            kind=kind,
            protocol_version=protocol_version,
            envelope_hash=envelope_hash,
            envelope_json=envelope,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            reply_to_message_id=reply_to_message_id,
            mission_id=mission_id,
            mission_run_id=mission_run_id,
            mission_step_id=mission_step_id,
            application_type=application_type,
            application_request_id=application_request_id,
            reply_to_request_id=reply_to_request_id,
            expects_reply=expects_reply,
            expires_at=expires_at,
            status="pending",
            max_attempts=max_attempts,
            next_attempt_at=next_attempt_at or utcnow(),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, record_id: str) -> AgentOutboxMessage | None:
        return self.session.get(AgentOutboxMessage, record_id)

    def get_by_message_id(self, message_id: str) -> AgentOutboxMessage | None:
        stmt = select(AgentOutboxMessage).where(AgentOutboxMessage.message_id == message_id)
        return self.session.scalars(stmt).first()

    def first_by_application_request_id(self, request_id: str) -> AgentOutboxMessage | None:
        """The oldest outbound message carrying this application request id (Stage 13D)."""
        stmt = (
            select(AgentOutboxMessage)
            .where(AgentOutboxMessage.application_request_id == request_id)
            .order_by(AgentOutboxMessage.created_at.asc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list(
        self, *, status: str | None = None, peer_id: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[AgentOutboxMessage]:
        stmt = select(AgentOutboxMessage).order_by(AgentOutboxMessage.created_at.desc())
        if status is not None:
            stmt = stmt.where(AgentOutboxMessage.status == status)
        if peer_id is not None:
            stmt = stmt.where(AgentOutboxMessage.peer_id == peer_id)
        return list(self.session.scalars(stmt.limit(limit).offset(offset)))

    def counts_by_status(self) -> dict[str, int]:
        stmt = select(AgentOutboxMessage.status, func.count()).group_by(AgentOutboxMessage.status)
        return {status: int(count) for status, count in self.session.execute(stmt)}

    def due_ids(self, now: datetime, *, limit: int = 50) -> list[str]:
        """Ids of records eligible for a delivery claim (due + claim free/expired)."""
        stmt = (
            select(AgentOutboxMessage.id)
            .where(AgentOutboxMessage.status.in_(_CLAIMABLE_OUTBOX_STATES))
            .where(
                or_(
                    AgentOutboxMessage.next_attempt_at.is_(None),
                    AgentOutboxMessage.next_attempt_at <= now,
                )
            )
            .where(
                or_(
                    AgentOutboxMessage.claim_owner.is_(None),
                    AgentOutboxMessage.claim_expires_at < now,
                )
            )
            .order_by(AgentOutboxMessage.next_attempt_at.asc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def claim(
        self, record_id: str, *, owner: str, now: datetime, claim_expiry: datetime
    ) -> int:
        """Atomically claim a due record for delivery. Returns 1 if claimed, else 0.

        SQLite serializes the conditional UPDATE, so two workers can never both claim the
        same record. The attempt counter is incremented as part of the claim.
        """
        stmt = (
            update(AgentOutboxMessage)
            .where(AgentOutboxMessage.id == record_id)
            .where(AgentOutboxMessage.status.in_(_CLAIMABLE_OUTBOX_STATES))
            .where(
                or_(
                    AgentOutboxMessage.next_attempt_at.is_(None),
                    AgentOutboxMessage.next_attempt_at <= now,
                )
            )
            .where(
                or_(
                    AgentOutboxMessage.claim_owner.is_(None),
                    AgentOutboxMessage.claim_expires_at < now,
                )
            )
            .values(
                status="in_flight",
                claim_owner=owner,
                claim_expires_at=claim_expiry,
                attempt_count=AgentOutboxMessage.attempt_count + 1,
                first_attempted_at=func.coalesce(AgentOutboxMessage.first_attempted_at, now),
                last_attempted_at=now,
                updated_at=now,
            )
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def release_and_update(self, record_id: str, *, owner: str, fields: dict) -> None:
        """Apply terminal/retry fields and clear the claim (only if this owner holds it)."""
        record = self.get(record_id)
        if record is None or record.claim_owner != owner:
            return
        for key, value in fields.items():
            setattr(record, key, value)
        record.claim_owner = None
        record.claim_expires_at = None
        record.updated_at = utcnow()
        self.session.flush()

    def update(self, record_id: str, *, fields: dict) -> AgentOutboxMessage | None:
        record = self.get(record_id)
        if record is None:
            return None
        for key, value in fields.items():
            setattr(record, key, value)
        record.updated_at = utcnow()
        self.session.flush()
        return record

    def recover_expired_claims(self, now: datetime) -> int:
        """Reset in-flight records whose claim lease expired (crash recovery). Same id reused."""
        stmt = (
            update(AgentOutboxMessage)
            .where(AgentOutboxMessage.status == "in_flight")
            .where(
                or_(
                    AgentOutboxMessage.claim_expires_at.is_(None),
                    AgentOutboxMessage.claim_expires_at < now,
                )
            )
            .values(
                status="retry_scheduled",
                claim_owner=None,
                claim_expires_at=None,
                next_attempt_at=now,
                updated_at=now,
            )
        )
        return int(self.session.execute(stmt).rowcount or 0)


class AgentInboxRepository:
    """Idempotent durable inbox for :class:`AgentInboxMessage` (Stage 13A)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_message_id(self, message_id: str) -> AgentInboxMessage | None:
        stmt = select(AgentInboxMessage).where(AgentInboxMessage.message_id == message_id)
        return self.session.scalars(stmt).first()

    def first_by_application_request_id(self, request_id: str) -> AgentInboxMessage | None:
        """The oldest inbound message carrying this application request id (Stage 13D)."""
        stmt = (
            select(AgentInboxMessage)
            .where(AgentInboxMessage.application_request_id == request_id)
            .order_by(AgentInboxMessage.received_at.asc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def by_reply_to_request_id(self, request_id: str) -> list[AgentInboxMessage]:
        """All inbound replies correlated to this request id, oldest first (Stage 13D)."""
        stmt = (
            select(AgentInboxMessage)
            .where(AgentInboxMessage.reply_to_request_id == request_id)
            .order_by(AgentInboxMessage.received_at.asc())
        )
        return list(self.session.scalars(stmt))

    def get(self, record_id: str) -> AgentInboxMessage | None:
        return self.session.get(AgentInboxMessage, record_id)

    def create(
        self,
        *,
        message_id: str,
        peer_id: str | None,
        sender_node_id: str,
        sender_fingerprint: str,
        kind: str,
        envelope_hash: str,
        envelope: dict,
        payload: dict,
        protocol_version: str = "1",
        conversation_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        reply_to_message_id: str | None = None,
        mission_id: str | None = None,
        mission_run_id: str | None = None,
        mission_step_id: str | None = None,
        ack_status: str = "accepted",
        ack_id: str | None = None,
    ) -> AgentInboxMessage:
        record = AgentInboxMessage(
            message_id=message_id,
            peer_id=peer_id,
            sender_node_id=sender_node_id,
            sender_fingerprint=sender_fingerprint,
            kind=kind,
            protocol_version=protocol_version,
            envelope_hash=envelope_hash,
            envelope_json=envelope,
            payload_json=payload,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            reply_to_message_id=reply_to_message_id,
            mission_id=mission_id,
            mission_run_id=mission_run_id,
            mission_step_id=mission_step_id,
            processing_state="stored",
            ack_status=ack_status,
            ack_id=ack_id,
            authenticated_at=utcnow(),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def increment_duplicate(self, message_id: str) -> AgentInboxMessage | None:
        record = self.get_by_message_id(message_id)
        if record is None:
            return None
        record.duplicate_count = (record.duplicate_count or 0) + 1
        self.session.flush()
        return record

    def list(
        self, *, peer_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[AgentInboxMessage]:
        stmt = select(AgentInboxMessage).order_by(AgentInboxMessage.received_at.desc())
        if peer_id is not None:
            stmt = stmt.where(AgentInboxMessage.peer_id == peer_id)
        return list(self.session.scalars(stmt.limit(limit).offset(offset)))

    def count(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(AgentInboxMessage)) or 0)


# -- Stage 13A.5: RF artifacts + per-backend workspace context -------------------


class RFArtifactRepository:
    """Bounded, audited references to backend workspace artifacts (never the bytes)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find(self, *, node_id: str, backend_id: str, relative_path: str) -> RFArtifact | None:
        stmt = (
            select(RFArtifact)
            .where(RFArtifact.node_id == node_id)
            .where(RFArtifact.backend_id == backend_id)
            .where(RFArtifact.relative_path == relative_path)
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def upsert(
        self,
        *,
        node_id: str,
        backend_id: str,
        relative_path: str,
        artifact_kind: str = "unknown",
        media_type: str | None = None,
        size_bytes: int | None = None,
        content_hash: str | None = None,
        mcp_call_id: str | None = None,
        mission_id: str | None = None,
        mission_run_id: str | None = None,
        mission_step_id: str | None = None,
    ) -> tuple[RFArtifact, bool]:
        """Insert an artifact reference, or update the existing one (idempotent). Returns
        ``(artifact, created)``."""
        existing = self.find(node_id=node_id, backend_id=backend_id, relative_path=relative_path)
        if existing is not None:
            existing.observed_at = utcnow()
            if size_bytes is not None:
                existing.size_bytes = size_bytes
            if content_hash is not None:
                existing.content_hash = content_hash
            if mcp_call_id is not None:
                existing.mcp_call_id = mcp_call_id
            self.session.flush()
            return existing, False
        artifact = RFArtifact(
            node_id=node_id,
            backend_id=backend_id,
            relative_path=relative_path,
            artifact_kind=artifact_kind,
            media_type=media_type,
            size_bytes=size_bytes,
            content_hash=content_hash,
            mcp_call_id=mcp_call_id,
            mission_id=mission_id,
            mission_run_id=mission_run_id,
            mission_step_id=mission_step_id,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact, True

    def get(self, artifact_id: str) -> RFArtifact | None:
        return self.session.get(RFArtifact, artifact_id)

    def list(
        self, *, backend_id: str | None = None, mission_id: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[RFArtifact]:
        stmt = select(RFArtifact).order_by(RFArtifact.created_at.desc())
        if backend_id is not None:
            stmt = stmt.where(RFArtifact.backend_id == backend_id)
        if mission_id is not None:
            stmt = stmt.where(RFArtifact.mission_id == mission_id)
        return list(self.session.scalars(stmt.limit(limit).offset(offset)))

    def count(self, *, backend_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(RFArtifact)
        if backend_id is not None:
            stmt = stmt.where(RFArtifact.backend_id == backend_id)
        return int(self.session.scalar(stmt) or 0)

    def update(self, artifact_id: str, *, fields: dict) -> RFArtifact | None:
        """Apply an additive field update (Stage 13D.2 identity/availability/pin)."""
        record = self.get(artifact_id)
        if record is None:
            return None
        for key, value in fields.items():
            setattr(record, key, value)
        record.observed_at = utcnow()
        self.session.flush()
        return record

    def by_digest(self, digest: str) -> list[RFArtifact]:
        """All local artifact records sharing a content digest (Stage 13D.2 dedup linkage)."""
        stmt = select(RFArtifact).where(RFArtifact.digest == digest)
        return list(self.session.scalars(stmt))

    def referenced_digests(self) -> set[str]:
        """Every digest still referenced by a live artifact record (for pin-aware GC)."""
        stmt = select(RFArtifact.digest).where(RFArtifact.digest.is_not(None))
        return {row for row in self.session.scalars(stmt) if row}

    def pinned_digests(self) -> set[str]:
        stmt = select(RFArtifact.digest).where(RFArtifact.pinned.is_(True)).where(
            RFArtifact.digest.is_not(None)
        )
        return {row for row in self.session.scalars(stmt) if row}


class ArtifactTransferRepository:
    """Durable cross-node artifact transfers (Stage 13D.2). Bytes live on disk, never here."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> ArtifactTransfer:
        record = ArtifactTransfer(**fields)
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, record_id: str) -> ArtifactTransfer | None:
        return self.session.get(ArtifactTransfer, record_id)

    def get_by_transfer_id(self, transfer_id: str) -> ArtifactTransfer | None:
        stmt = select(ArtifactTransfer).where(ArtifactTransfer.transfer_id == transfer_id).limit(1)
        return self.session.scalars(stmt).first()

    def update(self, transfer_id: str, *, fields: dict) -> ArtifactTransfer | None:
        record = self.get_by_transfer_id(transfer_id)
        if record is None:
            return None
        for key, value in fields.items():
            setattr(record, key, value)
        self.session.flush()
        return record

    def list(self, *, direction: str | None = None, state: str | None = None,
             peer_id: str | None = None, limit: int = 100,
             offset: int = 0) -> list[ArtifactTransfer]:
        stmt = select(ArtifactTransfer).order_by(ArtifactTransfer.created_at.desc())
        if direction is not None:
            stmt = stmt.where(ArtifactTransfer.direction == direction)
        if state is not None:
            stmt = stmt.where(ArtifactTransfer.state == state)
        if peer_id is not None:
            stmt = stmt.where(ArtifactTransfer.peer_id == peer_id)
        return list(self.session.scalars(stmt.limit(limit).offset(offset)))

    def claimable_inbound(self, now: datetime, *, limit: int = 20) -> list[ArtifactTransfer]:
        """Inbound transfers ready to (re)start: not terminal, due, and not actively claimed."""
        active = ("authorized", "transferring", "paused", "verifying")
        stmt = (
            select(ArtifactTransfer)
            .where(ArtifactTransfer.direction == "inbound")
            .where(ArtifactTransfer.state.in_(active))
            .where(or_(ArtifactTransfer.next_attempt_at.is_(None),
                       ArtifactTransfer.next_attempt_at <= now))
            .where(or_(ArtifactTransfer.claim_expires_at.is_(None),
                       ArtifactTransfer.claim_expires_at <= now))
            .order_by(ArtifactTransfer.created_at.asc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def claim(self, transfer_id: str, *, owner: str, lease_until: datetime) -> int:
        """Atomically claim an inbound transfer for one worker. Returns 1 on success."""
        now = utcnow()
        stmt = (
            update(ArtifactTransfer)
            .where(ArtifactTransfer.transfer_id == transfer_id)
            .where(or_(ArtifactTransfer.claim_expires_at.is_(None),
                       ArtifactTransfer.claim_expires_at <= now))
            .values(claim_owner=owner, claim_expires_at=lease_until)
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def counts_by_state(self) -> dict[str, int]:
        stmt = select(ArtifactTransfer.state, func.count()).group_by(ArtifactTransfer.state)
        return {state: int(count) for state, count in self.session.execute(stmt)}

    def active_count_for_peer(self, peer_id: str) -> int:
        active = ("authorized", "transferring", "verifying")
        stmt = (
            select(func.count()).select_from(ArtifactTransfer)
            .where(ArtifactTransfer.peer_id == peer_id)
            .where(ArtifactTransfer.direction == "inbound")
            .where(ArtifactTransfer.state.in_(active))
        )
        return int(self.session.scalar(stmt) or 0)


class ArtifactTransferAttemptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> ArtifactTransferAttempt:
        record = ArtifactTransferAttempt(**fields)
        self.session.add(record)
        self.session.flush()
        return record

    def update(self, attempt_id: str, *, fields: dict) -> ArtifactTransferAttempt | None:
        record = self.session.get(ArtifactTransferAttempt, attempt_id)
        if record is None:
            return None
        for key, value in fields.items():
            setattr(record, key, value)
        self.session.flush()
        return record

    def list_for_transfer(self, transfer_id: str) -> list[ArtifactTransferAttempt]:
        stmt = (
            select(ArtifactTransferAttempt)
            .where(ArtifactTransferAttempt.transfer_id == transfer_id)
            .order_by(ArtifactTransferAttempt.started_at.asc())
        )
        return list(self.session.scalars(stmt))


class RemoteArtifactReferenceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, ref_id: str) -> RemoteArtifactReference | None:
        return self.session.get(RemoteArtifactReference, ref_id)

    def find(self, *, peer_id: str, origin_artifact_id: str) -> RemoteArtifactReference | None:
        stmt = (
            select(RemoteArtifactReference)
            .where(RemoteArtifactReference.peer_id == peer_id)
            .where(RemoteArtifactReference.origin_artifact_id == origin_artifact_id)
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def upsert(self, **fields) -> tuple[RemoteArtifactReference, bool]:
        existing = self.find(peer_id=fields["peer_id"],
                             origin_artifact_id=fields["origin_artifact_id"])
        if existing is not None:
            for key in ("digest", "size_bytes", "artifact_kind", "display_name",
                        "conversation_id", "request_id"):
                if fields.get(key) is not None:
                    setattr(existing, key, fields[key])
            self.session.flush()
            return existing, False
        record = RemoteArtifactReference(**fields)
        self.session.add(record)
        self.session.flush()
        return record, True

    def update(self, ref_id: str, *, fields: dict) -> RemoteArtifactReference | None:
        record = self.get(ref_id)
        if record is None:
            return None
        for key, value in fields.items():
            setattr(record, key, value)
        self.session.flush()
        return record

    def list(self, *, peer_id: str | None = None, limit: int = 100,
             offset: int = 0) -> list[RemoteArtifactReference]:
        stmt = select(RemoteArtifactReference).order_by(RemoteArtifactReference.offered_at.desc())
        if peer_id is not None:
            stmt = stmt.where(RemoteArtifactReference.peer_id == peer_id)
        return list(self.session.scalars(stmt.limit(limit).offset(offset)))


class ArtifactPermissionRepository:
    """Per-peer artifact authorization updates (separate from key trust + message perms)."""

    _ALLOWED = frozenset({
        "may_offer_artifacts", "may_request_artifacts", "may_receive_artifacts",
        "max_artifact_bytes", "max_concurrent_transfers", "allowed_artifact_kinds_json",
    })

    def __init__(self, session: Session) -> None:
        self.session = session

    def update(self, peer_id: str, *, fields: dict) -> ExternalAgent | None:
        peer = self.session.get(ExternalAgent, peer_id)
        if peer is None:
            return None
        for key, value in fields.items():
            if key in self._ALLOWED:
                setattr(peer, key, value)
        peer.updated_at = utcnow()
        self.session.flush()
        return peer


class MissionStatusPermissionRepository:
    """Per-peer mission-status authorization (separate from trust + message + artifact perms)."""

    _ALLOWED = frozenset({
        "may_publish_mission_status", "may_query_mission_status",
        "may_receive_mission_status", "max_active_remote_snapshots",
    })

    def __init__(self, session: Session) -> None:
        self.session = session

    def update(self, peer_id: str, *, fields: dict) -> ExternalAgent | None:
        peer = self.session.get(ExternalAgent, peer_id)
        if peer is None:
            return None
        for key, value in fields.items():
            if key in self._ALLOWED:
                setattr(peer, key, value)
        peer.updated_at = utcnow()
        self.session.flush()
        return peer


class LocalMissionStatusPublicationRepository:
    """Per-(mission, peer) status-publication bookkeeping + monotonic sequence (Stage 13D.3)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, mission_id: str, peer_id: str) -> LocalMissionStatusPublication | None:
        stmt = (
            select(LocalMissionStatusPublication)
            .where(LocalMissionStatusPublication.mission_id == mission_id)
            .where(LocalMissionStatusPublication.peer_id == peer_id)
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def get_or_create(self, *, mission_id: str, peer_id: str, request_id: str | None,
                      conversation_id: str | None) -> LocalMissionStatusPublication:
        record = self.get(mission_id, peer_id)
        if record is None:
            record = LocalMissionStatusPublication(
                mission_id=mission_id, peer_id=peer_id, request_id=request_id,
                conversation_id=conversation_id,
            )
            self.session.add(record)
            self.session.flush()
        return record

    def list_for_mission(self, mission_id: str) -> list[LocalMissionStatusPublication]:
        stmt = select(LocalMissionStatusPublication).where(
            LocalMissionStatusPublication.mission_id == mission_id
        )
        return list(self.session.scalars(stmt))


class RemoteMissionSnapshotRepository:
    """Latest authenticated remote-mission snapshot per (peer, remote mission) (Stage 13D.3)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, snapshot_id: str) -> RemoteMissionSnapshot | None:
        return self.session.get(RemoteMissionSnapshot, snapshot_id)

    def find(self, *, peer_id: str, remote_mission_ref: str) -> RemoteMissionSnapshot | None:
        stmt = (
            select(RemoteMissionSnapshot)
            .where(RemoteMissionSnapshot.peer_id == peer_id)
            .where(RemoteMissionSnapshot.remote_mission_ref == remote_mission_ref)
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def create(self, **fields) -> RemoteMissionSnapshot:
        record = RemoteMissionSnapshot(**fields)
        self.session.add(record)
        self.session.flush()
        return record

    def list(self, *, peer_id: str | None = None, local_mission_id: str | None = None,
             limit: int = 100, offset: int = 0) -> list[RemoteMissionSnapshot]:
        stmt = select(RemoteMissionSnapshot).order_by(RemoteMissionSnapshot.received_at.desc())
        if peer_id is not None:
            stmt = stmt.where(RemoteMissionSnapshot.peer_id == peer_id)
        if local_mission_id is not None:
            stmt = stmt.where(RemoteMissionSnapshot.local_mission_id == local_mission_id)
        return list(self.session.scalars(stmt.limit(limit).offset(offset)))

    def active_count_for_peer(self, peer_id: str) -> int:
        stmt = (
            select(func.count()).select_from(RemoteMissionSnapshot)
            .where(RemoteMissionSnapshot.peer_id == peer_id)
            .where(RemoteMissionSnapshot.terminal_category.is_(None))
        )
        return int(self.session.scalar(stmt) or 0)


class RemoteMissionStatusEventRepository:
    """Append-only evidence of received remote status messages + dispositions (Stage 13D.3)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> RemoteMissionStatusEvent:
        record = RemoteMissionStatusEvent(**fields)
        self.session.add(record)
        self.session.flush()
        return record

    def list_for_snapshot(self, snapshot_id: str, *, limit: int = 200) -> list[
        RemoteMissionStatusEvent
    ]:
        stmt = (
            select(RemoteMissionStatusEvent)
            .where(RemoteMissionStatusEvent.snapshot_id == snapshot_id)
            .order_by(RemoteMissionStatusEvent.received_at.asc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))


class RFWorkspaceContextRepository:
    """Per-(node, backend) generic RF workspace context."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, node_id: str, backend_id: str) -> RFWorkspaceContextRecord | None:
        stmt = (
            select(RFWorkspaceContextRecord)
            .where(RFWorkspaceContextRecord.node_id == node_id)
            .where(RFWorkspaceContextRecord.backend_id == backend_id)
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def get_or_create(self, node_id: str, backend_id: str) -> RFWorkspaceContextRecord:
        record = self.get(node_id, backend_id)
        if record is None:
            record = RFWorkspaceContextRecord(node_id=node_id, backend_id=backend_id)
            self.session.add(record)
            self.session.flush()
        return record

    def list(self, node_id: str) -> list[RFWorkspaceContextRecord]:
        stmt = (
            select(RFWorkspaceContextRecord)
            .where(RFWorkspaceContextRecord.node_id == node_id)
            .order_by(RFWorkspaceContextRecord.backend_id.asc())
        )
        return list(self.session.scalars(stmt))

    def update(
        self, node_id: str, backend_id: str, *, fields: dict, bump_version: bool = True
    ) -> RFWorkspaceContextRecord:
        record = self.get_or_create(node_id, backend_id)
        for key, value in fields.items():
            setattr(record, key, value)
        if bump_version:
            record.context_version = (record.context_version or 0) + 1
        self.session.flush()
        return record


class RFBenchmarkRepository:
    """Persistence for operator-run RF comparative benchmarks (Stage 13A.5)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, *, node_id: str, scenario_id: str, scenario_version: str,
               backend_id: str, **fields) -> RFBenchmarkRun:
        run = RFBenchmarkRun(
            node_id=node_id, scenario_id=scenario_id, scenario_version=scenario_version,
            backend_id=backend_id, **fields,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def get(self, run_id: str) -> RFBenchmarkRun | None:
        return self.session.get(RFBenchmarkRun, run_id)

    def list(self, *, scenario_id: str | None = None, backend_id: str | None = None,
             limit: int = 100, offset: int = 0) -> list[RFBenchmarkRun]:
        stmt = select(RFBenchmarkRun).order_by(RFBenchmarkRun.created_at.desc())
        if scenario_id is not None:
            stmt = stmt.where(RFBenchmarkRun.scenario_id == scenario_id)
        if backend_id is not None:
            stmt = stmt.where(RFBenchmarkRun.backend_id == backend_id)
        return list(self.session.scalars(stmt.limit(limit).offset(offset)))


# -- Stage 13B: peer authorization + reply waits + inbound requests --------------


class PeerPermissionRepository:
    """Application-authorization updates on :class:`ExternalAgent` (separate from key trust)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, peer_id: str) -> ExternalAgent | None:
        return self.session.get(ExternalAgent, peer_id)

    def update(self, peer_id: str, *, fields: dict) -> ExternalAgent | None:
        peer = self.get(peer_id)
        if peer is None:
            return None
        for key, value in fields.items():
            setattr(peer, key, value)
        peer.updated_at = utcnow()
        self.session.flush()
        return peer


class MissionReplyWaitRepository:
    """Durable reply waits with an ATOMIC satisfy/timeout/resume claim (Stage 13B)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        mission_id: str,
        mission_run_id: str,
        mission_step_id: str | None,
        outbound_message_id: str,
        request_id: str,
        conversation_id: str | None,
        expected_peer_id: str,
        deadline: datetime | None,
        reason: str | None = None,
    ) -> MissionReplyWait:
        wait = MissionReplyWait(
            mission_id=mission_id,
            mission_run_id=mission_run_id,
            mission_step_id=mission_step_id,
            outbound_message_id=outbound_message_id,
            request_id=request_id,
            conversation_id=conversation_id,
            expected_peer_id=expected_peer_id,
            deadline=deadline,
            reason=reason,
        )
        self.session.add(wait)
        self.session.flush()
        return wait

    def get(self, wait_id: str) -> MissionReplyWait | None:
        return self.session.get(MissionReplyWait, wait_id)

    def latest_for_request(self, request_id: str) -> MissionReplyWait | None:
        """The most recent reply wait (any state) for a request id (Stage 13D resolution)."""
        stmt = (
            select(MissionReplyWait)
            .where(MissionReplyWait.request_id == request_id)
            .order_by(MissionReplyWait.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def pending_for_request(self, request_id: str) -> MissionReplyWait | None:
        stmt = (
            select(MissionReplyWait)
            .where(MissionReplyWait.request_id == request_id)
            .where(MissionReplyWait.state == "pending")
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list_for_mission(self, mission_id: str) -> list[MissionReplyWait]:
        stmt = (
            select(MissionReplyWait)
            .where(MissionReplyWait.mission_id == mission_id)
            .order_by(MissionReplyWait.created_at.desc())
        )
        return list(self.session.scalars(stmt))

    def pending_for_mission(self, mission_id: str) -> list[MissionReplyWait]:
        stmt = (
            select(MissionReplyWait)
            .where(MissionReplyWait.mission_id == mission_id)
            .where(MissionReplyWait.state == "pending")
        )
        return list(self.session.scalars(stmt))

    def list(self, *, state: str | None = None, conversation_id: str | None = None,
             limit: int = 100, offset: int = 0) -> list[MissionReplyWait]:
        """Return reply waits, newest first, optionally filtered by state/conversation."""
        stmt = select(MissionReplyWait).order_by(MissionReplyWait.created_at.desc())
        if state is not None:
            stmt = stmt.where(MissionReplyWait.state == state)
        if conversation_id is not None:
            stmt = stmt.where(MissionReplyWait.conversation_id == conversation_id)
        return list(self.session.scalars(stmt.limit(limit).offset(offset)))

    def counts_by_state(self) -> dict[str, int]:
        """Return ``{state: count}`` over all reply waits in a single grouped query."""
        stmt = select(MissionReplyWait.state, func.count()).group_by(MissionReplyWait.state)
        return {state: int(count) for state, count in self.session.execute(stmt)}

    def satisfy(
        self, wait_id: str, *, inbox_message_id: str, transport_message_id: str | None,
        now: datetime,
    ) -> int:
        """Atomically satisfy a pending wait. Returns 1 if claimed (one reply satisfies once)."""
        stmt = (
            update(MissionReplyWait)
            .where(MissionReplyWait.id == wait_id)
            .where(MissionReplyWait.state == "pending")
            .values(
                state="satisfied", satisfied_at=now,
                satisfying_inbox_message_id=inbox_message_id,
                satisfying_transport_message_id=transport_message_id,
            )
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def time_out(self, wait_id: str, *, now: datetime) -> int:
        """Atomically time out a pending wait. Returns 1 if claimed."""
        stmt = (
            update(MissionReplyWait)
            .where(MissionReplyWait.id == wait_id)
            .where(MissionReplyWait.state == "pending")
            .values(state="timed_out", timed_out_at=now)
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def claim_resume(self, wait_id: str, *, result: str, now: datetime) -> int:
        """Atomically claim the mission-resume for a wait. Returns 1 if this caller owns it."""
        stmt = (
            update(MissionReplyWait)
            .where(MissionReplyWait.id == wait_id)
            .where(MissionReplyWait.resume_result.is_(None))
            .values(resume_result=result, resume_attempt_at=now)
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def cancel_pending_for_mission(self, mission_id: str, *, now: datetime) -> int:
        stmt = (
            update(MissionReplyWait)
            .where(MissionReplyWait.mission_id == mission_id)
            .where(MissionReplyWait.state == "pending")
            .values(state="cancelled", cancelled_at=now)
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def cancel(self, wait_id: str, *, now: datetime) -> int:
        stmt = (
            update(MissionReplyWait)
            .where(MissionReplyWait.id == wait_id)
            .where(MissionReplyWait.state == "pending")
            .values(state="cancelled", cancelled_at=now)
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def due_timeout_ids(self, now: datetime, *, limit: int = 100) -> list[str]:
        stmt = (
            select(MissionReplyWait.id)
            .where(MissionReplyWait.state == "pending")
            .where(MissionReplyWait.deadline.is_not(None))
            .where(MissionReplyWait.deadline <= now)
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def unresumed_terminal(self, *, limit: int = 200) -> list[MissionReplyWait]:
        """Satisfied/timed-out waits whose mission resume was never completed (recovery)."""
        stmt = (
            select(MissionReplyWait)
            .where(MissionReplyWait.state.in_(("satisfied", "timed_out")))
            .where(MissionReplyWait.resume_result.is_(None))
            .limit(limit)
        )
        return list(self.session.scalars(stmt))


class InboundRequestRepository:
    """One mission per authenticated inbound request id (Stage 13B)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_request_id(self, request_id: str) -> InboundRequest | None:
        stmt = select(InboundRequest).where(InboundRequest.request_id == request_id).limit(1)
        return self.session.scalars(stmt).first()

    def create(
        self,
        *,
        request_id: str,
        peer_id: str,
        sender_node_id: str,
        conversation_id: str | None,
        inbox_message_id: str,
        envelope_hash: str | None,
        response_required: bool,
        state: str = "received",
    ) -> InboundRequest:
        record = InboundRequest(
            request_id=request_id,
            peer_id=peer_id,
            sender_node_id=sender_node_id,
            conversation_id=conversation_id,
            inbox_message_id=inbox_message_id,
            envelope_hash=envelope_hash,
            response_required=response_required,
            state=state,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def update(self, request_id: str, *, fields: dict) -> InboundRequest | None:
        record = self.get_by_request_id(request_id)
        if record is None:
            return None
        for key, value in fields.items():
            setattr(record, key, value)
        self.session.flush()
        return record

    def list(self, *, limit: int = 100, offset: int = 0) -> list[InboundRequest]:
        stmt = (
            select(InboundRequest)
            .order_by(InboundRequest.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(stmt))

    def count_open_for_peer(self, peer_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(InboundRequest)
            .where(InboundRequest.peer_id == peer_id)
            .where(InboundRequest.state == "mission_created")
        )
        return int(self.session.scalar(stmt) or 0)

    def count(self) -> int:
        """Return the total number of inbound requests."""
        return int(self.session.scalar(select(func.count()).select_from(InboundRequest)) or 0)

    def counts_by_state(self) -> dict[str, int]:
        """Return ``{state: count}`` over all inbound requests in a single grouped query."""
        stmt = select(InboundRequest.state, func.count()).group_by(InboundRequest.state)
        return {state: int(count) for state, count in self.session.execute(stmt)}


# -- Stage 14B managed SDR hardware -----------------------------------------------------------


class SDRDeviceRepository:
    """CRUD + presence/health/status transitions for :class:`SDRDevice` rows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> SDRDevice:
        device = SDRDevice(**fields)
        self.session.add(device)
        self.session.flush()
        return device

    def get(self, device_id: str) -> SDRDevice | None:
        return self.session.get(SDRDevice, device_id)

    def get_by_key(self, *, node_id: str, hardware_key: str) -> SDRDevice | None:
        stmt = (
            select(SDRDevice)
            .where(SDRDevice.node_id == node_id)
            .where(SDRDevice.hardware_key == hardware_key)
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list(
        self,
        *,
        node_id: str | None = None,
        provider_id: str | None = None,
        status: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[SDRDevice]:
        stmt = select(SDRDevice).order_by(SDRDevice.first_seen_at.asc())
        if node_id is not None:
            stmt = stmt.where(SDRDevice.node_id == node_id)
        if provider_id is not None:
            stmt = stmt.where(SDRDevice.provider_id == provider_id)
        if status is not None:
            stmt = stmt.where(SDRDevice.status == status)
        stmt = stmt.limit(limit).offset(offset)
        return list(self.session.scalars(stmt))

    def update(self, device_id: str, **fields) -> SDRDevice | None:
        device = self.get(device_id)
        if device is None:
            return None
        for key, value in fields.items():
            setattr(device, key, value)
        self.session.flush()
        return device

    def count(self, *, node_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(SDRDevice)
        if node_id is not None:
            stmt = stmt.where(SDRDevice.node_id == node_id)
        return int(self.session.scalar(stmt) or 0)

    def counts_by_status(self, *, node_id: str | None = None) -> dict[str, int]:
        stmt = select(SDRDevice.status, func.count()).group_by(SDRDevice.status)
        if node_id is not None:
            stmt = stmt.where(SDRDevice.node_id == node_id)
        return {status: int(count) for status, count in self.session.execute(stmt)}


class SDRDeviceCapabilityRepository:
    """The current capability snapshot per device (one row, revisioned)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_for_device(self, device_id: str) -> SDRDeviceCapability | None:
        stmt = select(SDRDeviceCapability).where(
            SDRDeviceCapability.device_id == device_id
        ).limit(1)
        return self.session.scalars(stmt).first()

    def upsert(self, *, device_id: str, node_id: str, fields: dict) -> SDRDeviceCapability:
        existing = self.get_for_device(device_id)
        if existing is None:
            record = SDRDeviceCapability(
                device_id=device_id, node_id=node_id, revision=1, **fields
            )
            self.session.add(record)
            self.session.flush()
            return record
        existing.revision = (existing.revision or 0) + 1
        for key, value in fields.items():
            setattr(existing, key, value)
        self.session.flush()
        return existing


class SDRDeviceHealthEventRepository:
    """Append-only factual health/presence history per device."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> SDRDeviceHealthEvent:
        record = SDRDeviceHealthEvent(**fields)
        self.session.add(record)
        self.session.flush()
        return record

    def latest(self, device_id: str) -> SDRDeviceHealthEvent | None:
        stmt = (
            select(SDRDeviceHealthEvent)
            .where(SDRDeviceHealthEvent.device_id == device_id)
            .order_by(SDRDeviceHealthEvent.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list_for_device(self, device_id: str, *, limit: int = 50) -> list[SDRDeviceHealthEvent]:
        stmt = (
            select(SDRDeviceHealthEvent)
            .where(SDRDeviceHealthEvent.device_id == device_id)
            .order_by(SDRDeviceHealthEvent.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))


class SDRDeviceBindingRepository:
    """Administrator-approved device->RF-backend bindings."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, *, device_id: str, backend_id: str) -> SDRDeviceBackendBinding | None:
        stmt = (
            select(SDRDeviceBackendBinding)
            .where(SDRDeviceBackendBinding.device_id == device_id)
            .where(SDRDeviceBackendBinding.backend_id == backend_id)
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list_for_device(self, device_id: str) -> list[SDRDeviceBackendBinding]:
        stmt = select(SDRDeviceBackendBinding).where(
            SDRDeviceBackendBinding.device_id == device_id
        )
        return list(self.session.scalars(stmt))

    def list(self, *, node_id: str | None = None) -> list[SDRDeviceBackendBinding]:
        stmt = select(SDRDeviceBackendBinding)
        if node_id is not None:
            stmt = stmt.where(SDRDeviceBackendBinding.node_id == node_id)
        return list(self.session.scalars(stmt))

    def upsert(
        self, *, node_id: str, device_id: str, backend_id: str, device_args: dict,
        source: str = "config", enabled: bool = True, notes: str | None = None,
    ) -> SDRDeviceBackendBinding:
        existing = self.get(device_id=device_id, backend_id=backend_id)
        if existing is None:
            record = SDRDeviceBackendBinding(
                node_id=node_id, device_id=device_id, backend_id=backend_id,
                device_args_json=device_args, source=source, enabled=enabled, notes=notes,
            )
            self.session.add(record)
            self.session.flush()
            return record
        existing.device_args_json = device_args
        existing.source = source
        existing.enabled = enabled
        existing.notes = notes
        self.session.flush()
        return existing


_HOLDING_LEASE_STATES = ("active", "orphaned")


class SDRDeviceLeaseRepository:
    """Durable device leases with ATOMIC, conflict-checked grant (single conditional UPDATE)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> SDRDeviceLease:
        lease = SDRDeviceLease(**fields)
        self.session.add(lease)
        self.session.flush()
        return lease

    def get(self, lease_id: str) -> SDRDeviceLease | None:
        return self.session.get(SDRDeviceLease, lease_id)

    def list(
        self,
        *,
        node_id: str | None = None,
        device_id: str | None = None,
        mission_id: str | None = None,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[SDRDeviceLease]:
        stmt = select(SDRDeviceLease).order_by(SDRDeviceLease.created_at.desc())
        if node_id is not None:
            stmt = stmt.where(SDRDeviceLease.node_id == node_id)
        if device_id is not None:
            stmt = stmt.where(SDRDeviceLease.device_id == device_id)
        if mission_id is not None:
            stmt = stmt.where(SDRDeviceLease.mission_id == mission_id)
        if active_only:
            stmt = stmt.where(SDRDeviceLease.state.in_(_HOLDING_LEASE_STATES))
        stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def holding_for_device(self, device_id: str) -> list[SDRDeviceLease]:
        stmt = (
            select(SDRDeviceLease)
            .where(SDRDeviceLease.device_id == device_id)
            .where(SDRDeviceLease.state.in_(_HOLDING_LEASE_STATES))
        )
        return list(self.session.scalars(stmt))

    def grant(
        self,
        lease_id: str,
        *,
        device_id: str,
        mode: str,
        owner_worker_id: str,
        owner_node_id: str,
        token: str,
        now: datetime,
        expiry: datetime,
    ) -> int:
        """Atomically move a PENDING lease to ACTIVE iff no conflicting lease holds the device.

        Returns 1 on success, 0 on conflict/lost race. SQLite serializes the conditional UPDATE,
        and the NOT EXISTS subquery (over the SAME table, excluding this row) makes the
        no-conflicting-lease check atomic with the grant. An ``exclusive`` request conflicts with
        any holding lease; a ``shared_receive`` request conflicts only with a non-shared holder.
        """
        conflict = (
            select(SDRDeviceLease.id)
            .where(SDRDeviceLease.device_id == device_id)
            .where(SDRDeviceLease.id != lease_id)
            .where(SDRDeviceLease.state.in_(_HOLDING_LEASE_STATES))
        )
        if mode == "shared_receive":
            conflict = conflict.where(SDRDeviceLease.lease_mode != "shared_receive")
        stmt = (
            update(SDRDeviceLease)
            .where(SDRDeviceLease.id == lease_id)
            .where(SDRDeviceLease.state == "pending")
            .where(~conflict.exists())
            .values(
                state="active",
                lease_mode=mode,
                owner_worker_id=owner_worker_id,
                owner_node_id=owner_node_id,
                lease_token=token,
                acquired_at=now,
                renewed_at=now,
                expires_at=expiry,
            )
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def renew(self, lease_id: str, *, token: str, now: datetime, expiry: datetime) -> int:
        """Renew an active lease only when the caller holds the matching token (no MissionStep)."""
        stmt = (
            update(SDRDeviceLease)
            .where(SDRDeviceLease.id == lease_id)
            .where(SDRDeviceLease.lease_token == token)
            .where(SDRDeviceLease.state == "active")
            .values(renewed_at=now, expires_at=expiry)
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def transition(
        self, lease_id: str, *, to_state: str, reason: str | None = None,
        from_states: tuple[str, ...] | None = None, released: bool = False,
    ) -> int:
        """Move a lease to a terminal/intermediate state, optionally gated on from-state."""
        now = utcnow()
        values: dict = {"state": to_state, "reason": reason}
        if released:
            values["released_at"] = now
            values["lease_token"] = None
        stmt = update(SDRDeviceLease).where(SDRDeviceLease.id == lease_id)
        if from_states is not None:
            stmt = stmt.where(SDRDeviceLease.state.in_(from_states))
        stmt = stmt.values(**values)
        return int(self.session.execute(stmt).rowcount or 0)

    def expired_active(self, now: datetime, *, limit: int = 100) -> list[SDRDeviceLease]:
        stmt = (
            select(SDRDeviceLease)
            .where(SDRDeviceLease.state == "active")
            .where(SDRDeviceLease.expires_at.is_not(None))
            .where(SDRDeviceLease.expires_at < now)
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def holding_all(self, *, node_id: str | None = None, limit: int = 1000) -> list[SDRDeviceLease]:
        stmt = select(SDRDeviceLease).where(SDRDeviceLease.state.in_(_HOLDING_LEASE_STATES))
        if node_id is not None:
            stmt = stmt.where(SDRDeviceLease.node_id == node_id)
        return list(self.session.scalars(stmt.limit(limit)))

    def counts_by_state(self, *, node_id: str | None = None) -> dict[str, int]:
        stmt = select(SDRDeviceLease.state, func.count()).group_by(SDRDeviceLease.state)
        if node_id is not None:
            stmt = stmt.where(SDRDeviceLease.node_id == node_id)
        return {state: int(count) for state, count in self.session.execute(stmt)}


class SDRDeviceLeaseEventRepository:
    """Append-only lease lifecycle audit."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> SDRDeviceLeaseEvent:
        record = SDRDeviceLeaseEvent(**fields)
        self.session.add(record)
        self.session.flush()
        return record

    def list_for_lease(self, lease_id: str, *, limit: int = 100) -> list[SDRDeviceLeaseEvent]:
        stmt = (
            select(SDRDeviceLeaseEvent)
            .where(SDRDeviceLeaseEvent.lease_id == lease_id)
            .order_by(SDRDeviceLeaseEvent.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))


# -- Stage 14C.1 real-hardware qualification --------------------------------------------------


class HardwareQualificationRunRepository:
    """Persisted real-hardware qualification runs (additive; evidence never overwritten)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> HardwareQualificationRun:
        run = HardwareQualificationRun(**fields)
        self.session.add(run)
        self.session.flush()
        return run

    def get(self, run_id: str) -> HardwareQualificationRun | None:
        return self.session.get(HardwareQualificationRun, run_id)

    def update(self, run_id: str, **fields) -> HardwareQualificationRun | None:
        run = self.get(run_id)
        if run is None:
            return None
        for key, value in fields.items():
            setattr(run, key, value)
        self.session.flush()
        return run

    def list(
        self, *, node_id: str | None = None, device_id: str | None = None, limit: int = 200
    ) -> list[HardwareQualificationRun]:
        stmt = select(HardwareQualificationRun).order_by(
            HardwareQualificationRun.started_at.desc())
        if node_id is not None:
            stmt = stmt.where(HardwareQualificationRun.node_id == node_id)
        if device_id is not None:
            stmt = stmt.where(HardwareQualificationRun.device_id == device_id)
        return list(self.session.scalars(stmt.limit(limit)))

    def latest_for_device(self, device_id: str) -> HardwareQualificationRun | None:
        stmt = (
            select(HardwareQualificationRun)
            .where(HardwareQualificationRun.device_id == device_id)
            .order_by(HardwareQualificationRun.started_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()


class HardwareQualificationCheckRepository:
    """Append-only qualification checks within a run."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> HardwareQualificationCheck:
        check = HardwareQualificationCheck(**fields)
        self.session.add(check)
        self.session.flush()
        return check

    def list_for_run(self, run_id: str) -> list[HardwareQualificationCheck]:
        stmt = (
            select(HardwareQualificationCheck)
            .where(HardwareQualificationCheck.run_id == run_id)
            .order_by(HardwareQualificationCheck.created_at.asc())
        )
        return list(self.session.scalars(stmt))


class HardwareRFMeasurementRepository:
    """Provenance records for physical RF measurements (bytes live in the artifact store)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> HardwareRFMeasurement:
        m = HardwareRFMeasurement(**fields)
        self.session.add(m)
        self.session.flush()
        return m

    def get(self, measurement_id: str) -> HardwareRFMeasurement | None:
        return self.session.get(HardwareRFMeasurement, measurement_id)

    def list(
        self, *, node_id: str | None = None, device_id: str | None = None,
        qualification_run_id: str | None = None, limit: int = 200,
    ) -> list[HardwareRFMeasurement]:
        stmt = select(HardwareRFMeasurement).order_by(HardwareRFMeasurement.created_at.desc())
        if node_id is not None:
            stmt = stmt.where(HardwareRFMeasurement.node_id == node_id)
        if device_id is not None:
            stmt = stmt.where(HardwareRFMeasurement.device_id == device_id)
        if qualification_run_id is not None:
            stmt = stmt.where(HardwareRFMeasurement.qualification_run_id == qualification_run_id)
        return list(self.session.scalars(stmt.limit(limit)))


# -- Stage 14C.2 field validation / soak / fault injection ------------------------------------


class _SimpleRepo:
    """Tiny create/get/list helper shared by the field-validation repositories."""

    MODEL = None

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields):
        row = self.MODEL(**fields)
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, row_id: str):
        return self.session.get(self.MODEL, row_id)

    def update(self, row_id: str, **fields):
        row = self.get(row_id)
        if row is None:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        self.session.flush()
        return row


class FieldCampaignRepository(_SimpleRepo):
    MODEL = FieldCampaign

    def list(self, *, node_id: str | None = None, limit: int = 200):
        stmt = select(FieldCampaign).order_by(FieldCampaign.created_at.desc())
        if node_id is not None:
            stmt = stmt.where(FieldCampaign.node_id == node_id)
        return list(self.session.scalars(stmt.limit(limit)))


class FieldNodeRepository(_SimpleRepo):
    MODEL = FieldNode

    def list_for_campaign(self, campaign_id: str):
        return list(self.session.scalars(
            select(FieldNode).where(FieldNode.campaign_id == campaign_id)))


class FieldDeviceAssignmentRepository(_SimpleRepo):
    MODEL = FieldDeviceAssignment

    def list_for_campaign(self, campaign_id: str):
        return list(self.session.scalars(
            select(FieldDeviceAssignment).where(
                FieldDeviceAssignment.campaign_id == campaign_id)))


class FieldRunRepository(_SimpleRepo):
    MODEL = FieldRun

    def list_for_campaign(self, campaign_id: str):
        return list(self.session.scalars(
            select(FieldRun).where(FieldRun.campaign_id == campaign_id)
            .order_by(FieldRun.started_at.asc())))


class FieldCheckRepository(_SimpleRepo):
    MODEL = FieldCheck

    def list_for_campaign(self, campaign_id: str):
        return list(self.session.scalars(
            select(FieldCheck).where(FieldCheck.campaign_id == campaign_id)
            .order_by(FieldCheck.created_at.asc())))


class FieldMeasurementRepository(_SimpleRepo):
    MODEL = FieldMeasurement

    def list_for_campaign(self, campaign_id: str, *, limit: int = 5000):
        return list(self.session.scalars(
            select(FieldMeasurement).where(FieldMeasurement.campaign_id == campaign_id)
            .order_by(FieldMeasurement.created_at.asc()).limit(limit)))

    def counts(self, campaign_id: str) -> tuple[int, int]:
        total = int(self.session.scalar(
            select(func.count()).select_from(FieldMeasurement)
            .where(FieldMeasurement.campaign_id == campaign_id)) or 0)
        ok = int(self.session.scalar(
            select(func.count()).select_from(FieldMeasurement)
            .where(FieldMeasurement.campaign_id == campaign_id)
            .where(FieldMeasurement.success.is_(True))) or 0)
        return total, ok


class FieldFaultRepository(_SimpleRepo):
    MODEL = FieldFaultInjection

    def list_for_campaign(self, campaign_id: str):
        return list(self.session.scalars(
            select(FieldFaultInjection).where(FieldFaultInjection.campaign_id == campaign_id)
            .order_by(FieldFaultInjection.created_at.asc())))


class FieldRecoveryRepository(_SimpleRepo):
    MODEL = FieldRecoveryResult

    def list_for_campaign(self, campaign_id: str):
        return list(self.session.scalars(
            select(FieldRecoveryResult).where(FieldRecoveryResult.campaign_id == campaign_id)))


class SoakRunRepository(_SimpleRepo):
    MODEL = SoakRun

    def list(self, *, campaign_id: str | None = None, limit: int = 100):
        stmt = select(SoakRun).order_by(SoakRun.started_at.desc())
        if campaign_id is not None:
            stmt = stmt.where(SoakRun.campaign_id == campaign_id)
        return list(self.session.scalars(stmt.limit(limit)))


class SoakSampleRepository(_SimpleRepo):
    MODEL = SoakSample

    def list_for_run(self, soak_run_id: str, *, limit: int = 100000):
        return list(self.session.scalars(
            select(SoakSample).where(SoakSample.soak_run_id == soak_run_id)
            .order_by(SoakSample.created_at.asc()).limit(limit)))


# ---------------------------------------------------------------------------
# Stage 14D — external-agent interoperability repositories.
# ---------------------------------------------------------------------------

#: Message states a delivery worker may claim (mirrors the peer outbox pattern).
_CLAIMABLE_INTEROP_STATES = ("pending", "retry_wait")


class InteropAgentRepository(_SimpleRepo):
    MODEL = InteropAgent

    def list(self, *, status: str | None = None, limit: int = 200, offset: int = 0):
        stmt = select(InteropAgent).order_by(InteropAgent.created_at.desc())
        if status is not None:
            stmt = stmt.where(InteropAgent.status == status)
        return list(self.session.scalars(stmt.limit(limit).offset(offset)))

    def get_by_fingerprint(self, fingerprint: str) -> InteropAgent | None:
        stmt = select(InteropAgent).where(InteropAgent.fingerprint == fingerprint)
        return self.session.scalars(stmt).first()


class InteropCredentialRepository(_SimpleRepo):
    MODEL = InteropCredential

    def list_for_agent(self, agent_id: str, *, status: str | None = None):
        stmt = (
            select(InteropCredential)
            .where(InteropCredential.agent_id == agent_id)
            .order_by(InteropCredential.created_at.desc())
        )
        if status is not None:
            stmt = stmt.where(InteropCredential.status == status)
        return list(self.session.scalars(stmt))

    def get_by_key_id(self, agent_id: str, key_id: str) -> InteropCredential | None:
        stmt = select(InteropCredential).where(
            InteropCredential.agent_id == agent_id, InteropCredential.key_id == key_id
        )
        return self.session.scalars(stmt).first()

    def get_active_ed25519(self, agent_id: str) -> list[InteropCredential]:
        stmt = select(InteropCredential).where(
            InteropCredential.agent_id == agent_id,
            InteropCredential.kind == "ed25519",
            InteropCredential.status == "active",
        )
        return list(self.session.scalars(stmt))

    def get_active_bearer_hashes(self, agent_id: str) -> list[InteropCredential]:
        stmt = select(InteropCredential).where(
            InteropCredential.agent_id == agent_id,
            InteropCredential.kind == "bearer",
            InteropCredential.status == "active",
        )
        return list(self.session.scalars(stmt))


class InteropEndpointRepository(_SimpleRepo):
    MODEL = InteropEndpoint

    def list_for_agent(self, agent_id: str):
        stmt = (
            select(InteropEndpoint)
            .where(InteropEndpoint.agent_id == agent_id)
            .order_by(InteropEndpoint.created_at.desc())
        )
        return list(self.session.scalars(stmt))


class InteropSubscriptionRepository(_SimpleRepo):
    MODEL = InteropSubscription

    def list_for_agent(self, agent_id: str):
        stmt = (
            select(InteropSubscription)
            .where(InteropSubscription.agent_id == agent_id)
            .order_by(InteropSubscription.created_at.desc())
        )
        return list(self.session.scalars(stmt))

    def list_active(self):
        stmt = select(InteropSubscription).where(InteropSubscription.status == "active")
        return list(self.session.scalars(stmt))

    def allocate_sequence(self, subscription_id: str) -> int | None:
        """Atomically allocate the next monotonic sequence for a subscription.

        Returns the allocated sequence, or ``None`` if the subscription is gone. The
        per-(subscription, sequence) unique constraint plus SQLite's serialized UPDATE
        guarantees no two messages ever share a sequence.
        """
        row = self.get(subscription_id)
        if row is None:
            return None
        seq = row.next_sequence
        row.next_sequence = seq + 1
        self.session.flush()
        return seq


class InteropSessionRepository(_SimpleRepo):
    MODEL = InteropSession

    def list_open_for_agent(self, agent_id: str):
        stmt = select(InteropSession).where(
            InteropSession.agent_id == agent_id, InteropSession.status == "open"
        )
        return list(self.session.scalars(stmt))

    def count_open(self) -> int:
        stmt = select(func.count()).select_from(InteropSession).where(
            InteropSession.status == "open"
        )
        return int(self.session.scalar(stmt) or 0)

    def count_open_for_agent(self, agent_id: str) -> int:
        stmt = select(func.count()).select_from(InteropSession).where(
            InteropSession.agent_id == agent_id, InteropSession.status == "open"
        )
        return int(self.session.scalar(stmt) or 0)


class InteropMessageRepository(_SimpleRepo):
    MODEL = InteropMessage

    def get_by_message_id(self, message_id: str) -> InteropMessage | None:
        stmt = select(InteropMessage).where(InteropMessage.message_id == message_id)
        return self.session.scalars(stmt).first()

    def list_for_agent(self, agent_id: str, *, status: str | None = None, limit: int = 200):
        stmt = (
            select(InteropMessage)
            .where(InteropMessage.agent_id == agent_id)
            .order_by(InteropMessage.created_at.desc())
        )
        if status is not None:
            stmt = stmt.where(InteropMessage.status == status)
        return list(self.session.scalars(stmt.limit(limit)))

    def counts_by_status(self) -> dict[str, int]:
        stmt = select(InteropMessage.status, func.count()).group_by(InteropMessage.status)
        return {status: int(count) for status, count in self.session.execute(stmt)}

    def replay(
        self, subscription_id: str, *, after_sequence: int, limit: int = 500
    ) -> list[InteropMessage]:
        """Ordered messages for a subscription with sequence > ``after_sequence``."""
        stmt = (
            select(InteropMessage)
            .where(
                InteropMessage.subscription_id == subscription_id,
                InteropMessage.sequence > after_sequence,
            )
            .order_by(InteropMessage.sequence.asc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def min_sequence(self, subscription_id: str) -> int | None:
        stmt = select(func.min(InteropMessage.sequence)).where(
            InteropMessage.subscription_id == subscription_id
        )
        return self.session.scalar(stmt)

    def due_ids(self, now: datetime, *, mode: str | None = None, limit: int = 50) -> list[str]:
        stmt = (
            select(InteropMessage.id)
            .where(InteropMessage.status.in_(_CLAIMABLE_INTEROP_STATES))
            .where(
                or_(
                    InteropMessage.next_attempt_at.is_(None),
                    InteropMessage.next_attempt_at <= now,
                )
            )
            .where(
                or_(
                    InteropMessage.claim_owner.is_(None),
                    InteropMessage.claim_expires_at < now,
                )
            )
        )
        if mode is not None:
            stmt = stmt.where(InteropMessage.delivery_mode == mode)
        stmt = stmt.order_by(InteropMessage.next_attempt_at.asc()).limit(limit)
        return list(self.session.scalars(stmt))

    def claim(self, record_id: str, *, owner: str, now: datetime, claim_expiry: datetime) -> int:
        stmt = (
            update(InteropMessage)
            .where(InteropMessage.id == record_id)
            .where(InteropMessage.status.in_(_CLAIMABLE_INTEROP_STATES))
            .where(
                or_(
                    InteropMessage.next_attempt_at.is_(None),
                    InteropMessage.next_attempt_at <= now,
                )
            )
            .where(
                or_(
                    InteropMessage.claim_owner.is_(None),
                    InteropMessage.claim_expires_at < now,
                )
            )
            .values(
                status="delivering",
                claim_owner=owner,
                claim_expires_at=claim_expiry,
                attempt_count=InteropMessage.attempt_count + 1,
                first_attempted_at=func.coalesce(InteropMessage.first_attempted_at, now),
                last_attempted_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def release_and_update(self, record_id: str, *, owner: str, fields: dict) -> None:
        record = self.get(record_id)
        if record is None or record.claim_owner != owner:
            return
        for key, value in fields.items():
            setattr(record, key, value)
        record.claim_owner = None
        record.claim_expires_at = None
        record.updated_at = utcnow()
        self.session.flush()

    def recover_expired_claims(self, now: datetime) -> int:
        stmt = (
            update(InteropMessage)
            .where(InteropMessage.status == "delivering")
            .where(
                or_(
                    InteropMessage.claim_expires_at.is_(None),
                    InteropMessage.claim_expires_at < now,
                )
            )
            .values(
                status="retry_wait",
                claim_owner=None,
                claim_expires_at=None,
                next_attempt_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def expire_old(self, now: datetime, *, limit: int = 200) -> int:
        """Move past-expiry, not-yet-terminal messages to ``expired``."""
        stmt = (
            update(InteropMessage)
            .where(InteropMessage.status.in_(_CLAIMABLE_INTEROP_STATES))
            .where(InteropMessage.expires_at.is_not(None))
            .where(InteropMessage.expires_at < now)
            .values(status="expired", claim_owner=None, claim_expires_at=None, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(self.session.execute(stmt).rowcount or 0)


class InteropDeliveryRepository(_SimpleRepo):
    MODEL = InteropDelivery

    def list_for_message(self, message_pk: str):
        stmt = (
            select(InteropDelivery)
            .where(InteropDelivery.message_pk == message_pk)
            .order_by(InteropDelivery.attempt_no.asc())
        )
        return list(self.session.scalars(stmt))


class InteropReceiptRepository(_SimpleRepo):
    MODEL = InteropReceipt

    def get_for(self, agent_id: str, message_id: str) -> InteropReceipt | None:
        stmt = select(InteropReceipt).where(
            InteropReceipt.agent_id == agent_id, InteropReceipt.message_id == message_id
        )
        return self.session.scalars(stmt).first()


class InteropNonceRepository(_SimpleRepo):
    MODEL = InteropNonce

    def exists(self, agent_id: str, nonce: str) -> bool:
        stmt = select(InteropNonce.id).where(
            InteropNonce.agent_id == agent_id, InteropNonce.nonce == nonce
        )
        return self.session.scalars(stmt).first() is not None

    def purge_expired(self, now: datetime) -> int:
        from sqlalchemy import delete

        stmt = delete(InteropNonce).where(InteropNonce.expires_at < now)
        return int(self.session.execute(stmt).rowcount or 0)


class InteropSubmissionRepository(_SimpleRepo):
    MODEL = InteropSubmission

    def get_for(self, agent_id: str, idempotency_key: str) -> InteropSubmission | None:
        stmt = select(InteropSubmission).where(
            InteropSubmission.agent_id == agent_id,
            InteropSubmission.idempotency_key == idempotency_key,
        )
        return self.session.scalars(stmt).first()


class InteropAuditRepository(_SimpleRepo):
    MODEL = InteropAudit

    def list(self, *, agent_id: str | None = None, limit: int = 200):
        stmt = select(InteropAudit).order_by(InteropAudit.created_at.desc())
        if agent_id is not None:
            stmt = stmt.where(InteropAudit.agent_id == agent_id)
        return list(self.session.scalars(stmt.limit(limit)))


# ---------------------------------------------------------------------------
# Stage 14E — data-platform repositories (node side).
# ---------------------------------------------------------------------------

_CLAIMABLE_BATCH_STATES = ("pending", "retry_wait")


class DataConsentProfileRepository(_SimpleRepo):
    MODEL = DataConsentProfile

    def list(self, *, tenant_id: str | None = None, limit: int = 200):
        stmt = select(DataConsentProfile).order_by(DataConsentProfile.created_at.desc())
        if tenant_id is not None:
            stmt = stmt.where(DataConsentProfile.tenant_id == tenant_id)
        return list(self.session.scalars(stmt.limit(limit)))

    def active(self, tenant_id: str, node_id: str) -> DataConsentProfile | None:
        stmt = (
            select(DataConsentProfile)
            .where(DataConsentProfile.tenant_id == tenant_id)
            .where(DataConsentProfile.node_id == node_id)
            .where(DataConsentProfile.state == "active")
            .order_by(DataConsentProfile.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()


class DataConsentGrantRepository(_SimpleRepo):
    MODEL = DataConsentGrant

    def list(self, *, tenant_id: str | None = None, node_id: str | None = None):
        stmt = select(DataConsentGrant).order_by(DataConsentGrant.created_at.desc())
        if tenant_id is not None:
            stmt = stmt.where(DataConsentGrant.tenant_id == tenant_id)
        if node_id is not None:
            stmt = stmt.where(DataConsentGrant.node_id == node_id)
        return list(self.session.scalars(stmt))

    def current_for_category(
        self, tenant_id: str, node_id: str, category: str
    ) -> DataConsentGrant | None:
        """The most-recent active grant for a category (effective state)."""
        stmt = (
            select(DataConsentGrant)
            .where(DataConsentGrant.tenant_id == tenant_id)
            .where(DataConsentGrant.node_id == node_id)
            .where(DataConsentGrant.category == category)
            .order_by(DataConsentGrant.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()


class DataConsentRevisionRepository(_SimpleRepo):
    MODEL = DataConsentRevision

    def list_for_grant(self, grant_id: str):
        stmt = (
            select(DataConsentRevision)
            .where(DataConsentRevision.grant_id == grant_id)
            .order_by(DataConsentRevision.revision_no.asc())
        )
        return list(self.session.scalars(stmt))

    def next_revision_no(self, grant_id: str) -> int:
        stmt = select(func.max(DataConsentRevision.revision_no)).where(
            DataConsentRevision.grant_id == grant_id
        )
        return int(self.session.scalar(stmt) or 0) + 1


class DataConsentAuditRepository(_SimpleRepo):
    MODEL = DataConsentAudit

    def list(self, *, tenant_id: str | None = None, limit: int = 200):
        stmt = select(DataConsentAudit).order_by(DataConsentAudit.created_at.desc())
        if tenant_id is not None:
            stmt = stmt.where(DataConsentAudit.tenant_id == tenant_id)
        return list(self.session.scalars(stmt.limit(limit)))


class DataCategoryPolicyRepository(_SimpleRepo):
    MODEL = DataCategoryPolicy

    def get_for(self, tenant_id: str, node_id: str, category: str) -> DataCategoryPolicy | None:
        stmt = select(DataCategoryPolicy).where(
            DataCategoryPolicy.tenant_id == tenant_id,
            DataCategoryPolicy.node_id == node_id,
            DataCategoryPolicy.category == category,
        )
        return self.session.scalars(stmt).first()

    def list(self, *, tenant_id: str | None = None):
        stmt = select(DataCategoryPolicy)
        if tenant_id is not None:
            stmt = stmt.where(DataCategoryPolicy.tenant_id == tenant_id)
        return list(self.session.scalars(stmt))


class DataExportRecordRepository(_SimpleRepo):
    MODEL = DataExportRecord

    def list(self, *, category: str | None = None, status: str | None = None,
             tenant_id: str | None = None, limit: int = 200, offset: int = 0):
        stmt = select(DataExportRecord).order_by(DataExportRecord.created_at.desc())
        if category is not None:
            stmt = stmt.where(DataExportRecord.category == category)
        if status is not None:
            stmt = stmt.where(DataExportRecord.status == status)
        if tenant_id is not None:
            stmt = stmt.where(DataExportRecord.tenant_id == tenant_id)
        return list(self.session.scalars(stmt.limit(limit).offset(offset)))

    def batchable(self, tenant_id: str, *, category: str | None = None, limit: int = 500):
        """Approved, export-eligible, not-yet-batched, not-superseded records."""
        stmt = (
            select(DataExportRecord)
            .where(DataExportRecord.tenant_id == tenant_id)
            .where(DataExportRecord.status == "approved")
            .where(DataExportRecord.batch_id.is_(None))
            .where(DataExportRecord.superseded_by.is_(None))
            .order_by(DataExportRecord.created_at.asc())
            .limit(limit)
        )
        if category is not None:
            stmt = stmt.where(DataExportRecord.category == category)
        return list(self.session.scalars(stmt))

    def counts_by_status(self) -> dict[str, int]:
        stmt = select(DataExportRecord.status, func.count()).group_by(DataExportRecord.status)
        return {s: int(c) for s, c in self.session.execute(stmt)}

    def list_for_batch(self, batch_id: str):
        stmt = (
            select(DataExportRecord)
            .where(DataExportRecord.batch_id == batch_id)
            .order_by(DataExportRecord.created_at.asc())
        )
        return list(self.session.scalars(stmt))

    def list_for_subject(self, tenant_id: str, subject_pseudonym: str, *, limit: int = 5000):
        stmt = (
            select(DataExportRecord)
            .where(DataExportRecord.tenant_id == tenant_id)
            .where(DataExportRecord.subject_pseudonym == subject_pseudonym)
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def expire_old(self, now: datetime, *, limit: int = 500) -> int:
        stmt = (
            update(DataExportRecord)
            .where(DataExportRecord.status.in_(("collected", "held", "approved")))
            .where(DataExportRecord.expires_at.is_not(None))
            .where(DataExportRecord.expires_at < now)
            .values(status="expired", updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(self.session.execute(stmt).rowcount or 0)


class DataExportBatchRepository(_SimpleRepo):
    MODEL = DataExportBatch

    def get_by_idempotency(self, destination_id: str, key: str) -> DataExportBatch | None:
        stmt = select(DataExportBatch).where(
            DataExportBatch.destination_id == destination_id,
            DataExportBatch.idempotency_key == key,
        )
        return self.session.scalars(stmt).first()

    def list(self, *, status: str | None = None, destination_id: str | None = None,
             limit: int = 200):
        stmt = select(DataExportBatch).order_by(DataExportBatch.created_at.desc())
        if status is not None:
            stmt = stmt.where(DataExportBatch.status == status)
        if destination_id is not None:
            stmt = stmt.where(DataExportBatch.destination_id == destination_id)
        return list(self.session.scalars(stmt.limit(limit)))

    def counts_by_status(self) -> dict[str, int]:
        stmt = select(DataExportBatch.status, func.count()).group_by(DataExportBatch.status)
        return {s: int(c) for s, c in self.session.execute(stmt)}

    def due_ids(self, now: datetime, *, limit: int = 50) -> list[str]:
        stmt = (
            select(DataExportBatch.id)
            .where(DataExportBatch.status.in_(_CLAIMABLE_BATCH_STATES))
            .where(or_(DataExportBatch.next_attempt_at.is_(None),
                       DataExportBatch.next_attempt_at <= now))
            .where(or_(DataExportBatch.claim_owner.is_(None),
                       DataExportBatch.claim_expires_at < now))
            .order_by(DataExportBatch.next_attempt_at.asc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def claim(self, batch_id: str, *, owner: str, now: datetime, claim_expiry: datetime) -> int:
        stmt = (
            update(DataExportBatch)
            .where(DataExportBatch.id == batch_id)
            .where(DataExportBatch.status.in_(_CLAIMABLE_BATCH_STATES))
            .where(or_(DataExportBatch.next_attempt_at.is_(None),
                       DataExportBatch.next_attempt_at <= now))
            .where(or_(DataExportBatch.claim_owner.is_(None),
                       DataExportBatch.claim_expires_at < now))
            .values(status="delivering", claim_owner=owner, claim_expires_at=claim_expiry,
                    attempt_count=DataExportBatch.attempt_count + 1, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(self.session.execute(stmt).rowcount or 0)

    def release_and_update(self, batch_id: str, *, owner: str, fields: dict) -> None:
        row = self.get(batch_id)
        if row is None or row.claim_owner != owner:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        row.claim_owner = None
        row.claim_expires_at = None
        row.updated_at = utcnow()
        self.session.flush()

    def recover_expired_claims(self, now: datetime) -> int:
        stmt = (
            update(DataExportBatch)
            .where(DataExportBatch.status == "delivering")
            .where(or_(DataExportBatch.claim_expires_at.is_(None),
                       DataExportBatch.claim_expires_at < now))
            .values(status="retry_wait", claim_owner=None, claim_expires_at=None,
                    next_attempt_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(self.session.execute(stmt).rowcount or 0)


class DataExportDestinationRepository(_SimpleRepo):
    MODEL = DataExportDestination

    def list(self, *, tenant_id: str | None = None):
        stmt = select(DataExportDestination).order_by(DataExportDestination.created_at.desc())
        if tenant_id is not None:
            stmt = stmt.where(DataExportDestination.tenant_id == tenant_id)
        return list(self.session.scalars(stmt))


class DataDeliveryAttemptRepository(_SimpleRepo):
    MODEL = DataDeliveryAttempt

    def list_for_batch(self, batch_id: str):
        stmt = (
            select(DataDeliveryAttempt)
            .where(DataDeliveryAttempt.batch_id == batch_id)
            .order_by(DataDeliveryAttempt.attempt_no.asc())
        )
        return list(self.session.scalars(stmt))


class DataDeletionRequestRepository(_SimpleRepo):
    MODEL = DataDeletionRequest

    def list(self, *, tenant_id: str | None = None, status: str | None = None, limit: int = 200):
        stmt = select(DataDeletionRequest).order_by(DataDeletionRequest.created_at.desc())
        if tenant_id is not None:
            stmt = stmt.where(DataDeletionRequest.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(DataDeletionRequest.status == status)
        return list(self.session.scalars(stmt.limit(limit)))


class DataDatasetRepository(_SimpleRepo):
    MODEL = DataDataset

    def list(self, *, tenant_id: str | None = None):
        stmt = select(DataDataset).order_by(DataDataset.created_at.desc())
        if tenant_id is not None:
            stmt = stmt.where(DataDataset.tenant_id == tenant_id)
        return list(self.session.scalars(stmt))


class DataDatasetVersionRepository(_SimpleRepo):
    MODEL = DataDatasetVersion

    def list_for_dataset(self, dataset_id: str):
        stmt = (
            select(DataDatasetVersion)
            .where(DataDatasetVersion.dataset_id == dataset_id)
            .order_by(DataDatasetVersion.version.asc())
        )
        return list(self.session.scalars(stmt))

    def next_version(self, dataset_id: str) -> int:
        stmt = select(func.max(DataDatasetVersion.version)).where(
            DataDatasetVersion.dataset_id == dataset_id
        )
        return int(self.session.scalar(stmt) or 0) + 1


class DataDatasetMemberRepository(_SimpleRepo):
    MODEL = DataDatasetMember

    def list_for_version(self, version_id: str, *, limit: int = 100000):
        stmt = (
            select(DataDatasetMember)
            .where(DataDatasetMember.version_id == version_id)
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def versions_containing_record(self, record_id: str):
        stmt = select(DataDatasetMember).where(DataDatasetMember.record_id == record_id)
        return list(self.session.scalars(stmt))

    def mark_excluded(self, record_id: str, now: datetime) -> int:
        stmt = (
            update(DataDatasetMember)
            .where(DataDatasetMember.record_id == record_id)
            .values(excluded=True)
            .execution_options(synchronize_session=False)
        )
        return int(self.session.execute(stmt).rowcount or 0)
