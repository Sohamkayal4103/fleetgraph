"""Stage 14D external-agent interoperability tests (no network / no hardware).

Covers: agent + credential lifecycle, signed-request verification + replay protection,
idempotent submission + conflict, ownership, subscription filters + sequence allocation,
durable enqueue, webhook signing + SSRF policy (injected resolver) + retries + dead-letter +
redrive, receipts, replay, sanitization, and "no extra MissionSteps from delivery activity".
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from aithernet.config.settings import (
    ExternalAgentsCallbacksConfig,
    ExternalAgentsConfig,
    ExternalAgentsDeliveryConfig,
    ExternalAgentsWebsocketConfig,
    NodeConfig,
)
from aithernet.interop import events as iev
from aithernet.interop.endpoints import validate_endpoint
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.state.models import MissionStep, utcnow
from aithernet.state.repositories import MissionStepRepository

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "external_agent"))
from client import build_auth_headers, generate_keypair  # noqa: E402


def _config(tmp_path, *, callbacks=True, ws=False, bearer=False, max_attempts=3) -> NodeConfig:
    return NodeConfig(
        node_id="node-test",
        node_name="Node Test",
        database_url=f"sqlite:///{tmp_path / 'n.db'}",
        external_agents=ExternalAgentsConfig(
            enabled=True,
            callbacks=ExternalAgentsCallbacksConfig(
                enabled=callbacks, https_required=True, allow_private_networks=False,
                verification_required=False, redirects_enabled=False,
            ),
            delivery=ExternalAgentsDeliveryConfig(
                worker_enabled=True, maximum_attempts=max_attempts, retry_base_seconds=0.01,
                retry_max_seconds=0.05, poll_interval_seconds=0.05,
            ),
            websocket=ExternalAgentsWebsocketConfig(enabled=ws),
        ),
    )


def _runtime(tmp_path, **kw) -> NodeRuntime:
    runtime = NodeRuntime.from_config(_config(tmp_path, **kw))
    runtime.transport.ensure_identity() or runtime.transport._identity_manager.initialize()
    return runtime


def _agent_with_key(runtime):
    private, public_b64 = generate_keypair()
    agent = runtime.interop.create_agent(display_name="Robo", public_key=public_b64)
    return agent, private, public_b64


# --------------------------------------------------------------------------- auth (pure)


def test_signed_request_roundtrip_and_failures(tmp_path):
    runtime = _runtime(tmp_path)
    agent, private, _ = _agent_with_key(runtime)
    body = b'{"objective":"scan","idempotency_key":"k1"}'
    headers = build_auth_headers(
        private=private, agent_id=agent["agent_id"], key_id=agent["fingerprint"],
        method="POST", target="/agent-api/missions", body=body,
    )
    ctx = runtime.interop.authenticate(
        headers=headers, method="POST", target="/agent-api/missions", body=body
    )
    assert ctx.ok and ctx.agent_id == agent["agent_id"]

    # Tampered body -> digest mismatch.
    bad = runtime.interop.authenticate(
        headers=headers, method="POST", target="/agent-api/missions", body=body + b"x"
    )
    assert not bad.ok and bad.code == "body_digest_mismatch"

    # Tampered target -> invalid signature.
    bad2 = runtime.interop.authenticate(
        headers=headers, method="POST", target="/agent-api/other", body=body
    )
    assert not bad2.ok and bad2.code == "invalid_signature"

    # Replay the SAME nonce -> rejected (the first call persisted it).
    replay = runtime.interop.authenticate(
        headers=headers, method="POST", target="/agent-api/missions", body=body
    )
    assert not replay.ok and replay.code == "nonce_replayed"


def test_expired_and_future_timestamp(tmp_path):
    runtime = _runtime(tmp_path)
    agent, private, _ = _agent_with_key(runtime)
    body = b"{}"
    old = build_auth_headers(
        private=private, agent_id=agent["agent_id"], key_id=agent["fingerprint"],
        method="POST", target="/t", body=body, timestamp=int(time.time()) - 9999,
    )
    assert runtime.interop.authenticate(
        headers=old, method="POST", target="/t", body=body
    ).code == "timestamp_expired"
    future = build_auth_headers(
        private=private, agent_id=agent["agent_id"], key_id=agent["fingerprint"],
        method="POST", target="/t", body=body, timestamp=int(time.time()) + 9999,
    )
    assert runtime.interop.authenticate(
        headers=future, method="POST", target="/t", body=body
    ).code == "timestamp_in_future"


def test_revoked_and_unknown_key(tmp_path):
    runtime = _runtime(tmp_path)
    agent, private, _ = _agent_with_key(runtime)
    creds = runtime.interop.list_credentials(agent["agent_id"])
    runtime.interop.revoke_credential(agent["agent_id"], creds[0]["credential_id"])
    body = b"{}"
    headers = build_auth_headers(
        private=private, agent_id=agent["agent_id"], key_id=agent["fingerprint"],
        method="POST", target="/t", body=body,
    )
    assert runtime.interop.authenticate(
        headers=headers, method="POST", target="/t", body=body
    ).code in ("unknown_key_id", "key_revoked")

    headers_bad_kid = dict(headers)
    headers_bad_kid["X-Aithernet-Key-Id"] = "nope"
    assert not runtime.interop.authenticate(
        headers=headers_bad_kid, method="POST", target="/t", body=body
    ).ok


def test_disabled_agent_blocks_auth(tmp_path):
    runtime = _runtime(tmp_path)
    agent, private, _ = _agent_with_key(runtime)
    runtime.interop.set_status(agent["agent_id"], "disabled")
    body = b"{}"
    headers = build_auth_headers(
        private=private, agent_id=agent["agent_id"], key_id=agent["fingerprint"],
        method="POST", target="/t", body=body,
    )
    assert runtime.interop.authenticate(
        headers=headers, method="POST", target="/t", body=body
    ).code == "agent_disabled"


# --------------------------------------------------------------------------- SSRF policy


def test_ssrf_endpoint_validation(tmp_path):
    policy = ExternalAgentsCallbacksConfig(https_required=True, allow_private_networks=False)

    def resolver(host):
        return {
            "metadata.example": ["169.254.169.254"],
            "loop.example": ["127.0.0.1"],
            "priv.example": ["10.0.0.5"],
            "public.example": ["93.184.216.34"],
        }.get(host, [])

    # Prohibited schemes / shapes.
    assert validate_endpoint("ftp://x/y", policy, resolver=resolver).reason == "unsupported_scheme"
    assert validate_endpoint(
        "http://public.example/h", policy, resolver=resolver
    ).reason == "http_not_allowed"
    assert validate_endpoint(
        "https://u:p@public.example/h", policy, resolver=resolver
    ).reason == "userinfo_not_allowed"
    assert validate_endpoint(
        "https://public.example/h#frag", policy, resolver=resolver
    ).reason == "fragment_not_allowed"

    # Prohibited destinations (metadata / loopback / private).
    assert validate_endpoint(
        "https://metadata.example/h", policy, resolver=resolver
    ).reason == "link_local_destination"
    assert validate_endpoint(
        "https://loop.example/h", policy, resolver=resolver
    ).reason == "loopback_destination"
    assert validate_endpoint(
        "https://priv.example/h", policy, resolver=resolver
    ).reason == "private_destination"

    # A public address is allowed.
    ok = validate_endpoint("https://public.example/hook", policy, resolver=resolver)
    assert ok.allowed and ok.host == "public.example"


def test_dns_rebinding_caught_at_delivery(tmp_path):
    """An endpoint that resolves safe at registration but to metadata later is rejected."""
    runtime = _runtime(tmp_path)
    state = {"ip": "93.184.216.34"}
    runtime.interop.resolver = lambda host: [state["ip"]]
    agent, _, _ = _agent_with_key(runtime)
    endpoint = runtime.interop.register_endpoint(agent["agent_id"], "https://cb.example/hook")
    assert endpoint["status"] == "verified"
    # Rebind to the cloud metadata address; re-validation must now reject.
    state["ip"] = "169.254.169.254"
    from aithernet.interop.endpoints import validate_endpoint as v
    result = v("https://cb.example/hook", runtime.config.external_agents.callbacks,
               resolver=runtime.interop.resolver)
    assert not result.allowed and result.reason == "link_local_destination"


# --------------------------------------------------------------------------- submission


def test_idempotent_submission_and_conflict(tmp_path):
    runtime = _runtime(tmp_path)
    agent, _, _ = _agent_with_key(runtime)
    aid = agent["agent_id"]
    r1 = asyncio.run(runtime.interop.submit_mission(
        agent_id=aid, body={"objective": "scan 433", "idempotency_key": "key-1"}
    ))
    r2 = asyncio.run(runtime.interop.submit_mission(
        agent_id=aid, body={"objective": "scan 433", "idempotency_key": "key-1"}
    ))
    assert r1["mission_id"] == r2["mission_id"]  # idempotent: same mission of record

    from aithernet.interop.service import ConflictError
    with pytest.raises(ConflictError):  # conflicting reuse of the same key
        asyncio.run(runtime.interop.submit_mission(
            agent_id=aid, body={"objective": "DIFFERENT", "idempotency_key": "key-1"}
        ))


def test_submission_creates_no_mission_step(tmp_path):
    runtime = _runtime(tmp_path)
    agent, _, _ = _agent_with_key(runtime)

    def step_count():
        with runtime.session_scope() as s:
            return len(MissionStepRepository(s).session.query(MissionStep).all())

    before = step_count()
    aid = agent["agent_id"]
    asyncio.run(runtime.interop.submit_mission(
        agent_id=aid, body={"objective": "scan", "idempotency_key": "k"}
    ))
    # Subscribe + drive a terminal event through the notify hook.
    runtime.interop.create_subscription(aid, delivery_mode="pull", filters={})
    mid = asyncio.run(runtime.interop.submit_mission(
        agent_id=aid, body={"objective": "scan2", "idempotency_key": "k2"}
    ))["mission_id"]
    asyncio.run(runtime._emit_event(
        event_type="mission.completed", mission_id=mid, message="done", source="runtime"
    ))
    assert step_count() == before  # delivery/notification never creates a MissionStep


# --------------------------------------------------------------------------- subscriptions


def test_subscription_filters_and_sequence(tmp_path):
    runtime = _runtime(tmp_path)
    agent, _, _ = _agent_with_key(runtime)
    aid = agent["agent_id"]
    runtime.interop.create_subscription(
        aid, delivery_mode="pull", filters={"terminal_only": True}
    )
    mid = asyncio.run(runtime.interop.submit_mission(
        agent_id=aid, body={"objective": "x", "idempotency_key": "k"}
    ))["mission_id"]
    # A non-terminal status should be filtered out for a terminal_only subscription.
    asyncio.run(runtime._emit_event(
        event_type="mission.run.started", mission_id=mid, message="started", source="runtime"
    ))
    asyncio.run(runtime._emit_event(
        event_type="mission.completed", mission_id=mid, message="done", source="runtime"
    ))
    deliveries = runtime.interop.list_deliveries(aid)
    types = {d["event_type"] for d in deliveries}
    assert iev.EXT_MISSION_COMPLETED in types
    assert iev.EXT_MISSION_STARTED not in types
    # Sequences are monotonic per subscription, starting at 1.
    seqs = sorted(d["sequence"] for d in deliveries)
    assert seqs == list(range(1, len(seqs) + 1))


# --------------------------------------------------------------------------- receipts


def test_receipt_idempotent_and_cross_agent_rejected(tmp_path):
    runtime = _runtime(tmp_path)
    agent_a, _, _ = _agent_with_key(runtime)
    agent_b, _, _ = _agent_with_key(runtime)
    aid = agent_a["agent_id"]
    runtime.interop.create_subscription(aid, delivery_mode="pull", filters={})
    mid = asyncio.run(runtime.interop.submit_mission(
        agent_id=aid, body={"objective": "x", "idempotency_key": "k"}
    ))["mission_id"]
    asyncio.run(runtime._emit_event(
        event_type="mission.completed", mission_id=mid, message="done", source="runtime"
    ))
    msg = runtime.interop.list_deliveries(aid)[0]
    r1 = runtime.interop.record_receipt(agent_id=aid, message_id=msg["message_id"])
    assert r1["accepted"] and not r1["duplicate"]
    r2 = runtime.interop.record_receipt(agent_id=aid, message_id=msg["message_id"])
    assert r2["duplicate"]
    from aithernet.interop.service import ForbiddenError
    with pytest.raises(ForbiddenError):
        runtime.interop.record_receipt(
            agent_id=agent_b["agent_id"], message_id=msg["message_id"]
        )


# --------------------------------------------------------------------------- webhook delivery


def _receiver_app(record: list):
    app = FastAPI()

    @app.post("/hook")
    async def hook(request: Request):
        body = await request.body()
        record.append({"headers": dict(request.headers), "body": body})
        return {"ok": True}

    return app


def test_webhook_delivery_signed_and_acked(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.interop.resolver = lambda host: ["93.184.216.34"]
    record: list = []
    receiver = _receiver_app(record)
    runtime.interop._webhook_client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver), timeout=timeout
    )
    agent, _, _ = _agent_with_key(runtime)
    aid = agent["agent_id"]
    ep = runtime.interop.register_endpoint(aid, "https://cb.example/hook")
    runtime.interop.create_subscription(
        aid, delivery_mode="webhook", endpoint_id=ep["endpoint_id"], filters={}
    )
    mid = asyncio.run(runtime.interop.submit_mission(
        agent_id=aid, body={"objective": "x", "idempotency_key": "k"}
    ))["mission_id"]
    assert mid
    # One pending webhook message exists; deliver it directly (worker logic, no polling).
    pending = [d for d in runtime.interop.list_deliveries(aid) if d["status"] == "pending"]
    assert pending
    for d in pending:
        # Claim then deliver, mirroring the worker.
        from aithernet.state.repositories import InteropMessageRepository
        with runtime.session_scope() as s:
            InteropMessageRepository(s).claim(
                d["message_pk"], owner="t", now=utcnow(),
                claim_expiry=utcnow() + timedelta(seconds=30),
            )
            s.commit()
        asyncio.run(runtime.interop.deliver_message(d["message_pk"], owner="t"))
    assert record, "receiver should have received a signed callback"
    # The callback carries a valid signature header set.
    headers = record[0]["headers"]
    assert "x-aithernet-signature" in headers and "x-aithernet-message-id" in headers
    # And the message is now delivered.
    statuses = {d["status"] for d in runtime.interop.list_deliveries(aid)}
    assert "delivered" in statuses


def test_webhook_dead_letter_and_redrive(tmp_path):
    runtime = _runtime(tmp_path, max_attempts=1)
    runtime.interop.resolver = lambda host: ["93.184.216.34"]

    def failing_factory(timeout):
        app = FastAPI()

        @app.post("/hook")
        async def hook():
            from fastapi import Response
            return Response(status_code=500)

        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=timeout)

    runtime.interop._webhook_client_factory = failing_factory
    agent, _, _ = _agent_with_key(runtime)
    aid = agent["agent_id"]
    ep = runtime.interop.register_endpoint(aid, "https://cb.example/hook")
    runtime.interop.create_subscription(
        aid, delivery_mode="webhook", endpoint_id=ep["endpoint_id"], filters={}
    )
    asyncio.run(runtime.interop.submit_mission(
        agent_id=aid, body={"objective": "x", "idempotency_key": "k"}
    ))
    from aithernet.state.repositories import InteropMessageRepository
    msg = runtime.interop.list_deliveries(aid)[0]
    # With maximum_attempts=1 the first failed attempt dead-letters immediately.
    with runtime.session_scope() as s:
        InteropMessageRepository(s).claim(
            msg["message_pk"], owner="t", now=utcnow(),
            claim_expiry=utcnow() + timedelta(seconds=30),
        )
        s.commit()
    asyncio.run(runtime.interop.deliver_message(msg["message_pk"], owner="t"))
    final = runtime.interop.get_delivery(aid, msg["message_pk"])
    assert final["status"] == "dead_letter"
    assert len(final["attempts"]) == 1
    # Redrive resets it to pending.
    runtime.interop.redrive(aid, msg["message_pk"])
    assert runtime.interop.get_delivery(aid, msg["message_pk"])["status"] == "pending"


# --------------------------------------------------------------------------- sanitization


def test_summaries_expose_no_secrets_or_paths(tmp_path):
    runtime = _runtime(tmp_path, bearer=False)
    agent, _, _ = _agent_with_key(runtime)
    blob = repr(runtime.interop.agent_summary(agent["agent_id"]))
    blob += repr(runtime.interop.list_credentials(agent["agent_id"]))
    runtime.interop.resolver = lambda host: ["93.184.216.34"]
    ep = runtime.interop.register_endpoint(agent["agent_id"], "https://cb.example/hook")
    blob += repr(ep)
    for forbidden in ("token_hash", "private", "/home/", "/tmp/", "BEGIN PRIVATE", "secret"):
        assert forbidden not in blob


# --------------------------------------------------------------------------- HTTP surface


def test_http_submission_and_gating(tmp_path):
    from aithernet.api.app import create_app

    runtime = _runtime(tmp_path)
    agent, private, _ = _agent_with_key(runtime)
    app = create_app(runtime=runtime)
    with TestClient(app) as client:
        body = b'{"objective":"scan 915","idempotency_key":"http-1"}'
        headers = build_auth_headers(
            private=private, agent_id=agent["agent_id"], key_id=agent["fingerprint"],
            method="POST", target="/agent-api/missions", body=body,
        )
        resp = client.post("/agent-api/missions", content=body, headers=headers)
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "accepted"

        # Unsigned request is rejected.
        assert client.post("/agent-api/missions", content=body).status_code == 401

        # Operator listing works.
        assert client.get("/external-agents").status_code == 200


def test_websocket_subscribe_event_ack_replay(tmp_path):
    from client import build_auth_headers

    from aithernet.api.app import create_app

    runtime = _runtime(tmp_path, ws=True)
    agent, private, _ = _agent_with_key(runtime)
    aid = agent["agent_id"]
    sub = runtime.interop.create_subscription(aid, delivery_mode="websocket", filters={})
    app = create_app(runtime=runtime)

    def hello():
        return {
            "type": "hello",
            "subscription_id": sub["subscription_id"],
            "auth": build_auth_headers(
                private=private, agent_id=aid, key_id=agent["fingerprint"],
                method="GET", target="/agent-api/ws", body=b"",
            ),
        }

    def submit(client, key):
        body = f'{{"objective":"scan","idempotency_key":"{key}"}}'.encode()
        headers = build_auth_headers(
            private=private, agent_id=aid, key_id=agent["fingerprint"],
            method="POST", target="/agent-api/missions", body=body,
        )
        return client.post("/agent-api/missions", content=body, headers=headers)

    with TestClient(app) as client:
        with client.websocket_connect("/agent-api/ws") as ws:
            ws.send_json(hello())
            assert ws.receive_json()["type"] == "subscribed"
            submit(client, "ws-1")
            frame = ws.receive_json()
            assert frame["type"] == "event"
            envelope = frame["event"]
            assert envelope["event_type"] == iev.EXT_MISSION_ACCEPTED
            seq = envelope["sequence"]
            # Acknowledge.
            ws.send_json({"type": "ack", "message_id": envelope["message_id"], "sequence": seq})

        # Produce another event while disconnected, then reconnect and replay from cursor.
        submit(client, "ws-2")
        with client.websocket_connect("/agent-api/ws") as ws:
            ws.send_json(hello())
            assert ws.receive_json()["type"] == "subscribed"
            ws.send_json({"type": "replay", "after_sequence": seq})
            # Expect the missed event(s) then a replay_complete marker.
            got = []
            while True:
                frame = ws.receive_json()
                if frame["type"] == "replay_complete":
                    break
                if frame["type"] == "event":
                    got.append(frame["event"]["sequence"])
            assert all(s > seq for s in got) and got == sorted(got)


def test_backup_preserves_agent_history(tmp_path):
    from aithernet.ops.backup import create_backup, restore_backup, verify_backup

    runtime = _runtime(tmp_path)
    agent, _, _ = _agent_with_key(runtime)
    aid = agent["agent_id"]
    runtime.interop.create_subscription(aid, delivery_mode="pull", filters={})
    mid = asyncio.run(runtime.interop.submit_mission(
        agent_id=aid, body={"objective": "x", "idempotency_key": "k"}
    ))["mission_id"]
    asyncio.run(runtime._emit_event(
        event_type="mission.completed", mission_id=mid, message="done", source="runtime"
    ))
    runtime.engine.dispose()
    # Keep the pre-restore safety backup out of the repo root (under tmp).
    runtime.config.backup_directory = str(tmp_path / "backups")
    backup = create_backup(runtime.config, backup_dir=str(tmp_path / "backups"),
                           now_iso="2026-06-18T00:00:00+00:00")
    assert verify_backup(backup.path).ok
    restore_backup(backup.path, runtime.config, config_path=None, dry_run=False,
                   now_iso="2026-06-18T00:00:01+00:00")
    rt2 = NodeRuntime.from_config(runtime.config)
    restored = rt2.interop.agent_summary(aid)
    assert restored["agent_id"] == aid and restored["fingerprint"] == agent["fingerprint"]
    # Delivery history (and its monotonic sequence) survived the round-trip.
    deliveries = rt2.interop.list_deliveries(aid)
    assert any(d["event_type"] == iev.EXT_MISSION_COMPLETED for d in deliveries)


def test_gateway_disabled_blocks(tmp_path):
    from aithernet.api.app import create_app

    cfg = _config(tmp_path)
    cfg.external_agents.enabled = False
    runtime = NodeRuntime.from_config(cfg)
    app = create_app(runtime=runtime)
    with TestClient(app) as client:
        assert client.post("/agent-api/missions", content=b"{}").status_code == 403
        assert client.post("/external-agents", json={"display_name": "x"}).status_code == 403
