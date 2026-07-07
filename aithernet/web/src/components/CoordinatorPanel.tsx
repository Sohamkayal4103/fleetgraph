import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useResource } from '../api/hooks'
import type { CoordinatorDiagnostics } from '../api/types'
import { Badge, Chips, ErrorNote, Field, Loading, ReadyBadge, StatusCard } from './StatusCard'

//: Human-readable explanation for each coordinator readiness reason code.
const READINESS_HINTS: Record<string, string> = {
  executable: 'No executable is configured.',
  executable_not_found: 'The configured executable was not found on PATH.',
  executable_not_runnable:
    'The executable resolved but did not run cleanly (the Gemini CLI requires Node.js 20+).',
  executable_provider_mismatch:
    'The executable does not identify as this provider — provider and executable must match (gemini_cli ↔ gemini).',
}

function readinessHint(reasons: string[]): string {
  for (const reason of reasons) {
    if (READINESS_HINTS[reason]) return READINESS_HINTS[reason]
  }
  return 'The coordinator is not configured.'
}

export function CoordinatorPanel({ refreshKey }: { refreshKey: number }) {
  const status = useResource(() => api.getCoordinatorStatus(), [refreshKey])
  const [diag, setDiag] = useState<CoordinatorDiagnostics | null>(null)
  const [diagError, setDiagError] = useState<string | null>(null)
  const [diagBusy, setDiagBusy] = useState(false)

  async function loadDiagnostics(probe: boolean) {
    setDiagBusy(true)
    setDiagError(null)
    try {
      setDiag(await api.getCoordinatorDiagnostics(probe))
    } catch (err) {
      setDiagError(errorMessage(err))
    } finally {
      setDiagBusy(false)
    }
  }

  const isCli = (s: { executable: string | null }) => s.executable !== null

  return (
    <StatusCard
      title="Coordinator"
      badge={status.data ? <ReadyBadge configured={status.data.configured} /> : undefined}
    >
      <ErrorNote error={status.error} />
      <Loading loading={status.loading && !status.data} />
      {status.data && (
        <>
          <Field label="Provider">{status.data.provider}</Field>
          {isCli(status.data) ? (
            <>
              <Field label="Executable">{status.data.executable}</Field>
              <Field label="Model">{status.data.model ?? 'default'}</Field>
              {status.data.cli_version && (
                <Field label="CLI version">{status.data.cli_version}</Field>
              )}
            </>
          ) : (
            <>
              <Field label="Model">{status.data.model ?? 'default'}</Field>
              <Field label="Base URL">{status.data.base_url_configured ? 'set' : 'unset'}</Field>
              <Field label="API key">{status.data.api_key_configured ? 'set' : 'unset'}</Field>
            </>
          )}
          {status.data.missing_configuration.length > 0 && (
            <Field label="Not ready">
              <Chips items={status.data.missing_configuration} tone="warn" />
            </Field>
          )}
          {!status.data.configured && (
            <p className="muted small-note">
              {readinessHint(status.data.missing_configuration)}
            </p>
          )}
          <Field label="Active">
            <Chips items={status.data.active_capabilities} tone="ok" />
          </Field>
        </>
      )}

      <div className="button-row">
        <button className="small" disabled={diagBusy} onClick={() => loadDiagnostics(false)}>
          Diagnostics
        </button>
        <button className="small" disabled={diagBusy} onClick={() => loadDiagnostics(true)}>
          Probe (one inference)
        </button>
      </div>
      <ErrorNote error={diagError} />
      {diag && <DiagnosticsView diag={diag} />}
    </StatusCard>
  )
}

function DiagnosticsView({ diag }: { diag: CoordinatorDiagnostics }) {
  const probe = diag.probe
  return (
    <div className="result-box">
      <Field label="Configured">
        <ReadyBadge configured={diag.configured} />
      </Field>
      {diag.executable !== null && (
        <>
          <Field label="Executable resolves">{diag.executable_resolves ? 'yes' : 'no'}</Field>
          {diag.resolved_executable_path && (
            <Field label="Path">{diag.resolved_executable_path}</Field>
          )}
          {diag.cli_version && <Field label="CLI version">{diag.cli_version}</Field>}
          {diag.working_directory && <Field label="Runtime dir">{diag.working_directory}</Field>}
        </>
      )}
      <Field label="Model">{diag.model}</Field>
      <Field label="Probe">
        {!probe.attempted ? (
          <Badge tone="muted">not run</Badge>
        ) : probe.succeeded ? (
          <Badge tone="ok">
            ok — {(probe.model_reported ?? []).join(', ') || 'default'}
            {probe.elapsed_ms != null ? ` (${probe.elapsed_ms} ms)` : ''}
          </Badge>
        ) : (
          <Badge tone="bad">failed</Badge>
        )}
      </Field>
      {probe.attempted && probe.succeeded && (probe.tool_calls ?? 0) === 0 && (
        <p className="muted small-note">Reasoning-only: 0 tool calls (as required).</p>
      )}
      {probe.attempted && probe.succeeded === false && probe.error && (
        <ErrorNote error={`[${probe.error_type}] ${probe.error}`} />
      )}
      {diag.hints.length > 0 && (
        <ul className="hints">
          {diag.hints.map((hint, idx) => (
            <li key={idx}>{hint}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
