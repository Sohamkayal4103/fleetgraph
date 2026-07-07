# Aithernet Packaging & Installers (Stage 14F)

This directory documents how Aithernet is built into distributable artifacts and
how operators install them. Version: **0.8.0-beta.1**.

Console entry points (from `pyproject.toml`):

- `aithernet` — the local SDR node CLI (`aithernet start`, `aithernet node preflight`, …)
- `aithernet-hosted` — the hosted control-plane CLI

---

## 1. Build the wheel / sdist

The project uses the **hatchling** build backend. From the repo root:

```bash
# Recommended: PEP 517 build (produces both wheel and sdist in dist/)
python -m build            # -> dist/aithernet-0.8.0b1-py3-none-any.whl + .tar.gz

# Or just a wheel:
pip wheel . -w dist/
```

The wheel bundles `src/aithernet` and `services` (see
`[tool.hatch.build.targets.wheel]`).

---

## 2. Install for beta users (no repo clone)

Beta users do **not** need to clone the repo. Install the published artifact
directly into an isolated environment with [`pipx`](https://pypa.github.io/pipx/):

```bash
pipx install ./dist/aithernet-0.8.0b1-py3-none-any.whl
# or from a release URL once published:
pipx install https://downloads.example.invalid/0.8.0-beta.1/aithernet-0.8.0b1-py3-none-any.whl
```

This gives `aithernet` / `aithernet-hosted` on `PATH` in their own venv. For an
unattended/system install, use the controlled installer in
[`scripts/install.sh`](../scripts/install.sh) (verifies signatures + checksums).

---

## 3. Build the Debian package

[`scripts/build_deb.sh`](../scripts/build_deb.sh) stages a complete install tree
and runs `dpkg-deb --build`. It does **not** require root.

```bash
# Inspect the staged layout without building (no dpkg-deb invoked):
DRY_RUN=1 bash scripts/build_deb.sh

# Build amd64 at the default version:
bash scripts/build_deb.sh

# Override version/arch:
VERSION=0.8.0-beta.1 ARCH=arm64 bash scripts/build_deb.sh
```

Installed layout:

| Path | Contents |
|------|----------|
| `/opt/aithernet` | application payload (`pip --target` tree) + launcher + `VERSION` + uninstall manifest |
| `/etc/aithernet` | `node.yaml.template`, `aithernet.env.template` (templates only) |
| `/var/lib/aithernet` | node state root: `{config,db,identity,run,logs,backups,artifacts}` (`identity` is `0700`) |
| `/lib/systemd/system/aithernet-node.service` | hardened unit |
| `/usr/share/doc/aithernet` | packaging docs + `README.Debian` |

GNU Radio / UHD / SoapySDR are **detected on the build host** and, when present,
declared only as `Recommends:` — never a hard `Depends:`. The package always
hard-depends only on `python3 (>= 3.11)`.

The `postinst` creates the dedicated `aithernet` service user/group, owns
`/var/lib/aithernet`, and sets `identity/` to `0700`.

---

## 4. Release archive format

A release is a set of artifacts plus integrity/authenticity metadata:

- `manifest.json` — version, channel, signing key id, and per-artifact
  `{name, kind, sha256, byte_size}` entries.
- `SHA256SUMS` — convenience checksums (the manifest is authoritative).
- `manifest.sig` — detached **Ed25519** signature over `manifest.json`.

The verification **public** key ships with the installer
(`scripts/aithernet-release.pub`). Full schema and the offline
signing/verification commands are in
[`release_archive.md`](./release_archive.md).

[`scripts/install.sh`](../scripts/install.sh) fetches the manifest, verifies the
signature **first**, then verifies each artifact's SHA-256 before unpacking.
It supports `--dry-run`, `--offline <dir>` (no network, works against a local
release dir or `file://`), `--uninstall`, and requires confirmation (`--yes`
bypasses for automation) before any privileged action.

---

## 5. What is intentionally NOT bundled

None of the following ever ship in a wheel, `.deb`, or release archive:

- Gemini / Codex / Google OAuth credentials or any provider API tokens.
- Release-**signing private** keys (only the public verification key ships).
- The operator's `.env` file.
- Consent records.
- Node **identity private keys**.
- External RF repo checkouts (`gr-mcp/`, `marconi`, `workspace/`).

`build_deb.sh` actively sanitizes the payload to enforce this boundary.

---

## 6. Containers vs. the local SDR node

Container images **may** be provided for the **hosted** services
(`services.control_plane`, `services.ingestion`) — see the
`aithernet-control-plane` / `aithernet-ingestion` systemd units in
[`../deploy/systemd/`](../deploy/systemd/).

A container alone is **not sufficient** for the **local SDR node**: the node
needs host device access (USB SDR via udev/`plugdev`), a system GNU Radio /
SoapySDR/UHD stack, and a persistent on-host state root. Install the node via
the `.deb`, `pipx`, or `scripts/install.sh` on the host, not as a bare
container.
