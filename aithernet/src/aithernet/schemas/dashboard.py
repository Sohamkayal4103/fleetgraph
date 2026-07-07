"""Typed read schemas for the Stage 13C operator-dashboard aggregate endpoints.

These validate the bounded, sanitized dicts the :class:`~aithernet.dashboard.service`
DashboardService produces. Heterogeneous timeline ``entries`` are typed as bounded JSON
objects (``dict``) on purpose — every entry is already sanitized and size-bounded by the
service, and a discriminated union per entry kind would add cost without changing the
contract: no field ever carries a signature, key, raw envelope, prompt, or environment.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class OverviewNode(BaseModel):
    node_id: str | None = None
    node_name: str | None = None
    runtime_status: str | None = None
    version: str | None = None
    started_at: Any | None = None
    mission_count: int | None = None
    event_count: int | None = None
    identity_initialized: bool = False
    fingerprint: str | None = None  # abbreviated sha256:<16> — never a full key/fingerprint


class WorkerHealth(BaseModel):
    enabled: bool | None = None
    running: bool | None = None
    degraded: bool | None = None
    active_count: int | None = None
    queued_count: int | None = None
    in_flight: int | None = None


class OverviewWorkers(BaseModel):
    mission: WorkerHealth
    transport: WorkerHealth


class ReadinessBlock(BaseModel):
    provider: str | None = None
    configured: bool | None = None
    missing_configuration: list[str] = []


class OverviewResponse(BaseModel):
    node: OverviewNode
    workers: OverviewWorkers
    coordinator: ReadinessBlock
    coding_agent: ReadinessBlock
    rf_backends: list[dict[str, Any]] = []
    peers: dict[str, int] = {}
    outbox: dict[str, int] = {}
    inbox_count: int = 0
    missions: dict[str, Any] = {}
    reply_waits: dict[str, Any] = {}
    inbound_requests: dict[str, Any] = {}
    recent_failures: dict[str, list[dict[str, Any]]] = {}


class FleetPeerEntry(BaseModel):
    peer_id: str
    name: str
    role: str
    trust_state: str
    enabled: bool
    transport_health: str
    last_success_at: str | None = None
    last_failure_at: str | None = None
    consecutive_failures: int = 0
    fingerprint: str | None = None  # abbreviated only
    manifest_at: str | None = None
    capabilities: Any | None = None
    permissions: dict[str, Any] = {}
    conversation_count: int = 0
    outstanding_messages: int = 0
    pending_reply_waits: int = 0


class FleetResponse(BaseModel):
    peers: list[FleetPeerEntry] = []


class ConversationTimelineResponse(BaseModel):
    conversation_id: str
    peers: list[str] = []
    message_count: int = 0
    entries: list[dict[str, Any]] = []
    legend: dict[str, str] = {}


class TopologyNode(BaseModel):
    id: str
    kind: str
    label: str
    status: str | None = None


class TopologyEdge(BaseModel):
    from_: str
    to: str
    kind: str

    model_config = {"extra": "allow", "populate_by_name": True}


class DistributedMissionTimelineResponse(BaseModel):
    mission_id: str
    mission_status: str
    mission_run_id: str | None = None
    iteration_count: int | None = None
    source_type: str
    is_inbound_mission: bool = False
    outbound_messages: list[dict[str, Any]] = []
    reply_waits: list[dict[str, Any]] = []
    response_obligation: dict[str, Any] | None = None
    inbound_request: dict[str, Any] | None = None
    remote_missions: list[dict[str, Any]] = []
    artifact_transfers: list[dict[str, Any]] = []
    imported_artifacts: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    topology: dict[str, Any] = {}
    legend: dict[str, str] = {}


class CommunicationsSummaryResponse(BaseModel):
    outbox_counts: dict[str, int] = {}
    inbox_count: int = 0
    reply_waits: dict[str, int] = {}
    inbound_requests: dict[str, Any] = {}
    peers: dict[str, int] = {}
    recent_failures: list[dict[str, Any]] = []
