// Operations dashboard section (Stage 14A, area 12).
//
// Read-only operational view: app version, schema/migration status, liveness/readiness with
// per-component startup/degraded state, storage usage, backup freshness, and the last preflight
// result. There is intentionally NO remote OS-command execution and NO destructive restore/
// upgrade control here — those are operator CLI actions. All data is bounded and sanitized.

import { api } from '../api/client'
import { useResource } from '../api/hooks'
import type { OpsComponent } from '../api/types'
import { Badge, Empty, ErrorNote, Field, Loading, StatusCard, type Tone } from './StatusCard'

function componentTone(state: string): Tone {
  if (state === 'ready') return 'ok'
  if (state === 'disabled') return 'muted'
  if (state === 'degraded') return 'warn'
  if (state === 'failed') return 'bad'
  return 'info'
}

function phaseTone(phase: string): Tone {
  if (phase === 'ready') return 'ok'
  if (phase === 'degraded') return 'warn'
  if (phase === 'shutting_down') return 'bad'
  return 'info'
}

function bytesGiB(n?: number): string {
  if (n === undefined || n === null) return '—'
  return `${(n / 1024 ** 3).toFixed(1)} GiB`
}

export function OperationsPage() {
  const overview = useResource(() => api.getOpsOverview(), [])
  const preflight = useResource(() => api.getOpsPreflight(), [])
  const backups = useResource(() => api.getOpsBackups(), [])

  return (
    <div className="grid">
      <StatusCard
        title="Node & readiness"
        badge={
          overview.data ? (
            <Badge tone={phaseTone(overview.data.readiness.phase)}>
              {overview.data.readiness.ready ? 'ready' : 'not ready'} · {overview.data.readiness.phase}
            </Badge>
          ) : undefined
        }
        actions={<button onClick={() => { overview.reload(); preflight.reload(); backups.reload() }}>Refresh</button>}
      >
        <Loading loading={overview.loading} />
        <ErrorNote error={overview.error} />
        {overview.data && (
          <>
            <Field label="Node">{overview.data.node_name} ({overview.data.node_id.slice(0, 8)}…)</Field>
            <Field label="Version">{overview.data.version}</Field>
            <Field label="Started">{overview.data.started_at}</Field>
            <Field label="State root">
              {overview.data.state_root_configured ? 'configured' : 'dev / repo-relative'}
            </Field>
            <Field label="Schema">
              {overview.data.migrations.error
                ? `error: ${overview.data.migrations.error}`
                : `${overview.data.migrations.applied} applied · ${
                    overview.data.migrations.pending?.length ?? 0
                  } pending · ${overview.data.migrations.schema_valid ? 'valid' : 'INVALID'}`}
            </Field>
            <h3 className="small-note">Startup components</h3>
            <table className="data-table">
              <thead>
                <tr><th>Component</th><th>Required</th><th>State</th></tr>
              </thead>
              <tbody>
                {overview.data.readiness.components.map((c: OpsComponent) => (
                  <tr key={c.name}>
                    <td className="mono">{c.name}</td>
                    <td>{c.required ? 'yes' : 'no'}</td>
                    <td><Badge tone={componentTone(c.state)}>{c.state}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="small-note muted">
              Graceful restart: send SIGINT (systemd stop) — workers/RF/subprocesses shut down in
              order; pending outbox + waits stay durable and recover on restart.
            </p>
          </>
        )}
      </StatusCard>

      <StatusCard title="Storage & backups">
        <Loading loading={overview.loading || backups.loading} />
        {overview.data && (
          <>
            <Field label="Free space">
              {overview.data.storage.error
                ? overview.data.storage.error
                : `${bytesGiB(overview.data.storage.free_bytes)} free of ${bytesGiB(
                    overview.data.storage.total_bytes,
                  )} (${overview.data.storage.free_pct ?? '—'}%)`}
            </Field>
            <Field label="Backups">{overview.data.backups.count}</Field>
            <Field label="Latest backup">
              {overview.data.backups.latest_created_at ?? 'none — run `aithernet backup create`'}
            </Field>
          </>
        )}
        <ErrorNote error={backups.error} />
        {backups.data && backups.data.length === 0 && <Empty>No backups yet.</Empty>}
        {backups.data && backups.data.length > 0 && (
          <table className="data-table">
            <thead><tr><th>Created</th><th>Version</th><th>Files</th></tr></thead>
            <tbody>
              {backups.data.slice(0, 8).map((b) => (
                <tr key={b.path}>
                  <td>{b.created_at ?? '—'}</td>
                  <td>{b.app_version ?? '—'}</td>
                  <td>{b.file_count ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      <StatusCard
        title="Preflight"
        badge={
          preflight.data ? (
            <Badge tone={preflight.data.ok ? 'ok' : 'bad'}>
              {preflight.data.ok ? 'ok' : 'failures'}
            </Badge>
          ) : undefined
        }
      >
        <Loading loading={preflight.loading} />
        <ErrorNote error={preflight.error} />
        {preflight.data && (
          <>
            <Field label="Result">
              {preflight.data.counts.ok ?? 0} ok · {preflight.data.counts.warn ?? 0} warn ·{' '}
              {preflight.data.counts.fail ?? 0} fail
            </Field>
            <table className="data-table">
              <tbody>
                {preflight.data.checks
                  .filter((c) => c.status !== 'ok')
                  .map((c) => (
                    <tr key={c.name}>
                      <td><Badge tone={c.status === 'fail' ? 'bad' : 'warn'}>{c.status}</Badge></td>
                      <td className="mono">{c.name}</td>
                      <td>{c.detail}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </>
        )}
      </StatusCard>
    </div>
  )
}
