# Aithernet 0.8.0-beta.9 — production release (PUBLISHED)

Private beta, receive-only. Published to the hosted control plane on the `early-access` channel
(beta.4–beta.8 remain immutable below it).

## Production publication record
- Release ID: `712cb7d7-7ef0-4b1d-8080-3ee78581a7f6` · status **published** · channel `early-access` (current)
- Source commit (release-affecting): `353e7cc` (branch `beta-qualification`)
- Signing key id: `aithernet-prod-betaqual-20260620` (production key; manifest signature verifies)
- Manifest digest: `sha256:362db77f0a75067b57ef628da1cb5018c9506be852f41419d9924632c2acc8e8`
- RF-MCP component: `rf-mcp 0.1.0+aithernet.2`
  `sha256:200b7671da9cfd694ae43c9a2d0d1a569eaa7808d1cfdccec74ff592ffa5a73c` (signed)

## Final production digests
- Delivery ZIP `aithernet-0.8.0-beta.9-ubuntu24.04-amd64.zip` — 18,177,376 B —
  `d11fed95375bc1e16c6d541a051ae60db561c1bfd335e6072938d03d480eb973`
  (assembled server-side from stored artifacts + the prod-signed verification set.)
- Package `aithernet_0.8.0~beta.9_amd64.deb` — 16,044,092 B —
  `b7838ed7b497ba33aab94b12fe0d7c2ad81c5d6a46ba0fa4a52158304a003886`
  (byte-identical to the locally-qualified candidate `.deb`.)

## What changed in beta.9 (zero-friction customer experience + runtime reliability)
- **One-command `aithernet run "<prompt>"`** — creates, starts, streams, prints the result + ids,
  meaningful exit code; `--detach`. No separate submit/start/watch. Runs through the canonical engine.
- **`aithernet mission retry <id>`** — re-runs a blocked/failed mission with a fresh run (transient
  streak reset), never duplicating the mission.
- **systemd service pins the canonical config** (`--config` in ExecStart, not env-only) and a managed
  `runtime.env` carries the provider PATH so NVM/Node CLI providers run under the service.
- **Coordinator timeout 180s** + fine-grained failure categories (timeout/network/rate_limit/auth/
  executable/configuration/malformed_output/schema_invalid) + bounded exponential backoff; auth/
  executable/config block once without burning the failure budget.
- **Managed RF-MCP diagnostics** describe the installed signed component; no gr-mcp clone guidance.
- **Guided `aithernet agents connect coordinator|coding`** — discover options, configure, resolve
  the CLI runtime, restart + verify. **`aithernet doctor --repair`** — safe non-destructive repair.
- **Truthful unified readiness** — doctor honors live-verified; setup never claims mission-readiness
  on RF-MCP alone. Docs validated against the packaged CLI (invalid `--content` removed).
- The "Local OpenAI-compatible" coordinator option is now actually runnable by the engine.

## Automated qualification (deterministic; no external creds, no physical RF)
`scripts/beta9_acceptance.sh` on clean Ubuntu 24.04 amd64, source-hidden `.deb`, normal user — all
PASS, on both the candidate and the live production-downloaded artifact:
install + `aithernet 0.8.0b9` + `release verify`; guided setup → real node id (no placeholder) +
absolute db/state/identity; unit ExecStart pins `--config` + reads `runtime.env`; NVM provider dir in
the managed PATH; `doctor --repair`; beta.8→beta.9 migration dry-run; **one-command `aithernet run`
completing through the canonical engine** against a deterministic local coordinator stub; zero RF/TX.
Plus the in-tree deterministic suite (service unit, classification/backoff, run+retry e2e, MCP,
docs, readiness, doctor-repair, agents-connect, local-coordinator wiring).

## Migration (beta.8 → beta.9)
```
sudo apt update && sudo apt install ./aithernet_0.8.0~beta.9_amd64.deb
aithernet setup            # detects the prior install and migrates (preserves id/missions/events)
aithernet run "<prompt>"
```
Do not delete beta.8 state. Identity, node name, missions/events/artifacts and providers carry forward.

## Limitations
- Receive-only; no RF transmission. WSL is software-only (audio endpoints are not SDRs; zero SDRs →
  physical RF readiness `unavailable`). Physical Pluto qualification not included.
- Real external-provider (Gemini API / Codex) live missions are optional canaries — run when
  credentials are already present; deterministic fixtures cover mandatory qualification.
