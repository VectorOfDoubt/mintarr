"""Tests for restore staging endpoints (no boot-time apply)."""

from __future__ import annotations

import io
import json
import os
import zipfile

import server

VALID_KEY = "tidalhires-test-api-key"


def _valid_restore_zip(path, *, entries: int = 1):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(entries):
            name = (
                "logs/decisions.jsonl" if i == 0 else "logs/release_switch_audit.jsonl"
            )
            zf.writestr(name, b'{"ok": true}\n')


def _client():
    return server.app.test_client()


def test_restore_post_requires_apikey():
    assert _client().post("/restore", json={}).status_code == 401


def test_restore_post_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MINTARR_RESTORE_ENABLED", raising=False)

    resp = _client().post(f"/restore?apikey={VALID_KEY}", json={})

    assert resp.status_code == 403
    assert resp.get_json()["error"] == "restore disabled"


def test_restore_status_reports_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("MINTARR_RESTORE_ENABLED", raising=False)
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(tmp_path / "staging"))

    resp = _client().get(f"/restore/status?apikey={VALID_KEY}")

    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is False
    assert resp.get_json()["pending"] is False


def test_restore_post_stages_backup_path(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    staging_dir = tmp_path / "staging"
    backup_dir.mkdir()
    backup_zip = backup_dir / "mintarr-backup.zip"
    _valid_restore_zip(backup_zip)
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(staging_dir))

    resp = _client().post(
        f"/restore?apikey={VALID_KEY}",
        json={"backup_path": str(backup_zip)},
    )

    assert resp.status_code == 202, resp.data
    payload = resp.get_json()
    assert payload["status"] is True
    assert payload["state"] == "staged"
    assert payload["restart_required"] is True
    assert payload["source_name"] == "mintarr-backup.zip"
    assert "staged_zip" not in payload
    marker = staging_dir / "restore_request.json"
    assert marker.is_file()
    marker_data = json.loads(marker.read_text())
    assert marker_data["state"] == "staged"
    assert marker_data["plan"]["entry_count"] == 1
    assert (staging_dir / f"{marker_data['restore_id']}.zip").is_file()


def test_restore_post_rejects_backup_path_outside_allowed_roots(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    outside_dir = tmp_path / "outside"
    staging_dir = tmp_path / "staging"
    backup_dir.mkdir()
    outside_dir.mkdir()
    backup_zip = outside_dir / "mintarr-backup.zip"
    _valid_restore_zip(backup_zip)
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(staging_dir))

    resp = _client().post(
        f"/restore?apikey={VALID_KEY}",
        json={"backup_path": str(backup_zip)},
    )

    assert resp.status_code == 400
    assert "outside allowed backup directories" in resp.get_json()["error"]
    assert not (staging_dir / "restore_request.json").exists()


def test_restore_post_rejects_nonexistent_backup_path(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    staging_dir = tmp_path / "staging"
    backup_dir.mkdir()
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(staging_dir))

    resp = _client().post(
        f"/restore?apikey={VALID_KEY}",
        json={"backup_path": str(backup_dir / "missing.zip")},
    )

    # A missing path is a bad request, not a 500.
    assert resp.status_code == 400
    assert "does not exist" in resp.get_json()["error"]
    assert not (staging_dir / "restore_request.json").exists()


def test_restore_post_rejects_backup_path_symlink_escape(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    outside_dir = tmp_path / "outside"
    staging_dir = tmp_path / "staging"
    backup_dir.mkdir()
    outside_dir.mkdir()
    outside_zip = outside_dir / "mintarr-backup.zip"
    _valid_restore_zip(outside_zip)
    symlink_path = backup_dir / "linked-backup.zip"
    os.symlink(outside_zip, symlink_path)
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(staging_dir))

    resp = _client().post(
        f"/restore?apikey={VALID_KEY}",
        json={"backup_path": str(symlink_path)},
    )

    assert resp.status_code == 400
    assert "outside allowed backup directories" in resp.get_json()["error"]
    assert not (staging_dir / "restore_request.json").exists()


def test_restore_post_rejects_second_pending_restore(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    staging_dir = tmp_path / "staging"
    backup_dir.mkdir()
    backup_zip = backup_dir / "mintarr-backup.zip"
    _valid_restore_zip(backup_zip)
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(staging_dir))

    first = _client().post(
        f"/restore?apikey={VALID_KEY}",
        json={"backup_path": str(backup_zip)},
    )
    second = _client().post(
        f"/restore?apikey={VALID_KEY}",
        json={"backup_path": str(backup_zip)},
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert "already staged" in second.get_json()["error"]


def test_restore_delete_cancels_staged_restore(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    staging_dir = tmp_path / "staging"
    backup_dir.mkdir()
    backup_zip = backup_dir / "mintarr-backup.zip"
    _valid_restore_zip(backup_zip)
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(staging_dir))
    staged = (
        _client()
        .post(
            f"/restore?apikey={VALID_KEY}",
            json={"backup_path": str(backup_zip)},
        )
        .get_json()
    )

    resp = _client().delete(f"/restore?apikey={VALID_KEY}")

    assert resp.status_code == 200
    assert resp.get_json()["state"] == "cancelled"
    assert not (staging_dir / "restore_request.json").exists()
    assert not (staging_dir / f"{staged['restore_id']}.zip").exists()


def test_restore_delete_after_apply_started_returns_409(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    staging_dir = tmp_path / "staging"
    backup_dir.mkdir()
    backup_zip = backup_dir / "mintarr-backup.zip"
    _valid_restore_zip(backup_zip)
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(staging_dir))
    _client().post(
        f"/restore?apikey={VALID_KEY}", json={"backup_path": str(backup_zip)}
    )
    # Simulate boot-time apply having begun.
    marker = staging_dir / "restore_request.json"
    data = json.loads(marker.read_text())
    data["state"] = "applying"
    marker.write_text(json.dumps(data))

    resp = _client().delete(f"/restore?apikey={VALID_KEY}")

    assert resp.status_code == 409
    assert "after apply starts" in resp.get_json()["error"]
    # marker is not removed by a rejected cancel
    assert (staging_dir / "restore_request.json").exists()


def test_restore_delete_without_staged_returns_404(monkeypatch, tmp_path):
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(tmp_path / "staging"))
    resp = _client().delete(f"/restore?apikey={VALID_KEY}")
    assert resp.status_code == 404
    assert "no staged restore" in resp.get_json()["error"]


def test_restore_post_accepts_multipart_upload(monkeypatch, tmp_path):
    staging_dir = tmp_path / "staging"
    upload_zip = tmp_path / "upload.zip"
    _valid_restore_zip(upload_zip)
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(staging_dir))

    with upload_zip.open("rb") as fh:
        resp = _client().post(
            f"/restore?apikey={VALID_KEY}",
            data={"file": (io.BytesIO(fh.read()), "uploaded.zip")},
            content_type="multipart/form-data",
        )

    assert resp.status_code == 202, resp.data
    assert resp.get_json()["source_name"] == "uploaded.zip"
    assert (staging_dir / "restore_request.json").is_file()


def test_restore_post_rejects_env_cap_violation(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    staging_dir = tmp_path / "staging"
    backup_dir.mkdir()
    backup_zip = backup_dir / "mintarr-backup.zip"
    _valid_restore_zip(backup_zip, entries=2)
    monkeypatch.setenv("MINTARR_RESTORE_ENABLED", "true")
    monkeypatch.setenv("MINTARR_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("MINTARR_RESTORE_STAGING_DIR", str(staging_dir))
    monkeypatch.setenv("MINTARR_RESTORE_MAX_ENTRIES", "1")

    resp = _client().post(
        f"/restore?apikey={VALID_KEY}",
        json={"backup_path": str(backup_zip)},
    )

    assert resp.status_code == 400
    assert "too many entries" in resp.get_json()["error"]
    assert not (staging_dir / "restore_request.json").exists()
