"""The communication runtime: the node's external-agent connection layer (Stage 7).

``CommunicationRuntime`` owns the persistence and event logic for external agents and
their messages. It operates *through* the owning :class:`~aithernet.orchestrator.runtime.
NodeRuntime` — reusing its session scope, event bus, mission creation, and one-step
execution — so external agents plug into the existing mission system without a parallel
code path. It is deliberately narrow:

* It persists connections and messages, and may create a mission from a message.
* On explicit request (``run_step``) it runs **exactly one** mission step via the Stage 5
  flow — never a loop.
* It never calls ``endpoint_url``, opens an outgoing session, or polls anything. An
  outbound message row is a recorded local summary, not proof of a network delivery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aithernet.communication.contracts import (
    ExternalAgentCreate,
    ExternalAgentDisabledError,
    ExternalAgentMessageCreate,
    ExternalAgentMessageRead,
    ExternalAgentMessageResponse,
    ExternalAgentNotFoundError,
    ExternalAgentRead,
    ExternalAgentStatus,
    ExternalAgentStatusValue,
    ExternalAgentValidationError,
    MessageDirection,
    find_secret_metadata_keys,
)
from aithernet.coordinator.contracts import CoordinatorConfigurationError
from aithernet.schemas.missions import MissionCreate, MissionRead
from aithernet.state.models import utcnow
from aithernet.state.repositories import (
    ExternalAgentMessageRepository,
    ExternalAgentRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

#: Source type stamped on missions created from an external-agent message. Kept exact so
#: the existing mission system treats these uniformly.
EXTERNAL_AGENT_SOURCE_TYPE = "external_agent"

#: External-agent lifecycle event types.
EVENT_EXTERNAL_AGENT_CONNECTED = "external_agent.connected"
EVENT_EXTERNAL_AGENT_DISCONNECTED = "external_agent.disconnected"
EVENT_EXTERNAL_AGENT_DISABLED = "external_agent.disabled"
EVENT_EXTERNAL_AGENT_MESSAGE_RECEIVED = "external_agent.message_received"
EVENT_EXTERNAL_AGENT_MESSAGE_REPLIED = "external_agent.message_replied"
EVENT_EXTERNAL_AGENT_MISSION_CREATED = "external_agent.mission_created"
EVENT_EXTERNAL_AGENT_STEP_RAN = "external_agent.step_ran"

#: Status value -> the event emitted when an agent transitions into it.
_STATUS_EVENT = {
    ExternalAgentStatusValue.CONNECTED.value: EVENT_EXTERNAL_AGENT_CONNECTED,
    ExternalAgentStatusValue.DISCONNECTED.value: EVENT_EXTERNAL_AGENT_DISCONNECTED,
    ExternalAgentStatusValue.DISABLED.value: EVENT_EXTERNAL_AGENT_DISABLED,
}

#: How many metadata keys to surface in a mission's payload summary (names only).
_PAYLOAD_KEY_PREVIEW = 12


class CommunicationRuntime:
    """Owns external-agent connections and messaging for one node."""

    def __init__(self, node: NodeRuntime) -> None:
        self._node = node

    # -- agent lifecycle ---------------------------------------------------------

    async def create_external_agent(self, payload: ExternalAgentCreate) -> ExternalAgentRead:
        """Persist a connecting external agent (status ``connected``) and emit an event.

        Rejects a raw secret smuggled into ``metadata`` by *key name* only — the secret
        value is never stored nor echoed back — since Stage 7 stores no credentials.
        """
        secret_keys = find_secret_metadata_keys(payload.metadata)
        if secret_keys:
            raise ExternalAgentValidationError(
                "Raw secrets must not be sent in metadata "
                f"(offending key(s): {', '.join(secret_keys)}). Use auth_type to record the "
                "scheme; secure secret storage is a later stage."
            )
        now = utcnow()
        with self._node.session_scope() as session:
            agent = ExternalAgentRepository(session).create(
                name=payload.name,
                agent_type=payload.agent_type,
                endpoint_url=payload.endpoint_url,
                transport=payload.transport,
                auth_type=payload.auth_type,
                status=ExternalAgentStatusValue.CONNECTED.value,
                metadata=payload.metadata,
                last_seen_at=now,
            )
            session.commit()
            agent_read = ExternalAgentRead.model_validate(agent)

        await self._node._emit_event(
            event_type=EVENT_EXTERNAL_AGENT_CONNECTED,
            mission_id=None,
            source="communication",
            message=f"External agent {agent_read.id} ('{agent_read.name}') connected.",
            payload={
                "agent_id": agent_read.id,
                "agent_type": agent_read.agent_type,
                "transport": agent_read.transport,
            },
        )
        return agent_read

    def list_external_agents(
        self,
        *,
        status: ExternalAgentStatusValue | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExternalAgentRead]:
        """Return external agents, newest first, optionally filtered by status."""
        status_value = status.value if status is not None else None
        with self._node.session_scope() as session:
            rows = ExternalAgentRepository(session).list(
                status=status_value, limit=limit, offset=offset
            )
            return [ExternalAgentRead.model_validate(row) for row in rows]

    def get_external_agent(self, agent_id: str) -> ExternalAgentRead | None:
        """Return a single external agent, or ``None`` if it does not exist."""
        with self._node.session_scope() as session:
            row = ExternalAgentRepository(session).get(agent_id)
            return ExternalAgentRead.model_validate(row) if row is not None else None

    async def update_external_agent_status(
        self, agent_id: str, status: ExternalAgentStatusValue
    ) -> ExternalAgentRead:
        """Update an agent's connection status and emit the matching lifecycle event.

        Raises :class:`ExternalAgentNotFoundError` for an unknown agent. Connecting an
        agent refreshes its ``last_seen_at``.
        """
        last_seen = utcnow() if status is ExternalAgentStatusValue.CONNECTED else None
        with self._node.session_scope() as session:
            repo = ExternalAgentRepository(session)
            if repo.get(agent_id) is None:
                raise ExternalAgentNotFoundError(f"External agent {agent_id} not found.")
            agent = repo.update(agent_id, status=status.value, last_seen_at=last_seen)
            session.commit()
            agent_read = ExternalAgentRead.model_validate(agent)

        await self._node._emit_event(
            event_type=_STATUS_EVENT[status.value],
            mission_id=None,
            source="communication",
            message=f"External agent {agent_id} status set to {status.value}.",
            payload={"agent_id": agent_id, "status": status.value},
        )
        return agent_read

    async def disable_external_agent(self, agent_id: str) -> ExternalAgentRead:
        """Disable an agent (preferred over hard deletion); emits ``external_agent.disabled``."""
        return await self.update_external_agent_status(
            agent_id, ExternalAgentStatusValue.DISABLED
        )

    def external_agent_status(self) -> ExternalAgentStatus:
        """Return a roster snapshot: connected count, total count, and the agents."""
        with self._node.session_scope() as session:
            repo = ExternalAgentRepository(session)
            rows = repo.list(limit=1000)
            total = repo.count()
            connected = repo.count(status=ExternalAgentStatusValue.CONNECTED.value)
            agents = [ExternalAgentRead.model_validate(row) for row in rows]
        return ExternalAgentStatus(
            connected_count=connected, total_count=total, agents=agents
        )

    # -- messaging ---------------------------------------------------------------

    async def receive_external_agent_message(
        self, agent_id: str, payload: ExternalAgentMessageCreate
    ) -> ExternalAgentMessageResponse:
        """Record an inbound message and run the requested follow-on actions.

        Persists the inbound message, refreshes the agent's ``last_seen_at``, optionally
        creates a mission (``source_type='external_agent'``, ``source_id=agent.id``) and
        runs exactly one mission step, then records an outbound reply summarising the
        outcome. A disabled agent is rejected; an invalid ``run_step``/``create_mission``
        combination is rejected; an unconfigured coordinator (when a step is requested) is
        surfaced clearly — none of these are silently treated as success.
        """
        agent = self.get_external_agent(agent_id)
        if agent is None:
            raise ExternalAgentNotFoundError(f"External agent {agent_id} not found.")
        if agent.status is ExternalAgentStatusValue.DISABLED:
            raise ExternalAgentDisabledError(
                f"External agent {agent_id} is disabled and cannot send messages. "
                "Re-enable it with a status update first."
            )
        if payload.run_step and not payload.create_mission:
            raise ExternalAgentValidationError(
                "run_step requires create_mission=true: a step needs a mission to run "
                "against. Set create_mission=true or run_step=false."
            )
        if payload.run_step and not self._node.coordinator.is_configured():
            raise CoordinatorConfigurationError(
                "The coordinator is not configured; cannot run a mission step. "
                "Send the message without run_step, or configure the coordinator."
            )

        # 1-3: persist the inbound message and refresh last_seen.
        now = utcnow()
        with self._node.session_scope() as session:
            messages = ExternalAgentMessageRepository(session)
            inbound = messages.create(
                agent_id=agent_id,
                direction=MessageDirection.INBOUND.value,
                message_type=payload.message_type,
                content=payload.content,
                payload=payload.payload,
            )
            ExternalAgentRepository(session).update(agent_id, last_seen_at=now)
            session.commit()
            inbound_id = inbound.id

        await self._node._emit_event(
            event_type=EVENT_EXTERNAL_AGENT_MESSAGE_RECEIVED,
            mission_id=None,
            source="communication",
            message=f"Message {inbound_id} received from external agent {agent_id}.",
            payload={
                "agent_id": agent_id,
                "message_id": inbound_id,
                "message_type": payload.message_type,
            },
        )

        mission: MissionRead | None = None
        step_response = None
        reply_parts: list[str] = []

        # 4: optionally create a mission linked to this message.
        if payload.create_mission:
            mission = await self._node.create_mission(
                MissionCreate(
                    content=payload.content,
                    source_type=EXTERNAL_AGENT_SOURCE_TYPE,
                    source_id=agent.id,
                    metadata=self._mission_metadata(agent, inbound_id, payload),
                )
            )
            with self._node.session_scope() as session:
                ExternalAgentMessageRepository(session).set_mission(inbound_id, mission.id)
                session.commit()
            await self._node._emit_event(
                event_type=EVENT_EXTERNAL_AGENT_MISSION_CREATED,
                mission_id=mission.id,
                source="communication",
                message=f"Mission {mission.id} created from external agent {agent_id}.",
                payload={
                    "agent_id": agent_id,
                    "message_id": inbound_id,
                    "mission_id": mission.id,
                },
            )
            reply_parts.append(f"Mission {mission.id} created")

            # 5: optionally run exactly one mission step.
            if payload.run_step:
                step_response = await self._node.run_mission_step(mission.id)
                step = step_response.step
                await self._node._emit_event(
                    event_type=EVENT_EXTERNAL_AGENT_STEP_RAN,
                    mission_id=mission.id,
                    source="communication",
                    message=(
                        f"Mission step {step.id} ran for external agent {agent_id} "
                        f"(target={step.route_target}, status={step.status.value})."
                    ),
                    payload={
                        "agent_id": agent_id,
                        "mission_id": mission.id,
                        "step_id": step.id,
                        "route_target": step.route_target,
                        "status": step.status.value,
                    },
                )
                reply_parts.append(
                    f"step {step.status.value} (target={step.route_target})"
                )
        else:
            reply_parts.append("Message recorded")

        # 6: record an outbound reply summarising what happened.
        reply_content = "; ".join(reply_parts) + "."
        with self._node.session_scope() as session:
            messages = ExternalAgentMessageRepository(session)
            messages.create(
                agent_id=agent_id,
                direction=MessageDirection.OUTBOUND.value,
                message_type="reply",
                content=reply_content,
                payload={
                    "in_reply_to": inbound_id,
                    "mission_id": mission.id if mission is not None else None,
                    "step_id": step_response.step.id if step_response is not None else None,
                },
                mission_id=mission.id if mission is not None else None,
            )
            inbound_row = messages.get(inbound_id)
            inbound_read = ExternalAgentMessageRead.model_validate(inbound_row)
            session.commit()

        await self._node._emit_event(
            event_type=EVENT_EXTERNAL_AGENT_MESSAGE_REPLIED,
            mission_id=mission.id if mission is not None else None,
            source="communication",
            message=f"Replied to external agent {agent_id}: {reply_content}",
            payload={"agent_id": agent_id, "in_reply_to": inbound_id},
        )

        # 7: return the agent (with refreshed last_seen), inbound message, mission, step.
        agent_after = self.get_external_agent(agent_id) or agent
        return ExternalAgentMessageResponse(
            agent=agent_after,
            message=inbound_read,
            mission=mission,
            step_response=step_response,
        )

    def list_external_agent_messages(
        self,
        *,
        agent_id: str | None = None,
        mission_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExternalAgentMessageRead]:
        """Return external-agent messages, newest first, optionally filtered."""
        with self._node.session_scope() as session:
            rows = ExternalAgentMessageRepository(session).list(
                agent_id=agent_id, mission_id=mission_id, limit=limit, offset=offset
            )
            return [ExternalAgentMessageRead.model_validate(row) for row in rows]

    # -- helpers -----------------------------------------------------------------

    @staticmethod
    def _mission_metadata(
        agent: ExternalAgentRead, message_id: str, payload: ExternalAgentMessageCreate
    ) -> dict:
        """Build the mission metadata recorded for an external-agent message."""
        return {
            "external_agent_id": agent.id,
            "external_agent_name": agent.name,
            "external_agent_type": agent.agent_type,
            "message_id": message_id,
            "message_type": payload.message_type,
            "payload_keys": sorted(str(k) for k in payload.payload)[:_PAYLOAD_KEY_PREVIEW],
        }
