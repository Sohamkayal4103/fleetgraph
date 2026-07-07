"""Readiness / liveness state for a node (Stage 14A, area 5).

Liveness answers "is the process alive and responding?" — it never touches the database or any
worker, so a liveness probe stays cheap and always-true while the process runs. Readiness answers
"have the REQUIRED dependencies initialized?" — it reports success only after schema migrations
complete, repositories initialize, and the mission/transport workers and any REQUIRED RF backend
are available. An experimental, non-autostart backend (Marconi) failing degrades an OPTIONAL
component and never makes the node unready.

The tracker is a small in-memory state object the lifespan updates as it starts each managed
service; it holds no secrets and is safe to expose verbatim.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum


class Phase(str, Enum):
    STARTING = "starting"            # process alive, startup in progress
    MIGRATING = "migrating"          # schema migrations running
    READY = "ready"                  # all required components ready
    DEGRADED = "degraded"            # ready, but an optional dependency is degraded/failed
    SHUTTING_DOWN = "shutting_down"  # graceful stop in progress


class ComponentState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass
class Component:
    name: str
    required: bool
    state: ComponentState = ComponentState.PENDING
    detail: str | None = None


@dataclass
class ReadinessTracker:
    """Thread-safe readiness state. ``required`` components gate readiness; optional ones don't."""

    components: dict[str, Component] = field(default_factory=dict)
    _phase: Phase = Phase.STARTING
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, name: str, *, required: bool) -> None:
        with self._lock:
            self.components.setdefault(name, Component(name=name, required=required))

    def mark(self, name: str, state: ComponentState, *, detail: str | None = None) -> None:
        with self._lock:
            comp = self.components.get(name)
            if comp is None:
                comp = Component(name=name, required=False)
                self.components[name] = comp
            comp.state = state
            comp.detail = detail
            self._recompute_locked()

    def set_phase(self, phase: Phase) -> None:
        with self._lock:
            if phase in (Phase.SHUTTING_DOWN, Phase.MIGRATING):
                # Explicit transient phases are honored (sticky for shutdown, advisory otherwise).
                self._phase = phase
                return
            # STARTING/READY are advisory — the phase is DERIVED from component states.
            self._recompute_locked()

    def _recompute_locked(self) -> None:
        # Shutting down is terminal; never recompute back to a serving state.
        if self._phase is Phase.SHUTTING_DOWN:
            return
        required = [c for c in self.components.values() if c.required]
        not_ready = [
            c for c in required
            if c.state not in (ComponentState.READY, ComponentState.DISABLED)
        ]
        if not_ready:
            # A required component still pending (startup) or failed (degraded) is NOT ready.
            any_pending = any(c.state is ComponentState.PENDING for c in not_ready)
            self._phase = Phase.STARTING if any_pending else Phase.DEGRADED
            return
        optional_bad = any(
            c.state in (ComponentState.DEGRADED, ComponentState.FAILED)
            for c in self.components.values() if not c.required
        )
        self._phase = Phase.DEGRADED if optional_bad else Phase.READY

    # -- queries -----------------------------------------------------------------

    @property
    def phase(self) -> Phase:
        with self._lock:
            return self._phase

    def is_ready(self) -> bool:
        with self._lock:
            if self._phase in (Phase.STARTING, Phase.MIGRATING, Phase.SHUTTING_DOWN):
                return False
            return all(
                c.state in (ComponentState.READY, ComponentState.DISABLED)
                for c in self.components.values() if c.required
            )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self._phase.value,
                "ready": self.is_ready_locked(),
                "components": [
                    {"name": c.name, "required": c.required, "state": c.state.value,
                     "detail": c.detail}
                    for c in self.components.values()
                ],
            }

    def is_ready_locked(self) -> bool:
        if self._phase in (Phase.STARTING, Phase.MIGRATING, Phase.SHUTTING_DOWN):
            return False
        return all(
            c.state in (ComponentState.READY, ComponentState.DISABLED)
            for c in self.components.values() if c.required
        )
