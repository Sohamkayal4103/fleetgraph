#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build_deb.sh — reproducible, dependency-light Debian package builder for the
# Aithernet local node (Stage 14F packaging).
#
# What it does
#   * Stages a complete install tree under a build dir WITHOUT requiring root.
#   * Installs the application into /opt/aithernet via `pip install --target`
#     (a self-contained tree; see VENV note below for the alternative).
#   * Lays down a systemd unit, /etc/aithernet config templates, /var/lib state
#     dirs, version + uninstaller metadata, and documentation references.
#   * Builds the package with `dpkg-deb --build` (fakeroot not required: we set
#     ownership/permissions in the staged tree and let dpkg-deb record them).
#
# What it deliberately NEVER bundles (security boundary)
#   * Gemini / Codex / Google OAuth credentials or any provider tokens.
#   * Release-signing private keys.
#   * The operator's .env, consent records, or node identity PRIVATE keys.
#   * External RF repo checkouts (gr-mcp/, marconi, workspace/).
#
# GNU Radio / SDR dependencies are DETECTED on the build host (not faked) and,
# when present, declared only as Debian `Recommends:` — never hard `Depends:` —
# so the package installs on hosts without a system GNU Radio.
#
# Usage
#   VERSION=0.8.0-beta.1 ARCH=amd64 bash scripts/build_deb.sh
#   DRY_RUN=1 bash scripts/build_deb.sh      # print the staged layout, no dpkg-deb
#
# Env vars
#   VERSION   package version          (default 0.8.0-beta.1)
#   ARCH      Debian architecture      (default amd64)
#   BUILD_DIR scratch dir              (default ./build/deb)
#   DRY_RUN   1 = stage + print only, do not invoke dpkg-deb
# ---------------------------------------------------------------------------
set -euo pipefail

VERSION="${VERSION:-0.8.0-beta.1}"
ARCH="${ARCH:-amd64}"
DRY_RUN="${DRY_RUN:-0}"

# The packaged launcher runs the bundled lib under the TARGET system's `python3` (Ubuntu 24.04 =
# 3.12). `pip install --target` fetches binary wheels (pydantic_core, cffi, cryptography, ...) for
# the BUILD interpreter, so a build under a different CPython minor version ships ABI-incompatible
# extensions ("No module named 'pydantic_core._pydantic_core'") that import-fail on the target.
# Guard against it: refuse to build under a mismatched interpreter unless TARGET_PY is overridden.
TARGET_PY="${TARGET_PY:-3.12}"
_BUILD_PY="$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo unknown)"
if [ "${ALLOW_PY_MISMATCH:-0}" != "1" ] && [ "${_BUILD_PY}" != "${TARGET_PY}" ]; then
  echo "[build_deb] ERROR: build python3 is ${_BUILD_PY} but the target ABI is ${TARGET_PY}." >&2
  echo "[build_deb]   Binary wheels would be ABI-incompatible on the target (Ubuntu ${TARGET_PY})." >&2
  echo "[build_deb]   Run with the system python3 (deactivate any venv), or set TARGET_PY/" >&2
  echo "[build_deb]   ALLOW_PY_MISMATCH=1 to override deliberately." >&2
  exit 3
fi

# Resolve repo root from this script's location (works from any CWD).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

BUILD_DIR="${BUILD_DIR:-${REPO_ROOT}/build/deb}"
PKG_NAME="aithernet"
# Debian versions cannot contain '-' in the upstream part beyond the revision;
# normalise the beta marker (0.8.0-beta.1 -> 0.8.0~beta.1) so it sorts correctly.
DEB_VERSION="${VERSION//-/\~}"
STAGE="${BUILD_DIR}/${PKG_NAME}_${DEB_VERSION}_${ARCH}"

log()  { printf '[build_deb] %s\n' "$*" >&2; }
die()  { printf '[build_deb] ERROR: %s\n' "$*" >&2; exit 1; }

log "package=${PKG_NAME} version=${DEB_VERSION} arch=${ARCH} dry_run=${DRY_RUN}"
log "repo root: ${REPO_ROOT}"
log "build dir: ${BUILD_DIR}"

# --- Detect (do not fake) GNU Radio / SDR tooling on the build host ----------
# These influence only the Recommends: line; the package never hard-depends on
# them so it installs cleanly on hosts without a system GNU Radio.
RECOMMENDS_LIST=()
detect() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    log "detected: ${label}"
    return 0
  fi
  log "not present: ${label} (will not be added to Recommends)"
  return 1
}
detect "gnuradio (gnuradio-config-info)" command -v gnuradio-config-info \
  && RECOMMENDS_LIST+=("gnuradio")
detect "uhd (uhd_find_devices)" command -v uhd_find_devices \
  && RECOMMENDS_LIST+=("uhd-host")
detect "soapysdr (SoapySDRUtil)" command -v SoapySDRUtil \
  && RECOMMENDS_LIST+=("soapysdr-tools")
# Join recommends with ", " (Debian control syntax); may be empty.
RECOMMENDS=""
if [[ "${#RECOMMENDS_LIST[@]}" -gt 0 ]]; then
  RECOMMENDS="$(printf '%s, ' "${RECOMMENDS_LIST[@]}")"
  RECOMMENDS="${RECOMMENDS%, }"
fi

# --- Clean + create the staged tree -----------------------------------------
rm -rf "${STAGE}"
mkdir -p \
  "${STAGE}/DEBIAN" \
  "${STAGE}/opt/aithernet/lib" \
  "${STAGE}/usr/bin" \
  "${STAGE}/etc/aithernet" \
  "${STAGE}/lib/systemd/system" \
  "${STAGE}/usr/share/doc/aithernet" \
  "${STAGE}/var/lib/aithernet"

# State subdirs matching the node state root layout:
#   <root>/{config,db,identity,run,logs,backups,artifacts}
for d in config db identity run logs backups artifacts; do
  mkdir -p "${STAGE}/var/lib/aithernet/${d}"
done

# --- Application payload into /opt/aithernet/lib -----------------------------
# Preferred: a relocatable target tree via `pip install --target`. If pip or a
# built wheel is unavailable (offline build hosts), we still stage the source
# packages so the layout is complete and inspectable.
APP_LIB="${STAGE}/opt/aithernet/lib"
stage_app() {
  # Try a wheel from dist/ first (reproducible), else fall back to the source tree.
  local wheel
  wheel="$(ls -1 "${REPO_ROOT}"/dist/aithernet-*.whl 2>/dev/null | head -n1 || true)"
  if [[ "${DRY_RUN}" != "1" ]] && command -v pip >/dev/null 2>&1 && [[ -n "${wheel}" ]]; then
    log "installing wheel into target tree: ${wheel}"
    pip install --quiet --no-compile --target "${APP_LIB}" "${wheel}"
    return
  fi
  if [[ "${DRY_RUN}" != "1" ]] && command -v pip >/dev/null 2>&1; then
    log "no wheel in dist/; building target tree from source via pip"
    pip install --quiet --no-compile --target "${APP_LIB}" "${REPO_ROOT}" || {
      log "pip source install failed; falling back to copying source packages"
      cp -a "${REPO_ROOT}/src/aithernet" "${APP_LIB}/"
      cp -a "${REPO_ROOT}/services" "${APP_LIB}/"
    }
    return
  fi
  # DRY_RUN or no pip: stage source packages so the layout is representative.
  log "staging source packages (dry-run or pip unavailable)"
  mkdir -p "${APP_LIB}"
  [[ -d "${REPO_ROOT}/src/aithernet" ]] && cp -a "${REPO_ROOT}/src/aithernet" "${APP_LIB}/" || true
  [[ -d "${REPO_ROOT}/services" ]]      && cp -a "${REPO_ROOT}/services" "${APP_LIB}/"      || true
}
stage_app

# pip records its install SOURCE in dist-info/direct_url.json; for a source install that is the
# build machine's checkout path (e.g. file:///home/<user>/…). It is never needed at runtime, so
# strip it to keep the shipped package free of developer paths.
find "${APP_LIB}" -name direct_url.json -delete 2>/dev/null || true
# pip also lays down dependency console scripts under lib/bin with a shebang pinned to the BUILD
# interpreter (e.g. #!/<build venv>/bin/python). The runtime never uses them — the launcher runs
# `python3 -m aithernet.cli` with lib/ on PYTHONPATH — so remove lib/bin entirely. This drops the
# build-interpreter path leak and the vestigial (non-PATH) console scripts in one step.
rm -rf "${APP_LIB}/bin"

# The packaged runtime launcher: runs the bundled CLI with the bundled lib on PYTHONPATH, from
# any working directory. It is the SINGLE runtime entrypoint — both the systemd unit and the
# /usr/bin/aithernet PATH wrapper below forward to it, so they always use the same installed
# runtime under /opt/aithernet. AITHERNET_HOME defaults to the launcher's own directory and may be
# overridden to relocate the runtime (used by the isolated package tests).
cat > "${STAGE}/opt/aithernet/bin-launcher" <<'LAUNCH'
#!/bin/sh
# Aithernet launcher: run the packaged CLI with the bundled lib on PYTHONPATH.
# Usage: bin-launcher <subcommand> [args...]
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
AITHERNET_HOME="${AITHERNET_HOME:-$HERE}"
export PYTHONPATH="${AITHERNET_HOME}/lib:${PYTHONPATH:-}"
exec python3 -m aithernet.cli "$@"
LAUNCH
chmod 0755 "${STAGE}/opt/aithernet/bin-launcher"

# The documented node CLI on the standard PATH. After `apt install ./aithernet_*.deb` the customer
# runs `aithernet ...` directly — no pipx, no PYTHONPATH, from any directory. It forwards to the
# packaged runtime under /opt/aithernet (the SAME runtime the systemd unit uses). Only the node CLI
# is exposed; the hosted control-plane (`aithernet-hosted`) is deliberately NOT placed on customer
# nodes. dpkg owns this file, so package removal deletes it automatically.
cat > "${STAGE}/usr/bin/aithernet" <<'WRAP'
#!/bin/sh
# Aithernet node CLI (installed on PATH by the .deb). Forwards to the packaged runtime under
# /opt/aithernet from any working directory; arguments and the child exit code pass through.
AITHERNET_HOME="${AITHERNET_HOME:-/opt/aithernet}"
exec "${AITHERNET_HOME}/bin-launcher" "$@"
WRAP
chmod 0755 "${STAGE}/usr/bin/aithernet"

# --- Sanitize: ensure no forbidden material slipped into the payload ---------
# Hard guard against accidentally bundling SECRETS / RUNTIME STATE / external
# checkouts. We target secret-bearing FILES (env files, keys, certs, token/
# credential/consent data) and external repo DIRECTORIES — never Python source
# modules (e.g. data/consent.py is legitimate code and must be kept).
# Secret/state file names (extensions + dotfiles), matched case-insensitively:
FORBIDDEN_FILE_GLOBS=(
  ".env" "*.env" "*.pem" "*.key" "*.pfx" "*.p12" "*.crt"
  "*.secret" "*.secrets" "*credentials*.json" "*token*.json"
  "*oauth*.json" "*consent*.json" "*identity*.key" "*signing*.key"
)
# External repo / runtime-state directories that must never be packaged:
FORBIDDEN_DIRS=( "gr-mcp" "marconi" "workspace" ".aithernet_state" )

for g in "${FORBIDDEN_FILE_GLOBS[@]}"; do
  while IFS= read -r hit; do
    [[ -z "${hit}" ]] && continue
    case "${hit}" in
      *"/etc/aithernet/aithernet.env.template") continue ;;  # our own template
    esac
    log "sanitize: removing forbidden file from payload: ${hit#${STAGE}}"
    rm -f "${hit}"
  done < <(find "${STAGE}/opt" -type f -iname "${g}" 2>/dev/null || true)
done
for d in "${FORBIDDEN_DIRS[@]}"; do
  while IFS= read -r hit; do
    [[ -z "${hit}" ]] && continue
    log "sanitize: removing forbidden directory from payload: ${hit#${STAGE}}"
    rm -rf "${hit}"
  done < <(find "${STAGE}/opt" -type d -name "${d}" 2>/dev/null || true)
done

# --- Config templates under /etc/aithernet ----------------------------------
# node.yaml: copy the repo template if present, else write a minimal stub.
if [[ -f "${REPO_ROOT}/configs/node.yaml" ]]; then
  cp "${REPO_ROOT}/configs/node.yaml" "${STAGE}/etc/aithernet/node.yaml.template"
else
  cat > "${STAGE}/etc/aithernet/node.yaml.template" <<'YAML'
# Aithernet node configuration template.
# Copy to /etc/aithernet/node.yaml and edit before first start.
node_state_root: /var/lib/aithernet
YAML
fi

# EnvironmentFile template — values are placeholders; the operator fills these
# in OUTSIDE the package (this file is a template only, never a real secret).
cat > "${STAGE}/etc/aithernet/aithernet.env.template" <<'ENV'
# Aithernet node environment — copy to /etc/aithernet/aithernet.env (chmod 0640,
# owner root:aithernet) and fill in. NEVER commit a populated copy.
# Provider credentials are supplied by the operator; none are shipped.
AITHERNET_CONFIG=/etc/aithernet/node.yaml
# AITHERNET_API_KEY=
# GEMINI_API_KEY=
ENV
chmod 0640 "${STAGE}/etc/aithernet/aithernet.env.template"

# --- systemd unit ------------------------------------------------------------
if [[ -f "${REPO_ROOT}/deploy/systemd/aithernet-node.service" ]]; then
  cp "${REPO_ROOT}/deploy/systemd/aithernet-node.service" \
     "${STAGE}/lib/systemd/system/aithernet-node.service"
elif [[ -f "${REPO_ROOT}/deploy/systemd/aithernet.service" ]]; then
  cp "${REPO_ROOT}/deploy/systemd/aithernet.service" \
     "${STAGE}/lib/systemd/system/aithernet.service"
fi

# --- version + uninstaller metadata -----------------------------------------
cat > "${STAGE}/opt/aithernet/VERSION" <<EOF
${VERSION}
EOF

# A self-contained uninstaller metadata file enumerating what the package owns.
cat > "${STAGE}/opt/aithernet/uninstall-manifest.txt" <<'EOF'
# Files & directories owned by the aithernet package.
# State under /var/lib/aithernet and config under /etc/aithernet are PRESERVED
# on package removal (purge removes config; state is always kept for safety).
/opt/aithernet
/usr/bin/aithernet
/lib/systemd/system/aithernet-node.service
/etc/aithernet/node.yaml.template
/etc/aithernet/aithernet.env.template
EOF

# --- documentation references ------------------------------------------------
for doc in packaging/README.md packaging/release_archive.md deploy/README.md; do
  if [[ -f "${REPO_ROOT}/${doc}" ]]; then
    cp "${REPO_ROOT}/${doc}" "${STAGE}/usr/share/doc/aithernet/$(basename "${doc}")"
  fi
done
cat > "${STAGE}/usr/share/doc/aithernet/README.Debian" <<EOF
Aithernet ${VERSION}

Installed layout:
  /opt/aithernet           application payload (pip --target tree) + launcher
  /etc/aithernet           config + env TEMPLATES (copy and edit before start)
  /var/lib/aithernet       node state root {config,db,identity,run,logs,backups,artifacts}
  systemd unit             aithernet-node.service

Not bundled by design: provider OAuth/API credentials, release-signing keys,
operator .env, consent records, node identity private keys, external RF repos.

First run:
  cp /etc/aithernet/node.yaml.template     /etc/aithernet/node.yaml
  cp /etc/aithernet/aithernet.env.template /etc/aithernet/aithernet.env
  \$EDITOR /etc/aithernet/aithernet.env
  systemctl daemon-reload && systemctl enable --now aithernet-node
EOF

# --- DEBIAN control + maintainer scripts ------------------------------------
INSTALLED_SIZE_KB="$(du -sk "${STAGE}" 2>/dev/null | cut -f1 || echo 0)"
{
  echo "Package: ${PKG_NAME}"
  echo "Version: ${DEB_VERSION}"
  echo "Architecture: ${ARCH}"
  echo "Maintainer: Aithernet <packaging@example.invalid>"
  echo "Section: comm"
  echo "Priority: optional"
  echo "Installed-Size: ${INSTALLED_SIZE_KB}"
  # python3-venv: required so `aithernet components install rf-mcp` can build the component's
  # system-site-packages runtime venv (python3 -m venv + ensurepip) on a clean machine.
  echo "Depends: python3 (>= 3.11), python3-venv"
  if [[ -n "${RECOMMENDS}" ]]; then
    echo "Recommends: ${RECOMMENDS}"
  fi
  echo "Description: Autonomous SDR node runtime (Aithernet local node)"
  echo " Installs the Aithernet local node runtime, a systemd unit, and config"
  echo " templates. GNU Radio / SDR tooling is optional (Recommends), detected"
  echo " at build time; provider credentials and signing keys are never bundled."
} > "${STAGE}/DEBIAN/control"

# Create a dedicated service user and prepare the state root on install.
cat > "${STAGE}/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
case "$1" in
  configure)
    if ! getent group aithernet >/dev/null 2>&1; then
      addgroup --system aithernet || true
    fi
    if ! getent passwd aithernet >/dev/null 2>&1; then
      adduser --system --ingroup aithernet --home /var/lib/aithernet \
              --no-create-home --disabled-login aithernet || true
    fi
    chown -R aithernet:aithernet /var/lib/aithernet || true
    chmod 0750 /var/lib/aithernet || true
    chmod 0700 /var/lib/aithernet/identity || true
    if command -v systemctl >/dev/null 2>&1; then
      systemctl daemon-reload || true
    fi
    ;;
esac
exit 0
POSTINST
chmod 0755 "${STAGE}/DEBIAN/postinst"

cat > "${STAGE}/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e
if command -v systemctl >/dev/null 2>&1; then
  systemctl stop aithernet-node.service 2>/dev/null || true
  systemctl disable aithernet-node.service 2>/dev/null || true
fi
exit 0
PRERM
chmod 0755 "${STAGE}/DEBIAN/prerm"

cat > "${STAGE}/DEBIAN/conffiles" <<'CONFFILES'
/etc/aithernet/node.yaml.template
/etc/aithernet/aithernet.env.template
CONFFILES

# Permissions: env template tightened; identity dir 0700.
chmod 0700 "${STAGE}/var/lib/aithernet/identity"

# --- Print layout / build ----------------------------------------------------
print_layout() {
  log "staged layout under ${STAGE}:"
  if command -v find >/dev/null 2>&1; then
    ( cd "${STAGE}" && find . -maxdepth 4 -print | LC_ALL=C sort | sed 's/^/    /' )
  fi
  log "control file:"
  sed 's/^/    /' "${STAGE}/DEBIAN/control" >&2
}

if [[ "${DRY_RUN}" == "1" ]]; then
  print_layout
  log "DRY_RUN=1: skipping dpkg-deb. Output would be: ${STAGE}.deb"
  exit 0
fi

command -v dpkg-deb >/dev/null 2>&1 || die "dpkg-deb not found (install dpkg-dev)"
print_layout
log "building package with dpkg-deb..."
dpkg-deb --root-owner-group --build "${STAGE}" "${STAGE}.deb"
log "built: ${STAGE}.deb"
