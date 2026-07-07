import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  buildUrl,
  baseUrl,
  setCsrfToken,
  createEnrollmentCode,
  getFleetNodes,
} from '../api/client'

function mockFetchOk(body: unknown) {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('api client URL + CSRF behavior', () => {
  beforeEach(() => {
    setCsrfToken(null)
  })

  it('builds fully-qualified URLs against the control plane base', () => {
    expect(buildUrl('/v1/auth/session')).toBe(`${baseUrl()}/v1/auth/session`)
    expect(buildUrl('v1/fleet/nodes', { tenant_id: 't1' })).toBe(
      `${baseUrl()}/v1/fleet/nodes?tenant_id=t1`,
    )
    // empty/undefined query values are dropped
    expect(buildUrl('/v1/fleet/nodes', { tenant_id: undefined })).toBe(
      `${baseUrl()}/v1/fleet/nodes`,
    )
  })

  it('includes the X-CSRF-Token header on a POST and uses credentials include', async () => {
    setCsrfToken('csrf-abc-123')
    const fetchMock = mockFetchOk({ code: 'ENR1', tenant_id: 't1', expires_at: 'soon' })

    await createEnrollmentCode('t1')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${baseUrl()}/v1/enrollment-codes`)
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('include')
    const headers = init.headers as Record<string, string>
    expect(headers['X-CSRF-Token']).toBe('csrf-abc-123')
    expect(headers['Content-Type']).toBe('application/json')
    expect(init.body).toBe(JSON.stringify({ tenant_id: 't1' }))
  })

  it('does NOT add the CSRF header on a GET', async () => {
    setCsrfToken('csrf-abc-123')
    const fetchMock = mockFetchOk([])

    await getFleetNodes('t1')

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${baseUrl()}/v1/fleet/nodes?tenant_id=t1`)
    expect(init.method).toBe('GET')
    const headers = init.headers as Record<string, string>
    expect(headers['X-CSRF-Token']).toBeUndefined()
  })
})
