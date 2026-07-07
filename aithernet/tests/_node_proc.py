"""Standalone Aithernet node entry-point for real-process integration tests (Stage 13D.2).

Launched as a SEPARATE OS process by ``test_artifact_restart.py`` so the restart/resume proof
terminates and re-launches an actual application process (not an in-memory runtime). All state
(node id, identity, database, artifact store, ports, chunk delay) comes from environment
variables, so the test can stop the process and relaunch it against the SAME persistent state.

This is test infrastructure only — it imports the normal production app/runtime and adds no
behavior beyond reading config from the environment. The only test-specific knob is the
artifact ``worker_chunk_delay_seconds``, a production config field that defaults to 0.
"""

from __future__ import annotations

import os

import uvicorn

from aithernet.api.app import create_app
from aithernet.config.settings import (
    AgentTransportConfig,
    ArtifactConfig,
    CommunicationConfig,
    CoordinatorConfig,
    DataCollectionConfig,
    DataExportConfig,
    DataPlatformConfig,
    ExternalAgentsCallbacksConfig,
    ExternalAgentsConfig,
    ExternalAgentsDeliveryConfig,
    ExternalAgentsWebsocketConfig,
    HardwareConfig,
    HardwareDiscoveryProviderConfig,
    IdentityConfig,
    MCPServerConfig,
    MCPSessionConfig,
    MissionExecutionConfig,
    NodeConfig,
    TransportLocalDevelopmentConfig,
    TransportOutboundConfig,
)
from aithernet.coordinator.runtime import CoordinatorRuntime
from aithernet.orchestrator.runtime import NodeRuntime


def _truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


def _config() -> NodeConfig:
    # Stage 13D.3: a short freshness window lets the test drive a snapshot to "stale" using
    # persisted timestamps. mission_execution is enabled so an inbound mission runs through real
    # lifecycle transitions (which the production mission-status publisher reports to the peer).
    return NodeConfig(
        node_id=os.environ["NODE_ID"],
        node_name=os.environ["NODE_ID"],
        host="127.0.0.1",
        port=int(os.environ["PORT"]),
        database_url=f"sqlite:///{os.environ['DB_PATH']}",
        log_level="warning",
        coordinator=CoordinatorConfig(provider="scripted_peer"),
        # No GNU Radio MCP autostart in these tests — the legacy backend is not needed and its
        # subprocess launch only slows/flakes node startup.
        gnuradio_mcp=MCPServerConfig(session=MCPSessionConfig(autostart=False, auto_restart=False)),
        mission_execution=MissionExecutionConfig(
            enabled=_truthy("MISSION_EXEC", "1"), poll_interval_seconds=0.2,
            lease_duration_seconds=30, lease_renew_interval_seconds=5,
        ),
        communication=CommunicationConfig(
            status_freshness_seconds=float(os.environ.get("STATUS_FRESHNESS", "300")),
        ),
        agent_transport=AgentTransportConfig(
            identity=IdentityConfig(state_directory=os.environ["IDENTITY_DIR"]),
            outbound=TransportOutboundConfig(enabled=True, poll_interval_seconds=0.2),
            local_development=TransportLocalDevelopmentConfig(allow_insecure_http=True),
        ),
        artifacts=ArtifactConfig(
            enabled=True,
            state_directory=os.environ["STORE_DIR"],
            chunk_bytes=int(os.environ.get("CHUNK_BYTES", str(64 * 1024))),
            worker_poll_interval_seconds=0.2,
            minimum_free_bytes=0,
            grant_ttl_seconds=float(os.environ.get("GRANT_TTL", "3600")),
            worker_chunk_delay_seconds=float(os.environ.get("CHUNK_DELAY", "0")),
        ),
        hardware=_hardware_config(),
        external_agents=_external_agents_config(),
        data_platform=_data_platform_config(),
    )


def _data_platform_config() -> DataPlatformConfig:
    """Stage 14E: enable the data platform with export to an HTTP ingestion destination."""
    if not _truthy("DP_ENABLED", "0"):
        return DataPlatformConfig(enabled=True)  # local-first default (export off)
    from aithernet.config.settings import DataPrivacyConfig
    return DataPlatformConfig(
        enabled=True, tenant_id=os.environ.get("DP_TENANT", "tenant-a"),
        collection=DataCollectionConfig(
            operational_enabled=True, training_trajectories_enabled=True,
        ),
        export=DataExportConfig(
            enabled=True, paused=False,
            maximum_attempts=int(os.environ.get("DP_MAX_ATTEMPTS", "8")),
            retry_base_seconds=0.2, retry_max_seconds=2.0, poll_interval_seconds=0.2,
            claim_seconds=10.0,
        ),
        # Secret-backed pseudonymization: the test sets DP_PSEUDO_SECRET in the node env.
        privacy=DataPrivacyConfig(pseudonymization_key_ref="DP_PSEUDO_SECRET"),
    )


def _external_agents_config() -> ExternalAgentsConfig:
    """Stage 14D: enable the external-agent gateway under a LOCAL-TEST network policy.

    Callbacks use an explicit private-development policy (http + loopback allowlisted) so the
    process acceptance test can run a local webhook receiver — production defaults stay https-
    only with loopback/metadata/private destinations rejected.
    """
    if not _truthy("EA_ENABLED", "0"):
        return ExternalAgentsConfig(enabled=False)
    return ExternalAgentsConfig(
        enabled=True,
        callbacks=ExternalAgentsCallbacksConfig(
            enabled=True, https_required=False, allow_private_networks=True,
            allowed_hosts=["127.0.0.1", "localhost"], allowed_cidrs=["127.0.0.0/8"],
            verification_required=False, redirects_enabled=False,
        ),
        delivery=ExternalAgentsDeliveryConfig(
            worker_enabled=True, maximum_attempts=int(os.environ.get("EA_MAX_ATTEMPTS", "20")),
            retry_base_seconds=0.2, retry_max_seconds=2.0, poll_interval_seconds=0.2,
            claim_seconds=10.0,
        ),
        websocket=ExternalAgentsWebsocketConfig(
            enabled=True, heartbeat_seconds=2.0, idle_timeout_seconds=30.0,
        ),
    )


def _hardware_config() -> HardwareConfig:
    """Stage 14B: a static_file discovery provider reading an operator-declared descriptors file.

    The acceptance smoke rewrites that file to make a device appear/disappear/return. Short
    intervals make discovery/reconciliation fast; operator lease actions are enabled so the test
    can acquire/release leases over the API without a coordinator.
    """
    if not _truthy("HW_ENABLED", "0"):
        return HardwareConfig(enabled=False)
    # Stage 14C.1: a REAL soapy PlutoSDR provider (HW_REAL_PLUTO=1) for the real-process restart
    # acceptance test; otherwise the Stage 14B static_file fake provider.
    if _truthy("HW_REAL_PLUTO", "0"):
        from aithernet.config.settings import HardwareQualificationConfig
        return HardwareConfig(
            enabled=True,
            operator_lease_actions_enabled=True,
            discovery_on_start=True,
            discovery_interval_seconds=float(os.environ.get("HW_DISCOVERY_INTERVAL", "1.0")),
            health_interval_seconds=1.0,
            lease_duration_seconds=float(os.environ.get("HW_LEASE_SECONDS", "120")),
            lease_renew_interval_seconds=5.0,
            stale_lease_grace_seconds=float(os.environ.get("HW_GRACE", "8.0")),
            health_event_min_interval_seconds=0.0,
            qualification=HardwareQualificationConfig(
                gnuradio_python=os.environ.get("GR_PYTHON", "/usr/bin/python3"),
                default_sample_rate=2_000_000.0, default_gain_db=40.0,
                default_duration_seconds=1.0, default_rx_frequency_hz=2_437_000_000.0),
            providers={
                "pluto": HardwareDiscoveryProviderConfig(
                    kind="soapy", command="SoapySDRUtil",
                    args=["--find=driver=plutosdr"], timeout_seconds=20),
            },
        )
    return HardwareConfig(
        enabled=True,
        operator_lease_actions_enabled=True,
        discovery_on_start=True,
        discovery_interval_seconds=0.5,
        health_interval_seconds=0.5,
        lease_duration_seconds=float(os.environ.get("HW_LEASE_SECONDS", "3")),
        lease_renew_interval_seconds=1.0,
        stale_lease_grace_seconds=0.0,
        health_event_min_interval_seconds=0.0,
        providers={
            "lab": HardwareDiscoveryProviderConfig(
                kind="static_file", descriptors_file=os.environ["HW_DESCRIPTORS_FILE"],
            ),
        },
    )


def _coordinator(config) -> CoordinatorRuntime | None:
    """A deterministic scripted coordinator when NODE_ROLE is set (requester/responder)."""
    role = os.environ.get("NODE_ROLE")
    if not role:
        return None
    from _scripted_coordinator import ScriptedPeerCoordinator
    return CoordinatorRuntime(config.coordinator, ScriptedPeerCoordinator(config.coordinator, role))


def main() -> None:
    config = _config()
    runtime = NodeRuntime.from_config(config, coordinator=_coordinator(config))
    uvicorn.run(create_app(runtime=runtime), host="127.0.0.1",
                port=int(os.environ["PORT"]), log_level="warning")


if __name__ == "__main__":
    main()
