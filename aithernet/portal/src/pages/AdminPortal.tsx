import { useState } from 'react'

import { api } from '../api/client'
import type { SupportCaseDetail } from '../api/types'
import { AdminOnly } from '../components/AdminOnly'
import {
  Badge,
  ConfirmButton,
  CopyButton,
  DataView,
  Field,
  Section,
  StatCard,
  StatusBanner,
} from '../components/ui'
import { useAction, useAsync } from '../hooks'
import { Link, useRouter } from '../routes/router'
import { useSession } from '../session'

const ADMIN_NAV: { to: string; label: string }[] = [
  { to: '/admin', label: 'Overview' },
  { to: '/admin/early-access', label: 'Early access' },
  { to: '/admin/policies', label: 'Policies' },
  { to: '/admin/invitations', label: 'Invitations' },
  { to: '/admin/email', label: 'Dev email' },
  { to: '/admin/tenants', label: 'Tenants & users' },
  { to: '/admin/fleet', label: 'Fleet' },
  { to: '/admin/releases', label: 'Releases' },
  { to: '/admin/support', label: 'Support' },
  { to: '/admin/audit', label: 'Audit' },
  { to: '/admin/ingestion', label: 'Ingestion' },
  { to: '/admin/drive', label: 'Drive archive' },
  { to: '/admin/research', label: 'Research' },
  { to: '/admin/status', label: 'System status' },
]

function stateKind(s: string): string {
  if (['online', 'ready', 'published', 'active'].includes(s)) return 'ok'
  if (['recent', 'candidate', 'draft'].includes(s)) return 'info'
  if (['stale', 'paused', 'degraded'].includes(s)) return 'warn'
  if (['offline', 'revoked', 'disabled'].includes(s)) return 'danger'
  return 'neutral'
}

function driveBadgeKind(s: string): string {
  if (s === 'connected') return 'ok'
  if (s === 'error') return 'warn'
  if (s === 'unconfigured') return 'info'
  return 'neutral'
}

function AdminOverview() {
  const { data, error } = useAsync(() => api.getDiagnostics(), [])
  const ready = useAsync(() => api.getReadiness(), [])
  return (
    <Section title="Admin overview">
      <DataView data={data} error={error} what="summary">
        {(d) => (
          <div className="stat-grid">
            <StatCard label="Tenants" value={d.counts.tenants ?? 0} to="/admin/tenants" />
            <StatCard label="Users" value={d.counts.users ?? 0} to="/admin/tenants" />
            <StatCard label="Nodes" value={d.counts.nodes ?? 0} to="/admin/fleet" />
            <StatCard label="Invitations" value={d.counts.invitations ?? 0} to="/admin/invitations" />
            <StatCard label="Releases" value={d.counts.releases ?? 0} to="/admin/releases" />
            <StatCard label="Support cases" value={d.counts.support_cases ?? 0} to="/admin/support" />
            <StatCard
              label="Control plane"
              value={<Badge kind={stateKind(ready.data?.status ?? '')}>{ready.data?.status ?? '…'}</Badge>}
              hint={`env: ${d.environment}`}
              to="/admin/status"
            />
            <StatCard label="Database" value={d.database_dialect} hint="central DB" to="/admin/status" />
          </div>
        )}
      </DataView>
    </Section>
  )
}

function PoliciesAdmin() {
  const { data, error, reload } = useAsync(() => api.getPolicies(), [])
  const { busy, message, run } = useAction()
  const [form, setForm] = useState({ policy_type: 'preview_terms', version: '', title: '', document_text: '', required: 'operational' })
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    const ok = await run(
      () =>
        api.publishPolicy({
          policy_type: form.policy_type,
          version: form.version,
          title: form.title,
          document_text: form.document_text,
          required_categories: form.required.split(',').map((s) => s.trim()).filter(Boolean),
        }),
      'Policy published.',
    )
    if (ok) reload()
  }
  return (
    <>
      <Section title="Policy versions">
        {message && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
        <DataView data={data} error={error} what="policies" isEmpty={(d) => d.length === 0}>
          {(policies) => (
            <table>
              <thead>
                <tr>
                  <th scope="col">Type</th>
                  <th scope="col">Version</th>
                  <th scope="col">Title</th>
                  <th scope="col">Required</th>
                  <th scope="col">Digest</th>
                  <th scope="col">Effective</th>
                </tr>
              </thead>
              <tbody>
                {policies.map((p) => (
                  <tr key={p.policy_id}>
                    <td>{p.policy_type}</td>
                    <td>{p.version}</td>
                    <td>{p.title}</td>
                    <td>{(p.required_categories ?? []).join(', ') || '—'}</td>
                    <td className="mono">{p.document_digest?.slice(0, 18)}…</td>
                    <td>{p.effective_date?.slice(0, 10) ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
      </Section>
      <Section title="Publish a policy version">
        <p>Historical acceptances are immutable; a new version supersedes the prior one.</p>
        <form onSubmit={submit} aria-label="Publish policy">
          <Field id="pol-type" label="Policy type">
            <input id="pol-type" value={form.policy_type} onChange={(e) => setForm({ ...form, policy_type: e.target.value })} required />
          </Field>
          <Field id="pol-version" label="Version">
            <input id="pol-version" value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })} required />
          </Field>
          <Field id="pol-title" label="Title">
            <input id="pol-title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} required />
          </Field>
          <Field id="pol-required" label="Required categories (comma-separated)">
            <input id="pol-required" value={form.required} onChange={(e) => setForm({ ...form, required: e.target.value })} />
          </Field>
          <Field id="pol-doc" label="Document text (test fixture — not legal text)">
            <textarea id="pol-doc" value={form.document_text} onChange={(e) => setForm({ ...form, document_text: e.target.value })} required />
          </Field>
          <button type="submit" disabled={busy}>{busy ? 'Publishing…' : 'Publish policy'}</button>
        </form>
      </Section>
    </>
  )
}

function EarlyAccessAdmin() {
  const [filter, setFilter] = useState('pending')
  const { data, error, reload } = useAsync(
    () => api.getEarlyAccessRequests(filter === 'all' ? undefined : filter),
    [filter],
  )
  const { busy, message, run } = useAction()

  async function act<T>(fn: () => Promise<T>, ok: string | ((result: T) => string)) {
    if (await run(fn, ok)) reload()
  }

  return (
    <Section title="Early-access requests">
      {message && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
      <p className="hint">
        Public access requests await review here. Approving creates and sends an invitation to the
        stored email — no retyping. Requesting access never auto-creates an account or grants access.
      </p>
      <div className="filter-row" role="group" aria-label="Filter by status">
        {['pending', 'waitlisted', 'approved', 'rejected', 'all'].map((s) => (
          <button
            key={s}
            type="button"
            className={filter === s ? 'chip active' : 'chip'}
            onClick={() => setFilter(s)}
          >
            {s}
            {data?.counts && s !== 'all' ? ` (${data.counts[s] ?? 0})` : ''}
          </button>
        ))}
      </div>
      <DataView data={data} error={error} what="requests" isEmpty={(d) => d.requests.length === 0}
        empty="No requests in this view.">
        {(d) => (
          <table>
            <thead><tr>
              <th scope="col">Email</th><th scope="col">Status</th><th scope="col">Requested</th>
              <th scope="col">Invitation</th><th scope="col">Actions</th>
            </tr></thead>
            <tbody>
              {d.requests.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.email}</td>
                  <td><Badge kind={stateKind(r.status)}>{r.status}</Badge></td>
                  <td>{r.created_at ? new Date(r.created_at).toLocaleString() : '—'}</td>
                  <td>{r.invitation_id
                    ? <Badge kind={stateKind(r.invitation_status ?? '')}>{r.invitation_status ?? 'sent'}</Badge>
                    : '—'}</td>
                  <td>
                    {(r.status === 'pending' || r.status === 'waitlisted') && (
                      <>
                        <button type="button" disabled={busy}
                          onClick={() => act(() => api.approveEarlyAccess(r.id),
                            `Invitation sent to ${r.email}.`)}>Approve &amp; invite</button>{' '}
                        <button type="button" className="btn-secondary" disabled={busy}
                          onClick={() => act(() => api.waitlistEarlyAccess(r.id), 'Wait-listed.')}>Wait-list</button>{' '}
                        <button type="button" className="btn-secondary" disabled={busy}
                          onClick={() => act(() => api.rejectEarlyAccess(r.id), 'Rejected.')}>Reject</button>
                      </>
                    )}
                    {r.status === 'approved' && r.invitation_status !== 'accepted' && (
                      <button type="button" className="btn-secondary" disabled={busy}
                        onClick={() => act(
                          () => api.resendEarlyAccessInvitation(r.id),
                          (out) => `Invitation re-sent to ${out.email}. New link expires ${new Date(out.expires_at).toLocaleString()}.`,
                        )}>
                        Resend invite</button>
                    )}
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

function InvitationsAdmin() {
  const { data, error, reload } = useAsync(() => api.getInvitations(), [])
  const { busy, message, run } = useAction()
  const [form, setForm] = useState({ email: '', proposed_tenant_name: '', role: 'tenant_admin', note: '' })
  const [created, setCreated] = useState<{ link: string } | null>(null)
  const [filter, setFilter] = useState('all')
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setCreated(null)
    const ok = await run(async () => {
      const inv = await api.createInvitation({
        email: form.email,
        role: form.role,
        proposed_tenant_name: form.proposed_tenant_name || undefined,
        note: form.note || undefined,
      })
      // The customer-facing acceptance link ALWAYS targets the canonical customer origin (www),
      // never this admin origin — admins may create invitations from admin.aithernet.online.
      setCreated({ link: `https://www.aithernet.online/app/accept-invite?token=${inv.token ?? ''}` })
    }, 'Invitation created — copy the one-time link below.')
    if (ok) reload()
  }
  return (
    <>
      <Section title="Create invitation">
        {message && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
        <form onSubmit={submit} aria-label="Create invitation">
          <Field id="inv-email" label="Invited email">
            <input id="inv-email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
          </Field>
          <Field id="inv-tenant" label="Proposed tenant name (new tenant)">
            <input id="inv-tenant" value={form.proposed_tenant_name} onChange={(e) => setForm({ ...form, proposed_tenant_name: e.target.value })} />
          </Field>
          <Field id="inv-role" label="Role">
            <select id="inv-role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
              <option value="tenant_admin">tenant_admin</option>
              <option value="tenant_operator">tenant_operator</option>
              <option value="tenant_viewer">tenant_viewer</option>
            </select>
          </Field>
          <Field id="inv-note" label="Note (optional)">
            <input id="inv-note" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
          </Field>
          <button type="submit" disabled={busy}>{busy ? 'Creating…' : 'Create invitation'}</button>
        </form>
        {created && (
          <div className="onetime">
            <p><strong>One-time invitation link</strong> (shown once; never stored in your browser):</p>
            <code className="mono break">{created.link}</code> <CopyButton value={created.link} label="Copy link" />
          </div>
        )}
      </Section>
      <Section
        title="Invitations"
        actions={
          <select aria-label="Status filter" value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="all">all</option>
            <option value="pending">pending</option>
            <option value="accepted">accepted</option>
            <option value="revoked">revoked</option>
          </select>
        }
      >
        <DataView data={data} error={error} what="invitations" isEmpty={(d) => d.length === 0}>
          {(invites) => {
            const rows = invites.filter((i) => filter === 'all' || i.status === filter)
            if (rows.length === 0) return <p className="state state-empty">No invitations match this filter.</p>
            return (
              <table>
                <thead>
                  <tr>
                    <th scope="col">Email</th>
                    <th scope="col">Tenant</th>
                    <th scope="col">Role</th>
                    <th scope="col">Status</th>
                    <th scope="col">Uses</th>
                    <th scope="col">Expires</th>
                    <th scope="col" />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((i) => (
                    <tr key={i.invitation_id}>
                      <td>{i.email}</td>
                      <td>{i.tenant_id ?? '(new)'}</td>
                      <td>{i.role}</td>
                      <td><Badge kind={stateKind(i.status)}>{i.status}</Badge></td>
                      <td>{i.used_count}/{i.maximum_uses}</td>
                      <td>{i.expires_at?.slice(0, 10)}</td>
                      <td>
                        {i.status === 'pending' && (
                          <ConfirmButton onConfirm={() => void run(() => api.revokeInvitation(i.invitation_id).then(reload), 'Invitation revoked.')}>
                            Revoke
                          </ConfirmButton>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          }}
        </DataView>
      </Section>
    </>
  )
}

function EmailPreviewAdmin() {
  const { data, error } = useAsync(() => api.getEmailPreview(), [])
  return (
    <Section title="Development email preview">
      <DataView data={data} error={error} what="email previews">
        {(d) =>
          !d.enabled ? (
            <p className="state state-empty">
              The development email sink is available only in development mode. This deployment uses
              a real email provider, so no local previews are shown here.
            </p>
          ) : d.messages.length === 0 ? (
            <p className="state state-empty">No development emails captured yet.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th scope="col">To</th>
                  <th scope="col">Type</th>
                  <th scope="col">Subject</th>
                  <th scope="col">When</th>
                  <th scope="col">Link</th>
                </tr>
              </thead>
              <tbody>
                {d.messages.map((m) => (
                  <tr key={m.id}>
                    <td>{m.to}</td>
                    <td><Badge kind="info">{m.kind}</Badge></td>
                    <td>{m.subject}</td>
                    <td>{m.created_at?.slice(0, 19).replace('T', ' ')}</td>
                    <td className="row-actions">
                      {m.link ? (
                        <>
                          <a className="btn-secondary" href={m.link.replace(window.location.origin, '')}>Open</a>
                          <CopyButton value={m.link} label="Copy link" />
                        </>
                      ) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        }
      </DataView>
    </Section>
  )
}

function TenantsUsersAdmin() {
  const tenants = useAsync(() => api.getTenants(), [])
  const users = useAsync(() => api.getUsers(), [])
  return (
    <>
      <Section title="Tenants">
        <DataView data={tenants.data} error={tenants.error} what="tenants" isEmpty={(d) => d.length === 0}>
          {(rows) => (
            <table>
              <thead><tr><th scope="col">Tenant</th><th scope="col">Name</th><th scope="col">Kind</th><th scope="col">Status</th><th scope="col">Created</th></tr></thead>
              <tbody>
                {rows.map((t) => (
                  <tr key={t.tenant_id}>
                    <td className="mono">{t.tenant_id}</td><td>{t.name}</td><td>{t.kind}</td>
                    <td><Badge kind={stateKind(t.status)}>{t.status}</Badge></td>
                    <td>{t.created_at?.slice(0, 10)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
      </Section>
      <Section title="Users & memberships">
        <DataView data={users.data} error={users.error} what="users" isEmpty={(d) => d.length === 0}>
          {(rows) => (
            <table>
              <thead><tr><th scope="col">Email</th><th scope="col">Platform admin</th><th scope="col">State</th><th scope="col">Memberships</th><th scope="col">Created</th></tr></thead>
              <tbody>
                {rows.map((u) => (
                  <tr key={u.user_id}>
                    <td>{u.email}</td>
                    <td>{u.is_platform_admin ? <Badge kind="ok">yes</Badge> : 'no'}</td>
                    <td>{u.disabled ? <Badge kind="danger">disabled</Badge> : <Badge kind="ok">active</Badge>}</td>
                    <td>{u.memberships.length === 0 ? '—' : u.memberships.map((m) => `${m.tenant_id}:${m.role}`).join(', ')}</td>
                    <td>{u.created_at?.slice(0, 10)}</td>
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

function FleetAdmin() {
  const summary = useAsync(() => api.getFleetSummary(), [])
  const { data, error, reload } = useAsync(() => api.getFleetNodes(), [])
  const { message, run } = useAction()
  return (
    <Section title="Fleet">
      {message && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
      {summary.data && (
        <div className="stat-grid small">
          <StatCard label="Total" value={summary.data.total} />
          {Object.entries(summary.data.by_state).map(([k, v]) => (
            <StatCard key={k} label={k} value={v} />
          ))}
        </div>
      )}
      <DataView data={data} error={error} what="nodes" isEmpty={(d) => d.length === 0}>
        {(nodes) => (
          <table>
            <thead>
              <tr>
                <th scope="col">Node</th><th scope="col">Tenant</th><th scope="col">State</th>
                <th scope="col">Version</th><th scope="col">Channel</th><th scope="col">Last seen</th>
                <th scope="col">Hardware</th><th scope="col">Export</th><th scope="col" />
              </tr>
            </thead>
            <tbody>
              {nodes.map((n) => (
                <tr key={n.hosted_node_id}>
                  <td>{n.display_name || n.node_id}</td>
                  <td className="mono">{n.tenant_id}</td>
                  <td><Badge kind={stateKind(n.state)}>{n.state}</Badge></td>
                  <td>{n.software_version ?? '—'}</td>
                  <td>{n.release_channel}</td>
                  <td>{n.last_heartbeat_at?.slice(0, 19).replace('T', ' ') ?? 'never'}</td>
                  <td>{n.hardware_families.length ? n.hardware_families.join(', ') : '—'} ({n.device_count})</td>
                  <td>{n.data_export_state ?? '—'}</td>
                  <td>
                    {n.status === 'revoked' ? (
                      <ConfirmButton confirmLabel="Restore" onConfirm={() => void run(() => api.restoreNode(n.hosted_node_id).then(reload), 'Node restored.')}>Restore</ConfirmButton>
                    ) : (
                      <ConfirmButton onConfirm={() => void run(() => api.revokeNode(n.hosted_node_id).then(reload), 'Node revoked.')}>Revoke</ConfirmButton>
                    )}
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

function ReleasesAdmin() {
  const { data, error, reload } = useAsync(() => api.getReleases(), [])
  const { busy, message, run } = useAction()
  const [form, setForm] = useState({ version: '', channel: 'early-access', notes: '' })
  async function create(e: React.FormEvent) {
    e.preventDefault()
    const ok = await run(() => api.createRelease(form.version, form.channel, form.notes), 'Draft created.')
    if (ok) reload()
  }
  const act = (fn: () => Promise<unknown>, ok: string) => void run(() => fn().then(reload), ok)
  return (
    <>
      <Section title="Create release draft">
        {message && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
        <form onSubmit={create} aria-label="Create release">
          <Field id="rel-version" label="Version"><input id="rel-version" value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })} required /></Field>
          <Field id="rel-channel" label="Channel">
            <select id="rel-channel" value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })}>
              <option>development</option><option>early-access</option><option>stable</option>
            </select>
          </Field>
          <Field id="rel-notes" label="Release notes"><textarea id="rel-notes" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} /></Field>
          <button type="submit" disabled={busy}>{busy ? 'Creating…' : 'Create draft'}</button>
        </form>
        <p className="hint">Signing requires a configured release signing key; publishing is blocked until a release is signed.</p>
      </Section>
      <Section title="Releases">
        <DataView data={data} error={error} what="releases" isEmpty={(d) => d.length === 0}>
          {(releases) => (
            <table>
              <thead><tr><th scope="col">Version</th><th scope="col">Channel</th><th scope="col">Status</th><th scope="col">Signing key</th><th scope="col">Artifacts</th><th scope="col" /></tr></thead>
              <tbody>
                {releases.map((r) => (
                  <tr key={r.release_id}>
                    <td>{r.version}</td>
                    <td>{r.channel}</td>
                    <td><Badge kind={stateKind(r.status)}>{r.status}</Badge></td>
                    <td className="mono">{r.signing_key_id ?? '—'}</td>
                    <td>{r.artifacts.map((a) => a.name).join(', ') || '—'}</td>
                    <td className="row-actions">
                      {(r.status === 'draft' || r.status === 'candidate') && (
                        <button type="button" className="btn-secondary" onClick={() => act(() => api.signRelease(r.release_id), 'Signed.')}>Sign</button>
                      )}
                      {r.status === 'candidate' && (
                        <button type="button" onClick={() => act(() => api.publishRelease(r.release_id), 'Published.')}>Publish</button>
                      )}
                      {r.status === 'published' && (
                        <ConfirmButton onConfirm={() => act(() => api.revokeRelease(r.release_id), 'Revoked.')}>Revoke</ConfirmButton>
                      )}
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

function SupportAdmin() {
  const { data, error, reload } = useAsync(() => api.getSupportCases(), [])
  const [open, setOpen] = useState<SupportCaseDetail | null>(null)
  const { busy, message, run } = useAction()
  const [reply, setReply] = useState('')
  async function openCase(id: string) {
    setOpen(await api.getSupportCase(id))
  }
  async function post() {
    if (!open) return
    const ok = await run(() => api.addSupportMessage(open.case_id, reply), 'Response posted.')
    if (ok) {
      setReply('')
      setOpen(await api.getSupportCase(open.case_id))
    }
  }
  return (
    <>
      <Section title="Support cases">
        <DataView data={data} error={error} what="support cases" isEmpty={(d) => d.length === 0}>
          {(cases) => (
            <table>
              <thead><tr><th scope="col">Subject</th><th scope="col">Tenant</th><th scope="col">Status</th><th scope="col">Created</th><th scope="col" /></tr></thead>
              <tbody>
                {cases.map((c) => (
                  <tr key={c.case_id}>
                    <td>{c.subject}</td>
                    <td className="mono">{c.tenant_id}</td>
                    <td><Badge kind={stateKind(c.status)}>{c.status}</Badge></td>
                    <td>{c.created_at?.slice(0, 10)}</td>
                    <td><button type="button" className="btn-secondary" onClick={() => void openCase(c.case_id)}>Open</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
      </Section>
      {open && (
        <Section
          title={`Case: ${open.subject}`}
          actions={
            <ConfirmButton confirmLabel="Resolve" onConfirm={() => void run(() => api.resolveSupportCase(open.case_id).then(() => reload()), 'Case resolved.')}>
              Mark resolved
            </ConfirmButton>
          }
        >
          {message && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
          <ul className="thread">
            {open.messages.length === 0 && <li className="state state-empty">No messages yet.</li>}
            {open.messages.map((m, i) => (
              <li key={i}><Badge kind={m.author_role === 'support' ? 'info' : 'neutral'}>{m.author_role}</Badge> {m.body} <span className="hint">{m.created_at?.slice(0, 19).replace('T', ' ')}</span></li>
            ))}
          </ul>
          <Field id="sup-reply" label="Operator response">
            <textarea id="sup-reply" value={reply} onChange={(e) => setReply(e.target.value)} />
          </Field>
          <button type="button" disabled={busy || !reply} onClick={() => void post()}>Post response</button>
        </Section>
      )}
    </>
  )
}

function AuditAdmin() {
  const { data, error } = useAsync(() => api.getAuditRecords(), [])
  const [q, setQ] = useState('')
  return (
    <Section
      title="Audit records"
      actions={<input aria-label="Filter audit" placeholder="filter action/tenant" value={q} onChange={(e) => setQ(e.target.value)} />}
    >
      <DataView data={data} error={error} what="audit records" isEmpty={(d) => d.length === 0}>
        {(records) => {
          const rows = records.filter((r) => !q || `${r.action} ${r.tenant_id ?? ''} ${r.target ?? ''}`.includes(q))
          return (
            <table>
              <thead><tr><th scope="col">When</th><th scope="col">Action</th><th scope="col">Actor</th><th scope="col">Tenant</th><th scope="col">Target</th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td>{r.created_at?.slice(0, 19).replace('T', ' ')}</td>
                    <td>{r.action}</td>
                    <td>{r.actor_kind}</td>
                    <td className="mono">{r.tenant_id ?? '—'}</td>
                    <td>{r.target ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        }}
      </DataView>
    </Section>
  )
}

function IngestionAdmin() {
  const { data, error } = useAsync(() => api.getIngestionStatus(), [])
  return (
    <Section title="Ingestion & datasets">
      <DataView data={data} error={error} what="ingestion status">
        {(d) => (
          <>
            <p>Ingestion service: <Badge kind={d.ready ? 'ok' : 'danger'}>{d.ready ? 'ready' : 'unavailable'}</Badge></p>
            <div className="stat-grid small">
              <StatCard label="Indexed batches" value={d.batches ?? '—'} />
              <StatCard label="Delivery failures" value="0" hint="bounded summary" />
              <StatCard label="Deletion requests" value="0" hint="bounded summary" />
            </div>
            <p className="hint">Dataset versioning and lineage are maintained in the ingestion service; raw training records are never exposed here.</p>
          </>
        )}
      </DataView>
    </Section>
  )
}

function DriveAdmin() {
  const { data, error } = useAsync(() => api.getDriveStatus(), [])
  return (
    <Section title="Google Drive archive status">
      <DataView data={data} error={error} what="Drive status">
        {(d) => (
          <table>
            <tbody>
              <tr><th scope="row">Configured</th><td>{String(d.configured)}</td></tr>
              <tr><th scope="row">Enabled</th><td>{String(d.enabled)}</td></tr>
              <tr><th scope="row">Credential present</th><td>{String(d.credential_present)}</td></tr>
              <tr><th scope="row">Last successful archive</th><td>{d.last_successful_archive_at ?? '—'}</td></tr>
              <tr><th scope="row">Failure category</th><td>{d.failure_category ?? '—'}</td></tr>
              <tr><th scope="row">Pending archives</th><td>{d.pending_archive_count}</td></tr>
            </tbody>
          </table>
        )}
      </DataView>
      <p className="hint">OAuth tokens, credential paths, encryption keys and folder contents are never shown.</p>
    </Section>
  )
}

function ResearchAdmin() {
  const status = useAsync(() => api.getResearchAdminStatus(), [])
  const drive = useAsync(() => api.getOwnerDriveStatus(), [])
  const packages = useAsync(() => api.getResearchPackages(), [])
  const quarantine = useAsync(() => api.getResearchQuarantine(), [])
  const jobs = useAsync(() => api.getResearchJobs(), [])
  const { busy, message, run } = useAction()
  const { query } = useRouter()
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [form, setForm] = useState({ refresh_token: '', account_label: '', root_folder_id: '' })
  const [label, setLabel] = useState('')

  // Result of the OAuth round-trip: the callback redirects back to /admin/research?drive=… .
  const driveResult = query.get('drive')
  const driveReason = query.get('reason')

  // Start the server-side OAuth flow: the browser navigates to Google's consent screen. The
  // refresh token never touches the browser — the callback stores it server-side.
  async function beginConnect() {
    try {
      const res = await api.startOwnerDriveOauth(label || undefined)
      if (res?.authorize_url && typeof window !== 'undefined') {
        window.location.assign(res.authorize_url)
      }
    } catch (e) {
      const err = e as { message?: string }
      // Surface a bounded error (e.g. not configured) without leaking anything.
      void run(() => Promise.reject(new Error(err.message ?? 'Could not start Google sign-in.')), 'Started.')
    }
  }

  async function connectManual(e: React.FormEvent) {
    e.preventDefault()
    const ok = await run(
      () =>
        api.connectOwnerDrive({
          refresh_token: form.refresh_token,
          account_label: form.account_label || undefined,
          root_folder_id: form.root_folder_id || undefined,
        }),
      'Owner Drive connected.',
    )
    if (ok) {
      // Never keep the refresh token in memory once it has been sent.
      setForm({ refresh_token: '', account_label: '', root_folder_id: '' })
      drive.reload()
      status.reload()
    }
  }

  function disconnect() {
    void run(() => api.disconnectOwnerDrive().then(() => { drive.reload(); status.reload() }), 'Owner Drive disconnected.')
  }

  function processJobs() {
    void run(
      () => api.processResearchJobs(),
      (out) => `Archive jobs processed — ${out.synced} synced, ${out.failed} failed, ${out.skipped} skipped.`,
    ).then((ok) => {
      if (ok) {
        status.reload()
        jobs.reload()
        packages.reload()
        drive.reload()
      }
    })
  }

  return (
    <>
      <Section
        title="Owner Drive connection"
        actions={
          drive.data?.connected ? (
            <ConfirmButton confirmLabel="Disconnect" onConfirm={disconnect}>Disconnect Drive</ConfirmButton>
          ) : undefined
        }
      >
        {driveResult === 'connected' && (
          <StatusBanner kind="success">Owner Google Drive connected. Queued archive jobs will sync automatically.</StatusBanner>
        )}
        {driveResult === 'error' && (
          <StatusBanner kind="error">Google Drive connection did not complete{driveReason ? ` (${driveReason})` : ''}. Please try again.</StatusBanner>
        )}
        {message && message.text && <StatusBanner kind={message.kind}>{message.text}</StatusBanner>}
        <DataView data={drive.data} error={drive.error} what="Drive status">
          {(d) => (
            <>
              <table>
                <tbody>
                  <tr>
                    <th scope="row">Status</th>
                    <td><Badge kind={driveBadgeKind(d.status)}>{d.status}</Badge></td>
                  </tr>
                  <tr><th scope="row">Account</th><td>{d.account_email ?? d.account_label ?? '—'}</td></tr>
                  <tr><th scope="row">Root folder</th><td>{d.root_folder_name ?? '—'}{d.root_folder_id ? <span className="mono"> ({d.root_folder_id})</span> : null}</td></tr>
                  <tr><th scope="row">Scope</th><td className="mono">{d.scope ?? '—'}</td></tr>
                  <tr><th scope="row">Last sync</th><td>{d.last_sync_at?.slice(0, 19).replace('T', ' ') ?? '—'}</td></tr>
                  <tr><th scope="row">Jobs</th><td>{d.queued_jobs} queued · {d.synced_jobs} synced · {d.failed_jobs} failed</td></tr>
                  {d.error_category && (
                    <tr><th scope="row">Attention</th><td><Badge kind="warn">{d.error_category}</Badge> reconnect required</td></tr>
                  )}
                </tbody>
              </table>

              {!d.oauth_configured ? (
                <div className="banner banner-info" role="status">
                  <p><strong>Google Drive OAuth is not configured on the server.</strong></p>
                  <p>An operator must set <code>GOOGLE_OAUTH_CLIENT_ID</code>, <code>GOOGLE_OAUTH_CLIENT_SECRET</code> and
                    <code> GOOGLE_OAUTH_REDIRECT_URI</code> in the hosted environment and register the redirect URI in the
                    Google Cloud console. No secret is shown here. Until then, client uploads keep succeeding and archive
                    jobs stay queued.</p>
                </div>
              ) : !d.connected ? (
                <>
                  <Field id="drive-connect-label" label="Account label / hint (optional)">
                    <input id="drive-connect-label" value={label} onChange={(e) => setLabel(e.target.value)}
                      placeholder="owner@your-domain.com" />
                  </Field>
                  <button type="button" disabled={busy} onClick={beginConnect}>Connect Google Drive</button>
                  <p className="hint">Opens Google’s consent screen. The app requests the least-privilege
                    <code> drive.file</code> scope and manages only the “Aithernet Research Archive” folder it creates.
                    The refresh token is stored on the server and never shown in your browser.</p>
                </>
              ) : (
                <p className="hint">Connected via {d.connected_via ?? 'oauth'}. Archive jobs for all consenting nodes
                  sync automatically — you do not reconnect per node or per mission.</p>
              )}

              {d.oauth_configured && d.status === 'error' && (
                <button type="button" disabled={busy} onClick={beginConnect}>Reconnect Google Drive</button>
              )}
            </>
          )}
        </DataView>

        <details className="advanced" open={showAdvanced}>
          <summary onClick={() => setShowAdvanced((v) => !v)}>Advanced: connect with a refresh token</summary>
          <form onSubmit={connectManual} aria-label="Connect owner Drive">
            <p className="hint">For out-of-band provisioning only. The normal path is “Connect Google Drive” above.</p>
            <Field id="drive-refresh" label="Google OAuth refresh token (write-only — never displayed back)">
              <textarea
                id="drive-refresh"
                value={form.refresh_token}
                onChange={(e) => setForm({ ...form, refresh_token: e.target.value })}
              />
            </Field>
            <Field id="drive-label" label="Account label (optional)">
              <input id="drive-label" value={form.account_label} onChange={(e) => setForm({ ...form, account_label: e.target.value })} />
            </Field>
            <Field id="drive-root" label="Root folder id (optional)">
              <input id="drive-root" value={form.root_folder_id} onChange={(e) => setForm({ ...form, root_folder_id: e.target.value })} />
            </Field>
            <button type="submit" disabled={busy || !form.refresh_token}>Connect with refresh token</button>
          </form>
        </details>
        <p className="hint">OAuth secrets, refresh/access tokens and folder contents are never displayed here.</p>
      </Section>

      <Section
        title="Research archive status"
        actions={<button type="button" disabled={busy} onClick={processJobs}>Process archive jobs</button>}
      >
        <DataView data={status.data} error={status.error} what="research status">
          {(s) => (
            <div className="stat-grid small">
              <StatCard label="Packages accepted" value={s.packages_accepted} />
              <StatCard label="Quarantined" value={s.quarantined} />
              <StatCard label="Jobs queued" value={s.archive_jobs_queued} />
              <StatCard label="Jobs synced" value={s.archive_jobs_synced} />
              <StatCard label="Jobs failed" value={s.archive_jobs_failed} />
              <StatCard label="Active capabilities" value={s.capabilities_active} />
            </div>
          )}
        </DataView>
      </Section>

      <Section title="Received packages">
        <DataView data={packages.data} error={packages.error} what="packages" isEmpty={(d) => d.length === 0}>
          {(rows) => (
            <table>
              <thead><tr>
                <th scope="col">Node</th><th scope="col">Tenant</th><th scope="col">Records</th>
                <th scope="col">Size</th><th scope="col">Status</th><th scope="col">Release</th>
                <th scope="col">Digest</th><th scope="col">Received</th>
              </tr></thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.package_id}>
                    <td className="mono">{p.node_id}</td>
                    <td className="mono">{p.tenant_id}</td>
                    <td>{p.record_count}</td>
                    <td>{(p.byte_size / 1024).toFixed(1)} KiB</td>
                    <td><Badge kind={stateKind(p.status)}>{p.status}</Badge></td>
                    <td>{p.release_version ?? '—'}</td>
                    <td className="mono">{p.package_sha256?.replace('sha256:', '').slice(0, 16)}…</td>
                    <td>{p.received_at?.slice(0, 19).replace('T', ' ') ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
      </Section>

      <Section title="Quarantine">
        <DataView data={quarantine.data} error={quarantine.error} what="quarantined items" isEmpty={(d) => d.length === 0}
          empty="Nothing quarantined.">
          {(rows) => (
            <table>
              <thead><tr><th scope="col">Category</th><th scope="col">Detail</th></tr></thead>
              <tbody>
                {rows.map((q) => (
                  <tr key={q.id}>
                    <td><Badge kind="warn">{q.reason_category}</Badge></td>
                    <td>{q.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataView>
        <p className="hint">Detected secrets are quarantined and never uploaded; only the reason
          category and a bounded detail are surfaced here — never the secret itself.</p>
      </Section>

      <Section title="Archive jobs">
        <DataView data={jobs.data} error={jobs.error} what="archive jobs" isEmpty={(d) => d.length === 0}>
          {(rows) => (
            <table>
              <thead><tr>
                <th scope="col">Job</th><th scope="col">Package</th><th scope="col">Node</th>
                <th scope="col">Status</th><th scope="col">Attempts</th><th scope="col">Drive file</th>
                <th scope="col">Last error</th>
              </tr></thead>
              <tbody>
                {rows.map((j) => (
                  <tr key={j.job_id}>
                    <td className="mono">{j.job_id}</td>
                    <td className="mono">{j.package_id}</td>
                    <td className="mono">{j.node_id}</td>
                    <td><Badge kind={stateKind(j.status)}>{j.status}</Badge></td>
                    <td>{j.attempts}</td>
                    <td className="mono">{j.drive_file_id ?? '—'}</td>
                    <td>{j.last_error_type ?? '—'}</td>
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

function SystemStatusAdmin() {
  const ready = useAsync(() => api.getReadiness(), [])
  const diag = useAsync(() => api.getDiagnostics(), [])
  const ingest = useAsync(() => api.getIngestionStatus(), [])
  return (
    <Section title="System status">
      <table>
        <thead><tr><th scope="col">Component</th><th scope="col">State</th><th scope="col">Detail</th></tr></thead>
        <tbody>
          <tr><th scope="row">Control plane</th><td><Badge kind={stateKind(ready.data?.status ?? '')}>{ready.data?.status ?? '…'}</Badge></td><td>env: {ready.data?.environment ?? '—'}</td></tr>
          <tr><th scope="row">Database</th><td><Badge kind="ok">{diag.data?.database_dialect ?? '…'}</Badge></td><td>central DB</td></tr>
          <tr><th scope="row">Object storage</th><td><Badge kind="info">{diag.data?.storage_backend ?? '…'}</Badge></td><td>blob backend</td></tr>
          <tr><th scope="row">Email delivery</th><td><Badge kind="info">{diag.data?.email_provider ?? '…'}</Badge></td><td>delivery mode</td></tr>
          <tr><th scope="row">Ingestion</th><td><Badge kind={ingest.data?.ready ? 'ok' : 'danger'}>{ingest.data?.ready ? 'ready' : 'unavailable'}</Badge></td><td>{ingest.data?.batches ?? 0} batches</td></tr>
          <tr><th scope="row">Drive archive</th><td><Badge kind="neutral">not active</Badge></td><td>not configured in this profile</td></tr>
          <tr><th scope="row">Release service</th><td><Badge kind="ok">available</Badge></td><td>{diag.data?.counts?.releases ?? 0} releases</td></tr>
        </tbody>
      </table>
    </Section>
  )
}

function renderAdminSection(path: string) {
  switch (path) {
    case '/admin/early-access': return <EarlyAccessAdmin />
    case '/admin/policies': return <PoliciesAdmin />
    case '/admin/invitations': return <InvitationsAdmin />
    case '/admin/email': return <EmailPreviewAdmin />
    case '/admin/tenants': return <TenantsUsersAdmin />
    case '/admin/fleet': return <FleetAdmin />
    case '/admin/releases': return <ReleasesAdmin />
    case '/admin/support': return <SupportAdmin />
    case '/admin/audit': return <AuditAdmin />
    case '/admin/ingestion': return <IngestionAdmin />
    case '/admin/drive': return <DriveAdmin />
    case '/admin/research': return <ResearchAdmin />
    case '/admin/status': return <SystemStatusAdmin />
    default: return <AdminOverview />
  }
}

export function AdminPortal() {
  const { session, loading } = useSession()
  const { path } = useRouter()
  if (loading) return null
  return (
    <AdminOnly session={session} fallback={<StatusBanner kind="error">Not authorized — platform administrators only.</StatusBanner>}>
      <div className="console">
        <nav className="subnav" aria-label="Admin sections">
          {ADMIN_NAV.map((n) => (
            <Link key={n.to} to={n.to}>{n.label}</Link>
          ))}
        </nav>
        <div className="console-body">{renderAdminSection(path)}</div>
      </div>
    </AdminOnly>
  )
}
