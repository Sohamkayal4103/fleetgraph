"""Phase 3+: deterministic peer-transport selection.

A sender may request ``auto``, ``ip``, ``rf_ota``, or ``simulated_rf``. ``auto`` chooses
deterministically from the configured + healthy transports by a fixed preference policy. RF is never
silently downgraded to IP unless fallback is explicitly allowed by the mission/node policy. Every
decision (requested, selected, reason, fallback policy + occurrence, profile) is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TRANSPORT_AUTO = "auto"
TRANSPORT_IP = "ip"
TRANSPORT_RF_OTA = "rf_ota"
TRANSPORT_SIMULATED_RF = "simulated_rf"

VALID_REQUESTS = (TRANSPORT_AUTO, TRANSPORT_IP, TRANSPORT_RF_OTA, TRANSPORT_SIMULATED_RF)

#: Deterministic auto-preference order (IP first: lowest latency/most available by default).
_AUTO_ORDER = (TRANSPORT_IP, TRANSPORT_SIMULATED_RF, TRANSPORT_RF_OTA)


@dataclass(frozen=True)
class SelectionResult:
    requested: str
    selected: str | None
    reason: str
    fallback_allowed: bool
    fallback_occurred: bool = False
    profile_id: str | None = None
    candidates: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class TransportSelectionError(ValueError):
    """Raised when no transport can satisfy the request under the policy."""


def select_transport(*, requested: str, available: dict, fallback_allowed: bool,
                     profile_id: str | None = None) -> SelectionResult:
    """Choose a transport. ``available`` maps transport_id -> healthy(bool).

    Rules: an explicit request must be available + healthy (no silent fallback unless allowed);
    ``auto`` walks the deterministic preference order over healthy transports. RF is never silently
    downgraded to IP unless ``fallback_allowed``."""
    if requested not in VALID_REQUESTS:
        raise TransportSelectionError(f"unknown transport {requested!r}")
    healthy = [t for t, ok in available.items() if ok]

    if requested == TRANSPORT_AUTO:
        for t in _AUTO_ORDER:
            if t in healthy:
                return SelectionResult(requested, t, f"auto-selected first healthy: {t}",
                                       fallback_allowed, profile_id=profile_id, candidates=healthy)
        raise TransportSelectionError("no healthy transport for auto selection")

    # explicit request
    if available.get(requested):
        return SelectionResult(requested, requested, "explicit request satisfied",
                               fallback_allowed, profile_id=profile_id, candidates=healthy)
    # requested transport unavailable/unhealthy
    if not fallback_allowed:
        raise TransportSelectionError(
            f"requested transport {requested!r} unavailable and fallback not allowed "
            "(RF is never silently downgraded to IP)")
    for t in _AUTO_ORDER:
        if t in healthy:
            return SelectionResult(requested, t,
                                   f"explicit {requested} unavailable; explicit-fallback to {t}",
                                   fallback_allowed, fallback_occurred=True, profile_id=profile_id,
                                   candidates=healthy)
    raise TransportSelectionError("no healthy transport available even with fallback")
