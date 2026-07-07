import { useState } from 'react'
import type { ReactNode } from 'react'

export function Section({
  title,
  actions,
  children,
}: {
  title: string
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="card">
      <div className="card-head">
        <h2>{title}</h2>
        {actions ? <div className="card-actions">{actions}</div> : null}
      </div>
      <div className="card-body">{children}</div>
    </section>
  )
}

/** A small summary card (count + label + optional link), used on overview dashboards. */
export function StatCard({
  label,
  value,
  hint,
  to,
}: {
  label: string
  value: ReactNode
  hint?: string
  to?: string
}) {
  const body = (
    <>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
      {hint ? <span className="stat-hint">{hint}</span> : null}
    </>
  )
  return to ? (
    <a className="stat" href={`#${to}`}>
      {body}
    </a>
  ) : (
    <div className="stat">{body}</div>
  )
}

export function Badge({ kind, children }: { kind?: string; children: ReactNode }) {
  return <span className={`badge badge-${(kind ?? 'neutral').toLowerCase()}`}>{children}</span>
}

export function StatusBanner({
  kind,
  children,
}: {
  kind: 'info' | 'error' | 'success'
  children: ReactNode
}) {
  return (
    <div className={`banner banner-${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
      {children}
    </div>
  )
}

export function Loading({ what }: { what: string }) {
  return (
    <p className="state state-loading" role="status" aria-live="polite">
      Loading {what}…
    </p>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="state state-empty" role="status">
      {children}
    </p>
  )
}

export function ErrorState({ message }: { message: string }) {
  return (
    <p className="state state-error" role="alert">
      {message}
    </p>
  )
}

/**
 * Renders one of the canonical data states: error, loading (data null), empty, or ready.
 */
export function DataView<T>({
  data,
  error,
  what,
  empty,
  isEmpty,
  children,
}: {
  data: T | null
  error: string | null
  what: string
  empty?: ReactNode
  isEmpty?: (d: T) => boolean
  children: (d: T) => ReactNode
}) {
  if (error) return <ErrorState message={error} />
  if (data === null) return <Loading what={what} />
  if (isEmpty && isEmpty(data)) return <Empty>{empty ?? `No ${what} yet.`}</Empty>
  return <>{children(data)}</>
}

export function CopyButton({ value, label = 'Copy' }: { value: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      className="btn-secondary"
      onClick={() => {
        void navigator.clipboard
          ?.writeText(value)
          .then(() => {
            setDone(true)
            setTimeout(() => setDone(false), 1500)
          })
          .catch(() => undefined)
      }}
    >
      {done ? 'Copied' : label}
    </button>
  )
}

/** A destructive action button that requires an inline confirmation click. */
export function ConfirmButton({
  onConfirm,
  children,
  confirmLabel = 'Confirm',
}: {
  onConfirm: () => void
  children: ReactNode
  confirmLabel?: string
}) {
  const [armed, setArmed] = useState(false)
  if (armed) {
    return (
      <span className="confirm">
        <button
          type="button"
          className="btn-danger"
          onClick={() => {
            setArmed(false)
            onConfirm()
          }}
        >
          {confirmLabel}
        </button>{' '}
        <button type="button" className="btn-secondary" onClick={() => setArmed(false)}>
          Cancel
        </button>
      </span>
    )
  }
  return (
    <button type="button" className="btn-danger" onClick={() => setArmed(true)}>
      {children}
    </button>
  )
}

export function Field({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
    </div>
  )
}

/** A clearly-marked placeholder for BRANDING ONLY (never product workflows). */
export function Placeholder({ children }: { children: ReactNode }) {
  return <p className="placeholder">{children}</p>
}

export function LegalReference({ label, url }: { label: string; url?: string }) {
  return (
    <li>
      {url ? (
        <a href={url} target="_blank" rel="noreferrer noopener">
          {label}
        </a>
      ) : (
        <span>{label} — operator-supplied document (not configured)</span>
      )}
    </li>
  )
}
