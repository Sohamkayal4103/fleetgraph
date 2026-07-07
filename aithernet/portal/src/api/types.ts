// Shared API types for the Aithernet hosted portal. These mirror the control-plane contract.
// None of these carry secrets: no node private keys, signing keys, OAuth tokens, env values,
// raw mission prompts, hidden reasoning, or raw database rows are ever modelled here.

export interface SessionUser {
  user_id: string
  email: string
  is_platform_admin: boolean
}

export interface Session {
  authenticated: boolean
  user: SessionUser | null
  csrf_token: string | null
}

export interface LoginResponse {
  user_id: string
  email: string
  csrf_token: string
  is_platform_admin?: boolean
}

export interface PolicyVersion {
  policy_id: string
  policy_type: string
  version: string
  title: string
  document_digest: string
  required_categories: string[]
  optional_categories: string[]
  effective_date: string | null
}

export interface PolicyStatus {
  policy_id: string
  version: string
  accepted: boolean
  accepted_at: string | null
}

export interface TenantMembership {
  tenant_id: string
  tenant_name: string
  role: string
}

// Node readiness/state is a bounded, non-sensitive summary. We never surface raw telemetry,
// private keys, or environment values.
export type NodeState =
  | 'online'
  | 'recent'
  | 'stale'
  | 'offline'
  | 'revoked'
  | 'degraded'
  | 'never_seen'
  | 'unknown'

export interface FleetNode {
  hosted_node_id: string
  tenant_id: string
  node_id: string
  display_name: string
  fingerprint: string
  software_version: string | null
  release_channel: string
  status: string
  state: NodeState
  last_heartbeat_at: string | null
  hardware_families: string[]
  device_count: number
  data_export_state: string | null
  mesh_memberships?: string[]
  enrolled_at: string
}

export interface FleetSummary {
  total: number
  by_state: Record<string, number>
}

// beta.3 mesh model (hosted, tenant-scoped).
export interface Mesh {
  mesh_id: string
  tenant_id: string
  display_name: string
  policy_version: number
  revoked: boolean
  member_count: number
  created_at: string
}

export interface MeshMember {
  hosted_node_id: string
  node_id: string
  role: string
  revoked: boolean
}

export interface MeshDetail extends Mesh {
  members: MeshMember[]
}

export interface EnrollmentCodeSummary {
  enrollment_code_id: string
  tenant_id: string
  status: string
  used_count: number
  maximum_uses: number
  node_name_constraint: string | null
  created_at: string
  expires_at: string | null
}

export interface ReleaseArtifact {
  name: string
  kind: string
  architecture: string
  os_family: string
  sha256: string
  byte_size: number
  content_type: string
}

export interface Release {
  release_id: string
  version: string
  channel: string
  status: 'draft' | 'candidate' | 'published' | 'paused' | 'revoked' | 'superseded'
  manifest_digest: string | null
  signing_key_id: string | null
  published_at: string | null
  artifacts: ReleaseArtifact[]
}

export interface SupportCase {
  case_id: string
  tenant_id: string
  subject: string
  category: string
  status: string
  opener_user_id: string
  created_at: string
}

export interface SupportMessage {
  author_role: string
  body: string
  created_at: string
}

export interface SupportCaseDetail extends SupportCase {
  messages: SupportMessage[]
}

export interface AuditRecord {
  id: string
  action: string
  actor_user_id: string | null
  actor_kind: string
  tenant_id: string | null
  target: string | null
  metadata: Record<string, unknown>
  created_at: string
}

export interface EnrollmentCode {
  enrollment_code_id: string
  code: string
  tenant_id: string
  expires_at: string
}

export interface Tenant {
  tenant_id: string
  name: string
  kind: string
  status: string
  created_at: string
}

export interface AdminUser {
  user_id: string
  email: string
  display_name: string
  is_platform_admin: boolean
  email_verified: boolean
  disabled: boolean
  created_at: string
  memberships: { tenant_id: string; role: string }[]
}

export interface Invitation {
  invitation_id: string
  tenant_id: string | null
  email: string
  role: string
  status: string
  used_count: number
  maximum_uses: number
  expires_at: string
  created_at: string
  token?: string
}

export interface InvitationValidation {
  valid: boolean
  status: 'valid' | 'expired' | 'revoked' | 'consumed' | 'invalid'
  email?: string
  tenant_id?: string | null
  proposed_tenant_name?: string | null
  role?: string
  expires_at?: string
  required_policies?: { policy_id: string; policy_type: string; version: string; title: string }[]
  /** True when the invited email already has an account: accept requires the existing password. */
  account_exists?: boolean
}

export interface EmailPreviewMessage {
  id: string
  to: string
  subject: string
  kind: string
  created_at: string
  link?: string
}

export interface EmailPreview {
  enabled: boolean
  messages: EmailPreviewMessage[]
}

export interface DriveStatus {
  configured: boolean
  enabled: boolean
  credential_present: boolean
  last_successful_archive_at: string | null
  failure_category: string | null
  pending_archive_count: number
  note: string | null
}

// Research owner-archive types (beta.9). None carry secrets: refresh tokens, OAuth credentials
// and record contents are NEVER modelled here — only bounded status/metadata.
export interface OwnerDriveStatus {
  // connected | disconnected | error | unconfigured
  status: string
  connected: boolean
  // Whether the server has Google OAuth configured (client id/secret/redirect). When false, the
  // admin sees a "not configured" message instead of a Connect button.
  oauth_configured: boolean
  scope: string | null
  account_label: string | null
  account_email: string | null
  root_folder_id: string | null
  root_folder_name: string | null
  connected_via: string | null // oauth | manual
  error_category: string | null // e.g. reconnect_required
  last_sync_at: string | null
  queued_jobs: number
  synced_jobs: number
  failed_jobs: number
}

// The admin OAuth start returns ONLY a Google authorization URL for the browser to navigate to.
// No secret is ever returned.
export interface StartOwnerDriveOAuth {
  authorize_url: string
  tenant_id: string
}

export interface ResearchAdminStatus {
  owner_drive: OwnerDriveStatus
  packages_accepted: number
  quarantined: number
  archive_jobs_queued: number
  archive_jobs_synced: number
  archive_jobs_failed: number
  capabilities_active: number
}

export interface ResearchPackage {
  package_id: string
  tenant_id: string
  node_id: string
  package_sha256: string
  record_count: number
  byte_size: number
  status: string
  release_version: string | null
  received_at: string | null
}

export interface ResearchQuarantineEntry {
  id: string
  tenant_id: string
  node_id: string
  reason_category: string
  detail: string
  created_at: string | null
}

export interface ResearchJob {
  job_id: string
  package_id: string
  tenant_id: string
  node_id: string
  status: string
  attempts: number
  drive_file_id: string | null
  last_error_type: string | null
}

export interface ProcessJobsResult {
  synced: number
  failed: number
  skipped: number
}

export interface ConnectOwnerDriveInput {
  refresh_token: string
  account_label?: string
  root_folder_id?: string
}

// Customer-facing per-node research summary (consent + owner-archive upload state). No Drive
// credentials, admin tokens or record contents are ever exposed here.
export interface ResearchNodeSummary {
  node_id: string
  consent: boolean
  owner_archive_upload: boolean
  packages_received: number
  last_upload_at: string | null
}

export interface SessionRecord {
  session_id: string
  revoked: boolean
  user_agent: string | null
  expires_at: string
  created_at: string
}

export interface Readiness {
  status: string
  schema_ok: boolean
  pending_migrations: string[]
  config_problems: string[]
  environment: string
}

export interface Diagnostics {
  version: string
  environment: string
  database_dialect: string
  email_provider: string
  storage_backend: string
  bootstrapped: boolean
  counts: Record<string, number>
}

export interface ApiError {
  status: number
  message: string
}
