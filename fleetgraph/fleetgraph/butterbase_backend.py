"""butterbase_backend.py — FleetGraph's data / auth / credits layer over Butterbase.

Everything FleetGraph produces is persisted in the Butterbase app (app_hbas8u7mao1y):

  * scenarios      — the catalog, AI-generated, and user-saved scenario specs
  * runs           — one row per simulation, with a lean summary + counts
  * asks           — every graph question asked against a run, with its answer
  * credit_ledger  — the "sim credits" economy (signup bonus, buy, spend-per-run)

and end-user auth (email/password -> JWT) is proxied to Butterbase's /auth endpoints.

URL layout (all derived from two env vars):
    BUTTERBASE_API_URL   origin, e.g. https://api.butterbase.ai
    BUTTERBASE_APP_ID    app id,  e.g. app_hbas8u7mao1y
      data / billing  ->  {API_URL}/v1/{APP_ID}/...
      auth            ->  {API_URL}/auth/{APP_ID}/...
    BUTTERBASE_API_KEY   bb_sk_ service key (butterbase_service role — bypasses RLS on writes)

Persistence is best-effort by contract: callers (api.py) wrap save_* in try/except so a
Butterbase hiccup never breaks the core sim/graph loop. Auth and credit calls DO raise, because
those results are load-bearing for the feature that invoked them.
"""

from __future__ import annotations

import os

import httpx

# --- the sim-credits economy ------------------------------------------------
SIGNUP_BONUS = 5     # free credits granted once, on first signup/login
CREDIT_PACK = 20     # credits granted per "buy" (the simulated purchase)
RUN_COST = 1         # credits a logged-in user spends per simulation

_TIMEOUT = 20.0


class ButterbaseBackendError(RuntimeError):
    pass


# --------------------------------------------------------------------------- config

def _api_url() -> str | None:
    return os.environ.get("BUTTERBASE_API_URL")


def _app_id() -> str | None:
    return os.environ.get("BUTTERBASE_APP_ID")


def _api_key() -> str | None:
    return os.environ.get("BUTTERBASE_API_KEY")


def is_configured() -> bool:
    return bool(_api_url() and _app_id() and _api_key())


def _app_base() -> str:
    return f"{_api_url().rstrip('/')}/v1/{_app_id()}"


def _auth_base() -> str:
    return f"{_api_url().rstrip('/')}/auth/{_app_id()}"


def _svc_headers() -> dict:
    return {"Authorization": f"Bearer {_api_key()}", "content-type": "application/json"}


def _require() -> None:
    if not is_configured():
        raise ButterbaseBackendError(
            "Butterbase backend not configured — set BUTTERBASE_API_URL, BUTTERBASE_APP_ID, "
            "BUTTERBASE_API_KEY."
        )


# --------------------------------------------------------------------------- REST core

async def _request(method: str, url: str, *, headers: dict, json=None, params=None):
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.request(method, url, headers=headers, json=json, params=params)
    if r.status_code >= 400:
        raise ButterbaseBackendError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code == 204 or not r.content:
        return None
    return r.json()


async def _get(table: str, *, params: dict | None = None):
    _require()
    return await _request("GET", f"{_app_base()}/{table}", headers=_svc_headers(), params=params)


async def _insert(table: str, data: dict) -> dict:
    _require()
    row = await _request("POST", f"{_app_base()}/{table}", headers=_svc_headers(), json=data)
    # The data API returns the created row (object) — normalise if wrapped in a list.
    if isinstance(row, list):
        return row[0] if row else {}
    return row or {}


# --------------------------------------------------------------------------- scenarios

async def save_scenario(*, name: str, spec: dict, description: str | None = None,
                        source: str = "custom", user_id: str | None = None) -> dict:
    return await _insert("scenarios", {
        "name": name, "description": description, "source": source,
        "spec": spec, "user_id": user_id,
    })


async def list_scenarios(*, user_id: str | None = None, limit: int = 50) -> list[dict]:
    params = {"select": "id,name,description,source,created_at",
              "order": "created_at.desc", "limit": str(limit)}
    if user_id:
        params["user_id"] = f"eq.{user_id}"
    return await _get("scenarios", params=params) or []


# --------------------------------------------------------------------------- runs

async def save_run(*, scenario_name: str, neo4j_run_id: str, summary: dict,
                   event_count: int, brake_count: int, relay_count: int,
                   duration_s: float | None = None, tick_count: int | None = None,
                   synced_neo4j: bool = False, scenario_id: str | None = None,
                   user_id: str | None = None) -> dict:
    return await _insert("runs", {
        "scenario_name": scenario_name, "neo4j_run_id": neo4j_run_id,
        "summary": summary, "event_count": event_count, "brake_count": brake_count,
        "relay_count": relay_count, "duration_s": duration_s, "tick_count": tick_count,
        "synced_neo4j": synced_neo4j, "scenario_id": scenario_id, "user_id": user_id,
    })


async def list_runs(*, user_id: str | None = None, limit: int = 25) -> list[dict]:
    params = {"select": "id,scenario_name,neo4j_run_id,event_count,brake_count,relay_count,"
                        "synced_neo4j,created_at",
              "order": "created_at.desc", "limit": str(limit)}
    if user_id:
        params["user_id"] = f"eq.{user_id}"
    return await _get("runs", params=params) or []


async def get_run(run_id: str) -> dict | None:
    rows = await _get("runs", params={"id": f"eq.{run_id}", "limit": "1"})
    return rows[0] if rows else None


async def latest_run_uuid(neo4j_run_id: str, *, user_id: str | None = None) -> str | None:
    """Map a Neo4j run label ('run-fog-...') to the most recent Butterbase runs.id (uuid)."""
    params = {"select": "id", "neo4j_run_id": f"eq.{neo4j_run_id}",
              "order": "created_at.desc", "limit": "1"}
    if user_id:
        params["user_id"] = f"eq.{user_id}"
    rows = await _get("runs", params=params)
    return rows[0]["id"] if rows else None


# --------------------------------------------------------------------------- asks

async def save_ask(*, question: str, run_id: str | None = None, cypher: str | None = None,
                   rows: list | None = None, answer: str | None = None,
                   user_id: str | None = None) -> dict:
    return await _insert("asks", {
        "question": question, "run_id": run_id, "cypher": cypher,
        "rows": rows, "answer": answer, "user_id": user_id,
    })


async def list_asks(run_id: str) -> list[dict]:
    return await _get("asks", params={
        "run_id": f"eq.{run_id}", "select": "question,answer,cypher,created_at",
        "order": "created_at.asc"}) or []


# --------------------------------------------------------------------------- credits

async def credit_balance(user_id: str) -> int:
    rows = await _get("credit_ledger", params={
        "user_id": f"eq.{user_id}", "select": "delta"}) or []
    return sum(int(r["delta"]) for r in rows)


async def credit_history(user_id: str, *, limit: int = 25) -> list[dict]:
    return await _get("credit_ledger", params={
        "user_id": f"eq.{user_id}", "select": "delta,reason,order_id,balance_after,created_at",
        "order": "created_at.desc", "limit": str(limit)}) or []


async def grant_credits(user_id: str, amount: int, reason: str,
                        order_id: str | None = None) -> int:
    """Add `amount` credits; returns the new balance."""
    new_balance = await credit_balance(user_id) + amount
    await _insert("credit_ledger", {
        "user_id": user_id, "delta": amount, "reason": reason,
        "order_id": order_id, "balance_after": new_balance})
    return new_balance


async def spend_credit(user_id: str, amount: int = RUN_COST,
                       reason: str = "run_spend") -> int | None:
    """Spend `amount` credits if the balance allows. Returns the new balance, or None if too low.

    Not transactional — fine for a single-user demo; a production build would do this in a
    Butterbase function with a row lock.
    """
    balance = await credit_balance(user_id)
    if balance < amount:
        return None
    new_balance = balance - amount
    await _insert("credit_ledger", {
        "user_id": user_id, "delta": -amount, "reason": reason,
        "order_id": None, "balance_after": new_balance})
    return new_balance


async def ensure_signup_bonus(user_id: str) -> int:
    """Grant the one-time signup bonus if the user has no ledger history yet. Returns balance."""
    existing = await _get("credit_ledger", params={
        "user_id": f"eq.{user_id}", "select": "id", "limit": "1"})
    if existing:
        return await credit_balance(user_id)
    return await grant_credits(user_id, SIGNUP_BONUS, "signup_bonus")


# --------------------------------------------------------------------------- auth (proxy)

async def signup(email: str, password: str, display_name: str | None = None) -> dict:
    _require()
    return await _request("POST", f"{_auth_base()}/signup",
                          headers={"content-type": "application/json"},
                          json={"email": email, "password": password,
                                "display_name": display_name})


async def login(email: str, password: str) -> dict:
    _require()
    return await _request("POST", f"{_auth_base()}/login",
                          headers={"content-type": "application/json"},
                          json={"email": email, "password": password})


async def me(access_token: str) -> dict:
    """Verify an end-user access token and return their profile (id, email, display_name)."""
    _require()
    return await _request("GET", f"{_auth_base()}/me",
                          headers={"Authorization": f"Bearer {access_token}"})


async def user_from_token(access_token: str | None) -> dict | None:
    """Best-effort: resolve the end-user from a Bearer token, or None if absent/invalid."""
    if not access_token or not is_configured():
        return None
    try:
        return await me(access_token)
    except ButterbaseBackendError:
        return None
