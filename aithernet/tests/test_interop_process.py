"""Stage 14D real-OS-process acceptance test for the external-agent gateway.

Four real processes — the Aithernet node, this test as the external AGENT, a webhook RECEIVER
process, and a WebSocket CLIENT — prove the end-to-end interoperability contract:

  provision -> credential -> verify endpoint -> signed idempotent submission -> durable
  acceptance receipt -> mission-status + terminal callbacks -> signature + digest verified ->
  acknowledge -> idempotent resubmission returns the same mission -> stop the receiver ->
  produce another event -> pending/retry persists -> SIGKILL the node -> restart against the
  same DB/identity/state -> restart the receiver -> pending delivery resumes with NO logical
  duplicate -> WebSocket connect / disconnect-during-production / reconnect+replay -> no orphans.

Runs only the local loopback network (no public internet, no hardware). It is process-heavy
and excluded from the ordinary suite by file naming (run it explicitly).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "examples" / "external_agent"))
from client import build_auth_headers, generate_keypair  # noqa: E402

NODE_LAUNCHER = _HERE / "_node_proc.py"
RECEIVER_LAUNCHER = _HERE / "_webhook_receiver.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, deadline: float, *, ready_key: str | None = None) -> bool:
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=3)
            if r.status_code == 200 and (ready_key is None or r.json().get("status") == ready_key):
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


def _launch_node(state: Path, port: int, log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "NODE_ID": "ea-node", "PORT": str(port),
        "DB_PATH": str(state / "db" / "aithernet.db"),
        "IDENTITY_DIR": str(state / "identity"), "STORE_DIR": str(state / "artifacts"),
        "MISSION_EXEC": "1", "NODE_ROLE": "requester", "EA_ENABLED": "1",
        "HW_ENABLED": "0",
    })
    (state / "db").mkdir(parents=True, exist_ok=True)
    handle = log.open("w")
    return subprocess.Popen(
        [sys.executable, str(NODE_LAUNCHER)], env=env, cwd=str(state.parent),
        stdout=handle, stderr=subprocess.STDOUT,
    )


def _launch_receiver(port: int, record: Path, node_pubkey: str, log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"PORT": str(port), "RECORD_FILE": str(record), "NODE_PUBKEY": node_pubkey})
    handle = log.open("w")
    return subprocess.Popen(
        [sys.executable, str(RECEIVER_LAUNCHER)], env=env,
        stdout=handle, stderr=subprocess.STDOUT,
    )


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    with __import__("contextlib").suppress(Exception):
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)


class _Agent:
    """Minimal signed-request external agent driver over real HTTP."""

    def __init__(self, base: str, agent_id: str, key_id: str, private):
        self.base, self.agent_id, self.key_id, self.private = base, agent_id, key_id, private

    def _h(self, method: str, target: str, body: bytes) -> dict:
        return build_auth_headers(private=self.private, agent_id=self.agent_id, key_id=self.key_id,
                                  method=method, target=target, body=body)

    def submit(self, objective: str, key: str) -> httpx.Response:
        target = "/agent-api/missions"
        body = json.dumps({"objective": objective, "idempotency_key": key}).encode()
        return httpx.post(self.base + target, content=body, headers=self._h("POST", target, body),
                          timeout=10)

    def ack(self, message_id: str) -> httpx.Response:
        target = "/agent-api/receipts"
        body = json.dumps({"message_id": message_id}).encode()
        return httpx.post(self.base + target, content=body, headers=self._h("POST", target, body),
                          timeout=10)

    def ws_hello(self, subscription_id: str) -> dict:
        auth = build_auth_headers(private=self.private, agent_id=self.agent_id, key_id=self.key_id,
                                  method="GET", target="/agent-api/ws", body=b"")
        return {"type": "hello", "subscription_id": subscription_id, "auth": auth}


def test_external_agent_process_acceptance(tmp_path):
    state = tmp_path / "node"
    state.mkdir()
    node_port, rcv_port = _free_port(), _free_port()
    base = f"http://127.0.0.1:{node_port}"
    record = tmp_path / "callbacks.jsonl"
    node = receiver = None
    try:
        # 1) Launch the node and wait for readiness.
        node = _launch_node(state, node_port, tmp_path / "node.log")
        assert _wait_http(f"{base}/health/ready", time.monotonic() + 40, ready_key="ready"), \
            "node did not become ready"
        httpx.post(f"{base}/identity/initialize", timeout=10)  # idempotent
        node_pubkey = httpx.get(f"{base}/identity/public", timeout=5).json()["public_key"]

        # 2) Launch the webhook receiver (knows the node public key to verify callbacks).
        receiver = _launch_receiver(rcv_port, record, node_pubkey, tmp_path / "rcv.log")
        assert _wait_http(f"http://127.0.0.1:{rcv_port}/health", time.monotonic() + 20,
                          ready_key="ok"), "receiver did not start"

        # 3) Provision an external agent with its Ed25519 public key.
        private, public_b64 = generate_keypair()
        created = httpx.post(f"{base}/external-agents",
                             json={"display_name": "robo", "public_key": public_b64},
                             timeout=10)
        assert created.status_code == 201, created.text
        agent_id = created.json()["agent_id"]
        key_id = created.json()["fingerprint"]
        agent = _Agent(base, agent_id, key_id, private)

        # 4) Register + (auto-)verify a callback endpoint and a webhook subscription.
        hook_url = f"http://127.0.0.1:{rcv_port}/hook"
        ep = httpx.post(f"{base}/external-agents/{agent_id}/endpoints",
                        json={"url": hook_url}, timeout=10)
        assert ep.status_code == 201, ep.text
        assert ep.json()["status"] == "verified"
        endpoint_id = ep.json()["endpoint_id"]
        sub = httpx.post(f"{base}/external-agents/{agent_id}/subscriptions",
                         json={"delivery_mode": "webhook", "endpoint_id": endpoint_id,
                               "filters": {}}, timeout=10)
        assert sub.status_code == 201, sub.text

        # Also create a websocket subscription for the WS portion later.
        ws_sub = httpx.post(f"{base}/external-agents/{agent_id}/subscriptions",
                            json={"delivery_mode": "websocket", "filters": {}}, timeout=10)
        ws_sub_id = ws_sub.json()["subscription_id"]

        # 5) Submit a signed mission and get a durable acceptance receipt.
        r = agent.submit("scan 433 MHz then report", "key-A")
        assert r.status_code == 202, r.text
        receipt = r.json()
        mission_id = receipt["mission_id"]
        assert receipt["status"] == "accepted"

        # 6) The mission completes locally (requester + no peers) -> accepted + completed callbacks.
        def have(event_type: str, mid: str) -> bool:
            return any(rec["event_type"] == event_type and rec["mission_id"] == mid
                       for rec in _records(record))

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (
            have("agent.mission.accepted", mission_id)
            and have("agent.mission.completed", mission_id)
        ):
            time.sleep(0.3)
        recs = _records(record)
        assert have("agent.mission.accepted", mission_id), recs
        assert have("agent.mission.completed", mission_id), recs
        # 9) Every recorded callback verified its signature + content digest.
        assert all(rec["verified"] for rec in recs), recs

        # 10) Acknowledge the delivered messages.
        for rec in recs:
            assert agent.ack(rec["message_id"]).status_code == 200

        # 11) Idempotent resubmission returns the SAME mission.
        again = agent.submit("scan 433 MHz then report", "key-A")
        assert again.status_code == 202
        assert again.json()["mission_id"] == mission_id

        # 12-14) Stop the receiver, produce another event, prove pending/retry persists.
        _kill(receiver)
        receiver = None
        r2 = agent.submit("second mission", "key-B")
        mission2 = r2.json()["mission_id"]
        # Give the worker time to attempt + fail delivery (receiver down) and schedule a retry.
        time.sleep(3)
        deliveries = httpx.get(f"{base}/external-agents/{agent_id}/deliveries", timeout=10).json()
        pending = [d for d in deliveries
                   if d["mission_id"] == mission2 and d["status"] in ("pending", "retry_wait")]
        assert pending, [d for d in deliveries if d["mission_id"] == mission2]

        # 15-16) SIGKILL the node, then restart it against the SAME db/identity/state.
        _kill(node)
        node = _launch_node(state, node_port, tmp_path / "node2.log")
        assert _wait_http(f"{base}/health/ready", time.monotonic() + 40, ready_key="ready")

        # 17) Restart the receiver (durable dedup re-seeded from the record file).
        receiver = _launch_receiver(rcv_port, record, node_pubkey, tmp_path / "rcv2.log")
        assert _wait_http(f"http://127.0.0.1:{rcv_port}/health", time.monotonic() + 20,
                          ready_key="ok")

        # 18) Pending delivery resumes after both restarts.
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not have("agent.mission.accepted", mission2):
            time.sleep(0.3)
        assert have("agent.mission.accepted", mission2), _records(record)

        # 19) No logical duplicate: each message id appears exactly once in the record.
        ids = [rec["message_id"] for rec in _records(record)]
        assert len(ids) == len(set(ids)), ids

        # 20-24) WebSocket: connect, disconnect during production, reconnect + replay.
        asyncio.run(_websocket_flow(base, agent, ws_sub_id))

    finally:
        # 25) No orphan processes remain.
        _kill(receiver)
        _kill(node)


async def _websocket_flow(base: str, agent: _Agent, subscription_id: str) -> None:
    import websockets

    ws_url = base.replace("http://", "ws://") + "/agent-api/ws"

    # Connect, subscribe, submit a mission, receive at least one live event, capture its sequence.
    last_seq = 0
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps(agent.ws_hello(subscription_id)))
        assert json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] == "subscribed"
        # Produce events from a different thread-safe call (real HTTP submit).
        agent.submit("ws mission one", "ws-A")
        frame = json.loads(await asyncio.wait_for(ws.recv(), 20))
        assert frame["type"] == "event"
        last_seq = frame["event"]["sequence"]
        # Disconnect WITHOUT acking the rest (context exit closes the socket).

    # Produce more events while disconnected.
    agent.submit("ws mission two", "ws-B")
    await asyncio.sleep(2)

    # Reconnect and replay from the last received sequence; expect ordered, newer events.
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps(agent.ws_hello(subscription_id)))
        assert json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] == "subscribed"
        await ws.send(json.dumps({"type": "replay", "after_sequence": last_seq}))
        seqs: list[int] = []
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if frame["type"] == "replay_complete":
                break
            if frame["type"] == "event":
                seqs.append(frame["event"]["sequence"])
        assert seqs == sorted(seqs)
        assert all(s > last_seq for s in seqs)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-x", "-vv"]))
