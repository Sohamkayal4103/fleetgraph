# Reference external-agent client (Stage 14D)

A small, dependency-light demonstration of how a **non-Aithernet** program talks to an
Aithernet node's external-agent gateway. It is **not** a production SDK and introduces no
framework — copy what you need.

It demonstrates:

- Ed25519 key generation / loading (`generate_keypair`)
- canonical **signed-request** construction (`build_auth_headers`) — no secret on the wire
- idempotent mission submission + receipt handling (`ExternalAgentClient.submit_mission`)
- **webhook callback** signature + content-digest verification (`verify_callback`)
- duplicate suppression by `message_id` (`CallbackDeduper`)
- delivery acknowledgement (`ExternalAgentClient.acknowledge`)
- WebSocket `hello` framing with a replay cursor (`ExternalAgentClient.ws_hello`)
- artifact-metadata retrieval (`ExternalAgentClient.get_artifact`)

Requires only `cryptography` and `httpx` (both already used by Aithernet).

## Quick start

```bash
# 1) operator provisions the agent with its PUBLIC key (one-time)
PUB=$(python -c "from client import generate_keypair; import base64; \
  k,p=generate_keypair(); print(p)")   # keep the private key OUT of the repo
aithernet external-agents create robo --public-key "$PUB"

# 2) the agent then signs every request itself (see client.ExternalAgentClient)
```

> Never commit a real private key. Tests generate ephemeral keys at runtime.

The end-to-end flow (provision → verify endpoint → signed submission → durable signed
webhook + WebSocket replay → restart-safe delivery) is exercised by
`tests/test_interop_process.py`, which drives four real OS processes (node, agent, webhook
receiver, WebSocket client) over loopback only.
