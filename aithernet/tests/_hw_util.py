"""Test helpers for Stage 14B managed hardware: an explicit fake discovery backend + runtime.

Production never invents devices; tests inject this fake provider through the registry (the
supported extension point) so the inventory/lease/binding paths can be exercised without real SDR
hardware.
"""

from __future__ import annotations

from aithernet.config.settings import (
    HardwareConfig,
    MissionExecutionConfig,
    NodeConfig,
)
from aithernet.hardware.contracts import DeviceHealth, HealthState
from aithernet.hardware.discovery import descriptor_to_caps, descriptor_to_discovered
from aithernet.orchestrator.runtime import NodeRuntime


class FakeDiscoveryBackend:
    """An explicit, controllable fake discovery provider (descriptor-driven)."""

    def __init__(self, backend_id: str = "fake", descriptors=None, *, available: bool = True):
        self.backend_id = backend_id
        self.descriptors = list(descriptors or [])
        self.available_flag = available
        self.fail_discover = False
        self._map: dict[str, dict] = {}

    def set_descriptors(self, descriptors) -> None:
        self.descriptors = list(descriptors)

    async def available(self) -> bool:
        return self.available_flag

    async def discover(self):
        if self.fail_discover:
            raise RuntimeError("simulated provider failure")
        self._map = {}
        out = []
        for d in self.descriptors:
            device = descriptor_to_discovered(self.backend_id, d)
            self._map[device.hardware_key] = d
            out.append(device)
        return out

    async def probe(self, device):
        return descriptor_to_caps(self._map.get(device.hardware_key, {}), source="probed")

    async def health(self, device):
        d = self._map.get(device.hardware_key)
        if d is None:
            return DeviceHealth(state=HealthState.MISSING)
        return DeviceHealth(state=str(d.get("health", HealthState.OK)))


def descriptor(
    *, serial: str | None = "ABC123", vendor: str = "Ettus", product: str = "B210",
    driver: str = "uhd", kind: str = "sdr", rx: bool = True, tx: bool = True, channels: int = 2,
    freq=((70e6, 6e9),), sample_rates=((1e6, 56e6),), shared_receive_safe: bool = False,
    health: str = "ok", metadata: dict | None = None,
) -> dict:
    """Build an operator/tool device descriptor for the fake provider."""
    return {
        "vendor": vendor, "product": product, "serial": serial, "driver": driver,
        "device_kind": kind, "health": health, "metadata": metadata or {},
        "capabilities": {
            "rx": rx, "tx": tx, "channels": channels,
            "frequency_ranges": [list(r) for r in freq],
            "sample_rate_ranges": [list(r) for r in sample_rates],
            "shared_receive_safe": shared_receive_safe,
        },
    }


def make_hw_runtime(
    tmp_path, *, descriptors=None, provider_id: str = "fake", required: bool = False,
    operator: bool = True, mission_execution: bool = False, **hw_kwargs,
) -> tuple[NodeRuntime, FakeDiscoveryBackend]:
    """Build a NodeRuntime with hardware enabled and a fake discovery backend injected."""
    base = tmp_path
    cfg = NodeConfig(
        node_id="hw-node", node_name="hw-node",
        database_url=f"sqlite:///{base}/hw.db",
        mission_execution=MissionExecutionConfig(enabled=mission_execution),
        hardware=HardwareConfig(
            enabled=True, operator_lease_actions_enabled=operator, discovery_on_start=False,
            discovery_interval_seconds=1, lease_duration_seconds=5,
            lease_renew_interval_seconds=2, stale_lease_grace_seconds=0,
            health_event_min_interval_seconds=0, **hw_kwargs,
        ),
    )
    runtime = NodeRuntime.from_config(cfg)
    fake = FakeDiscoveryBackend(provider_id, descriptors)
    runtime.hardware_discovery.register(fake, required=required)
    return runtime, fake
