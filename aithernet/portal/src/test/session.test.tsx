import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { getSession, login, logout } from '../api/client'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(body === undefined ? '' : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('getSession normalizes the flat server principal', () => {
  it('maps {user_id,email,is_platform_admin} -> {authenticated,user}', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({ user_id: 'u1', email: 'a@b.invalid', is_platform_admin: true, csrf_token: 't' }),
      ),
    )
    const s = await getSession()
    expect(s.authenticated).toBe(true)
    expect(s.user).toEqual({ user_id: 'u1', email: 'a@b.invalid', is_platform_admin: true })
    expect(s.csrf_token).toBe('t')
  })

  it('throws on 401 so the provider treats it as unauthenticated', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'not_authenticated' }, 401)))
    await expect(getSession()).rejects.toMatchObject({ status: 401 })
  })
})

describe('auth requests send credentials: include', () => {
  it('login, session and logout all include credentials', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ user_id: 'u', email: 'e@x.invalid', csrf_token: 't' }))
    vi.stubGlobal('fetch', fetchMock)
    await login('e@x.invalid', 'pw')
    await getSession()
    await logout()
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>
    expect(calls.length).toBeGreaterThanOrEqual(3)
    for (const call of calls) {
      expect(call[1].credentials).toBe('include')
    }
  })
})

describe('SessionProvider auth state after login + loading behavior', () => {
  it('starts loading, resolves to authenticated, and signIn refreshes state', async () => {
    vi.resetModules()
    const getSessionMock = vi.fn().mockResolvedValue({
      authenticated: true,
      user: { user_id: 'u1', email: 'admin@x.invalid', is_platform_admin: true },
      csrf_token: 't',
    })
    const loginMock = vi.fn().mockResolvedValue({ user_id: 'u1', email: 'admin@x.invalid', csrf_token: 't' })
    vi.doMock('../api/client', () => ({
      api: { getSession: getSessionMock, login: loginMock, logout: vi.fn() },
    }))
    const { SessionProvider, useSession } = await import('../session')
    const { result } = renderHook(() => useSession(), { wrapper: SessionProvider })

    expect(result.current.loading).toBe(true) // loading-state behavior
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.session?.authenticated).toBe(true)
    expect(result.current.session?.user?.email).toBe('admin@x.invalid')

    await result.current.signIn('admin@x.invalid', 'pw') // auth-state refresh after login
    expect(loginMock).toHaveBeenCalledWith('admin@x.invalid', 'pw')
    await waitFor(() => expect(result.current.session?.authenticated).toBe(true))
    vi.doUnmock('../api/client')
  })
})
