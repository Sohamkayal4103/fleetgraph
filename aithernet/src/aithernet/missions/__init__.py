"""Autonomous mission execution (Stage 12).

The autonomous worker repeatedly executes *atomic* mission steps — one coordinator decision
and at most one external route per iteration — until the coordinator chooses a lifecycle
outcome (``complete``/``wait``/``blocked``) or infrastructure stops it (budget, cancel,
pause, lease loss). This is NOT a predetermined workflow: the coordinator selects one
action after observing the latest state and result. One atomic step is not one whole
mission; the loop reassesses after every step.
"""

from __future__ import annotations

from aithernet.missions.lifecycle import (
    MissionTransitionError,
    is_terminal,
    validate_transition,
)

__all__ = ["MissionTransitionError", "is_terminal", "validate_transition"]
