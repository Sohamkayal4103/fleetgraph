#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# components/catgpt-gateway/build.sh
#
# Build the Aithernet-managed CatGPT Gateway runtime as a signed release image artifact so a fresh
# client never clones, builds, or pulls from a public repo. Produces a compressed `docker save` tar
# and a lock.json (provenance). Idempotent: reuses an existing, non-empty tar.
#
# Image source (first that applies):
#   1. an already-loaded local image  aithernet/catgpt-gateway:<VERSION>  or  catgpt-gateway:latest
#   2. a BUILD from  CATGPT_SOURCE_DIR  (a checkout of the pinned upstream commit; must have Dockerfile)
#
# Env:
#   VERSION            component version (default 1.0.0-beta.7)
#   OUT_DIR            output dir (default ~/.local/state/aithernet/catgpt-image-build)
#   CATGPT_SOURCE_DIR  pinned upstream checkout to build from, if no local image exists
#   ZSTD_LEVEL         zstd compression level (default 19)
#
# Flags:
#   --print-tar        print ONLY the resulting tar path on stdout (for scripts/build_release.sh)
#
# Never bakes browser cookies / sessions / web credentials into the image — those are runtime env
# only, provided by `aithernet catgpt setup` (0600 gateway.env), not build-time.
# -----------------------------------------------------------------------------
set -euo pipefail

VERSION="${VERSION:-1.0.0-beta.7}"
OUT_DIR="${OUT_DIR:-${HOME}/.local/state/aithernet/catgpt-image-build}"
ZSTD_LEVEL="${ZSTD_LEVEL:-19}"
UPSTREAM_REPO="https://github.com/GautamVhavle/CatGPT-Gateway"
UPSTREAM_COMMIT="79a1b69d429fa9951d796289b60470fb595cbb42"
UPSTREAM_LICENSE="MIT"
IMAGE_TAG="aithernet/catgpt-gateway:${VERSION}"
TAR_NAME="catgpt-gateway-${VERSION}-linux-amd64.docker.tar.zst"

PRINT_TAR=0
[ "${1:-}" = "--print-tar" ] && PRINT_TAR=1

log() { [ "${PRINT_TAR}" = "1" ] && echo "$*" >&2 || echo "[catgpt-image] $*" >&2; }

command -v docker >/dev/null || { log "ERROR: docker not found"; exit 3; }
command -v zstd   >/dev/null || { log "ERROR: zstd not found (apt install zstd)"; exit 3; }

mkdir -p "${OUT_DIR}"
TAR_PATH="${OUT_DIR}/${TAR_NAME}"

# Reuse an existing, non-empty tar (idempotent — the save is expensive).
if [ -s "${TAR_PATH}" ]; then
  log "reusing existing ${TAR_NAME} ($(stat -c%s "${TAR_PATH}") bytes)"
else
  # Ensure the versioned image is loaded locally.
  if ! docker image inspect "${IMAGE_TAG}" >/dev/null 2>&1; then
    if docker image inspect catgpt-gateway:latest >/dev/null 2>&1; then
      log "tagging existing catgpt-gateway:latest -> ${IMAGE_TAG}"
      docker tag catgpt-gateway:latest "${IMAGE_TAG}"
    elif [ -n "${CATGPT_SOURCE_DIR:-}" ] && [ -f "${CATGPT_SOURCE_DIR}/Dockerfile" ]; then
      log "building ${IMAGE_TAG} from ${CATGPT_SOURCE_DIR} (pinned ${UPSTREAM_COMMIT})"
      docker build -t "${IMAGE_TAG}" -f "${CATGPT_SOURCE_DIR}/Dockerfile" "${CATGPT_SOURCE_DIR}" >&2
    else
      log "ERROR: no local catgpt-gateway image and no CATGPT_SOURCE_DIR with a Dockerfile."
      log "       Provide CATGPT_SOURCE_DIR=<pinned CatGPT-Gateway checkout> and re-run."
      exit 4
    fi
  fi
  log "saving + compressing ${IMAGE_TAG} -> ${TAR_NAME} (zstd -${ZSTD_LEVEL}, may take a few minutes)"
  docker save "${IMAGE_TAG}" | zstd -T0 "-${ZSTD_LEVEL}" -q -o "${TAR_PATH}"
fi

SHA="$(sha256sum "${TAR_PATH}" | cut -d' ' -f1)"
SIZE="$(stat -c%s "${TAR_PATH}")"

# Provenance lock (no secrets).
cat > "${OUT_DIR}/catgpt-gateway-${VERSION}.lock.json" <<JSON
{
  "component": "catgpt-gateway",
  "version": "${VERSION}",
  "image_tag": "${IMAGE_TAG}",
  "artifact": "${TAR_NAME}",
  "artifact_sha256": "${SHA}",
  "artifact_byte_size": ${SIZE},
  "upstream_repo": "${UPSTREAM_REPO}",
  "upstream_commit": "${UPSTREAM_COMMIT}",
  "upstream_license": "${UPSTREAM_LICENSE}",
  "platform": "linux/amd64",
  "contains_web_credentials": false
}
JSON

log "DONE  ${TAR_NAME}  ${SIZE} bytes  sha256:${SHA}"
if [ "${PRINT_TAR}" = "1" ]; then
  echo "${TAR_PATH}"
fi
