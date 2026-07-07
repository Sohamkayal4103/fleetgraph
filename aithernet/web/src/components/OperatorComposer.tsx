// Bounded operator message composer (Stage 13C, Part G).
//
// The operator may only pick a TRUSTED, ENABLED, authorized peer (the backend re-validates),
// a message type, bounded text/data, and reply expectations. There is deliberately NO field
// for an endpoint, signing identity, HTTP header, or retry policy: the node signs and delivers
// through the existing durable Stage 13A outbox (the browser never contacts a peer). Submitting
// is disabled while in flight so a double-click cannot queue two messages (Part S.31).

import { useState } from 'react'
import { api } from '../api/client'
import { errorMessage, parseJsonObject } from '../api/hooks'
import type { FleetPeer, OperatorMessageResponse } from '../api/types'
import { Empty, ErrorNote, Field } from './StatusCard'
import { IdTag } from './commsUi'

export function OperatorComposer({
  peers,
  fixedPeerId,
  conversationId,
  onSent,
}: {
  peers: FleetPeer[]
  fixedPeerId?: string
  conversationId?: string | null
  onSent?: (res: OperatorMessageResponse) => void
}) {
  // Only trusted + enabled + may_receive_messages peers may be addressed (Part S.22/23).
  const eligible = peers.filter(
    (p) => p.trust_state === 'trusted' && p.enabled && p.permissions.may_receive_messages,
  )
  const [peerId, setPeerId] = useState(fixedPeerId ?? eligible[0]?.peer_id ?? '')
  const [messageType, setMessageType] = useState<'request' | 'update' | 'reply'>('request')
  const [text, setText] = useState('')
  const [dataText, setDataText] = useState('')
  const [expectsReply, setExpectsReply] = useState(false)
  const [deadline, setDeadline] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<OperatorMessageResponse | null>(null)

  const effectivePeer = fixedPeerId ?? peerId

  if (!fixedPeerId && eligible.length === 0) {
    return (
      <Empty>
        No trusted, enabled, message-authorized peer to send to. Trust a peer and grant
        “may_receive_messages” in the Fleet view first.
      </Empty>
    )
  }

  const submit = async () => {
    setError(null)
    if (!effectivePeer) {
      setError('Select a peer.')
      return
    }
    const parsed = parseJsonObject(dataText)
    if (!parsed.ok) {
      setError(parsed.error)
      return
    }
    setBusy(true)
    try {
      const res = await api.sendOperatorMessage({
        peer_id: effectivePeer,
        message_type: messageType,
        text,
        data: parsed.value,
        expects_reply: expectsReply,
        response_deadline: deadline || null,
        conversation_id: conversationId ?? null,
      })
      setResult(res)
      setText('')
      setDataText('')
      onSent?.(res)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="composer stack">
      {!fixedPeerId && (
        <Field label="Peer">
          <select
            value={peerId}
            onChange={(e) => setPeerId(e.target.value)}
            aria-label="Recipient peer"
          >
            {eligible.map((p) => (
              <option key={p.peer_id} value={p.peer_id}>
                {p.name} ({p.peer_id.slice(0, 8)}…)
              </option>
            ))}
          </select>
        </Field>
      )}
      <Field label="Type">
        <select
          value={messageType}
          onChange={(e) => setMessageType(e.target.value as 'request' | 'update' | 'reply')}
          aria-label="Message type"
        >
          <option value="request">request</option>
          <option value="update">update</option>
          <option value="reply">reply</option>
        </select>
      </Field>
      <label className="stack-label" htmlFor="composer-text">
        Message text
        <textarea
          id="composer-text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={3}
          placeholder="Bounded text — sent as untrusted data to the peer."
        />
      </label>
      <label className="stack-label" htmlFor="composer-data">
        Structured data (optional JSON object)
        <textarea
          id="composer-data"
          value={dataText}
          onChange={(e) => setDataText(e.target.value)}
          rows={2}
          placeholder='{"key": "value"}'
        />
      </label>
      <div className="inline-fields">
        <label className="check-label">
          <input
            type="checkbox"
            checked={expectsReply}
            onChange={(e) => setExpectsReply(e.target.checked)}
          />
          Expect a semantic reply
        </label>
        {expectsReply && (
          <label className="check-label">
            Deadline (ISO, optional)
            <input
              type="text"
              value={deadline}
              onChange={(e) => setDeadline(e.target.value)}
              placeholder="2026-06-14T12:00:00Z"
            />
          </label>
        )}
      </div>
      <div className="inline-fields">
        <button type="button" onClick={submit} disabled={busy}>
          {busy ? 'Queuing…' : 'Queue message'}
        </button>
        <span className="muted small-note">
          Queues one message via the durable outbox. Queued/acknowledged means DELIVERED, not
          semantically answered.
        </span>
      </div>
      <ErrorNote error={error} />
      {result && (
        <div className="result-box" role="status">
          <Field label="Queued">
            <IdTag id={result.message_id} label="message" />
          </Field>
          <Field label="Origin">{result.origin}</Field>
          <Field label="Delivery">{result.status} (not yet acknowledged or answered)</Field>
          {result.expects_reply && <Field label="Awaiting">a correlated semantic reply</Field>}
        </div>
      )}
    </div>
  )
}
