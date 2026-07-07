import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage } from '../api/hooks'
import type { CoordinatorDecision, Mission, MissionStep } from '../api/types'
import { ErrorNote, Field, JsonBlock, StatusBadge, StatusCard } from './StatusCard'

export function MissionComposer({
  onMissionCreated,
}: {
  onMissionCreated: (missionId: string) => void
}) {
  const [content, setContent] = useState('')
  const [sourceType, setSourceType] = useState('user')
  const [sourceId, setSourceId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [createdMission, setCreatedMission] = useState<Mission | null>(null)
  const [decision, setDecision] = useState<CoordinatorDecision | null>(null)
  const [step, setStep] = useState<MissionStep | null>(null)
  const [started, setStarted] = useState<string | null>(null)

  // mode: 'submit' (just persist), 'step' (manual one step — does NOT loop), or 'autonomous'
  // (queue the autonomous worker loop, which returns after queueing, not completion).
  async function submit(mode: 'submit' | 'step' | 'autonomous') {
    if (!content.trim()) {
      setError('Mission content is required.')
      return
    }
    setBusy(true)
    setError(null)
    setDecision(null)
    setStep(null)
    setCreatedMission(null)
    setStarted(null)
    try {
      const mission = await api.createMission({
        content: content.trim(),
        source_type: sourceType.trim() || 'user',
        source_id: sourceId.trim() || null,
      })
      setCreatedMission(mission)
      onMissionCreated(mission.id)
      if (mode === 'step') {
        const result = await api.runMissionStep(mission.id)
        setDecision(result.decision)
        setStep(result.step)
      } else if (mode === 'autonomous') {
        const response = await api.startMission(mission.id)
        setStarted(response.detail)
      }
      setContent('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <StatusCard title="Mission Composer">
      <label className="stack">
        <span className="field-label">Mission content</span>
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="Describe the mission for this node…"
          rows={4}
        />
      </label>
      <div className="inline-fields">
        <label className="stack">
          <span className="field-label">source_type</span>
          <input value={sourceType} onChange={(e) => setSourceType(e.target.value)} />
        </label>
        <label className="stack">
          <span className="field-label">source_id (optional)</span>
          <input value={sourceId} onChange={(e) => setSourceId(e.target.value)} />
        </label>
      </div>
      <div className="button-row">
        <button disabled={busy} onClick={() => submit('submit')}>
          Submit Mission
        </button>
        <button disabled={busy} onClick={() => submit('step')}>
          Submit + Run Step
        </button>
        <button disabled={busy} onClick={() => submit('autonomous')}>
          Submit + Start Autonomous
        </button>
      </div>

      <ErrorNote error={error} />

      {started && (
        <div className="result-box">
          <Field label="Autonomous">{started}</Field>
        </div>
      )}

      {createdMission && (
        <div className="result-box">
          <Field label="Mission">
            {createdMission.id} <StatusBadge status={createdMission.status} />
          </Field>
        </div>
      )}
      {decision && (
        <div className="result-box">
          <Field label="Decision">{decision.summary}</Field>
          <Field label="Next target">{decision.next_target}</Field>
        </div>
      )}
      {step && (
        <div className="result-box">
          <Field label="Step">
            {step.route_target} <StatusBadge status={step.status} />
          </Field>
          {step.error && <ErrorNote error={step.error} />}
          <JsonBlock value={step.result} />
        </div>
      )}
    </StatusCard>
  )
}
