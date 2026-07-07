# Aithernet 0.8.0-beta.10 — production release (PUBLISHED)

Private beta, receive-only. Published to the hosted control plane on the `early-access` channel
(the single live customer channel). beta.4 through beta.9 remain immutable and published.

## Release identifiers
- Release ID: `9af5f8a7-6b03-41d1-bc97-76ec738cea38` · status **published** · channel `early-access`
- Signing key id: `aithernet-prod-betaqual-20260620` (production key; re-signed server-side)
- Release manifest digest: `sha256:a345b5dfb305dd80bbd2d7905bfc8f23b3485a81073a663f866e809500edb3f5`
- Published at: `2026-06-23T04:24:21Z`
- Source commit (beta-qualification): `dd727e387e691ab451216970f39737cafa9361ed`
- RF-MCP component: `rf-mcp 0.1.0+aithernet.2`
  `sha256:200b7671da9cfd694ae43c9a2d0d1a569eaa7808d1cfdccec74ff592ffa5a73c` (signed; unchanged from beta.9)

## Production artifact digests (prod blob store == qualified candidate)
- Package `aithernet_0.8.0~beta.10_amd64.deb` — 16,070,764 B —
  `sha256:93f80fa861bdc1e8df8b5f842f525c0c7b1bfad812827f5902a5558ea7e5db94`
  (byte-identical to the locally-qualified candidate `.deb`).
- Wheel `aithernet-0.8.0b10-py3-none-any.whl` —
  `sha256:275e00cde3eb8c618965cbea3931f4dd05078323a6b9188f7f17693963eaf6f3`
- sdist `aithernet-0.8.0b10.tar.gz` —
  `sha256:aace8793f2c70ed414488da8ca838e17879bcca3d93bdfc028da448f72598bcb`
- Candidate `manifest.json` (LAN-signed, verifies with the bundled `aithernet-release.pub`) —
  `sha256:577a95740f478d3d1b8be48a90105ab7d0325162ff60d2a3311168bd95ec01a7`
- 16 artifacts attached + stored in the persistent `blobs` volume under
  `releases/early-access/0.8.0-beta.10/`.

## What beta.10 delivers
Part 1 — provider/runtime fixes: generic secret persistence + auto-propagation to the running
service (`agents secrets list/check/remove`); guided OpenAI-compatible/Z.AI config with URL+model
validation; normalized redacted provider errors; quota → recoverable `blocked_provider_quota` (no
budget burn) + coordinator `fallback_providers`; deterministic MCP tool-schema preflight (alias
canonicalization); managed RF-MCP diagnostics; coding-agent routing.
Part 2 — secure node-to-node CLI (`peers pair`, `run --on`, `agents gateway`) over the existing
signed transport/comms stack.
Parts 3–5 — owner research spool (secret-scanned, quarantine), dedicated Google Drive archive
(`Aithernet Research Data` root `1LWA36czXxe-TiEzUK_og64Uth7kmpL9U`), and leakage-free,
mission-grouped training-dataset builders.
Part 6 — `aithernet setup` owner-research opt-in (one plain-language question; everything automatic).

## Qualification (source-hidden, clean Ubuntu 24.04 amd64, normal user)
`scripts/beta10_acceptance.sh` — ALL 9 CHECKS PASSED against the built `.deb` (byte-identical to the
published artifact): install + canonical identity/absolute paths; generic secret persistence (no
value leakage to config/list/process/0600 file); guided Z.AI config (URL+model validated, bad URL
rejected, provider-appropriate secret hint); managed RF-MCP diagnostics (no source-clone guidance);
peer pairing requires a pinned key; owner research spool + leakage-free dataset build + secret-free
`data status`; one-command mission through the canonical engine; `run --on` refuses an unknown peer
against the live node (no remote shell); no transmit.
Deterministic in-repo suite: 78 new beta.10 tests + the existing suite, green.
Live Drive: real OAuth refresh + dedicated hierarchy creation + verified upload (size+digest), done
against the production Drive account.

## Not claimed
- No physical Pluto/USB-SDR qualification; receive-only — no RF transmission.
- Owner research collection is opt-in and owner-operated — not a default for external customers.
- No access to any model's hidden chain-of-thought; only provider-exposed reasoning + structured
  decision explanations are recorded; no secret/credential is collected or uploaded.
