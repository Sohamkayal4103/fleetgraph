"""Real Google Drive archive client (Stage 14E, Part 14 — productized).

Encrypted secondary archive ONLY. Google Drive is never the live database, object store, active
ingestion sink, mission state/queue, or primary RF capture sink. This module adds the real
``DriveClient`` (the abstraction + a fake already live in ``drive.py``) plus the OAuth installed-app
flow and the ``Aithernet Archive`` folder tree.

Design:
- Narrow scope ``drive.file`` — the app can only see/manage files it creates.
- OAuth Desktop client read from ``~/.local/state/aithernet/google-drive/oauth-client.json``;
  the refresh token is persisted to ``token.json`` in the same dir (dir 0700, files 0600).
- No heavy Google SDK: the Drive v3 REST API is called over ``httpx`` (already a core dependency);
  the authorization redirect is caught by a localhost loopback server (stdlib).
- Secrets (client secret, access/refresh tokens, authorization codes) are NEVER logged or returned.
- Uploads are idempotent: an ``appProperties`` idempotency key makes a retry resolve to the same
  file instead of creating a duplicate. Bundles must be encrypted before upload (enforced by the
  destination); ``seal_archive``/``open_archive`` provide the authenticated-encryption envelope.
"""

from __future__ import annotations

import base64
import json
import os
import secrets as _secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from aithernet.data import crypto
from aithernet.data.destinations.drive import DriveError

# -- archive layout -----------------------------------------------------------------------------

ARCHIVE_ROOT_NAME = "Aithernet Archive"
#: The fixed set of archive sub-folders (also the valid ``--category`` values).
ARCHIVE_FOLDERS = (
    "backups",
    "ingestion-archives",
    "training-datasets",
    "diagnostics",
    "approved-rf-artifacts",
)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_FILES = "https://www.googleapis.com/drive/v3/files"
_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_IDEM_KEY = "aith_idem"
_SHA_KEY = "aith_sha256"


def state_dir() -> Path:
    """The managed Google-Drive state directory (override with AITHERNET_DRIVE_STATE_DIR)."""
    env = os.environ.get("AITHERNET_DRIVE_STATE_DIR")
    return Path(env) if env else Path.home() / ".local/state/aithernet/google-drive"


def oauth_client_path() -> Path:
    return state_dir() / "oauth-client.json"


def token_path() -> Path:
    return state_dir() / "token.json"


def oauth_client_present() -> bool:
    """True when a Google Desktop OAuth client is installed for this node."""
    return oauth_client_path().is_file()


def _mask_client_id(client_id: str) -> str:
    """Show enough of the (non-secret) client id to identify it, never the secret."""
    if not client_id:
        return ""
    head = client_id.split(".")[0]
    return (head[:6] + "…" + head[-4:]) if len(head) > 12 else head + "…"


def oauth_client_status() -> dict:
    """A masked, secret-free description of the installed OAuth client (for `data` status/verify).

    Never returns the client secret or full client id — only whether a valid Desktop client is
    installed and a masked identifier + the repair command when it is not."""
    path = oauth_client_path()
    if not path.is_file():
        return {
            "installed": False,
            "reason": "oauth-client.json missing",
            "repair": "aithernet data drive-client install <google-oauth-client.json>",
        }
    try:
        raw = json.loads(path.read_text())
        node = raw.get("installed") or raw.get("web") or {}
        cid = node.get("client_id") or ""
        ok = bool(cid) and bool(node.get("client_secret"))
        typ = "installed" if raw.get("installed") else ("web" if raw.get("web") else "unknown")
        if not ok:
            return {"installed": False, "reason": "not a Desktop OAuth client",
                    "repair": "aithernet data drive-client install <google-oauth-client.json>"}
        return {"installed": True, "client_type": typ, "client_id_masked": _mask_client_id(cid)}
    except (OSError, json.JSONDecodeError):
        return {"installed": False, "reason": "oauth-client.json is not readable JSON",
                "repair": "aithernet data drive-client install <google-oauth-client.json>"}


def install_oauth_client(src_path: Path) -> dict:
    """Install an operator-provided Google **Desktop** OAuth client into the managed state dir.

    Validates it is a Desktop (``installed``) client with a client id + secret, then writes it to
    ``oauth-client.json`` (dir 0700, file 0600). Returns a masked, secret-free receipt. The client
    secret is never logged or returned. Aithernet does not embed an OAuth client in the release
    (installed desktop apps cannot keep a secret confidential); the operator supplies their own."""
    src = Path(src_path)
    if not src.is_file():
        raise DriveError("unconfigured", f"OAuth client file not found: {src}")
    try:
        raw = json.loads(src.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DriveError("unconfigured", "OAuth client file is not readable JSON") from exc
    node = raw.get("installed") or raw.get("web")
    if not node or not node.get("client_id") or not node.get("client_secret"):
        raise DriveError("unconfigured",
                         "not a Desktop OAuth client (need an 'installed' client id + secret)")
    _write_secret_file(oauth_client_path(), raw)
    return {"installed": True, "path": str(oauth_client_path()),
            "client_id_masked": _mask_client_id(node["client_id"]),
            "client_type": "installed" if raw.get("installed") else "web"}


def _ensure_state_dir() -> Path:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _write_secret_file(path: Path, data: dict) -> None:
    _ensure_state_dir()
    tmp = path.with_suffix(".tmp")
    # Create with 0600 from the start (umask-independent).
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


# -- archive authenticated-encryption envelope --------------------------------------------------

#: Magic contains the substring the destination's encrypted-required gate looks for.
_ARCHIVE_MAGIC = "aith-batch-enc.archive.v1"


def seal_archive(plaintext: bytes, key_b64: str) -> tuple[bytes, str]:
    """Encrypt a file for the Drive archive. Returns ``(bundle, plaintext_sha256_hex)``.

    The bundle is a JSON envelope (AES-256-GCM, established ``crypto`` primitive) whose first bytes
    contain the ``aith-batch-enc`` marker so the destination accepts it as encrypted. Fails closed
    when no key is supplied.
    """
    if not key_b64:
        raise DriveError("encryption_required", "no encryption key configured")
    plain_sha = crypto.sha256_hex(plaintext)
    env = crypto.encrypt(plaintext, key_b64, aad=plain_sha.encode())
    bundle = json.dumps({
        "magic": _ARCHIVE_MAGIC,
        "scheme": env.version,
        "nonce_b64": env.nonce_b64,
        "aad_b64": env.aad_b64,
        "plaintext_sha256": plain_sha,
        "ciphertext_b64": base64.b64encode(env.ciphertext).decode("ascii"),
    }).encode("utf-8")
    return bundle, plain_sha


def open_archive(bundle: bytes, key_b64: str) -> bytes:
    """Decrypt + verify an archive bundle produced by :func:`seal_archive`."""
    if not key_b64:
        raise DriveError("encryption_required", "no encryption key configured")
    try:
        obj = json.loads(bundle)
        env = crypto.EncryptionEnvelope(
            version=obj["scheme"], nonce_b64=obj["nonce_b64"],
            ciphertext=base64.b64decode(obj["ciphertext_b64"]), aad_b64=obj.get("aad_b64"),
        )
        plaintext = crypto.decrypt(env, key_b64)
    except Exception as exc:  # noqa: BLE001 — deterministic failure, no secret/traceback leak
        raise DriveError("decryption_failed", "archive bundle could not be decrypted") from exc
    if crypto.sha256_hex(plaintext) != obj.get("plaintext_sha256"):
        raise DriveError("integrity_failed", "decrypted plaintext digest mismatch")
    return plaintext


# -- OAuth (installed-app / Desktop client) -----------------------------------------------------


@dataclass
class _Loopback:
    code: str | None = None
    state: str | None = None
    error: str | None = None


class GoogleDriveOAuth:
    """Loads the Desktop OAuth client, runs authorization, and mints access tokens via refresh."""

    def __init__(self, client_path: Path | None = None, token_file: Path | None = None) -> None:
        self.client_path = client_path or oauth_client_path()
        self.token_file = token_file or token_path()
        self._access: tuple[str, float] | None = None  # (token, expires_at)

    def _client(self) -> dict:
        if not self.client_path.is_file():
            raise DriveError("unconfigured", "oauth-client.json not found")
        raw = json.loads(self.client_path.read_text())
        node = raw.get("installed") or raw.get("web")
        if not node or not node.get("client_id") or not node.get("client_secret"):
            raise DriveError("unconfigured", "oauth-client.json is not a Desktop OAuth client")
        return node

    def authorized(self) -> bool:
        return self.token_file.is_file() or getattr(self, "_seeded_refresh", None) is not None

    def _seed_refresh_token(self, refresh_token: str) -> None:
        """Server-side (owner archive worker): seed a refresh token in-memory — no token file.

        The token is never written to disk here and never leaves the process. ``access_token()``
        will refresh against it using the server-side OAuth client."""
        self._seeded_refresh = refresh_token

    def authorize(self, *, open_browser: bool = True, on_url=None, timeout: float = 300.0) -> dict:
        """Run the installed-app loopback flow; persist the refresh token (0600). No secrets."""
        node = self._client()
        result = _Loopback()
        ready = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence; never log query (may carry the code)
                return

            def do_GET(self):
                q = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(q)
                result.code = (params.get("code") or [None])[0]
                result.state = (params.get("state") or [None])[0]
                result.error = (params.get("error") or [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Aithernet: authorization received. You can close this tab.")
                ready.set()

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/"
        state = _secrets.token_urlsafe(24)
        auth_uri = node.get("auth_uri", "https://accounts.google.com/o/oauth2/v2/auth")
        params = {
            "response_type": "code", "client_id": node["client_id"],
            "redirect_uri": redirect_uri, "scope": DRIVE_SCOPE,
            "access_type": "offline", "prompt": "consent", "state": state,
        }
        auth_url = auth_uri + "?" + urllib.parse.urlencode(params)

        t = threading.Thread(target=server.handle_request, daemon=True)
        t.start()
        # Surface the URL to the operator BEFORE blocking (so they can open it manually), then
        # optionally try to open a browser too.
        if on_url:
            on_url(auth_url)
        if open_browser:
            try:
                import webbrowser
                webbrowser.open(auth_url)
            except Exception:  # noqa: BLE001
                pass
        if not ready.wait(timeout):
            server.server_close()
            raise DriveError("authorization_timeout", "no authorization received")
        server.server_close()
        if result.error:
            raise DriveError("authorization_denied", "authorization was not granted")
        if not result.code or result.state != state:
            raise DriveError("authorization_failed", "invalid authorization response")

        token_uri = node.get("token_uri", "https://oauth2.googleapis.com/token")
        resp = httpx.post(token_uri, data={
            "grant_type": "authorization_code", "code": result.code,
            "client_id": node["client_id"], "client_secret": node["client_secret"],
            "redirect_uri": redirect_uri,
        }, timeout=30)
        if resp.status_code != 200:
            raise DriveError("token_exchange_failed", "could not exchange the authorization code")
        tok = resp.json()
        if not tok.get("refresh_token"):
            raise DriveError("no_refresh_token", "Google did not return a refresh token")
        _write_secret_file(self.token_file, {
            "refresh_token": tok["refresh_token"], "scope": DRIVE_SCOPE,
            "token_uri": token_uri, "obtained_at": int(time.time()),
        })
        self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3000)) - 60)
        return {"authorized": True, "scope": DRIVE_SCOPE}

    def access_token(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        seeded = getattr(self, "_seeded_refresh", None)
        if seeded is not None:
            saved = {"refresh_token": seeded}
        elif self.token_file.is_file():
            saved = json.loads(self.token_file.read_text())
        else:
            raise DriveError("unauthorized", "not authorized — run 'aithernet drive authorize'")
        node = self._client()
        token_uri = saved.get("token_uri", node.get("token_uri", "https://oauth2.googleapis.com/token"))
        resp = httpx.post(token_uri, data={
            "grant_type": "refresh_token", "refresh_token": saved["refresh_token"],
            "client_id": node["client_id"], "client_secret": node["client_secret"],
        }, timeout=30)
        if resp.status_code != 200:
            raise DriveError("token_refresh_failed", "could not refresh the access token")
        tok = resp.json()
        self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3000)) - 60)
        return self._access[0]


# -- real Drive client --------------------------------------------------------------------------


class RealGoogleDriveClient:
    """Drive v3 REST client (httpx) implementing ``DriveClient`` over the ``drive.file`` scope."""

    def __init__(self, oauth: GoogleDriveOAuth | None = None, *,
                 client: httpx.Client | None = None) -> None:
        self.oauth = oauth or GoogleDriveOAuth()
        self._http = client or httpx.Client(timeout=60)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.oauth.access_token()}"}

    def _raise(self, resp: httpx.Response, what: str) -> None:
        # Map status to a bounded category; never include the response body (may echo input).
        code = resp.status_code
        cat = {401: "expired_credentials", 403: "permission_denied",
               404: "not_found", 429: "quota_exceeded"}.get(code, "drive_error")
        raise DriveError(cat, what)

    # folders ----------------------------------------------------------------
    def find_or_create_folder(self, name: str, parent_id: str | None) -> str:
        q = [f"name = '{name}'", f"mimeType = '{_FOLDER_MIME}'", "trashed = false"]
        if parent_id:
            q.append(f"'{parent_id}' in parents")
        r = self._http.get(_FILES, headers=self._headers(), params={
            "q": " and ".join(q), "fields": "files(id,name)", "spaces": "drive",
        })
        if r.status_code != 200:
            self._raise(r, "folder lookup failed")
        files = r.json().get("files", [])
        if files:
            return files[0]["id"]
        meta = {"name": name, "mimeType": _FOLDER_MIME}
        if parent_id:
            meta["parents"] = [parent_id]
        c = self._http.post(_FILES, headers=self._headers(), json=meta,
                            params={"fields": "id"})
        if c.status_code not in (200, 201):
            self._raise(c, "folder create failed")
        return c.json()["id"]

    def ensure_archive_tree(self) -> dict:
        """Create/locate ``Aithernet Archive`` + its sub-folders. Returns {name: folder_id}."""
        root = self.find_or_create_folder(ARCHIVE_ROOT_NAME, None)
        out = {"_root": root}
        for sub in ARCHIVE_FOLDERS:
            out[sub] = self.find_or_create_folder(sub, root)
        return out

    # DriveClient protocol ---------------------------------------------------
    def verify_folder(self, folder_id: str) -> bool:
        r = self._http.get(f"{_FILES}/{folder_id}", headers=self._headers(),
                          params={"fields": "id,trashed,mimeType"})
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            self._raise(r, "folder verify failed")
        body = r.json()
        return not body.get("trashed", False) and body.get("mimeType") == _FOLDER_MIME

    def find_by_idempotency(self, folder_id: str, idempotency_key: str) -> dict | None:
        q = [f"appProperties has {{ key='{_IDEM_KEY}' and value='{idempotency_key}' }}",
             f"'{folder_id}' in parents", "trashed = false"]
        r = self._http.get(_FILES, headers=self._headers(), params={
            "q": " and ".join(q), "spaces": "drive",
            "fields": "files(id,name,size,sha256Checksum,appProperties)",
        })
        if r.status_code != 200:
            self._raise(r, "idempotency lookup failed")
        files = r.json().get("files", [])
        if not files:
            return None
        f = files[0]
        return {"drive_file_id": f["id"], "folder_id": folder_id, "name": f.get("name"),
                "byte_size": int(f.get("size", 0)),
                "sha256": (f.get("appProperties") or {}).get(_SHA_KEY)}

    def resumable_upload(self, *, folder_id: str, name: str, data: bytes,
                         idempotency_key: str, chunk_bytes: int) -> dict:
        # Idempotency first: never create a duplicate logical archive on retry.
        existing = self.find_by_idempotency(folder_id, idempotency_key)
        if existing is not None:
            return existing
        bundle_sha = crypto.sha256_hex(data)
        meta = {"name": name, "parents": [folder_id],
                "appProperties": {_IDEM_KEY: idempotency_key, _SHA_KEY: bundle_sha}}
        # Start a resumable session, then PUT the bytes (a single PUT to the session URI is valid).
        start = self._http.post(_UPLOAD, headers={**self._headers(),
                                "Content-Type": "application/json; charset=UTF-8"},
                                params={"uploadType": "resumable", "fields": "id,size"},
                                json=meta)
        if start.status_code not in (200, 201):
            self._raise(start, "resumable session failed")
        session_uri = start.headers.get("location") or start.headers.get("Location")
        if not session_uri:
            raise DriveError("drive_error", "no resumable session URI returned")
        put = self._http.put(session_uri, headers={"Content-Type": "application/octet-stream"},
                            content=data)
        if put.status_code not in (200, 201):
            self._raise(put, "resumable upload failed")
        body = put.json()
        return {"drive_file_id": body["id"], "folder_id": folder_id, "name": name,
                "byte_size": int(body.get("size", len(data))), "sha256": bundle_sha}

    def delete_file(self, file_id: str) -> bool:
        r = self._http.delete(f"{_FILES}/{file_id}", headers=self._headers())
        if r.status_code in (200, 204):
            return True
        if r.status_code == 404:
            return False
        self._raise(r, "delete failed")
        return False

    # verification helpers (used by `drive verify`) --------------------------
    def get_metadata(self, file_id: str) -> dict | None:
        r = self._http.get(f"{_FILES}/{file_id}", headers=self._headers(),
                          params={"fields": "id,name,size,sha256Checksum,trashed,appProperties"})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            self._raise(r, "metadata fetch failed")
        return r.json()

    def download(self, file_id: str) -> bytes:
        r = self._http.get(f"{_FILES}/{file_id}", headers=self._headers(),
                          params={"alt": "media"})
        if r.status_code != 200:
            self._raise(r, "download failed")
        return r.content
