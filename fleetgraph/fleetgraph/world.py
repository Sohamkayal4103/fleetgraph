"""World geometry — where every actor is at time t, and what each car's lidar can actually see.

Pure, deterministic, dependency-light. The two interesting pieces of "physics":
  - fog shrinks the lidar's effective range (optical extinction),
  - large actors (trucks) and obstacles OCCLUDE line of sight (segment vs. rectangle).
RF, by contrast, penetrates fog far better (handled in radio.py) — that asymmetry is the story.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from fleetgraph.scenario import Actor, Scenario, Vec2


def _path_length(path: list[Vec2]) -> float:
    return sum(math.dist(path[i], path[i + 1]) for i in range(len(path) - 1))


def pos_at_arclen(path: list[Vec2], travelled: float) -> Vec2 | None:
    """Point on a polyline at arc-length `travelled` (used by the stateful vehicle integrator)."""
    if not path:
        return None
    if len(path) == 1:
        return path[0]
    travelled = max(0.0, travelled)
    acc = 0.0
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        seg = math.dist(a, b)
        if acc + seg >= travelled:
            f = 0.0 if seg == 0 else (travelled - acc) / seg
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        acc += seg
    return path[-1]


def arclen_of_point(path: list[Vec2], px: float, py: float) -> tuple[float, float]:
    """Arc-length of the point on the polyline closest to (px,py), and its distance from the path.
    Lets a signal placed anywhere (incl. a grid intersection) map onto a car's turning path."""
    if len(path) < 2:
        d = math.dist(path[0], (px, py)) if path else 1e9
        return (0.0, d)
    best_d, best_s, acc = 1e18, 0.0, 0.0
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        seg = math.dist(a, b)
        if seg == 0:
            proj, s_on = a, 0.0
        else:
            tt = ((px - a[0]) * (b[0] - a[0]) + (py - a[1]) * (b[1] - a[1])) / (seg * seg)
            tt = max(0.0, min(1.0, tt))
            proj = (a[0] + tt * (b[0] - a[0]), a[1] + tt * (b[1] - a[1]))
            s_on = tt * seg
        d = math.dist(proj, (px, py))
        if d < best_d:
            best_d, best_s = d, acc + s_on
        acc += seg
    return (best_s, best_d)


def arclen_of_x(path: list[Vec2], x: float) -> float | None:
    """Arc-length at which a (monotonic-in-x) path reaches x-coordinate `x`, or None if x is not
    reached along the path. Used to convert a stop line at world-x into a travel cap."""
    if len(path) < 2:
        return None
    acc = 0.0
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        seg = math.dist(a, b)
        lo, hi = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
        if lo <= x <= hi:
            f = 0.0 if b[0] == a[0] else (x - a[0]) / (b[0] - a[0])
            return acc + f * seg
        acc += seg
    return None


def position_at(actor: Actor, t: float, brake_time: float | None = None) -> Vec2 | None:
    """Actor position at sim time t, or None if it has not spawned yet.

    If `brake_time` is given, the actor is frozen at the position it had reached at that time
    (it braked and stopped there) — so a car that brakes for a hazard actually halts and stays put
    instead of driving through it at constant speed.
    """
    if t < actor.spawn_t or not actor.path:
        return None
    if brake_time is not None and t > brake_time:
        t = brake_time
    if len(actor.path) == 1 or actor.speed_mps <= 0:
        return actor.path[0]
    travelled = actor.speed_mps * (t - actor.spawn_t)
    total = _path_length(actor.path)
    travelled = min(travelled, total)
    acc = 0.0
    for i in range(len(actor.path) - 1):
        a, b = actor.path[i], actor.path[i + 1]
        seg = math.dist(a, b)
        if acc + seg >= travelled:
            f = 0.0 if seg == 0 else (travelled - acc) / seg
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        acc += seg
    return actor.path[-1]


def heading_at(actor: Actor, t: float, dt: float = 0.1) -> float:
    """Actor heading (radians) from its motion, 0 if stationary."""
    p0 = position_at(actor, t)
    p1 = position_at(actor, t + dt) or p0
    if p0 is None or p1 is None or (p1[0] == p0[0] and p1[1] == p0[1]):
        return 0.0
    return math.atan2(p1[1] - p0[1], p1[0] - p0[0])


def _seg_intersects_rect(p: Vec2, q: Vec2, center: Vec2, size: Vec2) -> bool:
    """Does segment p->q pass through the axis-aligned rectangle at `center` with `size` (w,l)?"""
    hw, hl = size[0] / 2.0, size[1] / 2.0
    minx, maxx = center[0] - hw, center[0] + hw
    miny, maxy = center[1] - hl, center[1] + hl
    # Liang-Barsky segment/AABB clip.
    x0, y0 = p
    dx, dy = q[0] - x0, q[1] - y0
    t0, t1 = 0.0, 1.0
    for pp_, qq_ in ((-dx, x0 - minx), (dx, maxx - x0), (-dy, y0 - miny), (dy, maxy - y0)):
        if pp_ == 0:
            if qq_ < 0:
                return False
        else:
            r = qq_ / pp_
            if pp_ < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 > t1:
                return False
    return True


def effective_lidar_range(base_range: float, fog_density: float) -> float:
    """Fog optically extinguishes lidar: heavy fog collapses range to near zero."""
    return base_range * math.exp(-2.3 * max(0.0, min(1.0, fog_density)))


@dataclass
class Sighting:
    hazard_id: str
    visible: bool          # lidar can actually see it now
    blocked_reason: str | None  # "out_of_range" | "fog" | "occluded_by:<id>" | None
    distance_m: float
    ahead: bool            # roughly in the car's direction of travel (relevance for braking)


def lidar_sightings(scenario: Scenario, car: Actor, t: float, fog_density: float,
                    pos_fn) -> list[Sighting]:
    """For one car at time t, classify each hazard as visible or (should-see-but) blind.

    `pos_fn(actor) -> Vec2 | None` supplies the CURRENT position of any actor (so this uses the
    engine's stateful integrated positions, keeping lidar consistent with what's on screen).
    """
    out: list[Sighting] = []
    cpos = pos_fn(car)
    if cpos is None:
        return out
    chead = heading_at(car, t)
    eff_range = effective_lidar_range(scenario.world.lidar_range_m, fog_density)
    occluders = [a for a in scenario.actors
                 if a.kind in ("truck", "obstacle") and a.id != car.id]
    for hz in scenario.hazards():
        hpos = pos_fn(hz)
        if hpos is None:
            continue
        dist = math.dist(cpos, hpos)
        # "ahead" = within +/-70deg of heading and generally in front.
        ang = math.atan2(hpos[1] - cpos[1], hpos[0] - cpos[0])
        ahead = abs(math.atan2(math.sin(ang - chead), math.cos(ang - chead))) < math.radians(75)
        nominal_range = scenario.world.lidar_range_m
        reason: str | None = None
        visible = True
        if dist > nominal_range:
            visible, reason = False, "out_of_range"
        elif dist > eff_range:
            visible, reason = False, "fog"           # would be in range on a clear day, fog hides it
        else:
            for occ in occluders:
                opos = pos_fn(occ)
                if opos and _seg_intersects_rect(cpos, hpos, opos, occ.size):
                    visible, reason = False, f"occluded_by:{occ.id}"
                    break
        out.append(Sighting(hazard_id=hz.id, visible=visible, blocked_reason=reason,
                            distance_m=dist, ahead=ahead))
    return out
