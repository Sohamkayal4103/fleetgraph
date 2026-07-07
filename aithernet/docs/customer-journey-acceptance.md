# Aithernet Customer-Journey Acceptance Record

Tracks the end-to-end first-customer journey milestone (plan: stabilize → distribution →
candidate → publish → clean-install → providers → physical SDR → update/rollback → data).
Each capability is classified: **implemented / auto-tested / manually-tested /
provider-tested / physically-tested / clean-machine-tested / not-tested.**

Branch: `beta-qualification`. No public release, no public Git tag.

---

## Phase 0 — Preserve & stabilize  ✅ (auto-tested where applicable)

| Item | Evidence | Status |
|---|---|---|
| Commit rf-mcp `+aithernet.2` (bounded receive-only IQ tool) | commit `281b33b` (build.sh + component.toml + patches/0002) | implemented |
| Push unpushed Pluto-dedup `87256aa` + new work | pushed `125c28f..b3dcad1` → `origin/beta-qualification` | done |
| Host hygiene | `openrouterkey.txt` 0664→0600; `rf_worker_agent.db` 0666→0640 | done |
| README developer-path removal | commit `b3dcad1`; `grep /home/operator README.md` → none | done |
| Working tree clean | `git status --porcelain --untracked-files=no` empty | done |

## Phase 1a — rf-mcp auto-wiring (the core product fix)  ✅ auto-tested

The GNU Radio MCP launch now resolves from the installed managed component (no developer
checkout, no hand-set `AITHERNET_GNURADIO_MCP_COMMAND`).

- Code: commit `3c83023` — `components/__init__.py` (`installed_mcp_launch`), `config/loader.py`
  (`_autowire_managed_mcp`), `doctor.py` (`mcp_wiring` check), `wizard.py`, `node.yaml`.
- **Proof (clean shell, no dev env vars):** `load_config` resolved
  `command=uv`, `args=['run','--no-sync','python','main.py']`,
  `cwd=~/.local/share/aithernet/components/rf-mcp`, `check_stdio_ready=[]`.
- Tests: `tests/test_mcp_component_wiring.py` (5) + components/doctor/wizard suites → **67 passed**.
- doctor: explicit env still wins (`mcp_wiring` = "explicit config" on this dev host, by design).

Classification: **implemented + auto-tested**. Clean-machine proof deferred to Phase 4.

## Phase 1b — portal de-placeholder branding  ✅ auto-tested

- `BRAND = 'Aithernet'` (was `'PLACEHOLDER BRANDING'`); title `Aithernet Portal`; header
  `Aithernet Portal`; example `NODE_VERSION` 0.8.0-beta.1 → 0.8.0-beta.4. Commit `f800b54`.
- Customer/admin release pages already render real version/channel/artifacts via `DataView`
  (error → loading → empty → ready) with an openssl+sha256 verify section.
- `npm run build` (tsc --noEmit + vite) clean; `tests/test_hosted_packaging.py` → **10 passed**
  (anti-placeholder scan of source + rebuilt dist).
- NOTE: portal frontend **not yet redeployed** — image rebuild is gated to Phase 3.

## Phase 1c — distribution persistence (read-only verification)  ✅

- Prod control-plane (`aithernet_prod-control-plane-1`) mounts named volume
  `aithernet_prod_blobs` → `/var/lib/aithernet/blobs`; `AITHERNET_HOSTED_BLOB_ROOT` matches;
  `AITHERNET_HOSTED_STORAGE_BACKEND=local` → release bytes persist on that volume (survive
  container recreation). MinIO present but not the active release sink.
- Metadata: prod Postgres schema migrated — `/health/ready` → `schema_ok:true`,
  `pending_migrations:[]`, `environment:production` (tables `hosted_releases`/`_artifacts`/
  `_channels` present).
- Current state: `/v1/public/releases` → `{"releases":[],"verification_public_key":null}` (no
  release published yet — clean slate for beta.4).
- **Phase 3 prerequisite discovered:** prod has NO release signing key configured
  (`AITHERNET_RELEASE_SIGNING_KEY_ID` / `_KEYS` empty). Publication must supply the signing
  seed (via `aithernet-hosted release sign --signing-key-file`) or set the prod env first.

## Phase 2 — rf-mcp runtime + complete beta.4 candidate  ✅ built + verified

**The central product fix landed and is proven end to end.**
- Patch 0002 applies; `components install rf-mcp` → `verify ok` (`0.1.0+aithernet.2`); commit `278c7c6`.
- **Component now actually runs GNU Radio:** the build creates a SYSTEM-site-packages venv on
  system python3 (apt GNU Radio is importable; isolated uv-3.14 could not). In-process MCP
  handshake against the managed component (no dev checkout) → **GNU Radio MCP 3.4.2, 17 tools
  incl. `acquire_iq_capture`** (patch 0002). `doctor` mission_subsystem READY.
- `manager.install` now runs the recorded `build_command` after provenance verification (bounded,
  no-shell), so a fresh `aithernet components install rf-mcp` is immediately runnable (`--no-build`
  for offline). Source-only inventory excludes `.venv`/build dirs.
- **beta.4 bundle built** by the new committed `scripts/build_release.sh` (commit `e1efaa2`) into
  `~/.local/state/aithernet/lan-release-0.8.0-beta.4/archive/` (13 artifacts):
  - wheel `aithernet-0.8.0b4-py3-none-any.whl` (sha `34da2835…`)
  - sdist `aithernet-0.8.0b4.tar.gz` (sha `604ecc24…`)
  - deb `aithernet_0.8.0~beta.4_amd64.deb` (16,019,434 B, sha `e05b9193…`)
  - `rf-mcp-0.1.0+aithernet.2-src.tar.gz`, `lock.json`, installer, NOTICE/THIRD-PARTY-NOTICES,
    `sbom.json`, `hardware-manifest.json`, `aithernet-release.pub`, `manifest.json`, `manifest.sig`,
    `SHA256SUMS`. signing_key_id `aithernet-lan-betaqual-0.8.0-beta.4-20260620`.
  - **Offline verify:** `openssl pkeyutl -verify` → "Signature Verified Successfully";
    `sha256sum -c SHA256SUMS` → all OK.
- **.deb correctness:** `Depends: python3 (>= 3.11), python3-venv`; `/usr/bin/aithernet` present;
  `aithernet-hosted` NOT on PATH; bundles rf-mcp spec `+aithernet.2` + both patches under
  `_component_specs/`; ships the auto-wire loader + install build step. Packaging tests: **37 passed**.

Classification: **implemented + auto-tested**; clean-machine/physical proof deferred to Phase 4/6.

---

## Phase 5 — provider & API experience (this host)

**Providers — selected == executed proven; honest fail-closed.**
- `agents provider-status` with a concrete config resolves coordinator=`gemini_cli` (exec 0.46.0)
  and coding=`codex_cli` (exec 0.137.0), `runtime_identity=workstation`, `executed_provider`
  matches selection.
- **Codex coding (LIVE, billed): PASS** — `agents test coding --live` → `ready:true`,
  `real_request:true`, `executed_provider:codex_cli`, `nonce_validated:true`, isolated workspace
  cleaned (no files left), 5.3 s, `client_version codex-cli 0.137.0`. **provider-tested.**
- **Gemini coordinator (LIVE): fail-closed (honest)** — `ready:false`, `executed_provider:gemini_cli`.
  Root cause: the Gemini CLI requires `GEMINI_API_KEY` for non-interactive inference (the OAuth
  `google_accounts.json` is not sufficient here); no key is configured on this host. The system
  reported NOT ready rather than faking readiness — the correct behavior. Re-runnable once a
  `GEMINI_API_KEY` (or a different coordinator, e.g. `anthropic`/`openai_compatible`) is configured.
- No secrets/tokens/paths/responses were printed by any provider command.

**Mission ingress — not prompt-only.** A typed mission submitted through the authenticated REST
API reaches the canonical `MissionEngine` and returns status/events (it is not a prompt passthrough).
Confirmed by the ingress suites passing: `tests/test_missions.py` (`POST /missions` → create →
emit event → `GET /missions/{id}` status) and `tests/test_comms.py` (inbound peer message →
`create_mission` + enqueue). The architecture audit independently traced 5 implemented+tested
ingress paths (CLI, REST, agent-message, external-agent gateway, peer), all converging on
`NodeRuntime.create_mission`. Classification: **auto-tested** (live single-node REST demo deferred
to the clean client in Phase 4 to avoid polluting the dev node DB / needing a billed coordinator).

## Phase 3 — PRODUCTION PUBLICATION (GATE 1, operator-approved)  ✅ published + verified

beta.4 is **live in the production control plane** (`app.aithernet.online`).
- Signing key: a fresh prod Ed25519 key was generated and stored in `hosted-prod.env` (0600, seed
  never printed/committed); key id `aithernet-prod-betaqual-20260620`, pubkey
  `v+qYpcHUDWYW1Hch2EXKXhVdVUeuQrlPD0B85Uz344A=`. Only the `control-plane` container was
  force-recreated (named volumes/data preserved; no `down -v`).
- Published via `aithernet-hosted release create/sign/publish` inside the prod container (10 content
  artifacts; the hosted control plane generated + signed its own manifest). Release id
  `2c3deea6-0d87-4142-afe1-f421fc996e50`, status `published`, manifest_digest
  `sha256:fc120d37…`.
- **Verification (all pass):**
  - `/v1/public/releases` lists beta.4 + `verification_public_key=v+qY…` (was null).
  - Artifact digests on the wire MATCH the local candidate (deb `e05b9193…`, whl `34da2835…`).
  - **Anonymous `.deb` download → HTTP 403** (auth required).
  - Manifest signature verifies with the served pubkey; manifest_digest matches stored.
  - Stored `.deb` blob on the `aithernet_prod_blobs` volume hashes to `e05b9193…`.
  - **Release survives control-plane container recreation** (still `published`).
- **Deferred to Phase 4 (clean client):** authorized customer/portal download (needs a logged-in
  tenant user with accepted policies). Live revoke not exercised (would revoke the published
  release; revocation is unit-tested + audit-confirmed).
- **Known cosmetic issue (fix in beta.5):** the `release create` CLI does not set artifact `kind`,
  so the hosted manifest labels every artifact `kind:"wheel"`. Informational only — integrity is by
  sha256/manifest, not kind. Track: teach `release create` to infer kind from filename.

## Phase 4 — preparation (this host)  ✅ ready for the operator

- **Prod SMTP verified** (first real send): `aithernet-hosted email-test --to mspawar@ucsc.edu` →
  `outcome:"sent"` via SMTP (`adityapawar@aithernet.online`). Operator should confirm inbox receipt.
- **Production invitation created + emailed** to `mspawar@ucsc.edu`: id
  `8a7dc453-fff0-4c05-a2af-9be83abe9359`, role `tenant_admin`, proposed tenant "UCSC Early Access",
  status `pending`, expires `2026-06-28`. The one-time token was delivered **only via the SMTP email**
  (redacted from all logs; DB stores a token digest only). Required policy "Aithernet Early-Access
  Evaluation Terms" (`ea-2026-06-20`) is published and will be presented at acceptance.
- **Phase 4 runbook produced:** `docs/phase4-clean-client-runbook.md` — exact customer-perspective
  steps for the clean Ubuntu 24.04 amd64 physical machine (invitation → account → policy → download →
  verify → install → setup → rf-mcp → providers → service mode → enroll → doctor → portal → mission →
  restart), with embedded digests/IDs and inline `⚠️ DEFECT (beta.5)` flags.
- **MCP crash scope determined:** the `mcp status/tools/diagnostics` JSONDecodeError is **outside** the
  required journey (setup/doctor/components-verify/enroll/mission do not call it; the service is up
  when a customer inspects MCP). Recorded for beta.5; does not block Phase 4.
- **beta.4 verification/install gaps found (cannot fix without modifying/re-publishing beta.4):**
  (1) no public detached manifest signature + PEM key → manual Ed25519 pre-install verify not possible
  (SHA-256-over-TLS + automatic node verification used instead); (2) released rf-mcp lacks the
  component sig/pubkey → install needs `--upstream-git` (manual git clone); (3) only a system service
  (`User=aithernet`) is shipped, not a `systemctl --user` unit for the workstation provider identity;
  (4) manifest artifact `kind` cosmetically labeled `wheel`. All tracked in the runbook §20 for beta.5.

## Pending (gated / requires clean physical client)
- Phase 4/6/7: clean-machine install, physical Pluto, update/rollback (separate physical box).
- Phase 5: live Gemini+Codex provider qualification (this host).

## Known pre-existing issues (not caused by this work)
- `tests/test_dotenv_loading.py::test_dotenv_values_are_loaded` fails only when run in a batch
  (env-leak from another test); passes in isolation. Confirmed pre-existing by reproducing on
  the pre-Phase-1a tree. Suite-isolation bug, unrelated to the MCP/loader change.
