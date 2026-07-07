import { useState } from 'react'

import { api } from '../api/client'
import type {
  EnrollmentCode,
  EnrollmentCodeSummary,
  Mesh,
  Release,
  SupportCaseDetail,
  TenantMembership,
} from '../api/types'
import {
  Badge,
  ConfirmButton,
  CopyButton,
  DataView,
  Field,
  Loading,
  Section,
  StatCard,
  StatusBanner,
} from '../components/ui'
import { useAction, useAsync } from '../hooks'
import { Link, useRouter } from '../routes/router'
import { useSession } from '../session'

const CUSTOMER_NAV: { to: string; label: string }[] = [
  { to: '/app', label: 'Overview' },
  { to: '/app/nodes', label: 'Nodes' },
  { to: '/app/meshes', label: 'Meshes' },
  { to: '/app/downloads', label: 'Downloads' },
  { to: '/app/data', label: 'Data' },
  { to: '/app/account', label: 'Account' },
  { to: '/app/support', label: 'Support' },
  { to: '/app/getting-started', label: 'Getting started' },
  { to: '/app/docs', label: 'Documentation' },
  { to: '/app/policies', label: 'Policies' },
  { to: '/app/sessions', label: 'Sessions' },
]

function stateKind(s: string): string {
  if (['online', 'ready', 'published', 'active'].includes(s)) return 'ok'
  if (['recent'].includes(s)) return 'info'
  if (['stale', 'paused', 'degraded'].includes(s)) return 'warn'
  if (['offline', 'revoked'].includes(s)) return 'danger'
  return 'neutral'
}

function CustomerOverview({ tenantId }: { tenantId: string | null }) {
  const policy = useAsync(() => api.getPolicyStatus(), [])
  const nodes = useAsync(() => (tenantId ? api.getFleetNodes(tenantId) : Promise.resolve([])), [tenantId])
  const releases = useAsync(() => api.getReleases('early-access'), [])
  const support = useAsync(() => (tenantId ? api.getSupportCases(tenantId) : Promise.resolve([])), [tenantId])
  const outstanding = (policy.data ?? []).filter((p) => !p.accepted).length
  return (
    <Section title="Customer overview">
      <p>Tenant: <strong>{tenantId ?? '(no tenant membership)'}</strong></p>
      <div className="stat-grid">
        <StatCard label="Outstanding policies" value={policy.data ? outstanding : '…'} to="/app/policies" />
        <StatCard label="Nodes" value={nodes.data?.length ?? '…'} to="/app/nodes" />
        <StatCard label="Available releases" value={(releases.data ?? []).filter((r) => r.status === 'published').length} to="/app/downloads" />
        <StatCard label="Support cases" value={support.data?.length ?? '…'} to="/app/support" />
        <StatCard
          label="Onboarding"
          value={<Badge kind={outstanding === 0 ? 'ok' : 'warn'}>{outstanding === 0 ? 'complete' : 'action needed'}</Badge>}
          to="/app/policies"
        />
      </div>
    </Section>
  )
}

function Account({ tenantId }: { tenantId: string | null }) {
  const { session } = useSession()
  const memberships = useAsync<TenantMembership[]>(() => api.getMemberships(), [])
  const nodes = useAsync(() => (tenantId ? api.getFleetNodes(tenantId) : Promise.resolve([])), [tenantId])
  return (
    <Section title="Account">
      <table>
        <tbody>
          <tr><th scope="row">Account ID</th><td className="mono">{session?.user?.user_id ?? '—'}</td></tr>
          <tr><th scope="row">Email (verified)</th><td>{session?.user?.email}</td></tr>
          <tr><th scope="row">Account state</th><td><Badge kind="ok">active</Badge></td></tr>
          <tr><th scope="row">Tenant memberships</th><td>
            {(memberships.data ?? []).length
              ? (memberships.data ?? []).map((m) => `${m.tenant_name} (${m.role})`).join(', ')
              : (tenantId ?? '—')}
          </td></tr>
          <tr><th scope="row">Roles</th><td>
            {session?.user?.is_platform_admin ? 'platform admin' : 'tenant member'}
          </td></tr>
          <tr><th scope="row">Nodes enrolled</th><td>{nodes.data?.length ?? '…'}</td></tr>
        </tbody>
      </table>
      <p className="hint">
        Your <strong>account</strong> (this email + immutable account ID) is separate from each
        <strong> node</strong>'s cryptographic identity. The email is never used as a node key and is
        never transmitted over the RF peer protocol. Your session is held in a secure HttpOnly cookie;
        no tokens are stored in the browser.
      </p>
      <p className="hint"><Link to="/app/sessions">Active sessions &amp; security events →</Link></p>
    </Section>
  )
}

function Policies({ tenantId }: { tenantId: string | null }) {
  const policies = useAsync(() => api.getPolicies(), [])
  const status = useAsync(() => api.getPolicyStatus(), [])
  const { busy, message, run } = useAction()
  async function accept(policyId: string, version: string) {
    await run(async () => {
      await api.acceptPolicy(policyId, version)
      status.reload()
    }, 'Policy accepted.')
  }
  return (
    <Section title="Policies">
      {message && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
      <DataView data={policies.data} error={policies.error} what="policies" isEmpty={(d) => d.length === 0}>
        {(list) => {
          const statusFor = (id: string) => (status.data ?? []).find((s) => s.policy_id === id)
          return (
            <table>
              <thead><tr><th scope="col">Type</th><th scope="col">Version</th><th scope="col">Title</th><th scope="col">Acceptance</th><th scope="col" /></tr></thead>
              <tbody>
                {list.map((p) => {
                  const st = statusFor(p.policy_id)
                  return (
                    <tr key={p.policy_id}>
                      <td>{p.policy_type}</td>
                      <td>{p.version}</td>
                      <td>{p.title}</td>
                      <td>{st?.accepted ? <Badge kind="ok">accepted</Badge> : <Badge kind="warn">not accepted</Badge>}</td>
                      <td>{!st?.accepted && tenantId && <button type="button" disabled={busy} onClick={() => void accept(p.policy_id, p.version)}>Accept</button>}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )
        }}
      </DataView>
    </Section>
  )
}

function NodesEnrollment({ tenantId }: { tenantId: string | null }) {
  const nodes = useAsync(() => (tenantId ? api.getFleetNodes(tenantId) : Promise.resolve([])), [tenantId])
  const codes = useAsync<EnrollmentCodeSummary[]>(
    () => (tenantId ? api.getEnrollmentCodes(tenantId) : Promise.resolve([])), [tenantId])
  const { busy, message, run } = useAction()
  const [code, setCode] = useState<EnrollmentCode | null>(null)
  async function mint() {
    if (!tenantId) return
    await run(async () => {
      setCode(await api.createEnrollmentCode(tenantId))
      codes.reload()
    }, 'One-time enrollment code created.')
  }
  async function revokeCode(id: string) {
    await run(async () => {
      await api.revokeEnrollmentCode(id)
      codes.reload()
    }, 'Enrollment code revoked.')
  }
  async function rename(hostedNodeId: string, current: string) {
    const next = window.prompt('New node display name', current)
    if (!next) return
    await run(async () => {
      await api.renameNode(hostedNodeId, next)
      nodes.reload()
    }, 'Node renamed.')
  }
  async function revokeNode(hostedNodeId: string) {
    await run(async () => {
      await api.revokeNode(hostedNodeId)
      nodes.reload()
    }, 'Node revoked.')
  }
  return (
    <>
      <Section title="Enroll a node" actions={<button type="button" disabled={busy || !tenantId} onClick={() => void mint()}>Create enrollment code</button>}>
        {message && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
        {!tenantId && <p className="state state-empty">Your account is not a member of a tenant, so node enrollment is unavailable.</p>}
        {code && (
          <div className="onetime">
            <p><strong>One-time enrollment code</strong> (shown once; it is never redisplayed):</p>
            <code className="mono break">{code.code}</code> <CopyButton value={code.code} />
            <p>On the node host, run:</p>
            <pre><code>aithernet enroll --base-url {window.location.origin} --code {code.code}</code></pre>
            <p className="hint">The generic package is byte-identical for every customer — the code binds the node to your tenant at enrollment.</p>
          </div>
        )}
      </Section>
      <Section title="Pending enrollment codes">
        <DataView data={codes.data} error={codes.error} what="codes" isEmpty={(d) => d.length === 0} empty="No enrollment codes.">
          {(list) => (
            <table>
              <thead><tr><th scope="col">Code</th><th scope="col">Status</th><th scope="col">Uses</th><th scope="col">Expires</th><th scope="col" /></tr></thead>
              <tbody>
                {list.map((c) => (
                  <tr key={c.enrollment_code_id}>
                    <td className="mono">{c.enrollment_code_id.slice(0, 8)}</td>
                    <td><Badge kind={stateKind(c.status)}>{c.status}</Badge></td>
                    <td>{c.used_count}/{c.maximum_uses}</td>
                    <td>{c.expires_at?.slice(0, 19).replace('T', ' ') ?? '—'}</td>
                    <td>{c.status === 'active' && <button type="button" disabled={busy} onClick={() => void revokeCode(c.enrollment_code_id)}>Revoke</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
      </Section>
      <Section title="Your nodes">
        <DataView data={nodes.data} error={nodes.error} what="nodes" isEmpty={(d) => d.length === 0} empty="No nodes enrolled yet — create an enrollment code above.">
          {(list) => (
            <table>
              <thead><tr><th scope="col">Node</th><th scope="col">State</th><th scope="col">Version</th><th scope="col">Meshes</th><th scope="col">Last seen</th><th scope="col">Hardware</th><th scope="col" /></tr></thead>
              <tbody>
                {list.map((n) => (
                  <tr key={n.hosted_node_id}>
                    <td>{n.display_name || n.node_id}</td>
                    <td><Badge kind={stateKind(n.state)}>{n.state}</Badge></td>
                    <td>{n.software_version ?? '—'}</td>
                    <td>{(n.mesh_memberships ?? []).length ? (n.mesh_memberships ?? []).length : '—'}</td>
                    <td>{n.last_heartbeat_at?.slice(0, 19).replace('T', ' ') ?? 'never'}</td>
                    <td>{n.hardware_families.length ? n.hardware_families.join(', ') : '—'}</td>
                    <td>
                      <button type="button" disabled={busy} onClick={() => void rename(n.hosted_node_id, n.display_name || n.node_id)}>Rename</button>{' '}
                      {n.status !== 'revoked' && <ConfirmButton onConfirm={() => void revokeNode(n.hosted_node_id)}>Revoke</ConfirmButton>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
      </Section>
    </>
  )
}

function Meshes({ tenantId }: { tenantId: string | null }) {
  const meshes = useAsync<Mesh[]>(() => (tenantId ? api.getMeshes(tenantId) : Promise.resolve([])), [tenantId])
  const nodes = useAsync(() => (tenantId ? api.getFleetNodes(tenantId) : Promise.resolve([])), [tenantId])
  const { busy, message, run } = useAction()
  const [name, setName] = useState('')
  async function create() {
    if (!tenantId || !name.trim()) return
    await run(async () => {
      await api.createMesh(tenantId, name.trim())
      setName('')
      meshes.reload()
    }, 'Mesh created.')
  }
  async function addMember(meshId: string, hostedNodeId: string) {
    await run(async () => {
      await api.addMeshMember(meshId, hostedNodeId)
      meshes.reload()
    }, 'Node added to mesh.')
  }
  return (
    <>
      <Section
        title="Create a mesh"
        actions={<button type="button" disabled={busy || !tenantId || !name.trim()} onClick={() => void create()}>Create mesh</button>}
      >
        {message && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
        {!tenantId && <p className="state state-empty">Join a tenant to manage meshes.</p>}
        <Field id="mesh-name" label="Mesh name">
          <input id="mesh-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Field Team A" />
        </Field>
        <p className="hint">
          A <strong>mesh</strong> is the explicit trust group that authorizes peer communication.
          Two nodes that can merely reach each other — even mutually key-trusted — cannot exchange
          messages until they share a mesh. Cross-tenant communication requires an explicit shared
          mesh; it never happens just because two nodes can hear each other over RF.
        </p>
      </Section>
      <Section title="Your meshes">
        <DataView data={meshes.data} error={meshes.error} what="meshes" isEmpty={(d) => d.length === 0} empty="No meshes yet — create one above.">
          {(list) => (
            <table>
              <thead><tr><th scope="col">Mesh</th><th scope="col">Members</th><th scope="col">Created</th><th scope="col">Add node</th></tr></thead>
              <tbody>
                {list.map((m) => (
                  <tr key={m.mesh_id}>
                    <td>{m.display_name} {m.revoked && <Badge kind="danger">revoked</Badge>}</td>
                    <td>{m.member_count}</td>
                    <td>{m.created_at.slice(0, 10)}</td>
                    <td>
                      <select disabled={busy} defaultValue="" onChange={(e) => { if (e.target.value) void addMember(m.mesh_id, e.target.value) }}>
                        <option value="">add a node…</option>
                        {(nodes.data ?? []).map((n) => (
                          <option key={n.hosted_node_id} value={n.hosted_node_id}>{n.display_name || n.node_id}</option>
                        ))}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
      </Section>
    </>
  )
}

// Customer-safe target label (was the literal "any / any").
function targetLabel(name: string): string {
  if (name.endsWith('.deb')) return 'Ubuntu 24.04 · amd64'
  if (name.endsWith('.whl') || name.endsWith('.tar.gz')) return 'Linux · any'
  if (name.endsWith('.pub') || name.endsWith('.sig') || name.endsWith('.json')
    || name === 'SHA256SUMS' || name.endsWith('.md')) return '—'
  return '—'
}

// The verification set is synthesized + public; only the .pub/manifest/sums go through the public
// download route. Packages + component bundle use the authenticated portal route.
function isPublicArtifact(name: string): boolean {
  return name.endsWith('.pub') || name === 'manifest.json' || name === 'manifest.sig'
    || name === 'SHA256SUMS'
}
function downloadHref(releaseId: string, name: string): string {
  return isPublicArtifact(name)
    ? api.publicDownloadUrl(releaseId, name)
    : api.downloadUrl(releaseId, name)
}

// The recommended Ubuntu bundle is now a single server-assembled ZIP. This component shows the
// authenticated one-click download plus the ZIP's SHA-256 (so the customer can confirm the exact
// delivery container) — the signed manifest inside remains the trust root.
function BundleDownload({ release, debName }: { release: Release; debName?: string }) {
  const meta = useAsync(() => api.getReleaseBundleMeta(release.release_id), [release.release_id])
  return (
    <>
      <p>
        <a className="btn-primary" href={api.releaseBundleUrl(release.release_id)} download>
          Download Ubuntu 24.04 amd64 bundle (.zip)
        </a>{' '}
        {debName && (
          <a className="btn-secondary" href={downloadHref(release.release_id, debName)} download>
            Just the .deb
          </a>
        )}
      </p>
      {meta.data && (
        <p className="hint">
          {meta.data.filename} · {(meta.data.byte_size / (1024 * 1024)).toFixed(1)} MiB ·{' '}
          {meta.data.files.length} files<br />
          ZIP SHA-256: <code className="break">{meta.data.sha256.replace('sha256:', '')}</code>
        </p>
      )}
    </>
  )
}

// Authenticated post-login customer journey. Commands are real packaged `aithernet …` commands
// (verified by tests/test_customer_docs_commands.py); version-specific values come from the
// published release metadata (never hard-coded).
export function GettingStarted() {
  const { data } = useAsync(() => api.getReleases('early-access'), [])
  const published = (data ?? []).filter((r) => r.status === 'published')
  const rel = published[0]
  const meta = useAsync(
    () => (rel ? api.getReleaseBundleMeta(rel.release_id) : Promise.resolve(null)),
    [rel?.release_id],
  )
  const version = rel?.version ?? '<version>'
  const deb = rel?.artifacts.find((a) => a.name.endsWith('.deb'))?.name
    ?? `aithernet_${version.replace(/-/g, '~')}_amd64.deb`
  const zip = meta.data?.filename ?? `aithernet-${version}-ubuntu24.04-amd64.zip`
  const zipSha = meta.data?.sha256?.replace('sha256:', '')
  const Step = ({ n, title, children }: { n: string; title: string; children: React.ReactNode }) => (
    <details className="release-card" open={n === '1'}>
      <summary><strong>{n}. {title}</strong></summary>
      {children}
    </details>
  )
  return (
    <Section title="Getting started — install &amp; run your node">
      <p className="hint">
        Ubuntu 24.04 LTS · amd64 · software-only. <code>sudo</code> is for APT/system changes only;
        run <code>aithernet</code> as your normal user (never root). The whole journey is four steps —
        <strong> download &amp; verify → install → <code>aithernet setup</code> → <code>aithernet run</code></strong>.
        You never edit YAML, systemd units, <code>PATH</code> or environment files, and there is no
        separate submit/start/watch. Full reference:{' '}
        <a href="/docs">documentation</a>.
      </p>
      <Step n="1" title="Download &amp; verify the release">
        <pre><code>{`sha256sum -c SHA256SUMS
openssl pkeyutl -verify -pubin -inkey aithernet-release.pub \\
  -rawin -in manifest.json -sigfile manifest.sig`}</code></pre>
        <p className="hint">From the Downloads page, get <code>{zip}</code> (which contains{' '}
          <code>{deb}</code>) plus the verification set served alongside it. Release {version}
          {rel?.signing_key_id ? <> · signed by <code>{rel.signing_key_id}</code></> : null}
          {zipSha ? <> · ZIP SHA-256: <code className="break">{zipSha}</code></> : null}.</p>
      </Step>
      <Step n="2" title="Install the package">
        <pre><code>{`unzip ${zip} -d aithernet-${version} && cd aithernet-${version}
sudo apt update
sudo apt install ./${deb}
aithernet --version
aithernet release verify "$PWD"`}</code></pre>
      </Step>
      <Step n="3" title="Guided setup — one command">
        <pre><code>{`aithernet setup`}</code></pre>
        <p className="hint"><code>aithernet setup</code> configures your workstation end-to-end: it
          initializes the canonical node identity and absolute state paths, configures and verifies
          the managed user service, resolves supported CLI runtimes (e.g. an NVM-installed CLI)
          automatically, verifies GNU Radio and the managed signed RF-MCP component, and runs a
          software-only readiness check. It guides you through connecting your agents in plain
          language — you never need adapter IDs:</p>
        <ul>
          <li><strong>Coordinator (planner):</strong> sign in with Gemini · use a Gemini API key ·
            use another supported local/OpenAI-compatible coordinator · configure later.</li>
          <li><strong>Coding agent:</strong> sign in with Codex · use another supported coding agent ·
            configure later.</li>
        </ul>
      </Step>
      <Step n="4" title="Run work — one command">
        <pre><code>{`aithernet run "<what you want Aithernet to do>"`}</code></pre>
        <p className="hint">A real software-only example:</p>
        <pre><code>{`aithernet run \\
  "Create, validate, execute, and summarize a bounded software-only GNU Radio signal generator. Do not use physical RF."`}</code></pre>
        <p className="hint">This single command creates the mission, starts it, shows progress,
          returns the result, and prints the mission, run and artifact IDs. Add <code>--detach</code>
          to run it in the background.</p>
      </Step>
      <Step n="5" title="Repair common drift">
        <pre><code>{`aithernet doctor
aithernet doctor --repair`}</code></pre>
        <p className="hint">Non-destructive: re-pins the canonical service config, restores the
          provider runtime PATH, reconciles the setup journal and restarts the service. No sudo, no
          data loss — it reports exactly what it changed.</p>
      </Step>
      <Step n="6" title="Upgrade from beta.8 (keep your state)">
        <pre><code>{`sudo apt update && sudo apt install ./${deb}
aithernet setup`}</code></pre>
        <p className="hint">Re-running <code>aithernet setup</code> detects your existing install and
          migrates it in place — preserving your node ID, node name, missions, events, artifacts and
          provider configuration, and regenerating the managed service. Do not delete your state, and
          you never edit systemd, <code>PATH</code> or YAML. <code>aithernet doctor --repair</code>
          is available if anything needs fixing.</p>
      </Step>
      <Step n="7" title="WSL limitations &amp; truthful readiness">
        <p className="hint">WSL is supported for software-only / agent qualification only; WSL audio
          endpoints are not SDRs. A software-only node is core-ready while physical-RF readiness is
          "unavailable" — that is expected, not an error. Receive-only; no RF transmission.
          Physical/USB SDR qualification belongs on supported physical Ubuntu hardware.</p>
      </Step>
      <details className="release-card">
        <summary><strong>Advanced / operator workflows (not part of normal onboarding)</strong></summary>
        <p className="hint">The standard journey is <code>setup</code> → <code>run</code>. These are
          for operators, automation and non-default setups; they are not required for onboarding.</p>
        <p><strong>Connect or reconfigure providers explicitly</strong> (the guided
          <code> setup</code> already does this for you):</p>
        <pre><code>{`aithernet agents connect coordinator
aithernet agents connect coding
# explicit, scriptable forms:
aithernet agents set-secret GEMINI_API_KEY
aithernet agents configure coordinator --provider gemini_api \\
  --model gemini-2.0-flash --api-key-ref GEMINI_API_KEY --identity-model workstation
aithernet agents configure coding --provider codex_cli \\
  --executable "$(command -v codex)" --identity-model workstation
aithernet agents configure coordinator --provider gemini_cli --executable "$(command -v gemini)"
aithernet agents provider-status coordinator
aithernet agents test coordinator --live`}</code></pre>
        <p><strong>Lower-level mission lifecycle</strong> (the normal workflow is one
          <code> aithernet run</code>):</p>
        <pre><code>{`aithernet mission submit "<prompt>"   # create only (does not run)
aithernet mission start <mission-id>  # start autonomous execution
aithernet mission watch <mission-id>  # read-only live status
aithernet mission retry <mission-id>  # re-run a blocked/failed mission (no duplicate)
aithernet mission show <mission-id>
aithernet mission events <mission-id>
aithernet mission artifacts <mission-id>
aithernet mission cancel <mission-id>`}</code></pre>
        <p><strong>Inspect / verify, SDR dependencies and service control:</strong></p>
        <pre><code>{`aithernet setup --status
aithernet components verify rf-mcp
aithernet mcp diagnostics
aithernet mcp tools
aithernet sdr install --mode recommended --profile software-only --yes
systemctl --user status aithernet-node.service --no-pager`}</code></pre>
        <p><strong>SDR peer transport (1.0.0-beta.1) — simulated RF:</strong> canonical peer messages
          can travel through authenticated encryption, fragmentation, CRC, FEC, BPSK/QPSK
          modulation, an impaired simulated channel, demodulation and reassembly to the receiving
          node&rsquo;s canonical ingress. Pair a trusted peer, inspect transports, then submit work
          over <code>simulated_rf</code> (the receiving node owns policy; RF is never silently
          downgraded to IP):</p>
        <pre><code>{`aithernet peers pair --name nodeB --public-key <peer-ed25519-key> --endpoint-url <url>
aithernet peers transports nodeB
aithernet peers test nodeB --transport simulated_rf
aithernet run --on nodeB --transport simulated_rf "<bounded software-only objective>"
aithernet rf profiles list
aithernet rf profiles inspect ref-bpsk-1k
aithernet rf transport`}</code></pre>
        <p><strong>Operator-controlled physical SDR transmission (rf_ota):</strong> physical TX is a
          real capability you control LOCALLY — no vendor approval, cloud authorization, subscription,
          or external service. Enable it on a TX-capable SDR, configure your bounds, then submit work
          over <code>rf_ota</code>:</p>
        <pre><code>{`aithernet rf tx devices                 # discover TX-capable SDRs
aithernet rf tx capabilities plutosdr   # device ranges/controls
aithernet rf tx enable
aithernet rf tx configure --device-id <id> --uri <uri> --max-duration-seconds 2 --gain-ceiling 60
aithernet rf tx test-plan --center-frequency-hz <YOU CHOOSE> --profile ref-bpsk-1k
aithernet rf tx loopback                # contained self-test (no radiation)
aithernet rf tx status
aithernet peers test nodeB --transport rf_ota
aithernet run --on nodeB --transport rf_ota "<objective>"`}</code></pre>
        <p className="hint">Aithernet validates device capabilities, enforces device + operator
          ranges, authenticates peers, bounds execution, requires your confirmation of the exact
          plan, and records it — but <strong>never chooses a frequency</strong>. You select all
          physical parameters and operate the radio per the rules at your location. Profile
          qualification (<em>software-validated</em>, <em>operator-custom</em>,
          <em>hardware-tested</em>) informs you but never blocks use. Cabled, shielded and OTA tests
          are run by you on your hardware; the shipped automated tests use loopback and do not
          radiate.</p>
        <p className="hint">Hosted/fleet enrollment and hardware-profile installation apply to hosted
          or physical-SDR deployments, not the standalone software-only workstation journey above.</p>
      </details>
    </Section>
  )
}

// Version ordering so the newest release is "current" regardless of API order. A final release
// (no -beta) outranks a beta of the same x.y.z; beta numbers compare numerically (beta.10 > beta.7).
function parseVersion(v: string): number[] {
  const m = v.match(/^(\d+)\.(\d+)\.(\d+)(?:[-.]beta[.-]?(\d+))?/i)
  if (!m) return [0, 0, 0, 0, 0]
  const isBeta = m[4] !== undefined
  return [Number(m[1]), Number(m[2]), Number(m[3]), isBeta ? 0 : 1, isBeta ? Number(m[4]) : 0]
}
function compareVersionsDesc(a: string, b: string): number {
  const pa = parseVersion(a)
  const pb = parseVersion(b)
  for (let i = 0; i < pa.length; i++) if (pa[i] !== pb[i]) return pb[i] - pa[i]
  return 0
}

type ReleaseRole = 'current' | 'previous' | 'archive'

function ReleaseCard({ release: r, role }: { release: Release; role: ReleaseRole }) {
  const deb = r.artifacts.find((a) => a.name.endsWith('.deb'))
  const heading =
    role === 'current' ? `Aithernet ${r.version} — recommended for Ubuntu 24.04 (amd64)`
      : role === 'previous' ? `Aithernet ${r.version} — previous release`
        : `Aithernet ${r.version}`
  return (
    <div className="release-card">
      <h3>
        {heading}{' '}
        {role === 'current' && <Badge kind="ok">current</Badge>}
        {role === 'previous' && <Badge kind="neutral">previous</Badge>}
      </h3>
      <p className="hint">
        Signed with key id <code>{r.signing_key_id ?? '—'}</code>. The bundle contains the{' '}
        complete release — every packaged artifact (<code>.deb</code>, wheel and sdist), the managed
        CatGPT Gateway runtime image, the verification set (manifest + signature + checksums +
        public key) and the offline RF-MCP component — so <code>sha256sum -c SHA256SUMS</code>{' '}
        succeeds directly from the extracted folder.
      </p>
      <BundleDownload release={r} debName={deb?.name} />
      <details>
        <summary>Verify &amp; install</summary>
        <pre><code>{`# verify BEFORE installing (standard tools):
sha256sum -c SHA256SUMS
openssl pkeyutl -verify -pubin -inkey aithernet-release.pub \\
  -rawin -in manifest.json -sigfile manifest.sig

# install
sudo apt install ./${deb?.name ?? 'aithernet_<version>_amd64.deb'}
aithernet --help

# (after install) optional all-in-one re-check of the download directory
aithernet release verify ./`}</code></pre>
      </details>
      <details>
        <summary>Advanced — all artifacts</summary>
        <table>
          <thead><tr>
            <th scope="col">Artifact</th><th scope="col">Type</th>
            <th scope="col">Target</th><th scope="col">Size</th>
            <th scope="col">SHA-256</th><th scope="col" />
          </tr></thead>
          <tbody>
            {r.artifacts.map((a) => (
              <tr key={a.name}>
                <td className="mono">{a.name}</td>
                <td>{a.kind ?? 'metadata'}</td>
                <td>{targetLabel(a.name)}</td>
                <td>{(a.byte_size / 1024).toFixed(1)} KiB</td>
                <td className="mono">{a.sha256?.replace('sha256:', '').slice(0, 16)}…</td>
                <td><a className="btn-secondary" href={downloadHref(r.release_id, a.name)}
                  download>{a.name.endsWith('.pub') ? 'Key' : 'Download'}</a></td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  )
}

function ReleasesCustomer() {
  const { data, error } = useAsync(() => api.getReleases('early-access'), [])
  // Distinguish a policy/authorization gate from a generic load error (no sign-in loop).
  // useAsync normalizes a 403 to "You do not have permission to view this." — for the releases
  // surface that means the account isn't authorized yet (most commonly: required terms unaccepted).
  const policyGated = !!error && /permission|required_polic|not_a_customer|forbidden|403/i.test(error)

  if (policyGated) {
    return (
      <Section title="Releases & downloads">
        <StatusBanner kind="info">
          You need to accept the required terms before you can download releases.
        </StatusBanner>
        <p><Link to="/app/policies">Review &amp; accept the required policy</Link> to continue.</p>
      </Section>
    )
  }

  return (
    <Section title="Releases & downloads">
      <p className="hint">
        Supported baseline: <strong>Ubuntu 24.04 LTS · amd64</strong>. Early-access qualification
        candidate — verify every release before installing. The newest release is recommended; the
        previous release stays available, and older releases are kept in the archive.
      </p>
      <DataView data={data} error={error} what="releases"
        isEmpty={(d) => d.filter((r) => r.status === 'published').length === 0}
        empty="No published releases are available yet.">
        {(releases) => {
          const published = releases.filter((r) => r.status === 'published')
            .slice().sort((a, b) => compareVersionsDesc(a.version, b.version))
          const current = published[0]
          const previous = published[1]
          const archive = published.slice(2)
          return (
            <>
              {current && <ReleaseCard release={current} role="current" />}
              {previous && <ReleaseCard release={previous} role="previous" />}
              {archive.length > 0 && (
                <details className="release-card">
                  <summary>Archive — older releases ({archive.length})</summary>
                  <p className="hint">Older releases and their hashes remain available; links are
                    unchanged.</p>
                  {archive.map((r) => <ReleaseCard key={r.release_id} release={r} role="archive" />)}
                </details>
              )}
            </>
          )
        }}
      </DataView>
    </Section>
  )
}

function SupportCustomer({ tenantId }: { tenantId: string | null }) {
  const cases = useAsync(() => (tenantId ? api.getSupportCases(tenantId) : Promise.resolve([])), [tenantId])
  const { busy, message, run } = useAction()
  const [subject, setSubject] = useState('')
  const [open, setOpen] = useState<SupportCaseDetail | null>(null)
  const [reply, setReply] = useState('')
  async function create(e: React.FormEvent) {
    e.preventDefault()
    if (!tenantId) return
    const ok = await run(() => api.createSupportCase(tenantId, subject), 'Support case created.')
    if (ok) {
      setSubject('')
      cases.reload()
    }
  }
  return (
    <>
      <Section title="Open a support case">
        {message && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
        {!tenantId ? (
          <p className="state state-empty">Your account is not a member of a tenant.</p>
        ) : (
          <form onSubmit={create} aria-label="Create support case">
            <Field id="sc-subject" label="Subject"><input id="sc-subject" value={subject} onChange={(e) => setSubject(e.target.value)} required /></Field>
            <button type="submit" disabled={busy}>{busy ? 'Creating…' : 'Create case'}</button>
          </form>
        )}
      </Section>
      <Section title="Your support cases">
        <DataView data={cases.data} error={cases.error} what="support cases" isEmpty={(d) => d.length === 0}>
          {(rows) => (
            <table>
              <thead><tr><th scope="col">Subject</th><th scope="col">Status</th><th scope="col">Created</th><th scope="col" /></tr></thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.case_id}>
                    <td>{c.subject}</td>
                    <td><Badge kind={stateKind(c.status)}>{c.status}</Badge></td>
                    <td>{c.created_at?.slice(0, 10)}</td>
                    <td><button type="button" className="btn-secondary" onClick={async () => setOpen(await api.getSupportCase(c.case_id))}>Open</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
      </Section>
      {open && (
        <Section title={`Case: ${open.subject}`}>
          <ul className="thread">
            {open.messages.length === 0 && <li className="state state-empty">No messages yet.</li>}
            {open.messages.map((m, i) => (
              <li key={i}><Badge kind={m.author_role === 'support' ? 'info' : 'neutral'}>{m.author_role}</Badge> {m.body} <span className="hint">{m.created_at?.slice(0, 19).replace('T', ' ')}</span></li>
            ))}
          </ul>
          <Field id="cust-reply" label="Add a message"><textarea id="cust-reply" value={reply} onChange={(e) => setReply(e.target.value)} /></Field>
          <button type="button" disabled={!reply} onClick={async () => { await api.addSupportMessage(open.case_id, reply); setReply(''); setOpen(await api.getSupportCase(open.case_id)) }}>Send</button>
        </Section>
      )}
    </>
  )
}

function SessionsSecurity() {
  const { data, error } = useAsync(() => api.getSessions(), [])
  const { signOut } = useSession()
  return (
    <Section title="Sessions & security" actions={<ConfirmButton confirmLabel="Sign out" onConfirm={() => void signOut()}>Sign out current session</ConfirmButton>}>
      <DataView data={data} error={error} what="sessions" isEmpty={(d) => d.length === 0}>
        {(sessions) => (
          <table>
            <thead><tr><th scope="col">Created</th><th scope="col">User agent</th><th scope="col">Expires</th><th scope="col">State</th></tr></thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.session_id}>
                  <td>{s.created_at?.slice(0, 19).replace('T', ' ')}</td>
                  <td>{s.user_agent ? s.user_agent.slice(0, 40) : '—'}</td>
                  <td>{s.expires_at?.slice(0, 10)}</td>
                  <td>{s.revoked ? <Badge kind="danger">revoked</Badge> : <Badge kind="ok">active</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </DataView>
      <p className="hint">Password reset and per-session revocation are operator-configured flows; session secrets are never displayed.</p>
    </Section>
  )
}

function DataPrivacy({ tenantId }: { tenantId: string | null }) {
  const nodes = useAsync(() => (tenantId ? api.getFleetNodes(tenantId) : Promise.resolve([])), [tenantId])
  const research = useAsync(
    () => (tenantId ? api.getResearchNodeSummary(tenantId) : Promise.resolve([])), [tenantId])
  return (
    <>
      <Section title="Data & privacy">
        <p>
          Telemetry is consent-controlled and export is OFF by default. Each node enforces consent
          locally; raw mission data, prompts and artifacts stay on the node and are never shown in
          this hosted portal.
        </p>
        <DataView data={nodes.data} error={nodes.error} what="export summary" isEmpty={(d) => d.length === 0} empty="No nodes to summarize.">
          {(list) => (
            <table>
              <thead><tr><th scope="col">Node</th><th scope="col">Data export state</th></tr></thead>
              <tbody>
                {list.map((n) => (
                  <tr key={n.hosted_node_id}><td>{n.display_name || n.node_id}</td><td>{n.data_export_state ?? 'off'}</td></tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
        <p className="hint">Manage raw data, consent and local artifacts in the node's local technical dashboard, not here.</p>
      </Section>
      <Section title="Research recording &amp; owner archive">
        <p>
          When you grant consent, each node uploads sanitized research packages to the Aithernet
          owner archive — not to a client-side Google Drive. Secrets are secret-scanned out and
          quarantined locally before anything leaves the node. Withdraw consent any time with
          <code> aithernet data research-consent --withdraw</code>.
        </p>
        <DataView data={research.data} error={research.error} what="research summary" isEmpty={(d) => d.length === 0}
          empty="No nodes reporting research status yet.">
          {(list) => (
            <table>
              <thead><tr>
                <th scope="col">Node</th><th scope="col">Consent</th><th scope="col">Owner-archive upload</th>
                <th scope="col">Packages received</th><th scope="col">Last upload</th>
              </tr></thead>
              <tbody>
                {list.map((r) => (
                  <tr key={r.node_id}>
                    <td className="mono">{r.node_id}</td>
                    <td>{r.consent ? <Badge kind="ok">granted</Badge> : <Badge kind="neutral">withdrawn</Badge>}</td>
                    <td>{r.owner_archive_upload ? <Badge kind="ok">on</Badge> : <Badge kind="neutral">off</Badge>}</td>
                    <td>{r.packages_received}</td>
                    <td>{r.last_upload_at?.slice(0, 19).replace('T', ' ') ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
        <p className="hint">This view shows only consent and upload status — never Google Drive
          credentials, admin tokens or record contents.</p>
      </Section>
    </>
  )
}

// beta.7 (FIX 11): the client-facing documentation section (managed CatGPT, noVNC, coordinator,
// coding agent, sandbox, GNU Radio, troubleshooting, architecture). Version-specific values come
// from published release metadata (never hard-coded).
export function ClientDocs() {
  const { data } = useAsync(() => api.getReleases('early-access'), [])
  const published = (data ?? []).filter((r) => r.status === 'published')
    .slice().sort((a, b) => compareVersionsDesc(a.version, b.version))
  const rel = published[0]
  const deb = rel?.artifacts.find((a) => a.name.endsWith('.deb'))?.name ?? 'aithernet_1.0.0~beta.9_amd64.deb'
  return (
    <Section title="Documentation">
      <p className="hint">
        The complete client guide for the current early-access release
        {rel ? <> (<strong>Aithernet {rel.version}</strong>)</> : null}. Commands are the real
        packaged <code>aithernet …</code> commands.
      </p>

      <details className="release-card" open>
        <summary><strong>A. Download &amp; verify</strong></summary>
        <pre><code>{`sha256sum -c SHA256SUMS
openssl pkeyutl -verify -pubin -inkey aithernet-release.pub \\
  -rawin -in manifest.json -sigfile manifest.sig
aithernet release verify ./
sudo apt install ./${deb}
hash -r; which aithernet; aithernet --version   # -> aithernet 1.0.0b9`}</code></pre>
      </details>

      <details className="release-card">
        <summary><strong>B. Enroll</strong></summary>
        <pre><code>{`aithernet setup
aithernet enroll --base-url https://www.aithernet.online --code <PORTAL_CODE>
aithernet doctor`}</code></pre>
        <p className="hint">Get your enrollment code from <Link to="/app/nodes">Nodes</Link>.</p>
      </details>

      <details className="release-card">
        <summary><strong>C. Managed CatGPT Gateway (coordinator)</strong></summary>
        <pre><code>{`aithernet catgpt setup
aithernet catgpt start
aithernet catgpt open        # opens http://127.0.0.1:6080/vnc.html?autoconnect=1
aithernet catgpt vnc-password   # the LOCAL noVNC password only
aithernet catgpt status`}</code></pre>
        <ul>
          <li>Open <code>http://127.0.0.1:6080/vnc.html?autoconnect=1</code> and log into
            ChatGPT/Claude yourself inside the noVNC browser.</li>
          <li>The noVNC password is <strong>local only</strong> — not your ChatGPT/Claude password.
            Aithernet never sees or stores your web-account password, cookies, or session, and ships
            no browser credentials in the image.</li>
          <li>CatGPT Gateway is <strong>coordinator-only</strong>; the coding agent is separate.</li>
          <li>Aithernet ships the gateway image as a signed release artifact — no git clone, no
            <code>docker login</code>, no Docker Hub pull, no manual build.</li>
        </ul>
      </details>

      <details className="release-card">
        <summary><strong>D. Verify the coordinator</strong></summary>
        <pre><code>{`aithernet service restart
aithernet agents provider-status coordinator
aithernet agents test coordinator --live   # expected model: catgpt-browser`}</code></pre>
        <p className="hint">Aithernet auto-persists the discovered model (<code>catgpt-browser</code>)
          — no manual <code>agents configure coordinator --model</code> needed.</p>
      </details>

      <details className="release-card">
        <summary><strong>E. Coding agent</strong></summary>
        <pre><code>{`aithernet agents connect coding
aithernet agents test coding --live
aithernet agents provider-status coding`}</code></pre>
      </details>

      <details className="release-card">
        <summary><strong>F. Sandbox / AppArmor (Ubuntu 24.04)</strong></summary>
        <p>Ubuntu 24.04 restricts unprivileged user namespaces, which can block the bubblewrap
          coding sandbox. Aithernet <strong>never changes host hardening silently</strong>:</p>
        <pre><code>{`aithernet coding-sandbox status
aithernet coding-sandbox repair --temporary   # asks first; runtime-only, resets on reboot
aithernet coding-sandbox restore              # or: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=1`}</code></pre>
      </details>

      <details className="release-card">
        <summary><strong>G. GNU Radio / RF-MCP (software-only)</strong></summary>
        <pre><code>{`aithernet doctor
aithernet run "Create, validate, execute, and summarize a bounded software-only GNU Radio signal generator. Do not use physical RF."`}</code></pre>
        <p className="hint">No physical RF by default in these examples.</p>
      </details>

      <details className="release-card">
        <summary><strong>H. Troubleshooting</strong></summary>
        <ul>
          <li>noVNC asks for a password → <code>aithernet catgpt vnc-password</code></li>
          <li>noVNC shows a directory listing → open <code>/vnc.html?autoconnect=1</code>
            (or <code>aithernet catgpt open</code>)</li>
          <li>coordinator model missing → auto-repairs via <code>aithernet catgpt status</code></li>
          <li>Docker missing → <code>aithernet catgpt install-runtime</code>
            (then <code>newgrp docker</code> or re-login)</li>
          <li>CatGPT image missing → re-download the complete portal bundle, or
            <code>aithernet components install catgpt-gateway</code></li>
          <li>coding sandbox degraded → <code>aithernet coding-sandbox repair --temporary</code>
            (restore with <code>… restore</code>)</li>
          <li>mission blocked → <code>aithernet mission events &lt;MISSION_ID&gt;</code>
            (then <code>aithernet coding-task status &lt;id&gt;</code>)</li>
        </ul>
      </details>

      <details className="release-card">
        <summary><strong>I. Architecture</strong></summary>
        <ul>
          <li><strong>MissionEngine</strong> owns missions, runs, budgets, events and retries.</li>
          <li><strong>CatGPT Gateway</strong> provides coordinator inference only.</li>
          <li><strong>Codex / Claude Code</strong> etc. are the coding providers.</li>
          <li><strong>RF-MCP / GNU Radio</strong> are tools; no physical RF unless explicitly
            configured and permitted.</li>
        </ul>
      </details>

      <details className="release-card">
        <summary><strong>J. Research recording &amp; owner-archive upload</strong></summary>
        <p>Sanitized research data goes to the <strong>Aithernet owner/admin archive</strong> after
          you consent — <strong>not</strong> to a client-side Google Drive. There is <strong>no
          Google Drive setup on the client</strong>: the owner archive Drive is connected once by an
          Aithernet administrator (in the admin portal Research section), managed by hosted. You never
          install an OAuth client, hold a refresh token, or configure Drive yourself.</p>
        <p>Recording is <strong>automatic after <code>aithernet setup</code> → Y</strong>. The setup
          question is now:</p>
        <pre><code>{`Enable sanitized research recording and upload to the Aithernet owner archive? [y/N]`}</code></pre>
        <p>Answer <strong>Y</strong> and every completed mission automatically writes one sanitized
          research record and queues it for upload to the owner archive — no manual step needed. It
          is provider-agnostic and works the same whether your coordinator/coding agents are CatGPT,
          the Claude / OpenAI / Gemini API, Codex, Claude Code, or any other supported provider.</p>
        <p><strong>Diagnostics only</strong> (you do <em>not</em> need these in normal operation):</p>
        <pre><code>{`aithernet data status                    # recorder_active, consent, packages, next_step
aithernet data verify                    # spool integrity + counts + upload state + next action
aithernet data research-consent                   # show current consent
aithernet data research-consent --grant           # grant consent (also offered during setup)
aithernet data research-consent --withdraw        # opt out — stops recording & upload
aithernet data upload-now                # force an upload attempt now (usually automatic)`}</code></pre>
        <p><strong>What is collected</strong> — provider-agnostic, structured research signal only:
          mission, coordinator, tool, coding, outcome, environment and quality metadata. This is the
          same regardless of which provider you use (CatGPT, Claude/OpenAI/Gemini API, Codex, Claude
          Code, etc.).</p>
        <p><strong>What is NEVER collected or uploaded</strong> — these are secret-scanned out and
          <strong> quarantined</strong> locally, never uploaded:</p>
        <ul>
          <li>API keys and provider tokens (including <code>CATGPT_GATEWAY_API_KEY</code>)</li>
          <li>OAuth / refresh tokens and Google credentials</li>
          <li>VNC passwords, cookies and browser sessions</li>
          <li><code>hosted-prod.env</code>, SSH keys and other host secrets</li>
        </ul>
        <p>Quarantined counts are reported without printing the secret. Records capture
          provider-exposed messages + structured metadata only — never hidden chain-of-thought.</p>
        <p><strong>How to opt out:</strong> <code>aithernet data research-consent --withdraw</code> — recording
          and upload stop immediately.</p>
        <p className="hint"><strong>Advanced / standalone only.</strong> <code>aithernet data
          drive-client</code> and <code>aithernet data sync</code> exist ONLY for the advanced
          standalone / local-archive mode (running your own local Drive archive). They are <em>not</em>
          the normal path — hosted clients never install a Drive client or run <code>data sync</code>;
          the owner archive is managed by hosted.</p>
        <p><strong>Fixing common problems (research upload)</strong></p>
        <ul>
          <li>No packages after a mission → confirm consent is granted in <code>aithernet data
            status</code>, then <strong>restart the node</strong> so a running worker picks up the
            mode (<code>aithernet service restart</code>).</li>
          <li>Nothing uploaded → run <code>aithernet data upload-now</code>; it reports the exact
            next step (nothing pending, or waiting on the owner archive).</li>
          <li>Quarantine/secrets detected → inspect the spool <code>quarantine/</code> locally;
            nothing there is ever uploaded.</li>
          <li><code>aithernet doctor</code> surfaces recorder/upload state with actionable guidance
            only when something needs you.</li>
        </ul>
      </details>
    </Section>
  )
}

function renderCustomerSection(path: string, tenantId: string | null) {
  switch (path) {
    case '/app/getting-started': return <GettingStarted />
    case '/app/docs': return <ClientDocs />
    case '/app/account': return <Account tenantId={tenantId} />
    case '/app/policies': return <Policies tenantId={tenantId} />
    case '/app/nodes': return <NodesEnrollment tenantId={tenantId} />
    case '/app/meshes': return <Meshes tenantId={tenantId} />
    case '/app/downloads': return <ReleasesCustomer />
    case '/app/support': return <SupportCustomer tenantId={tenantId} />
    case '/app/sessions': return <SessionsSecurity />
    case '/app/data': return <DataPrivacy tenantId={tenantId} />
    default: return <CustomerOverview tenantId={tenantId} />
  }
}

export function CustomerPortal() {
  const { session, loading } = useSession()
  const { path } = useRouter()
  const memberships = useAsync<TenantMembership[]>(() => api.getMemberships(), [])
  if (loading) return <Loading what="your account" />
  if (!session?.authenticated) {
    return (
      <Section title="Sign in to your account">
        <StatusBanner kind="info">You need to sign in to access the customer portal.</StatusBanner>
        <p>
          <Link to="/login">Sign in</Link>{' · '}
          have an invitation? <Link to="/app/accept-invite">Accept it</Link>{' · '}
          new here? <a href="/request-access">Request early access</a>
        </p>
      </Section>
    )
  }
  // Signed in, but the account is not yet provisioned to any tenant — a distinct state, NOT a
  // sign-in prompt. Wait for memberships to load before deciding.
  if (memberships.data !== null && memberships.data.length === 0) {
    return (
      <Section title="Your access is being set up">
        <StatusBanner kind="info">
          You&apos;re signed in as {session.user?.email}, but your account isn&apos;t part of a
          tenant yet.
        </StatusBanner>
        <p>
          Aithernet is invitation-only. If you requested early access, an administrator still needs
          to approve it and send your invitation — you&apos;ll get it by email. If you already have
          an invitation, accept it to finish setting up your account, tenant and policy acceptance.
        </p>
        <p>
          <Link to="/app/accept-invite">Accept an invitation</Link>{' · '}
          <a href="/request-access">Request early access</a>{' · '}
          <a href="/">Back to Aithernet</a>
        </p>
      </Section>
    )
  }
  const tenantId = memberships.data && memberships.data.length > 0 ? memberships.data[0].tenant_id : null
  return (
    <div className="console">
      <nav className="subnav" aria-label="Customer sections">
        {CUSTOMER_NAV.map((n) => (
          <Link key={n.to} to={n.to}>{n.label}</Link>
        ))}
      </nav>
      <div className="console-body">{renderCustomerSection(path, tenantId)}</div>
    </div>
  )
}
