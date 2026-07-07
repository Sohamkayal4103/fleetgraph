# Aithernet 1.0.0-beta.9 — release record

**Data-collection architecture correction.** beta.8 shipped a client-managed Google Drive model.
beta.9 corrects it: enrolled clients record sanitized research data locally and upload it to the
**Aithernet hosted owner archive** using their enrolled node identity + a scoped Research Upload
Capability. The owner Google Drive archive is managed **server-side**; clients never manage Google
OAuth, `oauth-client.json`, or Drive sync. beta.8 artifacts are untouched; beta.9 reuses the
validated beta.7/beta.8 CatGPT Gateway runtime image.

## Product intent
The collection destination is the **product owner's** archive (owner/admin Google Drive), not the
client's Drive. Clients consent to upload sanitized research/diagnostic records to Aithernet. The
old client-Drive path remains only as an advanced standalone mode.

## Architecture (all reuses existing enrolled node identity + hosted infrastructure)

### Client node
- **Provider-agnostic recorder** (`data/research_recorder.py`): hooks generic MissionEngine terminal
  outcomes (completed / blocked / MCP-required-block) via `MissionEngine._record_owner_research` —
  no CatGPT/Codex dependency; works for CatGPT, Claude/OpenAI/Gemini API, local, Codex, Claude Code,
  future providers, and MCP tool missions. Records the Aithernet **mission architecture** (§G:
  mission input, coordinator trajectory, tool-use, coding-agent, outcome, environment, quality) with
  optional sanitized provider fields (`coordinator_provider`, `coding_provider`, `tool_providers`,
  `effective_model`, `provider_support_level`, `live_verified`). No provider secret is recorded.
- **Local queue + uploader** (`data/research_upload.py`): short-lived spool snapshots; seals each as
  a batch bundle (`data/batch.seal_batch`, gzip) and POSTs to control-plane `POST
  /v1/node/research/packages`, signed with the enrolled Ed25519 identity + `x-aithernet-research-
  capability` header; on a 202 ACK the snapshot is compacted (ACK-based cleanup). Failures leave the
  package queued; automatic retry; `aithernet data upload-now` forces a retry; service restart
  resumes the queue. Capability token stored 0600 in `config/research-capability.json` (never in
  `hosted.json`, which strips tokens).
- **CLI**: setup prompt → *“Enable sanitized research recording and upload to the Aithernet owner
  archive?”*; `data status` (consent, recorder_active, local_queue, uploaded, hosted_ingestion,
  owner_archive_upload, `drive_sync: managed_by_hosted`, next_step); `data verify` (no client Google
  requirement); `data consent [--grant|--withdraw]`; `data upload-now`. `data drive-client` / `data
  sync` relabeled ADVANCED standalone/local-archive mode.
- **config**: `research_upload_mode` (`hosted_owner_archive` default | `standalone_drive`),
  `research_consent`, `research_consent_at`.

### Hosted (control-plane)
- **8 tables** (`services/control_plane/models.py`, migration `_m_0004_research_tables`):
  research_upload_capabilities, research_packages, research_package_artifacts, research_quarantine,
  research_archive_jobs, research_archive_ledger, research_collection_policies,
  research_owner_archive_connections.
- **ResearchMixin** (`service_research.py`): capability minting (consent-gated, auto-approved for
  enrolled beta nodes per policy; scoped/revocable/expiring; token digest only); package ingestion
  (verify capability + size + schema, **server-side secret scan** over `data/redaction`, dedup by
  package sha, store payload in the hosted blob store, index metadata + artifact manifests, enqueue
  archive job, ACK; quarantine on residual secret — never archived); owner-archive worker
  (writes to owner Drive if connected server-side, else keeps jobs queued); admin views
  (status/packages/quarantine/jobs) + owner-Drive connect/disconnect (refresh token server-side
  only, never returned).
- **Endpoints** (`app.py`): node `POST /v1/node/research/capability`, `POST
  /v1/node/research/packages` (node-signed), `GET /v1/research/node-summary` (customer); admin
  `/v1/admin/research/{status,packages,quarantine,jobs,process-jobs,drive,drive/connect,
  drive/disconnect,capabilities/revoke,policy}`.
- **CLI** (`services.control_plane.cli research`): `status`, `connect-drive` (refresh token from an
  env var, never a flag/log), `drive-status`, `process-jobs`.
- **roles**: `research.upload` (tenant operator/admin), `research.read.summary` (customer),
  `research.archive.manage` (platform-only).

### Security
Node signs every upload; capability required + scoped to node/tenant/policy; revoked/expired/wrong-
node capabilities rejected. Two-layer secret scanning (client redactor + server scan) blocks/
quarantines API keys, OAuth/refresh tokens, VNC passwords, `CATGPT_GATEWAY_API_KEY`, cookies,
`hosted-prod.env`-style content, SSH/Google credentials; quarantined packages are never archived.
Redaction patterns strengthened for `sk-` keys and `NAME=value` secret assignments. No owner Google
credentials or Drive tokens on any client; owner refresh token server-side only.

## Versions
public `1.0.0-beta.9` · PEP440 `1.0.0b9` · Debian `1.0.0~beta.9` · `aithernet --version` → `aithernet 1.0.0b9`

## Tests
`tests/test_research_ingest_beta9.py` (hosted service + signed HTTP wire path) and
`tests/test_data_collection_beta9.py` (client provider-agnostic matrix, uploader, capability,
consent, status/verify) + reconciled beta.8 tests + portal admin/docs tests. Regression: research
spool/setup/drive, data platform, control-plane migrations/api/mesh, doctor, beta.6/7 CatGPT/
sandbox/accountability all green.

## Core repo
- Release branch `release/v1.0.0-beta.9`; merge to `main`: **`d9794a6`** (release record on top).
  Annotated tag `v1.0.0-beta.9` → target `d9794a6`. beta.8 untouched; **no `1.0.0-beta.8.post1`**.

## Artifacts (bundle: Ed25519 sig + `sha256sum -c`, 16/16, `release verify` PASS)
- `.deb` `aithernet_1.0.0~beta.9_amd64.deb` (16,171,982 B) —
  `sha256:112979e4abdc7206e51b0b1a6537b9537437f1b4a62f5d8c0ef3388262922ee4`
- wheel `aithernet-1.0.0b9-py3-none-any.whl` —
  `sha256:b63205c5760d541db8dcf57b1d26d111f67984fc7fa88913cce0b9292dd207c5`
- sdist `aithernet-1.0.0b9.tar.gz` —
  `sha256:560dab37687d374326a92baaa8300ae0b6d7c9711f9d317db70ae54ace7345ac`
- CatGPT runtime image (**reused from beta.7**)
  `catgpt-gateway-1.0.0-beta.7-linux-amd64.docker.tar.zst` (683,225,095 B) —
  `sha256:8fe11135b8a512d6a38ff954b58ee3537a9818c42a678ab787657b314bd09b1f`

## GitHub Release
`v1.0.0-beta.9` (prerelease), 18 assets incl. the reused CatGPT image tar:
https://github.com/adityapawar0401/aithernet/releases/tag/v1.0.0-beta.9
Public `.deb` hash `112979e4…` matches; public manifest Ed25519 signature "Verified Successfully".

## Hosted control plane (early-access, current)
Release id `9189d59c-c977-4d8d-ad9f-af387e71d32c`, 18 artifacts, prod-signed
`aithernet-prod-betaqual-20260620`, manifest digest
`sha256:7cfa7c49baa1fca8b57397997fc431903d63d705e33dfc2f7a595e619fff5a96`; **published + current**
(channel `current_release_id` → beta.9). beta.8 previous; beta.7/older archived. Redeployed via the
standard `deploy/production/upgrade.sh`: migration **`0004_research_tables`** applied (8 research_*
tables live in prod Postgres), the control plane serves all **14 research routes**, and the portal
bundle carries the beta.9 Research admin section + owner-archive docs (`1.0.0b9`, `research-consent`).
Anonymous `POST /v1/node/research/packages` → **401** (node signature required); admin endpoints not
anonymously accessible. Owner refresh token server-side only; never returned to any client API.

## Website
`aithernet-site` `ac1f397` (Pages: success). `/install` + `/release-notes` show beta.9; beta.8
previous.

## Fresh-client acceptance (shipped .deb)
version `1.0.0b9`; `release verify` PASS; `data research-consent`/`upload-now`/`status`/`verify`
present; `setup --research` (unenrolled) → "Research recording enabled locally. Owner archive upload
will begin after enrollment." (no Google prompt); recorder wrote provider-agnostic records for
CatGPT, an API provider with CatGPT absent, and a blocked mission (with sanitized blocker reason) —
`data status` shows mission_records=3, `owner_archive_upload: pending_enrollment`,
`drive_sync: managed_by_hosted`, files > 0; `upload-now` unenrolled → pending (no crash, no Google);
**no `oauth-client.json` anywhere**; no secret leaked to status/verify. The enroll→mint→upload→202
hosted wire path is proven by `tests/test_research_ingest_beta9.py` (signed request through the real
FastAPI app). Live coordinator/mission with a real ChatGPT/Claude login remains operator-gated.

## Servicing update — admin owner-Drive OAuth connect flow (server/portal-only)
The initial beta.9 admin UI asked the owner/admin to paste a Google OAuth **refresh token** to
connect the owner archive Drive. That is replaced by a proper **server-side OAuth consent flow**:
the admin clicks **Connect Google Drive**, approves on Google, and the control plane exchanges the
code and stores the refresh token **server-side only** (never returned to the browser). Delivered
as a **server/portal-only** update on `main` — the **beta.9 client package is unchanged** and
beta.9 remains the current client release; no beta.10, no artifact overwrite, no retag. Details:
- **Config** (`config.py`): `GoogleOAuthConfig` + `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`
  (secret read at use time only) / `GOOGLE_OAUTH_REDIRECT_URI` / `GOOGLE_OAUTH_ARCHIVE_ROOT_NAME`;
  `google_oauth_configured` gate. Least-privilege scope `drive.file`.
- **Server OAuth** (`owner_drive_oauth.py`, new, control-plane only): authorize-URL build,
  server-side code exchange, refresh, best-effort account email — httpx, sanitized errors, no
  secret ever logged. Reuses the shipped `RealGoogleDriveClient` so **no client-package change**.
- **Migration `0005_owner_drive_oauth`**: `research_oauth_states` (single-use state nonce bound to
  the admin) + additive columns on `research_owner_archive_connections`
  (`root_folder_name`, `account_email`, `scope`, `error_category`, `connected_via`).
- **Service** (`service_research.py`): `start_owner_drive_oauth` / `complete_owner_drive_oauth`,
  richer `owner_drive_status` (status enum + account/root/scope/error/last-sync + job counts), and
  an archive worker that (a) writes the `Aithernet Research Archive/tenants/<t>/nodes/<n>/packages`
  tree via the server-side OAuth client for ALL nodes from ONE connection, (b) leaves jobs queued
  when Drive is disconnected, and (c) flags `reconnect_required` on a revoked/expired token.
- **Routes** (`app.py`): `POST /v1/admin/research/drive/oauth/start` (CSRF), `GET
  /v1/admin/research/drive/oauth/callback` (state-validated, admin session, redirects back with a
  bounded result), `POST /v1/admin/research/drive/archive/process-jobs` alias. The manual
  refresh-token `connect` route remains an advanced fallback.
- **Portal**: **Connect Google Drive** button (no token paste), rich status cards
  (account/root/scope/last-sync/queued·synced·failed/error), "not configured" operator message,
  reconnect prompt, and an Advanced refresh-token disclosure.
- **Tests**: `tests/test_research_drive_oauth_beta9.py` (admin-only, CSRF, state mint/validate/
  single-use/mismatch, mocked code exchange, token stored server-side + never returned, unconfigured
  status, one-connection-many-nodes steady state, reconnect-required on revoked token, HTTP wire
  path) + updated `portal/src/test/research-admin-beta9.test.tsx`.
- **Docs**: `docs/owner-drive-oauth.md`.
- **Deploy env**: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`,
  `GOOGLE_OAUTH_ARCHIVE_ROOT_NAME`, `AITHERNET_ADMIN_BASE_URL` wired through `docker-compose.yml`.
  Values are supplied only in the hosted env file; absent config => the admin portal shows the
  "not configured" message and archive jobs stay queued (clients unaffected).

## Notes
- `data consent` / `data records` are pre-existing Stage-14E sub-groups; the beta.9 commands are
  `data research-consent` and `data research-records` to avoid clobbering them.
- The hosted `release create` prints a cosmetic `release_not_found` traceback at the manifest
  read-back (same as beta.7/8); the draft is created with all artifacts and sign+publish succeed.
- 2 pre-existing `test_agent_providers` failures (registry drift + PATH-sensitive auth) are unrelated
  and predate beta.9.
