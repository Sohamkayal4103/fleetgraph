# Getting Started with Aithernet (post-login customer journey)

This is the canonical, end-to-end customer journey for a beta.10 software-only node on
**Ubuntu 24.04 LTS amd64**. Every `aithernet …` command below is a real packaged command (verified
by `tests/test_customer_docs_commands.py`). Version-specific values shown as `<…>` come from your
authorized release download (the authenticated portal fills them in from release metadata). No
developer paths, no secrets.

> The normal experience is just two commands: `aithernet setup`, then
> `aithernet run "<what you want done>"`. The sections below cover the optional provider, peer,
> and owner-research workflows. You never edit YAML, systemd units, PATH, environment files, OAuth
> files, model-adapter identifiers, mission queues, or upload scripts.

Conventions: `sudo` is used **only** for APT/system package changes. Personal-workstation setup and
all `aithernet` commands run as your **normal user** (never root). Do **not** use `pip` for GNU
Radio, SoapySDR, UHD, libiio or `python3-venv` — they are system packages.

## A. Download and verify the release

```bash
sha256sum <release.zip>
unzip <release.zip> -d <release-dir>
cd <release-dir>
sha256sum -c SHA256SUMS
openssl pkeyutl -verify -pubin -inkey aithernet-release.pub \
  -rawin -in manifest.json -sigfile manifest.sig
```

Expected: the ZIP SHA-256 matches the portal value; `sha256sum -c SHA256SUMS` prints `OK` for every
file; the signature prints `Signature Verified Successfully`. The portal shows: release version,
channel, signing key id, ZIP SHA-256, package architecture (amd64), supported Ubuntu (24.04) and
revocation status.

## B. Install the native package

Manual:

```bash
sudo apt update
sudo apt install ./<aithernet-package>.deb
```

Recommended installer (validates Ubuntu 24.04 amd64, refreshes stale indexes, retries, never uses
pip):

```bash
chmod +x aithernet-install.sh
./aithernet-install.sh --dry-run --deb "$PWD/<aithernet-package>.deb"
./aithernet-install.sh --deb "$PWD/<aithernet-package>.deb"
```

Then verify:

```bash
which aithernet
aithernet --version
aithernet -V
aithernet release verify "$PWD"
```

`apt` resolves `python3-venv` and the recommended SDR packages from Ubuntu main/universe. If a
package is "not installable" on a fresh machine, run `sudo apt update` and retry. A genuine
repository/network failure produces an actionable error.

## C. Standalone software-only setup (normal user)

```bash
aithernet setup \
  --profile software-only \
  --deployment-mode standalone \
  --service-mode systemd-user \
  --sdr-strategy recommended \
  --components-bundle-dir "$PWD" \
  --coordinator disabled \
  --coding disabled \
  --dry-run
```

Re-run without `--dry-run` to apply. Choose a unique, non-sensitive node name and the
`workstation` identity for a personal workstation. **Standalone makes zero hosted-enrollment
requests**; hosted mode is separate and needs an enrollment code. Software-only does **not** mean a
physical SDR is attached; WSL audio endpoints are not SDRs.

## D. Complete SDR dependencies

```bash
aithernet sdr install --mode recommended --profile software-only
aithernet sdr install --mode recommended --profile software-only --yes
```

`recommended` uses validated APT repositories; `offline` installs only pre-staged OS packages and
stops clearly when the release lacks them; `none` skips SDR dependency installation.

## E. Enable the workstation service

```bash
systemctl --user enable --now aithernet-node.service
systemctl --user is-enabled aithernet-node.service
systemctl --user is-active aithernet-node.service
```

Optional `loginctl enable-linger $USER` keeps it running while logged out — not required for an
ordinary logged-in workstation.

## F. Configure an API coordinator (Gemini API)

Store the key securely (no echo; written only to the 0600 service env file), then record the
reference and select the provider:

```bash
aithernet agents models gemini_api            # list supported models + the default
aithernet agents set-secret GEMINI_API_KEY
aithernet agents configure coordinator \
  --provider gemini_api \
  --model gemini-2.0-flash \
  --api-key-ref GEMINI_API_KEY \
  --identity-model workstation
systemctl --user restart aithernet-node.service
aithernet agents provider-status coordinator
aithernet agents test coordinator --live
```

The key is never printed or written to `node.yaml` (only `env:GEMINI_API_KEY` is recorded). The
live test must succeed under the service execution identity. Anthropic API and OpenAI-compatible
(including a local endpoint) are configured the same way with their own provider name and key
reference.

## G. Configure the coding provider (Codex CLI)

```bash
aithernet agents configure coding \
  --provider codex_cli \
  --executable "$(command -v codex)" \
  --identity-model workstation
aithernet agents provider-status coding
aithernet agents test coding --live
```

Authenticate the Codex CLI as your runtime Linux user; the systemd user service must see the
executable. Aithernet uploads no credential files; coding runs in a bounded per-task workspace.

## H. Optional: Gemini CLI coordinator (separate from Gemini API)

Install/log in to the Gemini CLI, then:

```bash
aithernet agents configure coordinator --provider gemini_cli --executable "$(command -v gemini)"
aithernet agents test coordinator --live
```

Gemini API customers do **not** need the Gemini CLI. A Gemini CLI workspace-trust failure is
reported as "configured, executable present, live test failed: workspace trust required" — not as a
Gemini API failure.

## I. Verify the complete node

```bash
aithernet setup --status
aithernet doctor
aithernet components verify rf-mcp
aithernet mcp diagnostics
aithernet mcp tools
systemctl --user status aithernet-node.service --no-pager
```

Expect: a real node identity (no placeholder id) and absolute state/database paths; the service
active; provider live-readiness as tested; GNU Radio + MCP available; RF-MCP provenance verified;
and **zero physical RF devices** when no SDR is connected.

## J. Run a software-only mission (one command)

```bash
aithernet run "Create and test a software-only GNU Radio signal generator."
```

`aithernet run` creates the mission, starts autonomous execution through the canonical
MissionEngine, streams progress, prints the final result, and shows the mission / run / artifact
identifiers. It returns exit code 0 on success, 1 if the mission is blocked/failed, and 2 if the
node isn't set up or running (it then points you back to `aithernet setup`). Add `--detach` to run
it in the background.

If a mission was blocked by a provider problem, fix it (e.g. `aithernet doctor --repair` or
`aithernet agents connect coordinator`) and retry the SAME mission — no need to recreate it:

```bash
aithernet mission retry <mission-id>
```

### Advanced: lower-level mission lifecycle

For operators and debugging only — the normal workflow is `aithernet run`. `mission submit` only
CREATES a mission (the prompt is a positional argument, not a flag); `mission watch` is read-only.

```bash
aithernet mission submit "<your operator prompt>"   # create only (does not run)
aithernet mission start <mission-id>                # start autonomous execution
aithernet mission watch <mission-id>                # read-only live status
aithernet mission show <mission-id>
aithernet mission steps <mission-id>
aithernet mission events <mission-id>
aithernet mission artifacts <mission-id>
aithernet mission cancel <mission-id>
```

You see the structured coordinator plan + DecisionRecord (steps), the coding route, events,
registered artifacts, and the final response — all through the canonical MissionEngine.

## K. WSL limitations

WSL is supported only for software-only and agent qualification. WSL audio endpoints are not RF
SDRs. Physical/USB SDR qualification should be done on supported physical Ubuntu hardware; WSL
software testing is not physical-hardware qualification.

## L. Troubleshooting

- **Stale APT indexes / missing repositories**: `sudo apt update`; ensure `universe` is enabled.
- **Ran setup as root**: re-run as your normal user; state belongs under your home.
- **Key in shell but not in service**: use `aithernet agents set-secret` (writes the 0600 service
  env file) — a key exported only in your shell is not visible to the systemd user service.
- **Gemini CLI workspace trust**: complete the scoped trust step; it is separate from Gemini API.
- **Codex executable not visible to systemd**: configure `--executable "$(command -v codex)"`.
- **MCP certificate-bundle error**: fixed in beta.8 (system CA store); re-run `aithernet mcp
  diagnostics`.
- **MCP restart cap reached**: the session settles into a stable failed state with one summary
  event; fix the cause and `aithernet mcp session start`.
- **Port 8080 in use**: `aithernet status`, or `aithernet service status` / `service restart`.
- **Setup and runtime show different identities**: run `aithernet setup --migrate`.
- **CLI provider status differs from runtime**: restart the service, then `aithernet agents
  provider-status`.
- **No physical SDR connected**: expected for software-only; physical RF readiness is `unavailable`
  while the node is still core-ready.
- **Setup status remaining deferred**: `aithernet setup --status` reconciles against reality.

## M. Managed secrets (any provider)

Store an API key for ANY provider under any standard environment-variable NAME; the value is read
without echo, written to a 0600 credential file, pushed to the running service automatically (no
manual `systemctl --user import-environment`), and verified:

```bash
aithernet agents set-secret ZAI_API_KEY      # prompts without echo; restarts + verifies the service
aithernet agents secrets list                # names + availability only — never values
aithernet agents secrets check ZAI_API_KEY
aithernet agents secrets remove ZAI_API_KEY
```

Secret values never appear in `node.yaml`, logs, events, diagnostics, process arguments, datasets,
or Drive uploads.

## N. Providers: OpenAI-compatible / Z.AI, fallback, and quota recovery

Connect a coordinator in plain language; for an OpenAI-compatible endpoint you are guided through
the base URL, model and secret (the Z.AI GLM profile is offered with its general and coding-plan
base URLs). The endpoint and model are validated before saving:

```bash
aithernet agents connect coordinator
aithernet agents connect coordinator --provider openai_compatible \
  --base-url https://api.z.ai/api/paas/v4/ --model <model-id> --api-key-ref ZAI_API_KEY
aithernet agents connect coding
```

If a coordinator provider's **quota** is exhausted, the mission parks in a recoverable
`blocked_provider_quota` state (it is **not** mislabeled `resource_budget_exhausted` and does not
burn the failure budget). Connect a different coordinator or wait for the quota to reset, then retry
the same mission:

```bash
aithernet mission retry <mission-id>
```

A coordinator fallback chain (`fallback_providers`) records every explicit provider switch in
mission provenance — switches are never silent. Provider failures are reported as normalized,
redacted, actionable errors (missing/invalid key, payment required, model unavailable, TLS, timeout,
malformed response, …) with a concise action — never with a raw key.

## O. Coding agent + managed RF-MCP

The coordinator decides per mission whether to answer directly, use the managed GNU Radio MCP, or
delegate to the coding agent (create/modify files, write or repair code, generate tests, build
reusable GNU Radio Python logic). The coding agent is not invoked for every mission. The RF backend
is the **managed, signed RF-MCP component** installed and verified by `aithernet setup` — never a
developer checkout or a manual source clone:

```bash
aithernet components verify rf-mcp
aithernet mcp diagnostics
aithernet mcp tools
```

Proposed MCP tool calls are validated against each tool's authoritative schema before execution
(unknown/missing fields are rejected and common aliases canonicalized), so missions don't waste
iterations learning a schema from failures.

## P. Peer nodes and remote missions

Pair a trusted peer in one step (its key is pinned), then submit work to it. The receiving node owns
mission creation, provider/hardware/RF policy, workspaces and artifacts — a remote request is never
remote shell:

```bash
aithernet peers pair --name nodeB --public-key <peer-ed25519-key> --endpoint-url <url> \
  --expected-node-id <node-id>
aithernet peers list
aithernet peers test <peer-id>
aithernet run --on nodeB "Create and test a software-only GNU Radio signal generator."
aithernet remote-mission list
```

For a server-side agent, mint a scoped gateway credential (it cannot bypass node policy):

```bash
aithernet agents gateway create edge-bot
aithernet agents gateway revoke edge-bot
```

## Q. Owner research recording + verified Google Drive sync (owner node)

This is an **owner-operated** option, not a default for external customers. Enable it during setup
(`aithernet setup` asks one plain-language question) or any time:

```bash
aithernet data setup            # enable owner_full, init the spool, verify Drive, test upload
aithernet data status           # concise dashboard (no secrets)
aithernet data verify           # verify spool integrity + Drive reachability
```

`data setup` enables `owner_full` collection, initializes the local append-only research spool,
verifies/refreshes the existing Google Drive connection, reuses the dedicated **Aithernet Research
Data** Drive hierarchy, and runs a bounded test upload whose remote size and digest are verified.
You never edit OAuth files, folder IDs, YAML, environment variables, or upload scripts.

Collected research data covers mission/runtime records, provider/model/token/latency/cost,
provider-exposed reasoning fields and structured rationale summaries, coordinator decisions and
routes, MCP tool schemas and corrected tool calls, coding tasks/patches/tests, peer events,
artifacts, outcome/quality labels, and (synthetic) RF SigMF data with derived features — plus
manifests, provenance, schemas, statistics and checksums.

**Secret exclusion:** every record is secret-scanned before it lands locally and again before
upload; a record that still carries a credential is diverted to quarantine and is never normalized
or uploaded (only quarantine *metadata* is archived). API keys, OAuth tokens, cookies, SSH/signing
private keys, and bearer headers are never collected or uploaded.

**No hidden chain-of-thought:** Aithernet does **not** capture any provider's hidden private
reasoning. It records only legitimately observable reasoning — provider-exposed reasoning fields and
the structured decision explanations providers return as normal output. It never fabricates or
infers hidden reasoning.

## R. Training-ready dataset builders

Build versioned, training-ready datasets from the research spool, one class at a time, with
leakage-free **mission-grouped** train/validation/evaluation splits and a held-out evaluation slice:

```bash
aithernet data build coordinator
aithernet data build routing
aithernet data build tool-calling
aithernet data build recovery
aithernet data build coding
aithernet data build rf-analysis
```

Each build emits train/validation/evaluation JSONL plus schema, dataset card, provenance,
statistics and `SHA256SUMS`. beta.10 builds the training-ready infrastructure; it does not fine-tune
or publish a model.

## S. Limitations

- Invitation-only early access; receive-only — **no RF transmission**.
- Physical Pluto/USB-SDR qualification is not part of this software-only journey.
- Owner research recording is opt-in and owner-operated; it is not enabled for external customers by
  default.
- No claim of access to any model's hidden chain-of-thought.
- No secret/credential is ever collected or uploaded.
