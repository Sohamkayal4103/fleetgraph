import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { NodeStateLabel, nodeStateMeta } from '../components/NodeStateLabel'
import type { NodeState } from '../api/types'

const CASES: { state: NodeState; label: string }[] = [
  { state: 'online', label: 'Online' },
  { state: 'recent', label: 'Recently seen' },
  { state: 'stale', label: 'Stale' },
  { state: 'offline', label: 'Offline' },
  { state: 'revoked', label: 'Revoked' },
  { state: 'unknown', label: 'Unknown' },
]

describe('NodeStateLabel', () => {
  it('maps every node state to an accessible text label (not color alone)', () => {
    for (const { state, label } of CASES) {
      const meta = nodeStateMeta(state)
      expect(meta.label).toBe(label)
      expect(meta.icon.length).toBeGreaterThan(0)
    }
  })

  it('renders a visible text label and an aria-label for screen readers', () => {
    render(<NodeStateLabel state="stale" />)
    // Visible text present
    expect(screen.getByText('Stale')).toBeInTheDocument()
    // Accessible name conveys state without relying on color
    expect(screen.getByRole('status')).toHaveAttribute('aria-label', 'Node state: Stale')
  })

  it('falls back to Unknown for an unrecognized state', () => {
    expect(nodeStateMeta('bogus' as NodeState).label).toBe('Unknown')
  })
})
