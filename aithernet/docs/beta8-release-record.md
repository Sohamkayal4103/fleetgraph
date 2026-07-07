# Aithernet 1.0.0-beta.8 — release record

Data-collection / Google Drive pipeline fix on the validated beta.7 architecture. beta.7 artifacts
are untouched; beta.8 **reuses the validated beta.7 CatGPT Gateway runtime image** (no re-tag, no
re-upload) — the app version advances to beta.8 while the managed sidecar image stays pinned.

## The defect (beta.7 client evidence)
`owner_full` consent was set and `data status` said "collection: enabled", but completed missions
wrote **nothing** to the local research spool; `data collect` / `data sync` did not exist; `data
verify --authorize` failed with a raw `DriveError: oauth-client.json not found`; and the CLI help
carried future-version "beta.10" labels.

## Fixes
1. **Automatic owner_full recording** — a guarded hook in `MissionEngine._handle_complete` writes one
   sanitized research record per completed mission (`data/research_recorder.py`). Never fails a
   completed mission. Records carry mission/run metadata, tool accountability, sanitized event
   summaries, coding-task ids + summaries, MCP tool calls, and RF-artifact manifest paths/hashes.
2. **Local spool recording** — records are secret-scanned before landing; residual-secret records
   are quarantined and never normalized/uploaded. After one completed mission, `data status` /
   `verify` show nonzero records and the spool contains files.
3. **Coherent CLI** — added `data collect`, `data sync`, `data research-records`, and a
   `data drive-client install|status` group; rewrote `data status`/`verify` UX (recorder_active,
   last_recorded_at, counts, drive state, exact next_step). All "beta.10" help text removed; no
   future-version labels shipped.
4. **Honest Google Drive OAuth** — Aithernet ships **no** embedded OAuth client (an installed
   desktop app cannot keep a client secret confidential). An operator installs their own Google
   *Desktop* OAuth client via `data drive-client install`; until then every Drive path degrades
   with a clear "local-only until repaired" message (never a raw `DriveError`). Secrets/tokens are
   never printed; the client secret is stored 0600.
5. **Status/verify/sync UX** — `sync` refuses clearly (structured reason + next_step) when no client
   is installed or Drive is not authorized; idempotent upload ledger; uploaded/skipped/failed
   counts.
6. **Guided setup** — answering **Y** enables owner_full, initializes the spool, activates automatic
   recording, and sets up Drive; if Drive cannot complete it says so ("local-only until repaired")
   and never claims a verified sync. `research_auto_sync` is only turned on when Drive is verified.
7. **Doctor** — `_check_research_collection` flags enabled-but-no-records, quarantine, Drive
   unavailable/not-authorized/pending with actionable guidance; quiet when collection is off.
8. **Client portal Documentation** — new "Research recording & Google Drive sync" section.
9. **config** — `data_platform.collection.research_auto_sync`; removed the shipped personal Drive
   root id (each client creates its own root; operator-overridable via env).

## Versions
public `1.0.0-beta.8` · PEP440 `1.0.0b8` · Debian `1.0.0~beta.8` · `aithernet --version` → `aithernet 1.0.0b8`

## Artifacts (bundle: Ed25519 sig + `sha256sum -c`, 16/16, `release verify` PASS)
- `.deb` `aithernet_1.0.0~beta.8_amd64.deb` (16,158,446 B) —
  `sha256:d50b59841219be18a2c604f371fecf0fb75760b579acb393df8ac309e6f328e6`
- wheel `aithernet-1.0.0b8-py3-none-any.whl` —
  `sha256:a53491078c64228c2028fcf5d16117eb43c2877a515be7d7cb8329224837de1e`
- sdist `aithernet-1.0.0b8.tar.gz` —
  `sha256:9fa3fd50c81d450c97147a5752bfa8e86fc9340ae629d3bf155a7f1727035b9b`
- CatGPT runtime image (**reused from beta.7**)
  `catgpt-gateway-1.0.0-beta.7-linux-amd64.docker.tar.zst` (683,225,095 B) —
  `sha256:8fe11135b8a512d6a38ff954b58ee3537a9818c42a678ab787657b314bd09b1f`
  (tag `aithernet/catgpt-gateway:1.0.0-beta.7`, pinned upstream `79a1b69d`, MIT, `runtime-image`).

## Tests
`tests/test_data_collection_beta8.py` (19) + `portal/src/test/research-docs-beta8.test.tsx` (1);
existing research/data/beta.7 suites green; portal 61 tests + build green; ruff clean on all new
code. Two pre-existing `test_agent_providers` failures are unrelated (registry drift + PATH-sensitive
auth readiness) and predate this change.

## Fresh-client acceptance (shipped .deb, deps vendored)
version `1.0.0b8`; `release verify` PASS; `data collect`/`sync`/`drive-client`/`research-records`
present; owner_full + a completed mission → local record appears (mission_records 1, files > 0);
`data status`/`verify` report recorder state + counts + Drive state + next action; `data sync`
auth-gated; `data verify --authorize` with no client → actionable install guidance (not a raw
DriveError); no secrets in written records; no "beta.10" in help. Live coordinator/mission with a
real ChatGPT/Claude noVNC login remains operator-gated (agent behavior unchanged from beta.7).

## Security
No secrets printed; `hosted-prod.env` never inspected/printed; no VNC password / API token / OAuth
token / web credentials leaked. The runtime image ships no cookies/sessions/credentials. Research
records are secret-scanned before write; quarantined records are never uploaded.

## Core repo
- Release branch `release/v1.0.0-beta.8`; merge to `main`: `673097d`. Annotated tag `v1.0.0-beta.8`
  → target `673097d`. beta.7 untouched; **no `1.0.0-beta.7.post1`**.

## GitHub Release
`v1.0.0-beta.8` (prerelease), 18 assets incl. the reused CatGPT image tar:
https://github.com/adityapawar0401/aithernet/releases/tag/v1.0.0-beta.8
Public `.deb` hash `d50b5984…` matches; public manifest Ed25519 signature "Verified Successfully".
GitHub renders the `.deb` as `aithernet_1.0.0.beta.8_amd64.deb` (`~`→`.`); bytes unchanged.

## Hosted control plane (early-access, current)
Release id `94cd901e-db66-4b58-843a-85886dbbe9d0`, 18 artifacts, prod-signed
`aithernet-prod-betaqual-20260620`, manifest digest
`sha256:02fe6d9a73374118c0d0eb8b8920436b146c4421f5bd1128450905a76640c615`; **published + current** on
early-access (channel `current_release_id` → beta.8). beta.7 previous; beta.6/beta.5/older published
(archived in the portal UI). The 1 GiB `max_artifact_bytes` bound from beta.7 sufficed (no prod limit
change). Anonymous artifact/release endpoints are not accessible (404, no data). Portal +
control-plane redeployed via `deploy/production/upgrade.sh` (volumes preserved; stack healthy); the
deployed portal bundle carries the beta.8 docs + `1.0.0b8` markers.

## Website
`aithernet-site` `aea19db` (Pages: success on rerun — first attempt hit the same transient GitHub
Pages deploy error as beta.7). Live `/install` + `/release-notes` show beta.8; beta.7 previous.
