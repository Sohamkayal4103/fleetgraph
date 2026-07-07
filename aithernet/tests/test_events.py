"""Event endpoint and status-count tests."""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
from fastapi.testclient import TestClient

from aithernet.orchestrator.event_bus import EventBus


def test_events_empty_initially(client: TestClient) -> None:
    response = client.get("/events")
    assert response.status_code == 200
    assert response.json() == []


def test_events_listed_after_mission(client: TestClient) -> None:
    client.post("/missions", json={"content": "mission A"})
    client.post("/missions", json={"content": "mission B"})

    events = client.get("/events").json()
    assert len(events) == 2
    assert all(e["event_type"] == "mission.received" for e in events)


def test_events_filter_by_mission(client: TestClient) -> None:
    a = client.post("/missions", json={"content": "A"}).json()
    client.post("/missions", json={"content": "B"}).json()

    filtered = client.get("/events", params={"mission_id": a["id"]}).json()
    assert len(filtered) == 1
    assert filtered[0]["mission_id"] == a["id"]


def test_status_reflects_counts(client: TestClient) -> None:
    client.post("/missions", json={"content": "one"})
    client.post("/missions", json={"content": "two"})

    status = client.get("/node/status").json()
    assert status["mission_count"] == 2
    assert status["event_count"] == 2


def test_event_bus_fans_out_to_subscriber() -> None:
    """A published event reaches a live subscriber's queue."""

    async def scenario() -> dict:
        bus = EventBus()
        async with bus.subscribe() as queue:
            assert bus.subscriber_count == 1
            await bus.publish({"event_type": "unit.test"})
            return await asyncio.wait_for(queue.get(), timeout=1.0)

    event = asyncio.run(scenario())
    assert event["event_type"] == "unit.test"


def test_event_stream_delivers_new_event(live_server: str) -> None:
    """The SSE stream delivers an event produced after the connection opens.

    Driven against a real server: one thread tails ``/events/stream`` while the main
    thread submits a mission once the stream is confirmed connected.
    """
    base_url = live_server
    connected = threading.Event()
    received: list[dict] = []

    def consume() -> None:
        with httpx.Client(timeout=10.0) as stream_client:
            with stream_client.stream("GET", f"{base_url}/events/stream") as response:
                for line in response.iter_lines():
                    if line.startswith(":"):
                        connected.set()
                    elif line.startswith("data:"):
                        received.append(json.loads(line[len("data:") :].strip()))
                        return

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    assert connected.wait(timeout=5), "stream never connected"

    posted = httpx.post(
        f"{base_url}/missions", json={"content": "streamed mission"}, timeout=10.0
    )
    assert posted.status_code == 201

    consumer.join(timeout=5)
    assert received, "no event received from stream"
    assert received[0]["event_type"] == "mission.received"
    assert received[0]["mission_id"] == posted.json()["id"]
