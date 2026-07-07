import type { ReactNode } from 'react'

export type Tone = 'ok' | 'warn' | 'bad' | 'muted' | 'info'

export function StatusCard({
  title,
  badge,
  actions,
  children,
}: {
  title: string
  badge?: ReactNode
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="card">
      <header className="card-header">
        <h2>{title}</h2>
        <div className="card-header-right">
          {badge}
          {actions}
        </div>
      </header>
      <div className="card-body">{children}</div>
    </section>
  )
}

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}

export function ReadyBadge({ configured }: { configured: boolean }) {
  return <Badge tone={configured ? 'ok' : 'warn'}>{configured ? 'configured' : 'unconfigured'}</Badge>
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null
  return <p className="error-note" role="alert">{error}</p>
}

export function Loading({ loading }: { loading: boolean }) {
  if (!loading) return null
  return <p className="muted">Loading…</p>
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted">{children}</p>
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="field-row">
      <span className="field-label">{label}</span>
      <span className="field-value">{children}</span>
    </div>
  )
}

export function Chips({ items, tone = 'muted' }: { items: string[]; tone?: Tone }) {
  if (!items.length) return <span className="muted">none</span>
  return (
    <span className="chips">
      {items.map((item) => (
        <span key={item} className={`chip chip-${tone}`}>
          {item}
        </span>
      ))}
    </span>
  )
}

export function JsonBlock({ value }: { value: unknown }) {
  if (value === null || value === undefined) return null
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  if (text === '{}' || text === '') return null
  return (
    <div className="json-scroll">
      <pre className="json-block">{text}</pre>
    </div>
  )
}

//: Default cap on rendered JSON characters. The full result is NOT truncated on the
//: backend / persisted record — this only bounds what the browser draws so a huge MCP
//: result (e.g. the full block catalog) cannot lock or destroy the page layout.
const JSON_PREVIEW_LIMIT = 20_000

export function JsonPreview({
  value,
  limit = JSON_PREVIEW_LIMIT,
}: {
  value: unknown
  limit?: number
}) {
  if (value === null || value === undefined) return null
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  if (text === '{}' || text === '') return null
  const truncated = text.length > limit
  const shown = truncated ? text.slice(0, limit) : text
  return (
    <div className="json-scroll">
      <pre className="json-block">{shown}</pre>
      {truncated && (
        <p className="muted small-note">
          Showing the first {limit.toLocaleString()} of {text.length.toLocaleString()}{' '}
          characters. The full result is persisted on the node (GET /mcp/tool-calls).
        </p>
      )}
    </div>
  )
}

const STATUS_TONES: Record<string, Tone> = {
  completed: 'ok',
  connected: 'ok',
  active: 'info',
  running: 'info',
  created: 'muted',
  received: 'muted',
  disconnected: 'muted',
  blocked: 'warn',
  failed: 'bad',
  disabled: 'bad',
  cancelled: 'muted',
}

export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONES[status] ?? 'muted'}>{status}</Badge>
}

export function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id
}

export function formatTime(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString()
}
