"""A standalone webhook-receiver process for the Stage 14D process acceptance test.

It is the "external callback receiver": a tiny uvicorn app that records every signed webhook
to a JSON-lines file, deduplicating by ``message_id`` so a duplicate at-least-once delivery
triggers no second logical action. It verifies the callback signature against the node's
published Ed25519 public key and records the verdict. State (port, record file, node public
key) comes from the environment so the test can stop and restart it independently of the node.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI, Request

RECORD_FILE = Path(os.environ["RECORD_FILE"])
NODE_PUBKEY = os.environ.get("NODE_PUBKEY", "")
_seen: set[str] = set()


def _canonical(obj: dict) -> bytes:
    return json.dumps(
        {k: v for k, v in obj.items() if k != "signature"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _verify(headers: dict, body: bytes, target: str) -> bool:
    low = {k.lower(): v for k, v in headers.items()}
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if low.get("x-aithernet-content-digest") != digest:
        return False
    message = _canonical({
        "agent_id": low.get("x-aithernet-agent", ""),
        "key_id": low.get("x-aithernet-key-id", ""),
        "method": "POST", "target": target,
        "timestamp": low.get("x-aithernet-timestamp", ""),
        "nonce": low.get("x-aithernet-nonce", ""),
        "content_digest": low.get("x-aithernet-content-digest", ""),
    })
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(NODE_PUBKEY))
        pub.verify(base64.b64decode(low.get("x-aithernet-signature", "")), message)
        return True
    except Exception:  # noqa: BLE001
        return False


app = FastAPI()


@app.post("/hook")
async def hook(request: Request) -> dict:
    body = await request.body()
    envelope = json.loads(body)
    message_id = envelope.get("message_id")
    duplicate = message_id in _seen
    verified = _verify(dict(request.headers), body, "/hook")
    if not duplicate:
        _seen.add(message_id)
        with RECORD_FILE.open("a") as fh:
            fh.write(json.dumps({
                "message_id": message_id,
                "event_type": envelope.get("event_type"),
                "sequence": envelope.get("sequence"),
                "mission_id": envelope.get("mission_id"),
                "payload_digest": envelope.get("payload_digest"),
                "verified": verified,
            }) + "\n")
    # Always 200: at-least-once transport delivery acknowledged regardless of dedup.
    return {"ok": True, "duplicate": duplicate}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def main() -> None:
    if not RECORD_FILE.exists():
        RECORD_FILE.touch()
    # Durable dedup across receiver restarts: re-seed seen ids from the record file.
    for line in RECORD_FILE.read_text().splitlines():
        if line.strip():
            _seen.add(json.loads(line).get("message_id"))
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]), log_level="warning")


if __name__ == "__main__":
    main()
