"""beta.10 Part 2: customer node-to-node CLI — peers pair, run --on <peer>, agents gateway.

These exercise the CLI plumbing over a mocked node API (the transport/comms backend itself is
proven by test_artifact_two_node.py + test_comms.py). They confirm pairing chains add->trust->test,
that `run --on` resolves a trusted peer and submits ONE coordinator request (never remote shell),
that an unknown/untrusted peer is refused, and that the gateway credential lifecycle works.
"""

from __future__ import annotations

from typer.testing import CliRunner

import aithernet.cli as cli
from aithernet.cli import app

runner = CliRunner()


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class _FakeClient:
    """Records requests and replays canned responses keyed by (method, path)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, path, **kw):
        self.calls.append(("GET", path))
        return self.routes[("GET", path)]

    def post(self, path, json=None, **kw):
        self.calls.append(("POST", path, json))
        # exact match first, then prefix match for id-bearing paths
        if ("POST", path) in self.routes:
            return self.routes[("POST", path)]
        for (m, p), r in self.routes.items():
            if m == "POST" and path.startswith(p):
                return r
        raise AssertionError(f"unexpected POST {path}")


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(cli, "_client", lambda base_url: client)


def test_peer_pair_requires_public_key():
    out = runner.invoke(app, ["peer", "pair", "--name", "nodeB", "--url", "http://x"])
    assert out.exit_code != 0
    assert "public-key" in out.output


def test_peer_pair_chains_add_trust_test(monkeypatch):
    client = _FakeClient({
        ("POST", "/peers"): _Resp(201, {"id": "p1", "name": "nodeB", "trusted": False}),
        ("POST", "/peers/p1/trust"): _Resp(200, {"id": "p1", "name": "nodeB", "trusted": True}),
        ("POST", "/peers/p1/test"): _Resp(200, {"ok": True}),
    })
    _patch_client(monkeypatch, client)
    out = runner.invoke(app, ["peer", "pair", "--name", "nodeB", "--public-key", "BASE64KEY",
                              "--url", "http://x"])
    assert out.exit_code == 0, out.output
    methods = [(c[0], c[1]) for c in client.calls]
    assert ("POST", "/peers") in methods
    assert ("POST", "/peers/p1/trust") in methods
    assert ("POST", "/peers/p1/test") in methods
    assert "Trusted" in out.output and "ok" in out.output


def test_peers_plural_alias_exists(monkeypatch):
    client = _FakeClient({("GET", "/peers"): _Resp(200, [])})
    _patch_client(monkeypatch, client)
    out = runner.invoke(app, ["peers", "list", "--url", "http://x"])
    assert out.exit_code == 0
    assert "No peers" in out.output


def test_run_on_unknown_peer_refused(monkeypatch):
    client = _FakeClient({("GET", "/peers"): _Resp(200, [])})
    _patch_client(monkeypatch, client)
    out = runner.invoke(app, ["run", "scan the band", "--on", "ghost", "--url", "http://x"])
    assert out.exit_code == 2
    assert "No trusted peer" in out.output


def test_run_on_untrusted_peer_refused(monkeypatch):
    client = _FakeClient({
        ("GET", "/peers"): _Resp(200, [{"id": "p1", "name": "nodeB", "trusted": False}])})
    _patch_client(monkeypatch, client)
    out = runner.invoke(app, ["run", "do it", "--on", "nodeB", "--url", "http://x"])
    assert out.exit_code == 2
    assert "not trusted" in out.output


def test_run_on_trusted_peer_submits_request(monkeypatch):
    client = _FakeClient({
        ("GET", "/peers"): _Resp(200, [{"id": "p1", "name": "nodeB", "trusted": True}]),
        ("POST", "/communications/send"): _Resp(200, {
            "request_id": "req-1", "conversation_id": "conv-1", "status": "pending",
            "peer_id": "p1"}),
    })
    _patch_client(monkeypatch, client)
    out = runner.invoke(app, ["run", "scan 100MHz", "--on", "nodeB", "--url", "http://x"])
    assert out.exit_code == 0, out.output
    # Exactly one coordinator request was submitted (a REQUEST, not remote shell).
    sends = [c for c in client.calls if c[0] == "POST" and c[1] == "/communications/send"]
    assert len(sends) == 1
    body = sends[0][2]
    assert body["message_type"] == "request" and body["expects_reply"] is True
    assert body["text"] == "scan 100MHz"
    assert "req-1" in out.output


def test_gateway_create_and_revoke(monkeypatch):
    create = _FakeClient({("POST", "/external-agents"): _Resp(201, {"agent_id": "ga-1"})})
    _patch_client(monkeypatch, create)
    out = runner.invoke(app, ["agents", "gateway", "create", "edge-bot", "--url", "http://x"])
    assert out.exit_code == 0, out.output
    assert "ga-1" in out.output and "scoped" in out.output.lower()

    revoke = _FakeClient({
        ("GET", "/external-agents"): _Resp(200, [{"agent_id": "ga-1", "display_name": "edge-bot"}]),
        ("POST", "/external-agents/ga-1/disable"): _Resp(200, {"agent_id": "ga-1"}),
    })
    _patch_client(monkeypatch, revoke)
    out = runner.invoke(app, ["agents", "gateway", "revoke", "edge-bot", "--url", "http://x"])
    assert out.exit_code == 0, out.output
    assert "revoked" in out.output.lower()
