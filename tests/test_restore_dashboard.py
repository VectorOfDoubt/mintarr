"""Tests for the System → Backup & Restore dashboard panel (restore slice 4)."""

from __future__ import annotations

import zipfile

import server

VALID_KEY = "tidalhires-test-api-key"


def _client():
    return server.app.test_client()


def _valid_zip(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("logs/decisions.jsonl", b'{"ok": true}\n')


def _stage(tmp_path, monkeypatch):
    import restore

    backup_dir = tmp_path / "backups"
    staging_dir = tmp_path / "staging"
    backup_dir.mkdir()
    backup_zip = backup_dir / "mintarr-backup.zip"
    _valid_zip(backup_zip)
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(staging_dir))
    restore.stage_restore_from_path(
        backup_zip, allowed_roots=(backup_dir,), staging_dir=staging_dir
    )
    return staging_dir


def test_restore_partial_requires_apikey():
    assert _client().get("/dashboard/v1/restore/partial").status_code == 401


def test_restore_partial_shows_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("MINTARR_RESTORE_ENABLED", raising=False)
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(tmp_path / "staging"))
    resp = _client().get(f"/dashboard/v1/restore/partial?apikey={VALID_KEY}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "disabled" in body
    assert "MINTARR_RESTORE_ENABLED=true" in body


def test_restore_partial_shows_pending_with_cancel(monkeypatch, tmp_path):
    _stage(tmp_path, monkeypatch)
    resp = _client().get(f"/dashboard/v1/restore/partial?apikey={VALID_KEY}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Restart Mintarr to apply" in body
    assert "Cancel staged restore" in body
    assert "/dashboard/v1/restore/cancel" in body


def test_restore_cancel_removes_marker_and_rerenders(monkeypatch, tmp_path):
    staging_dir = _stage(tmp_path, monkeypatch)
    resp = _client().post(f"/dashboard/v1/restore/cancel?apikey={VALID_KEY}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Staged restore cancelled" in body
    assert "No restore staged" in body
    assert not (staging_dir / "restore_request.json").exists()


def test_restore_cancel_requires_apikey():
    assert _client().post("/dashboard/v1/restore/cancel").status_code == 401


def test_restore_cancel_when_disabled_is_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("MINTARR_RESTORE_ENABLED", raising=False)
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(tmp_path / "staging"))
    resp = _client().post(f"/dashboard/v1/restore/cancel?apikey={VALID_KEY}")
    assert resp.status_code == 200
    assert "Restore is disabled" in resp.get_data(as_text=True)
