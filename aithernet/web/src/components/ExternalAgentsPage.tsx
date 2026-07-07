// External Agents dashboard section (Stage 14D, gateway observation surface).
//
// Read-only operator view of external (non-Aithernet) agents: identity + status + public-key
// fingerprint + permissions, callback endpoints + verification, subscriptions, deliveries
// (pending / retrying / dead-lettered / acknowledged) and gateway diagnostics. Status is shown
// as accessible TEXT via Badge tones (never colour alone). There are NO secrets, bearer tokens,
// callback-signing keys, raw payload dumps, private paths, or arbitrary URL test buttons.
// Disruptive administrative actions are operator-gated CLI/API actions, not exposed here.

import { useState } from 'react'

import { api } from '../api/client'
import { useResource } from '../api/hooks'
import type {
  InteropAgent,
  InteropDelivery,
  InteropEndpoint,
  InteropSubscription,
} from '../api/types'
import { Badge, Empty, ErrorNote, Loading, StatusCard, type Tone } from './StatusCard'

function statusTone(s: string): Tone {
  if (s === 'active' || s === 'verified' || s === 'delivered' || s === 'acknowledged') return 'ok'
  if (s === 'disabled' || s === 'paused' || s === 'pending_verification') return 'warn'
  if (s === 'revoked' || s === 'rejected' || s === 'dead_letter') return 'bad'
  return 'info'
}

export function ExternalAgentsPage() {
  const agents = useResource(() => api.getExternalAgents(), [])
  const diagnostics = useResource(() => api.getExternalAgentsDiagnostics(), [])
  const [selected, setSelected] = useState<string | null>(null)
  const endpoints = useResource(
    () => (selected ? api.getExternalAgentEndpoints(selected) : Promise.resolve([])),
    [selected],
  )
  const subscriptions = useResource(
    () => (selected ? api.getExternalAgentSubscriptions(selected) : Promise.resolve([])),
    [selected],
  )
  const deliveries = useResource(
    () => (selected ? api.getExternalAgentDeliveries(selected) : Promise.resolve([])),
    [selected],
  )

  const d = diagnostics.data
  return (
    <div className="grid">
      <StatusCard
        title="Gateway diagnostics"
        badge={
          <Badge tone={d?.enabled ? 'ok' : 'muted'}>{d?.enabled ? 'enabled' : 'disabled'}</Badge>
        }
        actions={
          <button type="button" onClick={() => diagnostics.reload()}>
            Reload
          </button>
        }
      >
        <Loading loading={diagnostics.loading} />
        <ErrorNote error={diagnostics.error} />
        {d && (
          <div className="kv">
            <div>
              <span className="muted">Worker</span>
              <Badge tone={d.worker_running ? 'ok' : 'muted'}>
                {d.worker_running ? (d.worker_degraded ? 'degraded' : 'running') : 'stopped'}
              </Badge>
            </div>
            <div>
              <span className="muted">Callbacks</span>
              <Badge tone={d.callbacks_enabled ? 'ok' : 'muted'}>
                {d.callbacks_enabled ? 'enabled' : 'disabled'}
              </Badge>
            </div>
            <div>
              <span className="muted">WebSocket</span>
              <Badge tone={d.websocket_enabled ? 'ok' : 'muted'}>
                {d.websocket_enabled ? 'enabled' : 'disabled'} ({d.live_websocket_connections} live)
              </Badge>
            </div>
            <div>
              <span className="muted">Agents</span>
              <code>
                {d.agents_active} active / {d.agents_total} total
              </code>
            </div>
            <div>
              <span className="muted">Pending deliveries</span>
              <code>{d.pending_messages}</code>
            </div>
            <div>
              <span className="muted">Dead letters</span>
              <Badge tone={d.dead_letter_messages ? 'bad' : 'ok'}>{d.dead_letter_messages}</Badge>
            </div>
          </div>
        )}
      </StatusCard>

      <StatusCard
        title="External agents"
        badge={<Badge tone="info">{agents.data?.length ?? 0}</Badge>}
        actions={
          <button type="button" onClick={() => agents.reload()}>
            Reload
          </button>
        }
      >
        <Loading loading={agents.loading} />
        <ErrorNote error={agents.error} />
        {agents.data && agents.data.length === 0 && (
          <Empty>No external agents. Provision one with `aithernet external-agents create`.</Empty>
        )}
        {agents.data && agents.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th>Auth</th>
                <th>Fingerprint</th>
                <th>Permissions</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {agents.data.map((a: InteropAgent) => (
                <tr key={a.agent_id}>
                  <td className="mono">{a.display_name}</td>
                  <td>
                    <Badge tone={statusTone(a.status)}>{a.status}</Badge>
                  </td>
                  <td>{a.identity_type}</td>
                  <td className="mono">{a.fingerprint ?? '—'}</td>
                  <td>{a.permissions.length}</td>
                  <td>
                    <button type="button" onClick={() => setSelected(a.agent_id)}>
                      Inspect
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      {selected && (
        <StatusCard title={`Agent detail · ${selected}`}>
          <h4>Endpoints</h4>
          <Loading loading={endpoints.loading} />
          <ErrorNote error={endpoints.error} />
          {endpoints.data && endpoints.data.length === 0 && <Empty>No callback endpoints.</Empty>}
          {endpoints.data && endpoints.data.length > 0 && (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Host</th>
                  <th>Status</th>
                  <th>Verified</th>
                </tr>
              </thead>
              <tbody>
                {endpoints.data.map((e: InteropEndpoint) => (
                  <tr key={e.endpoint_id}>
                    <td className="mono">
                      {e.scheme}://{e.host}:{e.port}
                    </td>
                    <td>
                      <Badge tone={statusTone(e.status)}>{e.status}</Badge>
                    </td>
                    <td>{e.verified_at ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h4>Subscriptions</h4>
          <Loading loading={subscriptions.loading} />
          {subscriptions.data && subscriptions.data.length === 0 && (
            <Empty>No subscriptions.</Empty>
          )}
          {subscriptions.data && subscriptions.data.length > 0 && (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Mode</th>
                  <th>Status</th>
                  <th>Next seq</th>
                  <th>Acked</th>
                </tr>
              </thead>
              <tbody>
                {subscriptions.data.map((s: InteropSubscription) => (
                  <tr key={s.subscription_id}>
                    <td>{s.delivery_mode}</td>
                    <td>
                      <Badge tone={statusTone(s.status)}>{s.status}</Badge>
                    </td>
                    <td>{s.next_sequence}</td>
                    <td>{s.last_acked_sequence}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h4>Deliveries</h4>
          <Loading loading={deliveries.loading} />
          {deliveries.data && deliveries.data.length === 0 && <Empty>No deliveries.</Empty>}
          {deliveries.data && deliveries.data.length > 0 && (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Event</th>
                  <th>Status</th>
                  <th>Attempts</th>
                  <th>Seq</th>
                </tr>
              </thead>
              <tbody>
                {deliveries.data.map((m: InteropDelivery) => (
                  <tr key={m.message_pk}>
                    <td className="mono">{m.event_type}</td>
                    <td>
                      <Badge tone={statusTone(m.status)}>{m.status}</Badge>
                    </td>
                    <td>
                      {m.attempt_count}/{m.max_attempts}
                    </td>
                    <td>{m.sequence}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </StatusCard>
      )}
    </div>
  )
}
