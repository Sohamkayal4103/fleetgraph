# Aithernet OTA (SDR over-the-air) peer transport — 1.0.0-beta.1 (software-qualified)

SDR/RF is added as **another peer transport beneath** the existing peer-message and MissionEngine
layers. The logical message — a signed canonical `MessageEnvelope` — is identical across loopback,
IP, hosted relay, simulated RF, cabled SDR, and (future, separately qualified) authorized OTA SDR.
**No mission logic lives in the modem layer.** Physical transmission is **closed** in beta.1.

## Architecture
```
coordinator / remote agent → canonical peer message → signed MessageEnvelope
  → transport selection (ip | simulated_rf | rf_ota | auto)
  → [OTA] serialize → AEAD seal → fragment → frame(CRC) → FEC → modulate
  → impaired RF channel (simulation) → demodulate → FEC/CRC → reassemble
  → AEAD open (replay/expiry/receiver) → canonical MessageEnvelope
  → receiving peer ingress → receiving MissionEngine (authoritative for all policy)
```
The receiving node owns mission acceptance, execution policy, provider/coding/SDR/RF access,
workspaces, artifacts, events, cancellation, and result publication. **An RF message never creates
shell access or bypasses the canonical MissionEngine or peer authentication.**

## Modules (`src/aithernet/transport/ota/`)
- `interface.py` — `PeerTransport` Protocol (send/receive/health/capabilities), `DeliveryReceipt`.
- `profiles.py` — `RFLinkProfile` (waveform only; **no** freq/gain/power/antenna/hardware), digests,
  `ProfileRegistry` with approval states (`unapproved` → `approved_simulation`; physical never here).
- `frame.py` — binary framing `HEADER||PAYLOAD||CRC` (CRC16-CCITT/CRC32), fragmentation/reassembly.
- `fec.py` — repetition-3 and Hamming(7,4).
- `modem.py` — pure-Python complex-baseband BPSK/QPSK with preamble correlation + phase recovery.
- `channel.py` — deterministic impaired channel (AWGN, phase, delay, frequency offset).
- `reliability.py` — stop-and-wait ARQ (bounded retransmission) + duplicate suppression.
- `security.py` — ChaCha20-Poly1305 AEAD over the serialized envelope; binds protocol version,
  sender/receiver node IDs, key id, message/correlation id, sequence, timestamp, expiry, nonce,
  payload type/length/digest, profile id; replay + expiry + constant-time receiver validation.
- `flowgraph.py` — deterministic GNU Radio TX/RX flowgraph generation + digest + **structural
  safety validation** (bounded block set; rejects shell/eval/socket/etc). Generation only.
- `simulated_rf.py` — `SimulatedRFLink`: the full two-node canonical exchange over modem+channel.
- `candidate.py` — deterministic **validate-and-promote** gate for a generated modem.
- `selection.py` — deterministic transport selection (RF never silently downgraded to IP).
- `authorization.py` — `RFTx{Capability,Policy,Plan,Lease,Authorization}` + `evaluate_tx_gates`.

## Security model
Authentication is the AEAD tag + the canonical envelope's Ed25519 signature — **successful
demodulation is never authentication**. Replay (nonce + monotonic sequence window), expiry, and
receiver-address checks run before delivery. No key material appears in logs, events, headers,
datasets, flowgraph arguments, or artifacts.

## Reference modem (beta.1)
A robust low-rate BPSK link (`ref-bpsk-1k`: 1 kBd, 8 sps, 0xAA preamble + 0x2DD4 sync, repetition-3
FEC, CRC-16, ≤128-byte frames). A QPSK profile (`ref-qpsk-2k`) demonstrates the plugin surface.
"Flexible protocols" = a documented, validated **plugin** registry — not unrestricted runtime
waveform generation.

## Operator-controlled physical transmission (beta.2)
Physical SDR transmission is a **real, supported capability under LOCAL operator control**. There is
**no vendor approval, cloud authorization, subscription, hidden allowlist, release key, or external
service** required to enable it. Aithernet does not reserve, withhold, or remotely gate physical TX.

A physical TX is permitted when the **local operator** has enabled it (`aithernet rf tx enable`) and
**all technical-correctness controls** pass — these keep the radio + network sound, they are not
feature-withholding: a TX-capable device is present and reports TX capability; the plan's parameters
are within the **device's actual ranges**; within the **operator's own configured ceilings**; the
peer message is **authenticated**; and the operator **confirms the exact plan** (interactively, or
via a local pre-authorization envelope the operator created). Any change to frequency, sample rate,
gain, bandwidth, duration, payload, hardware, URI, modem profile, or flowgraph requires a fresh
local confirmation (a new plan digest) unless it fits a local pre-authorization envelope.

Profile **qualification state** (`experimental` / `software_validated` / `operator_custom` /
`hardware_tested`) **informs** the operator and is recorded in the plan + research record — it is
**never** a lock. An operator may run a custom or experimental profile after a clear local
confirmation; profiles are rejected only for **concrete technical reasons** (malformed graph,
unsupported sample format, impossible sample rate, hardware-range violation, missing TX sink,
unbounded execution, failed build/import, failed integrity check).

**Aithernet never independently chooses an operating frequency** — the operator supplies all
physical parameters and is responsible for operating the radio in accordance with the rules
applicable at the operating location. The managed executor accepts a **structured plan only** (never
shell or arbitrary model source) and routes through the canonical runtime (peer identity,
MissionEngine, events, artifacts). No internet connection is required for local RF communication.

## Coding-agent role
The coding agent may PROPOSE transport plugins, encoders/decoders, packet formats, GNU Radio
flowgraphs, modem logic, fixtures, and tests in the bounded mission workspace — but **never
authorizes transmission**. The canonical deterministic system inspects files, runs tests, validates
the flowgraph, enforces RF policy, computes the exact plan, obtains authorization, and executes only
the authorized plan. No model-generated source is transmitted before passing validation +
authorization.

## CLI
- `aithernet run --on <peer> --transport simulated_rf|rf_ota "<objective>"` (rf_ota when the local
  operator has enabled TX + a capable device is present; never a silent RF→IP downgrade).
- `aithernet peers transports <peer>`, `aithernet peers test <peer> --transport simulated_rf|rf_ota`.
- `aithernet rf profiles list|inspect|validate`, `aithernet rf tx-policy`, `rf transport`.
- Operator TX: `aithernet rf tx enable|disable|status|configure|devices|capabilities <device>`,
  `rf tx plans authorize|list|inspect|revoke`, `rf tx test-plan|loopback|send-test-frame`.
GNU Radio implementation details are not exposed to normal users.

## Research data
Each RF exchange yields a **secret-free** record (message/sender/receiver ids, payload digest,
sizes, frame count, profile/modulation/rate/bandwidth/FEC/CRC, channel params + SNR, retries, acks,
latency, decode/signature/replay outcomes, flowgraph + implementation + plan digests, and whether
the path was simulated/cabled/shielded/OTA). Dataset classes: rf-transport-routing,
modem-profile-selection, frame-recovery, link-adaptation, delivery-prediction, retransmission-policy,
snr-error-analysis, generated-modem-validation, peer-message-delivery, simulated-channel-outcomes —
mission-grouped, leakage-free splits. **No key material**; **no claim of hidden chain-of-thought.**

## Qualification states & operator responsibility
The shipped code is **software-qualified** (pure simulation → synthetic impaired channel → GNU
Radio loopback → virtual two-node link → contained executor loopback). Whether a profile/path has
been **hardware-tested** is a descriptive state that depends on the **operator's own hardware** —
Aithernet records it but never uses it to withhold the feature. The operator runs cabled, shielded,
and authorized-OTA tests on their hardware after installation and may mark a profile
`hardware_tested` from their results.

We distinguish: **software-qualified** (passed in simulation/loopback here), **hardware-tested**
(the local operator confirmed it on real hardware), **operator-custom** (operator-authored,
accepted with local confirmation), and **physically tested by the current operator** (their own
field result). Untested-on-this-hardware is **not** "certified," but the feature is not hidden.

The operator selects all physical parameters and is responsible for operating the radio in
accordance with the rules applicable at the operating location. Aithernet selects no operating
frequency and implements no jamming, interference generation, unauthorized interception, evasion,
covert beaconing, third-party impersonation, or identity/policy bypass.

`aithernet rf tx test-plan`, `rf tx loopback`, `rf tx send-test-frame`, and
`peers test <peer> --transport rf_ota` support cabled/shielded/low-power-OTA testing; automated
qualification uses hardware mocks + GNU Radio loopback and **never radiates**.
