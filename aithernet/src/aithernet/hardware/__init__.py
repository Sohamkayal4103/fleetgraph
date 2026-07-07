"""Managed SDR hardware inventory, capability discovery, leasing, and backend binding (Stage 14B).

This package adds hardware OWNERSHIP and lifecycle on top of the existing RF backends — discovery
-> stable device record -> factual capability probing -> health/availability -> compatibility
check -> durable lease -> backend binding -> active RF work -> renewal -> release/recovery. It does
not create RF sample processing itself, embeds no vendor logic in the mission engine, and never
modifies the external RF backends.
"""

from __future__ import annotations

from aithernet.hardware.contracts import (
    DeviceCapabilities,
    DeviceHealth,
    DiscoveredDevice,
    HardwareDiscoveryBackend,
)
from aithernet.hardware.inventory import InventoryService, InventoryWorker
from aithernet.hardware.leases import LeaseService
from aithernet.hardware.registry import HardwareDiscoveryRegistry

__all__ = [
    "DeviceCapabilities",
    "DeviceHealth",
    "DiscoveredDevice",
    "HardwareDiscoveryBackend",
    "HardwareDiscoveryRegistry",
    "InventoryService",
    "InventoryWorker",
    "LeaseService",
]
