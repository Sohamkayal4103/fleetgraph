# Phase 4 — Clean-Client Install Runbook (operator-driven)

**Goal:** prove the complete first-customer software-only journey on a **separate, clean
Ubuntu 24.04 amd64 physical machine** — from opening the invitation through running a mission and
surviving a restart — using only the published beta.4 release, with **no developer checkout**.

**Do NOT do physical Pluto / RF work yet.** Physical qualification (Phase 6) starts only after this
software-only journey passes. No transmission at any point.

This runbook is written from the **customer's perspective**. Every step has an expected result.
**Any step that needs an undocumented manual action is a product defect** — record it in §20.
Known defects already identified are flagged inline as `⚠️ DEFECT (beta.5)`.

---

## Reference values (this qualification)

| Item | Value |
|---|---|
| Portal / API | `https://app.aithernet.online` |
| Admin portal | `https://admin.aithernet.online` (Cloudflare Access-protected) |
| Customer email | `mspawar@ucsc.edu` |
| Invitation | id `8a7dc453-fff0-4c05-a2af-9be83abe9359`, expires **2026-06-28** — link delivered by email |
| Release | `0.8.0-beta.4`, channel `early-access`, id `2c3deea6-0d87-4142-afe1-f421fc996e50` |
| Signing key id | `aithernet-prod-betaqual-20260620` |
| Verification public key (base64 Ed25519) | `v+qYpcHUDWYW1Hch2EXKXhVdVUeuQrlPD0B85Uz344A=` |
| Manifest digest | `sha256:fc120d37d906df19f2cf746c3180a0f2f9ac17c0e0c23c77acd9109a65239b71` |

**Published artifact SHA-256 (authoritative — also shown at `…/v1/public/releases`):**
```
e05b9193b98861c3ec171c476f7898dac6f7486d6f413779fc33f22d26dc082e  aithernet_0.8.0~beta.4_amd64.deb
34da2835b6ee5c5065f5a41ff968aed597bdab6e98774abc953c77ab46b71591  aithernet-0.8.0b4-py3-none-any.whl
604ecc24cbb417155bbe1d1f32184b80ad5cf0f79c048b2e3d548cf66dc87a39  aithernet-0.8.0b4.tar.gz
200b7671da9cfd694ae43c9a2d0d1a569eaa7808d1cfdccec74ff592ffa5a73c  rf-mcp-0.1.0+aithernet.2-src.tar.gz
6231349297491a3b538e873454aba7fa25c81243268d92bb5ede1bd4cf9dafff  lock.json
f1157f1395a55bbf986930780edb0db95cfa9e3c3373cc012de6aa8745d7afbc  aithernet-install.sh
83965214a92b9088…                                                  sbom.json
f6625cc6a16e7048…                                                  hardware-manifest.json
9c9730630bd6b057…                                                  NOTICE.md
7f75bb67a2f4b412…                                                  THIRD-PARTY-NOTICES.md
```

---

## 0. Clean-machine prerequisites

On the target machine confirm NONE of these exist (this is what makes it a real customer test):
```bash
ls /home/*/gnu/aithernet 2>/dev/null            # no Aithernet source checkout
ls /home/*/gnu/aithernet/gr-mcp 2>/dev/null     # no gr-mcp checkout
ls /home/*/gnu/marconi 2>/dev/null              # no Marconi checkout
which aithernet 2>/dev/null                      # not already installed
pip show aithernet 2>/dev/null                   # no editable dev install
lsb_release -d                                   # expect: Ubuntu 24.04 ... ; uname -m -> x86_64
```
Expected: the first four print nothing; OS is Ubuntu 24.04 amd64. If a developer checkout exists,
hide/rename it for the duration of acceptance.

---

## 1. Open the invitation
- In the inbox of **mspawar@ucsc.edu**, open the Aithernet invitation email (from
  `adityapawar@aithernet.online`).  *(An SMTP test + the invitation send were both accepted by the
  server during setup; if the email is missing, check spam, then re-issue from the admin side.)*
- The link has the canonical form `https://app.aithernet.online/#/accept-invite?token=<ONE-TIME-TOKEN>`.
- Open it in a **fresh private browser window**.

**Expected:** the portal loads the **Accept invitation** page (not "Sign in required"). The token is
read automatically and **scrubbed from the address bar** — you should NOT have to paste a token. A
manual paste, or a fall-through to `#/portal`, is a `⚠️ DEFECT`.

## 2. Create the account
- The page shows your email (`mspawar@ucsc.edu`) and proposed tenant **"UCSC Early Access"** (role
  `tenant_admin`). Set a password and submit.

**Expected:** the account + tenant + membership are created atomically and you are signed in. (If you
already had an account, it is reused and your password is NOT reset.)

## 3. Accept the required policy
- You are presented with **"Aithernet Early-Access Evaluation Terms" (version `ea-2026-06-20`)**.
  Read it (note: receive-only, no transmission authorized) and **Accept**.

**Expected:** acceptance is recorded; you land in the customer portal (`#/portal`). No policy
presented = a `⚠️ DEFECT`.

## 4. Confirm beta.4 is visible
- Portal → **Releases**.

**Expected:** one published release **`0.8.0-beta.4`**, channel `early-access`, with its artifacts and
SHA-256 values, and a "Verify your download" panel. Empty/placeholder text here = a `⚠️ DEFECT`.

## 5. Download the artifacts
From the Releases page, download (authenticated):
`aithernet_0.8.0~beta.4_amd64.deb`, `aithernet-0.8.0b4-py3-none-any.whl`, `aithernet-0.8.0b4.tar.gz`,
`rf-mcp-0.1.0+aithernet.2-src.tar.gz`, `lock.json`, `aithernet-install.sh`, `sbom.json`,
`hardware-manifest.json`, `NOTICE.md`, `THIRD-PARTY-NOTICES.md` into `~/Downloads/aithernet-beta4/`.

**Anonymous-access check (security):** in a logged-out tab, requesting a package directly must be
**rejected** (HTTP 403). Only an authenticated, policy-accepted member may download.

## 6. Verify SHA-256 and the signature
```bash
cd ~/Downloads/aithernet-beta4
# SHA-256 — authoritative integrity check against the HTTPS-published digests:
sha256sum aithernet_0.8.0~beta.4_amd64.deb       # must equal e05b9193…dc082e
sha256sum aithernet-0.8.0b4-py3-none-any.whl     # must equal 34da2835…b71591
sha256sum rf-mcp-0.1.0+aithernet.2-src.tar.gz    # must equal 200b7671…a5a73c
# Cross-check the published digests over TLS:
curl -s https://app.aithernet.online/v1/public/releases | python3 -m json.tool | grep -A1 sha256
```
**Expected:** every local digest equals the published digest. The release manifest is Ed25519-signed
with key id `aithernet-prod-betaqual-20260620`; the **node verifies that signature automatically** at
the authenticated download/update layer (§ update phase).

> ⚠️ DEFECT (beta.5) — **manual Ed25519 pre-install verification is not possible with beta.4 as
> published.** The hosted release does not serve a downloadable detached `manifest.sig` + PEM public
> key, `/v1/releases/{id}/manifest` is not publicly routed (falls through to the SPA), and
> `/v1/public/releases` exposes digests + the base64 public key but not the signature. The public
> website's `openssl pkeyutl -verify …` instructions therefore do not match the hosted release.
> **Fix for beta.5:** serve `manifest.json` + detached `manifest.sig` + a PEM `aithernet-release.pub`
> via public download routes (or ship `aithernet release verify`) and align the website. For beta.4,
> SHA-256-over-TLS + automatic node verification is the integrity path.

## 7. Install the `.deb`
```bash
sudo apt install ./aithernet_0.8.0~beta.4_amd64.deb
```
**Expected:** installs cleanly. Depends `python3 (>=3.11), python3-venv` are satisfied. The package
installs `/usr/bin/aithernet`, `/opt/aithernet/…`, `/etc/aithernet/…`, a `var/lib/aithernet` state
root, and a **system** unit `aithernet-node.service` (User `aithernet`). It does **not** put
`aithernet-hosted` on PATH.

## 8. Prove the CLI works with no source checkout
```bash
cd /          # anywhere outside any source tree
which aithernet                 # -> /usr/bin/aithernet
aithernet --help                # full command list
aithernet doctor | head         # runs from the packaged runtime, no repo
```
**Expected:** all work from `/`. The component spec is bundled in the package
(`/opt/aithernet/lib/aithernet/_component_specs/rf-mcp/…`), so no checkout is needed.

## 9. Guided setup — software-only profile first
```bash
aithernet setup            # interactive; choose: profile = software-only (or simulation)
# non-interactive equivalent:
# aithernet setup --hardware-profile software-only --deployment-mode hosted --resume
```
Setup is a resumable 14-step wizard (identity, state/config dirs, service mode, hosted enrollment,
hardware profile, managed components, coordinator/coding providers, diagnostics). For now select the
**software-only** profile; do not select Pluto.

**Expected:** each step reports DONE or DEFERRED with the exact follow-up command. An ordinary user
must not have to hand-edit YAML. Any required manual YAML edit = a `⚠️ DEFECT`.

## 10. Install + verify the managed rf-mcp component
The component spec (component.toml + patches `0001`,`0002`) ships inside the `.deb`. Install it:

```bash
# Source mode (builds +aithernet.2 from the pinned upstream commit + the bundled patches,
# then creates the component's system-site-packages venv):
git clone https://github.com/yoelbassin/gr-mcp /tmp/gr-mcp-src       # ⚠️ DEFECT — see below
aithernet components install rf-mcp --upstream-git /tmp/gr-mcp-src
aithernet components verify rf-mcp        # expect ok:true, version 0.1.0+aithernet.2
```
**Expected:** `verify` → `ok:true`, `version_matches_spec:true`, `signature_valid:true`, and a built
`.venv` under the install prefix. `aithernet doctor` then shows `rf_mcp: READY` and
`mission_subsystem: READY`. The component **auto-wires** into the GNU Radio MCP launch — no
`AITHERNET_GNURADIO_MCP_COMMAND` or developer checkout is needed at runtime.

> ⚠️ DEFECT (beta.5) — **the released component cannot be installed with `--bundle` and requires a
> manual `git clone` of upstream.** The beta.4 release ships `rf-mcp-…-src.tar.gz` + `lock.json` but
> **not** the component's detached signature + public key, so `aithernet components install rf-mcp
> --bundle <downloaded-dir>` cannot verify; and source mode requires `--upstream-git <local clone>`
> (the manager does not auto-fetch the pinned upstream). **Fix for beta.5:** include the component
> `…-src.tar.gz.sig` + `aithernet-component.pub` in the release so `--bundle` verifies offline, or
> have the installer fetch the pinned upstream itself. (rf-mcp is **not** required for a software-only
> mission; if this step is deferred, the software-only journey below still completes.)

## 11. Configure + live-test the providers (Gemini, Codex)
Provider auth belongs to the **runtime Linux identity**. Decide your identity model first (§12).

**As the user who will run the node**, authenticate the CLIs and test:
```bash
# Coordinator — Gemini:
export GEMINI_API_KEY=...                  # required for non-interactive Gemini inference
aithernet agents test coordinator --live   # expect ready:true, executed_provider:gemini_cli
# Coding — Codex:
codex login                                 # if not already authenticated
aithernet agents test coding --live         # expect ready:true, executed_provider:codex_cli, nonce_validated:true
aithernet agents provider-status            # sanitised: selected==executed, identity, no secrets
```
**Expected:** each `--live` test makes ONE bounded real request and reports `ready:true` only on
success; failure reports closed (it never fakes readiness). Selected provider == executed provider.

> Notes from this session's qualification: **Codex** passed a live bounded test (nonce validated,
> isolated workspace cleaned). **Gemini** requires `GEMINI_API_KEY` for non-interactive inference —
> the system correctly reported NOT ready without it. If you prefer not to use Gemini, set the
> coordinator to `anthropic` / `openai_compatible` (API key) or `disabled` (deterministic missions).

## 12. Select a service mode appropriate for the Linux identity
Two valid models — pick one:

- **Workstation (desktop user owns provider auth):** run the node **as your desktop user** so it
  inherits your `~/.gemini` / `~/.codex` CLI auth. The packaged unit runs as the `aithernet` system
  user, which **cannot** see your desktop CLI auth, so either run a **user service** or start
  manually:
  ```bash
  aithernet start --config ~/.local/share/aithernet/config/node.yaml &     # foreground/user-owned
  # (a `systemctl --user` unit is the durable form)
  ```
  > ⚠️ DEFECT (beta.5) — the `.deb` ships only a **system** service (`User=aithernet`); there is no
  > `systemctl --user` unit for the workstation identity, so workstation users must start the node
  > manually or hand-write a user unit. **Fix for beta.5:** ship an optional `systemctl --user` unit
  > (or document API-key/appliance mode as the supported hosted-service path).

- **Headless / appliance (system service):** give the `aithernet` system user **API-key** provider
  auth via `/etc/aithernet/aithernet.env` (e.g. `GEMINI_API_KEY=…`, an OpenAI-compatible/Anthropic
  key, or `disabled`), then:
  ```bash
  sudo systemctl enable --now aithernet-node.service
  systemctl status aithernet-node.service
  ```
  Desktop-keychain CLI logins (interactive `gemini`/`codex login`) do **not** transfer to the
  `aithernet` user — use API keys for this mode.

For this software-only acceptance, **either** mode is fine; record which you used.

## 13. Enroll the node
Get an **enrollment code** from the portal (Customer portal → Nodes → create enrollment code), then:
```bash
aithernet enroll --base-url https://app.aithernet.online/api --code <ENROLLMENT_CODE>
aithernet hosted status        # expect: enrolled
```
**Expected:** the node presents its Ed25519 identity, proves possession, and persists hosted state.

## 14. Run doctor
```bash
aithernet doctor
```
**Expected (software-only):** core READY; node identity present; enrollment enrolled; service per §12;
`mcp_wiring` READY (managed component) or MISSING if §10 was deferred; providers per §11;
`mission_subsystem` READY (software-only prerequisites; no transmit). `doctor` does NOT call any
node-HTTP MCP probe, so it does not hit the JSON-decode issue noted below.

## 15. Confirm the node in the customer portal
- Portal → **Nodes**.

**Expected:** the node appears with its software version (`0.8.0-beta.4`), release channel, and a
recent signed heartbeat / bounded status. Run `aithernet hosted heartbeat` and refresh if needed.

## 16. Run one software-only mission (canonical MissionEngine)
```bash
aithernet mission submit --content "Software-only readiness check: summarize node status."
aithernet mission list                         # note the mission id
aithernet mission show <MISSION_ID>            # status + steps + events + DecisionRecord
```
**Expected:** the typed mission is created and processed by the **canonical MissionEngine** (atomic
steps + events + a DecisionRecord), returning a terminal status. With a working coordinator it
reasons; with `disabled` it runs the deterministic control path. This proves the node is API-driven,
not prompt-only. (It also exercises the same path an external app/agent would use via the node REST /
external-agent API.)

## 17. Restart and verify survival
```bash
# workstation: stop the foreground/user process and restart it;
# appliance:   sudo systemctl restart aithernet-node.service
aithernet identity show          # same node_id + fingerprint as before
aithernet hosted status          # still enrolled
aithernet agents provider-status # providers still configured
aithernet mission show <MISSION_ID>   # prior mission state intact
```
**Expected:** node identity, enrollment, provider configuration, and mission state all **survive the
restart** unchanged.

## 18. (Optional) MCP inspection — note on the JSON-decode issue
`aithernet mcp tools` / `mcp status` / `mcp diagnostics` talk to the **running node's HTTP API**. Run
them only **while the node service/process is up** (after §12). If run with no node server running,
they currently raise a `JSONDecodeError` (they receive the portal SPA HTML instead of JSON).

> ⚠️ DEFECT (beta.5, NON-BLOCKING) — `mcp status/tools/diagnostics` should fail gracefully with a
> "node server not running" message instead of a `JSONDecodeError` traceback. **This is OUTSIDE the
> required journey** — `setup`, `doctor`, `components verify`, enrollment, and missions do **not**
> invoke it, and when a customer does inspect MCP the service is running. Record for beta.5; do not
> delay Phase 4.

---

## 19. Acceptance record to capture (per the milestone)
Record for each: clean-machine identity (hostname, `lsb_release`, `uname -m`); install transcript;
`aithernet doctor` output; setup transcript; enrollment evidence (portal node id + heartbeat);
provider readiness JSON (no secrets); mission id(s) + final status; restart-survival evidence; and
**every manual step you had to perform that this runbook did not anticipate** (→ §20).

## 20. Known beta.4 defects (none block the software-only journey; fix in beta.5)
1. **Manual Ed25519 pre-install verification not possible** (no public detached sig/PEM key; website
   openssl instructions mismatched). Integrity via SHA-256-over-TLS + automatic node verification. (§6)
2. **rf-mcp install needs a manual `git clone`** of upstream (no `--bundle`-installable signed
   component bundle in the release). (§10)
3. **No `systemctl --user` unit** for the workstation provider identity; the packaged service runs as
   the `aithernet` system user. (§12)
4. **`mcp status/tools/diagnostics` JSONDecodeError** when no node server is running — outside the
   required path, cosmetic. (§18)
5. **Artifact `kind` metadata** in the hosted manifest labels every artifact `kind:"wheel"` (the
   `release create` CLI doesn't infer kind). Cosmetic — filenames/digests/signatures/authorization are
   correct and verified.

## Stop condition
Physical Pluto / RF work (Phase 6) begins **only after** §1–§17 pass on the clean machine. No
transmission. After the software-only journey passes, proceed to Phase 6 with operator approval before
any physical SDR operation, unplug/reconnect, or reboot.
