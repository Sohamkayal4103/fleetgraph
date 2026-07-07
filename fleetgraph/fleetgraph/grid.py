"""Grid road network — a 2D city grid of horizontal + vertical roads with intersections.

Vehicles enter with a heading and a ROUTE (a sequence of turns: straight / left / right). This module
turns that into a concrete polyline through the grid, which the existing motion integrator drives
(so accel/decel, signals and stop-and-go all still apply). This keeps the engine unchanged: a grid
scenario is just one whose vehicles have grid-generated paths.

Conventions: screen x → right, y → down. Headings E(+x) W(-x) S(+y) N(-y). Right-hand traffic:
  horizontal road at y0: eastbound lanes on the south (y0 + (i+.5)w), westbound on the north.
  vertical road at x0:   southbound lanes on the west (x0 - (i+.5)w), northbound on the east.
"""

from __future__ import annotations

from pydantic import BaseModel

Vec2 = tuple[float, float]
Heading = str  # "E" | "W" | "N" | "S"

_DELTA = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}
# right turn (clockwise) and left turn (counter-clockwise) heading transitions
_RIGHT = {"E": "S", "S": "W", "W": "N", "N": "E"}
_LEFT = {"E": "N", "N": "W", "W": "S", "S": "E"}


class Grid(BaseModel):
    h_roads: list[float]              # y-centres of horizontal roads
    v_roads: list[float]              # x-centres of vertical roads
    lanes_each_way: int = 1
    lane_width: float = 4.0


def _nearest(vals: list[float], v: float) -> float:
    return min(vals, key=lambda a: abs(a - v))


def lane_coord(road_pos: float, heading: Heading, lane: int, w: float) -> float:
    """The fixed cross-axis coordinate of a lane (y for E/W, x for N/S)."""
    off = (lane + 0.5) * w
    if heading == "E":
        return road_pos + off      # south lanes
    if heading == "W":
        return road_pos - off      # north lanes
    if heading == "S":
        return road_pos - off      # west lanes
    return road_pos + off          # N: east lanes


def turn(heading: Heading, t: str) -> Heading:
    if t == "right":
        return _RIGHT[heading]
    if t == "left":
        return _LEFT[heading]
    return heading


def heading_for_point(grid: Grid, x: float, y: float) -> Heading:
    """Guess a sensible entry heading for a point dropped on the grid: whichever road (h or v) is
    closer, and which side of it the point is on (south half of an h-road → eastbound, etc.)."""
    dh = min((abs(y - hy), "h", hy) for hy in grid.h_roads) if grid.h_roads else (1e9, "h", 0)
    dv = min((abs(x - vx), "v", vx) for vx in grid.v_roads) if grid.v_roads else (1e9, "v", 0)
    if dh[0] <= dv[0]:
        return "E" if y >= dh[2] else "W"
    return "S" if x <= dv[2] else "N"


def generate_grid_path(sx: float, sy: float, heading: Heading, route: list[str],
                       grid: Grid, width: float, height: float, lane: int = 0) -> list[Vec2]:
    """Polyline from an entry point + heading, applying `route` turns at successive intersections
    until the vehicle leaves the grid (defaults to going straight when the route is exhausted)."""
    w = grid.lane_width
    margin = 30.0
    # snap onto the road implied by the heading, at the chosen lane
    if heading in ("E", "W"):
        y0 = _nearest(grid.h_roads, sy) if grid.h_roads else sy
        cur = (sx, lane_coord(y0, heading, lane, w))
    else:
        x0 = _nearest(grid.v_roads, sx) if grid.v_roads else sx
        cur = (lane_coord(x0, heading, lane, w), sy)
    wps: list[Vec2] = [cur]
    ri = 0
    for _ in range(60):
        dx, dy = _DELTA[heading]
        if heading in ("E", "W"):
            crossing = sorted((vx for vx in grid.v_roads if (vx - cur[0]) * dx > 1.0),
                              key=lambda vx: (vx - cur[0]) * dx)
        else:
            crossing = sorted((hy for hy in grid.h_roads if (hy - cur[1]) * dy > 1.0),
                              key=lambda hy: (hy - cur[1]) * dy)
        if not crossing:
            # exit to the boundary
            if heading == "E":
                wps.append((width + margin, cur[1]))
            elif heading == "W":
                wps.append((-margin, cur[1]))
            elif heading == "S":
                wps.append((cur[0], height + margin))
            else:
                wps.append((cur[0], -margin))
            break
        nxt = crossing[0]
        t = route[ri] if ri < len(route) else "straight"
        ri += 1
        if t == "straight":
            # keep going; just step past this intersection and continue
            if heading in ("E", "W"):
                cur = (nxt + dx * 0.5, cur[1])
            else:
                cur = (cur[0], nxt + dy * 0.5)
            continue
        nh = turn(heading, t)
        if heading in ("E", "W"):
            corner = (lane_coord(nxt, nh, lane, w), cur[1])   # turn onto vertical road at x=nxt
        else:
            corner = (cur[0], lane_coord(nxt, nh, lane, w))   # turn onto horizontal road at y=nxt
        wps.append(corner)
        cur = corner
        heading = nh
    return wps
