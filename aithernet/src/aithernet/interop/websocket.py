"""In-process WebSocket connection registry for external agents (Stage 14D).

The agent connects INBOUND to the node (the node never dials arbitrary WebSocket URLs).
This manager tracks live connections, enforces per-agent + global connection limits, and
fans freshly persisted events out to the matching live subscription via a bounded per-
connection queue. A slow consumer is detected (queue overflow) and signalled for close — it
never blocks mission execution or other agents. Persisted messages remain replayable, so a
dropped/slow socket loses nothing: the agent reconnects and replays from its ack cursor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass(eq=False)
class LiveConnection:
    """One live authenticated WebSocket connection bound to a subscription."""

    session_id: str
    agent_id: str
    subscription_id: str | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    slow: bool = False

    def offer(self, envelope: dict) -> bool:
        """Enqueue an event for sending. Returns False if the consumer is too slow."""
        try:
            self.queue.put_nowait(envelope)
            return True
        except asyncio.QueueFull:
            self.slow = True
            return False


class InteropWebsocketManager:
    """Tracks live agent WebSocket connections and routes events to them."""

    def __init__(self, *, maximum_connections: int = 32, per_agent: int = 4) -> None:
        self._by_agent: dict[str, set[LiveConnection]] = {}
        self._maximum = maximum_connections
        self._per_agent = per_agent

    @property
    def total(self) -> int:
        return sum(len(conns) for conns in self._by_agent.values())

    def count_for_agent(self, agent_id: str) -> int:
        return len(self._by_agent.get(agent_id, ()))

    def can_accept(self, agent_id: str) -> tuple[bool, str]:
        if self.total >= self._maximum:
            return False, "max_connections"
        if self.count_for_agent(agent_id) >= self._per_agent:
            return False, "per_agent_connections"
        return True, "ok"

    def register(self, conn: LiveConnection, *, queue_limit: int = 256) -> None:
        conn.queue = asyncio.Queue(maxsize=queue_limit)
        self._by_agent.setdefault(conn.agent_id, set()).add(conn)

    def unregister(self, conn: LiveConnection) -> None:
        conns = self._by_agent.get(conn.agent_id)
        if conns is not None:
            conns.discard(conn)
            if not conns:
                self._by_agent.pop(conn.agent_id, None)

    def push(self, *, agent_id: str, subscription_id: str, envelope: dict) -> int:
        """Offer an envelope to every live connection on this subscription.

        Returns the number of connections that accepted it. Connections that are too slow
        are flagged ``slow`` and skipped (their socket task closes them).
        """
        delivered = 0
        for conn in list(self._by_agent.get(agent_id, ())):
            if conn.subscription_id == subscription_id and not conn.slow:
                if conn.offer(envelope):
                    delivered += 1
        return delivered

    def live_subscription_ids(self, agent_id: str) -> set[str]:
        return {
            c.subscription_id
            for c in self._by_agent.get(agent_id, ())
            if c.subscription_id is not None
        }
