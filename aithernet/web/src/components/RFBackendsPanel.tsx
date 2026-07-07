// Stage 13A.5 focused RF Backends panel. Shows every configured RF backend (legacy stable +
// experimental Marconi), its session state, version/revision, generation, tool/artifact counts,
// workspace, and context freshness, with Start/Stop/Restart/Refresh-tools/Refresh-context
// controls. It does NOT replace the existing GNU Radio panel and is deliberately not a full RF
// laboratory UI. Polling only reads; it never starts a subprocess or a tool call. The
// coordinator/mission worker are NOT wired into these controls (that is Stage 13B/13C).
import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import type { RFBackendStatus, RFContext } from '../api/types'
import { Badge, ErrorNote, StatusCard } from './StatusCard'

function stateTone(state: string) {
  if (state === 'ready') return 'ok' as const
  if (state === 'degraded' || state === 'restarting' || state === 'starting') return 'warn' as const
  if (state === 'failed') return 'bad' as const
  return 'muted' as const
}

export function RFBackendsPanel({ onMutate }: { onMutate?: () => void }) {
  const [refreshKey, setRefreshKey] = useState(0)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const bump = () => {
    setRefreshKey((k) => k + 1)
    onMutate?.()
  }

  const backends = useResource(() => api.getRFBackends(), [refreshKey])
  const contexts = useResource(() => api.getRFContexts(), [refreshKey])

  const contextFor = (id: string): RFContext | undefined =>
    (contexts.data ?? []).find((c) => c.backend_id === id)

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setActionError(null)
    try {
      await fn()
      bump()
    } catch (err) {
      setActionError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <StatusCard
      title="RF Backends (Stage 13A.5)"
      badge={<Badge tone="muted">{(backends.data ?? []).length} backends</Badge>}
      actions={<button onClick={bump} disabled={busy}>Refresh</button>}
    >
      <ErrorNote error={actionError ?? backends.error} />
      {(backends.data ?? []).length === 0 && <p className="muted">No RF backends configured.</p>}

      <ul className="list">
        {(backends.data ?? []).map((b: RFBackendStatus) => {
          const ctx = contextFor(b.backend_id)
          return (
            <li key={b.backend_id} className="list-row" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                <div>
                  <Badge tone={stateTone(b.state)}>{b.state}</Badge>{' '}
                  <strong>{b.display_name}</strong>{' '}
                  <code>{b.backend_id}</code>{' '}
                  {b.experimental ? (
                    <Badge tone="warn">Experimental — simulation/file-analysis backend</Badge>
                  ) : (
                    <Badge tone="ok">stable</Badge>
                  )}
                  {b.default && <Badge tone="info">default</Badge>}
                </div>
              </div>
              <div className="muted small">
                version {b.version ?? '—'} · rev {b.source_revision ?? '—'} · gen{' '}
                {b.session_generation ?? '—'} · pid {b.pid ?? '—'} · {b.tool_count} tools ·{' '}
                {b.active_requests} active · workspace {b.workspace ?? '—'}
              </div>
              <div className="muted small">
                context: {ctx ? `${ctx.stale ? 'stale' : 'fresh'}${ctx.stale_reason ? ` (${ctx.stale_reason})` : ''}` : '—'}
                {' · '}artifacts: {ctx?.artifact_count ?? 0}
                {b.last_error ? ` · error: ${b.last_error}` : ''}
              </div>
              <div className="row-actions">
                {b.state !== 'ready' && b.enabled && (
                  <button disabled={busy} onClick={() => run(() => api.startRFBackend(b.backend_id))}>
                    Start
                  </button>
                )}
                {b.state === 'ready' && (
                  <button disabled={busy} onClick={() => run(() => api.stopRFBackend(b.backend_id))}>
                    Stop
                  </button>
                )}
                <button disabled={busy} onClick={() => run(() => api.restartRFBackend(b.backend_id))}>
                  Restart
                </button>
                <button disabled={busy} onClick={() => run(() => api.refreshRFTools(b.backend_id))}>
                  Refresh tools
                </button>
                <button disabled={busy} onClick={() => run(() => api.refreshRFContext(b.backend_id))}>
                  Refresh context
                </button>
              </div>
            </li>
          )
        })}
      </ul>
    </StatusCard>
  )
}
