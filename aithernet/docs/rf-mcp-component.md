# RF-MCP component — provenance, adoption, and reproducible bundle

The product-facing component is **`rf-mcp`**; its implementation identity is **gr-mcp**
(`yoelbassin/gr-mcp`). Aithernet runs the **installed** component in the managed prefix — never a
developer checkout, never the local `gr-mcp/` working tree, never Marconi.

## Provenance (canonical, formalized)
- **Upstream:** `https://github.com/yoelbassin/gr-mcp`
- **Pinned base commit:** `8ca731e3f9a083b32170c9a0e6ee3d1e61b4f3de`
- **Aithernet patch series:** `components/rf-mcp/patches/0001-python312-numpy-gnuradio-compat.patch`
  (relaxes `requires-python` to `>=3.12` for the Ubuntu 24.04 GNU Radio 3.10 / NumPy 1.x baseline).
  This is the exact, formalized form of the one tracked working-tree change in the local checkout —
  there is **no** dependency on an undocumented dirty file.
- **Patch-set digest:** `e1714606bef9ec0452871df370df8f8af09acc3978a79d77364a25e03ada31fd`
- **Version:** `0.1.0+aithernet.1` · **runtime:** `uv run --no-sync python main.py` · **entrypoint:**
  `main.py` · **license:** GPL-3.0-only (LICENSE + NOTICE ship with the source bundle).

## Adopting an existing working installation — `aithernet components adopt`
Use this when a working RF-MCP is already installed but lacks current component-manager metadata
(e.g. built by an older builder). Adoption records provenance + a hashed file inventory **without
replacing any runtime file**.

```
aithernet components adopt rf-mcp                         # dry-run report (default)
aithernet components adopt rf-mcp --source-checkout <ro>  # also verify installed == base+patch
aithernet components adopt rf-mcp --write-metadata        # write lock + inventory (backs up old)
aithernet components adopt rf-mcp --json
```

It inspects the managed prefix; inventories all owned files + per-file digests + an inventory digest;
verifies the executable/entrypoint, recorded version, and base commit; (optionally, read-only)
archives the pinned base commit from a local checkout, applies the patch series, and confirms the
installed files match the canonical source tree; records the full provenance; **stops and reports**
if anything mismatches; writes **only** metadata (lock + inventory), backing up the previous lock to
`.aithernet-component-lock.json.bak.<ts>`; and is idempotent. Afterwards `aithernet components verify
rf-mcp` and `aithernet doctor` report the component ready, and later file drift is detected via the
inventory. **Rollback:** restore the `.bak.<ts>` lock over `.aithernet-component-lock.json`.

The `--source-checkout` path is a development aid for deriving/verifying the source tree; it is
**never** a client runtime dependency.

## Reproducible client bundle (no developer dependency)
`components/rf-mcp/build.sh` is the reproducible build definition. It runs **outside** the repo and
produces a signed, installable bundle from the pinned provenance:

```
pinned base commit
  → apply formalized Aithernet patch series
  → deterministic source tree (fixed mtime/owner, sorted)  → reproducible source sha256
  → bundle: <name>-<ver>-src.tar.gz + .sha256 + detached Ed25519 .sig + public key + lock.json
  → Aithernet component / release channel
  → client: `aithernet components install rf-mcp --bundle <dir>`  → managed prefix
```

`install --bundle` verifies the archive digest, the pinned commit, and the detached signature against
the bundle's public key before extracting. The client therefore never needs to: clone gr-mcp, reach a
developer checkout, know any developer path, apply patches manually, or install Marconi. The spec +
patch + build.sh ship inside the wheel/.deb (`aithernet/_component_specs/rf-mcp/`), so adoption and
bundle install work from the installed package alone.

> Producing the production release / publishing the component to a channel is a separate operator
> gate and is not performed here.
