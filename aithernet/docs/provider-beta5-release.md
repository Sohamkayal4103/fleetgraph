# Aithernet 1.0.0-beta.5 — product/runtime patch release

A **product/runtime patch** on top of the provider-neutral agent runtime (beta.4). **beta.4 and all
earlier releases remain immutable** — this release adds a new *optional* coordinator provider and
three targeted mission-reliability fixes discovered during beta.4 testing. It changes **no public
website, no admin/www routing, and no `app.aithernet.online` programmatic-compatibility routes**;
`v1.0.0-beta.4` installed-node behaviour stays compatible (all changes are additive or strictly
more truthful).

## What's new

1. **CatGPT-Gateway coordinator provider (`catgpt_gateway`).** An *optional* coordinator that talks
   to a **local**, browser-session-backed gateway (CatGPT-Gateway) which re-exposes a ChatGPT/Claude
   web subscription as an OpenAI-compatible endpoint. It lets a user drive **coordinator reasoning**
   from a subscription while keeping Codex CLI / Claude Code / … as a **separate** coding executor.
   Coordinator-only by default; api key optional (local); the browser session/cookies never reach
   Aithernet.
2. **Coding-agent sandbox readiness preflight** in `aithernet doctor`. Non-destructive bubblewrap
   probes (uid-map + loopback/net-namespace) with the Ubuntu AppArmor userns sysctl as context, so a
   node with a broken sandbox is reported truthfully (`[degraded] coding_sandbox`, not READY) with
   the exact remediation.
3. **Mission reliability fixes (real beta.4 issues):**
   - **Quota exhaustion clarity** — the recoverable `blocked_provider_quota` message now also offers
     the local CatGPT Gateway as a concrete alternate coordinator, and a fresh live quota failure is
     reflected in provider-status (`mission_ready` no longer reads true off a stale earlier pass).
   - **MCP schema-call robustness** — the exact beta.4 field-name mistakes (`key`→`block_name`,
     `block_id`→`block_name`, `source_block`/`source_port`/`sink_block`/`sink_port`→ `*_block_name`/
     `*_port_name`) are canonicalized before dispatch, and a genuinely-invalid schema call returns a
     structured correction **without consuming a scarce gnuradio_mcp action**.
   - **Fallback budget protection** — a gnuradio_mcp flowgraph fallback that cannot build+validate+
     execute with the actions remaining is blocked early with a clear reason instead of spending the
     remainder on doomed partial work; retry re-probes coding-agent readiness so a locally-repaired
     sandbox is re-detected.
4. **Aithernet-managed CatGPT Gateway (`aithernet catgpt`).** The CatGPT coordinator path is now
   *client-usable without cloning/debugging the public gateway repo*. A small management wrapper
   (`aithernet catgpt setup|start|status|logs|stop`) renders an **Aithernet-authored** compose +
   env, binds the OpenAI-compatible API to **127.0.0.1 by default**, stores the local bearer token
   via managed secrets (`CATGPT_GATEWAY_API_KEY`, referenced by name), and points the coordinator
   provider at the local gateway. It runs upstream **CatGPT-Gateway (MIT)** as a *pinned local
   component* — a wrapper/sidecar, not a fork; no browser session/cookies/web credentials are ever
   stored in Aithernet, and **you sign into ChatGPT/Claude yourself via noVNC**. `agents connect
   coordinator` now offers *Aithernet-managed CatGPT Gateway* vs *External CatGPT Gateway endpoint*,
   and `provider-status coordinator` distinguishes `managed_gateway` + `gateway_status`
   (`stopped`/`starting`/`ready`/`login_required`/`error`).
5. **Mission routing/HTTP safety (release blocker fixed).** When the Aithernet node is down and a
   *foreign* web server (a reverse proxy, static site, or the CatGPT gateway) is bound to the node's
   port, mission commands used to crash — `mission list` with a raw `JSONDecodeError`, `run` by
   dumping a raw nginx `405` HTML page. Mission HTTP now parses **only** JSON bodies and, on a
   non-JSON body, emits a **structured, secret-free** error (method/scheme/host/port/path, status,
   content-type, server banner, a tag-stripped body preview, subsystem, and a foreign-endpoint
   hint) — never Authorization/bearer/cookies/full-HTML. A guardrail also **blocks** using the
   coordinator provider endpoint as the mission API ("Coordinator provider endpoint cannot be used
   as the Aithernet mission API."): the coordinator gateway only provides model inference.

## beta.4 compatibility

- Additive only: `catgpt_gateway` is a new selectable provider; no existing provider id, config
  field, CLI flag, or default changed. A node that never selects it behaves exactly as beta.4.
- `aithernet doctor` gains one new check (`coding_sandbox`); existing checks/among/JSON keys are
  unchanged. The new check reports READY on a host where bubblewrap works (as beta.4 hosts do).
- No hosted control-plane, portal, public-site, admin (`admin.aithernet.online`), unified-www
  origin, `/app/assets` MIME alias, or `app.aithernet.online` compatibility-route change.
- The version string is the single-source-of-truth `pyproject.toml` (`1.0.0-beta.5`, PEP 440
  `1.0.0b5`, Debian `1.0.0~beta.5`).

---

## Command reference

### Aithernet-managed CatGPT Gateway (recommended — no repo cloning)

```
# 1) One-time setup — writes the Aithernet-managed compose/env, generates + stores a local bearer
#    token via managed secrets, and points the coordinator provider at the local gateway.
aithernet catgpt setup                 # --provider chatgpt|claude, --api-port, --novnc-port, …
#   → API bound to 127.0.0.1:8000 by default; noVNC at 127.0.0.1:6080; token never printed.
#   → to BUILD the pinned upstream component from a checkout: --source-dir /path/to/CatGPT-Gateway

# 2) Start it, then sign in YOURSELF in the local browser (Aithernet never sees your session):
aithernet catgpt start
#   → open the noVNC URL and log into ChatGPT/Claude. (Google sign-in can be unreliable inside an
#     automated browser; prefer email/password if it misbehaves.)

# 3) Watch it come up (waits for login → model discovery):
aithernet catgpt status                # gateway_status: stopped|starting|login_required|ready|error
aithernet catgpt logs --tail 100       # bounded; the bearer token is never logged
aithernet catgpt stop                  # keeps your browser login for next time

# 4) Verify the coordinator end-to-end:
aithernet agents test coordinator --live
```

`agents connect coordinator` offers this managed path interactively (choice **1. Aithernet-managed
CatGPT Gateway**) or an external endpoint (**2. External CatGPT Gateway endpoint**). The managed
gateway is a **wrapper/sidecar** around upstream CatGPT-Gateway (MIT, pinned) — see
`notices/catgpt-gateway/` in the release bundle.

### Restarting the node so it loads the managed secret

`aithernet catgpt setup` stores the local bearer token in the **0600 managed secret store**
(`~/.config/aithernet/aithernet.env`) and points the coordinator at the gateway. The running node
must be (re)started so it loads that secret — **both** modes load it cleanly:

- **systemd-user / service mode (recommended):** `aithernet service restart`. The unit references
  the store via `EnvironmentFile=-%h/.config/aithernet/aithernet.env`, so the secret is present in
  the service environment.
- **manual / foreground mode:** restart `aithernet start`. beta.5 makes `aithernet start`
  **auto-load** the same 0600 store into its process environment (like the service's
  `EnvironmentFile`), with `override=False` so an explicitly exported shell value still wins. Values
  are never printed. This closes the earlier gap where a hand-started foreground node would 401
  mid-mission because the coordinator's `env:CATGPT_GATEWAY_API_KEY` resolved to nothing.

The bounded CLI probe (`aithernet agents test coordinator --live`) already hydrates the key from the
managed store independently, so it succeeds even before the node is restarted.

### Configure an external CatGPT Gateway coordinator (advanced)

```
# 1) Start CatGPT-Gateway yourself first (separate process on this workstation). Sign into
#    ChatGPT/Claude in its own browser/session flow if prompted. It exposes an OpenAI-compatible
#    API, by default at http://127.0.0.1:8000/v1 . Aithernet never needs its browser cookies.

# 2) Connect it as the COORDINATOR (guided):
aithernet agents connect coordinator
#   → choose "CatGPT Gateway — local OpenAI-compatible browser gateway"
#   → accept or enter the endpoint base URL (default http://127.0.0.1:8000/v1)
#   → choose a model — REQUIRED and never fabricated: picked from `GET /models` when the gateway
#     is up, or set explicitly (--model / AITHERNET_CATGPT_GATEWAY_MODEL). If the gateway isn't
#     running and no model is set, setup and the live test fail clearly (no placeholder default)
#   → if the gateway needs a LOCAL bearer token (its README often uses a placeholder such as
#     'dummy123'), connect PROMPTS for it (hidden), stores it via managed secrets as
#     CATGPT_GATEWAY_API_KEY, and saves the reference — no `set-secret` / env-export dance needed

# Non-interactive equivalent (token stored separately):
aithernet agents set-secret CATGPT_GATEWAY_API_KEY        # reads the token without echo
aithernet agents connect coordinator --provider catgpt_gateway \
    --base-url http://127.0.0.1:8000/v1 --model <model-id> --api-key-ref CATGPT_GATEWAY_API_KEY
```

Environment overrides (per-provider; no credential values in config):
`AITHERNET_CATGPT_GATEWAY_BASE_URL`, `AITHERNET_CATGPT_GATEWAY_MODEL`,
`AITHERNET_CATGPT_GATEWAY_API_KEY_REF` (the *name* of an env var holding the key). The live test /
mission calls resolve the managed secret from the 0600 store, so a CLI probe sends the same
`Authorization: Bearer <token>` the running service would.

### Test the coordinator (bounded, live)

CatGPT-Gateway uses the **simplest OpenAI-compatible path** — plain `POST /chat/completions`
(model + messages, **no** `response_format`/`tools`/`json_schema`) — and parses JSON from the
assistant text. Errors are classified **honestly** (never "unreachable" for a reachable endpoint):
`catgpt_gateway_unreachable`, `credential_rejected`, `browser_session_not_ready`,
`unsupported_gateway_feature` / `openai_compatibility_error`, `model_unavailable`,
`quota_exhausted` / `rate_limited`, `structured_output_incompatible`, `gateway_http_error`.

```
aithernet agents test coordinator --live
#   With CatGPT-Gateway STOPPED  → catgpt_gateway_unreachable ("is the local gateway running?").
#   Wrong/missing token          → credential_rejected (endpoint_reachable stays true).
#   With CatGPT-Gateway RUNNING  → ready; a bounded structured completion parses and is usable by
#                                  the mission coordinator.
```

### Provider status (coordinator vs coding stay separate)

```
aithernet agents provider-status coordinator
#   → provider: catgpt_gateway, endpoint, model, auth_readiness: local-gateway-configured
#     (or live-verified after --live), readiness.endpoint_reachable / mission_ready.

aithernet agents provider-status coding
#   → your coding agent (e.g. codex_cli) — configured SEPARATELY and unaffected by the coordinator.
```

Keep the coding agent separate — CatGPT Gateway is coordinator-only:

```
aithernet agents connect coding --provider codex_cli    # (or claude_code)
```

### Verify coding-agent sandbox readiness

```
aithernet doctor
#   Look for the `coding_sandbox` check:
#     [ready]     bubblewrap sandbox works (uid-map + loopback probe passed)
#     [degraded]  bubblewrap uid-map/loopback probe failed … — missions requiring the coding agent
#                 may fail until this is resolved.  (see remediation below)
```

### Example mission

```
aithernet run "Create, validate, execute, and summarize a bounded software-only GNU Radio signal \
generator. Do not use physical RF."
```

---

## Sandbox remediation behaviour (Ubuntu AppArmor userns)

`doctor` / `setup` **detect and explain** only — Aithernet never changes a sysctl and never needs
sudo. When the bubblewrap uid-map/loopback probe fails on Ubuntu with
`kernel.apparmor_restrict_unprivileged_userns=1` (the beta.4 `bwrap: loopback: Failed RTM_NEWADDR:
Operation not permitted` / `bwrap: setting up uid map: Permission denied`), the check is reported
**not READY** with:

```
Temporary test workaround:
    sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
Restore after testing:
    sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=1
```

> Security warning: this relaxes a host hardening control. Use only if acceptable on this
> workstation.

A mission that hits this at runtime blocks early with the same remediation and points at
`aithernet doctor`; `aithernet mission retry <id>` re-probes readiness so a locally-fixed host is
re-detected on the next run.

---

## Artifacts

Built by `bash scripts/build_release.sh` (auto-derives VERSION from `pyproject.toml`) into
`~/.local/state/aithernet/lan-release-1.0.0-beta.5/archive/`; LAN signing key id
`aithernet-lan-betaqual-1.0.0-beta.5-20260620`. `aithernet release verify <archive>` →
**RELEASE VERIFIED** (Ed25519 manifest signature, 15 files matched SHA256SUMS, all manifest digests).

- `aithernet_1.0.0~beta.5_amd64.deb` — 16,141,666 B — `sha256:d5f456f118b4e20867c4b5e5bbe8597dac3f8adaf8e6c7c15740c2c3cb0d15ce`
- `aithernet-1.0.0b5-py3-none-any.whl` — 918,525 B — `sha256:8738de843bd2f1a8f3ea5f0c0eefb4b77ac7a0511a86257c81b8c8dd25ed1b6c`
- `aithernet-1.0.0b5.tar.gz` — 1,468,788 B — `sha256:dcd797573126d96e8461e8517a16a0859b6a6f344529c85764ac3a4f08939f21`
- `rf-mcp-0.1.0+aithernet.2-src.tar.gz` — 91,339 B — `sha256:200b7671da9cfd694ae43c9a2d0d1a569eaa7808d1cfdccec74ff592ffa5a73c` (+ detached `.sig` + pinned `aithernet-component.pub`)
- `notices/catgpt-gateway/{NOTICE.md, LICENSE}` — upstream CatGPT-Gateway MIT license + pinned-source
  notice (the managed gateway is a wrapper/sidecar; upstream source is not redistributed here).
- `manifest.json` / `manifest.sig` / `SHA256SUMS` / `aithernet-release.pub` / `THIRD-PARTY-NOTICES.md`,
  plus installer + notices + `sbom.json` + `hardware-manifest.json`.

The `.deb` ships `/usr/bin/aithernet`, the beta.5 source under `/opt/aithernet/lib` (including
`coordinator/providers/catgpt_gateway.py`, `coding_agent/sandbox_preflight.py`, and the new
`catgpt/` management package), and `/opt/aithernet/VERSION` = `1.0.0-beta.5`.

## Production release record (PUBLISHED)

- Merged to `main` (merge commit `52c66fbcb58db7ce203a58446359af1fd1547411`); annotated tag
  `v1.0.0-beta.5` → that commit; GitHub Release `v1.0.0-beta.5` (prerelease) attached.
- Hosted control-plane release: id `4696f988-1976-4f29-bb54-d2ec9cc350ac` · status **published** ·
  channel `early-access` (now the current/latest early-access release; beta.4 remains available).
- Signing key id: `aithernet-prod-betaqual-20260620` · release manifest digest (prod-signed):
  `sha256:83ca043c22609235d120803ddbc5c1592afa42559b93d722b2b17d0a27db6091` · 19 artifacts attached.
- Published via the standard hosted mechanism (`python -m services.control_plane.cli release
  create → sign → publish`, run in the production control-plane); no image rebuild/deploy required
  (the catalog is data served by the running control-plane). Public site (`www.aithernet.online`)
  updated to beta.5 via the GitHub Pages flow; the authenticated portal (`app.aithernet.online`)
  serves the release. Verified: the served `.deb` bytes match
  `sha256:d5f456f118b4e20867c4b5e5bbe8597dac3f8adaf8e6c7c15740c2c3cb0d15ce`.

## Install (beta.5 `.deb`)

```
sudo apt install ./aithernet_1.0.0~beta.5_amd64.deb
hash -r                    # clear the shell's cached path to the OLD aithernet after an upgrade
which aithernet            # → /usr/bin/aithernet
aithernet --version        # → aithernet 1.0.0b5
```

> After a local `.deb` upgrade the shell may keep a cached path to a previously-installed
> `aithernet` (e.g. a `~/.local` or venv copy), so `aithernet --version` can appear stale until you
> run `hash -r` (or open a new shell). `aithernet doctor` also flags this: when the deb is installed
> but `aithernet` on PATH is not `/usr/bin/aithernet`, the `cli_path` check notes it (non-blocking)
> with the `hash -r` remediation.

## Qualification

Deterministic (no live external billing). New beta.5 tests: `test_catgpt_gateway_provider.py` (32),
`test_catgpt_managed_gateway_beta5.py` (18, managed gateway + contract + login-not-ready),
`test_cli_mission_routing_beta5.py` (12, non-JSON safety + coordinator/mission endpoint isolation),
`test_sandbox_preflight.py`, `test_mcp_schema_aliases_beta5.py`,
`test_mission_budget_and_sandbox_beta5.py`, plus the affected provider / mission / doctor / mcp /
`test_cli_node_api_json.py` regression suites. The managed-gateway docker layer is exercised through
a mocked runner; the contract probe was additionally verified **READY against the real running
gateway** (model `catgpt-browser`). Two pre-existing environmental failures in
`test_agent_providers.py` (catalogue `preferred_beta` drift; `auth_readiness` vocabulary drift) and
one env-dependent `~/.local` console-script test are unchanged by this release.
