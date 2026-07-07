# beta.3 artifact provenance — do NOT publish to production (yet)

**Decision: the existing LAN beta.3 artifacts must not be exposed as the production customer
download.** Production currently has **0 published releases**, which is the correct, safe state.

## Artifacts inspected (local, outside Git)
`~/.local/state/aithernet/lan-release-beta3/artifacts/` (version `0.8.0b3`):

| Artifact | SHA-256 |
| --- | --- |
| `aithernet-0.8.0b3-py3-none-any.whl` | `3f5df4e1d61303149719b7e7b4b96a93b19f16737025c518d584c89bcb980205` |
| `aithernet-0.8.0b3.tar.gz` | `fcf404d22f703a1ee91349d8d39225e8378810d182aa33ea1e4efaf7ad6d53c8` |
| `aithernet_0.8.0~beta.3_amd64.deb` | `692d4cc79a535af5b60ab15217c724b0ea50a5f5a9af3beaaf7697bffffd3c92` |

## Why they must not be published

These artifacts predate important node-side source changes and therefore **do not reproducibly
correspond to the current intended source**:

1. They were built before `faad3db` (managed RF component + `hardware`/`components` CLI) and
   `5683114` (the `drive` CLI) — so they lack those node commands.
2. They predate the **authenticated node-download** change (this work): the package download route
   is now restricted, and `aithernet.hosted.client.download` now sends **signed** node requests to
   `/v1/node/downloads/...`. A beta.3 node would attempt the old public `/v1/downloads/...` path and
   be refused (403) by the new authorization model — i.e. a published beta.3 would be unable to
   self-update.

## Required before any production download is published

A fresh qualification build **from the current source** (after the guided-setup / component
foundations are real), then verify before publishing:
- exact source commit + reproducible build inputs;
- manifest digest + per-artifact SHA-256 + byte counts;
- signing-key id + cryptographic signature over the manifest;
- package contents include the current node code (RF/Drive/components commands + signed-download
  client);
- the verification key is published (public) and the package artifacts are gated behind the
  authenticated customer/node routes.

Until then: **no production release, no public Git tag, and no "physically qualified" claim.** The
customer releases page truthfully shows "no published releases yet."
