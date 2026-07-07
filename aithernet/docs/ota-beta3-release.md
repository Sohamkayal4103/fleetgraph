# Aithernet 1.0.0-beta.3 — production release record (PUBLISHED)

Early-access channel. **Account-bound fleet, tenant-isolated secure meshes, and the complete OTA
link protocol.** beta.2 and all earlier releases remain immutable. 1.0.0-beta.3 becomes the current
early-access release; 1.0.0-beta.2 is the preceding release.

## What shipped (beta.3)

- **First-class mesh model + tenant isolation.** `Mesh`/`MeshMember` node store (migration 0020);
  pure `evaluate_mesh_ingress` guard; signed envelope `mesh_id` + `authority_id`; ingress
  enforcement in `transport.service` BEFORE canonical peer ingress / MissionEngine
  (`MeshAuthorizationError` → 403). An authenticated, key-trusted peer in another tenant is still
  rejected unless an explicit shared mesh exists.
- **Complete OTA MAC / link protocol** (`transport/ota/mac.py`): frame set BEACON/RTS/CTS/DATA/
  BLOCK_ACK/NACK/CANCEL/FIN/DELIVERY_RECEIPT (authenticated MacFrame codec); full link state
  machine; `point_to_point_tdd` + `rts_cts_shared_channel`; BLOCK_ACK-bitmap selective
  retransmission; deterministic `VirtualMedium` + `LossPlan`; slotted CSMA/CA contention with
  fairness/no-starvation and hidden-node handling. No radiation.
- **Four acknowledgement layers** kept distinct (link ACK / delivery receipt / mission ack /
  mission result) in MAC results and research records.
- **Hosted control plane**: hosted mesh model (`HostedMesh`/`HostedMeshMember`, migration 0003) +
  `/v1/meshes` CRUD; node rename (`/v1/fleet/nodes/{id}/rename`); enrollment-code list + revoke
  routes; `degraded` / `never_seen` fleet states; mesh memberships in the node listing; cross-tenant
  membership refused.
- **CLI**: `aithernet mesh create/list/inspect/join/leave/members/invite-node/revoke-node`
  (offline-capable, standalone), `enrollment disconnect`, `fleet heartbeat --now`.
- **Portal**: Meshes page (create/list/add-node), Nodes page (rename, pending-code revoke, mesh
  column), completed Account profile (account id, memberships, roles), degraded/never_seen states.
- **Research/datasets**: mac/mesh/fleet record builders with pseudonymized tenant/mesh/node ids;
  new classes mesh-authorization, cross-tenant-rejection, peer-selection, channel-access,
  rts-cts-outcomes, collision-recovery, delivery-latency, fleet-health.

## Identifiers

- Release ID: `6e64f856-dc0d-4d6a-98d7-ed0b499c9271` · status **published** · channel `early-access`
- Signing key id: `aithernet-prod-betaqual-20260620`
- Release manifest digest (prod-signed): `sha256:5b1dbdd50072d425f6fd0017aa691068c3cfad56e5191ce1c1cf32603bc4025e`
- RF-MCP component: `0.1.0+aithernet.2` `sha256:200b7671…` (unchanged from beta.2)
- 17 artifacts attached; prod blob `.deb` byte-identical to the qualified bundle.

## Production artifacts (prod blob == qualified bundle)

- `aithernet_1.0.0~beta.3_amd64.deb` — 16,105,670 B — `sha256:bcb3efd82d3a0c9dd5a039c62bb56b292ce3856f976f0f0c6a6edfd991c35975`
- `aithernet-1.0.0b3-py3-none-any.whl` — 865,118 B — `sha256:f74c6156429e2bdd6e48efcec93c6b1d080dbe49e19ad773b7eac2d33b2398bc`
- `aithernet-1.0.0b3.tar.gz` — 1,366,276 B — `sha256:1e7ce61eda3ed41f1a6133c69298446050217a20daaf7e4026574334ff62bfd1`

## Qualification (software; no radiation)

Deterministic regression + new beta.3 suites. New tests: 22 OTA MAC, 6 node mesh-isolation,
3 mac-research, 5 hosted mesh/rename/code/isolation. Hardware mocks + GNU Radio loopback +
deterministic virtual medium only — **no radiation; no physical OTA test fabricated**.

## Not claimed

No physical over-the-air transmission occurred during release qualification (mocks + loopback +
deterministic virtual medium; no radiation). Physical multi-node OTA, reboot/disconnect-reconnect,
and long-duration soak are operator-run. The live external-coordinator → coding-agent →
generated-modem scenario remains a documented post-release operator test using the operator's own
coordinator credential — it is not claimed to have passed. No hidden chain-of-thought collection.
