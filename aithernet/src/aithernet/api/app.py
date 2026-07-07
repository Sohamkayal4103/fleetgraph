"""FastAPI application factory.

The app is built around a single :class:`~aithernet.orchestrator.runtime.NodeRuntime`
stored on ``app.state.runtime``. Routers depend on it via :func:`get_runtime`, which
keeps handlers thin and makes the runtime trivially swappable in tests.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from aithernet import __version__
from aithernet.config.loader import load_config
from aithernet.config.settings import NodeConfig
from aithernet.orchestrator.runtime import NodeRuntime


def get_runtime(request: Request) -> NodeRuntime:
    """FastAPI dependency returning the runtime bound to the running app."""
    return request.app.state.runtime


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage the persistent MCP session and the autonomous mission worker (Stage 12).

    On startup: if MCP is configured and autostart is enabled, start the single managed
    session (a gr-mcp failure does NOT crash the API — it reports ``failed``/``degraded``
    honestly); then start the mission worker manager, which recovers interrupted runs and
    begins polling without blocking startup. On shutdown: stop the worker (finishing at
    atomic boundaries, releasing leases) before closing the MCP session cleanly, so no
    orphan gr-mcp/coordinator/Codex process remains.
    """
    runtime: NodeRuntime = app.state.runtime
    from aithernet.ops.readiness import ComponentState, Phase

    runtime.readiness.set_phase(Phase.STARTING)
    # Stage 13A.5: start every enabled+autostart RF backend independently. A backend that fails
    # to start NEVER crashes the API; the default backend gates readiness only when explicitly
    # required, and an experimental non-autostart backend never affects readiness.
    try:
        await runtime.rf.start_enabled_backends()
        runtime.readiness.mark("rf_default_backend", await _rf_default_state(runtime))
    except Exception:  # noqa: BLE001 — startup never crashes on RF
        runtime.readiness.mark(
            "rf_default_backend", ComponentState.FAILED, detail="start_enabled_backends raised"
        )
    try:
        await runtime.worker_manager.start()  # recovers + polls; never blocks startup
        if runtime.config.mission_execution.enabled:
            runtime.readiness.mark("mission_worker", ComponentState.READY)
    except Exception:  # noqa: BLE001
        runtime.readiness.mark("mission_worker", ComponentState.FAILED)
    # Stage 13A: load/auto-init identity, recover in-flight outbox claims, start the delivery
    # worker. Never blocks startup if peers are unreachable.
    try:
        await runtime.transport.start()
        _t = runtime.config.agent_transport
        if _t.enabled and _t.outbound.enabled:
            runtime.readiness.mark("transport_worker", ComponentState.READY)
    except Exception:  # noqa: BLE001
        runtime.readiness.mark("transport_worker", ComponentState.FAILED)
    # Stage 13B/13D.2/13D.3 recovery — reply waits, artifact transfers, status publication.
    # Never blocks startup; the durable outbox + sequence ordering keep everything safe.
    with contextlib.suppress(Exception):
        await runtime.comms.recover()
    with contextlib.suppress(Exception):
        await runtime.artifact_worker.start()
    with contextlib.suppress(Exception):
        await runtime.mission_status.recover()
    # Stage 14B: recover persisted hardware inventory + reconcile stale leases, then start the
    # bounded inventory polling worker. Off by default; never blocks startup or crashes on a
    # missing discovery tool (a node with no hardware stays fully usable).
    with contextlib.suppress(Exception):
        await runtime.hardware_worker.start()
    # Stage 14D: start the external-agent durable delivery worker (recovers in-flight claims,
    # then polls). Off by default; never blocks startup or crashes when an endpoint is offline.
    with contextlib.suppress(Exception):
        await runtime.interop.start()
        _ea = runtime.config.external_agents
        if _ea.enabled and _ea.delivery.worker_enabled:
            runtime.readiness.mark(
                "interop_delivery_worker",
                ComponentState.DEGRADED if runtime.interop.worker.is_degraded()
                else ComponentState.READY,
            )
    # Stage 14E: start the data-platform export worker (recovers claims, then polls). Off-leaning
    # (export disabled + paused by default); never blocks startup or fails the node when a
    # destination is unavailable.
    with contextlib.suppress(Exception):
        await runtime.data.start()
        _dp = runtime.config.data_platform
        if _dp.enabled and _dp.export.enabled and _dp.export.worker_enabled:
            runtime.readiness.mark(
                "data_export_worker",
                ComponentState.DEGRADED if runtime.data.worker.is_degraded()
                else ComponentState.READY,
            )
        # Pseudonymization fails CLOSED when its master secret is missing: data export degrades
        # (and is not reported ready) but local node + mission execution continue unaffected.
        if _dp.enabled:
            runtime.readiness.mark(
                "data_pseudonymization",
                ComponentState.READY if runtime.data.pseudonymization_ready()
                else ComponentState.DEGRADED,
            )
    # Required components are up (or honestly degraded) — expose readiness now, not before.
    runtime.readiness.set_phase(Phase.READY)
    try:
        yield
    finally:
        # Graceful shutdown ordering (Stage 14A area 6): stop accepting work, persist, stop
        # workers, stop RF + subprocesses — no orphan MCP/coordinator/coding processes; pending
        # outbox + waits stay durable; missions recover on restart.
        runtime.readiness.set_phase(Phase.SHUTTING_DOWN)
        # Stage 14B: stop hardware inventory polling first (it creates no work and holds no
        # subprocess); leases stay durable in the DB and are reconciled on next start.
        with contextlib.suppress(Exception):
            await runtime.data.shutdown()
        with contextlib.suppress(Exception):
            await runtime.interop.shutdown()
        with contextlib.suppress(Exception):
            await runtime.hardware_worker.shutdown()
        with contextlib.suppress(Exception):
            await runtime.artifact_worker.shutdown()
        with contextlib.suppress(Exception):
            await runtime.transport.shutdown()  # stop claiming, finish/persist in-flight
        with contextlib.suppress(Exception):
            await runtime.worker_manager.shutdown()  # stop at atomic boundaries, release leases
        # Stop every RF backend session independently — legacy AND Marconi — no orphans.
        with contextlib.suppress(Exception):
            await runtime.rf.shutdown()


async def _rf_default_state(runtime: NodeRuntime):
    """Map the default RF backend's reported state to a readiness ComponentState."""
    from aithernet.ops.readiness import ComponentState

    with contextlib.suppress(Exception):
        default_id = runtime.config.rf_backends.default_backend
        for status_obj in await runtime.rf.list_statuses():
            if status_obj.backend_id == default_id:
                state = getattr(status_obj.state, "value", status_obj.state)
                if state in ("ready", "running"):
                    return ComponentState.READY
                if state == "degraded":
                    return ComponentState.DEGRADED
                return ComponentState.FAILED
    return ComponentState.DEGRADED


def _default_dashboard_dir() -> Path:
    """Return the built web-dashboard directory (``web/dist`` at the repo root)."""
    # app.py -> api -> aithernet -> src -> <repo root>
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "web" / "dist"


def create_app(
    config: NodeConfig | None = None,
    *,
    runtime: NodeRuntime | None = None,
    dashboard_dir: Path | None = None,
) -> FastAPI:
    """Build the node FastAPI application.

    ``config`` (or a fully constructed ``runtime``) may be supplied for tests; when
    neither is given, configuration is loaded from ``configs/node.yaml`` and the
    environment. Routers are imported here to avoid import cycles with this module.

    CORS is enabled (origins from ``config.cors_allow_origins``) so the Stage 8 browser
    dashboard can call the API cross-origin during development. If a built dashboard
    directory exists (``dashboard_dir``, defaulting to ``web/dist``), it is served at
    ``/dashboard`` — never required, and it never shadows the API routes.
    """
    # Resolve a real CA bundle so HTTPS provider/MCP clients never crash on a missing certifi
    # cacert.pem in the packaged /opt/aithernet environment. Verification stays on.
    from aithernet.tls import ensure_ssl_env
    ensure_ssl_env()

    if runtime is None:
        runtime = NodeRuntime.from_config(config or load_config())

    app = FastAPI(
        title="Aithernet Node",
        version=__version__,
        summary="Autonomous SDR node runtime — node, coordinator, coding agent + GNU Radio MCP.",
        lifespan=_lifespan,
    )
    app.state.runtime = runtime

    # No credentials are used (no cookies/auth yet), so a wildcard origin is safe; this is
    # a local operator tool. Restrict cors_allow_origins for shared deployments.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime.config.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from aithernet.api import (
        agent_api,
        agent_transport,
        agents,
        artifacts,
        coding_agent,
        coding_tasks,
        comms,
        coordinator,
        dashboard,
        data,
        events,
        external_agents,
        field,
        gnuradio,
        hardware,
        health,
        identity,
        mcp,
        mission_runs,
        mission_steps,
        missions,
        node,
        ops,
        peers,
        remote_missions,
        rf,
    )

    app.include_router(health.router)
    app.include_router(node.router)
    app.include_router(coordinator.router)
    app.include_router(coding_agent.router)
    app.include_router(coding_tasks.router)
    app.include_router(mcp.router)
    app.include_router(gnuradio.router)
    app.include_router(missions.router)
    app.include_router(mission_runs.router)
    app.include_router(mission_steps.router)
    app.include_router(identity.router)
    app.include_router(peers.router)
    app.include_router(agent_transport.agent_v1_router)
    app.include_router(agent_transport.transport_router)
    app.include_router(rf.router)
    app.include_router(comms.router)
    app.include_router(remote_missions.router)
    app.include_router(ops.router)
    app.include_router(hardware.router)
    app.include_router(field.router)
    app.include_router(external_agents.router)
    app.include_router(agent_api.router)
    app.include_router(data.router)
    app.include_router(dashboard.router)
    app.include_router(artifacts.router)
    app.include_router(agents.router)
    app.include_router(agents.messages_router)
    app.include_router(events.router)

    resolved_dashboard = dashboard_dir if dashboard_dir is not None else _default_dashboard_dir()
    if resolved_dashboard.is_dir():
        app.mount(
            "/dashboard",
            StaticFiles(directory=resolved_dashboard, html=True),
            name="dashboard",
        )

    return app
