import { describe, expect, it } from 'vitest'

import { baseUrl, buildUrl } from '../api/client'

describe('browser API base is same-origin /api', () => {
  it('defaults to the same-origin relative /api path (no env override)', () => {
    expect(baseUrl()).toBe('/api')
    expect(baseUrl().startsWith('http')).toBe(false)
    expect(baseUrl()).not.toContain('127.0.0.1')
  })

  it('authentication client targets /api routes', () => {
    expect(buildUrl('/v1/auth/session')).toBe('/api/v1/auth/session')
    expect(buildUrl('/v1/auth/login')).toBe('/api/v1/auth/login')
    expect(buildUrl('/v1/auth/logout')).toBe('/api/v1/auth/logout')
  })

  it('does not mix localhost and 127.0.0.1 (uses no absolute host at all)', () => {
    const url = buildUrl('/v1/fleet/nodes', { tenant_id: 't1' })
    expect(url).toBe('/api/v1/fleet/nodes?tenant_id=t1')
    expect(url).not.toContain('localhost')
    expect(url).not.toContain('127.0.0.1')
  })
})
