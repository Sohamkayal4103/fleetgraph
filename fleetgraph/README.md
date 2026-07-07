# FleetGraph — *When sensors go blind, the mesh sees*

A graph-aware agentic app for **HackwithBay 3.0**. It simulates connected vehicles (V2V): when a
car's **lidar is blinded** by fog or an occluding truck, it falls back to **radio**, and cars share
**signed public state beacons** — relayed multi-hop through the mesh. Every perception, beacon,
relay, and braking decision becomes a **Neo4j** graph an agent reasons over to answer questions a
SQL join can't express ("why did the ambulance brake?" → the exact relay path that warned it).

The radio is **real, not simulated with `random()`**: beacons are signed Ed25519 envelopes pushed
through a real BPSK modem + impaired channel (reused from the `aithernet` SDR runtime), and a beacon
is delivered or lost based on SNR derived from the world (distance + fog + jammer).

## Run it

```bash
cp .env.example .env        # already filled with the Aura connection for this project
./dev.sh                    # starts backend :8099 + web UI :5180
# open http://localhost:5180
```

Then: **Run scenario** ▶ to play the built-in fog/pedestrian scene, scrub the timeline, and click
the question buttons (or type one) to ask the graph. Use the **+car / +truck / +pedestrian /
+obstacle / +jammer** tools and the fog slider to build your own scenario, then Run.

Headless checks (no browser):
```bash
.venv/bin/python smoke_radio.py     # the real-radio delivery cliff
.venv/bin/python run_scenario.py    # full scenario -> graph-event narrative
NEO4J_* .venv/bin/python smoke_graph.py   # push a run to Aura + ask the demo queries
```

## Architecture

```
Scenario JSON ──▶ engine (world.py: lidar fog+occlusion │ radio.py: real BPSK modem+channel │
                          engine.py: BFS multi-hop relay) ──▶ graph events + frames
   frames ─▶ web/ 2D world viewer          events ─▶ neo4j_sync.py ─▶ Neo4j (Aura)
                                                          │
   web "Ask" ─▶ RocketRide pipeline ─▶ Butterbase LLM → Neo4j Cypher → answer with the path
```

## Sponsor integration
- **Neo4j** — the awareness/propagation graph (`:Car -[:BEACONED]-> :Car`, relay chains,
  `:OBSERVED/:BLIND_TO/:AWARE_OF/:BRAKED_FOR`). Queried with multi-hop Cypher + centrality.
- **RocketRide** — the "Fleet Analyst" pipeline (Webhook → LLM→Cypher → **Neo4j node** → LLM→answer
  → output), deployed to cloud.rocketride.ai; the app's "Ask" calls it.
- **Butterbase** — backend: auth, database (scenario library + run history), **AI gateway** (powers
  the RocketRide LLM nodes and the natural-language → scenario generator), payment (scenario credits).
- *(bonus)* **Daytona** — run batches of headless sims to A/B a proposed safety tweak.
- *(bonus)* **Cognee** — cross-run memory ("in past fog runs, relaying via the middle car cut
  warning latency").

Built on top of [`../aithernet`](../aithernet) (used as a library for the real radio stack).
