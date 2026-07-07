# RocketRide "Fleet Analyst" pipeline

The cloud brain that turns a plain-English question about a FleetGraph run into a **read-only Neo4j
Cypher query**. It runs on **RocketRide** (`pipelines/fleet-analyst.pipe`), the LLM node calls the
**Butterbase** AI gateway, and the FastAPI app runs the returned Cypher against **Neo4j** Aura and
phrases the rows — so all three mandatory sponsors are load-bearing in one flow.

## Why this shape (and not a single linear pipeline)

RocketRide has **no first-class Neo4j / Cypher node** (confirmed against the component docs — the
documented DB nodes are `db_postgres`/`db_mysql`, SQL-only and agent-invoked). So the graph query
can't live *inside* a linear pipeline. Instead:

```
   app /ask ──question──▶ RocketRide chat pipeline ──Cypher──▶ app runs it on Neo4j ──rows──▶ Butterbase phrases ──▶ answer
                          (chat → llm_openai → response_answers)
```

RocketRide owns the hard NL→Cypher reasoning (the "analyst"); the app owns the graph I/O. Clean,
deterministic, and every generated query is validated read-only before it touches the database.

## The pipeline (`fleet-analyst.pipe`)

A stable `chat` pipeline — `chat → llm_openai → response_answers`:

- **chat** source — emits the `questions` lane (driven from Python via `client.chat()`).
- **llm_openai** ("analyst") — profile `butterbase`, pointed at the Butterbase AI gateway
  (`${ROCKETRIDE_GATEWAY_URL}` / `${ROCKETRIDE_GATEWAY_KEY}` / `${ROCKETRIDE_MODEL}`). Consumes
  `questions`, emits `answers`.
- **response_answers** — returns the `answers` lane (`response["answers"][0]` = the Cypher).

The **graph schema + Cypher rules + few-shot examples are supplied per request** via the `Question`
object (`addInstruction` / `addContext` / `addExample` / `addGoal` in `fleetgraph/rocketride_client.py`),
so the pipeline itself stays generic and prompt-free.

> ⚠️ **One field to confirm on deploy.** The offline RocketRide docs don't document a custom
> base-URL field for `llm_openai`, so the `.pipe` uses `endpoint` as the best guess. When you connect
> your RocketRide account, `check.py` validates against the live server; if `endpoint` is wrong, open
> the regenerated `.rocketride/schema/llm_openai.json` (`Pipe.schema.dependencies.profile.oneOf`) for
> the real field name. If `llm_openai` has no custom-endpoint field at all, switch the node to
> `llm_anthropic` (native Claude) or a native `llm_openai` OpenAI key — the app flow is unchanged.

## Setup + connect

1. `pip install rocketride` (already in the fleetgraph venv).
2. Set **`ROCKETRIDE_URI`** + **`ROCKETRIDE_APIKEY`** in `fleetgraph/.env` (from the RocketRide VS Code
   extension settings, or cloud.rocketride.ai). The app injects the gateway creds from its existing
   `BUTTERBASE_*` vars automatically.
3. `cd pipelines && python check.py` — verifies env + pipeline structure; once a key is set it
   connects, validates, and reports the real `llm_openai` base-URL field.
4. Restart `./dev.sh`. Now any **free-form** "Ask" (one not in the local catalog) is routed to the
   RocketRide analyst → Cypher → Neo4j → Butterbase phrasing; the answer card shows "via RocketRide ☁️".
   Catalog questions still use their hand-written Cypher, and if RocketRide is unset/unreachable the
   app falls back to the catalog so the demo never breaks.

The app **`use()`s the `.pipe` directly** at runtime (no separate deploy needed). To also host it as a
managed/scheduled endpoint, `client.deploy.add(pipeline)` (see `client.deploy.list/status/update/remove`).

## Guardrails

- The Question instructions force **read-only Cypher, scoped by `{run_id:$rid}`, ≤25 rows**.
- `rocketride_client.is_readonly_cypher()` **rejects** any generated query containing
  `CREATE/MERGE/DELETE/SET/REMOVE/DROP/DETACH/CALL/LOAD/FOREACH` before it runs — so a bad generation
  can neither mutate nor break out of the run scope.
