# 🚗📡 FleetGraph — *When sensors go blind, the mesh sees*

**HackwithBay 3.0 submission.** A graph-aware agentic application for connected-vehicle (V2V) road
safety. When a car's lidar/camera is blinded — by fog, a truck, or an occluded corner — it falls back
to **radio** and shares signed public state beacons with nearby cars, relayed multi-hop across the
mesh. Every perception, beacon, relay hop, and braking decision is written to a **Neo4j** graph, and
an agent **traverses** that graph to answer questions a SQL join can't express cleanly — *"why did the
ambulance brake for a pedestrian it never saw?"* → the exact relay path that warned it.

## ▶️ Watch the 3-minute demo

[![FleetGraph — 3-minute demo](https://img.youtube.com/vi/M3zUnrmp3yc/maxresdefault.jpg)](https://youtu.be/M3zUnrmp3yc)

*(click the image — a narrated walkthrough: the fog relay, asking the graph, a jammer attack, city grids, and how the three sponsors fit together.)*

> **Deployed app:** https://fleetgraph.butterbase.dev
> **Demo scenarios:** fog + relay, jammer silences the mesh, highway stalled-car, and 2D city **grids**
> with turning routes, traffic signals, and intersections.

The radio is **real, not `random()`**: beacons are Ed25519-signed envelopes pushed through an actual
BPSK modem + impaired channel (reused from the `aithernet/` SDR runtime), delivered or lost based on
SNR derived from the world (distance + fog + jammer). Same code path that would drive a physical SDR.

---

## The three mandatory sponsors — all load-bearing

| Sponsor | How it's woven into the core product |
|---|---|
| **Neo4j** | The awareness/propagation **property graph**. `(:Car)-[:BEACONED]->(:Car)` chains form relay paths; `:OBSERVED / :BLIND_TO / :AWARE_OF / :BRAKED_FOR / :AT_RISK / :RELAYED / :TRUSTS`. The agent answers with real multi-hop Cypher traversals (relay chains, critical-relay detection, blind-spot gaps). |
| **RocketRide** | The **"Fleet Analyst"** pipeline (Webhook → LLM→Cypher → **Neo4j node** → LLM→answer → output), deployed to cloud.rocketride.ai. The app's "Ask the graph" box calls the deployed endpoint (with local fallback). See [`fleetgraph/rocketride/fleet-analyst.md`](fleetgraph/rocketride/fleet-analyst.md). |
| **Butterbase** | The **backend of record**: end-user **auth** (email/JWT), **database** (scenarios + run history + agent Q&A), the **AI gateway** (powers the natural-language → scenario generator *and* the RocketRide LLM nodes), and **payment** — a "sim credits" economy where signed-in users spend a credit per simulation and can buy more. |

*(Bonus tracks Daytona / Cognee: see roadmap.)*

## What the demo shows
- A **2D driving world** (straight multi-lane roads *and* city grids with intersections): cars with
  realistic accel/decel, 3-phase traffic signals (green/yellow/red) with stop-and-go, lane changes,
  turning routes (first left/right), pedestrians, obstacles, and RF jammers — all authored by
  drag-and-drop or by **describing a scenario in words** (Butterbase AI gateway → Scenario JSON).
- A **live knowledge graph** built from the same event stream, mirrored into Neo4j.
- **Ask the graph** (via RocketRide) — grounded answers that cite the actual relay path.

## Architecture
```
Scenario (JSON, hand-built or LLM-generated via Butterbase)
   └─ engine: world sim (lidar fog/occlusion) │ real BPSK radio (aithernet) │ BFS multi-hop relay
        ├─▶ 2D world viewer (left)          events ─▶ Neo4j (graph brain)
        └─▶ live graph viewer (right)             │
   "Ask the graph" ─▶ RocketRide pipeline ─▶ Butterbase LLM → Cypher over Neo4j → cited answer
   Butterbase: auth · database (runs/scenarios) · AI gateway · sim-credits payment
```

## Repository layout
- [`fleetgraph/`](fleetgraph/) — **the hackathon project**: Python sim engine + FastAPI backend
  (`fleetgraph/`), Butterbase integration, RocketRide pipeline recipe, and the React/Vite web app
  (`web/`).
- [`aithernet/`](aithernet/) — an autonomous-SDR node runtime, **reused as a library** for the real
  Ed25519 signing + BPSK modem/impaired-channel that carries the V2V beacons. (MIT.)

## Run it locally
```bash
cd fleetgraph
cp .env.example .env      # fill NEO4J_* (+ optional BUTTERBASE_* / ROCKETRIDE_*)
./dev.sh                  # backend :8099 + web :5180 → open http://localhost:5180
```
Pick a scenario (or drag vehicles / a traffic light / a pedestrian onto the road, or type a
description), press **Run**, scrub the timeline, then ask the graph a question.

## License
MIT — see [LICENSE](LICENSE).
