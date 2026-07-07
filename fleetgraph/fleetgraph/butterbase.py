"""Butterbase integration — the AI gateway (OpenAI-compatible) powering FleetGraph's LLM features.

Butterbase is FleetGraph's backend-for-AI: its **AI gateway** gives one OpenAI-compatible endpoint
for Claude/GPT/Gemini (so we manage no per-provider keys). We use it for two load-bearing things:
  1. natural-language -> Scenario JSON (the "describe a scenario" box), and
  2. phrasing the graph agent's answer in plain English.

Config comes from env (set these to your Butterbase project's gateway):
  BUTTERBASE_GATEWAY_URL   e.g. https://api.butterbase.ai/gateway/v1   (the OpenAI-compatible base)
  BUTTERBASE_API_KEY       your bb_sk_... key
  BUTTERBASE_MODEL         a model the gateway exposes (e.g. gpt-4o-mini / claude-sonnet-4)

Auth, database and payment are provisioned through the Butterbase MCP tools + project dashboard
(see butterbase_backend.py for the data-access layer).
"""

from __future__ import annotations

import json
import os
import re

import httpx


def gateway_config() -> tuple[str | None, str | None, str]:
    return (
        os.environ.get("BUTTERBASE_GATEWAY_URL"),
        os.environ.get("BUTTERBASE_API_KEY"),
        os.environ.get("BUTTERBASE_MODEL", "gpt-4o-mini"),
    )


def is_configured() -> bool:
    base, key, _ = gateway_config()
    return bool(base and key)


class ButterbaseError(RuntimeError):
    pass


async def chat(messages: list[dict], *, temperature: float = 0.2, max_tokens: int = 2000) -> str:
    """One OpenAI-compatible chat completion through the Butterbase AI gateway."""
    base, key, model = gateway_config()
    if not (base and key):
        raise ButterbaseError(
            "Butterbase AI gateway not configured — set BUTTERBASE_GATEWAY_URL and BUTTERBASE_API_KEY."
        )
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
            json={"model": model, "messages": messages, "temperature": temperature,
                  "max_tokens": max_tokens},
        )
    if r.status_code >= 400:
        raise ButterbaseError(f"Butterbase gateway HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    return data["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response (tolerates ```json fences)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start = raw.find("{")
    if start == -1:
        raise ButterbaseError("Model did not return JSON.")
    depth = 0
    for i in range(start, len(raw)):
        depth += 1 if raw[i] == "{" else (-1 if raw[i] == "}" else 0)
        if depth == 0:
            return json.loads(raw[start:i + 1])
    raise ButterbaseError("Unbalanced JSON in model response.")


SCENARIO_SYSTEM = """You generate FleetGraph driving-scenario JSON for a 2-way road (lanes run along \
x). Output ONLY a JSON object, no prose.

Lanes (world.lanes, index order): 0 = north shoulder (dir -1), 1 = westbound drive (dir -1),
2 = eastbound drive (dir +1), 3 = south shoulder (dir +1). Cars going east use lane 2; west use 1;
parked/relay cars sit on shoulders (0 or 3). x ranges 0..world.width.

Schema:
{
  "name": "<kebab-name>",
  "description": "<one sentence>",
  "duration_s": 16, "tick_hz": 5, "seed": 7,
  "world": {"width": 220, "height": 44, "fog_density": 0.0-1.0,
            "lidar_range_m": 55, "brake_reaction_m": 30, "awareness_horizon_m": 160},
  "actors": [
    {"id":"car-A","kind":"car","label":"Ambulance","lane":2,"start_x":10,"speed_mps":12,"has_radio":true},
    {"id":"truck-1","kind":"truck","label":"Truck","lane":3,"start_x":120,"speed_mps":0,"has_radio":true},
    {"id":"ped-1","kind":"pedestrian","label":"Pedestrian","is_hazard":true,"has_radio":false,
     "path":[[140,8],[140,36]],"speed_mps":2,"spawn_t":2},
    {"id":"sig-1","kind":"signal","label":"Light","lane":2,"start_x":150,"green_s":15,"yellow_s":2,"red_s":5,"affects_dir":1}
  ],
  "events": [{"t":0,"kind":"jammer_on","pos":[100,22],"radius":40,"value":0.85}]
}

Rules: pedestrians/obstacles use "path" (a crossing pedestrian goes across the road, e.g. y 8->36);
cars/trucks/signals use lane + start_x. Give cars radio (has_radio:true) unless they're passive.
Heavy fog (>=0.6) blinds lidar so cars rely on the V2V mesh. Keep it to <= 8 actors."""


async def generate_scenario(prompt: str) -> dict:
    """Turn a natural-language description into a Scenario dict via the Butterbase gateway."""
    content = await chat(
        [{"role": "system", "content": SCENARIO_SYSTEM},
         {"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=1800,
    )
    return extract_json(content)


async def phrase_answer(question: str, rows: list[dict]) -> str:
    """Phrase graph query rows as a one-line plain-English answer (grounded in the rows)."""
    content = await chat(
        [{"role": "system", "content": "You explain graph-query results about a V2V driving sim in "
          "ONE concise sentence. Use ONLY the given rows; name the relay path if present. No preamble."},
         {"role": "user", "content": f"Question: {question}\nRows: {json.dumps(rows)}"}],
        temperature=0.1, max_tokens=200,
    )
    return content.strip()
