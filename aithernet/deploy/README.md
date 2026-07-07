# Aithernet production deployment (Stage 14A)

This directory holds the operational assets for running an Aithernet node as a long-running
service outside the development checkout. Nothing here changes the mission engine, coordinator,
transport, RF backends, or distributed protocols — it makes the completed node safely
installable, runnable, upgradeable, recoverable, observable and supportable.

> The external RF backends (`gr-mcp`, `marconi`) are operator-pinned and never modified by any
> Aithernet command, including upgrades.

## 1. Production directory layout

A node keeps all persistent state beneath an explicit **state root** (set `node_state_root` in
`node.yaml`). The deterministic layout (created by `aithernet node provision`) is:

```
<state_root>/
├── config/        node.yaml + aithernet.env (operator config; secrets via env only)
├── db/            SQLite database (aithernet.db)
├── identity/      Ed25519 node identity (mode 0700)
├── run/           PID + runtime state
├── logs/          rotating application logs (JSON lines)
├── backups/       node backups + rollback snapshots
├── rf/            per-backend RF workspaces (containment boundary)
├── coordinator/   coordinator runtime scratch
├── coding/        coding-agent workspace
├── artifacts/     content-addressed artifact store
└── tmp/           temporary files
```

Production never depends on the repository working directory; the node resolves absolute paths
from its config.

## 2. Provision

```bash
aithernet node provision /srv/aithernet/node \
    --node-id "$(uuidgen)" --node-name "node-1"     # idempotent; --dry-run shows actions
aithernet identity initialize --config /srv/aithernet/node/config/node.yaml   # generate identity
aithernet node preflight   --config /srv/aithernet/node/config/node.yaml      # validate
```

Provisioning never overwrites an existing identity or database; re-running is safe.

## 3. Preflight

`aithernet node preflight` validates the config file, database accessibility + migration state,
node identity, directory permissions, coordinator/coding-agent executables, RF backend commands
and working directories, workspace containment, port availability, and flags unsafe development
paths. It returns categorized, sanitized results and exits non-zero on any failure. It never
prints secrets or a full environment map.

## 4. systemd

Copy `deploy/systemd/aithernet.service` (or generate a tuned unit with
`aithernet node systemd-unit <state_root> --node-id <id>`), edit the `User`/paths/`EnvironmentFile`,
then:

```bash
sudo cp deploy/systemd/aithernet.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aithernet
```

The unit is working-directory independent, runs `node preflight` as `ExecStartPre`, restarts with
bounded backoff, stops gracefully on `SIGINT` with a 45s timeout, reaps the whole subprocess tree
(`KillMode=mixed`, so no orphan gr-mcp/coordinator/coding processes), and logs structured JSON to
the journal. Secrets come from the `EnvironmentFile`, never the command line. Docker is **not**
the primary production path (it may be documented as optional future work).

## 5. Health semantics

- `GET /health` / `GET /health/live` — liveness: returns immediately, never touches the database.
- `GET /health/ready` — readiness: HTTP 200 only after schema migrations complete, repositories
  initialize, and the mission/transport workers (when enabled) and the default RF backend (when
  `rf_backends.require_default_ready: true`) are available; otherwise HTTP 503. An experimental,
  non-autostart backend (Marconi) failing **degrades** an optional component and never makes the
  node unready.

CLI: `aithernet health live`, `aithernet health ready` (exit 1 when not ready).

## 6. Graceful shutdown

`SIGINT` (systemd stop) runs the ordered shutdown: stop accepting work → stop artifact transfers
→ stop transport delivery (persist in-flight) → stop mission worker at atomic boundaries (release
leases) → stop RF sessions and terminate coordinator/coding subprocesses. Pending outbox messages
and reply waits stay durable; interrupted missions recover on restart; shutdown is bounded.

## 7. Backup & restore

```bash
aithernet backup create  --config .../node.yaml          # consistent SQLite + config + public id
aithernet backup list    --config .../node.yaml
aithernet backup verify  <archive.tar.gz>                # checksum every file
aithernet backup restore <archive.tar.gz> --config .../node.yaml   # confirm; keeps a rollback snapshot
```

Backups use the SQLite online-backup API (never a raw copy of a live file), include the config
(which holds no inline secrets), the **public** identity document, and a manifest with the app
version, schema migrations, and a SHA-256 per file. The **private** identity key is included only
with `--include-private-identity`. Restore verifies every checksum, validates schema
compatibility, **refuses to overwrite a different node identity** (never merges identities),
preserves the current installation as a rollback snapshot, and supports `--dry-run`.

## 8. Upgrade & rollback

```bash
aithernet upgrade preflight --config .../node.yaml        # version + migration state + readiness
aithernet upgrade apply     --config .../node.yaml        # backup → migrate → health-check
aithernet upgrade verify    --config .../node.yaml        # schema valid + no pending migration
aithernet upgrade rollback  <pre-upgrade-backup> --config .../node.yaml
```

`apply` always creates a pre-upgrade backup (the rollback point) before migrating and stops on
any validation failure (the backup is retained). The workflow records — and never modifies —
external RF backend revisions. It never self-updates code from the network and never
automatically downgrades a migrated database; rollback is an explicit restore.

## 9. Logging

Production logs are line-delimited JSON (timestamp, severity, component, node id, optional
mission/message/backend ids, sanitized error type) on stderr (systemd journal) and an optional
rotating file under `logs/`. A redaction filter guarantees no private key, signature, raw
envelope, complete remote payload, prompt, credential, or environment map is ever written. Use
the journal or `logrotate` for retention.

## 10. Diagnostics bundle

```bash
aithernet diagnostics bundle --config .../node.yaml
```

Writes a sanitized `.tar.gz` for support containing only operational facts: version, migration
status, SQLite integrity result, health/component status, the config **structure with secrets
removed**, peer/mission/transfer counts, RF backend status, filesystem capacity, and a bounded
log tail. It excludes database contents, the private identity, signatures, raw messages, prompts,
credentials, RF captures, and environment values.

## 11. External RF backend requirements

The legacy GNU Radio backend (`gr-mcp`) and experimental Marconi backend are external,
operator-pinned checkouts launched as MCP subprocesses. Aithernet never imports, modifies, pulls,
or upgrades them. Point `rf_backends.backends.<id>.cwd`/`command` at the pinned checkout; upgrades
record their revisions but never touch them.

## 12. Managed SDR hardware (Stage 14B)

Managed hardware is **off by default** — a node with no SDR hardware stays fully usable for
simulation, coding, coordination, and artifact analysis. Enable it in `node.yaml` under the
`hardware` block. The inventory worker runs the configured discovery providers on a bounded
interval, persists **factual** devices/capabilities/health (unknown stays unknown — nothing is
ever invented), and the lease service grants durable, conflict-checked, bounded leases that
authorize managed physical-device use. Inventory polling and lease renewal create **no MissionStep**.

```yaml
hardware:
  enabled: true
  discovery_interval_seconds: 60
  health_interval_seconds: 30
  lease_duration_seconds: 120
  lease_renew_interval_seconds: 30
  stale_lease_grace_seconds: 30
  operator_lease_actions_enabled: false     # gate operator acquire/release/revoke (API/CLI/UI)
  providers:
    usrp:   { kind: uhd,    required: false }              # uhd_find_devices
    soapy:  { kind: soapy,  required: false }              # SoapySDRUtil --find
    lab:    { kind: static, devices: [ { vendor: Ettus, product: B210, serial: "30AD3F8",
              driver: uhd, device_kind: sdr,
              capabilities: { rx: true, tx: true, channels: 2,
                              frequency_ranges: [[70000000, 6000000000]] } } ] }
  required_devices:
    - { selector: "30AD3F8", description: "lab B210 (gates readiness when missing)" }
  bindings:
    - { device_selector: "30AD3F8", backend_id: legacy_gr_mcp, device_args: { driver: uhd } }
```

Discovery provider kinds: `soapy`, `uhd`, `static` (inline operator descriptors), `static_file`
(descriptors JSON file), and `command` (a custom discovery command emitting a strict JSON schema).
An absent tool reports **unavailable** (it never crashes startup unless that provider is `required`).

The coordinator may select **only** a known persisted `device_id`, a known `backend_id`, a bounded
purpose, and bounded RF requirements (direction/channels/frequency/sample-rate) via the atomic
`rf_device` route — it can never supply a device string, driver argument, shell fragment, path,
environment, or endpoint. A deterministic binding adapter derives the backend device-arguments from
the persisted device plus an administrator-approved binding.

Commands and endpoints (read endpoints always available; lease/admin actions require
`operator_lease_actions_enabled`):

```bash
aithernet hardware providers                  # discovery providers + availability
aithernet hardware refresh                    # run discovery once
aithernet hardware list                       # discovered devices (sanitized)
aithernet hardware show <device-id>
aithernet hardware capabilities <device-id>
aithernet hardware health <device-id>
aithernet hardware lease list [--active]
aithernet hardware lease acquire <device-id> --backend-id legacy_gr_mcp --direction rx
aithernet hardware lease release <lease-id>
aithernet hardware lease revoke  <lease-id>   # requires confirmation
```

```
GET  /hardware/status | /hardware/providers | /hardware/devices | /hardware/devices/{id}
GET  /hardware/devices/{id}/capabilities | /hardware/devices/{id}/health-events
POST /hardware/refresh
GET  /hardware/leases | /hardware/leases/{id} | /hardware/leases/{id}/events
POST /hardware/leases/acquire | /hardware/leases/{id}/release | /hardware/leases/{id}/revoke
```

Readiness: hardware inventory readiness is visible; a node with **no** attached device is not
unready by default. A `required` provider that is unavailable, or a configured required device that
is missing, gates readiness. `aithernet node preflight` reports configured providers, whether each
tool is on PATH, malformed static bindings, and required-device selectors. Backups include the
inventory + lease history (whole-DB), and restore preserves stable device identities. The external
RF backends (`gr-mcp`, `marconi`) are never modified by any hardware action.

## 13. Real SDR hardware qualification (Stage 14C.1)

Stage 14C.1 validates the managed-hardware runtime against ACTUAL attached SDR hardware. The
production `soapy`/`uhd` providers now run a deep per-device probe (`SoapySDRUtil --probe` /
`uhd_usrp_probe`) to record factual frequency/sample-rate/gain/antenna/channel/format capabilities
(unknown stays unknown). Probing opens the device, so it runs on demand (first discovery or a
qualification run) and never while a device holds a lease.

RF flowgraph execution runs in a **separately configured** GNU Radio interpreter
(`hardware.qualification.gnuradio_python`, default `/usr/bin/python3`) — Aithernet's own venv never
imports `gnuradio`. The two environments stay independently configurable.

```bash
aithernet hardware qualify preflight <device-id>     # read-only; never opens the device
aithernet hardware qualify run <device-id>           # REAL RX qualification (operator-invoked)
aithernet hardware qualify run <device-id> --survey  # + 2.4 GHz energy survey
aithernet hardware qualify status <qualification-id>
aithernet hardware qualify report <qualification-id>
```

A qualification run is operator-invoked (RF actions require `operator_lease_actions_enabled`) and
performs, against the real device: capability probe → deterministic backend binding → full lease
lifecycle (acquire/conflict/renew/release/reacquire) → TX-exclusive **lease** check (no RF emitted)
→ a bounded **real RX capture** (default 2 MS/s, 3 s, 2.437 GHz, manual gain) imported into the
artifact store with full provenance (device + capability revision + lease + backend + RF params +
SHA-256). It classifies support honestly from evidence (`discovered → probed → rx_qualified →
tx_lease_qualified → capture_qualified → survey_qualified → disconnect_recovery_qualified`;
`unsupported`/`failed`/`not_executed`) — never "supported" just because a device was discovered.

Read-only surfaces: `GET /hardware/qualifications[/{id}[/checks]]`,
`/hardware/devices/{id}/qualification-status`, `/hardware/support-matrix`, `/hardware/measurements`,
and a dashboard **Qualification & support** card. No raw probe output, device paths, credentials,
or absolute paths are exposed. The external RF backends are never modified.

> **Duplicate-discovery note:** SoapyPlutoSDR lists one physical board twice (`PlutoSDR #0`/`#1` at
> the same `uri`). Identity uses stable facts (provider/driver/**URI**/serial), never the `#N`
> label or list index, so exactly one device record is persisted.

## 14. Multi-node field operation & soak validation (Stage 14C.2)

Stage 14C.2 validates the node under realistic multi-node, repeated-operation, failure, and
long-duration conditions (`field_validation` config block). Read surfaces are always available;
campaign/soak start, fault injection, and physical TX require `operator_actions_enabled` /
`fault_injection_enabled` / `transmit_authorized` (and confirmation for disruptive faults/TX).

```bash
aithernet field preflight --device-id <id>
aithernet field campaign create "<name>" --profile smoke
aithernet field campaign start <campaign-id> --device-id <id> --iterations 3 --survey-windows 2
aithernet field campaign status|report <campaign-id>
aithernet field soak start --seconds 1800 --interval 5
aithernet field fault list
aithernet field fault inject device_missing --device-id <id> --yes   # validation mode only
```

A single-SDR campaign cycles inventory refresh → RX lease → bounded **real capture** → artifact
import → lease release → rest, recording full per-iteration provenance and detecting leaked leases,
orphan subprocesses, and unbounded resource growth against explicit configured thresholds (Part O).
Sampling, lease cycling, and soak create **no MissionStep**. Fault injection performs only bounded,
predefined actions — never an arbitrary shell/network command. Honest acceptance classification:
`software_harness_complete → single_device_soak_complete → multi_node_ip_complete →
two_device_rf_complete → extended_soak_complete` (+ `blocked`/`failed`); a single SDR classifies
two-radio RF as `not_executed`. Read-only API: `GET /field/campaigns[/{id}[/checks|measurements|
faults|nodes|report]]`. Dashboard: a **Field** view (campaigns, classification, per-check status as
accessible text). No credentials/tokens/private identity/raw paths/command output are exposed; the
external RF backends are never modified.

## 15. Current limitations

- The Operations dashboard and ops API are **read-only**; destructive restore/upgrade/rollback are
  CLI-only (never exposed over an unauthenticated remote API).
- systemd hardening is conservative to stay compatible with GNU Radio subprocesses; tighten per
  deployment after validation.
- Container images are intentionally out of scope (documented as optional future work).

## External-agent interoperability gateway (Stage 14D)

The gateway lets authenticated **external** (non-Aithernet) programs — orchestration systems,
robotics, ground-station apps, local scripts — submit idempotent missions and receive durable
**signed** webhook / WebSocket callbacks with replay. It is **separate** from the node-to-node
peer transport and never duplicates it.

It is **OFF by default** (`external_agents.enabled: false` in `node.yaml`). Webhook and WebSocket
delivery stay disabled until explicitly enabled.

- **Identity / auth** — an operator provisions an agent with its Ed25519 **public** key
  (`aithernet external-agents create <name> --public-key <b64>`). Every request is signed over a
  canonical description (agent id + key id + method + target + timestamp + nonce + body digest);
  no secret is sent on the wire. Replay protection = timestamp skew + a persisted nonce cache.
  Key rotation + revocation are supported. An optional operator bearer credential is stored hashed
  and shown once.
- **Callbacks (SSRF policy)** — `external_agents.callbacks` enforces https-only by default, rejects
  userinfo/fragments/loopback/link-local/metadata/private destinations, resolves + re-validates
  **every** address before each delivery (DNS-rebinding protection), disables redirects, and bounds
  timeouts + response size. A private deployment may set `allow_private_networks: true` and an
  explicit `allowed_hosts` / `allowed_cidrs` allowlist.
- **Durable delivery** — a restart-safe outbox worker delivers signed webhooks with bounded
  exponential backoff + jitter, dead-letters after `maximum_attempts`, and supports manual redrive.
  All attempts, acknowledgements, and dead-letter transitions are persisted. WebSocket subscriptions
  push live and replay missed events from an acknowledged cursor. Notification/delivery activity
  creates **no** MissionStep.
- **Operations** — readiness exposes `interop_delivery_worker` when enabled; `aithernet
  external-agents diagnostics` reports sanitized counts (no secrets). Agent records, public keys,
  permissions, endpoints (sanitized policy), subscriptions, pending delivery state, acknowledgements,
  dead-letter evidence, and replay cursors are part of the standard consistent backup; restore
  preserves agent ids + sequence integrity.

See `examples/external_agent/` for a reference client and `aithernet external-agents --help`.

## Privacy, consent + data export (Stage 14E)

A local-first data platform: bounded local collection, explicit per-category consent, structured
redaction + pseudonymization, a durable export outbox of immutable sealed batches, a destination
abstraction (local archive / authenticated HTTP ingestion / Google Drive / air-gapped), retention
+ traceable deletion, and a dataset registry with lineage. **The node is fully functional with it
disabled.** Configure under `node.yaml` `data_platform` (off-leaning defaults).

- **Consent** — training + raw-artifact collection are OFF and require an accepted participation
  policy (`aithernet data consent show|grant|withdraw` / `POST /data/consent/*`) plus a per-
  category grant. Unknown/expired/withdrawn/superseded state behaves as **not authorized**;
  withdrawal blocks new exports immediately and reconciles (quarantines) queued records.
- **Redaction** — every candidate record is redacted + pseudonymized before it can be queued:
  secrets/tokens/keys/db-urls/home-paths/tracebacks are removed; emails/IPs/ids are pseudonymized
  (stable per tenant, separated across tenants). It is pseudonymization, not anonymization.
- **Export** — approved records seal into immutable gzip-compressed, optionally AES-256-GCM
  **encrypted** batches with manifest + records digests, delivered by a restart-safe worker
  (bounded backoff, dead-letter, redrive) that honours an operator pause + disk floor and creates
  **no** MissionStep.
- **Destinations** — local archive (default, path-safe, atomic), HTTP ingestion (signed +
  idempotent), Google Drive (optional, encrypted, disabled-by-default, never the live store), and
  air-gapped bundles (first-class). All credentials/keys are env-REFERENCED, never inlined.
- **Ingestion service** — a SEPARATELY runnable service (`python -m services.ingestion`,
  configured by `INGEST_*` env) that authenticates each batch (tenant+node signed request, replay
  protection), verifies digests under decompression-bomb bounds, persists batch/record metadata +
  blobs, issues receipts, and supports deletion + dataset lineage. It runs over a private network;
  no public Internet is required for Stage 14E.
- **Retention/deletion** — retention by category/purpose; traceable deletion requests propagate
  through dataset lineage (members excluded) and tombstone record content while preserving bounded
  audit evidence; unknown remote-deletion state is never reported as success.
- **Manual Drive verification** — `aithernet data destinations verify-drive <id>` is operator-only,
  requires real configured credentials, and never runs in ordinary tests or at startup.

Consent history, policies, export records, batch manifests, receipts, destinations (sanitized —
no plaintext credentials), deletion history, and the dataset registry + lineage are part of the
standard consistent backup; restore preserves ids, consent revisions, queued delivery state, and
dataset versions. Plaintext OAuth tokens / encryption keys are never written to backups.
