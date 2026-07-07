// Hardware dashboard section (Stage 14B, Part P).
//
// Read-only by default: discovery providers + availability, discovered devices (present/missing,
// enabled/disabled, health, RX/TX, channels, concise frequency/sample-rate, backend bindings),
// and current leases (owning mission, expiry). Bounded operator actions (refresh, enable/disable,
// acquire/release, revoke-with-confirmation) appear only when the node enables operator lease
// actions. There is NO raw hardware path, device argument, full command output, or credential
// shown, and accessible TEXT states are used (never colour alone).

import { useState } from 'react'

import { api } from '../api/client'
import { useResource } from '../api/hooks'
import type { HardwareDevice, HardwareLease, HardwareSupportEntry } from '../api/types'
import { ConfirmButton } from './ConfirmButton'
import { Badge, Empty, ErrorNote, Field, Loading, StatusCard, type Tone } from './StatusCard'

function statusTone(status: string): Tone {
  if (status === 'present') return 'ok'
  if (status === 'disabled') return 'muted'
  if (status === 'degraded') return 'warn'
  if (status === 'missing' || status === 'unavailable') return 'warn'
  return 'bad'
}

function leaseTone(state: string): Tone {
  if (state === 'active') return 'ok'
  if (state === 'pending') return 'info'
  if (state === 'orphaned' || state === 'expired') return 'warn'
  return 'muted'
}

function classificationTone(c: string): Tone {
  if (c === 'capture_qualified' || c === 'survey_qualified' ||
      c === 'disconnect_recovery_qualified') return 'ok'
  if (c === 'rx_qualified' || c === 'tx_lease_qualified' || c === 'probed') return 'info'
  if (c === 'failed' || c === 'unsupported') return 'bad'
  return 'muted'
}

function rxtx(value: boolean | null | undefined, label: string): string {
  if (value === true) return label
  if (value === false) return `no-${label}`
  return `${label}?`
}

export function HardwarePage() {
  const status = useResource(() => api.getHardwareStatus(), [])
  const devices = useResource(() => api.getHardwareDevices(), [])
  const leases = useResource(() => api.getHardwareLeases(), [])
  const support = useResource(() => api.getHardwareSupportMatrix(), [])
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const operator = status.data?.operator_lease_actions_enabled ?? false

  function reloadAll() {
    status.reload()
    devices.reload()
    leases.reload()
  }

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setActionError(null)
    try {
      await action()
      reloadAll()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid">
      <StatusCard
        title="Discovery providers"
        badge={
          status.data ? (
            <Badge tone={status.data.enabled ? 'ok' : 'muted'}>
              {status.data.enabled ? 'enabled' : 'disabled'}
            </Badge>
          ) : undefined
        }
        actions={
          <button type="button" disabled={busy} onClick={() => run(() => api.refreshHardware())}>
            Refresh inventory
          </button>
        }
      >
        <Loading loading={status.loading} />
        <ErrorNote error={status.error} />
        {actionError && <ErrorNote error={actionError} />}
        {status.data && !status.data.enabled && (
          <Empty>Managed hardware is disabled; this node runs without attached SDR hardware.</Empty>
        )}
        {status.data && status.data.providers.length === 0 && status.data.enabled && (
          <Empty>No discovery providers configured.</Empty>
        )}
        {status.data && status.data.providers.length > 0 && (
          <table className="data-table">
            <thead>
              <tr><th>Provider</th><th>Kind</th><th>Required</th><th>Availability</th></tr>
            </thead>
            <tbody>
              {status.data.providers.map((p) => (
                <tr key={p.provider_id}>
                  <td className="mono">{p.provider_id}</td>
                  <td>{p.kind}</td>
                  <td>{p.required ? 'yes' : 'no'}</td>
                  <td>
                    <Badge tone={p.available ? 'ok' : 'warn'}>
                      {p.available ? 'available' : 'unavailable'}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      <StatusCard
        title="Devices"
        badge={<Badge tone="info">{devices.data?.length ?? 0}</Badge>}
        actions={<button type="button" onClick={() => devices.reload()}>Reload</button>}
      >
        <Loading loading={devices.loading} />
        <ErrorNote error={devices.error} />
        {devices.data && devices.data.length === 0 && <Empty>No devices discovered.</Empty>}
        {devices.data && devices.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>Device</th><th>Provider</th><th>Status</th><th>RX/TX</th>
                <th>Backends</th>{operator && <th>Actions</th>}
              </tr>
            </thead>
            <tbody>
              {devices.data.map((d: HardwareDevice) => (
                <tr key={d.device_id}>
                  <td>
                    <span className="mono">{d.display_name ?? d.device_id.slice(0, 8)}</span>
                    {d.identity_limited && <span className="small-note muted"> (identity-limited)</span>}
                  </td>
                  <td>{d.provider_id}</td>
                  <td>
                    <Badge tone={statusTone(d.status)}>{d.status}</Badge>
                    {!d.enabled && <span className="small-note muted"> disabled</span>}
                  </td>
                  <td>{rxtx(d.rx, 'rx')} / {rxtx(d.tx, 'tx')}</td>
                  <td>{d.compatible_backends.join(', ') || '—'}</td>
                  {operator && (
                    <td>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          run(() => api.setHardwareDeviceEnabled(d.device_id, !d.enabled))
                        }
                      >
                        {d.enabled ? 'Disable' : 'Enable'}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      <StatusCard
        title="Leases"
        badge={<Badge tone="info">{leases.data?.length ?? 0}</Badge>}
        actions={<button type="button" onClick={() => leases.reload()}>Reload</button>}
      >
        <Loading loading={leases.loading} />
        <ErrorNote error={leases.error} />
        {!operator && (
          <Field label="Operator actions">
            disabled (acquire/release/revoke are CLI-only on this node)
          </Field>
        )}
        {leases.data && leases.data.length === 0 && <Empty>No leases.</Empty>}
        {leases.data && leases.data.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>Lease</th><th>Device</th><th>Mode</th><th>Mission</th>
                <th>State</th><th>Expires</th>{operator && <th>Actions</th>}
              </tr>
            </thead>
            <tbody>
              {leases.data.map((l: HardwareLease) => (
                <tr key={l.lease_id}>
                  <td className="mono">{l.lease_id.slice(0, 8)}</td>
                  <td className="mono">{l.device_id.slice(0, 8)}</td>
                  <td>{l.lease_mode} · {l.direction}</td>
                  <td className="mono">{l.mission_id ? l.mission_id.slice(0, 8) : '—'}</td>
                  <td><Badge tone={leaseTone(l.state)}>{l.state}</Badge></td>
                  <td>{l.expires_at ?? '—'}</td>
                  {operator && (
                    <td>
                      {(l.state === 'active' || l.state === 'pending') && (
                        <>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => run(() => api.releaseHardwareLease(l.lease_id))}
                          >
                            Release
                          </button>{' '}
                          <ConfirmButton
                            label="Revoke"
                            danger
                            prompt="Revoke this lease? Active RF work loses the device."
                            disabled={busy}
                            onConfirm={() => run(() => api.revokeHardwareLease(l.lease_id))}
                          />
                        </>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </StatusCard>

      <StatusCard
        title="Qualification & support"
        badge={<Badge tone="info">{support.data?.length ?? 0}</Badge>}
        actions={<button type="button" onClick={() => support.reload()}>Reload</button>}
      >
        <Loading loading={support.loading} />
        <ErrorNote error={support.error} />
        {support.data && support.data.length === 0 && <Empty>No devices to qualify.</Empty>}
        {support.data && support.data.length > 0 && (
          <>
            <p className="small-note muted">
              Classification reflects ACTUAL qualification evidence — a device is never "supported"
              just because it was discovered. Run a real qualification with{' '}
              <span className="mono">aithernet hardware qualify run &lt;device-id&gt;</span>.
            </p>
            <table className="data-table">
              <thead>
                <tr><th>Device</th><th>Provider</th><th>Driver</th><th>Classification</th>
                  <th>Last qualified</th></tr>
              </thead>
              <tbody>
                {support.data.map((s: HardwareSupportEntry) => (
                  <tr key={s.device_id}>
                    <td className="mono">{s.display_name ?? s.device_id.slice(0, 8)}</td>
                    <td>{s.provider_id}</td>
                    <td>{s.driver ?? '—'}</td>
                    <td>
                      <Badge tone={classificationTone(s.classification)}>{s.classification}</Badge>
                    </td>
                    <td>{s.last_qualified_at ?? 'not qualified'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </StatusCard>
    </div>
  )
}
