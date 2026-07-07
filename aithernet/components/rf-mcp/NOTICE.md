# RF MCP component — source & license notice

The Aithernet **rf-mcp** component is the GNU Radio MCP server
[`yoelbassin/gr-mcp`](https://github.com/yoelbassin/gr-mcp), redistributed under
the **GNU General Public License v3.0 (GPL-3.0-only)**.

- **Upstream:** https://github.com/yoelbassin/gr-mcp
- **Pinned commit:** `8ca731e3f9a083b32170c9a0e6ee3d1e61b4f3de`
- **Aithernet modifications:** a single patch
  (`patches/0001-python312-numpy-gnuradio-compat.patch`) relaxing the required
  Python version from `>=3.13` to `>=3.12` so the component builds and runs
  against the GNU Radio 3.10 / Python 3.12 / NumPy 1.x bindings shipped on
  Ubuntu 24.04 LTS. No other source is changed.

## Corresponding source (GPL §3)

Because this component is distributed in binary/installable form, the
**complete corresponding source** is shipped alongside it as
`rf-mcp-<version>-src.tar.gz` (upstream at the pinned commit with the Aithernet
patch series applied), together with the upstream `LICENSE` (GPL-3.0) and this
notice. The `lock.json` manifest records the upstream URL/commit, the patch
SHA-256, the source-archive SHA-256, and the build/runtime commands so any
recipient can reproduce the exact source independently.

This notice satisfies the GPL obligation to convey the license and to provide
(or offer) the corresponding source for the conveyed work.
