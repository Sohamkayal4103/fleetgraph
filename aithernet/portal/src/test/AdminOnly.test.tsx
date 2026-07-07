import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AdminOnly, isPlatformAdmin } from '../components/AdminOnly'
import type { Session } from '../api/types'

function session(isAdmin: boolean, authenticated = true): Session {
  return {
    authenticated,
    user: { user_id: 'u1', email: 'u@example.com', is_platform_admin: isAdmin },
    csrf_token: 'x',
  }
}

describe('AdminOnly route/render guard', () => {
  it('isPlatformAdmin is true only for authenticated platform admins', () => {
    expect(isPlatformAdmin(session(true))).toBe(true)
    expect(isPlatformAdmin(session(false))).toBe(false)
    expect(isPlatformAdmin(session(true, false))).toBe(false)
    expect(isPlatformAdmin(null)).toBe(false)
    expect(isPlatformAdmin(undefined)).toBe(false)
  })

  it('hides admin controls when is_platform_admin is false', () => {
    render(
      <AdminOnly session={session(false)} fallback={<span>blocked</span>}>
        <button>Publish release</button>
      </AdminOnly>,
    )
    expect(screen.queryByRole('button', { name: 'Publish release' })).not.toBeInTheDocument()
    expect(screen.getByText('blocked')).toBeInTheDocument()
  })

  it('renders admin controls when is_platform_admin is true', () => {
    render(
      <AdminOnly session={session(true)}>
        <button>Publish release</button>
      </AdminOnly>,
    )
    expect(screen.getByRole('button', { name: 'Publish release' })).toBeInTheDocument()
  })
})
