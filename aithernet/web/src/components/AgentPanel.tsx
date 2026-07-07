import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, parseJsonObject, useResource } from '../api/hooks'
import type { ExternalAgentMessageResponse, ExternalAgentStatusValue } from '../api/types'
import {
  Badge,
  Empty,
  ErrorNote,
  Field,
  Loading,
  StatusBadge,
  StatusCard,
  formatTime,
  shortId,
} from './StatusCard'

const STATUS_VALUES: ExternalAgentStatusValue[] = ['connected', 'disconnected', 'disabled']

export function AgentPanel({ onMutate }: { onMutate: () => void }) {
  const [localTick, setLocalTick] = useState(0)
  const agents = useResource(() => api.getAgents(), [localTick])
  const roster = useResource(() => api.getAgentsStatus(), [localTick])

  // Connect form
  const [name, setName] = useState('')
  const [agentType, setAgentType] = useState('external')
  const [endpointUrl, setEndpointUrl] = useState('')
  const [transport, setTransport] = useState('local')
  const [authType, setAuthType] = useState('none')
  const [metadata, setMetadata] = useState('')
  const [connectError, setConnectError] = useState<string | null>(null)
  const [connectBusy, setConnectBusy] = useState(false)

  // Message form
  const [selected, setSelected] = useState<string | null>(null)
  const [messageType, setMessageType] = useState('mission_request')
  const [messageContent, setMessageContent] = useState('')
  const [payload, setPayload] = useState('')
  const [createMission, setCreateMission] = useState(true)
  const [runStep, setRunStep] = useState(false)
  const [messageError, setMessageError] = useState<string | null>(null)
  const [messageBusy, setMessageBusy] = useState(false)
  const [messageResult, setMessageResult] = useState<ExternalAgentMessageResponse | null>(null)

  const messages = useResource(
    () => (selected ? api.getAgentMessages(selected) : Promise.resolve([])),
    [selected, localTick],
  )

  function refreshAll() {
    setLocalTick((t) => t + 1)
    onMutate()
  }

  async function connect() {
    if (!name.trim()) {
      setConnectError('Agent name is required.')
      return
    }
    const meta = parseJsonObject(metadata)
    if (!meta.ok) {
      setConnectError(`metadata: ${meta.error}`)
      return
    }
    setConnectBusy(true)
    setConnectError(null)
    try {
      await api.connectAgent({
        name: name.trim(),
        agent_type: agentType.trim() || 'external',
        endpoint_url: endpointUrl.trim() || null,
        transport: transport.trim() || 'local',
        auth_type: authType.trim() || 'none',
        metadata: meta.value,
      })
      setName('')
      setEndpointUrl('')
      setMetadata('')
      refreshAll()
    } catch (err) {
      setConnectError(errorMessage(err))
    } finally {
      setConnectBusy(false)
    }
  }

  async function changeStatus(id: string, status: ExternalAgentStatusValue) {
    try {
      await api.setAgentStatus(id, status)
      refreshAll()
    } catch (err) {
      setConnectError(errorMessage(err))
    }
  }

  async function sendMessage() {
    if (!selected) {
      setMessageError('Select an agent first.')
      return
    }
    if (!messageContent.trim()) {
      setMessageError('Message content is required.')
      return
    }
    if (runStep && !createMission) {
      setMessageError('run_step requires create_mission (the backend rejects the combination).')
      return
    }
    const parsedPayload = parseJsonObject(payload)
    if (!parsedPayload.ok) {
      setMessageError(`payload: ${parsedPayload.error}`)
      return
    }
    setMessageBusy(true)
    setMessageError(null)
    setMessageResult(null)
    try {
      const result = await api.sendAgentMessage(selected, {
        message_type: messageType.trim() || 'mission_request',
        content: messageContent.trim(),
        payload: parsedPayload.value,
        create_mission: createMission,
        run_step: runStep,
      })
      setMessageResult(result)
      setMessageContent('')
      refreshAll()
    } catch (err) {
      setMessageError(errorMessage(err))
    } finally {
      setMessageBusy(false)
    }
  }

  return (
    <StatusCard
      title="External Agents"
      badge={
        roster.data ? (
          <Badge tone="info">
            {roster.data.connected_count}/{roster.data.total_count} connected
          </Badge>
        ) : undefined
      }
    >
      <p className="muted small-note">
        The node stores <code>endpoint_url</code> but never calls it (no outbound callbacks).
      </p>

      <h3>Connect an agent</h3>
      <div className="inline-fields">
        <label className="stack">
          <span className="field-label">name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="stack">
          <span className="field-label">agent_type</span>
          <input value={agentType} onChange={(e) => setAgentType(e.target.value)} />
        </label>
        <label className="stack">
          <span className="field-label">transport</span>
          <input value={transport} onChange={(e) => setTransport(e.target.value)} />
        </label>
        <label className="stack">
          <span className="field-label">auth_type</span>
          <input value={authType} onChange={(e) => setAuthType(e.target.value)} />
        </label>
      </div>
      <label className="stack">
        <span className="field-label">endpoint_url (optional, stored only)</span>
        <input value={endpointUrl} onChange={(e) => setEndpointUrl(e.target.value)} />
      </label>
      <label className="stack">
        <span className="field-label">metadata JSON (no secrets)</span>
        <textarea value={metadata} onChange={(e) => setMetadata(e.target.value)} rows={2} placeholder="{}" />
      </label>
      <div className="button-row">
        <button disabled={connectBusy} onClick={connect}>
          Connect agent
        </button>
      </div>
      <ErrorNote error={connectError} />

      <h3>Agents</h3>
      <Loading loading={agents.loading && !agents.data} />
      <ErrorNote error={agents.error} />
      {agents.data && agents.data.length === 0 && <Empty>No agents connected.</Empty>}
      {agents.data && agents.data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Type</th>
              <th>Status</th>
              <th>Last seen</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {agents.data.map((agent) => (
              <tr
                key={agent.id}
                className={agent.id === selected ? 'row-selected' : 'row-clickable'}
                onClick={() => setSelected(agent.id)}
              >
                <td title={agent.id}>{agent.name}</td>
                <td>{agent.agent_type}</td>
                <td>
                  <StatusBadge status={agent.status} />
                </td>
                <td>{formatTime(agent.last_seen_at)}</td>
                <td onClick={(e) => e.stopPropagation()}>
                  <select
                    value={agent.status}
                    onChange={(e) => changeStatus(agent.id, e.target.value as ExternalAgentStatusValue)}
                  >
                    {STATUS_VALUES.map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Send message {selected ? `to ${shortId(selected)}` : ''}</h3>
      {!selected && <Empty>Select an agent above to message it.</Empty>}
      {selected && (
        <>
          <div className="inline-fields">
            <label className="stack">
              <span className="field-label">message_type</span>
              <input value={messageType} onChange={(e) => setMessageType(e.target.value)} />
            </label>
          </div>
          <label className="stack">
            <span className="field-label">content</span>
            <textarea
              value={messageContent}
              onChange={(e) => setMessageContent(e.target.value)}
              rows={3}
            />
          </label>
          <label className="stack">
            <span className="field-label">payload JSON</span>
            <textarea value={payload} onChange={(e) => setPayload(e.target.value)} rows={2} placeholder="{}" />
          </label>
          <div className="checkbox-row">
            <label>
              <input
                type="checkbox"
                checked={createMission}
                onChange={(e) => setCreateMission(e.target.checked)}
              />
              create_mission
            </label>
            <label>
              <input type="checkbox" checked={runStep} onChange={(e) => setRunStep(e.target.checked)} />
              run_step
            </label>
          </div>
          <div className="button-row">
            <button disabled={messageBusy} onClick={sendMessage}>
              Send message
            </button>
          </div>
          <ErrorNote error={messageError} />
          {messageResult && (
            <div className="result-box">
              <Field label="Inbound">{messageResult.message.id}</Field>
              {messageResult.mission ? (
                <Field label="Mission">
                  {messageResult.mission.id} <StatusBadge status={messageResult.mission.status} />
                </Field>
              ) : (
                <Field label="Mission">none created</Field>
              )}
              {messageResult.step_response && (
                <Field label="Step">
                  {messageResult.step_response.step.route_target}{' '}
                  <StatusBadge status={messageResult.step_response.step.status} />
                </Field>
              )}
            </div>
          )}

          <h3>Messages</h3>
          <ErrorNote error={messages.error} />
          {messages.data && messages.data.length === 0 && <Empty>No messages for this agent.</Empty>}
          {messages.data && messages.data.length > 0 && (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Dir</th>
                  <th>Type</th>
                  <th>Content</th>
                  <th>Mission</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody>
                {messages.data.map((message) => (
                  <tr key={message.id}>
                    <td>
                      <Badge tone={message.direction === 'inbound' ? 'info' : 'muted'}>
                        {message.direction}
                      </Badge>
                    </td>
                    <td>{message.message_type}</td>
                    <td className="cell-truncate" title={message.content}>
                      {message.content}
                    </td>
                    <td>{message.mission_id ? shortId(message.mission_id) : '—'}</td>
                    <td>{formatTime(message.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </StatusCard>
  )
}
