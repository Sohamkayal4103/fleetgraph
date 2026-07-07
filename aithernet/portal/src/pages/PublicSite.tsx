import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { useRouter, Link } from '../routes/router'
import { Section, StatCard, Badge, DataView, CopyButton, LegalReference } from '../components/ui'
import { api } from '../api/client'
import type { Diagnostics, PolicyVersion, Readiness, Release } from '../api/types'

// Public, factual product website for the Aithernet hosted portal. Every claim here is grounded
// in tracked repository files (packaging/, scripts/, deploy/, src/aithernet/hardware/, tests/).
// It NEVER runs SDR/mission workloads and makes no unsupported claims: no fabricated uptime,
// SLAs, testimonials, addresses, phone numbers, staffed hours, or general availability. Aithernet
// is described as invitation-only, experimental, early-access software. The product brand name
// is defined once in the BRAND constant below.

const BRAND = 'Aithernet'
const NODE_VERSION = '1.0.0-beta.3'

// Render long digests/hashes so they wrap instead of causing horizontal overflow.
const wrapAnywhere: React.CSSProperties = { overflowWrap: 'anywhere', wordBreak: 'break-all' }
const codeBlock: React.CSSProperties = {
  whiteSpace: 'pre-wrap',
  overflowWrap: 'anywhere',
  background: 'var(--bg-deep)',
  border: '1px solid #2a2f52',
  color: '#e7ebf6',
  borderRadius: 'var(--radius-sm)',
  padding: 12,
  margin: '8px 0',
  fontFamily: 'var(--mono)',
}

function Code({ children }: { children: string }) {
  return (
    <pre style={codeBlock}>
      <code>{children}</code>
    </pre>
  )
}

// Customer-safe message for an authorization failure. 401 = not signed in; 403 = signed in but the
// account isn't authorized yet (pending approval / required-policy / no tenant) — NOT another
// "sign in" prompt. Anything else surfaces the bounded server message.
function authErrorMessage(e: { status?: number; message?: string }, what: string): string {
  if (e?.status === 401) return `Sign in to view ${what}.`
  if (e?.status === 403)
    return `Your account isn't authorized to view ${what} yet. Aithernet is invitation-only — `
      + `your early-access request may be pending approval, or you may need to accept the required `
      + `terms in your account.`
  return e?.message ?? `Could not load ${what}.`
}

function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return `${n} B`
  if (n < 1024) return `${n} B`
  const units = ['KiB', 'MiB', 'GiB']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(1)} ${units[i]} (${n} bytes)`
}

// ---- Overview --------------------------------------------------------------

function Overview() {
  return (
    <>
      <Section title={`${BRAND} — autonomous SDR node + hosted control plane`}>
        <p>
          Aithernet is an autonomous software-defined-radio (SDR) node runtime paired with a
          hosted early-access control plane. Each <strong>node</strong> is a self-contained
          hardware/software unit that runs missions locally: mission-level reasoning, an
          implementation/execution worker, GNU Radio access through an MCP integration, durable
          local state, and user-facing surfaces. The node is <strong>local-first</strong> — it
          continues executing missions on the host without an Internet connection.
        </p>
        <p>
          The hosted control plane (this site and the customer/admin portals) handles enrollment,
          policy acceptance, fleet visibility, signed releases, consent-controlled telemetry
          ingestion, and support. The portal itself never runs radio or mission workloads; those
          execute only on enrolled nodes.
        </p>
        <p>
          Aithernet is <strong>experimental, early-access software available by invitation only</strong>.
          The current node version is {NODE_VERSION}.
        </p>
      </Section>
      <Section title="What a node provides">
        <ul>
          <li>Autonomous mission runtime with a coordinator agent and an execution worker.</li>
          <li>GNU Radio integration over a managed MCP session for SDR work.</li>
          <li>Local CLI (<code>aithernet</code>), HTTP API, and a browser dashboard on the host.</li>
          <li>Durable local state under a node state root (config, db, identity, logs, artifacts).</li>
          <li>Signed releases and a verified installer/update path (Ed25519 + SHA-256).</li>
          <li>Consent-controlled telemetry that is off by default and gated by the operator.</li>
        </ul>
      </Section>
      <Section title="Explore">
        <div className="stat-row" style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
          <StatCard label="Early access" value="Invitation-only" to="/early-access" />
          <StatCard label="Supported systems" value="Linux" to="/systems" />
          <StatCard label="Qualified SDR" value="ADALM-PLUTO" to="/hardware" />
          <StatCard label="Install" value="pipx / .deb / installer" to="/install" />
          <StatCard label="Downloads" value="Signed releases" to="/downloads" />
          <StatCard label="Service status" value="Live" to="/status" />
        </div>
      </Section>
    </>
  )
}

// ---- Early access ----------------------------------------------------------

function EarlyAccess() {
  return (
    <>
      <Section title="Early access">
        <p>
          Aithernet is in an early-access preview. It is <strong>invitation-only</strong>: access
          requires an invitation or enrollment code from a tenant administrator. The software is
          experimental and under active development.
        </p>
        <p>
          <Link to="/app/accept-invite">Accept an invitation</Link> ·{' '}
          <Link to="/login">Sign in</Link>
        </p>
      </Section>

      <Section title="Local-first by design">
        <p>
          A node runs missions locally and does not depend on the hosted control plane to operate.
          Mission execution continues without Internet connectivity. The hosted control plane adds
          enrollment, fleet visibility across your nodes, signed release distribution, and support
          — it does not run radio or mission workloads.
        </p>
      </Section>

      <Section title="Privacy and data consent">
        <ul>
          <li>
            Telemetry is <strong>consent-gated</strong>: data export from a node is off by default
            and must be enabled explicitly by the operator.
          </li>
          <li>
            Releases and the installer never bundle provider OAuth/API credentials, release-signing
            private keys, the operator&apos;s environment file, consent records, or node identity
            private keys.
          </li>
          <li>
            The control plane surfaces a bounded, non-sensitive node summary (state, version,
            hardware families, device count) — never raw telemetry, private keys, or secrets.
          </li>
        </ul>
      </Section>

      <Section title="Current validated hardware">
        <p>
          The only SDR with physical acceptance evidence in this preview is the{' '}
          <strong>ADALM-PLUTO (PlutoSDR)</strong>, qualified on real hardware through device
          identity, lease lifecycle, RX capture, and restart-recovery checks. See{' '}
          <Link to="/hardware">supported SDR hardware</Link> for the full status of each provider.
        </p>
      </Section>

      <Section title="Known validation limitations">
        <ul>
          <li>
            Multi-SDR (two-device) operation and long-soak endurance testing are{' '}
            <strong>not claimed complete</strong>. Field validation to date covers a single device.
          </li>
          <li>
            UHD/USRP discovery is implemented at find-level only and is{' '}
            <strong>not physically qualified</strong> (no USRP was attached during qualification).
          </li>
          <li>
            The Debian package and installer are validated for <code>amd64</code>; <code>arm64</code>{' '}
            is supported by design but not physically tested.
          </li>
        </ul>
      </Section>

      <Section title="How support works">
        <p>
          After enrollment, sign in to the customer portal to open a support case. In local staging
          environments, support delivery runs through the local development support workflow. See{' '}
          <Link to="/contact">Contact</Link> for details.
        </p>
      </Section>
    </>
  )
}

// ---- Supported systems -----------------------------------------------------

function SupportedSystems() {
  return (
    <>
      <Section title="Supported operating systems">
        <p>
          The Aithernet node targets <strong>Linux with SDR hardware access</strong>. A container
          alone is not sufficient for the local node — it needs host device access (USB SDR via
          udev/<code>plugdev</code>), a system GNU Radio / SoapySDR / UHD stack, and a persistent
          on-host state root. Install on the host via <code>pipx</code>, the Debian package, or the
          signed installer.
        </p>
      </Section>

      <Section title="Distribution and packaging support">
        <table>
          <caption className="sr-caption" style={{ textAlign: 'left', marginBottom: 8 }}>
            Installation targets and their validation status.
          </caption>
          <thead>
            <tr>
              <th scope="col">Target</th>
              <th scope="col">Status</th>
              <th scope="col">Notes</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Debian/Ubuntu <code>.deb</code> — amd64</td>
              <td>
                <Badge kind="success">Tested</Badge>
              </td>
              <td>Built and validated by the acceptance suite at the default version.</td>
            </tr>
            <tr>
              <td>Debian/Ubuntu <code>.deb</code> — arm64</td>
              <td>
                <Badge kind="info">Supported by design</Badge>
              </td>
              <td>
                The build accepts <code>ARCH=arm64</code>, but arm64 is not physically tested.
              </td>
            </tr>
            <tr>
              <td>Python <code>pipx</code> on Linux</td>
              <td>
                <Badge kind="success">Tested</Badge>
              </td>
              <td>
                Installs the <code>aithernet-{NODE_VERSION}</code> wheel into an isolated venv.
              </td>
            </tr>
            <tr>
              <td>Signed installer (<code>scripts/install.sh</code>)</td>
              <td>
                <Badge kind="info">Supported by design</Badge>
              </td>
              <td>Verifies the Ed25519 signature and per-artifact SHA-256 before unpacking.</td>
            </tr>
            <tr>
              <td>Windows / macOS</td>
              <td>
                <Badge kind="neutral">Not supported</Badge>
              </td>
              <td>The node targets Linux plus an SDR stack; other OSes are out of scope.</td>
            </tr>
          </tbody>
        </table>
        <p>
          The Debian package hard-depends only on <code>python3 (&gt;= 3.11)</code>. GNU Radio, UHD,
          and SoapySDR are detected at build time and declared as <code>Recommends:</code> only,
          never a hard dependency.
        </p>
      </Section>
    </>
  )
}

// ---- Supported SDR hardware ------------------------------------------------

function SupportedHardware() {
  return (
    <>
      <Section title="Supported SDR hardware">
        <p>
          Hardware status reflects what the node&apos;s discovery providers and qualification runs
          actually report — devices and capabilities are never inferred from a model name. The
          status of each path below is grounded in the node hardware registry and tests.
        </p>
        <table>
          <caption style={{ textAlign: 'left', marginBottom: 8 }}>
            SDR providers and devices by validation status.
          </caption>
          <thead>
            <tr>
              <th scope="col">Path</th>
              <th scope="col">Status</th>
              <th scope="col">Notes</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>ADALM-PLUTO (PlutoSDR) via SoapySDR</td>
              <td>
                <Badge kind="success">Qualified</Badge>
              </td>
              <td>
                Physically qualified on real hardware: device identity, RX lease lifecycle, RX
                capture, and restart-recovery.
              </td>
            </tr>
            <tr>
              <td>SoapySDR provider (generic Soapy devices)</td>
              <td>
                <Badge kind="info">Supported</Badge>
              </td>
              <td>
                Real discovery via <code>SoapySDRUtil --find</code> / <code>--probe</code> when the
                tool is present.
              </td>
            </tr>
            <tr>
              <td>USRP via UHD</td>
              <td>
                <Badge kind="info">Supported (find-level)</Badge>
              </td>
              <td>
                Discovery via <code>uhd_find_devices</code> is implemented, but UHD is{' '}
                <strong>not physically qualified</strong> (no USRP attached during qualification);
                capabilities stay at low confidence.
              </td>
            </tr>
            <tr>
              <td>Operator-declared devices (static / static-file providers)</td>
              <td>
                <Badge kind="info">Supported</Badge>
              </td>
              <td>Inline or JSON-file device descriptors supplied by the operator.</td>
            </tr>
            <tr>
              <td>Marconi RF backend</td>
              <td>
                <Badge kind="warning">Experimental</Badge>
              </td>
              <td>
                Available alongside the legacy GNU Radio backend as an experimental RF backend.
              </td>
            </tr>
            <tr>
              <td>Simulation / file-analysis paths</td>
              <td>
                <Badge kind="neutral">Simulation</Badge>
              </td>
              <td>
                Software-only paths for testing without attached hardware; not real-RF evidence.
              </td>
            </tr>
            <tr>
              <td>Unknown / unqualified devices</td>
              <td>
                <Badge kind="neutral">Unqualified</Badge>
              </td>
              <td>
                Discovered devices remain unqualified until a real qualification run records
                evidence.
              </td>
            </tr>
          </tbody>
        </table>
      </Section>

      <Section title="Qualification is evidence-based">
        <p>
          A node records a qualification classification only from a real run. The ranked stages run
          from <code>discovered</code> and <code>probed</code> through <code>rx_qualified</code>,{' '}
          <code>tx_lease_qualified</code>, <code>capture_qualified</code>,{' '}
          <code>survey_qualified</code>, and <code>disconnect_recovery_qualified</code>. Multi-SDR
          and long-soak evidence is <strong>not</strong> claimed complete in this preview.
        </p>
        <p>
          Inspect what a node reports with the node CLI: <code>aithernet hardware providers</code>,{' '}
          <code>aithernet hardware list</code>, <code>aithernet hardware show &lt;device&gt;</code>,
          and run qualification with <code>aithernet hardware qualify run &lt;device&gt;</code>.
        </p>
      </Section>
    </>
  )
}

// ---- Installation ----------------------------------------------------------

function Installation() {
  return (
    <>
      <Section title="Installation">
        <p>
          Install the Aithernet node on a Linux host. Three supported paths are below; all install
          the same {NODE_VERSION} runtime. Commands are exact; replace enrollment values with the
          ones from your invitation.
        </p>
      </Section>

      <Section title="Option A — pipx (isolated venv)">
        <p>Install the published wheel into an isolated environment:</p>
        <Code>{`pipx install aithernet-${NODE_VERSION}-py3-none-any.whl`}</Code>
        <p>
          This puts <code>aithernet</code> (node CLI) and <code>aithernet-hosted</code> (hosted
          control-plane CLI) on <code>PATH</code> in their own venv.
        </p>
      </Section>

      <Section title="Option B — Debian package (.deb)">
        <p>Build the package (no root required) and install it:</p>
        <Code>{`# Inspect the staged layout only (no dpkg-deb invoked):
DRY_RUN=1 bash scripts/build_deb.sh

# Build amd64 at the default version:
bash scripts/build_deb.sh

# Override version/arch:
VERSION=${NODE_VERSION} ARCH=arm64 bash scripts/build_deb.sh

# Install the built package:
sudo dpkg -i build/deb/aithernet_0.8.0~beta.1_amd64.deb`}</Code>
        <p>
          The package installs the application under <code>/opt/aithernet</code>, config templates
          under <code>/etc/aithernet</code>, the node state root under <code>/var/lib/aithernet</code>,
          and the <code>aithernet-node.service</code> systemd unit. The <code>postinst</code> step
          creates a dedicated <code>aithernet</code> service user and tightens permissions.
        </p>
      </Section>

      <Section title="Option C — signed installer">
        <p>
          The controlled installer fetches a release manifest, verifies its Ed25519 signature first,
          then verifies each artifact&apos;s SHA-256 before unpacking. It never pipes downloads to a
          shell.
        </p>
        <Code>{`# Plan only, no privileged actions:
bash scripts/install.sh --dry-run

# Install from a local release directory with no network:
bash scripts/install.sh --offline ./release-dir

# Uninstall (preserves /var/lib/aithernet state and /etc/aithernet config):
bash scripts/install.sh --uninstall`}</Code>
      </Section>

      <Section title="systemd unit">
        <p>Run the node as a hardened, unprivileged service:</p>
        <Code>{`cp /etc/aithernet/aithernet.env.template /etc/aithernet/aithernet.env   # edit
systemctl daemon-reload
systemctl enable --now aithernet-node`}</Code>
        <p>
          The unit runs as the dedicated <code>aithernet</code> user (never root), with{' '}
          <code>plugdev</code>/<code>dialout</code> for SDR device access, and gates startup on a
          preflight check.
        </p>
      </Section>

      <Section title="First run, setup, and enrollment">
        <Code>{`# Prepare local node state:
aithernet setup

# Enroll the node with the hosted control plane:
aithernet enroll --base-url <control-plane-url> --code <enrollment-code>`}</Code>
        <p>
          Once the node reports in, it appears in your fleet in the customer portal. The local
          dashboard and API are served on the host; see the node CLI help with{' '}
          <code>aithernet --help</code>.
        </p>
      </Section>

      <Section title="Offline install and uninstall">
        <p>
          For air-gapped or no-network hosts, use <code>scripts/install.sh --offline &lt;dir&gt;</code>{' '}
          against a local release directory (or a <code>file://</code> path). Uninstalling with{' '}
          <code>--uninstall</code> stops and disables the unit and removes <code>/opt/aithernet</code>,
          while preserving node state under <code>/var/lib/aithernet</code> and config under{' '}
          <code>/etc/aithernet</code>.
        </p>
      </Section>
    </>
  )
}

// ---- Documentation ---------------------------------------------------------

function Documentation() {
  const topics: { title: string; where: ReactNode }[] = [
    {
      title: 'Installation',
      where: (
        <>
          See <Link to="/install">Installation</Link> for the pipx, <code>.deb</code>, and signed
          installer commands.
        </>
      ),
    },
    {
      title: 'Node setup and first run',
      where: (
        <>
          Run <code>aithernet setup</code> then <code>aithernet --help</code> on the host.
        </>
      ),
    },
    {
      title: 'Account onboarding',
      where: (
        <>
          Accept an invitation at <Link to="/app/accept-invite">Accept invitation</Link>, then{' '}
          <Link to="/login">sign in</Link>.
        </>
      ),
    },
    {
      title: 'Enrollment',
      where: (
        <>
          Use <code>aithernet enroll --base-url &lt;url&gt; --code &lt;code&gt;</code>; see{' '}
          <Link to="/install">Installation</Link>.
        </>
      ),
    },
    {
      title: 'CLI reference',
      where: <>In the node CLI: <code>aithernet --help</code> (subcommands print their own help).</>,
    },
    {
      title: 'Dashboard',
      where: <>The browser dashboard is served by the node on the host over its local API.</>,
    },
    {
      title: 'SDR discovery and qualification',
      where: (
        <>
          See <Link to="/hardware">Supported SDR hardware</Link>, and{' '}
          <code>aithernet hardware providers | list | qualify run &lt;device&gt;</code>.
        </>
      ),
    },
    {
      title: 'Privacy and data controls',
      where: (
        <>
          See <Link to="/early-access">Early access</Link>; telemetry export is consent-gated and
          off by default.
        </>
      ),
    },
    {
      title: 'Backup and restore (hosted)',
      where: (
        <>
          Operator runbook: <code>deploy/production/README.md</code> (sections 11–12,{' '}
          <code>backup.sh</code> / <code>restore.sh</code>).
        </>
      ),
    },
    {
      title: 'Updates and rollback (hosted)',
      where: (
        <>
          Operator runbook: <code>deploy/production/README.md</code> (sections 13–14). Node releases
          are listed in <Link to="/downloads">Downloads</Link>.
        </>
      ),
    },
    {
      title: 'Packaging and release archive',
      where: (
        <>
          <code>packaging/README.md</code> and <code>packaging/release_archive.md</code> describe the
          build and signed-release format.
        </>
      ),
    },
    {
      title: 'Troubleshooting',
      where: (
        <>
          The systemd unit gates start on <code>aithernet node preflight</code>; inspect logs via the
          journal (<code>journalctl -u aithernet-node</code>).
        </>
      ),
    },
    {
      title: 'Support',
      where: (
        <>
          See <Link to="/contact">Contact</Link> — sign in and open a support case.
        </>
      ),
    },
    {
      title: 'Security model',
      where: (
        <>
          Releases are signed with Ed25519 and verified before unpack; see{' '}
          <Link to="/install">Installation</Link> and <code>packaging/release_archive.md</code>.
        </>
      ),
    },
  ]
  return (
    <Section title="Documentation">
      <p>
        This index points to where each topic lives — in this portal, in the node CLI, or in tracked
        operator documentation in the repository.
      </p>
      <dl>
        {topics.map((t) => (
          <div key={t.title} style={{ marginBottom: 10 }}>
            <dt style={{ fontWeight: 600 }}>{t.title}</dt>
            <dd style={{ margin: '2px 0 0', color: 'var(--muted)' }}>{t.where}</dd>
          </div>
        ))}
      </dl>
    </Section>
  )
}

// ---- Downloads -------------------------------------------------------------

function osArchSummary(release: Release): string {
  const pairs = new Set<string>()
  for (const a of release.artifacts) {
    const os = a.os_family && a.os_family !== '' ? a.os_family : 'any'
    const arch = a.architecture && a.architecture !== '' ? a.architecture : 'any'
    pairs.add(`${os}/${arch}`)
  }
  return pairs.size ? Array.from(pairs).sort().join(', ') : 'unspecified'
}

function Downloads() {
  const [releases, setReleases] = useState<Release[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    api
      .getReleases('early-access')
      .then((r) => active && setReleases(r))
      .catch((e: { status?: number; message?: string }) => {
        if (!active) return
        setError(authErrorMessage(e, 'available releases'))
      })
    return () => {
      active = false
    }
  }, [])

  return (
    <Section title="Downloads">
      <p>
        Node software is distributed as signed releases. Only <strong>published</strong>, non-revoked
        releases are downloadable. Verify the signature and SHA-256 before installing (the signed
        installer does this automatically).
      </p>
      <DataView
        data={releases}
        error={error}
        what="releases"
        isEmpty={(r) => r.filter((x) => x.status === 'published').length === 0}
        empty="No published releases are available yet."
      >
        {(all) => (
          <>
            {all
              .filter((r) => r.status === 'published')
              .map((r) => (
                <div key={r.release_id} className="card" style={{ maxWidth: 'none' }}>
                  <h3>
                    Version {r.version} <Badge kind="success">{r.status}</Badge>{' '}
                    <Badge kind="info">{r.channel}</Badge>
                  </h3>
                  <p style={wrapAnywhere}>
                    Supported OS/arch: {osArchSummary(r)}
                    <br />
                    Signing key id: <code>{r.signing_key_id ?? 'unsigned'}</code>
                    {r.published_at ? (
                      <>
                        <br />
                        Published: {new Date(r.published_at).toLocaleString()}
                      </>
                    ) : null}
                  </p>
                  {r.artifacts.length === 0 ? (
                    <p className="placeholder">No artifacts listed for this release.</p>
                  ) : (
                    <table>
                      <thead>
                        <tr>
                          <th scope="col">Artifact</th>
                          <th scope="col">Type</th>
                          <th scope="col">Size</th>
                          <th scope="col">SHA-256</th>
                          <th scope="col">Download</th>
                        </tr>
                      </thead>
                      <tbody>
                        {r.artifacts.map((a) => (
                          <tr key={a.name}>
                            <td style={wrapAnywhere}>{a.name}</td>
                            <td>
                              {a.kind}
                              <br />
                              <span style={{ color: 'var(--muted)' }}>{a.content_type}</span>
                            </td>
                            <td>{formatBytes(a.byte_size)}</td>
                            <td style={wrapAnywhere}>
                              <code style={wrapAnywhere}>{a.sha256}</code>{' '}
                              <CopyButton value={a.sha256} label="Copy" />
                            </td>
                            <td>
                              {a.name.endsWith('.pub') ? (
                                <a href={api.publicDownloadUrl(r.release_id, a.name)} download={a.name}>
                                  Download key
                                </a>
                              ) : (
                                <a href="/login">Sign in to download</a>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              ))}
          </>
        )}
      </DataView>
    </Section>
  )
}

// ---- Release notes ---------------------------------------------------------

function ReleaseNotes() {
  const [releases, setReleases] = useState<Release[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    api
      .getReleases()
      .then((r) => active && setReleases(r))
      .catch((e: { status?: number; message?: string }) => {
        if (!active) return
        setError(authErrorMessage(e, 'available releases'))
      })
    return () => {
      active = false
    }
  }, [])

  return (
    <Section title="Release notes">
      <p>
        Release metadata is sourced from the control plane. No changelog is fabricated here; each
        entry shows the factual, signed metadata for that release.
      </p>
      <DataView
        data={releases}
        error={error}
        what="releases"
        isEmpty={(r) => r.length === 0}
        empty="No releases have been recorded yet."
      >
        {(all) => (
          <table>
            <thead>
              <tr>
                <th scope="col">Version</th>
                <th scope="col">Channel</th>
                <th scope="col">Status</th>
                <th scope="col">Published</th>
                <th scope="col">Manifest digest</th>
                <th scope="col">Signing key id</th>
              </tr>
            </thead>
            <tbody>
              {all.map((r) => (
                <tr key={r.release_id}>
                  <td>{r.version}</td>
                  <td>{r.channel}</td>
                  <td>
                    <Badge kind={r.status === 'published' ? 'success' : 'neutral'}>{r.status}</Badge>
                  </td>
                  <td>{r.published_at ? new Date(r.published_at).toLocaleString() : '—'}</td>
                  <td style={wrapAnywhere}>
                    <code style={wrapAnywhere}>{r.manifest_digest ?? '—'}</code>
                  </td>
                  <td style={wrapAnywhere}>
                    <code style={wrapAnywhere}>{r.signing_key_id ?? '—'}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </DataView>
    </Section>
  )
}

// ---- Legal -----------------------------------------------------------------

function Legal() {
  const [policies, setPolicies] = useState<PolicyVersion[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    api
      .getPolicies()
      .then((p) => active && setPolicies(p))
      .catch((e: { status?: number; message?: string }) => {
        if (!active) return
        setError(authErrorMessage(e, 'configured policy documents'))
      })
    return () => {
      active = false
    }
  }, [])

  return (
    <Section title="Legal">
      <div className="banner banner-info" role="status" style={{ marginBottom: 12 }}>
        Local test placeholder — not valid production legal terms.
      </div>
      <p>
        The documents below are the policy versions configured in this control plane. No legal text
        is authored in this site; documents are operator-supplied and identified by digest.
      </p>
      <DataView
        data={policies}
        error={error}
        what="policy documents"
        isEmpty={(p) => p.length === 0}
        empty="No policy documents are configured."
      >
        {(all) => (
          <>
            <table>
              <thead>
                <tr>
                  <th scope="col">Type</th>
                  <th scope="col">Version</th>
                  <th scope="col">Title</th>
                  <th scope="col">Effective date</th>
                  <th scope="col">Document digest</th>
                </tr>
              </thead>
              <tbody>
                {all.map((p) => (
                  <tr key={p.policy_id}>
                    <td>{p.policy_type}</td>
                    <td>{p.version}</td>
                    <td>{p.title}</td>
                    <td>{p.effective_date ?? '—'}</td>
                    <td style={wrapAnywhere}>
                      <code style={wrapAnywhere}>{p.document_digest}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <ul>
              {all.map((p) => (
                <LegalReference key={p.policy_id} label={`${p.title} (v${p.version})`} />
              ))}
            </ul>
          </>
        )}
      </DataView>
    </Section>
  )
}

// ---- Service status --------------------------------------------------------

type RowKind = 'ready' | 'degraded' | 'loading' | 'error' | 'info'

function StatusRow({
  name,
  kind,
  detail,
}: {
  name: string
  kind: RowKind
  detail: string
}) {
  const badgeKind =
    kind === 'ready' ? 'success' : kind === 'degraded' || kind === 'error' ? 'warning' : 'neutral'
  const label =
    kind === 'ready'
      ? 'Operational'
      : kind === 'degraded'
        ? 'Degraded'
        : kind === 'error'
          ? 'Unavailable'
          : kind === 'loading'
            ? 'Checking…'
            : 'Info'
  return (
    <tr>
      <th scope="row" style={{ fontWeight: 600 }}>
        {name}
      </th>
      <td>
        <Badge kind={badgeKind}>{label}</Badge>
      </td>
      <td style={wrapAnywhere}>{detail}</td>
    </tr>
  )
}

function ServiceStatus() {
  const [readiness, setReadiness] = useState<Readiness | null>(null)
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null)
  const [ingestion, setIngestion] = useState<{ ready: boolean; batches: number | null } | null>(null)
  const [readyErr, setReadyErr] = useState(false)
  const [diagErr, setDiagErr] = useState(false)

  useEffect(() => {
    let active = true
    api
      .getReadiness()
      .then((r) => active && setReadiness(r))
      .catch(() => active && setReadyErr(true))
    api
      .getDiagnostics()
      .then((d) => active && setDiagnostics(d))
      .catch(() => active && setDiagErr(true))
    api
      .getIngestionStatus()
      .then((i) => active && setIngestion(i))
      .catch(() => active && setIngestion({ ready: false, batches: null }))
    return () => {
      active = false
    }
  }, [])

  const controlPlane: RowKind = readyErr
    ? 'error'
    : readiness === null
      ? 'loading'
      : readiness.status === 'ready'
        ? 'ready'
        : 'degraded'

  const ingestionKind: RowKind = ingestion === null ? 'loading' : ingestion.ready ? 'ready' : 'degraded'
  const dbKind: RowKind = diagErr ? 'error' : diagnostics === null ? 'loading' : 'ready'

  const publishedCount =
    diagnostics?.counts && typeof diagnostics.counts.published_releases === 'number'
      ? diagnostics.counts.published_releases
      : diagnostics?.counts && typeof diagnostics.counts.releases === 'number'
        ? diagnostics.counts.releases
        : null

  return (
    <Section title="Service status">
      <p>
        Live status of the hosted services. This reflects the control plane this site is connected
        to; no uptime figures or SLAs are claimed. Secrets, database URLs, and internal hostnames are
        never shown.
      </p>
      <table>
        <caption style={{ textAlign: 'left', marginBottom: 8 }}>Hosted service component status.</caption>
        <thead>
          <tr>
            <th scope="col">Component</th>
            <th scope="col">Status</th>
            <th scope="col">Detail</th>
          </tr>
        </thead>
        <tbody>
          <StatusRow name="Public site" kind="ready" detail="This page is being served." />
          <StatusRow
            name="Customer/admin portal"
            kind={controlPlane === 'loading' ? 'loading' : controlPlane === 'error' ? 'error' : 'ready'}
            detail="Served from the same frontend as this site."
          />
          <StatusRow
            name="Control plane"
            kind={controlPlane}
            detail={
              readyErr
                ? 'Did not respond.'
                : readiness === null
                  ? 'Checking…'
                  : readiness.status === 'ready'
                    ? 'Responding normally.'
                    : 'Responding, reduced service.'
            }
          />
          <StatusRow
            name="Telemetry ingestion"
            kind={ingestionKind}
            detail={
              ingestion === null
                ? 'Checking…'
                : ingestion.ready ? 'Accepting submissions.' : 'Reduced service.'
            }
          />
          <StatusRow
            name="Database"
            kind={dbKind}
            detail={
              diagErr ? 'Did not respond.' : diagnostics === null ? 'Checking…' : 'Online.'
            }
          />
          <StatusRow
            name="Object storage"
            kind={dbKind}
            detail={
              diagErr ? 'Did not respond.' : diagnostics === null ? 'Checking…' : 'Online.'
            }
          />
          <StatusRow
            name="Email delivery"
            kind={dbKind}
            detail={
              diagErr ? 'Did not respond.' : diagnostics === null ? 'Checking…' : 'Online.'
            }
          />
          <StatusRow
            name="Google Drive archive"
            kind="info"
            detail="Not active in this profile."
          />
          <StatusRow
            name="Release service"
            kind={dbKind}
            detail={
              diagErr
                ? 'Diagnostics endpoint did not respond.'
                : diagnostics === null
                  ? 'Querying diagnostics…'
                  : publishedCount !== null
                    ? `${publishedCount} release(s) recorded.`
                    : 'Release counts not reported.'
            }
          />
        </tbody>
      </table>
    </Section>
  )
}

// ---- Contact ---------------------------------------------------------------

function Contact() {
  return (
    <Section title="Contact and support">
      <p>
        Aithernet is invitation-only early-access software. For support, sign in to the customer
        portal and open a support case — that is the supported channel for enrolled tenants.
      </p>
      <p>
        <Link to="/login">Sign in</Link> to open a case, or{' '}
        <Link to="/accept-invite">accept an invitation</Link> if you have one.
      </p>
      <p>
        In local staging environments, support delivery runs through the local development support
        workflow rather than external email. No general-availability contact details, telephone
        numbers, postal address, uptime guarantees, or staffed hours are offered for this preview.
      </p>
    </Section>
  )
}

// ---- Router ----------------------------------------------------------------

const PUBLIC_ROUTES: Record<string, () => JSX.Element> = {
  '/': Overview,
  '/early-access': EarlyAccess,
  '/systems': SupportedSystems,
  '/hardware': SupportedHardware,
  '/install': Installation,
  '/docs': Documentation,
  '/downloads': Downloads,
  '/release-notes': ReleaseNotes,
  '/legal': Legal,
  '/status': ServiceStatus,
  '/contact': Contact,
}

export function PublicSite() {
  const { path } = useRouter()
  const Page = PUBLIC_ROUTES[path] ?? Overview
  return (
    <div className="public-grid">
      <Page />
    </div>
  )
}

export const publicNav: { to: string; label: string }[] = [
  { to: '/', label: 'Overview' },
  { to: '/early-access', label: 'Early access' },
  { to: '/systems', label: 'Supported systems' },
  { to: '/hardware', label: 'Supported hardware' },
  { to: '/install', label: 'Installation' },
  { to: '/docs', label: 'Documentation' },
  { to: '/downloads', label: 'Downloads' },
  { to: '/release-notes', label: 'Release notes' },
  { to: '/legal', label: 'Legal' },
  { to: '/status', label: 'Service status' },
  { to: '/contact', label: 'Contact' },
]
