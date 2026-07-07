import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import { Badge, ErrorNote, Field, JsonPreview, Loading, StatusCard, formatTime } from './StatusCard'

function summaryText(value: Record<string, unknown> | null, key = 'summary'): string {
  if (!value) return '—'
  const v = value[key]
  if (typeof v === 'string') return v
  if (v != null) return String(v)
  return value['available'] === false ? 'unavailable' : '—'
}

export function GnuRadioContextPanel({ refreshKey, onMutate }: { refreshKey: number; onMutate: () => void }) {
  const [tick, setTick] = useState(0)
  const ctx = useResource(() => api.getGnuRadioContext(), [refreshKey, tick])
  const [busy, setBusy] = useState(false)
  const [refreshError, setRefreshError] = useState<string | null>(null)

  async function refresh() {
    setBusy(true)
    setRefreshError(null)
    try {
      await api.refreshGnuRadioContext()
      setTick((t) => t + 1)
      onMutate()
    } catch (err) {
      // Real backend errors (e.g. 502 when gr-mcp is down) are shown — not a fake outage.
      setRefreshError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const data = ctx.data
  return (
    <StatusCard
      title="GNU Radio Context"
      badge={
        data ? (
          <Badge tone={data.stale ? 'warn' : 'ok'}>{data.stale ? 'stale' : 'fresh'}</Badge>
        ) : undefined
      }
    >
      <ErrorNote error={ctx.error} />
      <Loading loading={ctx.loading && !data} />
      {data && (
        <>
          <Field label="Flowgraph">
            {data.active_flowgraph_name || data.active_flowgraph_path || '(none)'}
          </Field>
          <Field label="Status">{data.flowgraph_status}</Field>
          {data.stale_reason && <Field label="Stale reason">{data.stale_reason}</Field>}
          <Field label="Blocks">{(data.block_summary?.['count'] as number) ?? '—'}</Field>
          <Field label="Connections">{(data.connection_summary?.['count'] as number) ?? '—'}</Field>
          <Field label="Validation">{summaryText(data.validation_summary)}</Field>
          <Field label="Latest errors">{summaryText(data.latest_error_summary)}</Field>
          <Field label="Execution">{summaryText(data.execution_summary)}</Field>
          <Field label="Mission">{data.related_mission_id ?? '—'}</Field>
          <Field label="Source gen">{data.source_session_generation ?? '—'}</Field>
          <Field label="Last refresh">{formatTime(data.last_refreshed_at)}</Field>
          {/* Large summaries are bounded + scrollable; the full raw result stays on the MCP call. */}
          <JsonPreview value={data.block_summary} limit={4000} />
        </>
      )}
      <div className="button-row">
        <button className="small" disabled={busy} onClick={refresh}>
          Refresh GNU Radio context
        </button>
      </div>
      <ErrorNote error={refreshError} />
    </StatusCard>
  )
}
