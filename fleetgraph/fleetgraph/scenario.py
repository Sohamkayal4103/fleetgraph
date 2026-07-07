"""FleetGraph scenario schema — a real multi-lane road you can author precisely.

A scenario is pure data (JSON) produced three ways and consumed by one engine:
  - typed in natural language      -> LLM (Butterbase gateway) -> Scenario JSON
  - built by placing cars/trucks/obstacles on lanes in the editor
  - loaded from a saved library entry

The world is a horizontal road made of LANES. Each lane has a y-centre and a direction (+1 travels
east / +x, -1 travels west / -x). Vehicles belong to a lane and move along it at their speed; they
can perform MANEUVERS (e.g. "change to the left lane after 3 s"). Pedestrians/obstacles use an
explicit path. `resolve_paths()` turns the lane + start_x + maneuvers of each vehicle into the
concrete polyline the motion engine follows, so the engine stays simple and everything animates.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from fleetgraph.grid import Grid, generate_grid_path, heading_for_point

Vec2 = tuple[float, float]

ActorKind = Literal["car", "truck", "pedestrian", "obstacle", "signal", "crosswalk"]
EventKind = Literal["fog_bank", "jammer_on", "jammer_off", "spawn"]
LaneKind = Literal["drive", "shoulder"]


class Lane(BaseModel):
    y: float
    direction: int = 1                 # +1 = eastbound (+x), -1 = westbound (-x)
    kind: LaneKind = "drive"
    label: str | None = None


class Maneuver(BaseModel):
    at_t: float                        # sim time to begin the maneuver
    kind: Literal["lane_change", "stop"] = "lane_change"
    to_lane: int | None = None         # target lane index for a lane_change
    over_m: float = 10.0               # metres taken to complete a lane change


class Actor(BaseModel):
    """A thing on the road. Cars/trucks drive in a lane and carry radios; pedestrians are hazards;
    obstacles are static occluders. Trucks are large enough to block line of sight."""

    id: str
    kind: ActorKind
    # Lane-based motion (straight-road world): start in `lane` at `start_x`.
    lane: int | None = None
    start_x: float | None = None
    # Grid motion (grid world): enter at (start_x, start_y) with a heading and a turn route.
    start_y: float | None = None
    heading: str | None = None                 # "E" | "W" | "N" | "S"
    route: list[str] = Field(default_factory=list)  # ["straight","right","left", ...]
    speed_mps: float = 0.0
    spawn_t: float = 0.0
    maneuvers: list[Maneuver] = Field(default_factory=list)
    # Explicit polyline — used by pedestrians/obstacles, and auto-filled for vehicles by resolve_paths.
    path: list[Vec2] = Field(default_factory=list)
    size: Vec2 = (4.5, 2.0)            # (length_along_x, width_along_y)
    is_hazard: bool = False
    has_radio: bool = True
    label: str | None = None
    # Traffic signal timing (kind == "signal"): green -> yellow -> red, repeating, with an offset.
    # The stop line is at this element's x; a car travelling toward it stops on red/yellow, goes on
    # green. `affects_dir` restricts which travel direction it controls (+1/-1/0=both).
    green_s: float = 15.0
    yellow_s: float = 2.0
    red_s: float = 5.0
    phase_offset_s: float = 0.0
    affects_dir: int = 0

    def default_size(self) -> Vec2:
        if self.kind == "truck":
            return (9.0, 2.6)
        if self.kind == "pedestrian":
            return (0.7, 0.7)
        if self.kind == "obstacle":
            return (3.0, 3.0)
        if self.kind == "signal":
            return (1.2, 1.2)
        return (4.5, 2.0)


def signal_phase(sig: "Actor", t: float) -> str:
    """Signal colour at time t: 'green' -> 'yellow' -> 'red', repeating."""
    cycle = max(0.5, sig.green_s + sig.yellow_s + sig.red_s)
    phase = (t + sig.phase_offset_s) % cycle
    if phase < sig.green_s:
        return "green"
    if phase < sig.green_s + sig.yellow_s:
        return "yellow"
    return "red"


def signal_requires_stop(sig: "Actor", t: float) -> bool:
    """A car must stop for this signal (red, or yellow — treat yellow as 'prepare to stop')."""
    return signal_phase(sig, t) in ("red", "yellow")


class TimedEvent(BaseModel):
    t: float
    kind: EventKind
    area: tuple[Vec2, Vec2] | None = None
    pos: Vec2 | None = None
    radius: float | None = None
    value: float | None = None
    actor: Actor | None = None


class World(BaseModel):
    width: float = 200.0
    height: float = 44.0
    lanes: list[Lane] = Field(default_factory=list)   # straight-road model (filled by ensure_lanes)
    lane_width: float = 5.0
    grid: Grid | None = None                          # if set, this is a 2D grid network instead
    fog_density: float = 0.0
    lidar_range_m: float = 60.0
    brake_reaction_m: float = 28.0
    awareness_horizon_m: float = 150.0


class Scenario(BaseModel):
    name: str
    description: str | None = None
    seed: int = 7
    duration_s: float = 20.0
    tick_hz: float = 5.0
    world: World = Field(default_factory=World)
    actors: list[Actor] = Field(default_factory=list)
    events: list[TimedEvent] = Field(default_factory=list)
    trust_pairs: list[tuple[str, str]] | None = None

    def ensure_lanes(self) -> "Scenario":
        """Fill a default 2-way road (2 shoulders + one drive lane each way) if none defined.
        Skipped for grid worlds, which define their own road geometry."""
        if self.world.grid is None and not self.world.lanes:
            self.world.lanes = default_lanes(self.world.height, self.world.lane_width)
        return self

    def cars(self) -> list[Actor]:
        return [a for a in self.actors if a.has_radio and a.kind in ("car", "truck")]

    def occluders(self) -> list[Actor]:
        return [a for a in self.actors if a.kind in ("truck", "obstacle")]

    def hazards(self) -> list[Actor]:
        return [a for a in self.actors if a.is_hazard]


def default_lanes(height: float, lane_width: float = 5.0) -> list[Lane]:
    """Top shoulder, westbound drive lane, eastbound drive lane, bottom shoulder."""
    c = height / 2.0
    return [
        Lane(y=c - 1.5 * lane_width, direction=-1, kind="shoulder", label="north shoulder"),
        Lane(y=c - 0.5 * lane_width, direction=-1, kind="drive", label="westbound"),
        Lane(y=c + 0.5 * lane_width, direction=1, kind="drive", label="eastbound"),
        Lane(y=c + 1.5 * lane_width, direction=1, kind="shoulder", label="south shoulder"),
    ]


def nearest_lane(world: World, y: float) -> int:
    """Index of the lane whose centre is closest to y (for snapping placed cars)."""
    return min(range(len(world.lanes)), key=lambda i: abs(world.lanes[i].y - y))


def resolve_paths(scenario: Scenario) -> Scenario:
    """Generate each vehicle's polyline. On a grid world, vehicles follow their heading + turn route
    through the network; on a straight-road world, they follow their lane + start_x + maneuvers."""
    scenario.ensure_lanes()

    if scenario.world.grid is not None:
        g = scenario.world.grid
        for a in scenario.actors:
            if a.kind not in ("car", "truck") or a.start_x is None or a.start_y is None:
                continue
            heading = a.heading or heading_for_point(g, a.start_x, a.start_y)
            a.heading = heading
            a.path = generate_grid_path(a.start_x, a.start_y, heading, a.route, g,
                                        scenario.world.width, scenario.world.height)
        return scenario

    lanes = scenario.world.lanes
    span = scenario.world.width + 60.0
    for a in scenario.actors:
        if a.kind not in ("car", "truck") or a.lane is None:
            continue
        lane = lanes[a.lane]
        d = lane.direction
        x0 = a.start_x if a.start_x is not None else (0.0 if d > 0 else scenario.world.width)
        if a.speed_mps <= 0 and not a.maneuvers:      # parked vehicle
            a.path = [(x0, lane.y)]
            continue
        wps: list[Vec2] = [(x0, lane.y)]
        cur_y = lane.y
        for m in sorted(a.maneuvers, key=lambda mm: mm.at_t):
            x_at = x0 + d * a.speed_mps * max(0.0, m.at_t - a.spawn_t)
            if m.kind == "lane_change" and m.to_lane is not None and 0 <= m.to_lane < len(lanes):
                ny = lanes[m.to_lane].y
                wps.append((x_at, cur_y))
                wps.append((x_at + d * m.over_m, ny))
                cur_y = ny
            elif m.kind == "stop":
                wps.append((x_at, cur_y))
                a.path = wps
                break
        else:
            wps.append((x0 + d * span, cur_y))
        a.path = wps
    return scenario


# ---------------------------------------------------------------------------
# Prebuilt scenarios (plain-English names). Lane indices: 0 north shoulder,
# 1 westbound, 2 eastbound, 3 south shoulder.
# ---------------------------------------------------------------------------

def example_fog_pedestrian() -> Scenario:
    """Heavy fog blinds the ambulance's lidar; a parked car near the crossing sees the pedestrian
    and beacons it, relayed by a car in the middle, giving the ambulance early warning."""
    s = Scenario(
        name="fog-occluded-pedestrian",
        description="Heavy fog blinds the ambulance's lidar. A parked car by the crossing sees the "
                    "pedestrian and radios it; a middle car relays the message so the ambulance is "
                    "warned early and stops in time.",
        duration_s=14.0,
        world=World(width=200, height=44, fog_density=0.7, lidar_range_m=55,
                    brake_reaction_m=30, awareness_horizon_m=150),
        actors=[
            Actor(id="car-A", kind="car", label="Ambulance", lane=2, start_x=10, speed_mps=12.0),
            # parked on the north shoulder near where the pedestrian starts — sees it early
            Actor(id="car-B", kind="car", label="Parked car (sees pedestrian)", lane=0, start_x=150,
                  speed_mps=0.0),
            # parked on the north shoulder mid-route — relays the message onward
            Actor(id="car-C", kind="car", label="Middle car (relay)", lane=0, start_x=88,
                  speed_mps=0.0),
            # an oncoming car in the westbound lane, for a live two-way road
            Actor(id="car-D", kind="car", label="Oncoming car", lane=1, start_x=195, speed_mps=10.0),
            # a parked box truck (passive occluder, no radio)
            Actor(id="truck-T", kind="truck", label="Parked truck", lane=3, start_x=126,
                  speed_mps=0.0, has_radio=False),
            # the pedestrian steps off the north side and crosses the road at x=140
            Actor(id="ped-1", kind="pedestrian", label="Pedestrian", is_hazard=True, has_radio=False,
                  path=[(140, 8), (140, 36)], speed_mps=2.2, spawn_t=2.0),
        ],
    )
    return resolve_paths(s)


def example_jammer_attack() -> Scenario:
    """Same fog scene, but a jammer blankets the corridor and silences the mesh — the ambulance
    gets no radio warning and only its own lidar catches the pedestrian at the last second."""
    s = example_fog_pedestrian()
    s.name = "jammer-attack"
    s.description = ("A jammer blankets the corridor and silences the V2V mesh — the ambulance gets "
                     "NO radio warning and only its own lidar catches the pedestrian at the last "
                     "second (vs an early warning with the mesh).")
    s.events = [TimedEvent(t=0.0, kind="jammer_on", pos=(100, 22), radius=95, value=0.9)]
    return resolve_paths(s)


def example_highway_chain() -> Scenario:
    """A stalled car in fog; a column of cars in the eastbound lane passes the warning back to the
    ambulance at the rear via a multi-hop relay."""
    s = Scenario(
        name="highway-stalled-car",
        description="A stalled car in fog ahead. A column of cars relays the warning backward to "
                    "the ambulance at the rear, so it slows early.",
        duration_s=16.0,
        world=World(width=240, height=44, fog_density=0.72, lidar_range_m=55,
                    brake_reaction_m=30, awareness_horizon_m=200),
        actors=[
            # ambulance in the eastbound drive lane — clear lane until the stalled car
            Actor(id="car-A", kind="car", label="Ambulance (rear)", lane=2, start_x=10, speed_mps=12.0),
            # relay column parked on the shoulder (so the ambulance's lane stays clear)
            Actor(id="car-C1", kind="car", label="Car 1 (relay)", lane=3, start_x=72, speed_mps=0.0),
            Actor(id="car-C2", kind="car", label="Car 2 (relay)", lane=3, start_x=135, speed_mps=0.0),
            Actor(id="car-B", kind="car", label="Lead car (sees stall)", lane=3, start_x=194,
                  speed_mps=0.0),
            # the stalled car blocks the eastbound drive lane ahead
            Actor(id="stall-1", kind="pedestrian", label="Stalled car", is_hazard=True,
                  has_radio=False, path=[(200, 24.5)], speed_mps=0.0, spawn_t=3.0, size=(4.5, 2.0)),
        ],
    )
    return resolve_paths(s)


# ---------------------------------------------------------------------------
# Grid road-network layouts (2D city grids with intersections + turning routes).
# ---------------------------------------------------------------------------

def _grid_car(id, label, x, y, heading, route, speed=10.0):
    return Actor(id=id, kind="car", label=label, start_x=x, start_y=y, heading=heading,
                 route=route, speed_mps=speed, has_radio=True, size=(4.5, 2.0))


def example_grid_intersection() -> Scenario:
    """A single 4-way intersection, 2 lanes each way — cars go straight and turn; a pedestrian
    crosses in fog so the mesh still matters."""
    grid = Grid(h_roads=[70.0], v_roads=[110.0], lanes_each_way=2, lane_width=5.0)
    return Scenario(
        name="grid-intersection",
        description="A 4-way intersection. Cars follow turn routes (straight / left / right); heavy "
                    "fog means blinded cars rely on the V2V mesh.",
        duration_s=20.0, world=World(width=220, height=140, grid=grid, fog_density=0.45,
                                     lidar_range_m=55, brake_reaction_m=28, awareness_horizon_m=150),
        actors=[
            _grid_car("car-A", "Ambulance", 0, 70, "E", ["straight"], 11),
            _grid_car("car-B", "Car B", 110, 0, "S", ["left"], 9),
            _grid_car("car-C", "Car C", 220, 70, "W", ["right"], 9),
            _grid_car("car-D", "Car D", 110, 140, "N", ["straight"], 8),
            Actor(id="ped-1", kind="pedestrian", label="Pedestrian", is_hazard=True, has_radio=False,
                  path=[(150, 55), (150, 85)], speed_mps=1.6, spawn_t=3.0),
        ],
    )


def example_grid_2x2() -> Scenario:
    """A 2x2 grid (4 intersections) — richer turning + a longer relay chain."""
    grid = Grid(h_roads=[50.0, 120.0], v_roads=[80.0, 180.0], lanes_each_way=2, lane_width=5.0)
    return Scenario(
        name="grid-2x2",
        description="A 2x2 city grid. Multiple cars take different routes through four intersections "
                    "in fog; the graph shows who warned whom.",
        duration_s=24.0, world=World(width=260, height=170, grid=grid, fog_density=0.5,
                                     lidar_range_m=55, brake_reaction_m=28, awareness_horizon_m=160),
        actors=[
            _grid_car("car-A", "Ambulance", 0, 50, "E", ["straight", "right"], 11),
            _grid_car("car-B", "Car B", 80, 0, "S", ["left", "straight"], 9),
            _grid_car("car-C", "Car C", 260, 120, "W", ["right", "left"], 9),
            _grid_car("car-D", "Car D", 0, 120, "E", ["straight", "straight"], 10),
            Actor(id="ped-1", kind="pedestrian", label="Pedestrian", is_hazard=True, has_radio=False,
                  path=[(220, 105), (220, 135)], speed_mps=1.6, spawn_t=4.0),
        ],
    )


def example_grid_downtown() -> Scenario:
    """A 3x2 downtown grid, 3 lanes each way — a busy layout for a complex graph."""
    grid = Grid(h_roads=[45.0, 110.0, 175.0], v_roads=[90.0, 200.0], lanes_each_way=3, lane_width=4.5)
    return Scenario(
        name="grid-downtown",
        description="A multi-lane downtown grid — many cars, many routes, one messy connected graph.",
        duration_s=26.0, world=World(width=300, height=220, grid=grid, fog_density=0.5,
                                     lidar_range_m=55, brake_reaction_m=28, awareness_horizon_m=170),
        actors=[
            _grid_car("car-A", "Ambulance", 0, 45, "E", ["straight", "right"], 11),
            _grid_car("car-B", "Car B", 90, 0, "S", ["left"], 9),
            _grid_car("car-C", "Car C", 300, 110, "W", ["straight", "left"], 9),
            _grid_car("car-D", "Car D", 200, 220, "N", ["right", "straight"], 8),
            _grid_car("car-E", "Car E", 0, 175, "E", ["right"], 10),
            Actor(id="ped-1", kind="pedestrian", label="Pedestrian", is_hazard=True, has_radio=False,
                  path=[(120, 95), (120, 125)], speed_mps=1.6, spawn_t=4.0),
        ],
    )


def example_blank() -> Scenario:
    """An empty road — drag vehicles onto the lanes to build your own scenario."""
    s = Scenario(
        name="blank-road",
        description="An empty road. Drag cars/trucks/obstacles/a pedestrian onto the lanes (or click "
                    "a tool then click a lane) to build your own scenario, then Run.",
        duration_s=16.0,
        world=World(width=200, height=44, fog_density=0.3, lidar_range_m=55,
                    brake_reaction_m=30, awareness_horizon_m=150),
        actors=[],
        events=[],
    )
    return resolve_paths(s)


SCENARIOS: dict[str, tuple[str, callable]] = {
    "blank-road": ("Blank road — build your own", example_blank),
    "fog-occluded-pedestrian": ("Fog + relay (the hero case)", example_fog_pedestrian),
    "jammer-attack": ("Jammer silences the mesh (failure case)", example_jammer_attack),
    "highway-stalled-car": ("Highway stalled car (3-hop relay)", example_highway_chain),
    "grid-intersection": ("Grid: single 4-way intersection", example_grid_intersection),
    "grid-2x2": ("Grid: 2×2 city grid", example_grid_2x2),
    "grid-downtown": ("Grid: multi-lane downtown", example_grid_downtown),
}


def scenario_catalog() -> list[dict]:
    return [{"id": k, "name": v[0]} for k, v in SCENARIOS.items()]


def get_scenario(scenario_id: str) -> Scenario:
    return SCENARIOS[scenario_id][1]()


def load_scenario(data: dict) -> Scenario:
    """Validate a raw dict (JSON / LLM / editor) into a Scenario, fill lane defaults + footprints +
    generated vehicle paths."""
    scenario = Scenario.model_validate(data)
    scenario.ensure_lanes()
    raw_actors = {a.get("id"): a for a in data.get("actors", []) if isinstance(a, dict)}
    for a in scenario.actors:
        if "size" not in raw_actors.get(a.id, {}):
            a.size = a.default_size()
    return resolve_paths(scenario)
