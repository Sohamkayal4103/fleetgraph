// Typed control-plane API client for the Aithernet hosted portal.
//
// Security model:
//  - The session is carried by an HttpOnly cookie set by the server on login. We NEVER read
//    or store that cookie, and we NEVER store any auth token in localStorage/sessionStorage.
//  - State-changing requests (POST/PUT/PATCH/DELETE) must carry the CSRF token in the
//    `X-CSRF-Token` header. The token is the only auth-adjacent value we hold, kept in memory
//    and/or read from the readable `aithernet_hosted_csrf` cookie.
//  - All requests use `credentials: 'include'` so the HttpOnly session cookie is sent.

import type {
  AdminUser,
  AuditRecord,
  Diagnostics,
  DriveStatus,
  EmailPreview,
  EnrollmentCode,
  EnrollmentCodeSummary,
  FleetNode,
  FleetSummary,
  Invitation,
  InvitationValidation,
  LoginResponse,
  Mesh,
  MeshDetail,
  OwnerDriveStatus,
  StartOwnerDriveOAuth,
  PolicyStatus,
  PolicyVersion,
  ProcessJobsResult,
  Readiness,
  Release,
  ResearchAdminStatus,
  ResearchJob,
  ResearchNodeSummary,
  ResearchPackage,
  ResearchQuarantineEntry,
  ConnectOwnerDriveInput,
  Session,
  SessionRecord,
  SessionUser,
  SupportCase,
  SupportCaseDetail,
  Tenant,
  TenantMembership,
} from './types'

// Same-origin relative API base by default: the browser calls the control plane THROUGH the
// reverse proxy at `<page-origin>/api/...` (the proxy strips `/api` -> control-plane). This keeps
// the control-plane container internal/unpublished, avoids mixing localhost/127.0.0.1, and keeps
// the HttpOnly + SameSite=lax session cookie and CSRF origin same-origin. Never compile an
// absolute internal host (e.g. http://127.0.0.1:8100) into the frontend. Production may override
// VITE_CONTROL_PLANE_BASE_URL only with a same-origin path or a host the proxy serves /api on.
const DEFAULT_BASE_URL = '/api'

// Build-time public env var. No secrets — only a base URL (a same-origin path by default).
export function baseUrl(): string {
  const fromEnv =
    typeof import.meta !== 'undefined' &&
    (import.meta as { env?: Record<string, string | undefined> }).env
      ? (import.meta as { env?: Record<string, string | undefined> }).env?.VITE_CONTROL_PLANE_BASE_URL
      : undefined
  return (fromEnv && fromEnv.length > 0 ? fromEnv : DEFAULT_BASE_URL).replace(/\/$/, '')
}

const CSRF_COOKIE = 'aithernet_hosted_csrf'

// In-memory CSRF token. Survives within the SPA session but never persisted to storage.
let memoryCsrf: string | null = null

export function setCsrfToken(token: string | null): void {
  memoryCsrf = token
}

/** Read the readable CSRF cookie (NOT the HttpOnly session cookie). */
export function readCsrfCookie(): string | null {
  if (typeof document === 'undefined' || !document.cookie) return null
  for (const part of document.cookie.split(';')) {
    const [k, ...rest] = part.trim().split('=')
    if (k === CSRF_COOKIE) return decodeURIComponent(rest.join('='))
  }
  return null
}

/** Resolve the CSRF token: prefer in-memory (from /session or /login), fall back to cookie. */
export function currentCsrfToken(): string | null {
  return memoryCsrf ?? readCsrfCookie()
}

/** Build a fully-qualified request URL from a path and optional query params. */
export function buildUrl(path: string, query?: Record<string, string | undefined>): string {
  const clean = path.startsWith('/') ? path : `/${path}`
  let url = `${baseUrl()}${clean}`
  if (query) {
    const params = new URLSearchParams()
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') params.set(k, v)
    }
    const qs = params.toString()
    if (qs) url += `?${qs}`
  }
  return url
}

const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

interface RequestOptions {
  method?: string
  query?: Record<string, string | undefined>
  body?: unknown
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = (opts.method ?? 'GET').toUpperCase()
  const url = buildUrl(path, opts.query)

  const headers: Record<string, string> = { Accept: 'application/json' }
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json'

  // CSRF protection on state-changing requests only.
  if (MUTATING.has(method)) {
    const token = currentCsrfToken()
    if (token) headers['X-CSRF-Token'] = token
  }

  const res = await fetch(url, {
    method,
    headers,
    credentials: 'include', // send the HttpOnly session cookie
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  })

  if (!res.ok) {
    let message = res.statusText
    try {
      const data = (await res.json()) as { message?: string; detail?: string }
      message = data.message ?? data.detail ?? message
    } catch {
      // non-JSON error body; keep statusText
    }
    throw { status: res.status, message } as { status: number; message: string }
  }

  if (res.status === 204) return undefined as T
  const text = await res.text()
  return (text ? JSON.parse(text) : undefined) as T
}

// ---- Auth ----------------------------------------------------------------

export async function login(email: string, password: string): Promise<LoginResponse> {
  const data = await request<LoginResponse>('/v1/auth/login', {
    method: 'POST',
    body: { email, password },
  })
  setCsrfToken(data.csrf_token)
  return data
}

export async function logout(): Promise<void> {
  await request<void>('/v1/auth/logout', { method: 'POST' })
  setCsrfToken(null)
}

export async function getSession(): Promise<Session> {
  // The server returns a FLAT principal {user_id,email,is_platform_admin,csrf_token,...} on 200,
  // and 401 when unauthenticated (request() throws — handled by the caller as unauthenticated).
  // Normalize the flat principal into the Session { authenticated, user } shape the SPA reads.
  const data = await request<
    Partial<Session> & { user_id?: string; email?: string; is_platform_admin?: boolean }
  >('/v1/auth/session')
  if (data.csrf_token) setCsrfToken(data.csrf_token)
  const user: SessionUser | null =
    data.user ??
    (data.user_id && data.email
      ? { user_id: data.user_id, email: data.email, is_platform_admin: Boolean(data.is_platform_admin) }
      : null)
  return { authenticated: data.authenticated ?? user !== null, user, csrf_token: data.csrf_token ?? null }
}

/** Public, non-consuming validation of a one-time invitation token (bounded details). */
export function validateInvitation(token: string): Promise<InvitationValidation> {
  return request<InvitationValidation>('/v1/invitations/validate', { query: { token } })
}

export async function acceptInvitation(
  token: string,
  email: string,
  password: string,
  acceptRequiredPolicies = false,
): Promise<LoginResponse> {
  const data = await request<LoginResponse>('/v1/invitations/accept', {
    method: 'POST',
    body: { token, email, password, accept_required_policies: acceptRequiredPolicies },
  })
  if (data.csrf_token) setCsrfToken(data.csrf_token)
  return data
}

// ---- Policies ------------------------------------------------------------

export function getPolicies(): Promise<PolicyVersion[]> {
  return request<PolicyVersion[]>('/v1/policies')
}

export async function getPolicyStatus(): Promise<PolicyStatus[]> {
  // The server returns an OBJECT {active, accepted:[{policy_type,version}], outstanding, ...},
  // not a flat array. Normalize it into the per-active-policy PolicyStatus[] the UI consumes.
  const raw = await request<{
    active?: Array<{ policy_id: string; policy_type: string; version: string }>
    accepted?: Array<{ policy_type: string; version: string }>
  }>('/v1/policies/status')
  const acceptedKeys = new Set((raw.accepted ?? []).map((a) => `${a.policy_type}:${a.version}`))
  return (raw.active ?? []).map((a) => ({
    policy_id: a.policy_id,
    version: a.version,
    accepted: acceptedKeys.has(`${a.policy_type}:${a.version}`),
    accepted_at: null,
  }))
}

export function acceptPolicy(policyId: string, version: string): Promise<void> {
  return request<void>('/v1/policies/accept', {
    method: 'POST',
    body: { policy_id: policyId, version },
  })
}

// ---- Enrollment ----------------------------------------------------------

export function createEnrollmentCode(tenantId: string): Promise<EnrollmentCode> {
  return request<EnrollmentCode>('/v1/enrollment-codes', {
    method: 'POST',
    body: { tenant_id: tenantId },
  })
}

export function getEnrollmentCodes(tenantId: string): Promise<EnrollmentCodeSummary[]> {
  return request<EnrollmentCodeSummary[]>('/v1/enrollment-codes', { query: { tenant_id: tenantId } })
}

export function revokeEnrollmentCode(codeId: string): Promise<void> {
  return request<void>(`/v1/enrollment-codes/${encodeURIComponent(codeId)}/revoke`, {
    method: 'POST',
  })
}

// ---- Meshes --------------------------------------------------------------

export function createMesh(tenantId: string, displayName: string): Promise<Mesh> {
  return request<Mesh>('/v1/meshes', {
    method: 'POST',
    body: { tenant_id: tenantId, display_name: displayName },
  })
}

export function getMeshes(tenantId: string): Promise<Mesh[]> {
  return request<Mesh[]>('/v1/meshes', { query: { tenant_id: tenantId } })
}

export function getMesh(meshId: string): Promise<MeshDetail> {
  return request<MeshDetail>(`/v1/meshes/${encodeURIComponent(meshId)}`)
}

export function addMeshMember(meshId: string, hostedNodeId: string, role = 'member'): Promise<void> {
  return request<void>(`/v1/meshes/${encodeURIComponent(meshId)}/members`, {
    method: 'POST',
    body: { hosted_node_id: hostedNodeId, role },
  })
}

export function revokeMeshMember(meshId: string, hostedNodeId: string): Promise<void> {
  return request<void>(
    `/v1/meshes/${encodeURIComponent(meshId)}/members/${encodeURIComponent(hostedNodeId)}/revoke`,
    { method: 'POST' },
  )
}

export function renameNode(hostedNodeId: string, displayName: string): Promise<void> {
  return request<void>(`/v1/fleet/nodes/${encodeURIComponent(hostedNodeId)}/rename`, {
    method: 'POST',
    body: { display_name: displayName },
  })
}

// ---- Fleet ---------------------------------------------------------------

export function getFleetNodes(tenantId?: string): Promise<FleetNode[]> {
  return request<FleetNode[]>('/v1/fleet/nodes', { query: { tenant_id: tenantId } })
}

export function getFleetSummary(): Promise<FleetSummary> {
  return request<FleetSummary>('/v1/fleet/summary')
}

export function getNode(hostedNodeId: string): Promise<FleetNode> {
  return request<FleetNode>(`/v1/fleet/nodes/${encodeURIComponent(hostedNodeId)}`)
}

export function revokeNode(hostedNodeId: string): Promise<void> {
  return request<void>(`/v1/fleet/nodes/${encodeURIComponent(hostedNodeId)}/revoke`, {
    method: 'POST',
  })
}

export function restoreNode(hostedNodeId: string): Promise<void> {
  return request<void>(`/v1/fleet/nodes/${encodeURIComponent(hostedNodeId)}/restore`, {
    method: 'POST',
  })
}

// ---- Tenants / users -----------------------------------------------------

export function getTenants(): Promise<Tenant[]> {
  return request<Tenant[]>('/v1/tenants')
}

export function getMemberships(): Promise<TenantMembership[]> {
  return request<TenantMembership[]>('/v1/memberships')
}

export function getUsers(): Promise<AdminUser[]> {
  return request<AdminUser[]>('/v1/admin/users')
}

// ---- Invitations ---------------------------------------------------------

export interface CreateInvitationInput {
  email: string
  role?: string
  tenant_id?: string
  proposed_tenant_name?: string
  note?: string
}

export function createInvitation(input: CreateInvitationInput): Promise<Invitation> {
  return request<Invitation>('/v1/invitations', { method: 'POST', body: input })
}

export function getInvitations(tenantId?: string): Promise<Invitation[]> {
  return request<Invitation[]>('/v1/invitations', { query: { tenant_id: tenantId } })
}

export function revokeInvitation(invitationId: string): Promise<void> {
  return request<void>(`/v1/invitations/${encodeURIComponent(invitationId)}/revoke`, {
    method: 'POST',
  })
}

// ---- Early-access requests (admin review queue) --------------------------
export interface EarlyAccessRequest {
  id: string
  email: string
  status: string
  mailing_opt_in: boolean
  invitation_id: string | null
  invitation_status: string | null
  created_at: string | null
  updated_at: string | null
}
export interface EarlyAccessListing {
  requests: EarlyAccessRequest[]
  counts: Record<string, number>
}

export function getEarlyAccessRequests(status?: string): Promise<EarlyAccessListing> {
  return request<EarlyAccessListing>('/v1/admin/early-access', { query: { status } })
}
export function approveEarlyAccess(
  id: string,
  input?: { role?: string; tenant_name?: string },
): Promise<{ status: string; invitation_id: string; email: string }> {
  return request(`/v1/admin/early-access/${encodeURIComponent(id)}/approve`, {
    method: 'POST',
    body: input ?? {},
  })
}
export function rejectEarlyAccess(id: string): Promise<EarlyAccessRequest> {
  return request(`/v1/admin/early-access/${encodeURIComponent(id)}/reject`, { method: 'POST' })
}
export function waitlistEarlyAccess(id: string): Promise<EarlyAccessRequest> {
  return request(`/v1/admin/early-access/${encodeURIComponent(id)}/waitlist`, { method: 'POST' })
}
export function resendEarlyAccessInvitation(
  id: string,
): Promise<{ status: string; invitation_id: string; email: string; expires_at: string }> {
  // Resends the EXISTING invitation (rotated token, fresh expiry) to its recorded email. The
  // token never reaches the browser.
  return request(`/v1/admin/early-access/${encodeURIComponent(id)}/resend`, { method: 'POST' })
}

// ---- Policies (admin) ----------------------------------------------------

export interface PublishPolicyInput {
  policy_type: string
  version: string
  title: string
  document_text: string
  required_categories?: string[]
  optional_categories?: string[]
  supersedes_version?: string
}

export function publishPolicy(input: PublishPolicyInput): Promise<PolicyVersion> {
  return request<PolicyVersion>('/v1/policies', { method: 'POST', body: input })
}

// ---- Releases ------------------------------------------------------------

export function getReleases(channel?: string): Promise<Release[]> {
  return request<Release[]>('/v1/releases', { query: { channel } })
}

export function createRelease(version: string, channel: string, notes: string): Promise<Release> {
  return request<Release>('/v1/releases', { method: 'POST', body: { version, channel, notes } })
}

export function signRelease(releaseId: string): Promise<Release> {
  return request<Release>(`/v1/releases/${encodeURIComponent(releaseId)}/sign`, { method: 'POST' })
}

export function publishRelease(releaseId: string): Promise<Release> {
  return request<Release>(`/v1/releases/${encodeURIComponent(releaseId)}/publish`, {
    method: 'POST',
  })
}

export function revokeRelease(releaseId: string): Promise<Release> {
  return request<Release>(`/v1/releases/${encodeURIComponent(releaseId)}/revoke`, {
    method: 'POST',
  })
}

export function getReleaseManifest(releaseId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(
    `/v1/releases/${encodeURIComponent(releaseId)}/manifest`,
  )
}

/** AUTHENTICATED customer download (session + accepted policy + membership enforced server-side).
 *  Goes through the same-origin /api prefix so the session cookie is sent. */
export function downloadUrl(releaseId: string, name: string): string {
  return `/api/v1/portal/downloads/${encodeURIComponent(releaseId)}/${encodeURIComponent(name)}`
}

/** PUBLIC download — only the verification key (.pub); package artifacts are not served here. */
export function publicDownloadUrl(releaseId: string, name: string): string {
  return `/v1/downloads/${encodeURIComponent(releaseId)}/${encodeURIComponent(name)}`
}

export interface ReleaseBundleMeta {
  filename: string
  sha256: string
  byte_size: number
  files: { name: string; sha256: string }[]
}
/** Metadata for the server-assembled Ubuntu delivery ZIP (filename, SHA-256, contents). */
export function getReleaseBundleMeta(releaseId: string): Promise<ReleaseBundleMeta> {
  return request<ReleaseBundleMeta>(`/v1/portal/releases/${encodeURIComponent(releaseId)}/bundle`)
}
/** AUTHENTICATED single-file ZIP download (session cookie sent via the same-origin /api prefix). */
export function releaseBundleUrl(releaseId: string): string {
  return `/api/v1/portal/releases/${encodeURIComponent(releaseId)}/bundle.zip`
}

// ---- Support -------------------------------------------------------------

export function getSupportCases(tenantId?: string): Promise<SupportCase[]> {
  return request<SupportCase[]>('/v1/support/cases', { query: { tenant_id: tenantId } })
}

export function getSupportCase(caseId: string): Promise<SupportCaseDetail> {
  return request<SupportCaseDetail>(`/v1/support/cases/${encodeURIComponent(caseId)}`)
}

export function createSupportCase(
  tenantId: string,
  subject: string,
  category = 'general',
): Promise<SupportCase> {
  return request<SupportCase>('/v1/support/cases', {
    method: 'POST',
    body: { tenant_id: tenantId, subject, category },
  })
}

export function addSupportMessage(caseId: string, body: string): Promise<void> {
  return request<void>(`/v1/support/cases/${encodeURIComponent(caseId)}/messages`, {
    method: 'POST',
    body: { body },
  })
}

export function resolveSupportCase(caseId: string, status = 'resolved'): Promise<void> {
  return request<void>(`/v1/support/cases/${encodeURIComponent(caseId)}/resolve`, {
    method: 'POST',
    body: { status },
  })
}

// ---- Sessions ------------------------------------------------------------

export function getSessions(): Promise<SessionRecord[]> {
  return request<SessionRecord[]>('/v1/auth/sessions')
}

// ---- Admin / status ------------------------------------------------------

export function getAuditRecords(): Promise<AuditRecord[]> {
  return request<AuditRecord[]>('/v1/admin/audit')
}

export function getEmailPreview(): Promise<EmailPreview> {
  return request<EmailPreview>('/v1/admin/email-preview')
}

export function getDriveStatus(): Promise<DriveStatus> {
  return request<DriveStatus>('/v1/admin/drive-status')
}

// ---- Research owner-archive (admin) --------------------------------------

export function getResearchAdminStatus(): Promise<ResearchAdminStatus> {
  return request<ResearchAdminStatus>('/v1/admin/research/status')
}

export function getResearchPackages(): Promise<ResearchPackage[]> {
  return request<ResearchPackage[]>('/v1/admin/research/packages')
}

export function getResearchQuarantine(): Promise<ResearchQuarantineEntry[]> {
  return request<ResearchQuarantineEntry[]>('/v1/admin/research/quarantine')
}

export function getResearchJobs(status?: string): Promise<ResearchJob[]> {
  return request<ResearchJob[]>('/v1/admin/research/jobs', { query: { status } })
}

export function processResearchJobs(): Promise<ProcessJobsResult> {
  return request<ProcessJobsResult>('/v1/admin/research/process-jobs', { method: 'POST' })
}

export function getOwnerDriveStatus(): Promise<OwnerDriveStatus> {
  return request<OwnerDriveStatus>('/v1/admin/research/drive')
}

/** Start the owner-Drive OAuth flow. Returns ONLY a Google authorization URL to navigate to; the
 *  server mints a state nonce bound to this admin session. No secret or token is returned. */
export function startOwnerDriveOauth(accountLabel?: string): Promise<StartOwnerDriveOAuth> {
  return request<StartOwnerDriveOAuth>('/v1/admin/research/drive/oauth/start', {
    method: 'POST',
    body: { account_label: accountLabel || undefined },
  })
}

/** ADVANCED fallback: connect the OWNER archive Drive with a manually-provisioned refresh token.
 *  The token is sent once and never read back. The normal path is `startOwnerDriveOauth`. */
export function connectOwnerDrive(input: ConnectOwnerDriveInput): Promise<OwnerDriveStatus> {
  return request<OwnerDriveStatus>('/v1/admin/research/drive/connect', { method: 'POST', body: input })
}

export function disconnectOwnerDrive(): Promise<void> {
  return request<void>('/v1/admin/research/drive/disconnect', { method: 'POST' })
}

// ---- Research owner-archive (customer) -----------------------------------

export function getResearchNodeSummary(tenantId: string): Promise<ResearchNodeSummary[]> {
  return request<ResearchNodeSummary[]>('/v1/research/node-summary', { query: { tenant_id: tenantId } })
}

export function getReadiness(): Promise<Readiness> {
  return request<Readiness>('/health/ready')
}

export function getDiagnostics(): Promise<Diagnostics> {
  return request<Diagnostics>('/health/diagnostics')
}

/** Ingestion readiness/diagnostics are served at same-origin /ingest (not under /api). */
export async function getIngestionStatus(): Promise<{
  ready: boolean
  batches: number | null
}> {
  try {
    const [r, d] = await Promise.all([
      fetch('/ingest/v1/readiness', { credentials: 'include' }),
      fetch('/ingest/v1/diagnostics', { credentials: 'include' }),
    ])
    const ready = r.ok && (await r.json()).status === 'ready'
    const batches = d.ok ? ((await d.json()).batches ?? null) : null
    return { ready, batches }
  } catch {
    return { ready: false, batches: null }
  }
}

export const api = {
  baseUrl,
  buildUrl,
  request,
  login,
  logout,
  getSession,
  acceptInvitation,
  validateInvitation,
  getPolicies,
  getPolicyStatus,
  acceptPolicy,
  publishPolicy,
  createEnrollmentCode,
  getEnrollmentCodes,
  revokeEnrollmentCode,
  createMesh,
  getMeshes,
  getMesh,
  addMeshMember,
  revokeMeshMember,
  renameNode,
  getFleetNodes,
  getFleetSummary,
  getNode,
  revokeNode,
  restoreNode,
  getTenants,
  getMemberships,
  getUsers,
  createInvitation,
  getInvitations,
  revokeInvitation,
  getEarlyAccessRequests,
  approveEarlyAccess,
  rejectEarlyAccess,
  waitlistEarlyAccess,
  resendEarlyAccessInvitation,
  getReleaseBundleMeta,
  releaseBundleUrl,
  getReleases,
  createRelease,
  signRelease,
  publishRelease,
  revokeRelease,
  getReleaseManifest,
  downloadUrl,
  publicDownloadUrl,
  getSupportCases,
  getSupportCase,
  createSupportCase,
  addSupportMessage,
  resolveSupportCase,
  getSessions,
  getAuditRecords,
  getEmailPreview,
  getDriveStatus,
  getResearchAdminStatus,
  getResearchPackages,
  getResearchQuarantine,
  getResearchJobs,
  processResearchJobs,
  getOwnerDriveStatus,
  startOwnerDriveOauth,
  connectOwnerDrive,
  disconnectOwnerDrive,
  getResearchNodeSummary,
  getReadiness,
  getDiagnostics,
  getIngestionStatus,
  currentCsrfToken,
  setCsrfToken,
  readCsrfCookie,
}

export type Api = typeof api
