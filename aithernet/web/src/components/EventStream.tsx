import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { errorMessage } from '../api/hooks'
import type { NodeEvent } from '../api/types'
import { Badge, Empty, ErrorNote, StatusCard, formatTime, shortId } from './StatusCard'

// The backend emits SSE frames with a NAMED `event:` field, so a single `onmessage`
// handler would miss them. These are the event types the backend currently publishes
// (across Stages 1–7); we attach a listener per name. This is real backend domain
// knowledge, not synthesized data — every event shown comes verbatim from the stream, and
// GET /events remains the source of truth for history (the "Refresh recent" button).
const KNOWN_EVENT_TYPES = [
  'mission.received',
  'coordinator.processing_started',
  'coordinator.decision',
  'coordinator.failed',
  'coding_task.created',
  'coding_task.started',
  'coding_task.completed',
  'coding_task.failed',
  'mcp.tool_list.requested',
  'mcp.tool_list.completed',
  'mcp.tool_list.failed',
  'mcp.tool_call.created',
  'mcp.tool_call.started',
  'mcp.tool_call.completed',
  'mcp.tool_call.failed',
  'mission_step.started',
  'mission_step.completed',
  'mission_step.failed',
  'mission_step.blocked',
  'external_agent.connected',
  'external_agent.disconnected',
  'external_agent.disabled',
  'external_agent.message_received',
  'external_agent.message_replied',
  'external_agent.mission_created',
  'external_agent.step_ran',
  // Autonomous mission execution (Stage 12).
  'mission.run.created',
  'mission.queued',
  'mission.lease.acquired',
  'mission.lease.renewed',
  'mission.lease.lost',
  'mission.run.started',
  'mission.run.stopped',
  'mission.iteration.started',
  'mission.decision.completed',
  'mission.action.started',
  'mission.action.completed',
  'mission.action.failed',
  'mission.waiting',
  'mission.paused',
  'mission.resumed',
  'mission.cancel.requested',
  'mission.cancelled',
  'mission.completed',
  'mission.blocked',
  'mission.failed',
  'mission.recovered',
  'mission.budget.exhausted',
  'worker.started',
  'worker.stopped',
  'worker.degraded',
]

const MAX_EVENTS = 100

type ConnState = 'connecting' | 'live' | 'error'

export function EventStream() {
  const [events, setEvents] = useState<NodeEvent[]>([])
  const [conn, setConn] = useState<ConnState>('connecting')
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const seen = useRef<Set<string>>(new Set())

  const addEvents = useCallback((incoming: NodeEvent[]) => {
    setEvents((prev) => {
      const merged = [...prev]
      for (const event of incoming) {
        if (event.id && seen.current.has(event.id)) continue
        if (event.id) seen.current.add(event.id)
        merged.unshift(event)
      }
      return merged.slice(0, MAX_EVENTS)
    })
  }, [])

  useEffect(() => {
    const source = new EventSource(api.streamUrl)
    const onEvent = (e: MessageEvent) => {
      try {
        addEvents([JSON.parse(e.data) as NodeEvent])
      } catch {
        /* ignore malformed frame */
      }
    }
    source.onopen = () => setConn('live')
    // Browsers auto-reconnect EventSource on error; reflect the transient state.
    source.onerror = () => setConn('error')
    source.onmessage = onEvent // catches any unnamed frames
    for (const type of KNOWN_EVENT_TYPES) source.addEventListener(type, onEvent as EventListener)
    return () => source.close()
  }, [addEvents])

  async function refreshRecent() {
    setRefreshError(null)
    try {
      const recent = await api.getEvents()
      seen.current = new Set()
      setEvents([])
      addEvents([...recent].reverse())
    } catch (err) {
      setRefreshError(errorMessage(err))
    }
  }

  const connBadge =
    conn === 'live' ? (
      <Badge tone="ok">live</Badge>
    ) : conn === 'connecting' ? (
      <Badge tone="muted">connecting…</Badge>
    ) : (
      <Badge tone="warn">reconnecting…</Badge>
    )

  return (
    <StatusCard
      title="Live Activity"
      badge={connBadge}
      actions={
        <button className="small" onClick={refreshRecent}>
          Refresh recent
        </button>
      }
    >
      <ErrorNote error={refreshError} />
      {events.length === 0 && <Empty>Waiting for events… (actions you take here will appear live)</Empty>}
      {events.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Type</th>
              <th>Source</th>
              <th>Mission</th>
              <th>Message</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event, idx) => (
              <tr key={event.id || idx}>
                <td>{formatTime(event.created_at)}</td>
                <td className="mono">{event.event_type}</td>
                <td>{event.source}</td>
                <td title={event.mission_id ?? ''}>
                  {event.mission_id ? shortId(event.mission_id) : '—'}
                </td>
                <td className="cell-truncate" title={event.message}>
                  {event.message}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </StatusCard>
  )
}
