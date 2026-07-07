// Admin "Resend invite" button (live acceptance defect #1): it must call the resend endpoint
// (NOT silently re-approve), show a clear success message carrying the new expiry, and surface a
// truthful error when the server refuses (e.g. an already-accepted invitation).
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AdminPortal } from '../pages/AdminPortal'
import { RouterProvider } from '../routes/router'
import { SessionProvider } from '../session'

afterEach(() => {
  vi.unstubAllGlobals()
  window.location.hash = ''
})

const APPROVED_REQUEST = {
  id: 'req-1',
  email: 'approved@example.test',
  status: 'approved',
  created_at: '2026-06-01T00:00:00Z',
  invitation_id: 'inv-1',
  invitation_status: 'pending',
}

function mountAdmin(handlers: Record<string, (init: RequestInit) => Response>) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.toString(), 'http://admin.test')
    const method = (init.method ?? 'GET').toUpperCase()
    if (/\/v1\/auth\/session/.test(url.pathname)) {
      return new Response(JSON.stringify({ user_id: 'a', email: 'admin@x', is_platform_admin: true, authenticated: true, csrf_token: 'c' }), { status: 200, headers: { 'content-type': 'application/json' } })
    }
    if (/\/v1\/admin\/early-access\/[^/]+\/resend/.test(url.pathname) && method === 'POST') {
      return handlers.resend(init)
    }
    if (/\/v1\/admin\/early-access/.test(url.pathname)) {
      return new Response(JSON.stringify({ requests: [APPROVED_REQUEST], counts: { approved: 1 } }), { status: 200, headers: { 'content-type': 'application/json' } })
    }
    return new Response(JSON.stringify({}), { status: 200, headers: { 'content-type': 'application/json' } })
  }))
  window.location.hash = '/admin/early-access'
  return render(
    <RouterProvider>
      <SessionProvider>
        <AdminPortal />
      </SessionProvider>
    </RouterProvider>,
  )
}

describe('admin Resend invite', () => {
  it('calls the resend endpoint and shows the new expiry on success', async () => {
    const calls: string[] = []
    mountAdmin({
      resend: (init) => {
        calls.push((init.method ?? 'GET').toUpperCase())
        return new Response(JSON.stringify({ status: 'resent', invitation_id: 'inv-1', email: 'approved@example.test', expires_at: '2026-06-28T00:00:00Z' }), { status: 200, headers: { 'content-type': 'application/json' } })
      },
    })
    const button = await screen.findByRole('button', { name: 'Resend invite' })
    fireEvent.click(button)
    // It hit the dedicated resend endpoint (not approve).
    await waitFor(() => expect(calls).toEqual(['POST']))
    // A clear success message carrying the new expiry is shown.
    expect(await screen.findByText(/Invitation re-sent to approved@example\.test\. New link expires/)).toBeInTheDocument()
  })

  it('surfaces a truthful error when the server refuses (already accepted)', async () => {
    mountAdmin({
      resend: () => new Response(JSON.stringify({ detail: 'invitation_already_accepted' }), { status: 400, headers: { 'content-type': 'application/json' } }),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Resend invite' }))
    const banner = await screen.findByText('invitation_already_accepted')
    expect(banner).toBeInTheDocument()
  })
})
