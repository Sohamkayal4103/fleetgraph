import { useState } from 'react'
import { api } from './api/client'
import { useResource } from './api/hooks'
import { AgentPanel } from './components/AgentPanel'
import { ArtifactsPage } from './components/ArtifactsPage'
import { CodingAgentPanel } from './components/CodingAgentPanel'
import { CommunicationsPanel } from './components/CommunicationsPanel'
import { ConversationsPage } from './components/ConversationsPage'
import { CoordinatorPanel } from './components/CoordinatorPanel'
import { DiagnosticsPage } from './components/DiagnosticsPage'
import { DistributedMissionPanel } from './components/DistributedMissionPanel'
import { EventStream } from './components/EventStream'
import { FleetPage } from './components/FleetPage'
import { DataPrivacyPage } from './components/DataPrivacyPage'
import { ExternalAgentsPage } from './components/ExternalAgentsPage'
import { FieldPage } from './components/FieldPage'
import { GnuRadioContextPanel } from './components/GnuRadioContextPanel'
import { HardwarePage } from './components/HardwarePage'
import { McpPanel } from './components/McpPanel'
import { McpSessionPanel } from './components/McpSessionPanel'
import { MissionComposer } from './components/MissionComposer'
import { MissionExecutionPanel } from './components/MissionExecutionPanel'
import { MissionList } from './components/MissionList'
import { MissionStepPanel } from './components/MissionStepPanel'
import { NodeStatusPanel } from './components/NodeStatusPanel'
import { OperationsPage } from './components/OperationsPage'
import { OverviewPage } from './components/OverviewPage'
import { RemoteMissionsPage } from './components/RemoteMissionsPage'
import { RFBackendsPanel } from './components/RFBackendsPanel'
import { Badge } from './components/StatusCard'
import { TransportPanel } from './components/TransportPanel'

// Top-level operational areas (Part B). A lightweight in-app view switch — no router dependency.
const VIEWS = [
  'Overview',
  'Missions',
  'Conversations',
  'Fleet',
  'Artifacts',
  'Remote Missions',
  'RF Backends',
  'Hardware',
  'Field',
  'External Agents',
  'Data & Privacy',
  'Operations',
  'Diagnostics',
] as const
type View = (typeof VIEWS)[number]

export default function App() {
  const [refreshKey, setRefreshKey] = useState(0)
  const [selectedMissionId, setSelectedMissionId] = useState<string | null>(null)
  const [view, setView] = useState<View>('Overview')
  const bump = () => setRefreshKey((k) => k + 1)

  const node = useResource(() => api.getNodeStatus(), [refreshKey])

  // Only a real network failure is "backend unavailable" — an HTTP 4xx/5xx is a reachable
  // backend reporting an error, and must not be mislabeled as an outage.
  const unreachable = node.category === 'network' || node.category === 'timeout'
  const connection = node.data
    ? 'connected'
    : unreachable
      ? 'unavailable'
      : node.error
        ? 'error'
        : 'connecting'

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-left">
          <h1>Aithernet</h1>
          {node.data ? (
            <span className="node-label">
              {node.data.node_name} · {node.data.runtime_status}
            </span>
          ) : (
            <span className="node-label muted">node status unavailable</span>
          )}
        </div>
        <div className="topbar-right">
          <span className="api-url" title="VITE_AITHERNET_API_BASE_URL">
            API: {api.baseUrl}
          </span>
          {connection === 'connected' && <Badge tone="ok">connected</Badge>}
          {connection === 'connecting' && <Badge tone="muted">connecting…</Badge>}
          {connection === 'unavailable' && <Badge tone="bad">backend unavailable</Badge>}
          {connection === 'error' && <Badge tone="warn">error</Badge>}
          <button onClick={bump}>Refresh</button>
        </div>
      </header>

      <nav className="nav-tabs" aria-label="Dashboard areas">
        {VIEWS.map((v) => (
          <button
            key={v}
            type="button"
            className={`nav-tab${view === v ? ' nav-tab-active' : ''}`}
            aria-current={view === v ? 'page' : undefined}
            onClick={() => setView(v)}
          >
            {v}
          </button>
        ))}
      </nav>

      {connection === 'unavailable' && (
        <div className="banner banner-bad">
          Cannot reach the node at {api.baseUrl}. Start it with <code>aithernet start</code>, or set{' '}
          <code>VITE_AITHERNET_API_BASE_URL</code>. {node.error}
        </div>
      )}

      <main className="view">
        {view === 'Overview' && (
          <>
            <OverviewPage />
            <div className="grid">
              <NodeStatusPanel status={node.data} error={unreachable ? null : node.error} loading={node.loading} />
              <CoordinatorPanel refreshKey={refreshKey} />
              <CodingAgentPanel refreshKey={refreshKey} />
            </div>
          </>
        )}

        {view === 'Missions' && (
          <div className="grid">
            <MissionComposer
              onMissionCreated={(missionId) => {
                setSelectedMissionId(missionId)
                bump()
              }}
            />
            <MissionList refreshKey={refreshKey} selectedMissionId={selectedMissionId} onSelect={setSelectedMissionId} />
            <MissionExecutionPanel selectedMissionId={selectedMissionId} onMutate={bump} />
            <MissionStepPanel selectedMissionId={selectedMissionId} onStepRun={bump} />
            <DistributedMissionPanel selectedMissionId={selectedMissionId} />
            <CommunicationsPanel selectedMissionId={selectedMissionId} />
          </div>
        )}

        {view === 'Conversations' && <ConversationsPage />}

        {view === 'Artifacts' && <ArtifactsPage />}

        {view === 'Remote Missions' && <RemoteMissionsPage />}

        {view === 'Fleet' && (
          <>
            <FleetPage />
            <div className="grid">
              <TransportPanel onMutate={bump} />
              <AgentPanel onMutate={bump} />
            </div>
          </>
        )}

        {view === 'RF Backends' && (
          <div className="grid">
            <RFBackendsPanel onMutate={bump} />
            <McpSessionPanel refreshKey={refreshKey} onMutate={bump} />
            <GnuRadioContextPanel refreshKey={refreshKey} onMutate={bump} />
            <McpPanel onMutate={bump} />
          </div>
        )}

        {view === 'Hardware' && <HardwarePage />}

        {view === 'Field' && <FieldPage />}

        {view === 'External Agents' && <ExternalAgentsPage />}

        {view === 'Data & Privacy' && <DataPrivacyPage />}

        {view === 'Operations' && <OperationsPage />}

        {view === 'Diagnostics' && (
          <>
            <DiagnosticsPage />
            <div className="grid">
              <EventStream />
            </div>
          </>
        )}
      </main>

      <footer className="footer">
        <span>
          Aithernet operator dashboard · live persisted data only · ACK ≠ semantic reply · trust ≠
          authorization · reads never send or resume
        </span>
      </footer>
    </div>
  )
}
