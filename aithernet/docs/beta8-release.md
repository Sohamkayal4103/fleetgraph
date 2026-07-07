# Aithernet 0.8.0-beta.8 — production release (PUBLISHED)

Private beta, receive-only. Published to the hosted control plane on the `early-access` channel
(the single live customer channel; beta.4–beta.7 remain immutable below it).

## Production publication record
- Release ID: `706c2a2d-8abc-4cdb-a8ce-1d4b1faf84a5`
- Version: `0.8.0-beta.8` · channel `early-access` · status `published`
- Source commit (release-affecting): `8eb40a9` (branch `beta-qualification`)
- Signing key id: `aithernet-prod-betaqual-20260620` (production key; manifest signature verifies)
- Manifest digest: `sha256:e88375ccb4c0d1da753ef181fa3341c7401b4580c14f90d9c7b410f8ca6c4177`
- RF-MCP component: `rf-mcp 0.1.0+aithernet.2` (source bundle signed against the pinned key)
  `sha256:200b7671da9cfd694ae43c9a2d0d1a569eaa7808d1cfdccec74ff592ffa5a73c`

## Final production digests
- Delivery ZIP `aithernet-0.8.0-beta.8-ubuntu24.04-amd64.zip` — 18,131,722 B —
  `2529956202496abfec6dcebfa32036cbefcc30423b6b68a50f6a27b3ea314fa6`
  (assembled server-side from the stored artifacts + the prod-signed verification set; its SHA
  therefore differs from the local candidate ZIP `85e6004e…`, exactly as for beta.5–7.)
- Package `aithernet_0.8.0~beta.8_amd64.deb` — 16,037,142 B —
  `6ac64d5fd74fa30e58dc85f8809e74d26e7c8a8b440b397b159ddb4f864db488`
  (uploaded byte-identical to the locally-verified candidate `.deb`.)

## Verification (post-publication, as a customer)
- `GET /v1/public/releases` lists beta.8 as the newest published release; beta.4–7 still present.
- Public verification files (`manifest.json`/`manifest.sig`/`SHA256SUMS`/`aithernet-release.pub`)
  download without auth; the `.deb` requires authentication (correctly gated).
- Authenticated customer download of the delivery ZIP: host SHA-256 == portal metadata SHA-256;
  `sha256sum -c SHA256SUMS` all OK; `openssl pkeyutl -verify … manifest.json` → Verified.
- Clean Ubuntu 24.04 install smoke test: `apt install ./…deb` OK; `aithernet --version` →
  `aithernet 0.8.0b8`; `aithernet release verify` → VERIFIED (prod key, 13 files); setup binds a
  canonical `node.yaml` with a real `node_id` and absolute db/state/identity paths; `aithernet start`
  boots on the canonical config.

## What changed in beta.8
- Canonical config/state repair (one `node.yaml` with a real node id + absolute db/state/identity);
  beta.7 → beta.8 migration preserves identity, node name, prior missions/events and providers.
- First-class Gemini API coordinator (`gemini_api`, model `gemini-2.0-flash`) with secure
  provider-secret handling (key stored only as an `env:` ref in a 0600 env file, never in `node.yaml`).
- Codex coding provider (`codex_cli`) with an absolute, bounded per-task workspace.
- MCP/TLS correction (system CA store) and bounded MCP restart give-up.
- Mission permanent/transient failure classification.
- Truthful hardware discovery (non-RF Soapy factories excluded; no phantom SDRs).
- Setup-journal reconciliation and readiness domains; concise customer-facing errors.
- Complete authenticated portal Getting Started guide (fresh + beta.7 migration journeys).

## Migration (beta.7 → beta.8)
```
systemctl --user stop aithernet-node.service
sudo apt update && sudo apt install ./aithernet_0.8.0~beta.8_amd64.deb
aithernet setup --migrate --dry-run
aithernet setup --migrate
aithernet setup --status
systemctl --user restart aithernet-node.service
```
Do not delete beta.7 state. Identity and node name are preserved; prior missions/events remain.

## Diagnostics & rollback
- Diagnostics: `aithernet doctor`, `aithernet setup --status`, `aithernet components verify rf-mcp`,
  `aithernet mcp diagnostics --probe`, `aithernet mcp tools`.
- Rollback: reinstall the prior `aithernet_0.8.0~beta.7_amd64.deb` (still published on the portal) and
  restart the service; beta.7 state is untouched by the migration. The beta.8 release record can be
  revoked with `aithernet-hosted release revoke` if needed (this repoints the channel off beta.8
  without altering beta.4–7).

## Limitations
- Receive-only; no RF transmission is supported.
- WSL is software-only; WSL audio endpoints are not SDR hardware; with zero connected SDRs physical
  RF readiness is `unavailable` (expected, not an error).
- Physical Pluto qualification is not included in this release.
- Real external-provider (Gemini/Codex) live mission acceptance is a separately operator-tested,
  post-publication qualification item (deterministic fake-provider path is auto-tested).
