#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build.sh — reproducible builder for the Aithernet rf-mcp component.
#
# Produces, OUTSIDE the repo, a signed installable component from a pinned
# upstream commit + the tracked Aithernet patch series:
#   <out>/rf-mcp-<version>-src.tar.gz   GPL corresponding source (installable)
#   <out>/rf-mcp-<version>-src.tar.gz.sha256
#   <out>/rf-mcp-<version>-src.tar.gz.sig   detached Ed25519 signature
#   <out>/lock.json                     lock manifest (provenance + hashes)
#   <out>/LICENSE, <out>/NOTICE.md      license + source notice
#   <out>/aithernet-component.pub       verification public key (PEM)
# then installs (extracts) the source into the managed prefix.
#
# It NEVER modifies the external repositories (git archive is read-only) and uses
# no checkout-specific absolute path in the installed component.
#
# Env:
#   UPSTREAM_GIT   git source for the pinned commit (default: a fresh read-only clone of the
#                  upstream URL; override with a local mirror for offline builds).
#   OUT_DIR        artifact output dir (default: state dir, outside the repo).
#   COMPONENTS_ROOT managed install root (default: /opt/aithernet/components;
#                  falls back to a writable state dir when /opt is not writable).
#   SIGN_KEY       PEM Ed25519 private key (default: generated under OUT_DIR/keys).
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

NAME="rf-mcp"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "${HERE}/component.toml" | head -1)"
UPSTREAM_URL="$(sed -n 's/^upstream_url = "\(.*\)"/\1/p' "${HERE}/component.toml" | head -1)"
COMMIT="$(sed -n 's/^upstream_commit = "\(.*\)"/\1/p' "${HERE}/component.toml" | head -1)"
# Aithernet patch series applied IN ORDER on top of the pinned upstream commit.
PATCHES=(
  "${HERE}/patches/0001-python312-numpy-gnuradio-compat.patch"
  "${HERE}/patches/0002-bounded-receive-only-iq-acquisition-tool.patch"
)

# Source of the pinned commit. Default: a FRESH read-only clone of the upstream URL into a temp
# mirror — NEVER a developer checkout. Override UPSTREAM_GIT with a local mirror for offline builds.
UPSTREAM_GIT="${UPSTREAM_GIT:-}"
OUT_DIR="${OUT_DIR:-${HOME}/.local/state/aithernet/components/${NAME}}"
COMPONENTS_ROOT="${COMPONENTS_ROOT:-/opt/aithernet/components}"
PREFIX_BASE="${COMPONENTS_ROOT}"
if ! mkdir -p "${COMPONENTS_ROOT}/.wtest" 2>/dev/null; then
  PREFIX_BASE="${HOME}/.local/share/aithernet/components"   # /opt not writable here
fi
rmdir "${COMPONENTS_ROOT}/.wtest" 2>/dev/null || true
PREFIX="${PREFIX_BASE}/${NAME}"

log() { printf '[rf-mcp build] %s\n' "$*" >&2; }
mkdir -p "${OUT_DIR}" "${OUT_DIR}/keys"
chmod 700 "${OUT_DIR}/keys" 2>/dev/null || true

STAGE="$(mktemp -d)"; trap 'rm -rf "${STAGE}"' EXIT
TOP="${NAME}-${VERSION}"

# 0) Resolve the source mirror. Default: a fresh, read-only clone of the upstream URL (no developer
#    checkout). An explicit UPSTREAM_GIT (local mirror) is used as-is for offline builds.
if [[ -z "${UPSTREAM_GIT}" ]]; then
  UPSTREAM_GIT="${STAGE}/upstream.git"
  log "cloning upstream ${UPSTREAM_URL} (read-only mirror) for the pinned commit"
  git clone --quiet --bare "${UPSTREAM_URL}" "${UPSTREAM_GIT}"
fi

# 1) Deterministic archive of the pinned upstream commit (read-only on the mirror).
log "archiving upstream ${UPSTREAM_URL} @ ${COMMIT} (read-only)"
git -C "${UPSTREAM_GIT}" archive --format=tar --prefix="${TOP}/" "${COMMIT}" | tar -x -C "${STAGE}"

# 2) Apply the Aithernet patch series IN ORDER (strip our From/Subject header above the diff).
log "applying patch series (${#PATCHES[@]} patches)"
for _patch in "${PATCHES[@]}"; do
  log "  applying $(basename "${_patch}")"
  ( cd "${STAGE}/${TOP}" && sed -n '/^diff --git/,$p' "${_patch}" | patch -p1 --no-backup-if-mismatch -s )
done

# 3) Ship the GPL license + notice inside the source archive. LICENSE comes from the archived
#    upstream tree (the pinned commit), never a developer working directory.
[[ -f "${STAGE}/${TOP}/LICENSE" ]] || log "WARNING: upstream commit has no LICENSE file"
cp "${HERE}/NOTICE.md" "${STAGE}/${TOP}/AITHERNET-NOTICE.md"

# 4) Deterministic tarball (fixed mtime, sorted, numeric 0:0) -> reproducible sha256.
SRC="${OUT_DIR}/${NAME}-${VERSION}-src.tar.gz"
( cd "${STAGE}" && tar --sort=name --mtime='2026-01-01 00:00:00Z' \
    --owner=0 --group=0 --numeric-owner -czf "${SRC}" "${TOP}" )
SRC_SHA="$(sha256sum "${SRC}" | cut -d' ' -f1)"
echo "${SRC_SHA}  $(basename "${SRC}")" > "${SRC}.sha256"
# Deterministic digest of the ordered patch series (concatenated) for provenance.
PATCH_SHA="$(cat "${PATCHES[@]}" | sha256sum | cut -d' ' -f1)"
PATCH_SERIES_JSON="$(printf '"%s",' $(for p in "${PATCHES[@]}"; do basename "$p"; done) | sed 's/,$//')"

# 5) Sign the source archive with a component Ed25519 key (private stays under OUT_DIR/keys).
KEY="${SIGN_KEY:-${OUT_DIR}/keys/aithernet-component.key}"
if [[ ! -f "${KEY}" ]]; then
  log "generating component signing key (kept outside the repo, 0600)"
  ( umask 077; openssl genpkey -algorithm ed25519 -out "${KEY}" )
fi
openssl pkey -in "${KEY}" -pubout -out "${OUT_DIR}/aithernet-component.pub"
openssl pkeyutl -sign -inkey "${KEY}" -rawin -in "${SRC}" -out "${SRC}.sig"
[[ -f "${STAGE}/${TOP}/LICENSE" ]] && cp "${STAGE}/${TOP}/LICENSE" "${OUT_DIR}/LICENSE"
cp "${HERE}/NOTICE.md" "${OUT_DIR}/NOTICE.md"

# 6) Lock manifest (provenance + hashes + commands; no secrets).
PYMIN="$(sed -n 's/^python_min = "\(.*\)"/\1/p' "${HERE}/component.toml" | head -1)"
GRMIN="$(sed -n 's/^gnuradio_min = "\(.*\)"/\1/p' "${HERE}/component.toml" | head -1)"
cat > "${OUT_DIR}/lock.json" <<JSON
{
  "name": "${NAME}",
  "version": "${VERSION}",
  "license": "GPL-3.0-only",
  "upstream_url": "${UPSTREAM_URL}",
  "upstream_commit": "${COMMIT}",
  "patch_series": [${PATCH_SERIES_JSON}],
  "patch_sha256": "${PATCH_SHA}",
  "source_archive": "$(basename "${SRC}")",
  "source_sha256": "${SRC_SHA}",
  "python_min": "${PYMIN}",
  "gnuradio_min": "${GRMIN}",
  "build_command": "python3 -m venv --system-site-packages .venv && .venv/bin/pip install --no-input --disable-pip-version-check .",
  "runtime_command": ".venv/bin/python main.py",
  "signature": "$(basename "${SRC}").sig",
  "public_key": "aithernet-component.pub"
}
JSON

# 7) Install (extract the verified source) into the managed prefix — no checkout paths.
log "installing into managed prefix: ${PREFIX}"
rm -rf "${PREFIX}"; mkdir -p "${PREFIX_BASE}"
tar -xzf "${SRC}" -C "${STAGE}/install_extract" --one-top-level=x 2>/dev/null || { mkdir -p "${STAGE}/x"; tar -xzf "${SRC}" -C "${STAGE}/x"; }
mv "${STAGE}/x/${TOP}" "${PREFIX}"
cp "${OUT_DIR}/lock.json" "${PREFIX}/.aithernet-component-lock.json"

log "DONE  version=${VERSION}  sha256=${SRC_SHA}"
echo "OUT_DIR=${OUT_DIR}"
echo "PREFIX=${PREFIX}"
echo "SRC_SHA=${SRC_SHA}"
