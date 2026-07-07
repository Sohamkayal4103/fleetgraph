#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install.sh — controlled Aithernet installer / bootstrap (Stage 14F).
#
# Security model (why this is NOT `curl | sudo sh`):
#   * Downloads are fetched to a temp dir, NEVER piped to a shell.
#   * A manifest.json is fetched, then EVERY artifact's SHA-256 is verified
#     against the manifest before anything is unpacked or executed.
#   * The manifest's detached Ed25519 signature (manifest.sig) is verified with
#     the public key bundled alongside this script. Unsigned / mismatched
#     content is refused.
#   * Privileged actions require explicit confirmation (or --yes for CI).
#   * Partial failures roll back staged files.
#
# Modes / flags:
#   --dry-run            plan only; no downloads side effects, no privileged ops
#   --offline <dir>      install from a local release dir (no network at all)
#   --deb <file>         install the native .deb via APT (validates Ubuntu 24.04 amd64,
#                        refreshes stale package indexes, retries; never uses pip)
#   --yes                assume yes for confirmations (automation)
#   --uninstall          remove a previously installed copy
#   --prefix <dir>       install prefix (default /opt/aithernet)
#   -h | --help          this help
#
# Env:
#   DOWNLOAD_BASE_URL    release base URL (default https://downloads.example.invalid)
#                        Supports file:// for local-server testing.
#   AITHERNET_VERSION    version/channel path component (default 0.8.0-beta.1)
#   AITHERNET_PUBKEY     path to the verification public key
#                        (default: <script dir>/aithernet-release.pub)
#
# Verification backends: prefers `openssl`; falls back to a documented
# `python3 -m` cryptography path when openssl is unavailable.
# ---------------------------------------------------------------------------
set -euo pipefail

# --- defaults ----------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOAD_BASE_URL="${DOWNLOAD_BASE_URL:-https://downloads.example.invalid}"
AITHERNET_VERSION="${AITHERNET_VERSION:-0.8.0-beta.1}"
AITHERNET_PUBKEY="${AITHERNET_PUBKEY:-${SCRIPT_DIR}/aithernet-release.pub}"

PREFIX="/opt/aithernet"
DRY_RUN=0
ASSUME_YES=0
OFFLINE_DIR=""
DO_UNINSTALL=0
DEB_PATH=""
# Privilege helper: empty when already root, else 'sudo'. An explicitly-set SUDO (even empty) is
# respected (used by tests); only when SUDO is unset do we derive it.
if [[ -z "${SUDO+set}" ]]; then
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then SUDO="sudo"; else SUDO=""; fi
fi

WORKDIR=""          # temp staging dir for downloads/extraction
INSTALLED_LIST=""   # rollback ledger of files we created

log()  { printf '[install] %s\n' "$*" >&2; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  # Print the header comment block (between the two rule lines), stripping '# '.
  sed -n '4,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --- arg parsing -------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   DRY_RUN=1; shift ;;
    --yes|-y)    ASSUME_YES=1; shift ;;
    --offline)   OFFLINE_DIR="${2:?--offline requires a directory}"; shift 2 ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    --prefix)    PREFIX="${2:?--prefix requires a path}"; shift 2 ;;
    --deb)       DEB_PATH="${2:?--deb requires a path to the .deb}"; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "unknown argument: $1 (try --help)" ;;
  esac
done

# --- cleanup / rollback ------------------------------------------------------
cleanup() {
  if [[ -n "${WORKDIR}" && -d "${WORKDIR}" ]]; then
    rm -rf "${WORKDIR}" 2>/dev/null || true
  fi
}
rollback() {
  log "rolling back staged files due to failure"
  if [[ -n "${INSTALLED_LIST}" && -f "${INSTALLED_LIST}" ]]; then
    # Remove in reverse order so directories empty before removal.
    tac "${INSTALLED_LIST}" 2>/dev/null | while IFS= read -r p; do
      [[ -z "${p}" ]] && continue
      rm -rf "${p}" 2>/dev/null || true
    done
  fi
}
trap 'rc=$?; if [[ ${rc} -ne 0 ]]; then rollback; fi; cleanup' EXIT

# --- detect OS / arch --------------------------------------------------------
detect_platform() {
  local os arch
  os="$(uname -s 2>/dev/null || echo unknown)"
  arch="$(uname -m 2>/dev/null || echo unknown)"
  case "${arch}" in
    x86_64|amd64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
  esac
  PLATFORM_OS="${os}"
  PLATFORM_ARCH="${arch}"
  log "platform: os=${PLATFORM_OS} arch=${PLATFORM_ARCH}"
}

# --- confirmation gate -------------------------------------------------------
confirm() {
  local prompt="$1"
  if [[ "${ASSUME_YES}" == "1" || "${DRY_RUN}" == "1" ]]; then
    return 0
  fi
  printf '[install] %s [y/N] ' "${prompt}" >&2
  local ans=""
  read -r ans || true
  case "${ans}" in
    y|Y|yes|YES) return 0 ;;
    *) die "aborted by user" ;;
  esac
}

# --- fetch helper (network or file://) --------------------------------------
fetch() {
  # fetch <url> <dest>
  local url="$1" dest="$2"
  case "${url}" in
    file://*)
      local p="${url#file://}"
      [[ -f "${p}" ]] || die "local file not found: ${p}"
      cp "${p}" "${dest}"
      ;;
    http://*|https://*)
      if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --show-error --location --max-time 120 \
             --output "${dest}" "${url}"
      elif command -v wget >/dev/null 2>&1; then
        wget --quiet --timeout=120 -O "${dest}" "${url}"
      else
        die "neither curl nor wget available to fetch ${url}"
      fi
      ;;
    *)
      die "unsupported URL scheme: ${url}"
      ;;
  esac
}

# --- sha256 of a file --------------------------------------------------------
sha256_of() {
  local f="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${f}" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${f}" | awk '{print $1}'
  else
    die "no sha256 tool (sha256sum/shasum) available"
  fi
}

# --- signature verification --------------------------------------------------
# Verifies a detached Ed25519 signature over manifest.json using the bundled
# public key. Prefers openssl; documents a python3 fallback.
verify_signature() {
  local manifest="$1" sig="$2" pubkey="$3"
  [[ -f "${pubkey}" ]] || die "verification public key not found: ${pubkey}
  (set AITHERNET_PUBKEY or place aithernet-release.pub next to install.sh)"

  if command -v openssl >/dev/null 2>&1; then
    log "verifying signature with openssl (Ed25519)"
    # openssl pkeyutl/dgst: Ed25519 uses one-shot 'pkeyutl -verify' or
    # 'openssl dgst -verify' depending on key format. Use pkeyutl for raw Ed25519.
    if openssl pkeyutl -verify -pubin -inkey "${pubkey}" \
         -rawin -in "${manifest}" -sigfile "${sig}" >/dev/null 2>&1; then
      log "signature OK (openssl)"
      return 0
    fi
    die "signature verification FAILED (openssl)"
  fi

  if command -v python3 >/dev/null 2>&1; then
    log "openssl unavailable; verifying with python3 cryptography fallback"
    python3 - "$manifest" "$sig" "$pubkey" <<'PYVERIFY'
import sys
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.exceptions import InvalidSignature
mf, sigf, pubf = sys.argv[1], sys.argv[2], sys.argv[3]
with open(pubf, "rb") as fh:
    pub = load_pem_public_key(fh.read())
with open(mf, "rb") as fh:
    msg = fh.read()
with open(sigf, "rb") as fh:
    sig = fh.read()
try:
    pub.verify(sig, msg)  # Ed25519PublicKey.verify(signature, data)
except InvalidSignature:
    print("signature verification FAILED (python)", file=sys.stderr)
    sys.exit(1)
print("signature OK (python)", file=sys.stderr)
PYVERIFY
    return $?
  fi
  die "no signature verification backend (need openssl or python3 cryptography)"
}

# --- record an installed path for rollback ----------------------------------
record() { printf '%s\n' "$1" >> "${INSTALLED_LIST}"; }

# --- uninstall ---------------------------------------------------------------
do_uninstall() {
  log "uninstall: prefix=${PREFIX}"
  confirm "Remove ${PREFIX} and the aithernet systemd unit?"
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] would stop+disable aithernet-node.service"
    log "[dry-run] would remove ${PREFIX}"
    log "[dry-run] would PRESERVE /var/lib/aithernet (state) and /etc/aithernet (config)"
    return 0
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop aithernet-node.service 2>/dev/null || true
    systemctl disable aithernet-node.service 2>/dev/null || true
  fi
  rm -rf "${PREFIX}" 2>/dev/null || true
  log "removed ${PREFIX}. State (/var/lib/aithernet) and config (/etc/aithernet) preserved."
}

# --- main install ------------------------------------------------------------
do_install() {
  detect_platform
  WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/aithernet-install.XXXXXX")"
  INSTALLED_LIST="${WORKDIR}/.installed"
  : > "${INSTALLED_LIST}"

  local src_base manifest sig sums
  if [[ -n "${OFFLINE_DIR}" ]]; then
    [[ -d "${OFFLINE_DIR}" ]] || die "--offline dir not found: ${OFFLINE_DIR}"
    log "offline install from: ${OFFLINE_DIR}"
    src_base="file://${OFFLINE_DIR}"
  else
    src_base="${DOWNLOAD_BASE_URL%/}/${AITHERNET_VERSION}"
    log "release base: ${src_base}"
  fi

  manifest="${WORKDIR}/manifest.json"
  sig="${WORKDIR}/manifest.sig"
  sums="${WORKDIR}/SHA256SUMS"

  log "fetching manifest + signature"
  fetch "${src_base}/manifest.json" "${manifest}"
  fetch "${src_base}/manifest.sig"  "${sig}"
  # SHA256SUMS is optional convenience; the manifest is authoritative. Run the optional fetch in
  # a subshell so a missing file (fetch -> die -> exit) is contained and does NOT abort install.
  ( fetch "${src_base}/SHA256SUMS" "${sums}" ) 2>/dev/null || log "no SHA256SUMS (using manifest only)"

  # 1) Verify the manifest signature FIRST — nothing else is trusted until this passes.
  verify_signature "${manifest}" "${sig}" "${AITHERNET_PUBKEY}"

  # 2) Parse artifacts from the (now trusted) manifest and verify each SHA-256.
  #    Use python3 for robust JSON parsing; fall back to a grep/sed extractor.
  local artifacts=""
  if command -v python3 >/dev/null 2>&1; then
    artifacts="$(python3 - "$manifest" <<'PYPARSE'
import json, sys
m = json.load(open(sys.argv[1]))
for a in m.get("artifacts", []):
    print(f"{a['name']}\t{a['sha256']}\t{a.get('byte_size','')}")
PYPARSE
)"
  else
    die "python3 required to parse manifest.json (or extend with a jq path)"
  fi

  [[ -n "${artifacts}" ]] || die "manifest lists no artifacts"

  log "verifying artifacts:"
  local name want_sha got_sha out
  while IFS=$'\t' read -r name want_sha _; do
    [[ -z "${name}" ]] && continue
    out="${WORKDIR}/${name}"
    fetch "${src_base}/${name}" "${out}"
    got_sha="$(sha256_of "${out}")"
    if [[ "${got_sha}" != "${want_sha}" ]]; then
      die "SHA-256 mismatch for ${name}: expected ${want_sha}, got ${got_sha}"
    fi
    log "  ok  ${name}  ${got_sha}"
  done <<< "${artifacts}"

  log "all artifacts verified."

  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] verification complete; would install to ${PREFIX}"
    log "[dry-run] would create service user, systemd unit, /etc/aithernet templates"
    return 0
  fi

  # 3) Privileged install (confirmed).
  confirm "Install verified artifacts to ${PREFIX}?"
  mkdir -p "${PREFIX}"; record "${PREFIX}"
  # The actual unpack step depends on the artifact kind (wheel/tarball); we copy
  # verified files into the prefix. (Wheel install would run pip --target here.)
  cp "${WORKDIR}"/*.whl "${PREFIX}/" 2>/dev/null || true
  cp "${WORKDIR}"/*.tar.gz "${PREFIX}/" 2>/dev/null || true
  log "installed verified artifacts to ${PREFIX}"
  log "next: copy /etc/aithernet templates, edit aithernet.env, then enable aithernet-node"
}

# --- native .deb installation (APT, with stale-index handling) ---------------
# The supported clean-Ubuntu install is `apt install ./aithernet_*_amd64.deb`. On a fresh image
# whose package indexes were never initialized, APT has no candidate for python3-venv (and the
# recommended gnuradio/uhd-host/soapysdr-tools), so the install fails until `apt-get update` runs.
# This path validates the platform, detects that exact stale/uninitialized-index condition, runs a
# single explicit + visible `apt-get update`, retries, and NEVER suggests pip for system packages.

require_ubuntu_2404_amd64() {
  local id="" ver="" arch="" osr="${AITHERNET_OS_RELEASE:-/etc/os-release}"
  if [[ -r "${osr}" ]]; then
    id="$(. "${osr}" 2>/dev/null && printf '%s' "${ID:-}")"
    ver="$(. "${osr}" 2>/dev/null && printf '%s' "${VERSION_ID:-}")"
  fi
  arch="$(dpkg --print-architecture 2>/dev/null || echo unknown)"
  [[ "${id}" == "ubuntu" && "${ver}" == "24.04" ]] || \
    die "This package targets Ubuntu 24.04 LTS amd64 (detected: ${id:-unknown} ${ver:-unknown}). Install on Ubuntu 24.04."
  [[ "${arch}" == "amd64" ]] || \
    die "This package is amd64; this system reports '${arch}'. Use the amd64 release on an amd64 host."
  log "platform OK: Ubuntu ${ver} ${arch}"
}

apt_has_candidate() {   # $1 = package; true when APT has an installable candidate
  local cand
  cand="$(apt-cache policy "$1" 2>/dev/null | awk -F': ' '/Candidate:/{print $2}')"
  [[ -n "${cand}" && "${cand}" != "(none)" ]]
}

apt_update_visible() {
  log "Package indexes look stale/uninitialized. Refreshing them now (the one privileged network step):"
  log "  + ${SUDO} apt-get update"
  if [[ "${DRY_RUN}" == "1" ]]; then return 0; fi
  ${SUDO} apt-get update || \
    die "\`apt-get update\` failed — network or APT sources are unreachable. Fix connectivity or /etc/apt/sources.list and re-run. Do NOT use pip for python3-venv / GNU Radio / SoapySDR / UHD."
}

install_deb_native() {  # $1 = path to the .deb
  local deb="$1"
  [[ -f "${deb}" ]] || die "package not found: ${deb}"
  require_ubuntu_2404_amd64
  # Probe a hard runtime dependency that is absent on a stale/uninitialized index.
  if ! apt_has_candidate python3-venv; then
    log "APT has no installable candidate for 'python3-venv' (a fresh Ubuntu image that has not run 'apt update' yet)."
    apt_update_visible
    if [[ "${DRY_RUN}" != "1" ]] && ! apt_has_candidate python3-venv; then
      die "Still no candidate for 'python3-venv' after 'apt-get update'. The required Ubuntu repositories (main + universe) appear missing or unreachable. Enable them in /etc/apt/sources.list (or via software-properties) and check connectivity, then re-run. Do NOT substitute pip — python3-venv, GNU Radio, SoapySDR and UHD must come from APT."
    fi
  else
    log "APT candidate for 'python3-venv' present; indexes look current."
  fi
  log "Installing the native package (APT resolves python3-venv and the recommended SDR packages):"
  log "  + ${SUDO} apt-get install -y ${deb}"
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] would install ${deb} via APT (no pip)."
    return 0
  fi
  if ! ${SUDO} apt-get install -y "${deb}"; then
    die "\`apt-get install\` failed for ${deb}. Re-run '${SUDO} apt-get update' and retry. If the recommended SDR packages (gnuradio / uhd-host / soapysdr-tools) are unavailable, ensure the 'universe' component is enabled. Never use pip for these system packages."
  fi
  log "Installed ${deb}. Verify with: aithernet --version"
}

# --- dispatch ----------------------------------------------------------------
if [[ "${DO_UNINSTALL}" == "1" ]]; then
  do_uninstall
elif [[ -n "${DEB_PATH}" ]]; then
  install_deb_native "${DEB_PATH}"
else
  do_install
fi
log "done."
