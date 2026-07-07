// Data & Privacy dashboard section (Stage 14E, Part 23).
//
// Read-only operator view of consent (by category), collection/export state, pending + held
// records, queued batches, destinations + readiness, dead letters, deletions, datasets, and
// Drive status WITHOUT credentials. Status is shown as accessible TEXT via Badge tones (never
// colour alone). There are NO secrets, raw OAuth tokens, encryption keys, private paths, or
// arbitrary destination/URL inputs; disruptive actions are operator-gated CLI/API actions.

import { useState } from 'react'

import { api } from '../api/client'
import { useResource } from '../api/hooks'
import type {
  DataBatch,
  DataDestination,
  DataRecord,
} from '../api/types'
import { Badge, Empty, ErrorNote, Loading, StatusCard, type Tone } from './StatusCard'

function consentTone(allowed: boolean): Tone {
  return allowed ? 'ok' : 'muted'
}

function statusTone(s: string): Tone {
  if (['receipt_verified', 'delivered', 'approved', 'ready'].includes(s)) return 'ok'
  if (['held', 'retry_wait', 'pending', 'collected', 'degraded'].includes(s)) return 'warn'
  if (['dead_letter', 'cancelled', 'quarantined', 'rejected'].includes(s)) return 'bad'
  return 'info'
}

export function DataPrivacyPage() {
  const consent = useResource(() => api.getDataConsent(), [])
  const diag = useResource(() => api.getDataDiagnostics(), [])
  const destinations = useResource(() => api.getDataDestinations(), [])
  const batches = useResource(() => api.getDataBatches(), [])
  const [recCategory, setRecCategory] = useState<string | null>(null)
  const records = useResource<DataRecord[]>(
    () => api.getDataRecords(recCategory ?? undefined),
    [recCategory],
  )

  const d = diag.data
  return (
    <div className="grid">
      <StatusCard
        title="Data platform"
        badge={<Badge tone={d?.enabled ? 'ok' : 'muted'}>{d?.enabled ? 'enabled' : 'disabled'}</Badge>}
        actions={<button type="button" onClick={() => diag.reload()}>Reload</button>}
      >
        <Loading loading={diag.loading} />
        <ErrorNote error={diag.error} />
        {d && (
          <div className="kv">
            <div>
              <span className="muted">Privacy mode</span>
              <Badge tone={d.air_gapped ? 'info' : 'muted'}>
                {d.air_gapped ? 'air-gapped' : 'standard'}
              </Badge>
            </div>
            <div>
              <span className="muted">Collection</span>
              <Badge tone={d.collection_paused ? 'warn' : 'ok'}>
                {d.collection_paused ? 'paused' : 'active'}
              </Badge>
            </div>
            <div>
              <span className="muted">Export</span>
              <Badge tone={!d.export_enabled ? 'muted' : d.export_paused ? 'warn' : 'ok'}>
                {!d.export_enabled ? 'disabled' : d.export_paused ? 'paused' : 'active'}
              </Badge>
            </div>
            <div>
              <span className="muted">Pending records</span>
              <code>{d.pending_records}</code>
            </div>
            <div>
              <span className="muted">Held records</span>
              <code>{d.held_records}</code>
            </div>
            <div>
              <span className="muted">Pending batches</span>
              <code>{d.pending_batches}</code>
            </div>
            <div>
              <span className="muted">Dead letters</span>
              <Badge tone={d.dead_letter_batches ? 'bad' : 'ok'}>{d.dead_letter_batches}</Badge>
            </div>
            <div>
              <span className="muted">Drive</span>
              <Badge tone={d.drive_status.enabled ? 'ok' : 'muted'}>
                {d.drive_status.configured ? 'configured' : 'unconfigured'}
                {d.drive_status.credentials_present ? ' (creds present)' : ''}
              </Badge>
            </div>
          </div>
        )}
      </StatusCard>

      <StatusCard title="Effective consent" actions={<button type="button" onClick={() => consent.reload()}>Reload</button>}>
        <Loading loading={consent.loading} />
        <ErrorNote error={consent.error} />
        {consent.data && (
          <table className="data-table">
            <thead>
              <tr><th>Category</th><th>Collection</th><th>Export</th><th>Raw artifact</th><th>State</th></tr>
            </thead>
            <tbody>
              {Object.values(consent.data.effective).map((c) => (
                <tr key={c.category}>
                  <td className="mono">{c.category}</td>
                  <td><Badge tone={consentTone(c.collection_allowed)}>
                    {c.collection_allowed ? 'allowed' : 'not allowed'}</Badge></td>
                  <td><Badge tone={consentTone(c.export_allowed)}>
                    {c.export_allowed ? 'allowed' : 'not allowed'}</Badge></td>
                  <td><Badge tone={consentTone(c.raw_artifact_allowed)}>
                    {c.raw_artifact_allowed ? 'allowed' : 'not allowed'}</Badge></td>
                  <td>{c.revocation_state}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      <StatusCard title="Destinations" badge={<Badge tone="info">{destinations.data?.length ?? 0}</Badge>}>
        <Loading loading={destinations.loading} />
        {destinations.data && destinations.data.length === 0 && <Empty>No destinations.</Empty>}
        {destinations.data && destinations.data.length > 0 && (
          <table className="data-table">
            <thead><tr><th>Name</th><th>Kind</th><th>Enabled</th><th>Readiness</th></tr></thead>
            <tbody>
              {destinations.data.map((x: DataDestination) => (
                <tr key={x.destination_id}>
                  <td className="mono">{x.name}</td>
                  <td>{x.kind}</td>
                  <td><Badge tone={x.enabled ? 'ok' : 'muted'}>{x.enabled ? 'enabled' : 'disabled'}</Badge></td>
                  <td><Badge tone={statusTone(x.readiness)}>{x.readiness}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      <StatusCard
        title="Records"
        actions={
          <span>
            {['', 'operational', 'analytics', 'training', 'raw_artifact'].map((c) => (
              <button key={c || 'all'} type="button" onClick={() => setRecCategory(c || null)}>
                {c || 'all'}
              </button>
            ))}
          </span>
        }
      >
        <Loading loading={records.loading} />
        {records.data && records.data.length === 0 && <Empty>No records.</Empty>}
        {records.data && records.data.length > 0 && (
          <table className="data-table">
            <thead><tr><th>Category</th><th>Status</th><th>Sensitivity</th><th>Bytes</th></tr></thead>
            <tbody>
              {records.data.slice(0, 100).map((r: DataRecord) => (
                <tr key={r.record_id}>
                  <td className="mono">{r.category}</td>
                  <td><Badge tone={statusTone(r.status)}>{r.status}</Badge></td>
                  <td>{r.sensitivity}</td>
                  <td>{r.byte_size}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      <StatusCard title="Batches" badge={<Badge tone="info">{batches.data?.length ?? 0}</Badge>}>
        <Loading loading={batches.loading} />
        {batches.data && batches.data.length === 0 && <Empty>No batches.</Empty>}
        {batches.data && batches.data.length > 0 && (
          <table className="data-table">
            <thead><tr><th>Batch</th><th>Status</th><th>Records</th><th>Encrypted</th><th>Attempts</th></tr></thead>
            <tbody>
              {batches.data.map((b: DataBatch) => (
                <tr key={b.batch_id}>
                  <td className="mono">{b.batch_id.slice(0, 8)}</td>
                  <td><Badge tone={statusTone(b.status)}>{b.status}</Badge></td>
                  <td>{b.record_count}</td>
                  <td>{b.encrypted ? 'yes' : 'no'}</td>
                  <td>{b.attempt_count}/{b.max_attempts}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>
    </div>
  )
}
