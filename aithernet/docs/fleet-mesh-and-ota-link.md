# Account-bound fleet, tenant-isolated meshes, and the complete OTA link protocol (1.0.0-beta.3)

This document explains the identity, mesh, fleet and OTA medium-access model introduced in
1.0.0-beta.3. It builds on the operator-controlled physical SDR transport (beta.2) and the software
RF peer transport (beta.1); those remain unchanged and immutable.

## Account vs node identity

These are deliberately separate identities:

* An **account** is a human: an email and an immutable `account_id`, with tenant memberships and
  roles. It authenticates to the portal with a secure HttpOnly session cookie.
* A **node** is a machine: a `node_id` with an Ed25519 keypair (`node key id` + fingerprint), a
  display name, hostname, installation id, software version and capabilities. It authenticates to
  the control plane and to peers by signing — there is no shared secret to print.

The account email is **never** used as a node's cryptographic identity and is **never** transmitted
over the RF peer protocol. Release artifacts (`.deb`, wheel, sdist, customer ZIP) are generic and
byte-identical for every authorized customer — no email, OAuth token, tenant secret or personalized
identifier is embedded. The portal records that an account downloaded an artifact; the artifact
itself is not personalized.

## Tenant vs mesh

* A **tenant** is the billing/ownership boundary inside the hosted platform. Tenant membership
  alone does **not** authorize two nodes to talk.
* A **mesh** is the explicit trust group that authorizes peer communication. A mesh is bound to
  exactly one **authority**: a hosted `tenant` (the authority id is the tenant id) or a
  `standalone` owner (the authority id is the owner node's fingerprint). Membership lists node ids
  with pinned public keys, roles and scopes.

Two nodes that can merely reach each other — over IP or RF, even mutually key-trusted — cannot
exchange messages until they share a mesh. This is enforced cryptographically: every peer envelope
binds `mesh_id` and `authority_id` in its signed body, and the receiving node authorizes the
message against its **own** local mesh membership before the message ever reaches canonical peer
ingress / the MissionEngine. A foreign tenant/owner authority never matches a local mesh, so
cross-tenant spoofing is impossible.

### Cross-tenant communication

Cross-tenant communication requires an **explicit shared mesh** that an administrator (or both
standalone owners) creates and populates with the foreign node. It never happens just because two
nodes can hear each other over RF. Revoking a member takes effect immediately on the next message.

## Enrollment and the fleet

Hosted enrollment binds a generic install to an account/tenant:

```
authenticated portal user creates a one-time enrollment code
→ install the generic package
→ aithernet enroll --base-url <url> --code <code>
→ the node generates/loads its Ed25519 identity, proves possession, submits the code + node id + public key
→ the control plane atomically consumes the code, binds the node to the code's tenant, issues a signed credential
→ the node sends signed heartbeats; the portal shows it
```

Enrollment codes are one-time, tenant-scoped, created by an authenticated principal (with the
creator recorded for audit), short-lived, stored as a digest (never plaintext), revocable, and
atomically consumed. A consumed code cannot enroll into a different tenant or change the node key.

Heartbeats are signed and bind node id, key id, tenant id, sequence, timestamp, version, service
health and bounded capability summary. Replays, revoked nodes, wrong tenants and stale sequences are
rejected. Fleet states: `online`, `stale`, `offline`, `revoked`, `degraded` (a recent heartbeat
reporting non-ready service health), and `never_seen`. Network silence alone is never "failed" — it
advances `online → stale → offline` by age.

CLI:

```
aithernet enroll --base-url <url> --code <code>
aithernet enrollment status
aithernet enrollment disconnect        # stop reporting; keep the local identity
aithernet fleet heartbeat --now
```

## Standalone meshes (offline, no hosted platform)

Meshes are first-class on the node and work fully offline:

```
aithernet mesh create --name "Field Team A"          # standalone owner = this node's fingerprint
aithernet mesh invite-node <mesh> --node-id <id> --public-key <b64>
aithernet mesh members <mesh>
aithernet mesh revoke-node <mesh> <node>
aithernet mesh join --mesh-id <id> --name <n> --authority-id <a>
aithernet mesh leave <mesh>
```

Standalone mode requires no portal login, enrollment code, hosted tenant, hosted heartbeat, cloud
license, internet access or vendor authorization. Hosted enrollment **adds** account ownership,
remote fleet visibility and synchronized mesh management — it does not unlock core local features.
In hosted mode, the same meshes synchronize through the control plane (`/v1/meshes`).

## RF discovery

RF beacons may announce only bounded, non-sensitive information (protocol version, a short node
address derived from the public identity, a mesh discriminator, supported modem profiles, an
availability window). A beacon is not authentication and creates no trust. Account emails, tenant
names, OAuth information and secrets are never transmitted in RF discovery frames.

## The OTA link protocol (MAC)

Above the tested modem (BPSK/QPSK + FEC + CRC) and AEAD security layer, beta.3 adds a complete
medium-access + link protocol (`aithernet.transport.ota.mac`). It is deterministic and radio-free
for qualification (a virtual medium with an integer-microsecond clock, seeded backoff and an
explicit loss/collision model) — no physical transmission is involved.

Frame types: `BEACON`, `RTS`, `CTS`, `DATA`, `BLOCK_ACK`, `NACK`, `CANCEL`, `FIN`,
`DELIVERY_RECEIPT`. Each frame is CRC-protected and authenticated with an HMAC tag over its full
header+payload using the per-link key.

State machine:

```
IDLE → (carrier sense) → RTS_SENT → WAIT_CTS → CTS_GRANTED → DATA_BURST →
WAIT_BLOCK_ACK → RETRANSMIT | FIN → WAIT_DELIVERY_RECEIPT → COMPLETE   (plus FAILED / CANCELLED)
```

MAC modes:

* `point_to_point_tdd` — a reserved time-division slot, no contention.
* `rts_cts_shared_channel` — carrier sense + RTS/CTS reservation (NAV) + randomized binary
  exponential backoff for hidden-node mitigation and collision recovery, with fairness bounds (a
  node may not exceed a configured number of consecutive channel claims) and per-session expiry /
  cancellation.

Selective retransmission uses a `BLOCK_ACK` bitmap: only missing frames are resent, within a bounded
retransmit budget; duplicates are suppressed; a lost `BLOCK_ACK` causes a bounded retransmit round.

## Four acknowledgement layers (never conflated)

A radio frame ACK is never reported as mission success. The layers are kept distinct and recorded
separately:

1. **Link ACK** — a `BLOCK_ACK` names which frames arrived / are missing.
2. **Delivery receipt** — the complete envelope was reconstructed and authenticated.
3. **Mission acknowledgement** — the receiving MissionEngine accepted or rejected the request.
4. **Mission result** — the requested work completed, failed, was blocked or cancelled.

The MAC layer owns the first two; the mission layers come from canonical peer ingress and are
correlated by `session_id` in the research records.

## Operator-controlled RF parameters

Physical operating parameters (whether to transmit, where, on what frequency, at what power) are
selected by the LOCAL operator on TX-capable hardware, exactly as in beta.2. Aithernet never chooses
a frequency, and there is no vendor/cloud authorization gate. The MAC layer and mesh model add no
new transmission path: automated qualification uses the deterministic virtual medium and GNU Radio
loopback and **does not radiate**.

## Revocation and offline operation

Mesh membership and node enrollment can be revoked; revocation takes effect on the next message /
heartbeat. Nodes cache mesh membership and revocation locally so they can authorize peers offline.
Standalone nodes operate fully without the hosted platform.
