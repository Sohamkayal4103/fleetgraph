"""Stage 13A API + CLI tests (Part T items 58-70, plus regression for existing agent API)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aithernet.cli import app as cli_app
from aithernet.transport.ack import verify_ack
from aithernet.transport.envelope import MessageKind, build_envelope
from aithernet.transport.identity import IdentityManager

runner = CliRunner()


def _peer_identity(tmp_path, node_id="peer-node"):
    return IdentityManager(tmp_path / node_id, node_id=node_id, node_name=node_id).initialize()


def _init_identity(client: TestClient) -> dict:
    resp = client.post("/identity/initialize")
    assert resp.status_code == 200
    return resp.json()


def _add_trusted_peer(client: TestClient, peer_identity, *, endpoint="http://peer.local") -> str:
    created = client.post("/peers", json={
        "name": "Peer", "role": "node", "endpoint_url": endpoint,
        "expected_node_id": peer_identity.node_id, "public_key": peer_identity.public_key_b64,
    })
    assert created.status_code == 201
    peer_id = created.json()["id"]
    trusted = client.post(f"/peers/{peer_id}/trust", json={
        "public_key": peer_identity.public_key_b64
    })
    assert trusted.status_code == 200
    assert trusted.json()["trust_state"] == "trusted"
    return peer_id


def _signed_envelope(peer_identity, *, recipient_node_id, payload=None, message_id=None):
    return build_envelope(
        identity=peer_identity, recipient_node_id=recipient_node_id, recipient_agent_id=None,
        kind=MessageKind.AGENT_MESSAGE.value, payload=payload or {"subject": "s", "text": "hi"},
        message_id=message_id,
    )


# -- 58 identity endpoints -------------------------------------------------------


def test_identity_endpoints(transport_client: TestClient) -> None:
    status = transport_client.get("/identity/status").json()
    assert status["initialized"] is False  # not yet initialized
    info = _init_identity(transport_client)
    assert info["initialized"] and info["fingerprint"].startswith("sha256:")
    public = transport_client.get("/identity/public").json()
    assert public["public_key"] and "private" not in json.dumps(public).lower()
    fp = transport_client.get("/identity/fingerprint").json()
    assert fp["fingerprint"] == info["fingerprint"]


def test_identity_initialize_is_idempotent(transport_client: TestClient) -> None:
    first = _init_identity(transport_client)["fingerprint"]
    second = _init_identity(transport_client)["fingerprint"]
    assert first == second


# -- 59 peer CRUD / trust / revoke ----------------------------------------------


def test_peer_crud_trust_revoke(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    peer = _peer_identity(tmp_path)
    created = transport_client.post("/peers", json={
        "name": "B", "role": "node", "endpoint_url": "http://b.local",
        "expected_node_id": peer.node_id, "public_key": peer.public_key_b64,
    })
    assert created.status_code == 201
    pid = created.json()["id"]
    assert created.json()["trust_state"] == "untrusted"  # never auto-trusted
    assert transport_client.get(f"/peers/{pid}").json()["fingerprint"] == peer.fingerprint
    assert any(p["id"] == pid for p in transport_client.get("/peers").json())
    trusted = transport_client.post(f"/peers/{pid}/trust", json={})
    assert trusted.json()["trust_state"] == "trusted"
    revoked = transport_client.post(f"/peers/{pid}/revoke")
    assert revoked.json()["trust_state"] == "revoked"


def test_trust_rejects_key_replacement(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    peer = _peer_identity(tmp_path)
    other = _peer_identity(tmp_path, node_id="other")
    pid = _add_trusted_peer(transport_client, peer)
    # Replacing a trusted peer's pinned key with a different key is refused (409).
    resp = transport_client.post(f"/peers/{pid}/trust", json={"public_key": other.public_key_b64})
    assert resp.status_code == 409


# -- 60-63 inbound + send + status ----------------------------------------------


def test_inbound_message_accepted_and_acked(transport_client: TestClient, tmp_path) -> None:
    me = _init_identity(transport_client)
    peer = _peer_identity(tmp_path)
    _add_trusted_peer(transport_client, peer)
    env = _signed_envelope(peer, recipient_node_id="test-node-id")
    resp = transport_client.post("/agent/v1/messages", json=json.loads(env.model_dump_json()))
    assert resp.status_code == 200
    ack = resp.json()
    # The ack is validly signed by THIS node's identity.
    public = transport_client.get("/identity/public").json()["public_key"]
    ok, _ = verify_ack(ack, public_key_b64=public, expected_message_id=env.message_id,
                       expected_node_id="test-node-id")
    assert ok
    assert me["node_id"] == "test-node-id"
    inbox = transport_client.get("/agent-transport/inbox").json()
    assert len(inbox) == 1 and inbox[0]["message_id"] == env.message_id


def test_inbound_duplicate_idempotent(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    peer = _peer_identity(tmp_path)
    _add_trusted_peer(transport_client, peer)
    body = json.loads(_signed_envelope(peer, recipient_node_id="test-node-id").model_dump_json())
    r1 = transport_client.post("/agent/v1/messages", json=body)
    r2 = transport_client.post("/agent/v1/messages", json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert len(transport_client.get("/agent-transport/inbox").json()) == 1


def test_inbound_untrusted_rejected(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    peer = _peer_identity(tmp_path)
    # Peer added but NOT trusted.
    transport_client.post("/peers", json={
        "name": "B", "role": "node", "endpoint_url": "http://b",
        "expected_node_id": peer.node_id, "public_key": peer.public_key_b64,
    })
    body = json.loads(_signed_envelope(peer, recipient_node_id="test-node-id").model_dump_json())
    resp = transport_client.post("/agent/v1/messages", json=body)
    assert resp.status_code == 403
    assert resp.json()["error"] == "peer_not_trusted"


def test_inbound_revoked_rejected(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    peer = _peer_identity(tmp_path)
    pid = _add_trusted_peer(transport_client, peer)
    transport_client.post(f"/peers/{pid}/revoke")
    body = json.loads(_signed_envelope(peer, recipient_node_id="test-node-id").model_dump_json())
    assert transport_client.post("/agent/v1/messages", json=body).status_code == 403


def test_inbound_recipient_mismatch_rejected(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    peer = _peer_identity(tmp_path)
    _add_trusted_peer(transport_client, peer)
    body = json.loads(_signed_envelope(peer, recipient_node_id="some-other-node").model_dump_json())
    assert transport_client.post("/agent/v1/messages", json=body).status_code == 421


def test_inbound_oversized_rejected(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    peer = _peer_identity(tmp_path)
    _add_trusted_peer(transport_client, peer)
    env = _signed_envelope(
        peer, recipient_node_id="test-node-id", payload={"blob": "x" * 2_000_000}
    )
    resp = transport_client.post("/agent/v1/messages", json=json.loads(env.model_dump_json()))
    assert resp.status_code == 413


def test_manifest_and_health_endpoints(transport_client: TestClient) -> None:
    _init_identity(transport_client)
    manifest = transport_client.get("/agent/v1/manifest").json()
    assert manifest["signature"] and "agent_transport" in manifest["capabilities"]
    blob = json.dumps(manifest).lower()
    for forbidden in ("api_key", "password", "/home/", "database", "private"):
        assert forbidden not in blob
    health = transport_client.get("/agent/v1/health").json()
    assert health["status"] == "ok" and health["identity_initialized"] is True


def test_send_list_retry_cancel(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    peer = _peer_identity(tmp_path)
    pid = _add_trusted_peer(transport_client, peer)
    sent = transport_client.post("/agent-transport/messages", json={
        "peer_id": pid, "subject": "hi", "text": "hello"
    })
    assert sent.status_code == 200
    record = sent.json()["message"]
    mid = record["id"]
    assert record["status"] in ("pending", "retry_scheduled", "in_flight", "failed")
    listed = transport_client.get("/agent-transport/messages").json()
    assert any(m["id"] == mid for m in listed)
    # Outbox + inbox listings + transport status.
    assert isinstance(transport_client.get("/agent-transport/outbox").json(), list)
    cancelled = transport_client.post(f"/agent-transport/messages/{mid}/cancel").json()
    assert cancelled["status"] == "cancelled"


def test_transport_status_endpoint(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    _add_trusted_peer(transport_client, _peer_identity(tmp_path))
    status = transport_client.get("/agent-transport/status").json()
    assert status["identity"]["initialized"] is True
    assert status["peer_count"] == 1 and status["trusted_peer_count"] == 1
    assert "outbox_counts" in status


# -- 67 no secrets in any output -------------------------------------------------


def test_no_private_key_in_any_response(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    peer = _peer_identity(tmp_path)
    pid = _add_trusted_peer(transport_client, peer)
    transport_client.post("/agent-transport/messages", json={"peer_id": pid, "text": "x"})
    for path in ["/identity/status", "/identity/public", "/peers", f"/peers/{pid}",
                 "/agent-transport/status", "/agent-transport/outbox", "/agent/v1/manifest"]:
        blob = transport_client.get(path).text.lower()
        assert "begin private key" not in blob
        assert "pkcs8" not in blob


# -- 69 polling causes no sends --------------------------------------------------


def test_polling_does_not_send(transport_client: TestClient, tmp_path) -> None:
    _init_identity(transport_client)
    pid = _add_trusted_peer(transport_client, _peer_identity(tmp_path))
    sent = transport_client.post("/agent-transport/messages", json={"peer_id": pid, "text": "x"})
    mid = sent.json()["message"]["id"]
    before = transport_client.get(f"/agent-transport/messages/{mid}").json()["attempt_count"]
    # Repeated GET polling must not change delivery state.
    for _ in range(5):
        transport_client.get("/agent-transport/status")
        transport_client.get("/agent-transport/outbox")
    after = transport_client.get(f"/agent-transport/messages/{mid}").json()["attempt_count"]
    assert after == before


# -- 64-66 CLI -------------------------------------------------------------------


def test_cli_identity(transport_live_server: str) -> None:
    init = runner.invoke(cli_app, ["identity", "initialize", "--url", transport_live_server])
    assert init.exit_code == 0
    status = runner.invoke(cli_app, ["identity", "status", "--url", transport_live_server])
    assert status.exit_code == 0 and "Fingerprint" in status.stdout
    # No private key material in CLI output.
    assert "PRIVATE KEY" not in status.stdout
    fp = runner.invoke(cli_app, ["identity", "fingerprint", "--url", transport_live_server])
    assert fp.exit_code == 0 and "sha256:" in fp.stdout


def test_cli_peer_management(transport_live_server: str, tmp_path) -> None:
    runner.invoke(cli_app, ["identity", "initialize", "--url", transport_live_server])
    peer = _peer_identity(tmp_path)
    add = runner.invoke(cli_app, [
        "peer", "add", "--name", "B", "--role", "node", "--endpoint", "http://b.local",
        "--node-id", peer.node_id, "--public-key", peer.public_key_b64,
        "--url", transport_live_server,
    ])
    assert add.exit_code == 0 and "untrusted" in add.stdout
    listed = runner.invoke(cli_app, ["peer", "list", "--url", transport_live_server])
    assert listed.exit_code == 0 and peer.node_id in listed.stdout
    # Trust via CLI, then confirm.
    import re

    match = re.search(r"Peer ([0-9a-f-]{36})", listed.stdout)
    assert match
    pid = match.group(1)
    trusted = runner.invoke(cli_app, ["peer", "trust", pid, "--url", transport_live_server])
    assert trusted.exit_code == 0 and "trusted" in trusted.stdout
    # No private key material ever appears.
    assert "PRIVATE KEY" not in listed.stdout


def test_cli_message_send_list(transport_live_server: str, tmp_path) -> None:
    runner.invoke(cli_app, ["identity", "initialize", "--url", transport_live_server])
    # Create + trust a peer via API (CLI peer-endpoint vs node-url option overlap avoided).
    import httpx

    peer = _peer_identity(tmp_path)
    with httpx.Client(base_url=transport_live_server) as c:
        created = c.post("/peers", json={
            "name": "B", "role": "node", "endpoint_url": "http://b.local",
            "expected_node_id": peer.node_id, "public_key": peer.public_key_b64,
        })
        pid = created.json()["id"]
        c.post(f"/peers/{pid}/trust", json={})
    send = runner.invoke(cli_app, [
        "agent-message", "send", "--peer-id", pid, "--text", "hello",
        "--url", transport_live_server,
    ])
    assert send.exit_code == 0 and "Queued message" in send.stdout
    listed = runner.invoke(cli_app, ["agent-message", "list", "--url", transport_live_server])
    assert listed.exit_code == 0
    status = runner.invoke(cli_app, ["transport", "status", "--url", transport_live_server])
    assert status.exit_code == 0 and "Agent transport" in status.stdout


# -- regression: existing external-agent API remains compatible ------------------


def test_existing_agent_api_still_works(client: TestClient) -> None:
    connect = client.post("/agents/connect", json={"name": "legacy", "agent_type": "external"})
    assert connect.status_code == 201
    assert client.get("/agents").status_code == 200
    assert client.get("/agent-messages").status_code == 200  # Stage 7 route intact
