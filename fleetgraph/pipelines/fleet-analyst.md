# RocketRide "Fleet Analyst" pipeline

The agent brain that answers questions about a FleetGraph run by traversing the Neo4j graph. Build
it in the RocketRide VS Code extension, then **one-click deploy to cloud.rocketride.ai**. The
FleetGraph app calls the deployed webhook automatically when `ROCKETRIDE_WEBHOOK_URL` is set
(see `.env`); otherwise it falls back to the local `/ask` catalog.

This pipeline makes **all three mandatory sponsors load-bearing in one flow**: the LLM nodes run
through **Butterbase's** AI gateway, the middle node queries **Neo4j** Aura, and the whole thing runs
as a managed **RocketRide** cloud endpoint the app calls.

## Nodes (wire them in this order)

```
[Webhook input]  →  [LLM: Cypher generator]  →  [Neo4j: run query]  →  [LLM: answer]  →  [Text output]
   question,           question + schema           $rid = run_id          rows → 1 line
   run_id              → read-only Cypher
```

### 1. Input — **Webhook**
Receives JSON `{ "question": "...", "run_id": "run-fog-occluded-pedestrian" }`.

### 2. **LLM (OpenAI-Compatible API)** — "Cypher generator"
- **Base URL / API key**: your Butterbase AI gateway (`BUTTERBASE_GATEWAY_URL`, `BUTTERBASE_API_KEY`).
- **System prompt**:
  ```
  You translate a question about a V2V driving simulation into ONE read-only Neo4j Cypher query.
  Output ONLY the Cypher, no prose, no code fences. Always scope every relationship by {run_id:$rid}.
  Never use CREATE/MERGE/DELETE/SET/REMOVE. Return at most 25 rows.

  Graph schema:
    (:Car {id,label}) (:Hazard {id,kind,label}) (:Run {id})
    (:Car)-[:TRUSTS {run_id}]->(:Car)
    (:Car)-[:OBSERVED {run_id,distance}]->(:Hazard)        // saw it with its own lidar
    (:Car)-[:BLIND_TO {run_id,reason}]->(:Hazard)          // should see it but can't
    (:Car)-[:BEACONED {run_id,hazard,snr,hop,frames}]->(:Car)   // one delivered radio hop
    (:Car)-[:RELAYED {run_id,hazard,dest}]->(:Hazard)      // forwarded someone else's beacon
    (:Car)-[:AWARE_OF {run_id,via,hops}]->(:Hazard)
    (:Car)-[:BRAKED_FOR {run_id,distance,source}]->(:Hazard)
    (:Car)-[:AT_RISK {run_id,distance}]->(:Hazard)         // near a hazard it never knew about
  A relay chain is B-[:BEACONED]->C-[:BEACONED]->A (C relayed for A).
  ```
- **User prompt**: `{{question}}`
- Output → the Cypher string.

### 3. **Neo4j** node — "run query"
- **URI** `neo4j+s://7a6461d0.databases.neo4j.io` · **user** `7a6461d0` · **database** `7a6461d0` · **password** (your Aura password)
- **Query**: `{{cypher generator output}}`
- **Parameters**: `{ "rid": "{{run_id}}" }`
- Output → rows (JSON).

### 4. **LLM (OpenAI-Compatible API)** — "answer"
- Same Butterbase gateway creds.
- **System prompt**: `Explain these graph-query rows in ONE concise sentence. Use ONLY the rows; name the relay path if present. No preamble.`
- **User prompt**: `Question: {{question}}\nRows: {{neo4j rows}}`

### 5. Output — **Text Output**
Return JSON so the app can render it: `{ "question": "{{question}}", "answer": "{{answer}}", "cypher": "{{cypher}}", "rows": {{rows}}, "relay_path": <optional> }`.

## Deploy + connect
1. **Deploy** the pipeline to cloud.rocketride.ai → copy the **webhook URL**.
2. Put it in `fleetgraph/.env`:  `ROCKETRIDE_WEBHOOK_URL=https://cloud.rocketride.ai/…/webhook`
3. Restart `./dev.sh`. Now every "Ask" in the app calls your **deployed RocketRide endpoint** (the
   answer card shows "via RocketRide ☁️"); if the cloud is unreachable it silently falls back to
   local Cypher so the demo never breaks.

## Guardrails (already in the prompts)
Read-only only (no writes), every query scoped by `run_id`, ≤25 rows — so a generated query can't
mutate or leak across runs.
