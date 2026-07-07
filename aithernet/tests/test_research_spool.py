"""beta.10 Part 3: the local append-only research spool.

Proves the canonical directory tree is created (0700), clean records land content-addressed in
raw/ and dedup, secret-bearing records are diverted to quarantine/ (never raw, never uploaded),
and status() reports secret-free counts.
"""

from __future__ import annotations

from aithernet.data.research_spool import SPOOL_SUBDIRS, ResearchSpool


def test_ensure_layout_creates_all_subdirs(tmp_path):
    spool = ResearchSpool(tmp_path / "research")
    rec = spool.ensure_layout()
    assert set(rec["subdirs"]) == set(SPOOL_SUBDIRS)
    for name in SPOOL_SUBDIRS:
        d = spool.root / name
        assert d.is_dir()
        assert oct(d.stat().st_mode)[-3:] == "700"
    # idempotent
    again = spool.ensure_layout()
    assert again["created"] == []


def test_clean_record_lands_in_raw_and_dedupes(tmp_path):
    spool = ResearchSpool(tmp_path / "research")
    record = {"mission_id": "m1", "objective": "scan a band", "outcome": "completed"}
    r1 = spool.write_raw_record(record)
    assert r1["dest"] == "raw" and not r1["quarantined"]
    assert r1["digest"].startswith("sha256:")
    # same content dedupes to the same path (append-only, content-addressed)
    r2 = spool.write_raw_record(dict(record))
    assert r2["path"] == r1["path"]
    assert spool.counts()["raw"] == 1


def test_secret_bearing_record_is_quarantined(tmp_path):
    spool = ResearchSpool(tmp_path / "research")
    # The residual-secret check is a fail-safe for anything redaction MISSES. Simulate a record
    # that still carries a credential after redaction; the spool must divert it to quarantine and
    # keep it out of raw/ (so it is never normalized or uploaded).
    from aithernet.data.redaction import RedactionResult

    class _LeakyRedactor:
        def redact(self, payload):
            return RedactionResult(redacted={"mission_id": "m2",
                                             "leak": "Authorization: Bearer abcdef1234567890"})

    spool._redactor = _LeakyRedactor()
    r = spool.write_raw_record({"mission_id": "m2"})
    assert r["dest"] == "quarantine" and r["quarantined"] is True
    assert spool.counts()["quarantine"] == 1
    assert spool.counts()["raw"] == 0


def test_status_is_secret_free_and_reports_counts(tmp_path):
    spool = ResearchSpool(tmp_path / "research")
    spool.write_raw_record({"mission_id": "m1", "objective": "x"})
    st = spool.status()
    assert st["exists"] and st["healthy"]
    assert st["pending_raw"] == 1 and st["quarantined"] == 0
    assert st["schema_version"] == "aithernet.dataset.v1"
    # status carries counts only, never record bodies
    assert "objective" not in str(st)
