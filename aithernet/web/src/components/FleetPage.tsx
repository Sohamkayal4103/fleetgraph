// Fleet / peer view (Stage 13C, Part D). A peer list + detail with bounded operator actions.
//
// Trust (public-key identity) and application authorization (permissions) are shown and edited
// SEPARATELY (Part R.15). Health/manifest freshness come from persisted records, never a flag.
// Full public keys are never shown; only an abbreviated fingerprint. Destructive actions
// (trust, revoke, disable) require confirmation.

import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import type { FleetPeer } from '../api/types'
import { ConfirmButton } from './ConfirmButton'
import { OperatorComposer } from './OperatorComposer'
import { Badge, Empty, ErrorNote, Field, Loading, StatusCard } from './StatusCard'
import { AuthBadge, HealthBadge, IdTag, TrustBadge } from './commsUi'

const PERMISSION_LABELS: Record<string, string> = {
  may_send_requests: 'send requests',
  may_send_replies: 'send replies',
  may_receive_messages: 'receive messages',
  may_request_response: 'request response',
}

export function FleetPage() {
  const fleet = useResource(() => api.getFleet(), [])
  const [selected, setSelected] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const peers = fleet.data?.peers ?? []
  const current = peers.find((p) => p.peer_id === selected) ?? null

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setActionError(null)
    try {
      await fn()
      fleet.reload()
    } catch (err) {
      setActionError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page-grid">
      <StatusCard
        title="Fleet"
        badge={<Badge tone="muted">{peers.length} peers</Badge>}
        actions={<button onClick={fleet.reload}>Refresh</button>}
      >
        <Loading loading={fleet.loading && !fleet.data} />
        <ErrorNote error={fleet.error} />
        {fleet.data && peers.length === 0 && <Empty>No peers. Add one below.</Empty>}
        {peers.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Role</th>
                <th scope="col">Trust</th>
                <th scope="col">Health</th>
                <th scope="col">Auth</th>
                <th scope="col">Convs</th>
                <th scope="col">Waits</th>
              </tr>
            </thead>
            <tbody>
              {peers.map((p) => (
                <tr
                  key={p.peer_id}
                  className={p.peer_id === selected ? 'row-selected' : 'row-clickable'}
                  onClick={() => setSelected(p.peer_id)}
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      setSelected(p.peer_id)
                    }
                  }}
                >
                  <td>{p.name}</td>
                  <td>{p.role}</td>
                  <td><TrustBadge state={p.trust_state} /></td>
                  <td><HealthBadge health={p.transport_health} /></td>
                  <td>{authSummary(p)}</td>
                  <td>{p.conversation_count}</td>
                  <td>{p.pending_reply_waits}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <AddPeerForm onAdded={() => fleet.reload()} />
      </StatusCard>

      {current && (
        <StatusCard
          title={`Peer · ${current.name}`}
          badge={<TrustBadge state={current.trust_state} />}
        >
          <ErrorNote error={actionError} />
          <Field label="Peer id"><IdTag id={current.peer_id} label="peer" /></Field>
          <Field label="Role">{current.role}</Field>
          <Field label="Enabled">{current.enabled ? 'yes' : 'no'}</Field>
          <Field label="Fingerprint"><code className="mono">{current.fingerprint ?? '—'}</code></Field>
          <Field label="Health"><HealthBadge health={current.transport_health} /></Field>
          <Field label="Contact">
            last ok {current.last_success_at ?? '—'} · last fail {current.last_failure_at ?? '—'} ·
            failures {current.consecutive_failures}
          </Field>
          <Field label="Manifest freshness">{current.manifest_at ?? 'never'}</Field>
          <Field label="Capabilities">
            {current.capabilities ? (
              <code className="mono">{JSON.stringify(current.capabilities)}</code>
            ) : (
              <span className="muted">unknown</span>
            )}
          </Field>
          <Field label="Linkage">
            {current.conversation_count} conversations · {current.outstanding_messages} outstanding
            messages · {current.pending_reply_waits} pending reply waits
          </Field>

          <p className="field-label">Public-key trust (identity)</p>
          <div className="inline-fields">
            <ConfirmButton
              label="Trust"
              prompt="Trust this peer's public key?"
              disabled={busy || current.trust_state === 'trusted'}
              onConfirm={() => run(() => api.trustPeer(current.peer_id))}
            />
            <ConfirmButton
              label="Revoke"
              danger
              prompt="Revoke trust for this peer?"
              disabled={busy || current.trust_state === 'revoked'}
              onConfirm={() => run(() => api.revokePeer(current.peer_id))}
            />
            {current.enabled ? (
              <ConfirmButton
                label="Disable"
                danger
                prompt="Disable transport to this peer?"
                disabled={busy}
                onConfirm={() => run(() => api.disablePeer(current.peer_id))}
              />
            ) : (
              <button className="small" disabled={busy} onClick={() => run(() => api.enablePeer(current.peer_id))}>
                Enable
              </button>
            )}
            <button className="small" disabled={busy} onClick={() => run(() => api.testPeer(current.peer_id))}>
              Test health
            </button>
            <button className="small" disabled={busy} onClick={() => run(() => api.refreshPeerManifest(current.peer_id))}>
              Refresh manifest
            </button>
          </div>

          <p className="field-label">Application authorization (separate from trust)</p>
          <div className="perm-grid">
            {(Object.keys(PERMISSION_LABELS) as Array<keyof typeof PERMISSION_LABELS>).map((key) => {
              const granted = Boolean(current.permissions[key as keyof FleetPeer['permissions']])
              return (
                <label key={key} className="check-label">
                  <input
                    type="checkbox"
                    checked={granted}
                    disabled={busy}
                    onChange={(e) =>
                      run(() => api.setPeerPermissions(current.peer_id, { [key]: e.target.checked }))
                    }
                  />
                  {PERMISSION_LABELS[key]}
                </label>
              )
            })}
          </div>
        </StatusCard>
      )}

      {current && current.trust_state === 'trusted' && current.enabled &&
        current.permissions.may_receive_messages && (
        <StatusCard title={`Send to ${current.name}`}>
          <OperatorComposer peers={peers} fixedPeerId={current.peer_id} onSent={() => fleet.reload()} />
        </StatusCard>
      )}
    </div>
  )
}

function authSummary(p: FleetPeer) {
  return (
    <span className="auth-flags">
      {(Object.keys(PERMISSION_LABELS) as Array<keyof typeof PERMISSION_LABELS>).map((key) => (
        <AuthBadge
          key={key}
          label={key.replace('may_', '').replace(/_/g, ' ')}
          granted={Boolean(p.permissions[key as keyof FleetPeer['permissions']])}
        />
      ))}
    </span>
  )
}

function AddPeerForm({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState('')
  const [endpoint, setEndpoint] = useState('')
  const [nodeId, setNodeId] = useState('')
  const [publicKey, setPublicKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.createPeer({
        name,
        role: 'node',
        endpoint_url: endpoint,
        expected_node_id: nodeId,
        public_key: publicKey,
      })
      setName('')
      setEndpoint('')
      setNodeId('')
      setPublicKey('')
      onAdded()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="add-peer">
      <summary>Add peer</summary>
      <div className="stack">
        <label className="stack-label">Name<input value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="stack-label">Endpoint URL<input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="http://host:port" /></label>
        <label className="stack-label">Expected node id<input value={nodeId} onChange={(e) => setNodeId(e.target.value)} /></label>
        <label className="stack-label">Public key (base64)<input value={publicKey} onChange={(e) => setPublicKey(e.target.value)} /></label>
        <button disabled={busy} onClick={submit}>Add (untrusted)</button>
        <p className="muted small-note">A new peer is untrusted with no authorization until you trust its key and grant permissions.</p>
        <ErrorNote error={error} />
      </div>
    </details>
  )
}
