# Clean-machine acceptance checklist (PHASE 9)

An exact, ordered operator checklist to qualify Aithernet on a **clean** Ubuntu 24.04 amd64 machine
with a physical ADALM-PLUTO. Every step maps to a real command. Nothing here has been run on a clean
machine yet — this is the procedure to follow when one is available, and the record to fill in.

> Status legend to record per step: ✅ pass · ⚠️ pass-with-notes · ❌ fail · ⛔ blocked.
> Do **not** mark any step "clean-machine qualified" until it has actually passed on the clean
> machine. No transmit at any point — all RF work is receive-only.

## 0. Preconditions
- [ ] Clean Ubuntu 24.04 LTS amd64 (fresh install or fresh container/VM for the package steps).
- [ ] Network access for downloads; a physical PlutoSDR for the RF steps.
- [ ] No Aithernet developer checkout on the machine (the product must work from the `.deb` alone).

## 1. Access request
- [ ] On the public site, open **Request early access**, submit the email + privacy acknowledgement,
      complete the Turnstile challenge. Expect the generic "if eligible…" response.

## 2. Invitation
- [ ] Receive the invitation email (check spam). The one-time token is **only** inside the accept
      link (`…/#/accept-invite?token=…`) — never a separate code.

## 3. Login / onboarding
- [ ] Open the accept link in a private window → review the policy → create the account / accept →
      reach the customer portal. Log out and back in.

## 4. Download + signature verification
- [ ] From the portal, note the release version / channel / OS / arch / qualification status / known
      limitations / artifact name / byte count / SHA-256 / signing-key id.
- [ ] Download the `.deb`. Verify the SHA-256 matches; verify the detached signature against the
      published signing key. Do not install if either check fails.

## 5. `.deb` installation
- [ ] `sudo apt install ./aithernet_*.deb` (or `dpkg -i` + `apt -f install`).
- [ ] `aithernet doctor` reports `core_version` and `install_source: deb (/opt/aithernet)`.

## 6. Guided setup (works from the installed package, no repo)
- [ ] `aithernet setup --dry-run --profile plutosdr` — review the full plan (packages, sources,
      estimates, privileged ops, restart/logout needs). Confirm it makes no changes.
- [ ] `aithernet setup --profile plutosdr` (interactive) — complete deployment mode, node identity,
      directories, service mode, hardware profile, SDR strategy, providers.
- [ ] Interrupt mid-run; re-run `aithernet setup --resume` → it continues. Run again → idempotent.

## 7. SDR dependencies (receive-only Pluto path)
- [ ] `aithernet sdr plan --profile plutosdr` — confirm the package set is exactly
      `gnuradio gr-iio libiio-* soapysdr-tools soapysdr-module-plutosdr` (no UHD, no module-all).
- [ ] `aithernet sdr install --profile plutosdr --yes` (privileged: `apt`).
- [ ] `aithernet sdr verify --profile plutosdr` → `from gnuradio import gr, iio` OK and
      `SoapySDRUtil --find` OK.

## 8. Managed RF-MCP component
- [ ] `aithernet components plan rf-mcp` — review provenance (upstream commit, patch-set digest,
      license, install prefix).
- [ ] `aithernet components install rf-mcp …` — provenance + signature verified before install.
- [ ] `aithernet components verify rf-mcp` → inventory intact, signature valid.

## 9. AI providers
- [ ] `aithernet agents configure coordinator --provider gemini_cli` and complete Gemini CLI auth.
- [ ] `aithernet agents configure coding --provider codex_cli` and complete Codex CLI auth.
- [ ] `aithernet agents test coordinator` (and `--live` for one real inference) → ready.
- [ ] `aithernet agents provider-status` shows the runtime Linux identity and no secrets.

## 10. Enrollment
- [ ] `aithernet enroll --base-url <portal> --code <one-time-code>` → enrolled.
- [ ] `aithernet hosted heartbeat` succeeds.

## 11. Doctor (honest readiness)
- [ ] `aithernet doctor` → core/identity/enrollment/service/RF-stack/permissions/devices/components
      /providers/disk/hosted/mission all reported with explicit states.
- [ ] `aithernet doctor --fix-plan` lists remediation only for non-ready items.

## 12. Pluto discovery + permissions
- [ ] `aithernet hardware discover` enumerates the Pluto with a stable URI (`ip:` and/or `usb:`).
- [ ] Non-root access works (user in `plugdev`/`dialout`; udev rules present).

## 13. Custom receive-only mission + artifact
- [ ] `aithernet mission rf-plan "<objective>" --device <pluto> --freq-hz … --sample-rate …
      --duration-s …` → an authorized plan (digest) within the receive envelope.
- [ ] Run the bounded receive-only mission; confirm a bounded IQ/derived artifact is produced.
- [ ] Verify the artifact digest + metadata.
- [ ] Run a **second** mission with a different bounded objective (proves the interface is not
      hardcoded). Cancel a mission mid-run and confirm clean cancellation.

## 14. Resilience
- [ ] Disconnect/reconnect the Pluto → discovery + a subsequent mission recover.
- [ ] Restart the RF-MCP component / the node service → recovery, no orphaned state.
- [ ] Reboot the machine → the service comes back (if enabled) and `aithernet doctor` is ready.

## 15. Update / rollback / repair / uninstall
- [ ] Apply an update (when published) and verify the new version via `aithernet doctor`.
- [ ] Roll back and verify the prior version is restored.
- [ ] `aithernet components repair rf-mcp` restores any altered owned files (bounded).
- [ ] `aithernet components remove rf-mcp` removes only owned files; uninstall the `.deb` cleanly.

## Result
- [ ] All steps pass on the clean machine → **then** (and only then) record this build as
      clean-machine qualified, with the machine spec, date, and the doctor `--json` output attached.
