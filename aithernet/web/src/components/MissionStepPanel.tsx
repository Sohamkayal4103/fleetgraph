import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import { Empty, ErrorNote, Field, JsonBlock, Loading, StatusBadge, StatusCard, formatTime, shortId } from './StatusCard'

export function MissionStepPanel({
  selectedMissionId,
  onStepRun,
}: {
  selectedMissionId: string | null
  onStepRun: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [localTick, setLocalTick] = useState(0)

  const mission = useResource(
    () => (selectedMissionId ? api.getMission(selectedMissionId) : Promise.resolve(null)),
    [selectedMissionId, localTick],
  )
  const steps = useResource(
    () => (selectedMissionId ? api.getMissionSteps(selectedMissionId) : Promise.resolve([])),
    [selectedMissionId, localTick],
  )

  async function runStep() {
    if (!selectedMissionId) return
    setBusy(true)
    setRunError(null)
    try {
      await api.runMissionStep(selectedMissionId)
      setLocalTick((t) => t + 1)
      onStepRun()
    } catch (err) {
      setRunError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <StatusCard
      title="Mission Steps"
      actions={
        <button className="small" disabled={!selectedMissionId || busy} onClick={runStep}>
          Run one step
        </button>
      }
    >
      {!selectedMissionId && <Empty>Select a mission to inspect and run a step.</Empty>}
      {selectedMissionId && (
        <>
          <ErrorNote error={mission.error || steps.error || runError} />
          <Loading loading={(mission.loading || steps.loading) && !mission.data} />
          {mission.data && (
            <div className="result-box">
              <Field label="Mission">
                {shortId(mission.data.id)} <StatusBadge status={mission.data.status} />
              </Field>
              <Field label="Content">{mission.data.content}</Field>
            </div>
          )}
          {steps.data && steps.data.length === 0 && (
            <Empty>No steps yet — run one step (does not loop).</Empty>
          )}
          {steps.data &&
            steps.data.map((step) => (
              <div className="result-box" key={step.id}>
                <Field label="Step">
                  {step.route_target} <StatusBadge status={step.status} /> · {formatTime(step.created_at)}
                </Field>
                {step.route_action && <Field label="Action">{step.route_action}</Field>}
                {step.error && <ErrorNote error={step.error} />}
                <JsonBlock value={step.result} />
              </div>
            ))}
        </>
      )}
    </StatusCard>
  )
}
