"""Google Drive encrypted-archive tests (Stage 14E, Part 14).

Covers the archive encryption envelope (seal/open + fail-closed), the destination's
encryption-required + idempotency + deletion semantics via the in-memory fake, and the REAL
``RealGoogleDriveClient`` driven against a stateful httpx ``MockTransport`` (no Google account):
folder provisioning, idempotent upload (no duplicate), verify/download, delete, error→category
mapping with no response body leak, and token-file permissions.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat

import httpx
import pytest

from aithernet.data import crypto
from aithernet.data.destinations import google_drive_client as g
from aithernet.data.destinations.drive import (
    DriveError,
    FakeDriveClient,
    GoogleDriveArchiveDestination,
)

# -- archive envelope ---------------------------------------------------------------------------


def test_seal_archive_roundtrip_and_digest():
    key = crypto.generate_key_b64()
    plaintext = b"non-sensitive archive fixture \x00\x01\x02"
    bundle, sha = g.seal_archive(plaintext, key)
    assert bundle[:1] == b"{" and b"aith-batch-enc" in bundle[:64]   # destination gate accepts it
    assert sha == crypto.sha256_hex(plaintext)
    assert g.open_archive(bundle, key) == plaintext


def test_seal_archive_fails_closed_without_key():
    with pytest.raises(DriveError) as exc:
        g.seal_archive(b"x", "")
    assert exc.value.category == "encryption_required"


def test_open_archive_rejects_tampered_bundle():
    key = crypto.generate_key_b64()
    bundle, _ = g.seal_archive(b"hello", key)
    obj = json.loads(bundle)
    obj["ciphertext_b64"] = obj["ciphertext_b64"][:-4] + "AAAA"  # corrupt
    with pytest.raises(DriveError):
        g.open_archive(json.dumps(obj).encode(), key)


# -- destination (fake client) ------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_destination_rejects_unencrypted_bundle():
    dest = GoogleDriveArchiveDestination(folder_id="folder-test", client=FakeDriveClient())
    res = _run(dest.upload(batch_id="b1", idempotency_key="k1", bundle=b"plain-bytes", manifest={}))
    assert res.ok is False and res.failure_category == "encryption_required"


def test_destination_idempotent_upload_no_duplicate():
    fake = FakeDriveClient()
    dest = GoogleDriveArchiveDestination(folder_id="folder-test", client=fake)
    key = crypto.generate_key_b64()
    bundle, _ = g.seal_archive(b"encrypted-payload", key)
    r1 = _run(dest.upload(batch_id="b1", idempotency_key="idem-1", bundle=bundle, manifest={}))
    r2 = _run(dest.upload(batch_id="b1", idempotency_key="idem-1", bundle=bundle, manifest={}))
    assert r1.ok and r2.ok
    assert r1.receipt["drive_file_id"] == r2.receipt["drive_file_id"]   # same logical file
    assert fake.upload_calls == 2 and len(fake._files) == 1            # retried, but no duplicate


def test_destination_delete_unknown_state_is_not_success():
    # When the backend errors mid-delete, the remote state is UNKNOWN — never reported as success.
    class _RaisingDelete(FakeDriveClient):
        def delete_file(self, file_id):
            raise DriveError("expired_credentials", "auth")

    dest = GoogleDriveArchiveDestination(folder_id="folder-test", client=_RaisingDelete())
    res = _run(dest.delete(remote_ref="drive-1"))
    assert res.ok is False and res.state == "unknown"


# -- real client against a stateful mocked Drive API --------------------------------------------


class _OAuthStub:
    def access_token(self):
        return "test-access-token"


def _mock_drive():
    """A tiny stateful Drive: folders + files keyed by id, with appProperties idempotency."""
    state = {"seq": 0, "files": {}}

    def new_id(prefix):
        state["seq"] += 1
        return f"{prefix}{state['seq']}"

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        method = request.method
        # resumable session start
        if method == "POST" and path == "/upload/drive/v3/files":
            meta = json.loads(request.content)
            fid = new_id("file")
            state["_pending"] = (fid, meta)
            return httpx.Response(200, headers={"location": "https://up.example/session/" + fid})
        # resumable PUT (session URI on another host)
        if method == "PUT" and url.host == "up.example":
            fid = path.rsplit("/", 1)[-1]
            pend_id, meta = state["_pending"]
            data = request.content
            state["files"][pend_id] = {
                "id": pend_id, "name": meta["name"], "parents": meta.get("parents", []),
                "appProperties": meta.get("appProperties", {}), "size": str(len(data)),
                "content": data, "trashed": False, "mimeType": "application/octet-stream",
            }
            return httpx.Response(200, json={"id": pend_id, "size": str(len(data))})
        # files list (folder lookup / idempotency lookup)
        if method == "GET" and path == "/drive/v3/files":
            q = url.params.get("q", "")
            files = list(state["files"].values())
            if "mimeType = 'application/vnd.google-apps.folder'" in q:
                # folder lookup by name
                name = q.split("name = '", 1)[1].split("'", 1)[0]
                match = [f for f in files
                         if f.get("mimeType") == g._FOLDER_MIME
                         and f["name"] == name and not f["trashed"]]
            elif "appProperties has" in q:
                idem = q.split("value='", 1)[1].split("'", 1)[0]
                match = [f for f in files if f.get("appProperties", {}).get("aith_idem") == idem
                         and not f["trashed"]]
            else:
                match = []
            return httpx.Response(200, json={"files": [
                {"id": f["id"], "name": f["name"], "size": f.get("size"),
                 "appProperties": f.get("appProperties", {})} for f in match]})
        # folder create
        if method == "POST" and path == "/drive/v3/files":
            meta = json.loads(request.content)
            fid = new_id("folder")
            state["files"][fid] = {"id": fid, "name": meta["name"], "trashed": False,
                                   "mimeType": meta["mimeType"], "parents": meta.get("parents", [])}
            return httpx.Response(200, json={"id": fid})
        # metadata / download / delete by id
        if path.startswith("/drive/v3/files/"):
            fid = path.rsplit("/", 1)[-1]
            f = state["files"].get(fid)
            if method == "DELETE":
                if not f:
                    return httpx.Response(404)
                del state["files"][fid]
                return httpx.Response(204)
            if method == "GET" and url.params.get("alt") == "media":
                if not f:
                    return httpx.Response(404)
                return httpx.Response(200, content=f["content"])
            if method == "GET":
                if not f:
                    return httpx.Response(404)
                return httpx.Response(200, json={
                    "id": fid, "name": f["name"], "size": f.get("size"),
                    "sha256Checksum": None, "trashed": f["trashed"],
                    "mimeType": f.get("mimeType"), "appProperties": f.get("appProperties", {})})
        return httpx.Response(400, json={"error": "unhandled"})

    return state, handler


def _client():
    _, handler = _mock_drive()
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return g.RealGoogleDriveClient(_OAuthStub(), client=http)


def test_real_client_archive_tree_and_idempotent_upload():
    client = _client()
    tree = client.ensure_archive_tree()
    assert set(g.ARCHIVE_FOLDERS).issubset(tree.keys()) and tree["_root"]
    # idempotent upload: two calls with the same key -> one file
    data = b'{"magic":"aith-batch-enc.archive.v1","x":1}'
    m1 = client.resumable_upload(folder_id=tree["backups"], name="a.enc", data=data,
                                 idempotency_key="idem-xyz", chunk_bytes=1024)
    m2 = client.resumable_upload(folder_id=tree["backups"], name="a.enc", data=data,
                                 idempotency_key="idem-xyz", chunk_bytes=1024)
    assert m1["drive_file_id"] == m2["drive_file_id"]
    assert m1["byte_size"] == len(data)
    # verify metadata + download round-trips the exact bytes
    assert client.get_metadata(m1["drive_file_id"])["id"] == m1["drive_file_id"]
    assert client.download(m1["drive_file_id"]) == data
    # delete + verify gone
    assert client.delete_file(m1["drive_file_id"]) is True
    assert client.get_metadata(m1["drive_file_id"]) is None
    assert client.delete_file(m1["drive_file_id"]) is False  # already gone


def test_real_client_maps_errors_without_leaking_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "SECRET-LOOKING-DETAIL"}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = g.RealGoogleDriveClient(_OAuthStub(), client=http)
    with pytest.raises(DriveError) as exc:
        client.find_or_create_folder("x", None)
    assert exc.value.category == "permission_denied"
    assert "SECRET-LOOKING-DETAIL" not in str(exc.value)   # response body never surfaced


# -- token storage permissions ------------------------------------------------------------------


def test_token_file_written_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_DRIVE_STATE_DIR", str(tmp_path / "gd"))
    g._write_secret_file(g.token_path(), {"refresh_token": "rt", "scope": g.DRIVE_SCOPE})
    assert g.token_path().is_file()
    assert stat.S_IMODE(os.stat(g.token_path()).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(g.state_dir()).st_mode) == 0o700
