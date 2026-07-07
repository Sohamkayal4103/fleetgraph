// Stage 13B minimal communications panel. Shows conversations, inbound-request missions (with
// response-required indicator + source peer), per-mission communications (transport ACK vs
// SEMANTIC reply distinction + pending reply waits with deadline/status), and per-peer
// application-permission toggles. It is deliberately NOT the Stage 13C chat UI: no freeform
// composer beyond the existing operator messaging. Polling only reads — it never sends a
// message or resumes a mission.
import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import type { Peer } from '../api/types'
import { Badge, ErrorNote, StatusCard } from './StatusCard'

function waitTone(state: string) {
  if (state === 'satisfied') return 'ok' as const
  if (state === 'timed_out') return 'warn' as const
  if (state === 'cancelled') return 'bad' as const
  return 'info' as const
}

export function CommunicationsPanel({ selectedMissionId }: { selectedMissionId: string | null }) {
  const [refreshKey, setRefreshKey] = useState(0)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const bump = () => setRefreshKey((k) => k + 1)

  const conversations = useResource(() => api.getConversations(), [refreshKey])
  const inbound = useResource(() => api.getInboundRequests(), [refreshKey])
  const peers = useResource(() => api.getPeers(), [refreshKey])
  const comms = useResource(
    () => (selectedMissionId ? api.getMissionCommunications(selectedMissionId) : Promise.resolve(null)),
    [refreshKey, selectedMissionId],
  )

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

  const togglePermission = (peer: Peer, key: string, value: boolean) =>
    run(() => api.setPeerPermissions(peer.id, { [key]: value } as never))

  return (
    <StatusCard
      title="Peer Communications (Stage 13B)"
      badge={<Badge tone="muted">{(conversations.data ?? []).length} conversations</Badge>}
      actions={<button onClick={bump} disabled={busy}>Refresh</button>}
    >
      <ErrorNote error={actionError ?? conversations.error} />

      {/* Selected mission communications: ACK vs semantic reply + pending waits */}
      {selectedMissionId && (
        <>
          <h3>Mission communications</h3>
          {(comms.data?.outbound_messages ?? []).length === 0 && (
            <p className="muted small">No peer messages for this mission.</p>
          )}
          <ul className="list">
            {(comms.data?.outbound_messages ?? []).map((m) => (
              <li key={m.message_id} className="list-row">
                <div>
                  <code>{m.message_id.slice(0, 8)}</code> {m.message_type} →{' '}
                  <span className="muted">{(m.peer_id ?? '').slice(0, 8)}</span>
                  <div className="muted small">
                    delivery: {m.delivery_status} ·{' '}
                    {m.transport_acknowledged ? (
                      <Badge tone="ok">transport ACK</Badge>
                    ) : (
                      <Badge tone="muted">not acked</Badge>
                    )}{' '}
                    {m.expects_reply ? <Badge tone="info">awaiting semantic reply</Badge> : null}
                  </div>
                </div>
              </li>
            ))}
          </ul>
          {(comms.data?.reply_waits ?? []).length > 0 && (
            <>
              <h3>Pending reply waits</h3>
              <ul className="list">
                {(comms.data?.reply_waits ?? []).map((w) => (
                  <li key={w.wait_id} className="list-row">
                    <div>
                      <Badge tone={waitTone(w.state)}>{w.state}</Badge>{' '}
                      <code>{w.wait_id.slice(0, 8)}</code>
                      <div className="muted small">
                        deadline: {w.deadline ?? '—'} · peer {w.expected_peer_id.slice(0, 8)}
                      </div>
                    </div>
                    {w.state === 'pending' && (
                      <button
                        disabled={busy}
                        onClick={() => run(() => api.cancelReplyWait(selectedMissionId, w.wait_id))}
                      >
                        Cancel wait
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
          {comms.data?.response_obligation && (
            <p className="small">
              Response obligation:{' '}
              {comms.data.response_obligation.response_required ? (
                comms.data.response_obligation.response_queued ? (
                  <Badge tone="ok">reply queued</Badge>
                ) : (
                  <Badge tone="warn">reply required, not yet queued</Badge>
                )
              ) : (
                <Badge tone="muted">no reply required</Badge>
              )}
            </p>
          )}
        </>
      )}

      {/* Inbound-request missions */}
      <h3>Inbound coordinator requests</h3>
      {(inbound.data ?? []).length === 0 && <p className="muted small">None.</p>}
      <ul className="list">
        {(inbound.data ?? []).slice(0, 10).map((r) => (
          <li key={r.request_id} className="list-row">
            <div>
              <Badge tone={r.state === 'mission_created' ? 'ok' : 'warn'}>{r.state}</Badge>{' '}
              <code>{r.request_id.slice(0, 8)}</code> from {r.peer_id.slice(0, 8)}
              <div className="muted small">
                mission: {(r.mission_id ?? '—').slice(0, 8)} ·{' '}
                {r.response_required ? (
                  r.response_queued ? (
                    <Badge tone="ok">responded</Badge>
                  ) : (
                    <Badge tone="warn">response required</Badge>
                  )
                ) : (
                  <Badge tone="muted">no reply</Badge>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>

      {/* Peer application permissions (separate from key trust) */}
      <h3>Peer permissions</h3>
      <ul className="list">
        {(peers.data ?? []).filter((p) => p.trust_state !== 'revoked').map((p: Peer) => (
          <PeerPermissionRow key={p.id} peer={p} busy={busy} onToggle={togglePermission} />
        ))}
      </ul>
    </StatusCard>
  )
}

function PeerPermissionRow({
  peer,
  busy,
  onToggle,
}: {
  peer: Peer
  busy: boolean
  onToggle: (peer: Peer, key: string, value: boolean) => void
}) {
  const perms = useResource(() => api.getPeerPermissions(peer.id), [peer.id])
  const p = perms.data
  return (
    <li className="list-row" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
      <div>
        <strong>{peer.name}</strong> <code>{peer.id.slice(0, 8)}</code>{' '}
        <Badge tone={peer.trust_state === 'trusted' ? 'ok' : 'warn'}>{peer.trust_state}</Badge>
      </div>
      {p && (
        <div className="muted small">
          <label>
            <input
              type="checkbox"
              checked={p.may_send_requests}
              disabled={busy}
              onChange={(e) => onToggle(peer, 'may_send_requests', e.target.checked)}
            />{' '}
            may send requests
          </label>{' '}
          <label>
            <input
              type="checkbox"
              checked={p.may_request_response}
              disabled={busy}
              onChange={(e) => onToggle(peer, 'may_request_response', e.target.checked)}
            />{' '}
            may request response
          </label>{' '}
          <label>
            <input
              type="checkbox"
              checked={p.may_receive_messages}
              disabled={busy}
              onChange={(e) => onToggle(peer, 'may_receive_messages', e.target.checked)}
            />{' '}
            may receive
          </label>
        </div>
      )}
    </li>
  )
}
