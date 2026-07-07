"""The RF backend registry (Stage 13A.5, Part C).

Owns one INDEPENDENT persistent MCP session per configured RF backend. The legacy GNU Radio
backend reuses the node's existing Stage 11B session (so ``/mcp/*`` stays backward-compatible
and there is exactly one legacy process); additional backends (e.g. Marconi) each get their
own subprocess, session id, generation, tool cache, concurrency, and lifecycle state. One
backend crashing or restarting never affects another. Aithernet talks to every backend ONLY
through MCP — it never imports a backend's Python modules.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from aithernet.config.settings import LEGACY_RF_BACKEND_ID, RFBackendConfig
from aithernet.mcp.clients.stdio import resolve_command
from aithernet.mcp.contracts import MCPSessionState, MCPToolInfo
from aithernet.mcp.session import MCPSessionManager
from aithernet.rf import events as ev
from aithernet.rf.adapters import (
    LegacyGrMcpSemanticAdapter,
    MarconiSemanticAdapter,
    RFSemanticAdapter,
)
from aithernet.rf.contracts import (
    BACKEND_KIND_MCP_STDIO,
    RFBackendDisabledError,
    RFBackendNotFoundError,
    RFBackendNotReadyError,
    RFBackendState,
    RFBackendStatus,
    RFBackendUnavailableError,
    RFToolNotFoundError,
)
from aithernet.schemas.mcp import (
    MCPToolCallCreate,
    MCPToolCallRead,
    MCPToolCallStatus,
)
from aithernet.state.models import utcnow
from aithernet.state.repositories import MCPToolCallRepository

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

#: MCP session events that should mark a backend's derived RF context stale.
_STALE_SESSION_EVENTS = frozenset(
    {"mcp.session.restarting", "mcp.session.degraded", "mcp.session.failed", "mcp.session.stopped"}
)

#: MCP session state -> RF backend state.
_STATE_MAP = {
    MCPSessionState.STOPPED: RFBackendState.STOPPED,
    MCPSessionState.STARTING: RFBackendState.STARTING,
    MCPSessionState.READY: RFBackendState.READY,
    MCPSessionState.DEGRADED: RFBackendState.DEGRADED,
    MCPSessionState.RESTARTING: RFBackendState.RESTARTING,
    MCPSessionState.STOPPING: RFBackendState.STOPPED,
    MCPSessionState.FAILED: RFBackendState.FAILED,
}


class RFBackend:
    """One registered RF backend: metadata + its own session manager + semantic adapter."""

    def __init__(
        self,
        *,
        backend_id: str,
        config: RFBackendConfig,
        session: MCPSessionManager,
        adapter: RFSemanticAdapter,
        is_default: bool,
        is_legacy: bool,
    ) -> None:
        self.backend_id = backend_id
        self.config = config
        self.session = session
        self.adapter = adapter
        self.is_default = is_default
        self.is_legacy = is_legacy

    @property
    def configured(self) -> bool:
        """True when the backend has a resolvable launch command."""
        return resolve_command(self.config.command) is not None or (
            self.is_legacy and self.session.is_configured()
        )

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.configured


class RFBackendRegistry:
    """Owns and operates every configured RF backend for one node."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.node_id = runtime.config.node_id
        self.rf_config = runtime.config.rf_backends
        self._backends: dict[str, RFBackend] = {}
        self._build()

    # -- construction ------------------------------------------------------------

    def _build(self) -> None:
        default_id = self.rf_config.default_backend
        legacy_session = self.runtime.mcp.session
        # The legacy backend reuses the node's existing Stage 11B session (single legacy
        # process). Read its autostart/command from the ACTUAL session config (which may be
        # injected, e.g. in tests) rather than the static gnuradio_mcp config, so the legacy
        # autostart behavior matches the live session exactly.
        legacy_server_cfg = (
            legacy_session.config if legacy_session is not None
            else self.runtime.config.gnuradio_mcp
        )
        legacy_meta = self.rf_config.backends.get(LEGACY_RF_BACKEND_ID) or RFBackendConfig(
            display_name="Legacy GNU Radio MCP", experimental=False, enabled=True,
        )
        legacy_meta = legacy_meta.model_copy(update={
            "experimental": False,
            "autostart": legacy_server_cfg.session.autostart,
            "command": legacy_server_cfg.command,
            "cwd": legacy_server_cfg.cwd,
        })
        if legacy_session is not None:
            self._backends[LEGACY_RF_BACKEND_ID] = RFBackend(
                backend_id=LEGACY_RF_BACKEND_ID,
                config=legacy_meta,
                session=legacy_session,
                adapter=LegacyGrMcpSemanticAdapter(self.runtime),
                is_default=(default_id == LEGACY_RF_BACKEND_ID),
                is_legacy=True,
            )
        # Additional backends (e.g. Marconi): one independent session each.
        for backend_id, backend_cfg in self.rf_config.backends.items():
            if backend_id == LEGACY_RF_BACKEND_ID:
                continue
            session = MCPSessionManager.from_config(
                backend_cfg.to_mcp_server_config(),
                node_id=self.node_id,
                on_event=self._make_emitter(backend_id),
            )
            adapter = MarconiSemanticAdapter(
                self.runtime, workspace=backend_cfg.workspace, context=self.runtime.rf_context
            ) if backend_id == "marconi" else RFSemanticAdapter()
            self._backends[backend_id] = RFBackend(
                backend_id=backend_id,
                config=backend_cfg,
                session=session,
                adapter=adapter,
                is_default=(default_id == backend_id),
                is_legacy=False,
            )

    def _make_emitter(self, backend_id: str):
        async def _emit(event_type: str, message: str, payload: dict) -> None:
            # A new generation / lost session means this backend's derived context is stale.
            if event_type in _STALE_SESSION_EVENTS:
                with contextlib.suppress(Exception):
                    self.runtime.rf_context.mark_stale(backend_id, f"session event: {event_type}")
            with contextlib.suppress(Exception):
                await self.runtime._emit_event(
                    event_type=event_type, mission_id=None, source="rf",
                    message=f"[{backend_id}] {message}",
                    payload={**payload, "backend_id": backend_id},
                )

        return _emit

    # -- lookup ------------------------------------------------------------------

    def backend_ids(self) -> list[str]:
        return list(self._backends)

    def _require(self, backend_id: str) -> RFBackend:
        backend = self._backends.get(backend_id)
        if backend is None:
            raise RFBackendNotFoundError(f"Unknown RF backend '{backend_id}'.")
        return backend

    def get_backend(self, backend_id: str) -> RFBackend:
        return self._require(backend_id)

    # -- lifecycle ---------------------------------------------------------------

    async def start_enabled_backends(self) -> None:
        """Start each enabled+autostart backend independently (one failure never blocks others)."""
        for backend in self._backends.values():
            if backend.enabled and backend.config.autostart:
                with contextlib.suppress(Exception):
                    await self.start_backend(backend.backend_id)

    async def shutdown(self) -> None:
        """Close every backend session independently (no orphan process)."""
        for backend in self._backends.values():
            with contextlib.suppress(Exception):
                await backend.session.close()

    async def start_backend(self, backend_id: str):
        backend = self._require(backend_id)
        if not backend.enabled:
            if not backend.configured:
                raise RFBackendUnavailableError(
                    f"RF backend '{backend_id}' has no resolvable executable."
                )
            raise RFBackendDisabledError(f"RF backend '{backend_id}' is disabled.")
        await backend.session.start()
        await self._emit(ev.EVENT_BACKEND_STARTING, backend_id, "start requested")
        return await self.get_status(backend_id)

    async def stop_backend(self, backend_id: str):
        backend = self._require(backend_id)
        await backend.session.stop()
        await self._emit(ev.EVENT_BACKEND_STOPPED, backend_id, "stopped")
        return await self.get_status(backend_id)

    async def restart_backend(self, backend_id: str):
        backend = self._require(backend_id)
        await backend.session.restart()
        self.runtime.rf_context.mark_stale(backend_id, "backend restarted")
        await self._emit(ev.EVENT_BACKEND_RESTARTED, backend_id, "restarted")
        return await self.get_status(backend_id)

    # -- status ------------------------------------------------------------------

    async def get_status(self, backend_id: str) -> RFBackendStatus:
        backend = self._require(backend_id)
        session_status = await backend.session.get_status()
        if not backend.enabled:
            state = RFBackendState.DISABLED
        else:
            state = _STATE_MAP.get(session_status.state, RFBackendState.STOPPED)
        return RFBackendStatus(
            backend_id=backend_id,
            display_name=backend.config.display_name,
            backend_kind=backend.config.kind or BACKEND_KIND_MCP_STDIO,
            experimental=backend.config.experimental,
            enabled=backend.enabled,
            default=backend.is_default,
            state=state,
            provider=session_status.provider,
            command=session_status.command,
            cwd=backend.config.cwd,
            version=backend.session.server_info().get("version"),
            source_revision=backend.config.source_revision,
            session_id=session_status.session_id if session_status.session_id != "-" else None,
            session_generation=session_status.generation,
            pid=session_status.pid,
            tool_count=session_status.tool_count or 0,
            active_requests=session_status.active_requests,
            max_in_flight_requests=session_status.max_in_flight_requests,
            workspace=backend.config.workspace,
            last_activity_at=session_status.last_activity_at,
            last_error_type=session_status.last_error_type,
            last_error=session_status.last_error,
        )

    async def list_statuses(self) -> list[RFBackendStatus]:
        return [await self.get_status(bid) for bid in self._backends]

    # -- tools -------------------------------------------------------------------

    async def list_tools(self, backend_id: str, *, refresh: bool = False) -> list[MCPToolInfo]:
        backend = self._require(backend_id)
        if not backend.enabled:
            raise RFBackendDisabledError(f"RF backend '{backend_id}' is disabled.")
        tools = await backend.session.list_tools(refresh=refresh)
        if refresh:
            await self._emit(ev.EVENT_TOOLS_REFRESHED, backend_id, f"{len(tools)} tools")
        return tools

    async def _live_tool_names(self, backend: RFBackend) -> set[str]:
        names = backend.session.cached_tool_names()
        if not names:
            with contextlib.suppress(Exception):
                names = {t.name for t in await backend.session.list_tools()}
        return names

    # -- call --------------------------------------------------------------------

    async def call_tool(
        self,
        backend_id: str,
        tool_name: str,
        arguments: dict,
        *,
        execution_context=None,
    ) -> MCPToolCallRead:
        """Execute EXACTLY ONE MCP tool call on a backend, persisting an audited record.

        Enforces: backend exists + enabled + ready; the tool is in the live discovered
        catalog of THAT backend; one call only; failed calls are never auto-replayed. The
        legacy backend delegates to the node's existing audited path; other backends use the
        generic path that emits ``rf.call.*`` events and runs the backend's semantic adapter.
        """
        from aithernet.rf.contracts import RFCallExecutionContext

        ctx = execution_context or RFCallExecutionContext()
        backend = self._require(backend_id)
        if not backend.enabled:
            if not backend.configured:
                raise RFBackendUnavailableError(
                    f"RF backend '{backend_id}' has no resolvable executable."
                )
            raise RFBackendDisabledError(f"RF backend '{backend_id}' is disabled.")

        status = await backend.session.get_status()
        if status.state != MCPSessionState.READY:
            raise RFBackendNotReadyError(
                f"RF backend '{backend_id}' is not ready (state={status.state.value})."
            )

        names = await self._live_tool_names(backend)
        if tool_name not in names:
            raise RFToolNotFoundError(
                f"Tool '{tool_name}' is not in the live catalog of backend '{backend_id}'."
            )

        if backend.is_legacy:
            # One audited path for legacy: identical events/context to /mcp/call (Stage 11B).
            return await self.runtime.call_mcp_tool(
                MCPToolCallCreate(
                    tool_name=tool_name,
                    arguments=arguments,
                    caller=ctx.caller,
                    mission_id=ctx.mission_id,
                    mission_step_id=ctx.mission_step_id,
                    task_id=ctx.task_id,
                )
            )
        return await self._execute_generic(backend, tool_name, arguments, ctx)

    async def _execute_generic(
        self, backend: RFBackend, tool_name: str, arguments: dict, ctx
    ) -> MCPToolCallRead:
        session_status = await backend.session.get_status()
        version = backend.session.server_info().get("version")
        meta = {
            "session_id": session_status.session_id if session_status.session_id != "-" else None,
            "session_generation": session_status.generation,
            "backend_version": version,
            "backend_source_revision": backend.config.source_revision,
            "mission_id": ctx.mission_id,
            "mission_run_id": ctx.mission_run_id,
            "mission_step_id": ctx.mission_step_id,
        }

        with self.runtime.session_scope() as session:
            calls = MCPToolCallRepository(session)
            call = calls.create(
                tool_name=tool_name,
                caller=ctx.caller,
                arguments=arguments,
                mission_id=ctx.mission_id,
                mission_step_id=ctx.mission_step_id,
                task_id=ctx.task_id,
                session_id=meta["session_id"],
                session_generation=session_status.generation or None,
                status=MCPToolCallStatus.RUNNING.value,
                backend_id=backend.backend_id,
                backend_kind=BACKEND_KIND_MCP_STDIO,
                backend_version=version,
                backend_source_revision=backend.config.source_revision,
            )
            session.commit()
            call_id = call.id
        await self._emit(
            ev.EVENT_CALL_STARTED, backend.backend_id, f"call {call_id} for '{tool_name}'",
            mission_id=ctx.mission_id, extra={"call_id": call_id, "tool_name": tool_name},
        )

        started = utcnow()
        try:
            result = await backend.session.call_tool(tool_name, arguments)
        except Exception as exc:  # MCPError/MCPConfigurationError + unexpected; never replayed
            self._update_call(
                call_id, status=MCPToolCallStatus.FAILED, error=str(exc),
                error_type=type(exc).__name__, duration_ms=_ms(started),
            )
            await self._emit(
                ev.EVENT_CALL_FAILED, backend.backend_id, f"call {call_id} failed",
                mission_id=ctx.mission_id,
                extra={"call_id": call_id, "error_type": type(exc).__name__},
            )
            raise

        succeeded = result.status == "completed"
        self._update_call(
            call_id,
            status=MCPToolCallStatus.COMPLETED if succeeded else MCPToolCallStatus.FAILED,
            result=result.result, error=result.error,
            error_type=None if succeeded else "MCPToolError",
            tool_catalog_generation=session_status.tool_catalog_generation,
            duration_ms=_ms(started),
        )
        persisted = self.runtime.get_mcp_tool_call(call_id)
        # Backend semantic adapter: update derived RF context + index artifacts (success only
        # touches success state; failures only record errors).
        summary_type = None
        with contextlib.suppress(Exception):
            summary_type = await backend.adapter.on_call(persisted, meta=meta)
        if summary_type:
            self._update_call(call_id, result_summary_type=summary_type)
            persisted = self.runtime.get_mcp_tool_call(call_id)
        await self._emit(
            ev.EVENT_CALL_COMPLETED if succeeded else ev.EVENT_CALL_FAILED,
            backend.backend_id,
            f"call {call_id} {'completed' if succeeded else 'failed'} for '{tool_name}'",
            mission_id=ctx.mission_id,
            extra={"call_id": call_id, "tool_name": tool_name, "status": persisted.status.value},
        )
        return persisted

    # -- context refresh ---------------------------------------------------------

    async def refresh_context(self, backend_id: str, *, execution_context=None) -> dict:
        """Refresh a backend's RF context by calling ONLY its discovered read-only tools."""
        from aithernet.rf.contracts import RFCallExecutionContext

        backend = self._require(backend_id)
        if backend.is_legacy:
            ctx = execution_context or RFCallExecutionContext()
            context = await self.runtime.gnuradio.refresh(
                mission_id=ctx.mission_id, mission_step_id=ctx.mission_step_id
            )
            await self._emit(ev.EVENT_CONTEXT_REFRESHED, backend_id, "refreshed")
            return context.compact()
        if not backend.enabled:
            raise RFBackendDisabledError(f"RF backend '{backend_id}' is disabled.")
        status = await backend.session.get_status()
        if status.state != MCPSessionState.READY:
            raise RFBackendNotReadyError(f"RF backend '{backend_id}' is not ready.")
        names = await self._live_tool_names(backend)
        ctx = execution_context or RFCallExecutionContext(caller="rf_context")
        ctx.caller = "rf_context"
        for tool in backend.adapter.refresh_tools(names):
            with contextlib.suppress(Exception):
                await self._execute_generic(backend, tool, {}, ctx)
        self.runtime.rf_context.apply_fields(
            backend_id, fields={"status": "ready"}, call_id=None, refreshed=True,
            backend_version=backend.session.server_info().get("version"),
            session_id=status.session_id if status.session_id != "-" else None,
            session_generation=status.generation,
        )
        await self._emit(ev.EVENT_CONTEXT_REFRESHED, backend_id, "refreshed")
        return self.runtime.rf_context.compact(backend_id)

    def context(self, backend_id: str) -> dict:
        """Return a backend's current RF context as a dict (legacy references GNU Radio)."""
        backend = self._require(backend_id)
        if backend.is_legacy:
            gr = self.runtime.gnuradio.current()
            view = gr.compact()
            view.update({
                "node_id": self.node_id,
                "backend_id": backend_id,
                "status": gr.flowgraph_status.value,
                "stale": gr.stale,
                "stale_reason": gr.stale_reason,
                "context_version": gr.context_version,
                "source_session_generation": gr.source_session_generation,
            })
            with self.runtime.session_scope() as session:
                from aithernet.state.repositories import RFArtifactRepository
                view["artifact_count"] = RFArtifactRepository(session).count(backend_id=backend_id)
            return view
        return self.runtime.rf_context.current(backend_id)

    def list_contexts(self) -> list[dict]:
        return [self.context(bid) for bid in self._backends]

    # -- helpers -----------------------------------------------------------------

    def _update_call(self, call_id: str, **fields) -> None:
        status = fields.pop("status", None)
        with self.runtime.session_scope() as session:
            MCPToolCallRepository(session).update(
                call_id,
                status=status.value if status is not None else None,
                completed_at=utcnow() if status in (
                    MCPToolCallStatus.COMPLETED, MCPToolCallStatus.FAILED
                ) else None,
                **fields,
            )
            session.commit()

    async def _emit(self, event_type, backend_id, message, *, mission_id=None, extra=None) -> None:
        payload = {"backend_id": backend_id}
        if extra:
            payload.update(extra)
        with contextlib.suppress(Exception):
            await self.runtime._emit_event(
                event_type=event_type, mission_id=mission_id, source="rf",
                message=f"[{backend_id}] {message}", payload=payload,
            )


def _ms(started) -> int:
    return int((utcnow() - started).total_seconds() * 1000)
