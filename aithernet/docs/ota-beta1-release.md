# Aithernet 1.0.0-beta.1 — production release (PUBLISHED)

Private beta, early-access channel. SDR **simulated** peer transport shipped; **physical TX closed**.
beta.4–10 remain immutable and published (1.0.0-beta.1 is the current early-access release; beta.10
is the preceding release).

## Identifiers
- Release ID: `e08b63af-12c4-4ac1-b671-38c6ce24353e` · status **published** · channel `early-access`
- Signing key id: `aithernet-prod-betaqual-20260620`
- Release manifest digest: `sha256:0ca087afc35d5a818cd9f15ae4875fc93360437bdaf5684719cea4eebe478162`
- Source commit: `e626e09` · release-affecting: `1ceaaae` · tag: `v1.0.0-beta.1` → `c755562`
- RF-MCP component: `0.1.0+aithernet.2` `sha256:200b7671…` (unchanged)

## Production artifacts (prod blob == qualified bundle)
- `aithernet_1.0.0~beta.1_amd64.deb` — 16,092,598 B — `sha256:7a1e7ef4eb1fe7ebaba374d21fcd4bed340eb4193fc9c78f1d9d251f41a6080a`
- `aithernet-1.0.0b1-py3-none-any.whl` — `sha256:900111c8…`
- `aithernet-1.0.0b1.tar.gz` — `sha256:60847cec…`
- `manifest.json` `7ce35d18…` · `manifest.sig` `e7ef600d…` · `SHA256SUMS` `a5d858a3…` · 16 artifacts total

## What shipped
Secure, pluggable SDR peer transport: simulated_rf carrying canonical signed peer envelopes through
AEAD authentication, fragmentation, CRC, FEC, BPSK/QPSK modulation, an impaired channel, demod,
validation, reassembly to canonical ingress; `run --on --transport simulated_rf` integrated;
deterministic generated-modem validation/promotion gate; RF research records + RF dataset builders +
verified Drive snapshot upload. Physical RF transmission disabled; `rf_ota` closed; no frequency
chosen; Marconi disabled.

## Qualification at publication
107 tests (OTA Phases 1–4, selection, runtime-link, research/datasets, doc-CI, provider/MCP
regression). Production download verified (prod `.deb` byte-identical to the qualified bundle;
prod manifest signature verifies). Clean install on Ubuntu 24.04 → `1.0.0b1`. Production runtime
smoke on the shipped binary: two distinct identities, `simulated_rf` selected (no IP fallback),
receiver verified the signature, semantic result returned with correlation preserved, RFExchangeRecord
persisted + dataset record built, no SDR transmit call. Live Drive RF snapshot upload verified
(size+digest, idempotent).

## Post-release live qualification pending
Post-release live qualification pending because no external coordinator credential was configured
during release qualification. The live scenario — real external coordinator → real coding agent →
newly generated modem implementation → deterministic validation → simulated-RF runtime exchange —
is a documented operator test (run after connecting your own coordinator + coding agents). This is
**not a code defect**.

## Physical qualification remains closed
See `docs/ota-beta1-candidate.md` for the separate, operator-authorized cabled / shielded / OTA
requirements. Physical TX requires hardware+device TX capability, an opt-in policy, an approved
*physical* profile, authorized RF parameters, a bounded lease, and an operator authorization bound
to the exact plan digest. No frequency is chosen automatically; the operator supplies all RF
parameters and is responsible for regulatory compliance.
