// Field Validation dashboard section (Stage 14C.2, Part R).
//
// Read-only view of multi-node field/soak campaigns: campaigns + classification, checks
// (passed/failed/not-executed as accessible TEXT, never colour alone), and warnings. There is NO
// remote shell, no raw command output, no credentials, no lease tokens, and no private paths.
// Disruptive campaign/soak/fault actions are operator-gated CLI/API actions, not exposed here.

import { useState } from 'react'

import { api } from '../api/client'
import { useResource } from '../api/hooks'
import type { FieldCampaign, FieldCheck } from '../api/types'
import { Badge, Empty, ErrorNote, Field, Loading, StatusCard, type Tone } from './StatusCard'

function classTone(c: string): Tone {
  if (c.endsWith('_complete')) return 'ok'
  if (c === 'blocked') return 'warn'
  if (c === 'failed') return 'bad'
  return 'info'
}

function checkTone(status: string): Tone {
  if (status === 'passed') return 'ok'
  if (status === 'failed') return 'bad'
  if (status === 'not_executed') return 'muted'
  return 'info'
}

export function FieldPage() {
  const campaigns = useResource(() => api.getFieldCampaigns(), [])
  const [selected, setSelected] = useState<string | null>(null)
  const checks = useResource(
    () => (selected ? api.getFieldCampaignChecks(selected) : Promise.resolve([])),
    [selected],
  )

  return (
    <div className="grid">
      <StatusCard
        title="Field campaigns"
        badge={<Badge tone="info">{campaigns.data?.length ?? 0}</Badge>}
        actions={<button type="button" onClick={() => campaigns.reload()}>Reload</button>}
      >
        <Loading loading={campaigns.loading} />
        <ErrorNote error={campaigns.error} />
        {campaigns.data && campaigns.data.length === 0 && (
          <Empty>No field campaigns. Run `aithernet field campaign create` then `start`.</Empty>
        )}
        {campaigns.data && campaigns.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr><th>Name</th><th>Profile</th><th>Status</th><th>Classification</th>
                <th>Checks</th><th /></tr>
            </thead>
            <tbody>
              {campaigns.data.map((c: FieldCampaign) => (
                <tr key={c.id}>
                  <td className="mono">{c.name}</td>
                  <td>{c.profile}</td>
                  <td><Badge tone={c.status === 'completed' ? 'ok' : 'info'}>{c.status}</Badge></td>
                  <td><Badge tone={classTone(c.classification)}>{c.classification}</Badge></td>
                  <td>{c.checks_passed}✓ / {c.checks_failed}✗ / {c.checks_not_executed}∅</td>
                  <td>
                    <button type="button" onClick={() => setSelected(c.id)}>Checks</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      {selected && (
        <StatusCard
          title="Campaign checks"
          badge={<Badge tone="info">{checks.data?.length ?? 0}</Badge>}
          actions={<button type="button" onClick={() => setSelected(null)}>Close</button>}
        >
          <Loading loading={checks.loading} />
          <ErrorNote error={checks.error} />
          <Field label="Campaign">{selected.slice(0, 8)}…</Field>
          {checks.data && checks.data.length === 0 && <Empty>No checks recorded.</Empty>}
          {checks.data && checks.data.length > 0 && (
            <table className="data-table">
              <thead><tr><th>Check</th><th>Category</th><th>Status</th><th>Detail</th></tr></thead>
              <tbody>
                {checks.data.map((c: FieldCheck) => (
                  <tr key={c.name}>
                    <td className="mono">{c.name}</td>
                    <td>{c.category}</td>
                    <td><Badge tone={checkTone(c.status)}>{c.status}</Badge></td>
                    <td>{c.detail ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </StatusCard>
      )}
    </div>
  )
}
