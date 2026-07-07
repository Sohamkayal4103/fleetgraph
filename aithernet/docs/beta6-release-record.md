# Aithernet 1.0.0-beta.6 — release record (PUBLISHED)

Ships the Aithernet-managed **CatGPT Gateway runtime as a signed release image** (Option A). A fresh
client needs no git clone, no `docker login`, no manual build, and no Docker Hub pull — only its own
ChatGPT/Claude web login via noVNC. Coordinator-only; the coding agent and mission engine are
unchanged; the beta.5 mission/routing/sandbox/secret fixes are preserved.

## Versions
- public `1.0.0-beta.6` · PEP440 `1.0.0b6` · Debian `1.0.0~beta.6`
- `aithernet --version` → `aithernet 1.0.0b6`

## Core repo
- Release merge to `main`: `ef708b5` (branch `release/v1.0.0-beta.6`).
- Annotated tag `v1.0.0-beta.6` → commit `ef708b5` (tag object `1077200b`).
- Control-plane follow-ups on `main`: `e63add5` (env-overridable `max_artifact_bytes`),
  `5892353` (wire it through the app-env anchor).
- beta.5 and beta.4 untouched; **no `1.0.0-beta.5.post1`** created.

## Artifacts (bundle verified: Ed25519 manifest sig + `sha256sum -c`, 16/16)
- `.deb` `aithernet_1.0.0~beta.6_amd64.deb` — `sha256:199d2762b1a1c5c2ea7730fa1f725941c77eb424ec9279bccae6a61151a2225a`
- wheel `aithernet-1.0.0b6-py3-none-any.whl` — `sha256:88a19eb186ce401e684cea79535c8dcf306b7d4aa079b86f4e957e496d821914`
- sdist `aithernet-1.0.0b6.tar.gz` — `sha256:dc915daa3cc60610bb55099a94e993ce3ed2a81979e52bba55e4a6845c3b2fd0`
- **CatGPT runtime image** `catgpt-gateway-1.0.0-beta.6-linux-amd64.docker.tar.zst` — 683,225,096 B,
  `sha256:e96ecdf932e81bd82a46ecad98905d68184a5a3fbbff095f17e792db975a8f6e`
  (tag `aithernet/catgpt-gateway:1.0.0-beta.6`, built from pinned upstream `79a1b69d`, MIT;
  manifest `kind: runtime-image`; SBOM `contains_web_credentials: false`).

## GitHub Release
- `v1.0.0-beta.6` (prerelease), 19 assets including the CatGPT runtime image tar. GitHub renders the
  `.deb` asset as `aithernet_1.0.0.beta.6_amd64.deb` (`~`→`.`); bytes are unchanged (`199d2762…`).

## Hosted control plane (early-access, current)
- Release id `47046745-7743-458a-af77-7bb6e486f3ba`, status `published`, channel `early-access`,
  **now current** (`hosted_release_channels.early-access.current_release_id`), 20 artifacts.
- Prod-signed `aithernet-prod-betaqual-20260620`, manifest digest
  `sha256:00636ecd633c9e3c258bbb5466e14beea4fea4d2d0c048645a040cc458e17b1f`;
  served `verification_public_key = v+qYpcHUDWYW1Hch2EXKXhVdVUeuQrlPD0B85Uz344A=`.
- Publishing the ~650 MiB image required raising the catalog's per-artifact bound: added
  `AITHERNET_RELEASE_MAX_ARTIFACT_BYTES=1073741824` (1 GiB) to `hosted-prod.env`, reloaded the stack
  via `deploy/production/upgrade.sh` (volumes preserved). Anonymous artifact download stays
  auth-gated (HTTP 403).

## Website
- `aithernet-site` `80afc42` (Pages: success). `/install` and `/release-notes` show beta.6 current;
  beta.5 listed as a previous release.

## Security
- No secrets printed; `hosted-prod.env` contents never inspected/printed.
- The runtime image contains no cookies, sessions, web credentials, bearer token, or VNC password
  (image env carries only the base-image / non-secret gateway defaults); those are runtime-only.
