// Shared, accessible building blocks for the Stage 13C communication views.
//
// Every status indicator pairs a textual label with its colour (never colour alone — Part Q),
// the ACK-vs-semantic-reply and trust-vs-authorization distinctions are explicit, and all
// remote/structured content is rendered as escaped TEXT (React escapes by default, so peer
// content can never inject HTML/script — Part R.10/11).

import { useState } from 'react'
import { Badge, type Tone } from './StatusCard'

/** A truncated, copyable id. The full (safe) id is in the title and copied on click (Part Q). */
export function IdTag({ id, label }: { id: string | null | undefined; label?: string }) {
  const [copied, setCopied] = useState(false)
  if (!id) return <span className="muted">—</span>
  const short = id.length > 10 ? `${id.slice(0, 8)}…` : id
  const copy = () => {
    void navigator.clipboard?.writeText(id).then(
      () => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1200)
      },
      () => undefined,
    )
  }
  return (
    <span className="id-tag">
      {label && <span className="id-tag-label">{label}</span>}
      <code className="mono" title={id}>{short}</code>
      <button
        type="button"
        className="copy-btn"
        onClick={copy}
        aria-label={`Copy ${label ?? 'id'} ${id}`}
        title="Copy full id"
      >
        {copied ? '✓' : '⧉'}
      </button>
    </span>
  )
}

const DELIVERY_TONES: Record<string, Tone> = {
  pending: 'muted',
  in_flight: 'info',
  delivered: 'info',
  acknowledged: 'ok',
  received: 'ok',
  failed: 'bad',
  dead_letter: 'bad',
}

/** Delivery state of a transport message (distinct from "answered" — Part R.14). */
export function DeliveryBadge({ status }: { status?: string }) {
  if (!status) return null
  return <Badge tone={DELIVERY_TONES[status] ?? 'muted'}>delivery: {status}</Badge>
}

/** Transport ACK — "the peer's NODE accepted delivery", explicitly NOT a semantic answer. */
export function AckBadge({ acknowledged }: { acknowledged?: boolean }) {
  return acknowledged ? (
    <Badge tone="ok">ACK ✓ (delivered, not answered)</Badge>
  ) : (
    <Badge tone="muted">no ACK</Badge>
  )
}

/** Semantic reply — an authenticated, correlated answer that satisfied a wait. */
export function ReplyBadge({ semantic }: { semantic?: boolean }) {
  if (!semantic) return null
  return <Badge tone="ok">semantic reply ✓</Badge>
}

/** Public-key TRUST (identity). Shown separately from application authorization. */
export function TrustBadge({ state }: { state: string }) {
  const tone: Tone = state === 'trusted' ? 'ok' : state === 'revoked' ? 'bad' : 'warn'
  return <Badge tone={tone}>trust: {state}</Badge>
}

/** Application AUTHORIZATION (permission) — deliberately distinct from key trust (Part R.15). */
export function AuthBadge({ label, granted }: { label: string; granted: boolean }) {
  return (
    <Badge tone={granted ? 'info' : 'muted'}>
      {label}: {granted ? 'allowed' : 'denied'}
    </Badge>
  )
}

const HEALTH_TONES: Record<string, Tone> = {
  healthy: 'ok',
  failing: 'bad',
  unknown: 'muted',
  disabled: 'warn',
}

/** Transport health derived ONLY from persisted contact records (never fabricated "online"). */
export function HealthBadge({ health }: { health: string }) {
  return <Badge tone={HEALTH_TONES[health] ?? 'muted'}>health: {health}</Badge>
}

const DIRECTION_LABEL: Record<string, string> = {
  outbound: '→ outbound',
  inbound: '← inbound',
  system: '• system',
}

/** A direction + message-type label (text, not colour) for a timeline entry. */
export function KindLabel({ kind, direction }: { kind: string; direction?: string }) {
  const dir = direction ? DIRECTION_LABEL[direction] ?? direction : ''
  return (
    <span className="kind-label">
      {dir && <span className="kind-direction">{dir}</span>}
      <span className="kind-name">{kind.replace(/_/g, ' ')}</span>
    </span>
  )
}

/** Bounded, ESCAPED preview of peer text/structured-data (no HTML/script injection possible). */
export function DataPreview({ text, data }: { text?: string | null; data?: string | null }) {
  if (!text && !data) return null
  return (
    <div className="data-preview">
      {text && <p className="preview-text">{text}</p>}
      {data && <code className="preview-data mono">{data}</code>}
    </div>
  )
}
