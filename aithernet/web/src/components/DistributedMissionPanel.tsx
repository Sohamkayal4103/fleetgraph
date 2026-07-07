// Distributed mission timeline + topology (Stage 13C, Part H).
//
// Shows one mission's distributed communication: peer_message actions, transport ACK, durable
// waits, semantic replies, timeouts, resumes, inbound-request source and response obligation —
// plus a lightweight, accessible node-and-edge topology DERIVED ONLY from persisted correlation
// records. Remote mission status is shown as "unknown" because the peer never supplies it.

import { api } from '../api/client'
import { useResource } from '../api/hooks'
import type { CommMessageSummary, ReplyWait, TimelineEntry, TopologyEdge } from '../api/types'
import { Badge, Empty, ErrorNote, Field, Loading, StatusCard } from './StatusCard'
import { AckBadge, IdTag, KindLabel } from './commsUi'

export function DistributedMissionPanel({ selectedMissionId }: { selectedMissionId: string | null }) {
  const timeline = useResource(
    () => (selectedMissionId ? api.getDistributedMissionTimeline(selectedMissionId) : Promise.resolve(null)),
    [selectedMissionId],
  )
  const data = timeline.data

  return (
    <StatusCard
      title="Distributed mission"
      badge={data ? <Badge tone={data.is_inbound_mission ? 'info' : 'muted'}>{data.is_inbound_mission ? 'inbound' : 'local'}</Badge> : undefined}
      actions={selectedMissionId ? <button onClick={timeline.reload}>Refresh</button> : undefined}
    >
      {!selectedMissionId && <Empty>Select a mission to see its distributed communication.</Empty>}
      <Loading loading={timeline.loading && !data} />
      <ErrorNote error={timeline.error} />
      {data && (
        <>
          <Field label="Status">{data.mission_status}</Field>
          <Field label="Run / iteration">
            <IdTag id={data.mission_run_id} /> · iter {data.iteration_count ?? '—'}
          </Field>
          {data.response_obligation && (
            <Field label="Response obligation">
              <Badge tone={data.response_obligation.response_queued ? 'ok' : 'warn'}>
                {data.response_obligation.response_queued ? 'response queued' : 'response required'}
              </Badge>
            </Field>
          )}
          {data.inbound_request && (
            <Field label="Inbound source">
              peer <IdTag id={data.inbound_request.peer_id} /> · request{' '}
              <IdTag id={data.inbound_request.request_id} />
            </Field>
          )}

          <Topology nodes={data.topology.nodes} edges={data.topology.edges} />

          <p className="field-label">Outbound peer messages</p>
          {data.outbound_messages.length === 0 ? (
            <Empty>none</Empty>
          ) : (
            data.outbound_messages.map((m: CommMessageSummary) => (
              <div key={m.message_id} className="badge-row">
                <span className="kind-name">{m.message_type ?? 'message'}</span>
                <IdTag id={m.message_id} label="msg" />→ <IdTag id={m.peer_id} label="peer" />
                <Badge tone="muted">delivery: {m.delivery_status ?? '—'}</Badge>
                <AckBadge acknowledged={m.transport_acknowledged} />
                {m.expects_reply && <Badge tone="info">expects reply</Badge>}
              </div>
            ))
          )}

          <p className="field-label">Reply waits</p>
          {data.reply_waits.length === 0 ? (
            <Empty>none</Empty>
          ) : (
            data.reply_waits.map((w: ReplyWait) => (
              <div key={w.wait_id} className="badge-row">
                <Badge tone={w.state === 'satisfied' ? 'ok' : w.state === 'timed_out' ? 'bad' : w.state === 'cancelled' ? 'muted' : 'warn'}>
                  wait: {w.state}
                </Badge>
                <IdTag id={w.request_id} label="req" />
                {w.deadline && <span className="muted small-note">deadline {w.deadline}</span>}
              </div>
            ))
          )}

          <p className="field-label">Correlation timeline</p>
          {data.entries.length === 0 ? (
            <Empty>No distributed events yet.</Empty>
          ) : (
            <ol className="timeline">
              {data.entries.map((e: TimelineEntry, i: number) => (
                <li key={`${e.kind}-${i}`} className="timeline-row timeline-system">
                  <div className="timeline-head">
                    <KindLabel kind={e.event_type ?? e.route_target ?? e.kind} />
                    <span className="muted small-note">{e.at ?? ''}</span>
                  </div>
                  {e.message && <p className="preview-text">{e.message}</p>}
                  {e.status && <Badge tone="muted">{e.status}</Badge>}
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </StatusCard>
  )
}

function Topology({ nodes, edges }: { nodes: import('../api/types').TopologyNode[]; edges: TopologyEdge[] }) {
  if (nodes.length === 0) return null
  const labelFor = (id: string) => nodes.find((n) => n.id === id)?.label ?? id
  return (
    <figure className="topology" aria-label="Distributed mission topology">
      <figcaption className="field-label">Topology (persisted correlations only)</figcaption>
      <div className="topo-nodes" role="list">
        {nodes.map((n) => {
          // A bare peer node carries no status; a remote_mission node carries the AUTHENTICATED
          // state + freshness (Stage 13D.3) — "unknown" only when no snapshot exists.
          const freshness = (n as { freshness?: string }).freshness
          const statusText =
            n.kind === 'peer'
              ? 'peer node'
              : `${n.status ?? 'unknown'}${freshness ? ` · ${freshness}` : ''}`
          return (
            <div key={n.id} className={`topo-node topo-${n.kind}`} role="listitem">
              <span className="topo-label">{n.label}</span>
              <span className="topo-status muted">{statusText}</span>
            </div>
          )
        })}
      </div>
      <ul className="topo-edges">
        {edges.map((e, i) => (
          <li key={`${e.from}-${e.to}-${i}`}>
            <span className="mono">{labelFor(e.from)}</span>
            <span className="topo-arrow"> ──{e.kind.replace(/_/g, ' ')}
              {e.acknowledged !== undefined ? (e.acknowledged ? ' (acked)' : ' (no ack)') : ''}
              {e.satisfied !== undefined ? (e.satisfied ? ' (satisfied)' : '') : ''}→ </span>
            <span className="mono">{labelFor(e.to)}</span>
          </li>
        ))}
      </ul>
    </figure>
  )
}
