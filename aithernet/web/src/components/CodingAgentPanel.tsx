import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import {
  Chips,
  Empty,
  ErrorNote,
  Field,
  Loading,
  ReadyBadge,
  StatusBadge,
  StatusCard,
  formatTime,
  shortId,
} from './StatusCard'

//: Human-readable explanation for each coding-agent readiness reason code.
const READINESS_HINTS: Record<string, string> = {
  executable: 'The configured executable was not found on PATH.',
  executable_not_runnable: 'The executable exists but could not be run.',
  executable_provider_mismatch:
    'The executable is the wrong agent for this provider — provider and executable must match (claude_code↔claude, codex_cli↔codex).',
  executable_probe_failed: 'The executable identity probe timed out or failed.',
}

function readinessHint(reasons: string[]): string {
  for (const reason of reasons) {
    if (READINESS_HINTS[reason]) return READINESS_HINTS[reason]
  }
  return 'The coding agent is not configured.'
}

export function CodingAgentPanel({ refreshKey }: { refreshKey: number }) {
  const [localTick, setLocalTick] = useState(0)
  const codingAgent = useResource(() => api.getCodingAgentStatus(), [refreshKey])
  const tasks = useResource(() => api.getCodingTasks(), [refreshKey, localTick])

  const [objective, setObjective] = useState('')
  const [runError, setRunError] = useState<string | null>(null)
  const [runBusy, setRunBusy] = useState(false)

  async function runTask() {
    if (!objective.trim()) {
      setRunError('Objective is required.')
      return
    }
    setRunBusy(true)
    setRunError(null)
    try {
      await api.runCodingTask({ objective: objective.trim() })
      setObjective('')
      setLocalTick((t) => t + 1)
    } catch (err) {
      // Real backend errors surface here (e.g. 503 when the coding agent is unconfigured).
      setRunError(errorMessage(err))
    } finally {
      setRunBusy(false)
    }
  }

  return (
    <StatusCard title="Coding Agent">
      <h3>Coding agent</h3>
      <ErrorNote error={codingAgent.error} />
      <Loading loading={codingAgent.loading && !codingAgent.data} />
      {codingAgent.data && (
        <>
          <Field label="Provider">
            {codingAgent.data.provider} <ReadyBadge configured={codingAgent.data.configured} />
          </Field>
          <Field label="Executable">{codingAgent.data.executable}</Field>
          <Field label="Workspace">{codingAgent.data.workspace}</Field>
          {codingAgent.data.missing_configuration.length > 0 && (
            <Field label="Not ready">
              <Chips items={codingAgent.data.missing_configuration} tone="warn" />
            </Field>
          )}
          {!codingAgent.data.configured && (
            <p className="muted small-note">{readinessHint(codingAgent.data.missing_configuration)}</p>
          )}
          <Field label="Active">
            <Chips items={codingAgent.data.active_capabilities} tone="ok" />
          </Field>
        </>
      )}

      <h3>Create &amp; run a coding task</h3>
      <label className="stack">
        <span className="field-label">objective</span>
        <textarea value={objective} onChange={(e) => setObjective(e.target.value)} rows={2} />
      </label>
      <div className="button-row">
        <button disabled={runBusy} onClick={runTask}>
          Create + run
        </button>
      </div>
      <ErrorNote error={runError} />

      <h3>Coding tasks</h3>
      <ErrorNote error={tasks.error} />
      {tasks.data && tasks.data.length === 0 && <Empty>No coding tasks yet.</Empty>}
      {tasks.data && tasks.data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Status</th>
              <th>Objective</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {tasks.data.map((task) => (
              <tr key={task.id}>
                <td title={task.id}>{shortId(task.id)}</td>
                <td>
                  <StatusBadge status={task.status} />
                </td>
                <td className="cell-truncate" title={task.objective}>
                  {task.objective}
                </td>
                <td>{formatTime(task.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </StatusCard>
  )
}
