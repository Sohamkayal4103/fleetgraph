// Stage 13A minimal transport-infrastructure panel: local identity, transport-worker status,
// trusted peers, outbox counts, inbox count, recent delivery failures, and basic operator
// actions (add/trust/revoke/test/refresh-manifest peers, send a basic message, retry a failed
// one). This is deliberately small — it inspects 13A infrastructure only and never wires the
// coordinator or mission worker into these controls (that is Stage 13B/13C). Polling here only
// reads; it never starts a worker or triggers a send.
import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import type { Peer } from '../api/types'
import { Badge, ErrorNote, StatusCard } from './StatusCard'

function trustTone(state: string) {
  if (state === 'trusted') return 'ok' as const
  if (state === 'revoked') return 'bad' as const
  return 'warn' as const
}

export function TransportPanel({ onMutate }: { onMutate?: () => void }) {
  const [refreshKey, setRefreshKey] = useState(0)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [form, setForm] = useState({ name: '', endpoint: '', nodeId: '', publicKey: '' })
  const [msg, setMsg] = useState({ peerId: '', text: '' })

  const bump = () => {
    setRefreshKey((k) => k + 1)
    onMutate?.()
  }

  const status = useResource(() => api.getTransportStatus(), [refreshKey])
  const peers = useResource(() => api.getPeers(), [refreshKey])
  const outbox = useResource(() => api.getOutbox(), [refreshKey])

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setActionError(null)
    try {
      await fn()
      bump()
    } catch (err) {
      setActionError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const identity = status.data?.identity
  const worker = status.data?.worker
  const failures = (outbox.data ?? []).filter(
    (m) => m.status === 'failed' || m.status === 'dead_letter',
  )

  return (
    <StatusCard
      title="Agent Transport (Stage 13A)"
      badge={
        identity?.initialized ? (
          <Badge tone="ok">identity ready</Badge>
        ) : (
          <Badge tone="warn">no identity</Badge>
        )
      }
      actions={<button onClick={bump} disabled={busy}>Refresh</button>}
    >
      <ErrorNote error={actionError ?? status.error} />

      {/* Identity + worker summary */}
      <div className="kv">
        <div>
          <span className="muted">Fingerprint</span>
          <code>{identity?.fingerprint_short ?? '—'}</code>
        </div>
        <div>
          <span className="muted">Worker</span>
          {worker ? (
            <Badge tone={worker.running ? (worker.degraded ? 'warn' : 'ok') : 'muted'}>
              {worker.running ? (worker.degraded ? 'degraded' : 'running') : 'stopped'}
            </Badge>
          ) : (
            '—'
          )}
        </div>
        <div>
          <span className="muted">Peers</span>
          {status.data ? `${status.data.peer_count} (${status.data.trusted_peer_count} trusted)` : '—'}
        </div>
        <div>
          <span className="muted">Inbox</span>
          {status.data?.inbox_count ?? '—'}
        </div>
      </div>

      {!identity?.initialized && (
        <button disabled={busy} onClick={() => run(() => api.initializeIdentity())}>
          Initialize identity
        </button>
      )}

      <div className="muted small">Outbox: {JSON.stringify(status.data?.outbox_counts ?? {})}</div>

      {/* Peers */}
      <h3>Peers</h3>
      {(peers.data ?? []).length === 0 && <p className="muted">No peers.</p>}
      <ul className="list">
        {(peers.data ?? []).map((peer: Peer) => (
          <li key={peer.id} className="list-row">
            <div>
              <Badge tone={trustTone(peer.trust_state)}>{peer.trust_state}</Badge>{' '}
              <strong>{peer.name}</strong> <span className="muted">{peer.expected_node_id}</span>
              <div className="muted small">
                {peer.endpoint_url} · {peer.fingerprint?.slice(0, 23) ?? 'no key'}
              </div>
            </div>
            <div className="row-actions">
              {peer.trust_state !== 'trusted' && (
                <button disabled={busy} onClick={() => run(() => api.trustPeer(peer.id))}>
                  Trust
                </button>
              )}
              {peer.trust_state !== 'revoked' && (
                <button disabled={busy} onClick={() => run(() => api.revokePeer(peer.id))}>
                  Revoke
                </button>
              )}
              <button disabled={busy} onClick={() => run(() => api.testPeer(peer.id))}>
                Test
              </button>
              <button disabled={busy} onClick={() => run(() => api.refreshPeerManifest(peer.id))}>
                Manifest
              </button>
            </div>
          </li>
        ))}
      </ul>

      {/* Add peer */}
      <h3>Add peer</h3>
      <div className="form-grid">
        <input
          placeholder="name"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
        <input
          placeholder="endpoint URL"
          value={form.endpoint}
          onChange={(e) => setForm({ ...form, endpoint: e.target.value })}
        />
        <input
          placeholder="expected node id"
          value={form.nodeId}
          onChange={(e) => setForm({ ...form, nodeId: e.target.value })}
        />
        <input
          placeholder="public key (base64)"
          value={form.publicKey}
          onChange={(e) => setForm({ ...form, publicKey: e.target.value })}
        />
      </div>
      <button
        disabled={busy || !form.name}
        onClick={() =>
          run(async () => {
            await api.createPeer({
              name: form.name,
              role: 'node',
              endpoint_url: form.endpoint || null,
              expected_node_id: form.nodeId || null,
              public_key: form.publicKey || null,
            })
            setForm({ name: '', endpoint: '', nodeId: '', publicKey: '' })
          })
        }
      >
        Add peer (untrusted)
      </button>

      {/* Send a basic operator message */}
      <h3>Send message</h3>
      <div className="form-grid">
        <input
          placeholder="peer id"
          value={msg.peerId}
          onChange={(e) => setMsg({ ...msg, peerId: e.target.value })}
        />
        <input
          placeholder="text"
          value={msg.text}
          onChange={(e) => setMsg({ ...msg, text: e.target.value })}
        />
      </div>
      <button
        disabled={busy || !msg.peerId}
        onClick={() =>
          run(async () => {
            await api.sendAgentTransportMessage({ peer_id: msg.peerId, text: msg.text })
            setMsg({ peerId: '', text: '' })
          })
        }
      >
        Queue message
      </button>

      {/* Recent delivery failures */}
      {failures.length > 0 && (
        <>
          <h3>Recent delivery failures</h3>
          <ul className="list">
            {failures.slice(0, 10).map((m) => (
              <li key={m.id} className="list-row">
                <div>
                  <Badge tone="bad">{m.status}</Badge> <code>{m.message_id.slice(0, 8)}</code>
                  <span className="muted small"> {m.error_type}</span>
                </div>
                <button disabled={busy} onClick={() => run(() => api.retryAgentTransportMessage(m.id))}>
                  Retry
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </StatusCard>
  )
}
