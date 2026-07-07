import type { ReactNode } from 'react'
import type { Session } from '../api/types'

/**
 * Route/render guard for platform-admin-only UI.
 *
 * The server always enforces authorization, but the UI must ALSO refuse to render admin
 * controls for non-admins. This component returns its `fallback` (default: nothing) unless the
 * session's user is authenticated AND `is_platform_admin` is true.
 */
export function isPlatformAdmin(session: Session | null | undefined): boolean {
  return Boolean(session?.authenticated && session?.user?.is_platform_admin === true)
}

export function AdminOnly({
  session,
  children,
  fallback = null,
}: {
  session: Session | null | undefined
  children: ReactNode
  fallback?: ReactNode
}) {
  if (!isPlatformAdmin(session)) return <>{fallback}</>
  return <>{children}</>
}
