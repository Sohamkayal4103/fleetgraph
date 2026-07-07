// beta.9 (servicing) — the admin "Research Archive" section renders the owner-Drive OAuth
// connection (Connect Google Drive with NO refresh-token paste in the normal path), the archive
// status counts, and the packages/quarantine/jobs tables. When Google OAuth is not configured on
// the server it shows a clear message instead of a Connect button. Non-admins are refused.
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AdminPortal } from '../pages/AdminPortal'
import { RouterProvider } from '../routes/router'
import { SessionProvider } from '../session'

afterEach(() => {
  vi.unstubAllGlobals()
  window.location.hash = ''
  window.history.replaceState(null, '', '/')
})

const CONNECTED_DRIVE = {
  status: 'connected',
  connected: true,
  oauth_configured: true,
  scope: 'https://www.googleapis.com/auth/drive.file',
  account_label: 'Owner Research Drive',
  account_email: 'owner@aithernet.online',
  root_folder_id: 'folder-123',
  root_folder_name: 'Aithernet Research Archive',
  connected_via: 'oauth',
  error_category: null,
  last_sync_at: '2026-07-04T12:00:00Z',
  queued_jobs: 1,
  synced_jobs: 5,
  failed_jobs: 0,
}
const STATUS = {
  owner_drive: CONNECTED_DRIVE,
  packages_accepted: 7,
  quarantined: 2,
  archive_jobs_queued: 1,
  archive_jobs_synced: 5,
  archive_jobs_failed: 0,
  capabilities_active: 3,
}
const PACKAGES = [{
  package_id: 'pkg-1', tenant_id: 't1', node_id: 'node-alpha',
  package_sha256: 'sha256:abcdef0123456789', record_count: 12, byte_size: 4096,
  status: 'accepted', release_version: '1.0.0-beta.9', received_at: '2026-07-04T11:00:00Z',
}]
const QUARANTINE = [{
  id: 'q-1', tenant_id: 't1', node_id: 'node-alpha',
  reason_category: 'secret_detected', detail: 'redacted api key in tool output',
  created_at: '2026-07-04T10:00:00Z',
}]
const JOBS = [{
  job_id: 'job-1', package_id: 'pkg-1', tenant_id: 't1', node_id: 'node-alpha',
  status: 'synced', attempts: 1, drive_file_id: 'file-9', last_error_type: null,
}]

interface Opts { isAdmin?: boolean; drive?: Record<string, unknown>; onStart?: () => void }

function mountAdmin({ isAdmin = true, drive = CONNECTED_DRIVE, onStart }: Opts = {}) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.toString(), 'http://admin.test')
    const p = url.pathname
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
    if (/\/v1\/auth\/session/.test(p)) {
      return json({ user_id: 'a', email: 'admin@x', is_platform_admin: isAdmin, authenticated: true, csrf_token: 'c' })
    }
    if (/\/v1\/admin\/research\/drive\/oauth\/start$/.test(p)) {
      onStart?.()
      return json({ authorize_url: 'https://accounts.google.com/o/oauth2/v2/auth?client_id=x', tenant_id: '*' })
    }
    if (/\/v1\/admin\/research\/status$/.test(p)) return json({ ...STATUS, owner_drive: drive })
    if (/\/v1\/admin\/research\/drive$/.test(p)) return json(drive)
    if (/\/v1\/admin\/research\/packages$/.test(p)) return json(PACKAGES)
    if (/\/v1\/admin\/research\/quarantine$/.test(p)) return json(QUARANTINE)
    if (/\/v1\/admin\/research\/jobs/.test(p)) return json(JOBS)
    return json({})
  }))
  window.history.replaceState(null, '', '/admin/research')
  return render(
    <RouterProvider>
      <SessionProvider>
        <AdminPortal />
      </SessionProvider>
    </RouterProvider>,
  )
}

describe('beta.9 Research Archive admin — owner Drive OAuth', () => {
  it('renders connected owner-Drive status, archive counts, and packages/quarantine tables', async () => {
    mountAdmin()
    expect(await screen.findByText('Owner Drive connection')).toBeInTheDocument()
    expect(await screen.findByText('connected')).toBeInTheDocument()
    // Account email + root folder NAME are shown (server-side metadata, never a token).
    expect(await screen.findByText('owner@aithernet.online')).toBeInTheDocument()
    expect(await screen.findByText(/Aithernet Research Archive/)).toBeInTheDocument()
    // Archive status counts.
    expect(await screen.findByText('Packages accepted')).toBeInTheDocument()
    expect(await screen.findByText('Active capabilities')).toBeInTheDocument()
    // Received-packages + quarantine tables.
    expect((await screen.findAllByText('node-alpha')).length).toBeGreaterThan(0)
    expect(await screen.findByText('1.0.0-beta.9')).toBeInTheDocument()
    expect(await screen.findByText('secret_detected')).toBeInTheDocument()
    // Manual process button is present (retry/testing only).
    expect(screen.getByRole('button', { name: 'Process archive jobs' })).toBeInTheDocument()
    // No refresh token is ever displayed back.
    expect(document.body.textContent ?? '').not.toContain('refresh-token')
  })

  it('offers "Connect Google Drive" (no refresh-token paste) when disconnected, and starts OAuth', async () => {
    const onStart = vi.fn()
    mountAdmin({
      drive: { ...CONNECTED_DRIVE, status: 'disconnected', connected: false, account_email: null,
        connected_via: null, last_sync_at: null, queued_jobs: 0, synced_jobs: 0 },
      onStart,
    })
    const btn = await screen.findByRole('button', { name: 'Connect Google Drive' })
    expect(btn).toBeInTheDocument()
    // The refresh-token form is tucked behind an "Advanced" disclosure, not the primary path.
    expect(screen.getByText(/Advanced: connect with a refresh token/)).toBeInTheDocument()
    // Clicking "Connect Google Drive" starts the server-side OAuth flow (no token pasted).
    fireEvent.click(btn)
    await waitFor(() => expect(onStart).toHaveBeenCalled())
  })

  it('shows a clear "not configured" message when server Google OAuth is absent', async () => {
    mountAdmin({
      drive: { ...CONNECTED_DRIVE, status: 'unconfigured', connected: false, oauth_configured: false,
        account_email: null, connected_via: null, last_sync_at: null,
        queued_jobs: 1, synced_jobs: 0 },
    })
    expect(await screen.findByText(/Google Drive OAuth is not configured on the server/)).toBeInTheDocument()
    expect(screen.getByText(/GOOGLE_OAUTH_CLIENT_ID/)).toBeInTheDocument()
    // No Connect button when the server cannot offer OAuth.
    expect(screen.queryByRole('button', { name: 'Connect Google Drive' })).not.toBeInTheDocument()
  })

  it('refuses to render for a non-admin', async () => {
    mountAdmin({ isAdmin: false })
    await waitFor(() => expect(screen.getByText(/Not authorized/)).toBeInTheDocument())
    expect(screen.queryByText('Owner Drive connection')).not.toBeInTheDocument()
  })
})
