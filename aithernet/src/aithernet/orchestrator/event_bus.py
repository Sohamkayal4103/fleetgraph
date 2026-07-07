"""In-process publish/subscribe bus for live runtime events.

The bus is intentionally simple and dependency-free: publishers push already-serialized
event dicts, and each subscriber receives them through its own bounded queue. It powers
the ``GET /events/stream`` endpoint and is the integration point for future stages that
will react to runtime events (coordinator scheduling, peer fan-out, etc.).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

#: Per-subscriber queue bound. A slow consumer drops the oldest event rather than
#: letting the producer block or memory grow without limit.
_QUEUE_MAXSIZE = 1000


class EventBus:
    """A minimal asyncio fan-out bus.

    All methods are coroutine-safe under a single event loop, which is how the API
    runs. Subscribers are managed via the :meth:`subscribe` async context manager so
    they are always unregistered on disconnect.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict]] = set()

    @property
    def subscriber_count(self) -> int:
        """Number of currently connected subscribers."""
        return len(self._subscribers)

    async def publish(self, event: dict) -> None:
        """Deliver ``event`` to every current subscriber.

        If a subscriber's queue is full the oldest event is discarded to make room,
        keeping a stalled stream from blocking mission processing.
        """
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await queue.put(event)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict]]:
        """Register a subscriber queue for the duration of the context."""
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
