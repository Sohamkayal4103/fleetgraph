import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from './client'

function fetchReturning(impl: () => Promise<Response>): void {
  globalThis.fetch = vi.fn(impl) as unknown as typeof fetch
}

describe('api client error categorization', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('labels a real connection failure as network ("backend unavailable")', async () => {
    fetchReturning(() => Promise.reject(new TypeError('Failed to fetch')))
    const err = (await api.getNodeStatus().catch((e) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.category).toBe('network')
    expect(err.unreachable).toBe(true)
    expect(err.message).toContain('Backend unavailable')
  })

  it('surfaces a 400 JSON detail without mislabeling it as an outage', async () => {
    fetchReturning(async () =>
      new Response(JSON.stringify({ detail: 'mission content is required' }), {
        status: 400,
        headers: { 'content-type': 'application/json' },
      }),
    )
    const err = (await api.getMissions().catch((e) => e)) as ApiError
    expect(err.category).toBe('http')
    expect(err.status).toBe(400)
    expect(err.detail).toBe('mission content is required')
    expect(err.unreachable).toBe(false)
  })

  it('surfaces a 500 text body and is NOT treated as unreachable', async () => {
    fetchReturning(async () => new Response('internal explosion', { status: 500 }))
    const err = (await api.getNodeStatus().catch((e) => e)) as ApiError
    expect(err.category).toBe('http')
    expect(err.status).toBe(500)
    expect(err.detail).toContain('internal explosion')
    expect(err.unreachable).toBe(false) // the core bug: HTTP error != network outage
    expect(err.message).toContain('[500]')
  })

  it('reports a malformed 2xx JSON body as a parse error', async () => {
    fetchReturning(async () => new Response('definitely not json', { status: 200 }))
    const err = (await api.getNodeStatus().catch((e) => e)) as ApiError
    expect(err.category).toBe('parse')
    expect(err.unreachable).toBe(false)
  })

  it('labels an aborted/timed-out request as timeout (not network)', async () => {
    fetchReturning(() => {
      const abort = new Error('aborted')
      abort.name = 'AbortError'
      return Promise.reject(abort)
    })
    const err = (await api.getNodeStatus().catch((e) => e)) as ApiError
    expect(err.category).toBe('timeout')
    expect(err.unreachable).toBe(false)
    expect(err.message.toLowerCase()).toContain('timed out')
  })

  it('requests coordinator diagnostics with the probe flag and returns the parsed body', async () => {
    let requestedUrl = ''
    globalThis.fetch = vi.fn(async (url: string | URL | Request) => {
      requestedUrl = String(url)
      return new Response(
        JSON.stringify({ provider: 'gemini_cli', configured: true, probe: { attempted: true } }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      )
    }) as unknown as typeof fetch
    const diag = await api.getCoordinatorDiagnostics(true)
    expect(requestedUrl).toContain('/coordinator/diagnostics')
    expect(requestedUrl).toContain('probe=true')
    expect(diag.provider).toBe('gemini_cli')
    expect(diag.configured).toBe(true)
  })

  it('reads the managed MCP session status (Stage 11B)', async () => {
    let url = ''
    globalThis.fetch = vi.fn(async (u: string | URL | Request) => {
      url = String(u)
      return new Response(
        JSON.stringify({ session_id: 's1', state: 'ready', generation: 2, pid: 999 }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      )
    }) as unknown as typeof fetch
    const session = await api.getMcpSession()
    expect(url).toContain('/mcp/session')
    expect(session.state).toBe('ready')
    expect(session.generation).toBe(2)
  })

  it('refreshes the GNU Radio context via the right endpoint', async () => {
    let method = ''
    let url = ''
    globalThis.fetch = vi.fn(async (u: string | URL | Request, init?: RequestInit) => {
      url = String(u)
      method = String(init?.method)
      return new Response(JSON.stringify({ flowgraph_status: 'none', stale: false }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }) as unknown as typeof fetch
    const ctx = await api.refreshGnuRadioContext()
    expect(method).toBe('POST')
    expect(url).toContain('/gnuradio/context/refresh')
    expect(ctx.flowgraph_status).toBe('none')
  })

  it('prefers JSON detail, then falls back to an explicit message field', async () => {
    fetchReturning(async () =>
      new Response(JSON.stringify({ message: 'fallback message field' }), {
        status: 502,
        headers: { 'content-type': 'application/json' },
      }),
    )
    const err = (await api.getNodeStatus().catch((e) => e)) as ApiError
    expect(err.detail).toBe('fallback message field')
  })
})
