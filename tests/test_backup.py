"""Tests for the read-only state backup (Phase 3 slice 6 — export half)."""

from __future__ import annotations

import io
import os
import sqlite3
import time
import zipfile

import pytest

import server

VALID_KEY = "tidalhires-test-api-key"


def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def test_build_backup_zip_includes_state_but_not_audio(tmp_path):
    import backup

    db = tmp_path / "state.db"
    _make_db(db)

    output = tmp_path / "output"
    (output / "job-1").mkdir(parents=True)
    (output / "job-1" / "verification.json").write_text("{}")
    # Audio must never be captured.
    (output / "job-1" / "track.flac").write_bytes(b"AUDIO")

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "rec.json").write_text("{}")

    log = tmp_path / "decisions.jsonl"
    log.write_text('{"decision":"ACCEPT"}\n')

    data = backup.build_backup_zip(
        state_db_path=db,
        output_base=output,
        archive_dirs={"blocked": blocked},
        log_files={"decisions.jsonl": log},
    )

    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert "state_db.sqlite" in names
    assert "sidecars/job-1/verification.json" in names
    assert "archive/blocked/rec.json" in names
    assert "logs/decisions.jsonl" in names
    assert not any(n.endswith(".flac") for n in names)
    # The captured state_db is a real, readable SQLite image (online-backup API).
    assert zf.read("state_db.sqlite").startswith(b"SQLite format 3\x00")


def test_build_backup_zip_skips_missing_sources(tmp_path):
    import backup

    data = backup.build_backup_zip(
        state_db_path=tmp_path / "nope.db",
        output_base=tmp_path / "nope",
        archive_dirs={"blocked": tmp_path / "nope-dir"},
        log_files={"decisions.jsonl": tmp_path / "nope.jsonl"},
    )
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == []


def test_backup_endpoint_requires_apikey():
    client = server.app.test_client()
    assert client.get("/backup").status_code == 401


def test_backup_endpoint_streams_zip():
    client = server.app.test_client()
    resp = client.get(f"/backup?apikey={VALID_KEY}")
    assert resp.status_code == 200
    assert resp.content_type == "application/zip"
    assert "attachment; filename=mintarr-backup-" in resp.headers["Content-Disposition"]
    # Body is a valid zip even when state dirs are empty in tests.
    zipfile.ZipFile(io.BytesIO(resp.get_data()))


def test_write_backup_file_writes_atomically_and_prunes_retention(tmp_path):
    import backup

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    keep_foreign = backup_dir / "operator-note.zip"
    keep_foreign.write_text("not managed by Mintarr")
    old_a = backup_dir / "mintarr-backup-20260101-000000.zip"
    old_b = backup_dir / "mintarr-backup-20260102-000000.zip"
    old_a.write_bytes(b"old-a")
    old_b.write_bytes(b"old-b")
    os_time = time.time()
    # Make deterministic age ordering independent of filesystem timestamp
    # resolution: the new backup should be newest, old_b second, old_a pruned.
    old_a_stat = os_time - 20
    old_b_stat = os_time - 10

    os.utime(old_a, (old_a_stat, old_a_stat))
    os.utime(old_b, (old_b_stat, old_b_stat))

    def _build_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("state_db.sqlite", b"SQLite format 3\x00")
        return buf.getvalue()

    written = backup.write_backup_file(
        build_zip=_build_zip,
        backup_dir=backup_dir,
        retention=2,
        now=1770000000,
    )

    assert written.name == "mintarr-backup-20260202-024000.zip"
    assert written.exists()
    assert zipfile.ZipFile(written).namelist() == ["state_db.sqlite"]
    assert old_b.exists()
    assert not old_a.exists()
    assert keep_foreign.exists()
    assert not list(backup_dir.glob("*.tmp"))


def test_write_backup_file_removes_tmp_when_builder_fails(tmp_path):
    import backup

    def _fail():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        backup.write_backup_file(
            build_zip=_fail,
            backup_dir=tmp_path,
            retention=30,
            now=1770000000,
        )

    assert not list(tmp_path.glob("*.tmp"))
