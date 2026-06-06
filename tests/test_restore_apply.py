"""Tests for boot-time restore apply (Phase 3 restore slice 3)."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path


def _state_db_bytes(marker: str) -> bytes:
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE records (jid TEXT)")
        conn.execute("INSERT INTO records VALUES (?)", (marker,))
        conn.commit()
        conn.close()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)


def _build_restore_zip(path, *, state_marker="restored"):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("state_db.sqlite", _state_db_bytes(state_marker))
        zf.writestr(
            "sidecars/abcdef123456/verification.json", b'{"jid":"abcdef123456"}'
        )
        zf.writestr("archive/blocked/blk.json", b'{"x":1}')
        zf.writestr("logs/decisions.jsonl", b'{"decision":"ACCEPT"}\n')


def _targets(tmp_path):
    import restore

    live = tmp_path / "live"
    (live / "output").mkdir(parents=True)
    (live / "blocked").mkdir(parents=True)
    return restore.RestoreTargets(
        state_db_path=live / "state.db",
        output_base=live / "output",
        archive_dirs={
            "blocked": live / "blocked",
            "discarded": live / "discarded",
            "expired": live / "expired",
        },
        log_paths={
            "decisions.jsonl": live / "decisions.jsonl",
            "release_switch_audit.jsonl": live / "audit.jsonl",
        },
    )


def _stage(tmp_path, *, state_marker="restored"):
    """Stage a restore via the real staging path and return (staging_dir, zip)."""
    import restore

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    staging_dir = tmp_path / "staging"
    backup_zip = backup_dir / "mintarr-backup.zip"
    _build_restore_zip(backup_zip, state_marker=state_marker)
    restore.stage_restore_from_path(
        backup_zip,
        allowed_roots=(backup_dir,),
        staging_dir=staging_dir,
    )
    return staging_dir


def _safety_builder():
    def _build():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("state_db.sqlite", b"SQLite format 3\x00")
        return buf.getvalue()

    return _build


def _apply(tmp_path, staging_dir, targets, *, enabled=True, build_safety=None):
    import restore

    return restore.apply_pending_restore(
        staging_dir=staging_dir,
        targets=targets,
        safety_backup_dir=tmp_path / "safety",
        build_safety_zip=build_safety or _safety_builder(),
        enabled=enabled,
    )


def test_apply_no_marker_is_noop(tmp_path):
    targets = _targets(tmp_path)
    result = _apply(tmp_path, tmp_path / "empty-staging", targets)
    assert result.outcome == "no_pending"
    assert result.start_workers is True


def test_apply_happy_path_restores_state_and_keeps_audio(tmp_path):
    targets = _targets(tmp_path)
    # Current state to be overwritten, plus a stale WAL and untouched audio.
    targets.state_db_path.write_bytes(_state_db_bytes("old"))
    (targets.state_db_path.parent / "state.db-wal").write_bytes(b"stale-wal")
    audio = targets.output_base / "abcdef123456" / "track.flac"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"AUDIO")

    staging_dir = _stage(tmp_path, state_marker="restored")
    result = _apply(tmp_path, staging_dir, targets)

    assert result.outcome == "applied"
    assert result.start_workers is True
    # state_db replaced with restored content
    conn = sqlite3.connect(str(targets.state_db_path))
    rows = [r[0] for r in conn.execute("SELECT jid FROM records")]
    conn.close()
    assert rows == ["restored"]
    # stale WAL removed
    assert not (targets.state_db_path.parent / "state.db-wal").exists()
    # sidecar + archive + log restored
    assert (targets.output_base / "abcdef123456" / "verification.json").exists()
    assert (targets.archive_dirs["blocked"] / "blk.json").exists()
    assert targets.log_paths["decisions.jsonl"].exists()
    # audio untouched
    assert audio.read_bytes() == b"AUDIO"
    # marker consumed, status applied, safety backup written
    assert not (staging_dir / "restore_request.json").exists()
    status = json.loads((staging_dir / "restore_status.json").read_text())
    assert status["state"] == "applied"
    assert (tmp_path / "safety" / result.restore_id / "pre-restore-state.zip").exists()


def test_apply_disabled_leaves_marker_and_starts_workers(tmp_path):
    targets = _targets(tmp_path)
    staging_dir = _stage(tmp_path)
    result = _apply(tmp_path, staging_dir, targets, enabled=False)
    assert result.outcome == "disabled_skip"
    assert result.start_workers is True
    assert (staging_dir / "restore_request.json").exists()


def test_apply_interrupted_marker_fails_closed(tmp_path):
    targets = _targets(tmp_path)
    staging_dir = _stage(tmp_path)
    # Simulate a prior boot that began applying and never finished.
    marker = staging_dir / "restore_request.json"
    data = json.loads(marker.read_text())
    data["state"] = "applying"
    marker.write_text(json.dumps(data))

    result = _apply(tmp_path, staging_dir, targets)
    assert result.outcome == "failed_partial"
    assert result.start_workers is False
    status = json.loads((staging_dir / "restore_status.json").read_text())
    assert status["state"] == "failed_partial"


def test_apply_safety_backup_failure_aborts_before_replacement(tmp_path):
    targets = _targets(tmp_path)
    targets.state_db_path.write_bytes(_state_db_bytes("old"))
    staging_dir = _stage(tmp_path)

    def _boom():
        raise RuntimeError("snapshot failed")

    result = _apply(tmp_path, staging_dir, targets, build_safety=_boom)
    assert result.outcome == "failed_preflight"
    assert result.start_workers is True
    # Current state untouched
    conn = sqlite3.connect(str(targets.state_db_path))
    rows = [r[0] for r in conn.execute("SELECT jid FROM records")]
    conn.close()
    assert rows == ["old"]
    # marker consumed, no half-applied state
    assert not (staging_dir / "restore_request.json").exists()


def test_apply_failure_mid_replacement_fails_closed_and_keeps_marker(
    tmp_path, monkeypatch
):
    import restore

    targets = _targets(tmp_path)
    targets.state_db_path.write_bytes(_state_db_bytes("old"))
    staging_dir = _stage(tmp_path)

    # Force the replacement step to blow up after the applying marker is set.
    def _boom(*a, **k):
        raise RuntimeError("disk full mid-replace")

    monkeypatch.setattr(restore, "_apply_entries", _boom)

    result = _apply(tmp_path, staging_dir, targets)
    assert result.outcome == "failed_partial"
    assert result.start_workers is False
    # marker left in applying so the next boot also fails closed
    marker = json.loads((staging_dir / "restore_request.json").read_text())
    assert marker["state"] == "applying"
    status = json.loads((staging_dir / "restore_status.json").read_text())
    assert status["state"] == "failed_partial"


def test_apply_status_write_failure_after_commit_still_fails_closed(
    tmp_path, monkeypatch
):
    # Codex #91 finding: if the status write fails *after* marker=applying, the
    # exception must not escape and flip the server wrapper to start_workers.
    import restore

    targets = _targets(tmp_path)
    targets.state_db_path.write_bytes(_state_db_bytes("old"))
    staging_dir = _stage(tmp_path)

    def _boom_apply(*a, **k):
        raise RuntimeError("disk full mid-replace")

    def _boom_status(*a, **k):
        raise OSError("cannot write status file")

    monkeypatch.setattr(restore, "_apply_entries", _boom_apply)
    monkeypatch.setattr(restore, "_write_status_file", _boom_status)

    # Must not raise, and must fail closed.
    result = _apply(tmp_path, staging_dir, targets)
    assert result.outcome == "failed_partial"
    assert result.start_workers is False
    # marker stays applying so the next boot also fails closed
    marker = json.loads((staging_dir / "restore_request.json").read_text())
    assert marker["state"] == "applying"


def test_apply_revalidation_failure_is_preflight(tmp_path):
    targets = _targets(tmp_path)
    targets.state_db_path.write_bytes(_state_db_bytes("old"))
    staging_dir = _stage(tmp_path)
    # Corrupt the staged zip after staging.
    data = json.loads((staging_dir / "restore_request.json").read_text())
    Path(data["staged_zip"]).write_bytes(b"not a zip")

    result = _apply(tmp_path, staging_dir, targets)
    assert result.outcome == "failed_preflight"
    assert result.start_workers is True
    conn = sqlite3.connect(str(targets.state_db_path))
    rows = [r[0] for r in conn.execute("SELECT jid FROM records")]
    conn.close()
    assert rows == ["old"]
