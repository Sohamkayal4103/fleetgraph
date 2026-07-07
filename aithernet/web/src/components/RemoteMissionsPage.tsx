// Remote missions operator view (Stage 13D.3, Part L).
//
// Shows authenticated remote-mission snapshots: peer, correlated local mission/request,
// conversation, latest state, FRESHNESS (unknown/fresh/stale/terminal — text + colour, never
// colour alone), sequence, response obligation, last update, and linked artifacts/transfers.
// Allows a single bounded status-query action for a known snapshot (the backend signs and
// delivers; the browser never contacts a peer). All content is bounded and escaped; no bytes,
// paths, signatures, prompts, or keys are ever shown.

import { useState } from 'react'
import { api } from '../api/client'
import { useResource, errorMessage } from '../api/hooks'
import type { Freshness, RemoteMissionSnapshot, RemoteMissionStatusEvent } from '../api/types'
import { Badge, Empty, ErrorNote, Field, Loading, StatusCard, type Tone } from './StatusCard'
import { IdTag } from './commsUi'

const FRESHNESS_TONE: Record<Freshness, Tone> = {
  unknown: 'muted',
  fresh: 'ok',
  stale: 'warn',
  terminal: 'info',
}

function FreshnessBadge({ freshness }: { freshness: Freshness }) {
  return <Badge tone={FRESHNESS_TONE[freshness] ?? 'muted'}>freshness: {freshness}</Badge>
}

export function RemoteMissionsPage() {
  const [refreshKey, setRefreshKey] = useState(0)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [openEvents, setOpenEvents] = useState<string | null>(null)
  const snapshots = useResource(() => api.getRemoteMissions(), [refreshKey])

  const reload = () => setRefreshKey((k) => k + 1)

  const query = async (id: string) => {
    setBusy(true)
    setActionError(null)
    try {
      await api.queryRemoteMission(id)
      reload()
    } catch (err) {
      setActionError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const rows = snapshots.data ?? []

  return (
    <StatusCard
      title="Remote missions"
      badge={<Badge tone="muted">{rows.length}</Badge>}
      actions={
        <button type="button" className="small" onClick={reload} disabled={snapshots.loading}>
          Refresh
        </button>
      }
    >
      <Loading loading={snapshots.loading && rows.length === 0} />
      <ErrorNote error={snapshots.error} />
      <ErrorNote error={actionError} />
      {!snapshots.loading && rows.length === 0 && !snapshots.error && (
        <Empty>
          No remote-mission snapshots yet. A snapshot appears only when a peer you authorized
          (<code>may_publish_mission_status</code>) sends authenticated status about a mission you
          requested. State is never inferred — absence shows as “unknown”.
        </Empty>
      )}
      <ul className="stack" style={{ listStyle: 'none', padding: 0 }}>
        {rows.map((s) => (
          <li key={s.snapshot_id} className="result-box">
            <RemoteMissionRow
              snap={s}
              busy={busy}
              onQuery={() => query(s.snapshot_id)}
              eventsOpen={openEvents === s.snapshot_id}
              onToggleEvents={() =>
                setOpenEvents(openEvents === s.snapshot_id ? null : s.snapshot_id)
              }
            />
          </li>
        ))}
      </ul>
    </StatusCard>
  )
}

function RemoteMissionRow({
  snap,
  busy,
  onQuery,
  eventsOpen,
  onToggleEvents,
}: {
  snap: RemoteMissionSnapshot
  busy: boolean
  onQuery: () => void
  eventsOpen: boolean
  onToggleEvents: () => void
}) {
  return (
    <div className="stack">
      <div className="inline-fields">
        <span className="kind-name">{snap.state ?? 'unknown'}</span>
        <FreshnessBadge freshness={snap.freshness} />
        <Badge tone="muted">seq {snap.latest_sequence}</Badge>
        {snap.terminal_category && <Badge tone="info">terminal: {snap.terminal_category}</Badge>}
        {snap.query_pending && <Badge tone="warn">query pending</Badge>}
      </div>
      <Field label="Peer">
        <IdTag id={snap.peer_id} label="peer" />
      </Field>
      <Field label="Remote mission">
        <IdTag id={snap.remote_mission_ref} />
      </Field>
      <Field label="Local mission">
        {snap.local_mission_id ? <IdTag id={snap.local_mission_id} /> : <span className="muted">—</span>}
      </Field>
      <Field label="Conversation">
        {snap.conversation_id ? <IdTag id={snap.conversation_id} /> : <span className="muted">—</span>}
      </Field>
      <Field label="Obligation">{snap.response_obligation ?? '—'}</Field>
      <Field label="Linked">
        {snap.artifact_count} artifact(s), {snap.transfer_count} transfer(s)
      </Field>
      <Field label="Last update">{snap.received_at ?? '—'}</Field>
      <div className="inline-fields">
        <button
          type="button"
          className="small"
          onClick={onQuery}
          disabled={busy}
          aria-label={`Query latest status for snapshot ${snap.snapshot_id}`}
        >
          Query status
        </button>
        <button type="button" className="small" onClick={onToggleEvents}>
          {eventsOpen ? 'Hide events' : 'Show events'}
        </button>
      </div>
      {eventsOpen && <SnapshotEvents snapshotId={snap.snapshot_id} />}
    </div>
  )
}

function SnapshotEvents({ snapshotId }: { snapshotId: string }) {
  const events = useResource(() => api.getRemoteMissionEvents(snapshotId), [snapshotId])
  const rows: RemoteMissionStatusEvent[] = events.data ?? []
  return (
    <div className="json-scroll">
      <Loading loading={events.loading} />
      <ErrorNote error={events.error} />
      <table className="data-table">
        <thead>
          <tr>
            <th>seq</th>
            <th>state</th>
            <th>type</th>
            <th>disposition</th>
            <th>at</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => (
            <tr key={e.event_id}>
              <td>{e.sequence ?? '—'}</td>
              <td>{e.state ?? '—'}</td>
              <td>{e.message_type ?? '—'}</td>
              <td>{e.disposition}</td>
              <td className="muted">{e.received_at ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
