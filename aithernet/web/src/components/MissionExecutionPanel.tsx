import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import type { MissionExecutionRun } from '../api/types'
import {
  Empty,
  ErrorNote,
  Field,
  Loading,
  StatusBadge,
  StatusCard,
  formatTime,
  shortId,
} from './StatusCard'

const TERMINAL = new Set(['completed', 'blocked', 'failed', 'cancelled'])
const POLL_MS = 2000

// The autonomous execution panel: lifecycle controls + the live run/lease/budget view +
// the mission timeline. Polling here is READ-ONLY — it never starts a worker, executes a
// route, or calls a model; only the explicit Start/Run-one-step buttons do work. Lease
// TOKENS are never sent by the backend, so they can never be displayed.
export function MissionExecutionPanel({
  selectedMissionId,
  onMutate,
}: {
  selectedMissionId: string | null
  onMutate: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [opError, setOpError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  const execution = useResource(
    () => (selectedMissionId ? api.getMissionExecution(selectedMissionId) : Promise.resolve(null)),
    [selectedMissionId, tick],
  )
  const timeline = useResource(
    () => (selectedMissionId ? api.getMissionTimeline(selectedMissionId) : Promise.resolve(null)),
    [selectedMissionId, tick],
  )

  const run = execution.data?.run ?? null
  const runStatus = run?.status ?? null
  const terminal = runStatus !== null && TERMINAL.has(runStatus)

  // Read-only live refresh while a run is in flight. Stops once the run is terminal so the
  // dashboard is not polling forever. This never mutates server state.
  useEffect(() => {
    if (!selectedMissionId || terminal) return
    const id = setInterval(() => setTick((t) => t + 1), POLL_MS)
    return () => clearInterval(id)
  }, [selectedMissionId, terminal])

  async function op(label: string, fn: () => Promise<unknown>, confirmMsg?: string) {
    if (!selectedMissionId) return
    if (confirmMsg && !window.confirm(confirmMsg)) return
    setBusy(true)
    setOpError(null)
    try {
      await fn()
      setTick((t) => t + 1)
      onMutate()
    } catch (err) {
      setOpError(`${label} failed: ${errorMessage(err)}`)
    } finally {
      setBusy(false)
    }
  }

  const canStart = !busy && !!selectedMissionId && (run === null || terminal)
  const canPause =
    !busy && runStatus !== null && ['active', 'queued', 'waiting'].includes(runStatus)
  const canResume = !busy && runStatus !== null && ['paused', 'waiting'].includes(runStatus)
  const canCancel = !busy && runStatus !== null && !terminal
  const canStep = !busy && runStatus !== null && !terminal

  return (
    <StatusCard
      title="Autonomous Execution"
      actions={
        <button className="small" disabled={!selectedMissionId || busy} onClick={() => setTick((t) => t + 1)}>
          Refresh
        </button>
      }
    >
      {!selectedMissionId && (
        <Empty>Select a mission to start or watch autonomous execution.</Empty>
      )}
      {selectedMissionId && (
        <>
          <div className="button-row">
            <button
              disabled={!canStart}
              onClick={() => op('Start', () => api.startMission(selectedMissionId))}
            >
              Start
            </button>
            <button
              disabled={!canPause}
              onClick={() => op('Pause', () => api.pauseMission(selectedMissionId))}
            >
              Pause
            </button>
            <button
              disabled={!canResume}
              onClick={() => op('Resume', () => api.resumeMission(selectedMissionId))}
            >
              Resume
            </button>
            <button
              disabled={!canStep}
              onClick={() => op('Run one step', () => api.runMissionAutoStep(selectedMissionId))}
            >
              Run one step
            </button>
            <button
              className="danger"
              disabled={!canCancel}
              onClick={() =>
                op(
                  'Cancel',
                  () => api.cancelMission(selectedMissionId),
                  'Cancel this mission run? It will stop at the next atomic boundary.',
                )
              }
            >
              Cancel
            </button>
          </div>

          <ErrorNote error={opError || execution.error} />
          <Loading loading={execution.loading && !execution.data} />

          {execution.data && (
            <div className="result-box">
              <Field label="Mission">
                {shortId(execution.data.mission.id)}{' '}
                <StatusBadge status={execution.data.mission.status} />
              </Field>
              {run ? <RunView run={run} budgets={execution.data.budget_remaining} /> : (
                <Empty>No execution run yet — press Start to queue one.</Empty>
              )}
            </div>
          )}

          {timeline.data && timeline.data.entries.length > 0 && (
            <div className="result-box scrollable" style={{ maxHeight: 240, overflowY: 'auto' }}>
              <Field label="Timeline">{timeline.data.entries.length} entries</Field>
              <table className="data-table">
                <tbody>
                  {timeline.data.entries
                    .slice()
                    .reverse()
                    .map((e, idx) => (
                      <tr key={`${e.at}-${idx}`}>
                        <td>{formatTime(e.at)}</td>
                        <td className="mono">{e.event_type || e.route_target || e.kind}</td>
                        <td className="cell-truncate" title={e.message}>
                          {e.message}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </StatusCard>
  )
}

function RunView({
  run,
  budgets,
}: {
  run: MissionExecutionRun
  budgets: Record<string, number>
}) {
  return (
    <>
      <Field label="Run">
        {shortId(run.id)} <StatusBadge status={run.status} /> · iteration {run.iteration_count}
      </Field>
      <Field label="Activity">
        coordinator_calls={run.coordinator_call_count} · failures={run.consecutive_failures} ·
        recoveries={run.recovery_count} · elapsed={Math.round(run.elapsed_seconds)}s
      </Field>
      <Field label="Lease">
        {run.execution_owner_id ? (
          <>
            owner {run.execution_owner_id} · expires {formatTime(run.lease_expiry)}
          </>
        ) : (
          'no active lease'
        )}
      </Field>
      {run.last_route_target && (
        <Field label="Last action">
          {run.last_route_target}
          {run.last_decision_id ? ` · decision ${shortId(run.last_decision_id)}` : ''}
        </Field>
      )}
      <Field label="Budget remaining">
        <span className="mono">
          {Object.entries(budgets)
            .map(([k, v]) => `${k}=${typeof v === 'number' ? Math.round(v) : v}`)
            .join('  ')}
        </span>
      </Field>
      {run.waiting && (
        <Field label="Waiting">
          <span className="mono">{JSON.stringify(run.waiting)}</span>
        </Field>
      )}
      {run.blocked_reason && <ErrorNote error={`Blocked: ${run.blocked_reason}`} />}
      {run.failure_message && <ErrorNote error={`Failed: ${run.failure_message}`} />}
      {run.final_response && (
        <Field label="Final response">
          <span className="final-response">{run.final_response}</span>
        </Field>
      )}
    </>
  )
}
