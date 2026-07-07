import type { NodeState } from '../api/types'

// Accessibility: node state is conveyed with TEXT and an ICON glyph, never color alone.
// Each entry has a stable, human-readable label and an aria description.
interface StateMeta {
  label: string
  icon: string
  description: string
}

const STATE_META: Record<NodeState, StateMeta> = {
  online: { label: 'Online', icon: '●', description: 'Node is online and reporting.' },
  recent: { label: 'Recently seen', icon: '◐', description: 'Node was seen recently.' },
  stale: { label: 'Stale', icon: '◑', description: 'Node has not reported recently.' },
  offline: { label: 'Offline', icon: '○', description: 'Node is offline.' },
  revoked: { label: 'Revoked', icon: '⊘', description: 'Node enrollment has been revoked.' },
  degraded: { label: 'Degraded', icon: '◍', description: 'Node reports non-ready service health.' },
  never_seen: { label: 'Never seen', icon: '◌', description: 'Node has never sent a heartbeat.' },
  unknown: { label: 'Unknown', icon: '?', description: 'Node state is unknown.' },
}

export function nodeStateMeta(state: NodeState): StateMeta {
  return STATE_META[state] ?? STATE_META.unknown
}

export function NodeStateLabel({ state }: { state: NodeState }) {
  const meta = nodeStateMeta(state)
  return (
    <span
      className={`node-state node-state--${state}`}
      role="status"
      aria-label={`Node state: ${meta.label}`}
      title={meta.description}
    >
      <span aria-hidden="true">{meta.icon}</span> {meta.label}
    </span>
  )
}
