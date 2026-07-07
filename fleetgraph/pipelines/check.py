#!/usr/bin/env python3
"""check.py — verify the RocketRide "fleet-analyst" setup.

Run from the pipelines/ dir:  python check.py
  - always: reports env config + validates fleet-analyst.pipe structure (offline-safe).
  - if ROCKETRIDE_APIKEY is set: connects, validates the pipeline against the live server, and
    prints the llm_openai profile schema so we can confirm the Butterbase-gateway base-URL field
    name (the one field the offline docs don't pin down), plus current deployments.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE = os.path.join(HERE, "fleet-analyst.pipe")
OK, BAD, TODO = "✅", "❌", "→"


def load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _mask(v: str | None) -> str:
    if not v:
        return "(unset)"
    return v[:8] + "…" if len(v) > 10 else "set"


def check_env() -> bool:
    print("— environment —")
    uri = os.environ.get("ROCKETRIDE_URI")
    key = os.environ.get("ROCKETRIDE_APIKEY")
    print(f"  {OK if uri else TODO} ROCKETRIDE_URI    = {uri or '(unset — defaults to https://api.rocketride.ai)'}")
    print(f"  {OK if key else TODO} ROCKETRIDE_APIKEY = {_mask(key)}")
    # LLM creds the pipeline needs (injected from BUTTERBASE_* by the app at runtime)
    gw = os.environ.get("ROCKETRIDE_GATEWAY_URL") or os.environ.get("BUTTERBASE_GATEWAY_URL")
    gk = os.environ.get("ROCKETRIDE_GATEWAY_KEY") or os.environ.get("BUTTERBASE_API_KEY")
    gm = os.environ.get("ROCKETRIDE_MODEL") or os.environ.get("BUTTERBASE_MODEL")
    print(f"  {OK if gw else TODO} gateway url  = {gw or '(unset)'}")
    print(f"  {OK if gk else TODO} gateway key  = {_mask(gk)}")
    print(f"  {OK if gm else TODO} model        = {gm or '(unset)'}")
    return bool(gw and gk)


def check_pipe() -> bool:
    print("\n— pipeline (fleet-analyst.pipe) —")
    try:
        pipe = json.load(open(PIPE, encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  {BAD} could not parse: {exc}")
        return False
    comps = {c["id"]: c for c in pipe.get("components", [])}
    providers = [c.get("provider") for c in pipe.get("components", [])]
    print(f"  {OK} project_id = {pipe.get('project_id')}")
    print(f"  {OK} source     = {pipe.get('source')}")
    print(f"  {OK} components = {' → '.join(providers)}")
    expect = ["chat", "llm_openai", "response_answers"]
    ok = providers == expect and pipe.get("source") in comps
    # lane wiring sanity
    for c in pipe.get("components", []):
        for wire in c.get("input", []):
            print(f"       {c['id']} ← lane '{wire.get('lane')}' from '{wire.get('from')}'")
    print(f"  {OK if ok else BAD} structure {'valid (chat → llm_openai → response_answers)' if ok else 'UNEXPECTED'}")
    return ok


async def check_server() -> None:
    print("\n— live server —")
    try:
        from rocketride import RocketRideClient
    except Exception as exc:  # noqa: BLE001
        print(f"  {BAD} rocketride SDK not importable: {exc}")
        return
    client = RocketRideClient()
    try:
        await client.connect()
        print(f"  {OK} connected · authenticated={client.is_authenticated()}")
        try:
            pipe = json.load(open(PIPE, encoding="utf-8"))
            res = await client.validate(pipe, source=pipe.get("source"))
            print(f"  {OK} validate() → {res}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {BAD} validate() failed: {exc}")
            print(f"       {TODO} if it complains about the llm_openai 'endpoint' field, open the "
                  f"regenerated .rocketride/schema/llm_openai.json and use the real base-URL field name.")
        try:
            deps = await client.deploy.list()
            print(f"  {OK} deployments: {deps}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {TODO} deploy.list(): {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {BAD} connect failed: {exc}")
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    load_dotenv(os.path.join(HERE, ".env"))
    print("RocketRide fleet-analyst — setup check\n" + "=" * 40)
    env_ok = check_env()
    pipe_ok = check_pipe()
    if os.environ.get("ROCKETRIDE_APIKEY"):
        asyncio.run(check_server())
    else:
        print("\n[offline] No ROCKETRIDE_APIKEY — skipping live connect / validate / deploy.")
        print(f"  {TODO} Set ROCKETRIDE_URI + ROCKETRIDE_APIKEY (RocketRide VS Code extension, or")
        print( "        cloud.rocketride.ai), then re-run to validate against the server and confirm")
        print( "        the llm_openai base-URL field for the Butterbase gateway.")
    print("\n" + "=" * 40)
    print(f"pipeline structure: {'OK' if pipe_ok else 'FIX'} · gateway creds: {'OK' if env_ok else 'MISSING'}")
    return 0 if pipe_ok else 1


if __name__ == "__main__":
    sys.exit(main())
