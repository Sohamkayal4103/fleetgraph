import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'

// Dependency-free History-API path router. The portal is mounted under the canonical origin
// https://www.aithernet.online with these owned paths: /login, /logout, /status,
// /accept-invite, /app and /app/* (and, on the admin origin only, /admin/*). Assets load from
// the fixed /app/assets/ base (vite `base`), so the SPA shell can be served at any of those
// paths without a <base href> that would rewrite form actions.
//
// `path` is window.location.pathname (query EXCLUDED); `query` parses window.location.search so
// pages can read URL parameters (e.g. a one-time invitation token or a validated return_to).

interface RouterContextValue {
  path: string
  query: URLSearchParams
  navigate: (to: string) => void
  /** Replace the current entry WITHOUT adding history (used to scrub a token from the URL). */
  replace: (to: string) => void
}

const RouterContext = createContext<RouterContextValue>({
  path: '/',
  query: new URLSearchParams(),
  navigate: () => {},
  replace: () => {},
})

// ---- Legacy hash → path compatibility ------------------------------------------------------
// beta.4 shipped a hash-routed SPA and sent invitation links like
// https://app.aithernet.online/#/accept-invite?token=…. Old bookmarks / emails must keep working
// wherever the SPA is served (the legacy app host during the compat window, or www after cutover).
// Translate a leading `#/…` route to its new path equivalent exactly once, then history-replace so
// the hash is gone and normal path routing takes over.
export function hashToPath(hash: string): { path: string; search: string } | null {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  if (!raw.startsWith('/')) return null
  const qIndex = raw.indexOf('?')
  let p = qIndex >= 0 ? raw.slice(0, qIndex) : raw
  const search = qIndex >= 0 ? raw.slice(qIndex) : ''
  if (p === '/portal') p = '/app'
  else if (p.startsWith('/portal/')) p = '/app/' + p.slice('/portal/'.length)
  else if (p === '/signin' || p === '/sign-in') p = '/login'
  else if (p === '/accept-invite') p = '/app/accept-invite'
  // /admin*, /status stay as-is; anything else falls through unchanged.
  return { path: p, search }
}

function currentLocation(): { path: string; query: URLSearchParams } {
  if (typeof window === 'undefined') return { path: '/', query: new URLSearchParams() }
  const path = window.location.pathname || '/'
  return { path, query: new URLSearchParams(window.location.search) }
}

export function RouterProvider({ children }: { children: ReactNode }) {
  // Run the hash-compat shim synchronously on first render so no component ever observes a
  // legacy hash route. history.replaceState keeps this out of the Back/Forward stack.
  if (typeof window !== 'undefined' && window.location.hash.startsWith('#/')) {
    const mapped = hashToPath(window.location.hash)
    if (mapped) {
      window.history.replaceState(null, '', `${mapped.path}${mapped.search}`)
    }
  }

  const [state, setState] = useState(currentLocation)

  useEffect(() => {
    const onPop = () => setState(currentLocation())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const navigate = useCallback((to: string) => {
    window.history.pushState(null, '', to)
    setState(currentLocation())
    window.scrollTo(0, 0)
  }, [])

  const replace = useCallback((to: string) => {
    window.history.replaceState(null, '', to)
    setState(currentLocation())
  }, [])

  return (
    <RouterContext.Provider value={{ path: state.path, query: state.query, navigate, replace }}>
      {children}
    </RouterContext.Provider>
  )
}

export function useRouter() {
  return useContext(RouterContext)
}

/** Intercept plain left-clicks so in-app navigation uses the History API (real hrefs otherwise —
 *  middle-click / ctrl-click / new-tab all work, and the links are crawlable/same-origin). */
export function Link({
  to,
  children,
  className,
}: {
  to: string
  children: ReactNode
  className?: string
}) {
  const { navigate, path } = useRouter()
  const active = path === to
  return (
    <a
      href={to}
      className={className}
      aria-current={active ? 'page' : undefined}
      onClick={(e) => {
        if (e.defaultPrevented) return
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
        e.preventDefault()
        navigate(to)
      }}
    >
      {children}
    </a>
  )
}
