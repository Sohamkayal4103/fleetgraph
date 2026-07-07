// An accessible inline confirmation for destructive actions (Part Q.59 / Part R.16).
//
// Clicking the action reveals an inline confirm/cancel pair (a labelled group with role and a
// focused confirm button) instead of firing immediately — so trust, revoke, disable, retry,
// cancel-wait, etc. always require a second, deliberate, keyboard-operable confirmation.

import { useState } from 'react'

export function ConfirmButton({
  label,
  confirmLabel = 'Confirm',
  prompt,
  danger,
  disabled,
  onConfirm,
}: {
  label: string
  confirmLabel?: string
  prompt?: string
  danger?: boolean
  disabled?: boolean
  onConfirm: () => void
}) {
  const [open, setOpen] = useState(false)

  if (!open) {
    return (
      <button
        type="button"
        className={`small${danger ? ' danger' : ''}`}
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        {label}
      </button>
    )
  }
  return (
    <span
      className="confirm-inline"
      role="group"
      aria-label={`Confirm: ${label}`}
    >
      <span className="confirm-prompt">{prompt ?? `${label}?`}</span>
      <button
        type="button"
        className={`small${danger ? ' danger' : ''}`}
        autoFocus
        onClick={() => {
          setOpen(false)
          onConfirm()
        }}
      >
        {confirmLabel}
      </button>
      <button type="button" className="small" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </span>
  )
}
