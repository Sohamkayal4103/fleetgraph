import { api } from '../api/client'
import { useResource } from '../api/hooks'
import { Empty, ErrorNote, Loading, StatusBadge, StatusCard, formatTime, shortId } from './StatusCard'

export function MissionList({
  refreshKey,
  selectedMissionId,
  onSelect,
}: {
  refreshKey: number
  selectedMissionId: string | null
  onSelect: (missionId: string) => void
}) {
  const { data, error, loading, reload } = useResource(() => api.getMissions(), [refreshKey])

  return (
    <StatusCard
      title="Missions"
      actions={
        <button className="small" onClick={reload}>
          Refresh
        </button>
      }
    >
      <ErrorNote error={error} />
      <Loading loading={loading && !data} />
      {data && data.length === 0 && <Empty>No missions yet.</Empty>}
      {data && data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Status</th>
              <th>Source</th>
              <th>Content</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {data.map((mission) => (
              <tr
                key={mission.id}
                className={mission.id === selectedMissionId ? 'row-selected' : 'row-clickable'}
                onClick={() => onSelect(mission.id)}
              >
                <td title={mission.id}>{shortId(mission.id)}</td>
                <td>
                  <StatusBadge status={mission.status} />
                </td>
                <td>{mission.source_type}</td>
                <td className="cell-truncate" title={mission.content}>
                  {mission.content}
                </td>
                <td>{formatTime(mission.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </StatusCard>
  )
}
