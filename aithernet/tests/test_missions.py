"""Mission endpoint tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_create_mission_persists(client: TestClient) -> None:
    response = client.post(
        "/missions",
        json={"content": "Scan 433 MHz band", "source_type": "user", "source_id": "op-1"},
    )
    assert response.status_code == 201
    mission = response.json()
    assert mission["id"]
    assert mission["content"] == "Scan 433 MHz band"
    assert mission["source_type"] == "user"
    assert mission["source_id"] == "op-1"
    assert mission["status"] == "received"
    assert mission["created_at"]

    # The mission is retrievable by id.
    got = client.get(f"/missions/{mission['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == mission["id"]


def test_create_mission_emits_event(client: TestClient) -> None:
    created = client.post("/missions", json={"content": "Listen on 915 MHz"}).json()

    events = client.get("/events", params={"mission_id": created["id"]}).json()
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "mission.received"
    assert event["mission_id"] == created["id"]
    assert event["source"] == "runtime"


def test_list_missions_newest_first(client: TestClient) -> None:
    first = client.post("/missions", json={"content": "first"}).json()
    second = client.post("/missions", json={"content": "second"}).json()

    missions = client.get("/missions").json()
    assert len(missions) == 2
    assert {m["id"] for m in missions} == {first["id"], second["id"]}


def test_get_missing_mission_returns_404(client: TestClient) -> None:
    response = client.get("/missions/does-not-exist")
    assert response.status_code == 404


def test_create_mission_rejects_empty_content(client: TestClient) -> None:
    response = client.post("/missions", json={"content": ""})
    assert response.status_code == 422
