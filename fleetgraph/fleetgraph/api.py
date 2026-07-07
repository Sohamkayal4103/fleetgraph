"""FleetGraph backend API.

One small FastAPI app that the web UI calls:
  - run a scenario (headless engine) and sync the resulting graph into Neo4j,
  - hand back the event + frame stream so the browser can animate world | graph,
  - answer graph questions by running Cypher against Neo4j (the "agent" surface; the same queries
    the RocketRide/Butterbase pipeline will host).

Butterbase is the backend-of-record: every run + question is persisted there, end users log in
through it (email/password -> JWT), and a "sim credits" economy gates simulations for signed-in
users. All of that is best-effort — if Butterbase is unreachable the core sim/graph loop still runs
(anonymous, ungated). See butterbase_backend.py (data/auth/credits) and butterbase.py (AI gateway).

Neo4j connection comes from env: NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, NEO4J_DATABASE.
"""

from __future__ import annotations

import os

import anyio
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from fleetgraph import butterbase
from fleetgraph import butterbase_backend as bb
from fleetgraph import rocketride_client as rr
from fleetgraph.engine import RunResult, run_scenario
from fleetgraph.neo4j_sync import GraphSync
from fleetgraph.scenario import (
    Scenario,
    example_fog_pedestrian,
    get_scenario,
    load_scenario,
    scenario_catalog,
)


def _sync() -> GraphSync | None:
    uri, pw = os.environ.get("NEO4J_URI"), os.environ.get("NEO4J_PASSWORD")
    if not (uri and pw):
        return None
    return GraphSync(uri, os.environ.get("NEO4J_USER", "neo4j"), pw,
                     database=os.environ.get("NEO4J_DATABASE", "neo4j"))


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _uid(user: dict | None) -> str | None:
    if not user:
        return None
    return user.get("id") or user.get("user", {}).get("id")


app = FastAPI(title="FleetGraph API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=False)


# --------------------------------------------------------------------------- run

class RunRequest(BaseModel):
    scenario: dict | None = None      # a Scenario dict; None -> built-in example
    run_id: str = "run-demo-1"
    sync_neo4j: bool = True


@app.get("/health")
def health() -> dict:
    return {"ok": True, "neo4j_configured": bool(os.environ.get("NEO4J_URI")),
            "butterbase_configured": butterbase.is_configured(),
            "butterbase_backend_configured": bb.is_configured()}


class GenerateRequest(BaseModel):
    prompt: str


@app.post("/scenarios/generate")
async def scenarios_generate(req: GenerateRequest,
                             authorization: str | None = Header(default=None)) -> dict:
    """Natural-language -> Scenario JSON, via the Butterbase AI gateway (validated before returning).

    The validated scenario is also saved to Butterbase (source='ai') so it shows up in the catalog.
    """
    if not butterbase.is_configured():
        raise HTTPException(503, "Butterbase AI gateway not configured "
                                 "(set BUTTERBASE_GATEWAY_URL + BUTTERBASE_API_KEY).")
    try:
        data = await butterbase.generate_scenario(req.prompt)
        scenario = load_scenario(data)      # validate + fill lanes/paths
    except butterbase.ButterbaseError as exc:
        raise HTTPException(502, f"Butterbase gateway error: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — invalid generated scenario
        raise HTTPException(422, f"Generated scenario was invalid: {type(exc).__name__}: {exc}") from exc
    if bb.is_configured():
        user = await bb.user_from_token(_bearer(authorization))
        try:
            await bb.save_scenario(name=scenario.name, spec=scenario.model_dump(),
                                   description=scenario.description, source="ai", user_id=_uid(user))
        except bb.ButterbaseBackendError:
            pass
    return scenario.model_dump()


@app.get("/scenarios/example")
def scenario_example() -> dict:
    return example_fog_pedestrian().model_dump()


@app.get("/scenarios/catalog")
def scenarios_catalog() -> list[dict]:
    return scenario_catalog()


@app.get("/scenarios/{scenario_id}")
def scenario_by_id(scenario_id: str) -> dict:
    try:
        return get_scenario(scenario_id).model_dump()
    except KeyError as exc:
        raise HTTPException(404, f"no scenario '{scenario_id}'") from exc


@app.post("/scenarios/validate")
def scenario_validate(body: dict) -> dict:
    try:
        s = load_scenario(body)
        return {"valid": True, "cars": len(s.cars()), "hazards": len(s.hazards())}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid scenario: {type(exc).__name__}: {exc}") from exc


def _execute_run(scenario: Scenario, run_id: str, sync_neo4j: bool) -> tuple[RunResult, bool]:
    """Blocking sim + Neo4j sync — run off the event loop via anyio.to_thread."""
    result = run_scenario(scenario)
    synced = False
    if sync_neo4j:
        gs = _sync()
        if gs is not None:
            try:
                gs.ensure_schema()
                gs.reset_run(run_id)
                gs.apply_run(run_id, scenario, result.events)
                synced = True
            finally:
                gs.close()
    return result, synced


def _run_counts(events: list[dict]) -> tuple[int, int, int]:
    """(event_count, brake_count, relay_count) from the flat event stream."""
    brake = sum(1 for e in events if e.get("type") == "braked_for")
    relay = sum(1 for e in events if e.get("type") in ("heard", "relayed_by"))
    return len(events), brake, relay


@app.post("/run")
async def run(req: RunRequest, authorization: str | None = Header(default=None)) -> dict:
    scenario: Scenario = load_scenario(req.scenario) if req.scenario else example_fog_pedestrian()

    # Signed-in users spend a sim credit per run; anonymous visitors run free (demo stays open).
    user = await bb.user_from_token(_bearer(authorization)) if bb.is_configured() else None
    if user:
        if await bb.credit_balance(_uid(user)) < bb.RUN_COST:
            raise HTTPException(402, "Out of sim credits — buy a pack to keep running.")

    result, synced = await anyio.to_thread.run_sync(_execute_run, scenario, req.run_id, req.sync_neo4j)
    event_count, brake_count, relay_count = _run_counts(result.events)

    db_run_id: str | None = None
    credits_remaining: int | None = None
    if bb.is_configured():
        uid = _uid(user)
        if user:
            credits_remaining = await bb.spend_credit(uid)
            if credits_remaining is None:            # depleted in a race — don't hard-fail the run
                credits_remaining = 0
        summary = {
            **result.summary,
            "duration_s": scenario.duration_s, "tick_hz": scenario.tick_hz,
            "tick_count": len(result.frames),
            "brake_count": brake_count, "relay_count": relay_count,
        }
        try:
            row = await bb.save_run(
                scenario_name=scenario.name, neo4j_run_id=req.run_id, summary=summary,
                event_count=event_count, brake_count=brake_count, relay_count=relay_count,
                duration_s=scenario.duration_s, tick_count=len(result.frames),
                synced_neo4j=synced, user_id=uid)
            db_run_id = row.get("id")
        except bb.ButterbaseBackendError:
            pass

    return {
        "run_id": req.run_id,
        "db_run_id": db_run_id,
        "scenario": scenario.model_dump(),
        "events": result.events,
        "frames": result.frames,
        "summary": result.summary,
        "neo4j_synced": synced,
        "credits_remaining": credits_remaining,
    }


# --------------------------------------------------------------------------- ask

# The demo question catalog. Each is a parameterised Cypher traversal over one run. This is exactly
# the logic the RocketRide/Butterbase pipeline will host: an NL question maps to a graph query, and
# the answer is grounded in a real path, not the model's imagination.
ASK_CATALOG: dict[str, dict] = {
    "why_brake": {
        "q": "Why did the ambulance brake?",
        # Works whether the warning came over the mesh (a BEACONED relay chain from an observer) or
        # from the car's own lidar (no chain -> relay_path is just the car itself, via='lidar').
        "cypher": """
            MATCH (a:Car)-[br:BRAKED_FOR {run_id:$rid}]->(h:Hazard)
            OPTIONAL MATCH cp = (obs:Car)-[:BEACONED* {run_id:$rid}]->(a)
              WHERE (obs)-[:OBSERVED {run_id:$rid}]->(h)
            WITH a, h, br, cp ORDER BY length(cp) DESC
            WITH a, h, br, collect(cp)[0] AS best
            RETURN a.id AS car, h.id AS hazard, round(br.distance, 1) AS braked_at_m,
                   br.source AS via,
                   CASE WHEN best IS NULL THEN [a.id] ELSE [n IN nodes(best) | n.id] END AS relay_path,
                   CASE WHEN best IS NULL THEN a.id ELSE head([n IN nodes(best) | n.id]) END AS first_saw""",
    },
    "critical_relay": {
        "q": "Which car is the critical relay?",
        "cypher": """
            MATCH (relay:Car)-[:RELAYED {run_id:$rid}]->(h:Hazard)
            WHERE NOT (relay)-[:OBSERVED {run_id:$rid}]->(h)
            RETURN relay.id AS relay, collect(DISTINCT h.id) AS forwarded_hazards""",
    },
    "at_risk": {
        "q": "Which car came near a hazard it never knew about?",
        "cypher": """
            MATCH (c:Car)-[r:AT_RISK {run_id:$rid}]->(h:Hazard)
            RETURN c.id AS car, h.id AS hazard, round(r.distance, 1) AS came_within_m
            ORDER BY r.distance""",
    },
    "blind_gap": {
        "q": "What is any car blind to that it never learned about?",
        "cypher": """
            MATCH (c:Car)-[:BLIND_TO {run_id:$rid}]->(h:Hazard)
            WHERE NOT (c)-[:AWARE_OF {run_id:$rid}]->(h)
            RETURN c.id AS car, collect(h.id) AS blind_and_unaware""",
    },
    "radio_hops": {
        "q": "Show every radio hop that delivered.",
        "cypher": """
            MATCH (s:Car)-[b:BEACONED {run_id:$rid}]->(r:Car)
            RETURN s.id AS from_car, r.id AS to_car, b.hazard AS about, b.snr AS snr_db,
                   b.hop AS hop, b.frames AS modem_frames, b.sig_verified AS signature_ok
            ORDER BY s.id""",
    },
    "who_knows": {
        "q": "Who knows about the hazard, and how?",
        "cypher": """
            MATCH (c:Car)-[a:AWARE_OF {run_id:$rid}]->(h:Hazard)
            RETURN c.id AS car, h.id AS hazard, a.via AS via, a.path AS relay_path, a.hops AS hops
            ORDER BY a.hops""",
    },
}


class AskRequest(BaseModel):
    run_id: str = "run-demo-1"
    question_id: str | None = None
    question: str | None = None


@app.get("/ask/catalog")
def ask_catalog() -> list[dict]:
    return [{"id": k, "question": v["q"]} for k, v in ASK_CATALOG.items()]


def _match_question(text: str) -> str | None:
    """Very small keyword router (placeholder for the LLM->Cypher step via Butterbase gateway)."""
    t = text.lower()
    if "brake" in t or "braked" in t or "stop" in t:
        return "why_brake"
    if "relay" in t or "forward" in t:
        return "critical_relay"
    if "blind" in t or "gap" in t or "miss" in t:
        return "blind_gap"
    if "hop" in t or "deliver" in t or "signal" in t:
        return "radio_hops"
    if "know" in t or "aware" in t or "pedestrian" in t:
        return "who_knows"
    return None


async def _persist_ask(*, question: str, run_id: str, cypher: str | None,
                       rows: list | None, answer: str | None, user: dict | None) -> None:
    """Best-effort: record the question + answer in Butterbase, linked to the run when we can."""
    if not bb.is_configured():
        return
    try:
        db_run = await bb.latest_run_uuid(run_id)
        await bb.save_ask(question=question, run_id=db_run, cypher=cypher,
                          rows=rows, answer=answer, user_id=_uid(user))
    except bb.ButterbaseBackendError:
        pass


@app.post("/ask")
async def ask(req: AskRequest, authorization: str | None = Header(default=None)) -> dict:
    """Answer a graph question, grounded in a real Neo4j traversal.

    Catalog questions use their hand-written Cypher. A free-form question with no catalog match is
    sent to the deployed RocketRide "fleet analyst", which returns a read-only Cypher query we run
    here (RocketRide has no Neo4j node). Either way the rows are phrased in one sentence via the
    Butterbase gateway. Falls back to the catalog when RocketRide is unset/unreachable so the demo
    never breaks."""
    user = await bb.user_from_token(_bearer(authorization)) if bb.is_configured() else None

    gs = _sync()
    if gs is None:
        raise HTTPException(503, "Neo4j not configured (set NEO4J_URI/USER/PASSWORD/DATABASE).")

    qid = req.question_id or (_match_question(req.question) if req.question else None)
    cypher: str | None = None
    question_label: str | None = None
    source = "local"
    if qid in ASK_CATALOG:
        entry = ASK_CATALOG[qid]
        cypher, question_label = entry["cypher"].strip(), entry["q"]
    elif req.question and rr.is_configured():
        gen = await rr.generate_cypher(req.question, req.run_id)   # RocketRide: NL -> read-only Cypher
        if gen:
            cypher, question_label, source = gen, req.question, "rocketride"

    if cypher is None:
        gs.close()
        hint = ("Pick one from /ask/catalog." if not rr.is_configured()
                else "Pick one from /ask/catalog, or rephrase — the analyst couldn't turn that into a query.")
        raise HTTPException(400, f"Could not map the question to a graph query. {hint}")

    try:
        with gs._driver.session(database=gs._database) as s:
            rows = [dict(r) for r in s.run(cypher, rid=req.run_id)]
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — a generated query that won't run shouldn't 500
        raise HTTPException(422, f"Graph query failed: {type(exc).__name__}: {exc}") from exc
    finally:
        gs.close()

    answer_text = None
    if butterbase.is_configured() and rows:
        try:
            answer_text = await butterbase.phrase_answer(question_label, rows)
        except butterbase.ButterbaseError:
            answer_text = None
    await _persist_ask(question=question_label, run_id=req.run_id, cypher=cypher,
                       rows=rows, answer=answer_text, user=user)
    return {"question": question_label, "question_id": qid, "cypher": cypher,
            "rows": rows, "answer": answer_text, "grounded": True, "source": source}


# --------------------------------------------------------------------------- auth + credits

class SignupRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


def _require_backend() -> None:
    if not bb.is_configured():
        raise HTTPException(503, "Butterbase backend not configured "
                                 "(set BUTTERBASE_API_URL, BUTTERBASE_APP_ID, BUTTERBASE_API_KEY).")


async def _session_payload(auth: dict) -> dict:
    """Shape a Butterbase login/signup response + grant the one-time bonus + attach credit balance."""
    user = auth.get("user", {})
    uid = _uid(user)
    balance = await bb.ensure_signup_bonus(uid) if uid else 0
    return {
        "access_token": auth.get("access_token"),
        "refresh_token": auth.get("refresh_token"),
        "expires_in": auth.get("expires_in"),
        "user": user,
        "credits": balance,
    }


@app.post("/auth/signup")
async def auth_signup(req: SignupRequest) -> dict:
    _require_backend()
    try:
        await bb.signup(req.email, req.password, req.display_name)
        auth = await bb.login(req.email, req.password)   # sign the new user straight in
    except bb.ButterbaseBackendError as exc:
        raise HTTPException(400, f"Signup failed: {exc}") from exc
    return await _session_payload(auth)


@app.post("/auth/login")
async def auth_login(req: LoginRequest) -> dict:
    _require_backend()
    try:
        auth = await bb.login(req.email, req.password)
    except bb.ButterbaseBackendError as exc:
        raise HTTPException(401, "Invalid email or password.") from exc
    return await _session_payload(auth)


@app.get("/auth/me")
async def auth_me(authorization: str | None = Header(default=None)) -> dict:
    _require_backend()
    user = await bb.user_from_token(_bearer(authorization))
    if not user:
        raise HTTPException(401, "Not signed in.")
    uid = _uid(user)
    return {"user": user, "credits": await bb.credit_balance(uid)}


@app.get("/credits")
async def credits(authorization: str | None = Header(default=None)) -> dict:
    _require_backend()
    user = await bb.user_from_token(_bearer(authorization))
    if not user:
        raise HTTPException(401, "Not signed in.")
    uid = _uid(user)
    return {"balance": await bb.credit_balance(uid), "history": await bb.credit_history(uid),
            "pack_size": bb.CREDIT_PACK, "run_cost": bb.RUN_COST}


@app.post("/credits/buy")
async def credits_buy(authorization: str | None = Header(default=None)) -> dict:
    """Buy a pack of sim credits. (Ledger-only demo: grants a pack immediately with a synthetic
    order id. Swap this for Butterbase Stripe Connect checkout to charge real money.)"""
    _require_backend()
    user = await bb.user_from_token(_bearer(authorization))
    if not user:
        raise HTTPException(401, "Not signed in.")
    uid = _uid(user)
    order_id = f"simpack-{bb.CREDIT_PACK}"
    balance = await bb.grant_credits(uid, bb.CREDIT_PACK, "purchase", order_id=order_id)
    return {"balance": balance, "granted": bb.CREDIT_PACK, "order_id": order_id}


# --------------------------------------------------------------------------- history

@app.get("/runs")
async def runs_list(authorization: str | None = Header(default=None)) -> list[dict]:
    _require_backend()
    user = await bb.user_from_token(_bearer(authorization))
    if not user:
        raise HTTPException(401, "Not signed in.")
    return await bb.list_runs(user_id=_uid(user))


@app.get("/runs/{run_id}")
async def run_detail(run_id: str) -> dict:
    _require_backend()
    row = await bb.get_run(run_id)
    if not row:
        raise HTTPException(404, f"no run '{run_id}'")
    return row


@app.get("/runs/{run_id}/asks")
async def run_asks(run_id: str) -> list[dict]:
    _require_backend()
    return await bb.list_asks(run_id)
