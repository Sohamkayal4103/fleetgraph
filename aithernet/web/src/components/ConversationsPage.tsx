// Conversations list + timeline detail (Stage 13C, Parts E & F) with an operator composer.
//
// The list is bounded and filterable; the detail timeline visually distinguishes every event
// kind (request/update/reply, inbound/outbound, transport ACK vs semantic reply, reply-wait
// lifecycle, resume). Message content is a bounded, escaped preview; signatures, keys, raw
// envelopes and headers are never present. Reads create no side effects.

import { useMemo, useState } from 'react'
import { api } from '../api/client'
import { useResource } from '../api/hooks'
import type { ConversationSummary, TimelineEntry } from '../api/types'
import { OperatorComposer } from './OperatorComposer'
import { Badge, Empty, ErrorNote, Field, Loading, StatusCard } from './StatusCard'
import { AckBadge, DataPreview, DeliveryBadge, IdTag, KindLabel, ReplyBadge } from './commsUi'

type DirectionFilter = 'all' | 'outbound' | 'inbound'

export function ConversationsPage() {
  const conversations = useResource(() => api.getConversations(), [])
  const waits = useResource(() => api.getReplyWaits(), [])
  const fleet = useResource(() => api.getFleet(), [])
  const [selected, setSelected] = useState<string | null>(null)
  const [peerFilter, setPeerFilter] = useState('')
  const [direction, setDirection] = useState<DirectionFilter>('all')
  const [pendingOnly, setPendingOnly] = useState(false)
  const [search, setSearch] = useState('')

  const pendingByConv = useMemo(() => {
    const map = new Map<string, number>()
    for (const w of waits.data ?? []) {
      if (w.state === 'pending' && w.conversation_id) {
        map.set(w.conversation_id, (map.get(w.conversation_id) ?? 0) + 1)
      }
    }
    return map
  }, [waits.data])

  const rows = useMemo(() => {
    let list: ConversationSummary[] = conversations.data ?? []
    if (peerFilter) list = list.filter((c) => c.peers.includes(peerFilter))
    if (direction === 'outbound') list = list.filter((c) => c.outbound > 0)
    if (direction === 'inbound') list = list.filter((c) => c.inbound > 0)
    if (pendingOnly) list = list.filter((c) => (pendingByConv.get(c.conversation_id) ?? 0) > 0)
    if (search) list = list.filter((c) => c.conversation_id.includes(search.trim()))
    return list
  }, [conversations.data, peerFilter, direction, pendingOnly, search, pendingByConv])

  const peers = fleet.data?.peers ?? []
  const peerOptions = Array.from(new Set((conversations.data ?? []).flatMap((c) => c.peers)))

  return (
    <div className="page-grid">
      <StatusCard
        title="Conversations"
        badge={<Badge tone="muted">{rows.length}</Badge>}
        actions={<button onClick={() => { conversations.reload(); waits.reload() }}>Refresh</button>}
      >
        <Loading loading={conversations.loading && !conversations.data} />
        <ErrorNote error={conversations.error} />
        <div className="filter-bar" role="group" aria-label="Conversation filters">
          <label className="filter-label">
            Peer
            <select value={peerFilter} onChange={(e) => setPeerFilter(e.target.value)}>
              <option value="">any</option>
              {peerOptions.map((p) => (
                <option key={p} value={p}>{p.slice(0, 8)}…</option>
              ))}
            </select>
          </label>
          <label className="filter-label">
            Direction
            <select value={direction} onChange={(e) => setDirection(e.target.value as DirectionFilter)}>
              <option value="all">all</option>
              <option value="outbound">has outbound</option>
              <option value="inbound">has inbound</option>
            </select>
          </label>
          <label className="check-label">
            <input type="checkbox" checked={pendingOnly} onChange={(e) => setPendingOnly(e.target.checked)} />
            pending reply
          </label>
          <label className="filter-label">
            Search id
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="conversation id" />
          </label>
        </div>
        {conversations.data && rows.length === 0 && <Empty>No conversations match.</Empty>}
        {rows.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Conversation</th>
                <th scope="col">Peers</th>
                <th scope="col">Out / In</th>
                <th scope="col">Pending</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr
                  key={c.conversation_id}
                  className={c.conversation_id === selected ? 'row-selected' : 'row-clickable'}
                  tabIndex={0}
                  onClick={() => setSelected(c.conversation_id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      setSelected(c.conversation_id)
                    }
                  }}
                >
                  <td><code className="mono" title={c.conversation_id}>{c.conversation_id.slice(0, 8)}…</code></td>
                  <td>{c.peers.map((p) => p.slice(0, 8)).join(', ') || '—'}</td>
                  <td>{c.outbound} / {c.inbound}</td>
                  <td>
                    {(pendingByConv.get(c.conversation_id) ?? 0) > 0 ? (
                      <Badge tone="warn">{pendingByConv.get(c.conversation_id)} pending</Badge>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      {selected && (
        <ConversationDetail
          conversationId={selected}
          peers={peers}
          onSent={() => { conversations.reload(); waits.reload() }}
        />
      )}
    </div>
  )
}

function ConversationDetail({
  conversationId,
  peers,
  onSent,
}: {
  conversationId: string
  peers: import('../api/types').FleetPeer[]
  onSent: () => void
}) {
  const timeline = useResource(() => api.getConversationTimeline(conversationId), [conversationId])
  const data = timeline.data
  const composerPeer = data?.peers[0]

  return (
    <>
      <StatusCard
        title="Conversation timeline"
        badge={data ? <Badge tone="muted">{data.message_count} messages</Badge> : undefined}
        actions={<button onClick={timeline.reload}>Refresh</button>}
      >
        <Loading loading={timeline.loading && !data} />
        <ErrorNote error={timeline.error} />
        {data && (
          <>
            <Field label="Peers">{data.peers.map((p) => p.slice(0, 8)).join(', ') || '—'}</Field>
            <p className="legend small-note">
              <Badge tone="ok">ACK ✓</Badge> = peer node accepted delivery (not an answer) ·{' '}
              <Badge tone="ok">semantic reply ✓</Badge> = authenticated correlated answer
            </p>
            <ol className="timeline">
              {data.entries.map((e, i) => (
                <TimelineRow key={e.message_id ?? e.wait_id ?? `${e.kind}-${i}`} entry={e} />
              ))}
            </ol>
          </>
        )}
      </StatusCard>

      {composerPeer && (
        <StatusCard title="Reply / send in this conversation">
          <OperatorComposer
            peers={peers}
            fixedPeerId={composerPeer}
            conversationId={conversationId}
            onSent={() => { onSent(); timeline.reload() }}
          />
        </StatusCard>
      )}
    </>
  )
}

function TimelineRow({ entry }: { entry: TimelineEntry }) {
  const isReplyWait = entry.kind.startsWith('reply_wait')
  return (
    <li className={`timeline-row timeline-${entry.direction ?? 'system'}`}>
      <div className="timeline-head">
        <KindLabel kind={entry.kind} direction={entry.direction} />
        <span className="muted small-note">{entry.at ?? ''}</span>
      </div>
      {!isReplyWait && (
        <>
          <DataPreview text={entry.text_preview} data={entry.data_preview} />
          <div className="badge-row">
            <DeliveryBadge status={entry.delivery_status} />
            {entry.direction !== 'inbound' && <AckBadge acknowledged={entry.transport_acknowledged} />}
            <ReplyBadge semantic={entry.semantic_reply} />
            {entry.expects_reply && <Badge tone="info">expects reply</Badge>}
            {(entry.duplicate_count ?? 0) > 0 && (
              <Badge tone="muted">duplicate ×{entry.duplicate_count}</Badge>
            )}
            {entry.error_type && <Badge tone="bad">error: {entry.error_type}</Badge>}
          </div>
          <details className="metadata">
            <summary>metadata</summary>
            <div className="meta-grid">
              <Field label="request id"><IdTag id={entry.request_id} /></Field>
              {entry.reply_to_request_id && (
                <Field label="reply-to"><IdTag id={entry.reply_to_request_id} /></Field>
              )}
              {entry.mission_id ? (
                <Field label="mission"><IdTag id={entry.mission_id} /> (coordinator)</Field>
              ) : (
                entry.direction === 'outbound' && <Field label="origin">operator (manual)</Field>
              )}
            </div>
          </details>
        </>
      )}
      {isReplyWait && (
        <div className="badge-row">
          <Badge tone={entry.state === 'satisfied' ? 'ok' : entry.state === 'timed_out' ? 'bad' : entry.state === 'cancelled' ? 'muted' : 'warn'}>
            reply wait: {entry.state}
          </Badge>
          {entry.deadline && <span className="muted small-note">deadline {entry.deadline}</span>}
          <IdTag id={entry.request_id} label="req" />
        </div>
      )}
    </li>
  )
}
