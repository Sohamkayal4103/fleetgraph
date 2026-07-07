"""The graph event stream — the one output the engine produces.

Every tick the engine emits a flat list of GraphEvents. The SAME stream drives three consumers:
  - the 2D world visualisation (left pane),
  - the Neo4j sync (right pane / the graph brain),
  - the demo narrative / audit.

Keeping events as small typed dicts (not ORM objects) means the engine has zero DB or web
dependency and stays trivially testable and replayable (record a run, replay it identically).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GraphEvent:
    type: str                 # e.g. "observed", "blind_to", "beaconed", "heard", "aware", "braked_for"
    t: float                  # simulation time (seconds)
    tick: int
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, "t": round(self.t, 3), "tick": self.tick, **self.data}


class EventLog:
    """Collects events for a run; also a convenience emitter the engine calls."""

    def __init__(self) -> None:
        self.events: list[GraphEvent] = []

    def emit(self, type: str, t: float, tick: int, **data) -> GraphEvent:
        ev = GraphEvent(type=type, t=t, tick=tick, data=data)
        self.events.append(ev)
        return ev

    def as_dicts(self) -> list[dict]:
        return [e.to_dict() for e in self.events]
