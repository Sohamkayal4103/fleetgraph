import { useEffect, useState } from 'react'
import { useSession } from './session'
import { useRouter, Link } from './routes/router'
import { PublicSite } from './pages/PublicSite'
import { CustomerPortal } from './pages/CustomerPortal'
import { AdminPortal } from './pages/AdminPortal'
import { SignIn, AcceptInvite } from './pages/Auth'
import { AdminOnly } from './components/AdminOnly'
import { ErrorBoundary } from './components/ErrorBoundary'

// Everything is same-origin under https://www.aithernet.online now. Public marketing pages are
// served by the static site at these SAME-ORIGIN relative paths (the edge routes them to the
// marketing origin); the portal owns /login, /logout, /status, /app and /app/*. No customer-facing
// link points at another hostname — no www./app. transition during navigation.
const SITE_NAV: { label: string; to?: string; href?: string }[] = [
  { label: 'Product', href: '/product' },
  { label: 'Documentation', href: '/docs' },
  { label: 'Status', to: '/status' },
]

// Fixed "go to top" button, mirroring the public site: hidden until the page is scrolled past a
// threshold, then a solid periwinkle circle in the bottom-right that scrolls smoothly to the top.
// Reduced-motion aware; accessible label. Styling lives in .to-top (styles.css).
function ToTop() {
  const [show, setShow] = useState(false)
  useEffect(() => {
    const onScroll = () => {
      const y = window.pageYOffset || document.documentElement.scrollTop || 0
      const next = y > 400
      setShow((prev) => (prev === next ? prev : next))
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])
  function toTop() {
    const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' })
  }
  return (
    <button
      type="button"
      className={show ? 'to-top show' : 'to-top'}
      aria-label="Go to top"
      title="Go to top"
      onClick={toTop}
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M12 19V6" />
        <path d="M6 12l6-6 6 6" />
      </svg>
    </button>
  )
}

export function App() {
  const { session, loading, signOut } = useSession()
  const { path, navigate } = useRouter()

  // /logout is a real path: revoke the session, then land on /login. (The API logout also clears
  // the host-only cookies server-side.)
  useEffect(() => {
    if (path !== '/logout') return
    let cancelled = false
    void (async () => {
      await signOut()
      if (!cancelled) navigate('/login')
    })()
    return () => {
      cancelled = true
    }
  }, [path, signOut, navigate])

  async function onSignOut() {
    await signOut()
    navigate('/login')
  }

  function renderRoute() {
    if (path === '/app/accept-invite' || path === '/accept-invite') return <AcceptInvite />
    if (path === '/app' || path.startsWith('/app/')) return <CustomerPortal />
    if (path === '/admin' || path.startsWith('/admin/')) return <AdminPortal />
    if (path === '/login') return <SignIn />
    if (path === '/logout') return <p className="hint">Signing you out…</p>
    if (path === '/status') return <PublicSite />
    // The canonical marketing site owns "/", "/product", etc. If the SPA is ever reached at a
    // root marketing path (e.g. legacy app-host landing), show the slim in-app landing.
    return <PublicSite />
  }

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <header className="app-header" role="banner">
        {/* One unified shell: this bar mirrors the public site header (same brand, container,
            height, typography and buttons) so moving between marketing pages and the portal never
            changes layout. */}
        <div className="app-bar">
          <Link className="brand" to="/app">
            <strong>Aithernet</strong><span className="dot">.</span>
            <span className="brand-tag">Portal</span>
          </Link>
          <nav aria-label="Primary" className="primary-nav">
            {SITE_NAV.map((n) =>
              n.href ? (
                <a key={n.label} href={n.href}>{n.label}</a>
              ) : (
                <Link key={n.label} to={n.to!}>{n.label}</Link>
              ),
            )}
          </nav>
          <nav aria-label="Account" className="account-nav">
            {session?.authenticated ? (
              <>
                <span className="who">{session.user?.email}</span>
                <AdminOnly session={session}>
                  {/* Admin lives on its own isolated origin (Cloudflare Access). Only admins see it. */}
                  <a href="https://admin.aithernet.online/admin" rel="noopener noreferrer">Admin</a>
                </AdminOnly>
                <button type="button" className="signout" onClick={onSignOut}>
                  Sign out
                </button>
              </>
            ) : (
              <>
                <a href="/">Home</a>
                <Link className="btn primary" to="/login">Sign in</Link>
              </>
            )}
          </nav>
        </div>
      </header>

      <main id="main" role="main">
        <ErrorBoundary>
          {loading && (path === '/app' || path.startsWith('/app/')) ? null : renderRoute()}
        </ErrorBoundary>
      </main>

      <footer className="app-footer" role="contentinfo">
        <div className="app-foot-inner">
          <span className="foot-brand">Aithernet<span className="dot">.</span></span>
          <span>© 2026 Aithernet</span>
          <a href="/product">Product</a>
          <a href="/docs">Documentation</a>
          <a href="/privacy">Privacy &amp; legal</a>
          <Link to="/app/support">Support</Link>
          <Link to="/status">Status</Link>
        </div>
      </footer>

      <ToTop />
    </div>
  )
}
