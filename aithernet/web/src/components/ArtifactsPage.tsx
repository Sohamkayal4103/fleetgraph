// Artifacts & transfers operational view (Stage 13D.2, Part I).
//
// Shows local/imported artifacts, remote offers, and transfer progress with direction, peer,
// expected/received bytes, verification state, and store quota. Bounded actions (request, cancel,
// retry, pin/unpin, GC) are confirmed for destructive ones. There is NO arbitrary file selector
// and the browser NEVER contacts a peer's binary endpoint — the node performs every transfer.
// No byte content, absolute path, key, signature, or grant material is ever rendered.

import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, useAutoReload, useResource } from '../api/hooks'
import type { ArtifactTransfer } from '../api/types'
import { ConfirmButton } from './ConfirmButton'
import { Badge, Empty, ErrorNote, Field, Loading, StatusCard } from './StatusCard'
import { IdTag } from './commsUi'

function transferTone(state: string) {
  if (state === 'completed') return 'ok' as const
  if (['failed', 'rejected', 'expired'].includes(state)) return 'bad' as const
  if (['transferring', 'verifying', 'authorized'].includes(state)) return 'info' as const
  return 'muted' as const
}

function bytes(n: number | null | undefined) {
  if (n === null || n === undefined) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export function ArtifactsPage() {
  const artifacts = useResource(() => api.getArtifacts(), [])
  const offers = useResource(() => api.getRemoteArtifactOffers(), [])
  const transfers = useResource(() => api.getArtifactTransfers(), [])
  const store = useResource(() => api.getArtifactStoreStatus(), [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useAutoReload(transfers.reload, 3000, true)

  const reloadAll = () => {
    artifacts.reload(); offers.reload(); transfers.reload(); store.reload()
  }
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      reloadAll()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page-grid">
      <StatusCard
        title="Managed artifact store"
        actions={<button onClick={reloadAll}>Refresh</button>}
      >
        <Loading loading={store.loading && !store.data} />
        <ErrorNote error={store.error ?? error} />
        {store.data && (
          <>
            <Field label="Used / quota">
              {bytes(store.data.used_bytes)} / {bytes(store.data.total_quota_bytes)} (available{' '}
              {bytes(store.data.available_in_quota_bytes)})
            </Field>
            <Field label="Free disk">{bytes(store.data.free_disk_bytes)} (min {bytes(store.data.minimum_free_bytes)})</Field>
            <Field label="Per-artifact max">{bytes(store.data.max_artifact_bytes)}</Field>
            <div className="inline-fields">
              <ConfirmButton
                label="GC (apply)"
                prompt="Garbage-collect unreferenced store objects? Pinned objects are kept."
                disabled={busy}
                onConfirm={() => run(() => api.artifactStoreGc(false))}
              />
            </div>
          </>
        )}
      </StatusCard>

      <StatusCard
        title="Local & imported artifacts"
        badge={<Badge tone="muted">{artifacts.data?.length ?? 0}</Badge>}
      >
        <Loading loading={artifacts.loading && !artifacts.data} />
        <ErrorNote error={artifacts.error} />
        {artifacts.data && artifacts.data.length === 0 && <Empty>No artifacts.</Empty>}
        {artifacts.data && artifacts.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Artifact</th>
                <th scope="col">Kind</th>
                <th scope="col">Availability</th>
                <th scope="col">Size</th>
                <th scope="col">Pin</th>
              </tr>
            </thead>
            <tbody>
              {artifacts.data.map((a) => (
                <tr key={a.artifact_id}>
                  <td><IdTag id={a.artifact_id} /> {a.display_name && <span className="muted small-note">{a.display_name}</span>}</td>
                  <td>{a.artifact_kind}</td>
                  <td><Badge tone={a.availability_state === 'imported' ? 'ok' : 'info'}>{a.availability_state}</Badge></td>
                  <td>{bytes(a.size_bytes)}</td>
                  <td>
                    {a.pinned ? (
                      <button className="small" disabled={busy} onClick={() => run(() => api.unpinArtifact(a.artifact_id))}>Unpin</button>
                    ) : (
                      <button className="small" disabled={busy} onClick={() => run(() => api.pinArtifact(a.artifact_id))}>Pin</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      <StatusCard
        title="Remote offers"
        badge={<Badge tone="muted">{offers.data?.length ?? 0}</Badge>}
      >
        <ErrorNote error={offers.error} />
        {offers.data && offers.data.length === 0 && <Empty>No remote offers.</Empty>}
        {offers.data && offers.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr><th scope="col">From peer</th><th scope="col">Kind</th><th scope="col">Size</th><th scope="col">Action</th></tr>
            </thead>
            <tbody>
              {offers.data.map((o) => (
                <tr key={o.id}>
                  <td><IdTag id={o.peer_id} /></td>
                  <td>{o.artifact_kind ?? '—'}</td>
                  <td>{bytes(o.size_bytes)}</td>
                  <td>
                    <button className="small" disabled={busy}
                      onClick={() => run(() => api.requestArtifact({ peer_id: o.peer_id, origin_artifact_id: o.origin_artifact_id }))}>
                      Request
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      <StatusCard
        title="Transfers"
        badge={<Badge tone="muted">{transfers.data?.length ?? 0}</Badge>}
      >
        <ErrorNote error={transfers.error} />
        {transfers.data && transfers.data.length === 0 && <Empty>No transfers.</Empty>}
        {transfers.data && transfers.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Transfer</th>
                <th scope="col">Dir</th>
                <th scope="col">State</th>
                <th scope="col">Progress</th>
                <th scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {transfers.data.map((t: ArtifactTransfer) => (
                <tr key={t.transfer_id}>
                  <td><IdTag id={t.transfer_id} /></td>
                  <td>{t.direction === 'inbound' ? '← in' : '→ out'}</td>
                  <td><Badge tone={transferTone(t.state)}>{t.state}</Badge>{t.error_type && <span className="muted small-note"> {t.error_type}</span>}</td>
                  <td>{bytes(t.received_bytes)} / {bytes(t.expected_size)}</td>
                  <td>
                    {!['completed', 'cancelled', 'rejected', 'expired'].includes(t.state) && (
                      <ConfirmButton label="Cancel" danger prompt="Cancel this transfer? It will not resume." disabled={busy}
                        onConfirm={() => run(() => api.cancelArtifactTransfer(t.transfer_id))} />
                    )}
                    {t.state === 'failed' && (
                      <button className="small" disabled={busy} onClick={() => run(() => api.retryArtifactTransfer(t.transfer_id))}>Retry</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="muted small-note">
          Bytes are transferred node-to-node over the authenticated peer endpoint — never through
          the browser. Completion requires an exact SHA-256 + size match.
        </p>
      </StatusCard>
    </div>
  )
}
