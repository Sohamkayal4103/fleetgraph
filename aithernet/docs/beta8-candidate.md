# Aithernet 0.8.0-beta.8 — private candidate + clean-client handoff (NOT published)

Final source commit: `8eb40a9` (branch `beta-qualification`). Built with `scripts/build_release.sh`
→ `~/.local/state/aithernet/lan-release-0.8.0-beta.8/archive`. Candidate signing (local):
`aithernet-lan-betaqual-0.8.0-beta.8-20260620`. Production publication re-signs with the production
key (as for beta.5–7); not performed.

## Candidate digests (full SHA-256, final build at 8eb40a9)
- ZIP `aithernet-0.8.0-beta.8-ubuntu24.04-amd64.zip` — 18,132,524 B —
  `85e6004e68443ff3d3416aa7e6dbb4f3215d5a6338a1d0d5f8fc773f50475d1d`
- `.deb` `aithernet_0.8.0~beta.8_amd64.deb` — 16,037,142 B —
  `6ac64d5fd74fa30e58dc85f8809e74d26e7c8a8b440b397b159ddb4f864db488`
- wheel `aithernet-0.8.0b8-py3-none-any.whl` —
  `92cbfbd49b282a2fed44f0c8303e50833e33e65472ecdf9c2e40d73a0404f78f`
- sdist `aithernet-0.8.0b8.tar.gz` —
  `7aa5d61ad48bff6309679a22839f3992d31b809f6c57542c61a1a4c9a0a6abe1`
- RF-MCP src `rf-mcp-0.1.0+aithernet.2-src.tar.gz` —
  `200b7671da9cfd694ae43c9a2d0d1a569eaa7808d1cfdccec74ff592ffa5a73c`

## Verification (final build)
- Release manifest signature verifies; RF-MCP component signature verifies against the **pinned**
  `aithernet-component.pub` (matches the committed trust anchor).
- Deterministic ZIP (identical on rebuild); `sha256sum -c SHA256SUMS` from an empty extract: exit 0.
- `.deb` installs source-hidden; `aithernet --version` → `aithernet 0.8.0b8`.
- beta.7→beta.8 migration preserves identity + binds the canonical config (auto-tested).
- Secret/dev-path scan: deb 0 `/home/aditya`, 0 `*.key`/`*.env`/`*.db`, 0 `aithernet-hosted`;
  sdist 0 `/home/aditya`. The only "PRIVATE KEY" string hits are the redaction regex
  (`data/redaction.py`), the cryptography library's PEM markers, and the handoff evidence script's
  own leak-scan regex — never an actual key.

## Clean-client handoff
`~/.local/state/aithernet/beta8-clean-client-handoff/` — the ZIP + loose release files +
`aithernet-install.sh` + RF-MCP signed bundle + notices + `README.md` + `ACCEPTANCE-GUIDE.md`
(Journeys A fresh / B beta.7 upgrade) + `aithernet-accept.sh` (bounded evidence collector) +
`beta8-clean-client-handoff-inventory.txt` + `beta8-clean-client-handoff-SHA256SUMS`. No source,
databases, identities, credentials, dev paths or test workspaces.

## Operator-gated (not run here)
- Real Gemini API + Codex live mission acceptance (deterministic fake-provider path is auto-tested);
  run on the clean client with authorized credentials via `aithernet-accept.sh`.
- Physical SDR / Pluto qualification — out of scope; never run.
