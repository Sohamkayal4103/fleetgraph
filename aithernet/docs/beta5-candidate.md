# beta.5 candidate — built + verified, NOT published

Closes the four beta.5 release blockers (complete pre-install verification, self-contained RF-MCP
delivery, packaged personal-workstation service, correct artifact kinds) plus the MCP diagnostics
robustness fix. **beta.4 is untouched and remains the immutable prior candidate.** Do not publish
beta.5 without explicit approval.

## Commits (this branch, beta-qualification)
- `42b198a` Area 5 — MCP CLI fails cleanly on non-JSON node responses (no JSONDecodeError traceback)
- `3311ba8` Area 4 — accurate release artifact kinds (no more all-wheel) + `public_key_pem`
- `3bb76f2` Area 1 — complete customer release verification + `aithernet release verify`
- `5f4de40` Area 2 — self-contained, pinned-trust offline rf-mcp bundle
- `e5a07c7` Area 3 — personal-workstation node service mode (+ doctor execution identity)
- `324162b` Area 2/3 — wire `aithernet setup --components-bundle-dir`
- `e27bd72` Area 6 — bump to 0.8.0-beta.5

## Candidate
- Version `0.8.0-beta.5`, channel `early-access`. Built by `scripts/build_release.sh` into
  `~/.local/state/aithernet/lan-release-0.8.0-beta.5/archive/` (13 manifest artifacts + manifest.sig
  + SHA256SUMS). Release signing key id `aithernet-lan-betaqual-0.8.0-beta.5-20260620`.
- Manifest digest: `sha256:` over the canonical manifest; `aithernet release verify` →
  **RELEASE VERIFIED** (Ed25519 signature + 14 SHA256SUMS + every manifest digest).

### Artifact inventory (kind — accurate, not all wheel)
| Artifact | kind | size | sha256 (prefix) |
|---|---|---|---|
| aithernet_0.8.0~beta.5_amd64.deb | deb | 16,018,830 | 1f75dda8… |
| aithernet-0.8.0b5-py3-none-any.whl | wheel | 737,441 | a1a56050… |
| aithernet-0.8.0b5.tar.gz | sdist | 1,172,083 | 98beca54… |
| rf-mcp-0.1.0+aithernet.2-src.tar.gz | component-source | 91,339 | 200b7671… |
| rf-mcp-0.1.0+aithernet.2-src.tar.gz.sig | signature | 64 | 118f5349… |
| aithernet-component.pub | pubkey (PINNED) | 113 | 8398428f… |
| lock.json | metadata | 894 | 6231349… |
| aithernet-install.sh | installer | 11,566 | f1157f13… |
| sbom.json | metadata | 1,424 | 42b42863… |
| hardware-manifest.json | metadata | 1,902 | f6625cc6… |
| NOTICE.md / THIRD-PARTY-NOTICES.md | notice | 1,361 / 845 | 9c973063… / aa173289… |
| aithernet-release.pub | pubkey | 113 | c24e8b5b… |
| manifest.json / manifest.sig / SHA256SUMS | (verification set) | — | — |

## Verification evidence
- **Release signature:** `aithernet release verify <archive>` → manifest Ed25519 signature verified,
  14 files matched SHA256SUMS, all manifest artifact digests match. Manual openssl path still valid.
- **Customer download set (hosted, code-tested):** a published signed release now serves
  `manifest.json` + `manifest.sig` + PEM `aithernet-release.pub` + `SHA256SUMS` as PUBLIC downloads
  exactly matching the signed manifest; packages stay authenticated; revoked releases serve nothing
  (`tests/test_release_verification_files.py`).
- **Component signature (pinned trust):** the bundle is signed with the Aithernet component key whose
  public key is pinned in the spec (`components/rf-mcp/aithernet-component.pub`, shipped in the
  `.deb` `_component_specs`). `aithernet components install rf-mcp --bundle <archive>` →
  `ok:true`, signature valid, no missing/changed files. A bundle signed with any other key is
  rejected (`untrusted_key`) — `tests/test_component_bundle_pinned.py`.
- **Offline RF-MCP install (no upstream clone):** installed from the release archive bundle; full
  install builds the system-site-packages venv; MCP handshake → GNU Radio MCP 3.4.2, 17 tools incl.
  `acquire_iq_capture`.
- **User-service install:** `aithernet service install/start/stop/restart/status/logs`; the user
  unit runs as the desktop user with an optional 0600 `EnvironmentFile`; `doctor` reports the actual
  execution identity (`tests/test_service_mode.py`).
- **Artifact kinds:** accurate end to end (service → DB → manifest → portal Type column);
  `tests/test_release_artifact_kinds.py`.
- **MCP diagnostics:** `mcp status/diagnostics/tools/...` return a structured readiness failure (no
  traceback) when the node API is not serving JSON (`tests/test_cli_node_api_json.py`).
- **Package contents:** the `.deb` ships `/usr/bin/aithernet`, the pinned `aithernet-component.pub` +
  spec, the `release verify` + `service` CLI, and the kind inference; not `aithernet-hosted` on PATH.
- **Regression:** 18 release/component/service/cli/doctor/setup/packaging/hosted suites pass (exit 0).
- **Secrets/dev-paths:** no tracked secrets (only `.example`/`.pub`/test fixtures); no `/home/operator`
  in shipped `src/`/`components/`/`scripts/` (only pre-existing `deploy/compose/` operator comments).

## Remaining before a published beta.5 customer release (gated)
1. **Publish to prod** (operator approval — outward-facing). Prod control-plane image must be
   rebuilt to the beta.5 code so it serves the verification set; the existing prod signing key
   (`aithernet-prod-betaqual-20260620`) signs the hosted manifest.
2. **Clean-machine acceptance on beta.5** (the Phase 4 runbook, updated for the now-working
   `aithernet release verify` and `components install rf-mcp --bundle`).
3. Website (`aithernet-site`) install page: add the `aithernet release verify` step (the openssl
   path already works once the verification files are served).

Do not publish beta.5 or tag publicly until 1–2 pass.

---

## PUBLISHED to production (2026-06-21)

beta.5 is **published** in the prod control plane (`app.aithernet.online`), alongside the
preserved beta.4.

- Release id: `e82cc80f-4a33-4122-82a7-c0959f36253b`, channel `early-access`, status `published`.
- Signed with the established prod key `aithernet-prod-betaqual-20260620`; manifest digest
  `sha256:6203f449a19753452f6bc45ee30e0ecd566a8b9868843cea6e06002ceea3eaed`; served verification
  public key `v+qYpcHUDWYW1Hch2EXKXhVdVUeuQrlPD0B85Uz344A=`.
- Prod control-plane + portal + public-site rebuilt/recreated on beta.5 code; Postgres, MinIO,
  volumes, accounts, invitations, policies and **beta.4** preserved (only the app tier recreated).
- A build-infra fix was required: the control-plane Dockerfile now `COPY components` (the wheel
  force-includes `components/rf-mcp`); committed separately.

### Production verification (all pass)
- Both releases published (`0.8.0-beta.5`, `0.8.0-beta.4`); digests on the wire MATCH the candidate.
- **Anonymous** package download → 403; **unauthorized** principal (no membership) → `not_a_customer`.
- **Authorized** (admin) download of all 12 artifacts → bytes match the recorded/candidate digests.
- `aithernet release verify` on the **prod-served** set → RELEASE VERIFIED (Ed25519 vs the prod key,
  13 SHA256SUMS, all manifest digests); manual `openssl pkeyutl -verify` → "Signature Verified
  Successfully".
- Artifact **kinds** accurate in prod (deb/wheel/sdist/component-source/signature/pubkey/installer/
  notices/sbom/metadata — not all wheel).
- Manifest bytes **immutable** (two fetches identical, == signed digest).
- RF-MCP component bundle + detached sig + **pinned** `aithernet-component.pub` (== committed pin) +
  notices + metadata all downloadable.
- Release data **persists** after recreating only the control-plane container.
- **Revocation** verified on a throwaway release (`0.0.0-revoke-test`, left revoked) → 403 after
  revoke; beta.5/beta.4 untouched.
- Candidate archive at `lan-release-0.8.0-beta.5/archive/` **unchanged** (deb sha `1f75dda8…`).

### Notes
- The `aithernet-hosted release create` CLI prints a cosmetic traceback after creating a draft
  (it tries to render the not-yet-signed manifest); the create + uploads succeed. Minor, hosted-CLI
  only.
- A throwaway `0.0.0-revoke-test` release remains in prod in the `revoked` state (not downloadable,
  not in the public listing); harmless, can be pruned.
- Customer-portal UI view for the invited user is not exercised here (invitation acceptance is held
  per instructions); availability is verified via the public listing + authorized service path.

No GitHub release, no public Git tag, beta.4 not revoked, nothing installed on the dev host, no
enrollment code, no physical Pluto work.
