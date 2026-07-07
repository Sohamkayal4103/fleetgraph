"""Sanitized managed-hardware event types (Stage 14B, Part Q).

Event payloads carry only ids, provider names, states, and bounded capability counts — never
full device descriptors, raw USB/device paths, environment data, arbitrary command output,
credentials, or absolute system paths. Repeated health events are rate-limited by the inventory
service so a flapping device cannot flood the log.
"""

from __future__ import annotations

EVENT_DISCOVERY_STARTED = "hardware.discovery.started"
EVENT_DISCOVERY_COMPLETED = "hardware.discovery.completed"
EVENT_DISCOVERY_FAILED = "hardware.discovery.failed"

EVENT_DEVICE_DISCOVERED = "hardware.device.discovered"
EVENT_DEVICE_UPDATED = "hardware.device.updated"
EVENT_DEVICE_MISSING = "hardware.device.missing"
EVENT_DEVICE_RETURNED = "hardware.device.returned"
EVENT_DEVICE_HEALTH_CHANGED = "hardware.device.health_changed"
EVENT_CAPABILITIES_UPDATED = "hardware.capabilities.updated"

EVENT_LEASE_REQUESTED = "hardware.lease.requested"
EVENT_LEASE_ACQUIRED = "hardware.lease.acquired"
EVENT_LEASE_RENEWED = "hardware.lease.renewed"
EVENT_LEASE_RELEASED = "hardware.lease.released"
EVENT_LEASE_EXPIRED = "hardware.lease.expired"
EVENT_LEASE_REVOKED = "hardware.lease.revoked"
EVENT_LEASE_FAILED = "hardware.lease.failed"
EVENT_LEASE_ORPHANED = "hardware.lease.orphaned"

# -- Stage 14C.1 real-hardware qualification + physical capture --
EVENT_QUALIFICATION_STARTED = "hardware.qualification.started"
EVENT_QUALIFICATION_CHECK_STARTED = "hardware.qualification.check_started"
EVENT_QUALIFICATION_CHECK_PASSED = "hardware.qualification.check_passed"
EVENT_QUALIFICATION_CHECK_FAILED = "hardware.qualification.check_failed"
EVENT_QUALIFICATION_CHECK_NOT_EXECUTED = "hardware.qualification.check_not_executed"
EVENT_QUALIFICATION_COMPLETED = "hardware.qualification.completed"

EVENT_PHYSICAL_CAPTURE_STARTED = "hardware.physical_capture.started"
EVENT_PHYSICAL_CAPTURE_COMPLETED = "hardware.physical_capture.completed"
EVENT_PHYSICAL_CAPTURE_FAILED = "hardware.physical_capture.failed"
