"""Centralized mission lifecycle transition validation (Stage 12).

One validated transition function (no scattered status assignments). Terminal states are
never reopened except by an explicit new run. Invalid transitions are rejected clearly.
"""

from __future__ import annotations

from aithernet.schemas.missions import TERMINAL_MISSION_STATES, MissionStatus

S = MissionStatus

#: Allowed lifecycle transitions. Same machine for the mission and its execution run.
_ALLOWED: dict[MissionStatus, frozenset[MissionStatus]] = {
    S.RECEIVED: frozenset({S.QUEUED, S.CANCELLED}),
    S.QUEUED: frozenset({S.ACTIVE, S.PAUSED, S.CANCELLED}),
    S.ACTIVE: frozenset(
        {S.WAITING, S.PAUSED, S.COMPLETED, S.BLOCKED, S.FAILED, S.CANCELLED, S.QUEUED}
    ),
    S.WAITING: frozenset({S.QUEUED, S.PAUSED, S.CANCELLED}),
    S.PAUSED: frozenset({S.QUEUED, S.CANCELLED}),
    # Terminal states have no outgoing transitions (a new run is a new row, not a reopen).
    S.COMPLETED: frozenset(),
    S.BLOCKED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}


class MissionTransitionError(Exception):
    """Raised when an invalid mission/run lifecycle transition is attempted."""


def is_terminal(status: MissionStatus) -> bool:
    return status in TERMINAL_MISSION_STATES


def can_transition(current: MissionStatus, target: MissionStatus) -> bool:
    if current == target:
        return True  # idempotent no-op transitions are allowed
    return target in _ALLOWED.get(current, frozenset())


def validate_transition(current: MissionStatus, target: MissionStatus) -> None:
    """Raise :class:`MissionTransitionError` if ``current -> target`` is not allowed."""
    if not can_transition(current, target):
        raise MissionTransitionError(
            f"Invalid mission transition {current.value} -> {target.value}."
        )
