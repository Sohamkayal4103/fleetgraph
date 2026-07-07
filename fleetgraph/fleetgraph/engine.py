"""The FleetGraph simulation engine.

Per tick it: places actors, runs each car's lidar (fog + occlusion), detects blind spots, has every
observer beacon its sightings over the REAL radio mesh, propagates awareness through multi-hop
relays (BFS over the links that actually delivered), and decides braking. Every semantic transition
becomes a GraphEvent. It also records a lightweight per-tick `frame` (positions, active links) for
the 2D viewer. No DB, no web, no LLM here — just world -> radio -> graph facts.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from fleetgraph.events import EventLog
from fleetgraph.radio import (
    LinkConditions,
    RadioMesh,
    make_car_identity,
    predict_delivery,
    snr_db_for,
)
from fleetgraph.scenario import (
    Actor,
    Scenario,
    resolve_paths,
    signal_phase,
    signal_requires_stop,
)
from fleetgraph.world import (
    _path_length,
    arclen_of_point,
    arclen_of_x,
    lidar_sightings,
    pos_at_arclen,
    position_at,
)


@dataclass
class RunResult:
    scenario_name: str
    events: list[dict]
    frames: list[dict]
    summary: dict = field(default_factory=dict)


class _State:
    def __init__(self) -> None:
        self.observed: dict[str, set[str]] = {}       # car -> hazards seen by its own lidar
        self.aware: dict[str, set[str]] = {}          # car -> hazards it knows (lidar or radio)
        self.aware_via_radio: dict[str, set[str]] = {}
        self.braked: dict[str, set[str]] = {}
        self.blind_emitted: set[tuple[str, str]] = set()
        self.heard_hops: set[tuple[str, str, str]] = set()   # (sender, receiver, hazard) — one fact
        self.relayed_edges: set[tuple[str, str, str, str]] = set()  # (relay, source, dest, hazard)
        self.trusted_emitted = False


class SimEngine:
    def __init__(self, scenario: Scenario) -> None:
        resolve_paths(scenario)          # ensure lanes + generated vehicle polylines are in place
        self.s = scenario
        self.log = EventLog()
        self.mesh = RadioMesh(profile_id="ref-bpsk-1k")
        for car in scenario.cars():
            self.mesh.add_car(make_car_identity(car.id, car.label or car.id))
        self.st = _State()
        self._frames: list[dict] = []
        # Stateful vehicle motion: arc-length travelled along each vehicle's path, integrated per
        # tick. `perm_stop` = braked for a hazard (stays stopped); `waiting` = held at a red signal
        # this tick (resumes on green).
        self.veh_travel: dict[str, float] = {}
        self.veh_speed: dict[str, float] = {}          # current speed (for gradual accel/decel)
        self.perm_stop: set[str] = set()
        self.stop_target: dict[str, float] = {}        # arc-length a hazard-braking car eases to
        self.waiting: set[str] = set()
        for a in scenario.actors:
            if a.kind in ("car", "truck"):
                self.veh_travel[a.id] = 0.0
                self.veh_speed[a.id] = a.speed_mps      # start at cruising speed
        for car in scenario.cars():
            self.st.observed[car.id] = set()
            self.st.aware[car.id] = set()
            self.st.aware_via_radio[car.id] = set()
            self.st.braked[car.id] = set()

    # -- environment ---------------------------------------------------------
    def _fog_at(self, t: float) -> float:
        fog = self.s.world.fog_density
        for ev in self.s.events:
            if ev.kind == "fog_bank" and ev.t <= t and ev.value is not None:
                fog = max(fog, ev.value)
        return min(1.0, fog)

    def _active_jammers(self, t: float) -> list[tuple[tuple[float, float], float, float]]:
        """Return [(pos, radius, strength)] for jammers on at time t."""
        active: dict[int, tuple] = {}
        for i, ev in enumerate(self.s.events):
            if ev.kind == "jammer_on" and ev.t <= t and ev.pos is not None:
                active[i] = (ev.pos, ev.radius or 20.0, ev.value or 0.7)
            if ev.kind == "jammer_off" and ev.t <= t and ev.pos is not None:
                for k, (p, _, _) in list(active.items()):
                    if math.dist(p, ev.pos) < 2.0:
                        active.pop(k, None)
        return list(active.values())

    def _interference_at(self, pos: tuple[float, float], jammers) -> float:
        best = 0.0
        for jpos, radius, strength in jammers:
            d = math.dist(pos, jpos)
            if d <= radius:
                best = max(best, strength * (1.0 - d / radius))
        return best

    # -- positions -----------------------------------------------------------
    def _pos(self, actor: Actor, t: float):
        """Current position. Vehicles use the stateful integrator (so they stop-and-go for signals
        and stay stopped for hazards); pedestrians/obstacles/signals use their static/closed path."""
        if actor.kind in ("car", "truck"):
            return pos_at_arclen(actor.path, self.veh_travel.get(actor.id, 0.0))
        return position_at(actor, t)

    def _veh_dir(self, v: Actor) -> int:
        lanes = self.s.world.lanes
        if v.lane is not None and 0 <= v.lane < len(lanes):
            return lanes[v.lane].direction
        if len(v.path) >= 2:
            return 1 if v.path[-1][0] >= v.path[0][0] else -1
        return 1

    def _signal_pos(self, sig: Actor) -> tuple[float | None, float]:
        """A signal's (x, y): start_x/start_y (grid), or start_x + lane-y / path (straight road)."""
        lanes = self.s.world.lanes
        sx = sig.start_x if sig.start_x is not None else (sig.path[0][0] if sig.path else None)
        if sig.start_y is not None:
            sy = sig.start_y
        elif sig.lane is not None and 0 <= sig.lane < len(lanes):
            sy = lanes[sig.lane].y
        elif sig.path:
            sy = sig.path[0][1]
        else:
            sy = 0.0
        return sx, sy

    ACCEL = 2.5     # m/s^2 pull-away
    DECEL = 3.5     # m/s^2 comfortable braking

    def _advance_vehicles(self, t: float, dt: float) -> None:
        """Integrate each vehicle forward one tick with gradual accel/decel, honouring a permanent
        hazard-stop target and red/yellow signals (stops before them, resumes on green)."""
        signals = [a for a in self.s.actors if a.kind == "signal"]
        self.waiting = set()
        for v in self.s.actors:
            if v.kind not in ("car", "truck"):
                continue
            base = v.speed_mps
            if base <= 0 and not v.maneuvers:
                continue  # parked
            total = _path_length(v.path)
            cur = self.veh_travel.get(v.id, 0.0)
            speed = self.veh_speed.get(v.id, base)
            vdir = self._veh_dir(v)
            gap = 3.0 + v.size[0] / 2.0

            # nearest arc-length the car must not pass (a "stop line")
            cap = total
            signal_hold = False
            if v.id in self.perm_stop:
                cap = min(cap, self.stop_target.get(v.id, cur))
            else:
                is_grid = self.s.world.grid is not None
                on_path_tol = (self.s.world.grid.lane_width * 2.2) if is_grid else 6.0
                for sig in signals:
                    if not signal_requires_stop(sig, t):
                        continue
                    if not is_grid and sig.affects_dir != 0 and sig.affects_dir != vdir:
                        continue
                    sx, sy = self._signal_pos(sig)
                    if sx is None:
                        continue
                    # where the car's (possibly turning) path reaches the light, if it's on the route
                    at, dist_from = arclen_of_point(v.path, sx, sy)
                    if dist_from > on_path_tol:
                        continue
                    stop_travel = at - gap
                    if cur <= stop_travel + 0.5:
                        cap = min(cap, stop_travel)
                        signal_hold = True

            # gradual accel/decel: cap speed so the car can still stop by `cap`, then ramp toward it
            dist_to_stop = max(0.0, cap - cur)
            v_limit = math.sqrt(2.0 * self.DECEL * dist_to_stop) if dist_to_stop < 1e6 else base
            target = min(base, v_limit)
            if target > speed:
                speed = min(target, speed + self.ACCEL * dt)
            else:
                speed = max(target, speed - self.DECEL * dt)
            speed = max(0.0, speed)

            nxt = min(cur + speed * dt, cap, total)
            self.veh_travel[v.id] = nxt
            self.veh_speed[v.id] = speed
            if signal_hold and speed < 0.4:
                self.waiting.add(v.id)

    # -- radio conditions ----------------------------------------------------

    # -- radio conditions ----------------------------------------------------
    def _snr(self, u: Actor, v: Actor, t: float, fog: float, jammers) -> float | None:
        pu, pv = self._pos(u, t), self._pos(v, t)
        if pu is None or pv is None:
            return None
        cond = LinkConditions(distance_m=math.dist(pu, pv), fog_density=fog,
                              interference=self._interference_at(pv, jammers))
        return snr_db_for(cond)

    def _deliver(self, u: Actor, v: Actor, t: float, fog: float, jammers, seed: int) -> tuple[bool, float]:
        """CHEAP reachability: SNR from geometry -> measured delivery cliff. O(1), no modem."""
        snr = self._snr(u, v, t, fog, jammers)
        if snr is None:
            return (False, -99.0)
        return (predict_delivery(snr, seed=seed), snr)

    # -- main loop -----------------------------------------------------------
    def run(self) -> RunResult:
        cars = self.s.cars()
        dt = 1.0 / self.s.tick_hz
        n_ticks = int(self.s.duration_s * self.s.tick_hz) + 1

        if not self.st.trusted_emitted:
            pairs = self.s.trust_pairs or [(a.id, b.id) for i, a in enumerate(cars) for b in cars[i + 1:]]
            for a, b in pairs:
                self.log.emit("trusts", 0.0, 0, a=a, b=b)
            self.st.trusted_emitted = True

        for tick in range(n_ticks):
            t = tick * dt
            fog = self._fog_at(t)
            jammers = self._active_jammers(t)
            frame = self._frame_skeleton(t, tick, fog, jammers)

            # 1) lidar: observed + blind_to
            # `relevant` = hazards ahead within the (wide) awareness horizon -> candidates for the
            # mesh to warn about. `concern` = the (near) subset within braking distance.
            relevant: dict[str, dict[str, float]] = {c.id: {} for c in cars}
            concern: dict[str, dict[str, float]] = {c.id: {} for c in cars}
            pos_fn = lambda a: self._pos(a, t)  # noqa: E731 — current integrated positions
            for car in cars:
                for s in lidar_sightings(self.s, car, t, fog, pos_fn):
                    if s.ahead and s.distance_m <= self.s.world.awareness_horizon_m:
                        relevant[car.id][s.hazard_id] = s.distance_m
                    if s.ahead and s.distance_m <= self.s.world.brake_reaction_m:
                        concern[car.id][s.hazard_id] = s.distance_m
                    if s.visible:
                        if s.hazard_id not in self.st.observed[car.id]:
                            self.st.observed[car.id].add(s.hazard_id)
                            newly_aware = s.hazard_id not in self.st.aware[car.id]
                            self.st.aware[car.id].add(s.hazard_id)
                            self.log.emit("observed", t, tick, car=car.id, hazard=s.hazard_id,
                                          distance=round(s.distance_m, 1))
                            if newly_aware:  # observing implies awareness (via lidar)
                                self.log.emit("aware", t, tick, car=car.id, hazard=s.hazard_id,
                                              via="lidar", path=[car.id], hops=0)
                    elif s.ahead and s.blocked_reason and s.distance_m <= self.s.world.lidar_range_m:
                        if (car.id, s.hazard_id) not in self.st.blind_emitted:
                            self.st.blind_emitted.add((car.id, s.hazard_id))
                            self.log.emit("blind_to", t, tick, car=car.id, hazard=s.hazard_id,
                                          reason=s.blocked_reason, distance=round(s.distance_m, 1))

            # 2) build the delivery graph over radio cars (who can hear whom right now)
            adj: dict[str, list[str]] = {c.id: [] for c in cars}
            for u in cars:
                for v in cars:
                    if u.id == v.id:
                        continue
                    delivered, snr = self._deliver(u, v, t, fog, jammers, seed=self.s.seed + tick)
                    if delivered:
                        adj[u.id].append(v.id)
                    frame["links"].append({"from": u.id, "to": v.id,
                                           "snr": round(snr, 1), "delivered": delivered})

            # 3) propagate awareness: for each car with a relevant+unknown hazard ahead, find a
            #    delivery path from an observer (direct or multi-hop relay) through the live mesh.
            for car in cars:
                for hz, dist in relevant[car.id].items():
                    if hz in self.st.aware[car.id]:
                        continue  # already knows (saw it, or heard earlier)
                    observers = [o.id for o in cars if hz in self.st.observed[o.id] and o.id != car.id]
                    path = self._bfs_path(adj, observers, car.id)
                    if path:
                        self.st.aware[car.id].add(hz)
                        self.st.aware_via_radio[car.id].add(hz)
                        via = "direct" if len(path) == 2 else "relay"
                        # emit the propagation chain. Each hop is carried by the REAL modem once
                        # (cached) so the graph's beacon edges carry genuine frame/retry/signature
                        # facts, not just the reachability estimate.
                        for i in range(len(path) - 1):
                            hop_key = (path[i], path[i + 1], hz)
                            if hop_key in self.st.heard_hops:
                                continue  # this physical hop is one fact — don't re-emit per receiver
                            self.st.heard_hops.add(hop_key)
                            snr = self._snr(self._actor(path[i]), self._actor(path[i + 1]),
                                            t, fog, jammers) or -99.0
                            is_relay_hop = i > 0            # first hop is from the observer
                            payload = {"type": "state_beacon", "from": path[i],
                                       "hazards": [{"id": hz, "source": path[0]}]}
                            real = self.mesh.send_beacon_real_cached(
                                path[i], path[i + 1], payload, snr_db=snr, seed=self.s.seed)
                            self.log.emit("heard", t, tick, receiver=path[i + 1], sender=path[i],
                                          hazard=hz, snr=round(snr, 1),
                                          hop=("relay" if is_relay_hop else "direct"),
                                          frames=real.frames_sent, retries=real.retries,
                                          signature_verified=real.signature_verified,
                                          real_modem=True)
                        for mid in path[1:-1]:
                            rk = (mid, path[0], path[-1], hz)
                            if rk in self.st.relayed_edges:
                                continue
                            self.st.relayed_edges.add(rk)
                            self.log.emit("relayed_by", t, tick, relay=mid, hazard=hz,
                                          source=path[0], dest=path[-1])
                        self.log.emit("aware", t, tick, car=car.id, hazard=hz, via=via,
                                      path=path, hops=len(path) - 1)

            # 4) braking / unaware risk
            for car in cars:
                for hz, dist in concern[car.id].items():
                    if hz in self.st.aware[car.id]:
                        if hz not in self.st.braked[car.id]:
                            self.st.braked[car.id].add(hz)
                            self.perm_stop.add(car.id)   # commits to stop for the hazard
                            # ease to a stop a safe gap before the hazard (gradual decel handles it)
                            hpos = self._pos(self._actor(hz), t)
                            vdir = self._veh_dir(car)
                            tgt = arclen_of_x(car.path, hpos[0] - vdir * 6.0) if hpos else None
                            self.stop_target[car.id] = max(self.veh_travel.get(car.id, 0.0),
                                                           tgt if tgt is not None else 0.0)
                            src = "lidar" if hz in self.st.observed[car.id] else "radio"
                            self.log.emit("braked_for", t, tick, car=car.id, hazard=hz,
                                          distance=round(dist, 1), source=src)
                    elif dist < 10.0:
                        self.log.emit("unaware_risk", t, tick, car=car.id, hazard=hz,
                                      distance=round(dist, 1))

            # positions at this tick (before advancing), then integrate vehicles forward one tick
            # (which also determines who is waiting at a red signal), then tag the frame.
            frame["actors"] = self._actor_positions(t)
            frame["signals"] = [{"id": a.id, "x": self._signal_pos(a)[0] or 0,
                                 "y": self._signal_pos(a)[1], "phase": signal_phase(a, t),
                                 "red": signal_phase(a, t) == "red"}
                                for a in self.s.actors if a.kind == "signal"]
            self._advance_vehicles(t, dt)
            for a in frame["actors"]:
                a["braked"] = a["id"] in self.perm_stop
                a["waiting"] = a["id"] in self.waiting
            self.frames_append(frame)

        return RunResult(scenario_name=self.s.name, events=self.log.as_dicts(),
                         frames=self._frames, summary=self._summary())

    # -- helpers -------------------------------------------------------------
    _frames: list[dict] = []

    def frames_append(self, frame: dict) -> None:
        self._frames.append(frame)

    def _frame_skeleton(self, t: float, tick: int, fog: float, jammers) -> dict:
        return {"t": round(t, 3), "tick": tick, "fog": round(fog, 2),
                "jammers": [{"x": j[0][0], "y": j[0][1], "r": j[1]} for j in jammers],
                "actors": [], "links": []}

    def _actor_positions(self, t: float) -> list[dict]:
        import math as _m
        actors = []
        for a in self.s.actors:
            if a.kind == "signal":
                continue  # signals render from frame["signals"], not the actor list
            p = self._pos(a, t)
            if p is None:
                continue
            angle = 0.0
            if a.kind in ("car", "truck"):
                tr = self.veh_travel.get(a.id, 0.0)
                q = pos_at_arclen(a.path, tr + 0.6) or pos_at_arclen(a.path, tr - 0.6)
                if q and (q[0] != p[0] or q[1] != p[1]):
                    angle = _m.atan2(q[1] - p[1], q[0] - p[0])
            actors.append({"id": a.id, "kind": a.kind, "label": a.label,
                           "x": round(p[0], 2), "y": round(p[1], 2), "angle": round(angle, 3),
                           "size": list(a.size), "is_hazard": a.is_hazard})
        return actors

    def _bfs_path(self, adj, sources, target) -> list[str] | None:
        if not sources:
            return None
        q = deque()
        prev: dict[str, str | None] = {}
        for s in sources:
            if s == target:
                return [s]
            q.append(s)
            prev[s] = None
        while q:
            u = q.popleft()
            for v in adj.get(u, []):
                if v in prev:
                    continue
                prev[v] = u
                if v == target:
                    path = [v]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])
                    return list(reversed(path))
                q.append(v)
        return None

    def _actor(self, actor_id: str) -> Actor:
        return next(a for a in self.s.actors if a.id == actor_id)

    def _summary(self) -> dict:
        braked = [(c, list(h)) for c, h in self.st.braked.items() if h]
        via_radio = {c: list(h) for c, h in self.st.aware_via_radio.items() if h}
        return {"braked": dict(braked), "aware_via_radio": via_radio,
                "n_events": len(self.log.events)}


def run_scenario(scenario: Scenario) -> RunResult:
    engine = SimEngine(scenario)
    engine._frames = []   # fresh per run (avoid class-attr sharing)
    return engine.run()
