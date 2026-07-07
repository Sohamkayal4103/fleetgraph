# CatGPT Gateway component — source & license notice

The Aithernet-managed **CatGPT Gateway** (the optional local coordinator model backend driven by
`aithernet catgpt`) runs the upstream project **CatGPT-Gateway** as a *pinned local component*.

- **Upstream:** https://github.com/GautamVhavle/CatGPT-Gateway
- **License:** **MIT** (Copyright (c) 2026 GautamVhavle) — see `LICENSE.upstream` in this directory.
- **Pinned reference:** commit `79a1b69d429fa9951d796289b60470fb595cbb42`
  (`2026-06-21`, tag/branch `main`).

## What Aithernet ships (Option A: the runtime image is shipped)

Aithernet ships a management wrapper **plus the pinned runtime image**, so a client never clones,
builds, or pulls CatGPT-Gateway from a public repo:

- **Aithernet-authored** management CLI (`aithernet catgpt setup|start|status|logs|stop`,
  `install-runtime`), a generated `docker-compose.yml` + `gateway.env`, a contract probe, and the
  coordinator-provider integration. These are original Aithernet work (MIT, part of Aithernet).
- **The runtime image** built from the pinned upstream commit, tagged
  `aithernet/catgpt-gateway:<version>` and shipped as a compressed `docker save` tar
  (`catgpt-gateway-<version>-linux-amd64.docker.tar.zst`) in the release bundle. `aithernet catgpt
  start` loads it locally (`pull_policy: never`) — no Docker Hub pull, no `docker login`.
- MIT permits redistribution of the software in compiled/binary form provided the copyright and
  permission notice are included: they ship as **this notice + `LICENSE.upstream`** in
  `notices/catgpt-gateway/` of the bundle. No upstream modification is made beyond the standard
  container build of the pinned commit.
- The image contains **no** ChatGPT/Claude cookies, sessions, passwords, or web-account data. The
  bearer token / VNC password are runtime-only (the 0600 `gateway.env` written by `catgpt setup`),
  never baked into the image, and the user performs their own web-account login via noVNC.

## Boundaries and privacy

- The gateway **only** provides coordinator model inference. It is never the Aithernet mission API,
  database, run queue, or event store.
- The API is bound to `127.0.0.1` by default and requires a local bearer token (stored by Aithernet
  in its 0600 managed secret store as `CATGPT_GATEWAY_API_KEY`, referenced by name only).
- The user signs into ChatGPT/Claude themselves in the local browser (noVNC). Aithernet never
  stores the browser session, cookies, or the user's web-service credentials, and never logs the
  bearer token or VNC password.
