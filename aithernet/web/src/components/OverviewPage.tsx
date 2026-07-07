// Overview page (Stage 13C, Part C): a concise, honest node + fleet + comms health rollup.
// Health is derived from persisted/worker state — never inferred from a configured flag alone.
// Auto-refreshes on a bounded interval; reads are side-effect free.

import { api } from '../api/client'
import { useAutoReload, useResource } from '../api/hooks'
import type { FailureEntry, Overview } from '../api/types'
import { Badge, Empty, ErrorNote, Field, Loading, StatusCard } from './StatusCard'
import { IdTag } from './commsUi'

function healthTone(running: boolean | null | undefined, degraded: boolean | null | undefined) {
  if (running && !degraded) return 'ok' as const
  if (running && degraded) return 'warn' as const
  return 'bad' as const
}

export function OverviewPage() {
  const overview = useResource(() => api.getOverview(), [])
  useAutoReload(overview.reload, 5000, true)
  const data = overview.data

  return (
    <div className="page-grid">
      <StatusCard
        title="Node"
        badge={data ? <Badge tone="ok">{data.node.runtime_status ?? 'unknown'}</Badge> : undefined}
        actions={<button onClick={overview.reload}>Refresh</button>}
      >
        <Loading loading={overview.loading && !data} />
        <ErrorNote error={overview.error} />
        {data && (
          <>
            <Field label="Name">{data.node.node_name}</Field>
            <Field label="Node id"><IdTag id={data.node.node_id} label="node" /></Field>
            <Field label="Version">{data.node.version}</Field>
            <Field label="Identity">
              {data.node.identity_initialized ? (
                <span>initialized · fingerprint <code className="mono">{data.node.fingerprint ?? '—'}</code></span>
              ) : (
                <Badge tone="warn">not initialized</Badge>
              )}
            </Field>
            <Field label="Missions / Events">
              {data.node.mission_count ?? '—'} / {data.node.event_count ?? '—'}
            </Field>
          </>
        )}
      </StatusCard>

      {data && (
        <StatusCard title="Workers & readiness">
          <Field label="Mission worker">
            <Badge tone={healthTone(data.workers.mission.running, data.workers.mission.degraded)}>
              {data.workers.mission.running ? 'running' : 'stopped'}
              {data.workers.mission.degraded ? ' · degraded' : ''}
            </Badge>{' '}
            active {data.workers.mission.active_count ?? 0} · queued {data.workers.mission.queued_count ?? 0}
          </Field>
          <Field label="Transport worker">
            <Badge tone={healthTone(data.workers.transport.running, data.workers.transport.degraded)}>
              {data.workers.transport.running ? 'running' : 'stopped'}
              {data.workers.transport.degraded ? ' · degraded' : ''}
            </Badge>{' '}
            in-flight {data.workers.transport.in_flight ?? 0}
          </Field>
          <Field label="Coordinator">
            <Badge tone={data.coordinator.configured ? 'ok' : 'warn'}>
              {data.coordinator.provider} · {data.coordinator.configured ? 'ready' : 'unconfigured'}
            </Badge>
          </Field>
          <Field label="Coding agent">
            <Badge tone={data.coding_agent.configured ? 'ok' : 'warn'}>
              {data.coding_agent.provider} · {data.coding_agent.configured ? 'ready' : 'unconfigured'}
            </Badge>
          </Field>
          <Field label="RF backends">
            {data.rf_backends.length === 0 ? (
              <Empty>none</Empty>
            ) : (
              data.rf_backends.map((b) => (
                <Badge key={b.backend_id} tone={b.state === 'ready' ? 'ok' : b.state === 'failed' ? 'bad' : 'muted'}>
                  {b.backend_id}: {b.state} ({b.tool_count} tools)
                </Badge>
              ))
            )}
          </Field>
        </StatusCard>
      )}

      {data && (
        <StatusCard title="Distributed communication">
          <Field label="Peers">
            {data.peers.trusted ?? 0} trusted / {data.peers.total ?? 0} total
          </Field>
          <Field label="Outbox">
            pending {data.outbox.pending ?? 0} · in-flight {data.outbox.in_flight ?? 0} · acked{' '}
            {data.outbox.acknowledged ?? 0} ·{' '}
            <span className={(data.outbox.failed ?? 0) + (data.outbox.dead_letter ?? 0) > 0 ? 'text-bad' : ''}>
              failed {data.outbox.failed ?? 0} · dead {data.outbox.dead_letter ?? 0}
            </span>
          </Field>
          <Field label="Inbox">{data.inbox_count}</Field>
          <Field label="Missions">
            active {data.missions.active ?? 0} · waiting {data.missions.waiting ?? 0} · queued{' '}
            {data.missions.queued ?? 0}
          </Field>
          <Field label="Reply waits pending">{data.reply_waits.pending ?? 0}</Field>
          <Field label="Inbound requests">{data.inbound_requests.total ?? 0}</Field>
        </StatusCard>
      )}

      {data && (
        <StatusCard
          title="Recent failures"
          badge={
            <Badge tone={overviewFailureCount(data) > 0 ? 'warn' : 'muted'}>
              {overviewFailureCount(data)}
            </Badge>
          }
        >
          <FailureList title="Communication" items={data.recent_failures.communication ?? []} />
          <FailureList title="RF" items={data.recent_failures.rf ?? []} />
          <FailureList title="Mission" items={data.recent_failures.mission ?? []} />
          {overviewFailureCount(data) === 0 && <Empty>No recent failures.</Empty>}
        </StatusCard>
      )}
    </div>
  )
}

function overviewFailureCount(data: Overview) {
  const f = data.recent_failures
  return (f.communication?.length ?? 0) + (f.rf?.length ?? 0) + (f.mission?.length ?? 0)
}

function FailureList({ title, items }: { title: string; items: FailureEntry[] }) {
  if (!items.length) return null
  return (
    <div className="failure-list">
      <p className="field-label">{title}</p>
      <ul>
        {items.map((f, i) => (
          <li key={`${f.event_type}-${i}`}>
            <code className="mono">{f.event_type}</code> {f.message ?? ''}{' '}
            <span className="muted small-note">{f.at ?? ''}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
