"""Typed, bounded API schemas for managed SDR hardware (Stage 14B, Part N).

These read models surface ONLY sanitized facts. They never expose ``lease_token``,
``owner_worker_id``, raw device paths, credentials, environment, or arbitrary provider metadata.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class HardwareProviderRead(BaseModel):
    provider_id: str
    kind: str
    enabled: bool
    required: bool
    available: bool


class HardwareCapabilityRead(BaseModel):
    device_id: str
    revision: int
    rx_supported: bool | None = None
    tx_supported: bool | None = None
    full_duplex: bool | None = None
    channel_count: int | None = None
    shared_receive_safe: bool = False
    source: str = "unknown"
    confidence: str = "unknown"
    capabilities: dict = Field(default_factory=dict)
    probed_at: datetime | None = None


class HardwareDeviceRead(BaseModel):
    device_id: str
    node_id: str
    hardware_key: str
    provider_id: str
    device_kind: str
    vendor: str | None = None
    product: str | None = None
    serial: str | None = None
    driver: str | None = None
    transport: str | None = None
    channel: str | None = None
    display_name: str | None = None
    identity_limited: bool
    enabled: bool
    administrative_state: str
    presence_state: str
    health_state: str
    status: str
    capability_revision: int
    rx: bool | None = None
    tx: bool | None = None
    channel_count: int | None = None
    metadata: dict = Field(default_factory=dict)
    compatible_backends: list[str] = Field(default_factory=list)
    active_lease_count: int = 0
    first_seen_at: datetime
    last_seen_at: datetime | None = None


class HardwareHealthEventRead(BaseModel):
    id: str
    device_id: str
    health_state: str
    previous_health_state: str | None = None
    presence_state: str | None = None
    detail: str | None = None
    created_at: datetime


class HardwareLeaseRead(BaseModel):
    lease_id: str
    node_id: str
    device_id: str
    backend_id: str | None = None
    mission_id: str | None = None
    mission_run_id: str | None = None
    mission_step_id: str | None = None
    requested_operation: str | None = None
    direction: str
    requested_channels: list = Field(default_factory=list)
    lease_mode: str
    state: str
    reason: str | None = None
    acquired_at: datetime | None = None
    renewed_at: datetime | None = None
    expires_at: datetime | None = None
    released_at: datetime | None = None
    created_at: datetime


class HardwareLeaseEventRead(BaseModel):
    id: str
    lease_id: str
    device_id: str
    event_type: str
    state: str | None = None
    reason: str | None = None
    created_at: datetime


class HardwareStatusRead(BaseModel):
    enabled: bool
    operator_lease_actions_enabled: bool
    providers: list[HardwareProviderRead] = Field(default_factory=list)
    devices_by_status: dict = Field(default_factory=dict)
    leases_by_state: dict = Field(default_factory=dict)


class LeaseAcquireRequest(BaseModel):
    """Operator lease-acquire request — bounded fields only (no device args / paths / env)."""

    device_id: str
    backend_id: str
    direction: str = "rx"
    lease_mode: str = "exclusive"
    operation: str | None = None
    channels: list[int] = Field(default_factory=list)
    frequency_hz: float | None = None
    sample_rate: float | None = None
    duration_seconds: float | None = None


class LeaseActionRequest(BaseModel):
    reason: str | None = None


class HardwareRefreshResult(BaseModel):
    availability: dict[str, bool] = Field(default_factory=dict)
    devices_by_status: dict = Field(default_factory=dict)


# -- Stage 14C.1 qualification --------------------------------------------------------------

class HardwareQualificationCheckRead(BaseModel):
    id: str
    run_id: str
    name: str
    category: str
    status: str
    detail: str | None = None
    evidence: dict = Field(default_factory=dict)
    created_at: datetime


class HardwareQualificationRunRead(BaseModel):
    id: str
    node_id: str
    device_id: str
    hardware_key: str | None = None
    provider_id: str | None = None
    status: str
    support_classification: str
    capability_revision: int | None = None
    environment: dict = Field(default_factory=dict)
    summary: str | None = None
    warnings: list = Field(default_factory=list)
    checks_total: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    checks_not_executed: int = 0
    started_at: datetime
    completed_at: datetime | None = None


class HardwareMeasurementRead(BaseModel):
    id: str
    device_id: str
    qualification_run_id: str | None = None
    capability_revision: int | None = None
    lease_id: str | None = None
    backend_id: str | None = None
    mission_id: str | None = None
    kind: str
    center_frequency_hz: float | None = None
    sample_rate_sps: float | None = None
    gain_db: float | None = None
    antenna: str | None = None
    channel: str | None = None
    stream_format: str | None = None
    capture_bytes: int | None = None
    sample_count: int | None = None
    sha256: str | None = None
    artifact_id: str | None = None
    processing_revision: str | None = None
    analysis_params: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    confidence: str = "unknown"
    warnings: list = Field(default_factory=list)
    created_at: datetime


class HardwareSupportEntry(BaseModel):
    device_id: str
    display_name: str | None = None
    provider_id: str
    driver: str | None = None
    architecture_supported: bool = True
    discovered: bool = True
    classification: str
    last_qualified_at: datetime | None = None


class QualificationRunRequest(BaseModel):
    device_id: str
    do_capture: bool = True
    do_survey: bool = False
    capture_duration: float | None = None


class DeviceCaptureRequest(BaseModel):
    """Operator bounded RX-capture request (RX only — never transmits)."""

    lease_id: str | None = None   # if given, capture under this already-held active lease
    backend_id: str | None = None
    frequency_hz: float | None = None
    sample_rate: float | None = None
    gain_db: float | None = None
    duration_s: float | None = None
