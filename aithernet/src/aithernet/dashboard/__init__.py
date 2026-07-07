"""Stage 13C operator-dashboard read models.

Bounded, sanitized, read-only aggregate views over the already-persisted distributed state
(node/worker/coordinator health, fleet/peers, conversations, distributed-mission correlation,
communication summary). These compose the existing Stage 12/13A/13A.5/13B read helpers and
repositories — they never mutate state, never start a transport/mission/resume, and never
expose private keys, signatures, raw envelopes, prompts, credentials, or environment.
"""

from aithernet.dashboard.service import DashboardService

__all__ = ["DashboardService"]
