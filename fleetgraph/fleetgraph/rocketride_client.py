"""RocketRide integration — the deployed "Fleet Analyst" brain.

RocketRide hosts the hard reasoning step: turn a free-form English question about a V2V run into ONE
read-only Neo4j Cypher query. RocketRide has no first-class Neo4j node, so the FastAPI app runs the
returned Cypher against Neo4j Aura itself, then phrases the rows via Butterbase. This keeps all three
sponsors load-bearing: RocketRide = the NL→Cypher analyst, Neo4j = the graph, Butterbase = the LLM
gateway the pipeline's llm_openai node calls (+ the app's DB/auth/credits).

Pipeline: pipelines/fleet-analyst.pipe  (chat → llm_openai → response_answers). The graph schema +
Cypher rules are supplied PER REQUEST via the Question object (addInstruction/addContext/addExample),
so the pipeline stays generic. Runtime path: connect() → use(filepath=...) → chat(Question).

Config (env): ROCKETRIDE_URI + ROCKETRIDE_APIKEY (from cloud.rocketride.ai / the VS Code extension).
The pipeline's llm_openai creds are injected from the app's Butterbase gateway vars at use() time.

Guarded end-to-end: any failure returns None, so /ask falls back to the local Cypher catalog and the
demo never breaks. No RocketRide account configured → is_configured() is False and we never call out.
"""

from __future__ import annotations

import asyncio
import os
import re

_client = None          # type: ignore  # lazily-created RocketRideClient
_token: str | None = None
_lock = asyncio.Lock()

# The FleetGraph graph schema the analyst reasons over (mirrors neo4j_sync + the /ask catalog).
GRAPH_SCHEMA = """\
Nodes:   (:Car {id,label})  (:Hazard {id,kind,label})  (:Run {id})
Edges (every one carries {run_id}):
  (:Car)-[:TRUSTS {run_id}]->(:Car)
  (:Car)-[:OBSERVED {run_id,distance}]->(:Hazard)     // saw it with its own lidar
  (:Car)-[:BLIND_TO {run_id,reason}]->(:Hazard)        // should see it but can't (fog/occlusion)
  (:Car)-[:BEACONED {run_id,hazard,snr,hop,frames}]->(:Car)  // one delivered radio hop
  (:Car)-[:RELAYED {run_id,hazard,dest}]->(:Hazard)    // forwarded someone else's beacon
  (:Car)-[:AWARE_OF {run_id,via,hops}]->(:Hazard)
  (:Car)-[:BRAKED_FOR {run_id,distance,source}]->(:Hazard)
  (:Car)-[:AT_RISK {run_id,distance}]->(:Hazard)       // near a hazard it never knew about
A relay chain is B-[:BEACONED]->C-[:BEACONED]->A (C relayed for A)."""

FEW_SHOT: list[tuple[str, str]] = [
    ("Why did the ambulance brake?",
     "MATCH (a:Car)-[br:BRAKED_FOR {run_id:$rid}]->(h:Hazard) "
     "RETURN a.id AS car, h.id AS hazard, round(br.distance,1) AS braked_at_m, br.source AS via"),
    ("Show every radio hop that delivered.",
     "MATCH (s:Car)-[b:BEACONED {run_id:$rid}]->(r:Car) "
     "RETURN s.id AS from_car, r.id AS to_car, b.hazard AS about, b.snr AS snr_db, b.hop AS hop "
     "ORDER BY s.id LIMIT 25"),
]

_WRITE_RE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH|CALL|LOAD|FOREACH)\b", re.I)


def is_configured() -> bool:
    return bool(os.environ.get("ROCKETRIDE_URI") and os.environ.get("ROCKETRIDE_APIKEY"))


def is_readonly_cypher(cypher: str) -> bool:
    """Reject anything that could mutate the graph — the LLM output is untrusted."""
    return bool(cypher) and not _WRITE_RE.search(cypher)


def _pipe_path() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "pipelines", "fleet-analyst.pipe"))


def _pipeline_env() -> dict:
    """Feed the pipeline's ${ROCKETRIDE_*} from the app's existing Butterbase gateway creds."""
    return {
        "ROCKETRIDE_GATEWAY_URL": os.environ.get("BUTTERBASE_GATEWAY_URL", ""),
        "ROCKETRIDE_GATEWAY_KEY": os.environ.get("BUTTERBASE_API_KEY", ""),
        "ROCKETRIDE_MODEL": os.environ.get("BUTTERBASE_MODEL", "anthropic/claude-sonnet-4.6"),
    }


def _clean_cypher(text: str) -> str:
    """Strip ```/```cypher fences and surrounding prose the model may add."""
    t = text.strip()
    fenced = re.search(r"```(?:cypher|sql)?\s*(.*?)\s*```", t, re.DOTALL | re.I)
    if fenced:
        t = fenced.group(1).strip()
    return t


async def _reset() -> None:
    global _client, _token
    c = _client
    _client, _token = None, None
    if c is not None:
        try:
            await c.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def _ensure():
    """Lazily connect + start the pipeline once; reuse the token across requests."""
    global _client, _token
    if _client is not None and _token is not None:
        return _client, _token
    async with _lock:
        if _client is not None and _token is not None:
            return _client, _token
        from rocketride import RocketRideClient  # imported lazily so the SDK is optional
        client = RocketRideClient()               # reads ROCKETRIDE_URI/APIKEY from env
        await client.connect()
        res = await client.use(filepath=_pipe_path(), env=_pipeline_env(), use_existing=True)
        _client = client
        _token = res.get("token")
        return _client, _token


async def generate_cypher(question: str, run_id: str) -> str | None:
    """Ask the deployed analyst to translate an NL question into read-only Cypher (scoped by $rid).

    Returns the Cypher string, or None on any failure / non-read-only output (caller falls back).
    """
    if not is_configured():
        return None
    try:
        from rocketride.schema import Question
        client, token = await _ensure()
        q = Question()
        q.addGoal("Translate the user's question about a V2V (vehicle-to-vehicle) driving simulation "
                  "into ONE read-only Neo4j Cypher query.")
        q.addInstruction("format", "Output ONLY the Cypher query — no prose, no markdown, no code fences.")
        q.addInstruction("read-only", "Use only MATCH / OPTIONAL MATCH / WHERE / RETURN / WITH / ORDER BY "
                                      "/ LIMIT. Never CREATE, MERGE, DELETE, SET, REMOVE, DROP, or CALL.")
        q.addInstruction("scope", "Scope EVERY relationship pattern by {run_id:$rid}. Return at most 25 rows.")
        q.addContext("Graph schema:\n" + GRAPH_SCHEMA)
        for given, result in FEW_SHOT:
            q.addExample(given, result)
        q.addQuestion(question)
        resp = await client.chat(token=token, question=q)
        answers = (resp or {}).get("answers") or []
        if not answers:
            return None
        cypher = _clean_cypher(str(answers[0]))
        return cypher if is_readonly_cypher(cypher) else None
    except Exception:  # noqa: BLE001 — never break /ask; drop the connection so we reconnect next time
        await _reset()
        return None


async def aclose() -> None:
    await _reset()
