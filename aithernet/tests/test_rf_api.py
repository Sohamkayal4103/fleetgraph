"""Stage 13A.5 API / CLI / routing / backward-compat tests (Part T items 18-54, 60-66)."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from _rf_util import make_rf_runtime
from aithernet.api.app import create_app
from aithernet.cli import app as cli_app

runner = CliRunner()


def _client(rt) -> TestClient:
    return TestClient(create_app(runtime=rt))


# -- API: backend list/status/start/stop, tools, call, context, artifacts --------


def test_backend_list_and_status(tmp_path) -> None:
    rt = make_rf_runtime(tmp_path, legacy=False, marconi=True)
    with _client(rt) as client:
        backends = client.get("/rf/backends").json()
        ids = {b["backend_id"] for b in backends}
        assert "marconi" in ids and "legacy_gr_mcp" in ids
        marconi = client.get("/rf/backends/marconi").json()
        assert marconi["experimental"] is True and marconi["state"] in ("stopped", "disabled")
        assert client.get("/rf/backends/nope").status_code == 404


def test_start_call_context_artifacts_flow(tmp_path) -> None:
    rt = make_rf_runtime(tmp_path, marconi=True)
    with _client(rt) as client:
        started = client.post("/rf/backends/marconi/start").json()
        assert started["state"] == "ready" and started["version"] == "9.9.9-fake"
        tools = client.get("/rf/backends/marconi/tools").json()
        assert any(t["name"] == "list_devices" for t in tools)
        # One atomic call.
        call = client.post(
            "/rf/backends/marconi/call",
            json={"tool_name": "capture", "arguments": {"device_id": "sim0"}},
        ).json()
        assert call["backend_id"] == "marconi" and call["status"] == "completed"
        # Context + artifacts.
        ctx = client.get("/rf/contexts/marconi").json()
        assert ctx["backend_id"] == "marconi"
        arts = client.get("/rf/artifacts", params={"backend_id": "marconi"}).json()
        assert any(a["relative_path"] == "captures/c1.cf32" for a in arts)
        # Stopped backend call -> 409 (honest, not a network error).
        client.post("/rf/backends/marconi/stop")
        rejected = client.post(
            "/rf/backends/marconi/call", json={"tool_name": "list_devices", "arguments": {}}
        )
        assert rejected.status_code == 409


def test_unknown_backend_and_missing_tool_categorized(tmp_path) -> None:
    rt = make_rf_runtime(tmp_path, marconi=True)
    with _client(rt) as client:
        assert client.post("/rf/backends/ghost/start").status_code == 404
        client.post("/rf/backends/marconi/start")
        missing = client.post(
            "/rf/backends/marconi/call", json={"tool_name": "ghost_tool", "arguments": {}}
        )
        assert missing.status_code == 404


def test_legacy_endpoints_unchanged(tmp_path) -> None:
    # Backward-compat: /mcp/* and /gnuradio/context still work and target the legacy backend.
    rt = make_rf_runtime(tmp_path, legacy=False, marconi=True)
    with _client(rt) as client:
        assert client.get("/mcp/session").status_code == 200
        assert client.get("/mcp/status").status_code == 200
        assert client.get("/gnuradio/context").status_code == 200
        assert client.get("/mcp/tool-calls").status_code == 200


def test_polling_creates_no_subprocess(tmp_path) -> None:
    rt = make_rf_runtime(tmp_path, marconi=True)
    with _client(rt) as client:
        # Repeated status reads must not start the (non-autostart) Marconi process.
        for _ in range(5):
            client.get("/rf/backends")
            client.get("/rf/backends/marconi")
            client.get("/rf/contexts")
        assert client.get("/rf/backends/marconi").json()["pid"] is None


def test_benchmark_api(tmp_path) -> None:
    rt = make_rf_runtime(tmp_path, marconi=True)
    with _client(rt) as client:
        client.post("/rf/backends/marconi/start")
        scenarios = client.get("/rf/benchmarks/scenarios").json()
        assert any(s["scenario_id"] == "tool_discovery" for s in scenarios)
        run = client.post(
            "/rf/benchmarks/run", params={"scenario_id": "tool_discovery", "backend_id": "marconi"}
        ).json()
        assert run[0]["backend_id"] == "marconi" and run[0]["backend_version"] == "9.9.9-fake"
        listed = client.get("/rf/benchmarks").json()
        assert len(listed) >= 1


def test_no_secrets_in_rf_responses(tmp_path) -> None:
    rt = make_rf_runtime(tmp_path, marconi=True)
    with _client(rt) as client:
        client.post("/rf/backends/marconi/start")
        for path in ("/rf/backends", "/rf/backends/marconi", "/rf/contexts/marconi"):
            blob = client.get(path).text.lower()
            assert "marconi_workspace" not in blob  # env var names never surfaced
            assert "environment" not in blob


# -- routing via mission step (one MCP call per step; budgets) --------------------


def test_rf_mcp_route_one_call_per_step(tmp_path) -> None:
    rt = make_rf_runtime(tmp_path, marconi=True)

    async def scenario():
        await rt.rf.start_backend("marconi")
        mission = await rt.create_mission(
            __import__("aithernet.schemas.missions", fromlist=["MissionCreate"]).MissionCreate(
                content="rf", source_type="user"
            )
        )
        from aithernet.coordinator.contracts import CoordinatorDecision

        decision = CoordinatorDecision.from_model_json(
            {"summary": "use marconi", "next_target": "rf_mcp", "action": "list",
             "message": "m", "structured_payload": {
                 "backend_id": "marconi", "tool_name": "list_devices", "arguments": {}},
             "expected_result": "devices"},
            mission_id=mission.id,
        )
        before = len(rt.list_mcp_tool_calls())
        route = await rt._route_rf_mcp(mission.id, decision)
        after = len(rt.list_mcp_tool_calls())
        assert route.status == "completed"
        assert route.result["backend_id"] == "marconi"
        assert (after - before) == 1  # exactly one MCP call for the step
        await rt.rf.shutdown()

    asyncio.run(scenario())


def test_rf_mcp_route_unknown_backend_failed(tmp_path) -> None:
    rt = make_rf_runtime(tmp_path, marconi=True)
    from aithernet.coordinator.contracts import CoordinatorDecision

    decision = CoordinatorDecision.from_model_json(
        {"summary": "s", "next_target": "rf_mcp", "action": "a", "message": "m",
         "structured_payload": {"backend_id": "ghost", "tool_name": "x", "arguments": {}},
         "expected_result": "e"},
        mission_id="m1",
    )
    route = asyncio.run(rt._route_rf_mcp("m1", decision))
    assert route.status == "failed"


def test_rf_mcp_route_stopped_backend_blocked(tmp_path) -> None:
    rt = make_rf_runtime(tmp_path, marconi=True)
    from aithernet.coordinator.contracts import CoordinatorDecision

    decision = CoordinatorDecision.from_model_json(
        {"summary": "s", "next_target": "rf_mcp", "action": "a", "message": "m",
         "structured_payload": {"backend_id": "marconi", "tool_name": "list_devices",
                                "arguments": {}}, "expected_result": "e"},
        mission_id="m1",
    )
    route = asyncio.run(rt._route_rf_mcp("m1", decision))
    assert route.status == "blocked"  # not ready -> blocked, never replayed


# -- CLI -------------------------------------------------------------------------


def test_cli_rf_backends_and_tools(tmp_path) -> None:
    rt = make_rf_runtime(tmp_path, marconi=True)
    import threading
    import time

    import uvicorn

    app = create_app(runtime=rt)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8231, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server start timeout")
        time.sleep(0.02)
    try:
        url = "http://127.0.0.1:8231"
        backends = runner.invoke(cli_app, ["rf", "backends", "--url", url])
        assert backends.exit_code == 0 and "marconi" in backends.stdout
        assert "experimental" in backends.stdout
        start = runner.invoke(cli_app, ["rf", "backend", "start", "marconi", "--url", url])
        assert start.exit_code == 0
        tools = runner.invoke(cli_app, ["rf", "tools", "marconi", "--url", url])
        assert tools.exit_code == 0 and "list_devices" in tools.stdout
        call = runner.invoke(cli_app, [
            "rf", "call", "marconi", "list_devices", "--url", url
        ])
        assert call.exit_code == 0 and "completed" in call.stdout
        bench = runner.invoke(cli_app, [
            "rf", "benchmark", "run", "tool_discovery", "--backend-id", "marconi", "--url", url
        ])
        assert bench.exit_code == 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)
