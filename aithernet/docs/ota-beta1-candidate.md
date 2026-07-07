# Aithernet 1.0.0-beta.1 — private software candidate (NOT published)

Branch `ota-transport` (from beta.10 `fd7e59e`). Physical TX **closed**. beta.4–10 immutable.
**Not signed with the production key; not published.**

## What this candidate contains
SDR over-the-air added as **another peer transport** beneath the canonical peer + MissionEngine
layers (see `docs/ota-transport.md`). Software-qualified; no physical RF.

## Integration completed (items 1–3)
- **Runtime RF carrier** (`transport/ota/runtime_link.py`): full bidirectional canonical path —
  envelope → seal/modulate/impaired-channel/demod/open → **receiver ingress (authoritative,
  verifies Ed25519 signature)** → signed result → RF return → sender. CLI: `run --on --transport`
  (auto|ip|simulated_rf; rf_ota physical closed; no silent RF→IP downgrade), `peers transports`,
  `peers test --transport`.
- **Research persistence** (`transport/ota/research.py`): each RF exchange → secret-free normalized
  records (ids, digests, sizes, profile/modulation/rates, frame count, FEC/CRC, channel params,
  retries/acks, latency, decode/signature/replay/delivery outcomes). Secret-scanned; no key material.
- **RF datasets**: `data build peer-rf-delivery|frame-recovery|retransmission-policy|
  simulated-channel-outcomes|...` → mission-grouped, leakage-free train/val/eval + schema + card +
  provenance + statistics + SHA256SUMS. **Live Drive** upload of a snapshot artifact verified
  (remote size + digest; idempotent re-upload; cleaned up).

## Tests
**46 OTA tests green** across Phases 1–4 + selection + runtime-link + research/datasets; ruff clean.
The full two-node simulated-RF path and the generated-modem validation/promotion gate pass.

## Not met — item 4 (live coding-agent modem generation)
Blocked on an **inaccessible external coordinator-LLM credential** (OpenRouter key 401; no
Gemini/Anthropic/OpenAI keys usable; codex/gemini tokens belong to those tools and may not be
repurposed). The safety-critical **validation/promotion gate** is implemented + tested; only the
live-agent *generation* could not run. Reported, not fabricated.

## No physical TX
`aithernet rf tx-policy` → `physical_tx: disabled`; `rf transport` → `rf_ota:
closed-pending-hardware-and-plan-authorization`. No SDR transmit call exists in any automated path.
Marconi not enabled; no frequency chosen; no firmware change.

## Remaining requirements for PHYSICAL qualification (separate, operator-authorized)
1. **Cabled two-SDR test**: two TX-capable SDRs connected by coax with ≥ (TX power + margin) of
   **attenuation** and RF isolation; operator-supplied authorized frequency/sample-rate/bandwidth/
   gain/duration bounds; an `RFTxPlan` whose digest an operator authorization binds to; a bounded
   TX lease; verification that received frames decode and research records capture true SNR/EVM.
2. **Shielded test**: the same inside a shielded enclosure / RF anechoic chamber to contain
   emissions; confirm no leakage beyond the enclosure; same plan-digest authorization.
3. **Authorized OTA test**: only on spectrum the operator is licensed/authorized to use (e.g. an
   amateur band with a licensed operator, or a controlled/experimental authorization), at minimum
   power, with explicit operator approval of the exact plan, logging, and compliance with local
   regulations. Aithernet selects no frequency; the operator supplies all RF parameters.
Each step requires promoting a profile to `approved_physical` only after that step's qualification,
and re-authorization on any plan change.
