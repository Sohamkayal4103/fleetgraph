"""Stage 13D content-addressed artifact-store tests (Part Q — artifact store).

Pure filesystem, no network/DB. Verifies safe import + streaming hash, content dedup, path
traversal / symlink / special-file rejection, atomic commit, per-artifact/total/free-space
quotas, chunked receive with idempotent duplicate chunks, digest/size verification (mismatch
quarantines and never publishes), pin-aware GC, and partial cleanup. Bytes never touch SQLite.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from aithernet.artifacts.store import ArtifactStore, ArtifactStoreError


def _store(tmp_path, **over):
    opts = {"total_quota_bytes": 10_000_000, "max_artifact_bytes": 5_000_000,
            "minimum_free_bytes": 0}
    opts.update(over)
    return ArtifactStore(str(tmp_path / "store"), **opts)


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_import_hashes_and_addresses_content(tmp_path):
    st = _store(tmp_path)
    data = b"RF" * 100_000
    src = _write(tmp_path, "cap.bin", data)
    obj = st.import_file(src, expected_digest=_digest(data), expected_size=len(data))
    assert obj.digest == _digest(data)
    assert obj.size_bytes == len(data)
    assert obj.deduplicated is False
    assert st.has_object(obj.digest)
    assert os.path.isfile(src)  # the source is never moved/mutated


def test_content_deduplication(tmp_path):
    st = _store(tmp_path)
    data = b"same-bytes" * 1000
    a = st.import_file(_write(tmp_path, "a.bin", data))
    b = st.import_file(_write(tmp_path, "b.bin", data))
    assert a.digest == b.digest
    assert b.deduplicated is True


def test_symlink_source_rejected(tmp_path):
    st = _store(tmp_path)
    target = _write(tmp_path, "real.bin", b"x" * 10)
    link = str(tmp_path / "link.bin")
    os.symlink(target, link)
    with pytest.raises(ArtifactStoreError) as e:
        st.import_file(link)
    assert e.value.code == "unsafe_source"


def test_special_file_rejected(tmp_path):
    st = _store(tmp_path)
    with pytest.raises(ArtifactStoreError) as e:
        st.import_file("/dev/null")
    assert e.value.code == "unsafe_source"


def test_traversal_rejected(tmp_path):
    st = _store(tmp_path)
    with pytest.raises(ArtifactStoreError):
        st.partial_abs("../escape")
    with pytest.raises(ArtifactStoreError):
        st.object_abs("sha256:" + "z" * 64)  # malformed digest


def test_per_artifact_limit_enforced(tmp_path):
    st = _store(tmp_path, max_artifact_bytes=1000)
    with pytest.raises(ArtifactStoreError) as e:
        st.import_file(_write(tmp_path, "big.bin", b"x" * 2000))
    assert e.value.code == "too_large"


def test_total_quota_enforced(tmp_path):
    st = _store(tmp_path, total_quota_bytes=1500, max_artifact_bytes=10_000)
    st.import_file(_write(tmp_path, "a.bin", b"a" * 1000))
    with pytest.raises(ArtifactStoreError) as e:
        st.import_file(_write(tmp_path, "b.bin", b"b" * 1000))
    assert e.value.code == "store_quota"


def test_minimum_free_space_blocks(tmp_path):
    # An impossibly high free-space floor blocks any new import.
    st = _store(tmp_path, minimum_free_bytes=10**18)
    with pytest.raises(ArtifactStoreError) as e:
        st.import_file(_write(tmp_path, "a.bin", b"a" * 100))
    assert e.value.code == "low_disk"


def test_chunked_receive_verify_and_commit(tmp_path):
    st = _store(tmp_path)
    data = b"chunked-rf-capture-" * 50_000
    dig = _digest(data)
    tid = "t-1"
    half = len(data) // 2
    st.append_chunk(tid, offset=0, data=data[:half], max_total_bytes=len(data))
    st.append_chunk(tid, offset=0, data=data[:half], max_total_bytes=len(data))  # duplicate chunk
    assert st.partial_size(tid) == half
    st.append_chunk(tid, offset=half, data=data[half:], max_total_bytes=len(data))
    obj = st.verify_and_commit_partial(tid, expected_digest=dig, expected_size=len(data))
    assert obj.digest == dig and st.has_object(dig)


def test_chunk_overflow_rejected(tmp_path):
    st = _store(tmp_path)
    with pytest.raises(ArtifactStoreError) as e:
        st.append_chunk("t-2", offset=0, data=b"x" * 100, max_total_bytes=10)
    assert e.value.code == "size_overflow"


def test_digest_mismatch_quarantines_and_never_publishes(tmp_path):
    st = _store(tmp_path)
    st.append_chunk("t-bad", offset=0, data=b"WRONG", max_total_bytes=5)
    bad = "sha256:" + "0" * 64
    with pytest.raises(ArtifactStoreError) as e:
        st.verify_and_commit_partial("t-bad", expected_digest=bad, expected_size=5)
    assert e.value.code == "digest_mismatch"
    assert not st.has_object(bad)


def test_size_mismatch_quarantines(tmp_path):
    st = _store(tmp_path)
    data = b"hello"
    st.append_chunk("t-sz", offset=0, data=data, max_total_bytes=10)
    with pytest.raises(ArtifactStoreError) as e:
        st.verify_and_commit_partial("t-sz", expected_digest=_digest(data), expected_size=10)
    assert e.value.code == "size_mismatch"


def test_range_read_within_object(tmp_path):
    st = _store(tmp_path)
    data = bytes(range(256)) * 100
    obj = st.import_file(_write(tmp_path, "r.bin", data))
    assert st.read_range(obj.digest, offset=10, length=5) == data[10:15]
    with pytest.raises(ArtifactStoreError) as e:
        st.read_range(obj.digest, offset=10**9, length=5)
    assert e.value.code == "bad_range"


def test_gc_respects_pins(tmp_path):
    st = _store(tmp_path)
    keep = st.import_file(_write(tmp_path, "keep.bin", b"keep" * 500))
    drop = st.import_file(_write(tmp_path, "drop.bin", b"drop" * 500))
    dry = st.gc(pinned_digests={keep.digest, drop.digest}, dry_run=True)
    assert dry["removed_objects"] == 0
    applied = st.gc(pinned_digests={keep.digest}, dry_run=False)
    assert applied["removed_objects"] == 1
    assert st.has_object(keep.digest) and not st.has_object(drop.digest)


def test_partial_cleanup_expiry(tmp_path):
    st = _store(tmp_path)
    st.append_chunk("old", offset=0, data=b"x" * 10, max_total_bytes=10)
    st.append_chunk("active", offset=0, data=b"y" * 10, max_total_bytes=10)
    # now far in the future, keep the 'active' transfer.
    removed = st.cleanup_partials(older_than_seconds=0, now_ts=10**12, keep_transfer_ids={"active"})
    assert removed == 1
    assert st.partial_size("active") == 10


def test_status_has_no_absolute_paths(tmp_path):
    st = _store(tmp_path)
    status = st.status()
    blob = " ".join(str(v) for v in status.values())
    assert str(tmp_path) not in blob
    assert "used_bytes" in status and "free_disk_bytes" in status
