# Aithernet 1.0.0-beta.4 — production release record (PUBLISHED)

Provider-neutral agent runtime. beta.3 and all earlier releases remain immutable.

## Identifiers
- Release ID: `d5b2e07d-b6ae-49e3-ab6d-af1be6b2bd50` · status **published** · channel `early-access`
- Signing key id: `aithernet-prod-betaqual-20260620`
- Release manifest digest (prod-signed): `sha256:c5c15744c4d3e76ddd8b8278e03cecda407ce2dbee111ad3178185752660ef7d`
- Source commit: `9aed5d7` · tag `v1.0.0-beta.4` · branch `provider-runtime-beta4`
- RF-MCP component `0.1.0+aithernet.2` (unchanged); 17 artifacts attached.

## Artifacts (prod blob == qualified bundle)
- `aithernet_1.0.0~beta.4_amd64.deb` — 16,115,306 B — `sha256:5daf37916e42096ec5a86e518699ba0a0072ff6e18c8f2bc94c236714047ab48`
- `aithernet-1.0.0b4-py3-none-any.whl` — 885,381 B — `sha256:1d1b8083c607e3aeae46e93db3c194786ee46ca9fe045d9933b15510d982ae35`
- `aithernet-1.0.0b4.tar.gz` — 1,392,597 B — `sha256:4c47852edb2cbc73f33bfc836eaf3bb072beeefeaad40c107daf2dd2b84fc675`

## Qualification (deterministic; no live external billing)
49 new provider-neutral tests + provider regression green (coordinator 13, coding_agent 23, all
adapters/errors/readiness/dataset-builder); only 2 pre-existing environmental failures. Clean install
on Ubuntu 24.04 → 1.0.0b4 with full provider discovery (11 coordinators, 5 coding), presets, capacity,
billing labels. .deb uses the cpython-312 ABI; sdist clean (no state/secrets/build artifacts).

## Live qualification
Executables present on the qualification host: `codex` 0.137.0, `gemini` 0.46.0; `claude` absent. A
bounded `gemini_cli` coordinator live decision was attempted and FAILED (executable present but not
live-authed) — NOT claimed as passed. All API/cloud/subscription live methods are operator-run
(no working credentials on the qualification host). Absence of credentials is not a publication
blocker; no live method is claimed to have passed.
