"""Sanitized field-validation event types (Stage 14C.2, Part S).

Payloads carry only ids, states, bounded counts, and sanitized metrics — never credentials,
lease tokens, private identity, raw paths, or command output. Repeated measurement/sample events
are rate-limited by the service.
"""

from __future__ import annotations

EVENT_CAMPAIGN_STARTED = "field.campaign.started"
EVENT_CAMPAIGN_COMPLETED = "field.campaign.completed"
EVENT_CAMPAIGN_FAILED = "field.campaign.failed"

EVENT_CHECK_STARTED = "field.check.started"
EVENT_CHECK_PASSED = "field.check.passed"
EVENT_CHECK_FAILED = "field.check.failed"
EVENT_CHECK_NOT_EXECUTED = "field.check.not_executed"

EVENT_SOAK_SAMPLED = "field.soak.sampled"
EVENT_THRESHOLD_EXCEEDED = "field.threshold.exceeded"

EVENT_FAULT_INJECTED = "field.fault.injected"
EVENT_FAULT_RECOVERED = "field.fault.recovered"
EVENT_FAULT_CLEANUP_FAILED = "field.fault.cleanup_failed"

EVENT_NODE_JOINED = "field.node.joined"
EVENT_NODE_UNAVAILABLE = "field.node.unavailable"
EVENT_NODE_RECOVERED = "field.node.recovered"

EVENT_RF_PAYLOAD_STARTED = "field.rf.payload.started"
EVENT_RF_PAYLOAD_COMPLETED = "field.rf.payload.completed"
EVENT_RF_PAYLOAD_FAILED = "field.rf.payload.failed"
