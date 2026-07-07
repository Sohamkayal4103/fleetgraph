# Aithernet 1.0.0-beta.2 — production release (PUBLISHED)

Early-access channel. **Operator-controlled physical SDR transmission** + `rf_ota` peer transport.
beta.1 and all earlier releases remain immutable. 1.0.0-beta.2 is the current early-access release;
1.0.0-beta.1 is the preceding release.

## Identifiers
- Release ID: `362ce654-f4b9-43a7-96a2-5a9670be6da3` · status **published** · channel `early-access`
- Signing key id: `aithernet-prod-betaqual-20260620`
- Release manifest digest: `sha256:4a992050c6987cbfd1d442c96270bafe007e349770aadc1019a1fabb723f83c0`
- Source commit: `ee567f8` · tag: `v1.0.0-beta.2` → `51fea81`
- RF-MCP component: `0.1.0+aithernet.2` `sha256:200b7671…` (unchanged)

## Production artifacts (prod blob == qualified bundle)
- `aithernet_1.0.0~beta.2_amd64.deb` — 16,097,882 B — `sha256:4bbd60ebbfab1e8c88308f5424e88287b2e497e5388d2b08dd20b6c9fddabe39`
- `aithernet-1.0.0b2-py3-none-any.whl` — `sha256:9f41dedf…`
- `aithernet-1.0.0b2.tar.gz` — `sha256:e1b70be7…`
- 16 artifacts; manifest signature verifies with the published key.

## Policy correction
Physical SDR transmission is now a **real, LOCALLY operator-controlled capability**. No vendor
approval, cloud authorization, subscription, hidden allowlist, release key, or external service is
required. Aithernet does not reserve, withhold, or remotely gate physical TX. Retained are the
**technical-correctness controls** the operator asked for: local enablement, device capability +
range enforcement, operator ceilings, peer authentication, replay protection, bounded execution,
and explicit local plan confirmation. Aithernet never chooses an operating frequency.

What shipped: operator TX config + CLI (`rf tx enable/disable/status/configure/devices/
capabilities/test-plan/loopback/send-test-frame`, `rf tx plans authorize/list/inspect/revoke`);
`rf_ota` selectable when locally enabled + a capable device is present; managed TX executor
(structured plan only — no shell/model source; loopback backend for automated tests, hardware
backend operator-run); PlutoSDR (libiio) capability discovery (open to other SDRs); descriptive
profile qualification states that inform but never prohibit; physical-TX research records; the
`physical-tx` dataset class.

## Qualification (software; no radiation)
83 tests (OTA Phases 1–4, selection, runtime-link, research/datasets, operator-TX, doc-CI). Hardware
mocks + GNU Radio loopback only — **no radiation; no air test fabricated**. Production download
verified (prod `.deb` byte-identical to the qualified bundle; manifest signature verifies). Clean
install on Ubuntu 24.04 → `1.0.0b2`; `rf tx` disabled by default → enabled by local
`rf tx enable`; `vendor_authorization_required: false`; PlutoSDR capability range reported; rf_ota
state accurate; loopback decoded with `radiated: false`.

## Physical qualification is operator-run
Cabled, shielded, and authorized-OTA tests are run by the operator on their own hardware after
installation (the operator supplies all RF parameters and is responsible for local-rules
compliance). The operator may mark a profile `hardware_tested` from their results. The optional live
external-coordinator → coding-agent → generated-modem test uses the operator's own coordinator
credential.

## Not claimed
No physical OTA test occurred here. No approved operating frequency. No live coding-agent modem
generation during release qualification. No unrestricted model-generated RF. No hidden
chain-of-thought collection.
