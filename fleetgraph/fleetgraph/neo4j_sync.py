"""Neo4j sync — turn the simulation's graph-event stream into a property graph.

This is the "graph brain": the same event stream that draws the 2D world also MERGEs a Neo4j graph
where the agent can traverse *how information moved through the fleet* — questions a SQL join can't
express cleanly (multi-hop relay chains, critical relays, blind-spot gaps).

Graph model
-----------
Nodes:
  (:Run {id, scenario})                     one simulation run
  (:Car {id, label})                        a radio-equipped vehicle
  (:Hazard {id, kind})                      something to avoid (pedestrian, ...)

Relationships (each carries `run_id` so multiple runs coexist and algorithms scope to one run):
  (:Car)-[:TRUSTS]->(:Car)                  pinned-key mesh trust (both directions)
  (:Car)-[:OBSERVED {t, distance}]->(:Hazard)      saw it with its own lidar
  (:Car)-[:BLIND_TO {t, reason, distance}]->(:Hazard)   should see it but can't (fog/occlusion/range)
  (:Car)-[:BEACONED {t, hazard, snr, hop, frames, retries, sig_verified}]->(:Car)
                                             one radio hop that DELIVERED (chains form relay paths)
  (:Car)-[:RELAYED {t, hazard, source, dest}]->(:Hazard)  forwarded someone else's beacon
  (:Car)-[:AWARE_OF {t, via, hops}]->(:Hazard)     knows about it (via lidar or the mesh)
  (:Car)-[:BRAKED_FOR {t, distance, source}]->(:Hazard)
  (:Car)-[:AT_RISK {t, distance}]->(:Hazard)       near a hazard it does NOT know about

The relay chain is emergent structure: B-[:BEACONED]->C-[:BEACONED]->A means C relayed for A. So
"who is the critical relay?" is betweenness centrality over :BEACONED, and "why did A brake?" is the
path (a)-[:BRAKED_FOR]->(h)<-...-[:BEACONED*]-(observer)-[:OBSERVED]->(h).
"""

from __future__ import annotations

from typing import Iterable

from fleetgraph.scenario import Scenario

# One-time schema (id uniqueness). Safe to run repeatedly.
SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT car_id IF NOT EXISTS FOR (c:Car) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT hazard_id IF NOT EXISTS FOR (h:Hazard) REQUIRE h.id IS UNIQUE",
    "CREATE CONSTRAINT run_id IF NOT EXISTS FOR (r:Run) REQUIRE r.id IS UNIQUE",
]

Statement = tuple[str, dict]


def _seed_statements(run_id: str, scenario: Scenario) -> list[Statement]:
    """Create the Run node and all Car/Hazard nodes up front so later MERGEs just add edges."""
    stmts: list[Statement] = [(
        "MERGE (r:Run {id:$run_id}) SET r.scenario=$scenario, r.fog=$fog",
        {"run_id": run_id, "scenario": scenario.name, "fog": scenario.world.fog_density},
    )]
    for c in scenario.cars():
        stmts.append(("MERGE (c:Car {id:$id}) SET c.label=$label",
                      {"id": c.id, "label": c.label or c.id}))
    for h in scenario.hazards():
        stmts.append(("MERGE (h:Hazard {id:$id}) SET h.kind=$kind, h.label=$label",
                      {"id": h.id, "kind": h.kind, "label": h.label or h.id}))
    return stmts


def statements_for_event(run_id: str, ev: dict) -> list[Statement]:
    """Map one graph event to idempotent MERGE statements (no-op for unknown/frame-only types)."""
    t = ev.get("t")
    et = ev["type"]
    rp = {"run_id": run_id, "t": t}

    if et == "trusts":
        return [(
            "MERGE (a:Car {id:$a}) MERGE (b:Car {id:$b}) "
            "MERGE (a)-[r1:TRUSTS {run_id:$run_id}]->(b) "
            "MERGE (b)-[r2:TRUSTS {run_id:$run_id}]->(a)",
            {"a": ev["a"], "b": ev["b"], "run_id": run_id},
        )]
    if et == "observed":
        return [(
            "MATCH (c:Car {id:$car}) MATCH (h:Hazard {id:$hazard}) "
            "MERGE (c)-[r:OBSERVED {run_id:$run_id}]->(h) "
            "SET r.t=coalesce(r.t,$t), r.distance=$distance",
            {**rp, "car": ev["car"], "hazard": ev["hazard"], "distance": ev.get("distance")},
        )]
    if et == "blind_to":
        return [(
            "MATCH (c:Car {id:$car}) MATCH (h:Hazard {id:$hazard}) "
            "MERGE (c)-[r:BLIND_TO {run_id:$run_id}]->(h) "
            "SET r.t=coalesce(r.t,$t), r.reason=$reason, r.distance=$distance",
            {**rp, "car": ev["car"], "hazard": ev["hazard"],
             "reason": ev.get("reason"), "distance": ev.get("distance")},
        )]
    if et == "heard":
        # one delivered radio hop: sender BEACONED to receiver (chains build relay paths)
        return [(
            "MATCH (s:Car {id:$sender}) MATCH (rc:Car {id:$receiver}) "
            "MERGE (s)-[b:BEACONED {run_id:$run_id, hazard:$hazard}]->(rc) "
            "SET b.t=coalesce(b.t,$t), b.snr=$snr, b.hop=$hop, b.frames=$frames, "
            "b.retries=$retries, b.sig_verified=$sig",
            {**rp, "sender": ev["sender"], "receiver": ev["receiver"], "hazard": ev["hazard"],
             "snr": ev.get("snr"), "hop": ev.get("hop"), "frames": ev.get("frames"),
             "retries": ev.get("retries"), "sig": ev.get("signature_verified")},
        )]
    if et == "relayed_by":
        return [(
            "MATCH (c:Car {id:$relay}) MATCH (h:Hazard {id:$hazard}) "
            "MERGE (c)-[r:RELAYED {run_id:$run_id, hazard:$hazard, dest:$dest}]->(h) "
            "SET r.t=coalesce(r.t,$t), r.source=$source",
            {**rp, "relay": ev["relay"], "hazard": ev["hazard"],
             "source": ev.get("source"), "dest": ev.get("dest")},
        )]
    if et == "aware":
        return [(
            "MATCH (c:Car {id:$car}) MATCH (h:Hazard {id:$hazard}) "
            "MERGE (c)-[r:AWARE_OF {run_id:$run_id}]->(h) "
            "SET r.t=coalesce(r.t,$t), r.via=$via, r.hops=$hops, r.path=$path",
            {**rp, "car": ev["car"], "hazard": ev["hazard"], "via": ev.get("via"),
             "hops": ev.get("hops"), "path": ev.get("path")},
        )]
    if et == "braked_for":
        return [(
            "MATCH (c:Car {id:$car}) MATCH (h:Hazard {id:$hazard}) "
            "MERGE (c)-[r:BRAKED_FOR {run_id:$run_id}]->(h) "
            "SET r.t=coalesce(r.t,$t), r.distance=$distance, r.source=$source",
            {**rp, "car": ev["car"], "hazard": ev["hazard"],
             "distance": ev.get("distance"), "source": ev.get("source")},
        )]
    if et == "unaware_risk":
        return [(
            "MATCH (c:Car {id:$car}) MATCH (h:Hazard {id:$hazard}) "
            "MERGE (c)-[r:AT_RISK {run_id:$run_id}]->(h) "
            "SET r.t=coalesce(r.t,$t), r.distance=$distance",
            {**rp, "car": ev["car"], "hazard": ev["hazard"], "distance": ev.get("distance")},
        )]
    return []


def build_statements(run_id: str, scenario: Scenario, events: Iterable[dict]) -> list[Statement]:
    """All statements to materialise a run — for offline inspection/testing without a live DB."""
    stmts = list(_seed_statements(run_id, scenario))
    for ev in events:
        stmts.extend(statements_for_event(run_id, ev))
    return stmts


class GraphSync:
    """Applies the event stream to a live Neo4j (Aura or local). Lazy driver import so the mapping
    module stays usable with no neo4j installed / no connection (offline statement generation)."""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j") -> None:
        from neo4j import GraphDatabase  # lazy
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database

    def close(self) -> None:
        self._driver.close()

    def ensure_schema(self) -> None:
        with self._driver.session(database=self._database) as s:
            for stmt in SCHEMA_STATEMENTS:
                s.run(stmt)

    def reset_run(self, run_id: str) -> None:
        """Remove a run's relationships + node so a re-run of the same scenario starts clean."""
        with self._driver.session(database=self._database) as s:
            s.run("MATCH ()-[r {run_id:$run_id}]-() DELETE r", run_id=run_id)
            s.run("MATCH (r:Run {id:$run_id}) DETACH DELETE r", run_id=run_id)

    def apply_run(self, run_id: str, scenario: Scenario, events: Iterable[dict]) -> int:
        """MERGE the whole run. Returns the number of statements executed."""
        stmts = build_statements(run_id, scenario, events)
        with self._driver.session(database=self._database) as s:
            for cypher, params in stmts:
                s.run(cypher, **params)
        return len(stmts)

    def apply_event(self, run_id: str, ev: dict) -> None:
        """Apply a single event live (for streaming a run into the graph as it plays)."""
        with self._driver.session(database=self._database) as s:
            for cypher, params in statements_for_event(run_id, ev):
                s.run(cypher, **params)
