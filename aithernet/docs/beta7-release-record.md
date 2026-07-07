# Aithernet 1.0.0-beta.7 — release record (PUBLISHED)

Product hardening on the validated beta.6 architecture. Keeps the beta.6 shipped-image packaging
(Aithernet ships the signed CatGPT Gateway image — no clone, no `docker login`, no Docker Hub pull).

## Fixes
1. CatGPT coordinator **model auto-persistence** (`catgpt-browser` persisted after noVNC login — no
   manual `agents configure coordinator --model`).
2. `provider-status` clarity: `configured_model` / `discovered_gateway_model` / `effective_model` /
   `managed_gateway` / `repair_available` / `repair_action`; never `mission_ready` without a model.
3. noVNC UX: `catgpt open`, `catgpt vnc-password` (local password only — never the web password or
   API token), and `…/vnc.html?autoconnect=1` URLs in setup/start/status.
4. Kept the beta.6 Docker/image packaging; beta.7-tagged signed image artifact.
5. Guided `coding-sandbox status|repair|restore` (confirmed sudo only; runtime-only; doctor reports
   a relaxed-hardening note with the restore command).
6. Mission preflight blocks a degraded coding sandbox early (before spending coding budget).
7. Tool-accountability record on every completion (`mission.tool_accountability` event +
   `mission.completed` payload).
8. MCP/GNU-Radio requirement enforcement (`missions/requirements.py`): a mission that *requires* MCP
   will not complete through a fallback.
9. `coding-task status|logs|artifacts <id>`; `mission events` surfaces `coding_task_id`.
10. Doctor managed-CatGPT readiness check (quiet when not configured).
11. Client portal **Documentation** section.
12. Portal release **current / previous / archive** grouping.
13. Website `/install` + `/release-notes` point to beta.7.

## Versions
public `1.0.0-beta.7` · PEP440 `1.0.0b7` · Debian `1.0.0~beta.7` · `aithernet --version` → `aithernet 1.0.0b7`

## Core repo
- Release merge to `main`: `65ece1d`. Annotated tag `v1.0.0-beta.7` (object `49c1d67d`) → `65ece1d`.
- beta.6 untouched; **no `1.0.0-beta.6.post1`**.

## Artifacts (bundle: Ed25519 sig + `sha256sum -c`, 16/16, `release verify` PASS)
- `.deb` `aithernet_1.0.0~beta.7_amd64.deb` — `sha256:a0a016cf0e187b36041fc59a94519b3d27f5b085b95d44e1b751cb69f68304c2`
- wheel `aithernet-1.0.0b7-py3-none-any.whl` — `sha256:4046ba77e5640dcace5deac28f5297dbaef9b02152320f9931397154665287f1`
- sdist `aithernet-1.0.0b7.tar.gz` — `sha256:afd0fa60e06544a630c79e4bf2025285b9813ec9695f46340c64333e67a13f8e`
- CatGPT runtime image `catgpt-gateway-1.0.0-beta.7-linux-amd64.docker.tar.zst` — 683,225,095 B,
  `sha256:8fe11135b8a512d6a38ff954b58ee3537a9818c42a678ab787657b314bd09b1f`
  (tag `aithernet/catgpt-gateway:1.0.0-beta.7`, pinned upstream `79a1b69d`, MIT, `runtime-image`).

## GitHub Release
`v1.0.0-beta.7` (prerelease), 19 assets incl. the CatGPT image tar. `.deb` renders as
`aithernet_1.0.0.beta.7_amd64.deb` (`~`→`.`); bytes unchanged.

## Hosted control plane (early-access, current)
Release id `46c46dcf-5dc8-4b19-917a-0e60a5e3c60a`, 20 artifacts, prod-signed
`aithernet-prod-betaqual-20260620`, manifest digest `sha256:bcdadb91cf5d3aa1f747a03286a48cf9df3feaa6e697297275b174056d24bbcb`;
**current** on early-access. beta.6 previous; beta.5/beta.4/older archived. The 1 GiB
`max_artifact_bytes` bound from beta.6 sufficed (no prod limit change). Anonymous download stays
auth-gated (403). Portal + control-plane redeployed via `deploy/production/upgrade.sh`.

## Website
`aithernet-site` `0f1762b` (Pages: success). `/install` + `/release-notes` show beta.7; beta.6 previous.

## Security
No secrets printed; `hosted-prod.env` never inspected/printed; no VNC password / API token / web
credentials leaked; the runtime image ships no cookies/sessions/credentials.
