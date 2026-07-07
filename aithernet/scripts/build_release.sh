#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build_release.sh — reproducible Aithernet release-bundle builder.
#
# Produces, OUTSIDE git, the complete customer download bundle for ONE version:
#   aithernet-<pep440>-py3-none-any.whl        core wheel
#   aithernet-<pep440>.tar.gz                  clean sdist
#   aithernet_<deb-version>_amd64.deb          .deb (/usr/bin/aithernet; no aithernet-hosted)
#   rf-mcp-<rfver>-src.tar.gz                  managed RF-MCP component source (GPL corresponding)
#   lock.json                                  component provenance lock
#   aithernet-install.sh                       guided installer (scripts/install.sh)
#   NOTICE.md, THIRD-PARTY-NOTICES.md          licensing notices
#   sbom.json                                  dependency SBOM (from pyproject)
#   hardware-manifest.json                     hardware-profile recipes (from PROFILES)
#   aithernet-release.pub                      verification public key (PEM)
#   manifest.json                              canonical signed manifest (artifacts + digests)
#   manifest.sig                               detached Ed25519 signature over manifest.json
#   SHA256SUMS                                 checksums of every artifact (incl. manifest.json)
#
# Reuses `python -m build`, scripts/build_deb.sh and components/rf-mcp/build.sh. The output
# contains NO developer-checkout path, NO secret, and NO runtime state. The release private key
# stays under <out>/keys (0600); it is never placed in the published archive.
#
# Env:
#   VERSION    release version (default: pyproject [project].version, e.g. 0.8.0-beta.4)
#   CHANNEL    release channel (default: early-access)
#   CREATED    manifest created timestamp, fixed for reproducibility (default: 2026-06-20T00:00:00Z)
#   KEY_ID     signing key id recorded in the manifest (default derived from version)
#   OUT_DIR    bundle output root (default: ~/.local/state/aithernet/lan-release-<tag>)
#   SIGN_KEY   PEM Ed25519 release private key (default: generated under OUT_DIR/keys, 0600)
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${HERE}/.." && pwd)"
PY="${PY:-${REPO_ROOT}/.venv/bin/python}"

log() { printf '[build-release] %s\n' "$*" >&2; }

VERSION="${VERSION:-$(sed -n 's/^version = "\(.*\)"/\1/p' "${REPO_ROOT}/pyproject.toml" | head -1)}"
CHANNEL="${CHANNEL:-early-access}"
CREATED="${CREATED:-2026-06-20T00:00:00Z}"
PEP440="$("${PY}" -c "from packaging.version import Version; print(str(Version('${VERSION}')))")"
DEB_VERSION="${VERSION//-/\~}"
TAG="$(printf '%s' "${VERSION}" | tr -c '0-9A-Za-z.' '-')"
OUT_DIR="${OUT_DIR:-${HOME}/.local/state/aithernet/lan-release-${TAG}}"
KEY_ID="${KEY_ID:-aithernet-lan-betaqual-${TAG}-20260620}"
RFVER="$(sed -n 's/^version = "\(.*\)"/\1/p' "${REPO_ROOT}/components/rf-mcp/component.toml" | head -1)"

ARCHIVE="${OUT_DIR}/archive"
KEYS="${OUT_DIR}/keys"
DIST="${OUT_DIR}/dist"
DEB_BUILD="${OUT_DIR}/deb-build"

log "version=${VERSION} pep440=${PEP440} deb=${DEB_VERSION} rfver=${RFVER} channel=${CHANNEL}"
log "out=${OUT_DIR}"

rm -rf "${ARCHIVE}" "${DIST}" "${DEB_BUILD}"
mkdir -p "${ARCHIVE}" "${KEYS}"
chmod 700 "${KEYS}"

cd "${REPO_ROOT}"

# 1) Wheel + clean sdist (hatchling honours the sdist exclude block in pyproject).
log "building wheel + sdist"
"${PY}" -m build --outdir "${DIST}" >&2
cp "${DIST}/aithernet-${PEP440}-py3-none-any.whl" "${ARCHIVE}/"
cp "${DIST}/aithernet-${PEP440}.tar.gz" "${ARCHIVE}/"

# 2) .deb (sanitized payload; /usr/bin/aithernet; never ships aithernet-hosted).
log "building .deb"
VERSION="${VERSION}" ARCH=amd64 BUILD_DIR="${DEB_BUILD}" bash scripts/build_deb.sh >&2
cp "${DEB_BUILD}/aithernet_${DEB_VERSION}_amd64.deb" "${ARCHIVE}/"

# 3) Managed RF-MCP component bundle (self-contained, OFFLINE-installable). Built in an ISOLATED
#    root so the live install is never touched, and SIGNED with the PINNED component key so a
#    customer can `aithernet components install rf-mcp --bundle <this dir>` with verified trust.
#    Ships the source tarball + lock + detached signature + the (pinned) component public key.
COMPONENT_SIGN_KEY="${COMPONENT_SIGN_KEY:-${HOME}/.local/state/aithernet/component-signing/aithernet-component.key}"
if [[ ! -f "${COMPONENT_SIGN_KEY}" ]]; then
  log "ERROR: pinned component signing key not found: ${COMPONENT_SIGN_KEY}"
  log "       (its public key is committed at components/rf-mcp/aithernet-component.pub)"
  exit 1
fi
log "building rf-mcp component bundle (${RFVER}, signed with the pinned component key)"
CTMP="$(mktemp -d)"; trap 'rm -rf "${CTMP}"' EXIT
SIGN_KEY="${COMPONENT_SIGN_KEY}" OUT_DIR="${CTMP}/out" COMPONENTS_ROOT="${CTMP}/root" \
  bash components/rf-mcp/build.sh >&2
cp "${CTMP}/out/rf-mcp-${RFVER}-src.tar.gz" "${ARCHIVE}/"
cp "${CTMP}/out/rf-mcp-${RFVER}-src.tar.gz.sig" "${ARCHIVE}/"
cp "${CTMP}/out/aithernet-component.pub" "${ARCHIVE}/"
cp "${CTMP}/out/lock.json" "${ARCHIVE}/"
# The bundle's public key MUST equal the pinned key committed in the spec (trust anchor).
if ! diff -q "${ARCHIVE}/aithernet-component.pub" components/rf-mcp/aithernet-component.pub >/dev/null; then
  log "ERROR: built component public key does not match the pinned components/rf-mcp/aithernet-component.pub"
  exit 1
fi

# 3b) CatGPT Gateway runtime image (Option A) — shipped so a fresh client never clones, builds, or
#     pulls from a public repo. Included as a compressed `docker save` tar; `aithernet catgpt start`
#     loads it locally (pull_policy: never). Set CATGPT_IMAGE_TAR to a prebuilt tar, or let the
#     component build produce/reuse one (needs docker + a local image or CATGPT_SOURCE_DIR).
CATGPT_IMAGE_TAR="${CATGPT_IMAGE_TAR:-}"
if [ -z "${CATGPT_IMAGE_TAR}" ] && [ -x components/catgpt-gateway/build.sh ]; then
  log "preparing CatGPT Gateway runtime image component"
  if CATGPT_IMAGE_TAR="$(VERSION="${VERSION}" bash components/catgpt-gateway/build.sh --print-tar)"; then
    :
  else
    log "WARNING: CatGPT image component build did not produce a tar"
    CATGPT_IMAGE_TAR=""
  fi
fi
if [ -n "${CATGPT_IMAGE_TAR}" ] && [ -f "${CATGPT_IMAGE_TAR}" ]; then
  cp "${CATGPT_IMAGE_TAR}" "${ARCHIVE}/"
  log "included CatGPT Gateway runtime image: $(basename "${CATGPT_IMAGE_TAR}")"
else
  log "WARNING: no CatGPT Gateway runtime image tar included — a fresh client could NOT start the"
  log "         managed gateway from this bundle. Build it (components/catgpt-gateway/build.sh) or"
  log "         set CATGPT_IMAGE_TAR=<path>, then rebuild."
fi

# 4) Installer + notices + release README (canonical verify/install summary; full journey in portal).
log "adding installer + notices + README"
cp scripts/install.sh "${ARCHIVE}/aithernet-install.sh"
cp components/rf-mcp/NOTICE.md "${ARCHIVE}/NOTICE.md"
# beta.5: ship the CatGPT Gateway component notice + upstream MIT license (wrapper/sidecar; the
# upstream source is not redistributed in the .deb, so the notice + license satisfy MIT).
mkdir -p "${ARCHIVE}/notices/catgpt-gateway"
cp components/catgpt-gateway/NOTICE.md "${ARCHIVE}/notices/catgpt-gateway/NOTICE.md"
cp components/catgpt-gateway/LICENSE.upstream "${ARCHIVE}/notices/catgpt-gateway/LICENSE"
DEB_NAME="aithernet_${DEB_VERSION}_amd64.deb"
cat > "${ARCHIVE}/README.md" <<EOF
# Aithernet ${VERSION} — release bundle (Ubuntu 24.04 LTS · amd64)

Verify BEFORE installing (standard tools, no Aithernet binary yet):

\`\`\`bash
sha256sum -c SHA256SUMS
openssl pkeyutl -verify -pubin -inkey aithernet-release.pub \\
  -rawin -in manifest.json -sigfile manifest.sig
\`\`\`

Install the native package (sudo is for APT only; never use pip for GNU Radio / SoapySDR / UHD /
libiio / python3-venv):

\`\`\`bash
sudo apt update
sudo apt install ./${DEB_NAME}
aithernet --version
\`\`\`

Then follow the complete, copyable post-login journey (setup, providers, services, missions) on the
**authenticated customer portal → Getting started** page. Standalone setup makes zero hosted
requests; software-only does not require a physical SDR. This bundle also ships the signed managed
RF-MCP component (source + detached signature + pinned key + lock) and the guided
\`aithernet-install.sh --deb ./${DEB_NAME}\`.
EOF
cat > "${ARCHIVE}/THIRD-PARTY-NOTICES.md" <<EOF
# Aithernet ${VERSION} — Third-Party Notices

Aithernet bundles, as a separately versioned managed component, the GNU Radio MCP server
("rf-mcp", ${RFVER}), derived from https://github.com/yoelbassin/gr-mcp under **GPL-3.0-only**.
The corresponding source is shipped as \`rf-mcp-${RFVER}-src.tar.gz\` (with its LICENSE and
AITHERNET-NOTICE.md inside) to satisfy the GPL source-availability obligation.

Aithernet also ships the optional local **CatGPT Gateway** coordinator backend (\`aithernet catgpt\`).
The upstream project (CatGPT-Gateway, https://github.com/GautamVhavle/CatGPT-Gateway) is
**MIT-licensed**; Aithernet ships its runtime as a pinned container image
(\`catgpt-gateway-*-linux-amd64.docker.tar.zst\`, tagged \`aithernet/catgpt-gateway:<version>\`,
built from the pinned upstream commit) so a client never clones, builds, or pulls it. MIT permits
this binary redistribution with the copyright + permission notice, which ship under
\`notices/catgpt-gateway/\` (\`NOTICE.md\` + upstream \`LICENSE\`). The image contains no browser
session/cookies/web credentials. The gateway only provides coordinator model inference; it is never
the mission API/DB/run-queue, is bound to 127.0.0.1 by default, and the user performs their own
ChatGPT/Claude login via noVNC.

The Aithernet node runtime itself is MIT-licensed. Its Python dependencies (see \`sbom.json\`)
are installed from PyPI under their own licenses and are not redistributed in this bundle.

GNU Radio and SDR system packages (gnuradio, libiio, UHD, SoapySDR, drivers) are installed from
the distribution's signed apt repositories per the selected hardware profile; see
\`hardware-manifest.json\`. They are not redistributed here.
EOF

# 5) SBOM (from pyproject dependencies) + hardware-profile manifest (from PROFILES).
log "generating sbom.json + hardware-manifest.json"
"${PY}" - "${VERSION}" > "${ARCHIVE}/sbom.json" <<'PY'
import json, sys, tomllib, pathlib
version = sys.argv[1]
data = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
deps = data["project"].get("dependencies", [])
def name_of(spec):
    for i, ch in enumerate(spec):
        if ch in "<>=!~[ ":
            return spec[:i]
    return spec
comps = [{"name": name_of(d), "version_spec": d, "type": "library", "source": "PyPI"} for d in deps]
# The shipped CatGPT Gateway runtime image (built from the pinned upstream commit).
comps.append({
    "name": "catgpt-gateway", "version_spec": version, "type": "container-image",
    "source": "https://github.com/GautamVhavle/CatGPT-Gateway",
    "commit": "79a1b69d429fa9951d796289b60470fb595cbb42", "license": "MIT",
    "image_tag": f"aithernet/catgpt-gateway:{version}",
    "artifact": f"catgpt-gateway-{version}-linux-amd64.docker.tar.zst",
    "contains_web_credentials": False,
})
sbom = {
    "bomFormat": "AithernetSBOM", "specVersion": "1.0", "version": 1,
    "metadata": {"component": {"name": "aithernet", "version": version},
                 "baseline": "Ubuntu 24.04 LTS amd64",
                 "python_min": data["project"].get("requires-python", "")},
    "components": comps,
}
print(json.dumps(sbom, indent=2))
PY

"${PY}" - > "${ARCHIVE}/hardware-manifest.json" <<'PY'
import json
from aithernet.hardware.setup import PROFILES
profiles = {}
for name, p in PROFILES.items():
    profiles[name] = {"description": p.get("description", ""), "apt": p.get("apt", []),
                      "post": p.get("post", []), "groups": p.get("groups", [])}
print(json.dumps({"baseline": "Ubuntu 24.04 LTS amd64",
                  "mode": "signed distribution packages (no source build by default)",
                  "profiles": profiles}, indent=2))
PY

# 6) Release signing key (PEM Ed25519): reuse if present, else generate (0600, never published).
KEY="${SIGN_KEY:-${KEYS}/aithernet-release.key}"
if [[ ! -f "${KEY}" ]]; then
  log "generating release signing key (kept under ${KEYS}, 0600)"
  ( umask 077; openssl genpkey -algorithm ed25519 -out "${KEY}" )
fi
openssl pkey -in "${KEY}" -pubout -out "${ARCHIVE}/aithernet-release.pub"
cp "${ARCHIVE}/aithernet-release.pub" "${KEYS}/aithernet-release.pub"
printf '%s\n' "${KEY_ID}" > "${KEYS}/KEY_ID.txt"

# 7) Canonical manifest over every artifact (compact, sorted) — matches the published format the
#    customer verifies with `openssl pkeyutl -verify` + `sha256sum -c`.
"${PY}" - "${ARCHIVE}" "${CHANNEL}" "${CREATED}" "${KEY_ID}" "${VERSION}" \
    > "${ARCHIVE}/manifest.json" <<'PY'
import hashlib, json, sys, pathlib
archive, channel, created, key_id, version = sys.argv[1:6]
skip = {"manifest.json", "manifest.sig", "SHA256SUMS"}
def kind_of(n):
    if n.endswith(".whl"): return "wheel"
    if n.endswith(".deb"): return "deb"
    if n.endswith(".sig"): return "signature"
    if n.endswith(".pub") or n.endswith(".pem"): return "pubkey"
    if n.startswith("rf-mcp-") and n.endswith("-src.tar.gz"): return "component-source"
    if n.endswith(".docker.tar.zst") or n.endswith(".docker.tar"): return "runtime-image"
    if n.startswith("aithernet-") and n.endswith(".tar.gz"): return "sdist"
    if n == "aithernet-install.sh": return "installer"
    if n in ("NOTICE.md", "THIRD-PARTY-NOTICES.md"): return "notice"
    return "metadata"
arts = []
for p in sorted(pathlib.Path(archive).iterdir()):
    if not p.is_file() or p.name in skip:
        continue
    b = p.read_bytes()
    arts.append({"byte_size": len(b), "kind": kind_of(p.name), "name": p.name,
                 "sha256": hashlib.sha256(b).hexdigest()})
manifest = {"artifacts": arts, "channel": channel, "created": created,
            "signing_key_id": key_id, "version": version}
sys.stdout.write(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
PY

# 8) Detached Ed25519 signature over the canonical manifest bytes.
log "signing manifest.json"
openssl pkeyutl -sign -inkey "${KEY}" -rawin -in "${ARCHIVE}/manifest.json" \
  -out "${ARCHIVE}/manifest.sig"

# 9) SHA256SUMS over everything except the signature + the sums file itself.
log "writing SHA256SUMS"
( cd "${ARCHIVE}" && \
  find . -maxdepth 1 -type f ! -name manifest.sig ! -name SHA256SUMS -printf '%P\n' \
    | LC_ALL=C sort | xargs sha256sum > SHA256SUMS )

log "DONE  ${VERSION}  ->  ${ARCHIVE}"
printf 'ARCHIVE=%s\nKEY_ID=%s\n' "${ARCHIVE}" "${KEY_ID}"
