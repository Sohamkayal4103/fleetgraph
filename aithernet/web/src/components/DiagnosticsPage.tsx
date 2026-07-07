// Delivery + communication diagnostics (Stage 13C, Parts K, I, J).
//
// Outbox/inbox operational views, durable reply waits, and inbound coordinator requests — all
// read-only except bounded, confirmed operator retries/cancels. ACK is shown distinctly from a
// semantic answer; "transport authenticated" is shown distinctly from "application authorized".
// Errors are the backend's already-sanitized strings; no signatures/keys/envelopes appear.

import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import type { InboundRequest, InboxMessage, OutboxMessage, ReplyWait } from '../api/types'
import { ConfirmButton } from './ConfirmButton'
import { Badge, Empty, ErrorNote, Loading, StatusCard } from './StatusCard'
import { AckBadge, DeliveryBadge, IdTag } from './commsUi'

const RETRYABLE = new Set(['failed', 'dead_letter'])
const CANCELLABLE = new Set(['pending', 'in_flight'])

export function DiagnosticsPage() {
  const outbox = useResource(() => api.getOutbox(), [])
  const inbox = useResource(() => api.getInbox(), [])
  const waits = useResource(() => api.getReplyWaits(), [])
  const inbound = useResource(() => api.getInboundRequests(), [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (fn: () => Promise<unknown>, reload: () => void) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      reload()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page-grid">
      <StatusCard
        title="Outbox"
        badge={<Badge tone="muted">{outbox.data?.length ?? 0}</Badge>}
        actions={<button onClick={outbox.reload}>Refresh</button>}
      >
        <Loading loading={outbox.loading && !outbox.data} />
        <ErrorNote error={outbox.error ?? error} />
        {outbox.data && outbox.data.length === 0 && <Empty>Outbox empty.</Empty>}
        {outbox.data && outbox.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Message</th>
                <th scope="col">Delivery</th>
                <th scope="col">ACK</th>
                <th scope="col">Attempts</th>
                <th scope="col">Error</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {outbox.data.map((m: OutboxMessage) => (
                <tr key={m.id}>
                  <td><IdTag id={m.message_id} /></td>
                  <td><DeliveryBadge status={m.status} /></td>
                  <td><AckBadge acknowledged={Boolean(m.acknowledged_at)} /></td>
                  <td>{m.attempt_count}/{m.max_attempts}</td>
                  <td className="error-cell">{m.error_type ?? '—'}</td>
                  <td>
                    {RETRYABLE.has(m.status) && (
                      <ConfirmButton
                        label="Retry"
                        prompt="Retry delivery? (only after the underlying issue changed)"
                        disabled={busy}
                        onConfirm={() => run(() => api.retryAgentTransportMessage(m.id), outbox.reload)}
                      />
                    )}
                    {CANCELLABLE.has(m.status) && (
                      <ConfirmButton
                        label="Cancel"
                        danger
                        prompt="Cancel this pending message?"
                        disabled={busy}
                        onConfirm={() => run(() => api.cancelAgentTransportMessage(m.id), outbox.reload)}
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="muted small-note">
          Retries are transport retries — they reuse the same message id and never create a new
          mission step. A permanent cryptographic/authorization failure is not retried until the
          underlying issue changes.
        </p>
      </StatusCard>

      <StatusCard
        title="Inbox"
        badge={<Badge tone="muted">{inbox.data?.length ?? 0}</Badge>}
        actions={<button onClick={inbox.reload}>Refresh</button>}
      >
        <Loading loading={inbox.loading && !inbox.data} />
        <ErrorNote error={inbox.error} />
        {inbox.data && inbox.data.length === 0 && <Empty>Inbox empty.</Empty>}
        {inbox.data && inbox.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Message</th>
                <th scope="col">Sender</th>
                <th scope="col">Kind</th>
                <th scope="col">Dup</th>
                <th scope="col">Received</th>
              </tr>
            </thead>
            <tbody>
              {inbox.data.map((m: InboxMessage) => (
                <tr key={m.id}>
                  <td><IdTag id={m.message_id} /></td>
                  <td><code className="mono" title={m.sender_node_id}>{m.sender_node_id.slice(0, 12)}…</code></td>
                  <td>{m.kind}</td>
                  <td>{m.duplicate_count > 0 ? <Badge tone="muted">×{m.duplicate_count} (idempotent)</Badge> : '—'}</td>
                  <td className="muted small-note">{m.received_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      <StatusCard
        title="Reply waits"
        badge={<Badge tone="muted">{waits.data?.length ?? 0}</Badge>}
        actions={<button onClick={waits.reload}>Refresh</button>}
      >
        <Loading loading={waits.loading && !waits.data} />
        <ErrorNote error={waits.error} />
        {waits.data && waits.data.length === 0 && <Empty>No reply waits.</Empty>}
        {waits.data && waits.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Wait</th>
                <th scope="col">State</th>
                <th scope="col">Mission</th>
                <th scope="col">Peer</th>
                <th scope="col">Deadline</th>
                <th scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {waits.data.map((w: ReplyWait) => (
                <tr key={w.wait_id}>
                  <td><IdTag id={w.wait_id} /></td>
                  <td><WaitStateBadge state={w.state} /></td>
                  <td><IdTag id={w.mission_id} /></td>
                  <td><IdTag id={w.expected_peer_id} /></td>
                  <td className="muted small-note">{w.deadline ?? '—'}</td>
                  <td>
                    {w.state === 'pending' && (
                      <ConfirmButton
                        label="Cancel wait"
                        danger
                        prompt="Cancel this pending wait? A later reply cannot resume the mission."
                        disabled={busy}
                        onConfirm={() => run(() => api.cancelReplyWait(w.mission_id, w.wait_id), waits.reload)}
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="muted small-note">
          A wait is satisfied only by an authenticated, correlated semantic reply — never by the
          UI, and never by a transport ACK.
        </p>
      </StatusCard>

      <StatusCard
        title="Inbound requests"
        badge={<Badge tone="muted">{inbound.data?.length ?? 0}</Badge>}
        actions={<button onClick={inbound.reload}>Refresh</button>}
      >
        <Loading loading={inbound.loading && !inbound.data} />
        <ErrorNote error={inbound.error} />
        <p className="muted small-note">
          Transport authenticated does NOT necessarily mean application authorized — an
          authenticated request with no authorization creates no mission.
        </p>
        {inbound.data && inbound.data.length === 0 && <Empty>No inbound requests.</Empty>}
        {inbound.data && inbound.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Request</th>
                <th scope="col">Peer</th>
                <th scope="col">State</th>
                <th scope="col">Mission</th>
                <th scope="col">Response</th>
              </tr>
            </thead>
            <tbody>
              {inbound.data.map((r: InboundRequest) => (
                <tr key={r.request_id}>
                  <td><IdTag id={r.request_id} /></td>
                  <td><IdTag id={r.peer_id} /></td>
                  <td><InboundStateBadge req={r} /></td>
                  <td>{r.mission_id ? <IdTag id={r.mission_id} /> : <Badge tone="muted">no mission</Badge>}</td>
                  <td>
                    {r.response_required ? (
                      <Badge tone={r.response_queued ? 'ok' : 'warn'}>
                        {r.response_queued ? 'response queued' : 'response required'}
                      </Badge>
                    ) : (
                      <span className="muted">not required</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>
    </div>
  )
}

function WaitStateBadge({ state }: { state: string }) {
  const tone = state === 'satisfied' ? 'ok' : state === 'timed_out' ? 'bad' : state === 'cancelled' ? 'muted' : 'warn'
  return <Badge tone={tone}>{state}</Badge>
}

function InboundStateBadge({ req }: { req: InboundRequest }) {
  if (req.rejection_reason) return <Badge tone="bad">rejected</Badge>
  if (req.state === 'mission_created' || req.mission_id) return <Badge tone="ok">authorized</Badge>
  return <Badge tone="muted">{req.state}</Badge>
}
