# Google Drive encrypted archive — implementation plan

**Status: PLAN ONLY. The real Google Drive client is NOT implemented.** Drive is
an optional, disabled-by-default **encrypted secondary archive** sink — never the
live database, object store, mission queue, or RF capture sink.

## What exists today (verified)

- `src/aithernet/data/destinations/drive.py`: `GoogleDriveArchiveDestination`
  (validate / readiness / upload / delete, idempotency, resumable chunking,
  **encryption-required** enforcement), a `DriveClient` Protocol, and an
  in-memory `FakeDriveClient` for tests.
- `data/service.py` constructs the destination with `client=self._drive_client`.

## What is missing (the gap to real acceptance)

- **No real `DriveClient`** — nothing talks to the Google Drive API.
- **No OAuth** — nothing loads `~/.local/state/aithernet/google-drive/oauth-client.json`,
  runs consent, or stores/refreshes a token.
- No `google-*` dependencies; no folder-tree provisioning; no Drive tests.

Therefore the operator's real-acceptance sequence (OAuth → encrypted upload →
file id → byte/digest verify → idempotent retry → delete → verify) cannot run.
This must be a separate, reviewed commit — not faked.

## Proposed implementation (separate reviewed commit + tests)

1. **Dependencies** (optional extra `aithernet[drive]`): `google-auth`,
   `google-auth-oauthlib`, `google-api-python-client` — or a thin raw-HTTP Drive
   v3 client to avoid heavy deps. Keep Drive optional so core installs stay lean.
2. **`RealGoogleDriveClient(DriveClient)`**:
   - Load the Desktop OAuth client from
     `~/.local/state/aithernet/google-drive/oauth-client.json` (dir 0700, file 0600).
   - First-run authorization via the installed-app loopback flow; persist the
     **refresh token** to `~/.local/state/aithernet/google-drive/token.json`
     (dir 0700, file 0600, outside git). Never log tokens or client contents.
   - Scope: **`https://www.googleapis.com/auth/drive.file`** (narrow — only files
     the app creates).
   - `verify_folder` → `files.get(fileId, fields=id)`.
   - `find_by_idempotency` → `files.list(q="appProperties has {key='aith_idem' and value='…'}", spaces='drive')`.
   - `resumable_upload` → Drive resumable upload protocol; set
     `appProperties.aith_idem` = idempotency key and `parents=[folder_id]`.
   - `delete_file` → `files.delete(fileId)`.
3. **Folder provisioning** (create-if-absent), recording folder ids:
   ```
   Aithernet Archive/
   ├── backups/
   ├── ingestion-archives/
   ├── training-datasets/
   ├── diagnostics/
   └── approved-rf-artifacts/
   ```
4. **Wiring**: a `_drive_client` factory builds the real client when credentials
   are present; otherwise readiness stays `unconfigured` (current behavior).
5. **Encryption stays before upload** — reuse the existing encrypted-bundle
   pipeline; the destination already rejects unencrypted bundles.
6. **Tests**: mock the Google HTTP layer for the client (auth refresh, list,
   resumable upload incl. mid-upload interrupt + resume, idempotent retry,
   delete, error→category mapping). Destination-logic tests keep using the fake.
7. **CLI/diagnostics**: a `data destination drive verify` readiness check and an
   operator acceptance command that runs OAuth → encrypted fixture upload →
   returns file id/bytes/digest → idempotent retry (same id) → delete → verify,
   printing **no** tokens.

## Real acceptance (operator-gated, after the commit lands)

OAuth consent in a browser → run the acceptance command with a small
non-sensitive **encrypted** fixture → confirm: returned Drive file id, byte count,
digest match, idempotent retry resolves to the same id, explicit deletion, and
deletion verified. Raw continuous RF captures are never auto-uploaded.

## Priority

Per project guidance, Drive comes **after** the live portal + physical-SDR /
component path. Recommend landing this only once the hosted portal acceptance is
done. `oauth-client.json` is already in place (0600) for when it does.
