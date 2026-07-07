import type { NodeStatus } from '../api/types'
import { ErrorNote, Field, Loading, StatusCard } from './StatusCard'
import { formatTime } from './StatusCard'

export function NodeStatusPanel({
  status,
  error,
  loading,
}: {
  status: NodeStatus | null
  error: string | null
  loading: boolean
}) {
  return (
    <StatusCard title="Node">
      <ErrorNote error={error} />
      {!status ? (
        <Loading loading={loading} />
      ) : (
        <>
          <Field label="Name">{status.node_name}</Field>
          <Field label="ID">{status.node_id}</Field>
          <Field label="Runtime">{status.runtime_status}</Field>
          <Field label="Version">{status.version}</Field>
          <Field label="Database">{status.database_path}</Field>
          <Field label="Started">{formatTime(status.started_at)}</Field>
          <Field label="Missions">{status.mission_count}</Field>
          <Field label="Events">{status.event_count}</Field>
        </>
      )}
    </StatusCard>
  )
}
