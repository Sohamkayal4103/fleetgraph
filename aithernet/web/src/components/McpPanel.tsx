import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, parseJsonObject, useResource } from '../api/hooks'
import type { McpDiagnostics, McpToolCall, McpToolInfo } from '../api/types'
import {
  Badge,
  Chips,
  Empty,
  ErrorNote,
  Field,
  JsonPreview,
  Loading,
  ReadyBadge,
  StatusBadge,
  StatusCard,
  formatTime,
  shortId,
} from './StatusCard'

export function McpPanel({ onMutate }: { onMutate: () => void }) {
  const [localTick, setLocalTick] = useState(0)
  const status = useResource(() => api.getMcpStatus(), [localTick])
  const calls = useResource(() => api.getMcpToolCalls(), [localTick])

  const [diagnostics, setDiagnostics] = useState<McpDiagnostics | null>(null)
  const [diagError, setDiagError] = useState<string | null>(null)
  const [diagBusy, setDiagBusy] = useState(false)

  const [tools, setTools] = useState<McpToolInfo[] | null>(null)
  const [toolsError, setToolsError] = useState<string | null>(null)
  const [toolsBusy, setToolsBusy] = useState(false)

  const [toolName, setToolName] = useState('')
  const [args, setArgs] = useState('')
  const [callError, setCallError] = useState<string | null>(null)
  const [callBusy, setCallBusy] = useState(false)
  const [lastCall, setLastCall] = useState<McpToolCall | null>(null)

  async function loadDiagnostics(probe: boolean) {
    setDiagBusy(true)
    setDiagError(null)
    try {
      setDiagnostics(await api.getMcpDiagnostics(probe))
    } catch (err) {
      setDiagError(errorMessage(err))
    } finally {
      setDiagBusy(false)
    }
  }

  async function loadTools() {
    setToolsBusy(true)
    setToolsError(null)
    try {
      setTools(await api.getMcpTools())
    } catch (err) {
      // A real backend error (e.g. 502 when gr-mcp/GNU Radio is unavailable) is shown as-is.
      setToolsError(errorMessage(err))
    } finally {
      setToolsBusy(false)
    }
  }

  async function callTool() {
    if (!toolName.trim()) {
      setCallError('Tool name is required (use “List tools” to discover real names).')
      return
    }
    const parsedArgs = parseJsonObject(args)
    if (!parsedArgs.ok) {
      setCallError(`arguments: ${parsedArgs.error}`)
      return
    }
    setCallBusy(true)
    setCallError(null)
    setLastCall(null)
    try {
      const call = await api.callMcpTool(toolName.trim(), { arguments: parsedArgs.value })
      setLastCall(call)
      setLocalTick((t) => t + 1)
      onMutate()
    } catch (err) {
      setCallError(errorMessage(err))
    } finally {
      setCallBusy(false)
    }
  }

  return (
    <StatusCard
      title="GNU Radio MCP"
      badge={status.data ? <ReadyBadge configured={status.data.configured} /> : undefined}
    >
      <ErrorNote error={status.error} />
      <Loading loading={status.loading && !status.data} />
      {status.data && (
        <>
          <Field label="Provider">{status.data.provider}</Field>
          <Field label="Command">{status.data.command ?? '(unset)'}</Field>
          {status.data.missing_configuration.length > 0 && (
            <Field label="Missing">
              <Chips items={status.data.missing_configuration} tone="warn" />
            </Field>
          )}
        </>
      )}

      <div className="button-row">
        <button className="small" disabled={diagBusy} onClick={() => loadDiagnostics(false)}>
          Diagnostics
        </button>
        <button className="small" disabled={diagBusy} onClick={() => loadDiagnostics(true)}>
          Probe (launch server)
        </button>
        <button className="small" disabled={toolsBusy} onClick={loadTools}>
          List tools
        </button>
      </div>
      <ErrorNote error={diagError} />
      {diagnostics && <DiagnosticsView diagnostics={diagnostics} />}

      <ErrorNote error={toolsError} />
      {tools && tools.length === 0 && <Empty>Server returned no tools.</Empty>}
      {tools && tools.length > 0 && (
        <div className="result-box">
          <span className="field-label">Tools (from the live server — never hardcoded):</span>
          <div className="chips">
            {tools.map((tool) => (
              <button
                key={tool.name}
                className="chip chip-info chip-button"
                title={tool.description ?? ''}
                onClick={() => setToolName(tool.name)}
              >
                {tool.name}
              </button>
            ))}
          </div>
        </div>
      )}

      <h3>Call a tool</h3>
      <label className="stack">
        <span className="field-label">tool_name</span>
        <input value={toolName} onChange={(e) => setToolName(e.target.value)} placeholder="(pick from List tools)" />
      </label>
      <label className="stack">
        <span className="field-label">arguments JSON</span>
        <textarea value={args} onChange={(e) => setArgs(e.target.value)} rows={2} placeholder="{}" />
      </label>
      <div className="button-row">
        <button disabled={callBusy} onClick={callTool}>
          Call tool
        </button>
      </div>
      <ErrorNote error={callError} />
      {lastCall && (
        <div className="result-box">
          <Field label="Call">
            {shortId(lastCall.id)} <StatusBadge status={lastCall.status} />
          </Field>
          {lastCall.error && <ErrorNote error={lastCall.error} />}
          <JsonPreview value={lastCall.result} />
        </div>
      )}

      <h3>Persisted calls</h3>
      <ErrorNote error={calls.error} />
      {calls.data && calls.data.length === 0 && <Empty>No tool calls yet.</Empty>}
      {calls.data && calls.data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Tool</th>
              <th>Caller</th>
              <th>Status</th>
              <th>Time</th>
            </tr>
          </thead>
          <tbody>
            {calls.data.map((call) => (
              <tr key={call.id}>
                <td className="mono">{call.tool_name}</td>
                <td>{call.caller}</td>
                <td>
                  <StatusBadge status={call.status} />
                </td>
                <td>{formatTime(call.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </StatusCard>
  )
}

function DiagnosticsView({ diagnostics }: { diagnostics: McpDiagnostics }) {
  const probe = diagnostics.probe
  return (
    <div className="result-box">
      <Field label="Configured">
        <ReadyBadge configured={diagnostics.configured} />
      </Field>
      <Field label="Command resolves">{diagnostics.command_resolves ? 'yes' : 'no'}</Field>
      {diagnostics.resolved_command_path && (
        <Field label="Resolved path">{diagnostics.resolved_command_path}</Field>
      )}
      <Field label="Probe">
        {!probe.attempted ? (
          <Badge tone="muted">not run</Badge>
        ) : probe.succeeded ? (
          <Badge tone="ok">ok — {probe.tool_count} tool(s)</Badge>
        ) : (
          <Badge tone="bad">failed</Badge>
        )}
      </Field>
      {probe.attempted && probe.succeeded && probe.tool_names && (
        <Field label="Tools">
          <Chips items={probe.tool_names} tone="info" />
        </Field>
      )}
      {probe.attempted && probe.succeeded === false && probe.error && (
        <ErrorNote error={`[${probe.error_type}] ${probe.error}`} />
      )}
      {diagnostics.hints.length > 0 && (
        <ul className="hints">
          {diagnostics.hints.map((hint, idx) => (
            <li key={idx}>{hint}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
