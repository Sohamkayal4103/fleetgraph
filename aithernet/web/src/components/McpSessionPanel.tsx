import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import type { McpSessionState } from '../api/types'
import { Badge, ErrorNote, Field, Loading, StatusCard, formatTime } from './StatusCard'

const STATE_TONE: Record<McpSessionState, 'ok' | 'warn' | 'bad' | 'muted'> = {
  ready: 'ok',
  starting: 'warn',
  restarting: 'warn',
  degraded: 'warn',
  stopping: 'warn',
  stopped: 'muted',
  failed: 'bad',
}

const TRANSITIONAL = new Set(['starting', 'restarting', 'stopping'])

export function McpSessionPanel({ refreshKey, onMutate }: { refreshKey: number; onMutate: () => void }) {
  const [tick, setTick] = useState(0)
  const session = useResource(() => api.getMcpSession(), [refreshKey, tick])
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  // Dashboard polling reads /mcp/session; it never spawns a subprocess (handled server-side).
  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setActionError(null)
    try {
      await action()
      setTick((t) => t + 1)
      onMutate()
    } catch (err) {
      setActionError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const data = session.data
  const inTransition = data ? TRANSITIONAL.has(data.state) : false
  const disabled = busy || inTransition

  return (
    <StatusCard
      title="MCP Session"
      badge={data ? <Badge tone={STATE_TONE[data.state]}>{data.state}</Badge> : undefined}
    >
      <ErrorNote error={session.error} />
      <Loading loading={session.loading && !data} />
      {data && (
        <>
          <Field label="Configured">{data.configured ? 'yes' : 'no'}</Field>
          <Field label="Session id">{data.session_id}</Field>
          <Field label="Generation">{data.generation}</Field>
          <Field label="Process (PID)">{data.pid ?? '—'}</Field>
          <Field label="Uptime">
            {data.uptime_seconds != null ? `${Math.round(data.uptime_seconds)}s` : '—'}
          </Field>
          <Field label="Restart count">{data.restart_count}</Field>
          <Field label="Tool count">{data.tool_count ?? '—'}</Field>
          <Field label="Active requests">
            {data.active_requests} / {data.max_in_flight_requests}
          </Field>
          <Field label="Last activity">{formatTime(data.last_activity_at)}</Field>
          {data.missing_configuration.length > 0 && (
            <Field label="Missing">{data.missing_configuration.join(', ')}</Field>
          )}
          {data.last_error && (
            <ErrorNote error={`[${data.last_error_type}] ${data.last_error}`} />
          )}
        </>
      )}

      <div className="button-row">
        <button className="small" disabled={disabled} onClick={() => run(api.startMcpSession)}>
          Start
        </button>
        <button className="small" disabled={disabled} onClick={() => run(api.restartMcpSession)}>
          Restart
        </button>
        <button className="small" disabled={disabled} onClick={() => run(api.stopMcpSession)}>
          Stop
        </button>
        <button className="small" disabled={disabled} onClick={() => run(() => api.getMcpTools(true))}>
          Refresh tools
        </button>
      </div>
      <ErrorNote error={actionError} />
    </StatusCard>
  )
}
