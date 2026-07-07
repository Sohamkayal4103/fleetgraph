"""Verify the event -> Cypher mapping (offline), then, if NEO4J_* env is set, push a run to Aura
and ask the graph the demo questions the agent will later ask through RocketRide.

    .venv/bin/python smoke_graph.py                 # offline: just generate + count statements
    NEO4J_URI=... NEO4J_USER=... NEO4J_PASSWORD=... .venv/bin/python smoke_graph.py   # + live
"""

import os

from fleetgraph.engine import run_scenario
from fleetgraph.neo4j_sync import GraphSync, build_statements
from fleetgraph.scenario import example_fog_pedestrian

RUN_ID = "run-demo-1"

DEMO_QUERIES = {
    "Why did car-A brake? (trace the info back to who saw it)":
        """MATCH (a:Car {id:'car-A'})-[br:BRAKED_FOR]->(h:Hazard)
           MATCH (obs:Car)-[:OBSERVED]->(h)
           MATCH chain = (obs)-[:BEACONED* {run_id:$rid}]->(a)
           RETURN obs.id AS saw_it, [n IN nodes(chain) | n.id] AS relay_path,
                  h.id AS hazard, br.distance AS braked_at_m, br.source AS via
           ORDER BY length(chain) LIMIT 3""",
    "Who is the critical relay (forwarded info it never saw itself)?":
        """MATCH (relay:Car)-[:RELAYED {run_id:$rid}]->(h:Hazard)
           WHERE NOT (relay)-[:OBSERVED]->(h)
           RETURN relay.id AS relay, h.id AS hazard""",
    "What is car-A blind to that it never became aware of? (danger gap)":
        """MATCH (a:Car {id:'car-A'})-[:BLIND_TO]->(h:Hazard)
           WHERE NOT (a)-[:AWARE_OF]->(h)
           RETURN a.id AS car, collect(h.id) AS blind_and_unaware""",
    "Every radio hop that delivered, with real modem facts:":
        """MATCH (s:Car)-[b:BEACONED {run_id:$rid}]->(r:Car)
           RETURN s.id AS from, r.id AS to, b.hazard AS about, b.snr AS snr_db,
                  b.hop AS hop, b.frames AS frames, b.sig_verified AS signed
           ORDER BY s.id""",
}


def main() -> None:
    scenario = example_fog_pedestrian()
    result = run_scenario(scenario)
    stmts = build_statements(RUN_ID, scenario, result.events)
    print(f"OFFLINE: {len(result.events)} events -> {len(stmts)} idempotent Cypher statements")
    kinds: dict[str, int] = {}
    for e in result.events:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    print("  event mix:", kinds)

    uri = os.environ.get("NEO4J_URI")
    pw = os.environ.get("NEO4J_PASSWORD")
    if not (uri and pw):
        print("\n(no NEO4J_* env set — skipping live sync. Set NEO4J_URI/USER/PASSWORD to push.)")
        return

    user = os.environ.get("NEO4J_USER", "neo4j")
    database = os.environ.get("NEO4J_DATABASE", "neo4j")
    print(f"\nLIVE: connecting to {uri} as {user} (db={database}) ...")
    sync = GraphSync(uri, user, pw, database=database)
    sync.ensure_schema()
    sync.reset_run(RUN_ID)
    n = sync.apply_run(RUN_ID, scenario, result.events)
    print(f"  applied {n} statements.\n")
    with sync._driver.session() as s:
        for label, q in DEMO_QUERIES.items():
            print(f"Q: {label}")
            for rec in s.run(q, rid=RUN_ID):
                print("   ", dict(rec))
            print()
    sync.close()
    print("Live graph populated. Open Neo4j Browser and run:  MATCH (n) RETURN n")


if __name__ == "__main__":
    main()
