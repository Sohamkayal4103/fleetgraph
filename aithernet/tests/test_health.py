"""Health and node-status endpoint tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "aithernet-node"
    assert body["version"]


def test_node_status_initial_counts(client: TestClient) -> None:
    response = client.get("/node/status")
    assert response.status_code == 200
    body = response.json()
    assert body["node_id"] == "test-node-id"
    assert body["node_name"] == "aithernet-test-node"
    assert body["runtime_status"] == "running"
    assert body["mission_count"] == 0
    assert body["event_count"] == 0
