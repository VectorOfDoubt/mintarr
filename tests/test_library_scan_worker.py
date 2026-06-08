"""Tests for F5.4 slice 5c library scan worker."""

from __future__ import annotations

import library_evidence
import library_scan_worker
import state_db


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _fresh_db(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    return db_file


def test_fetch_lidarr_trackfiles_snapshots_album_trackfiles():
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        if url == "http://lidarr/api/v1/album":
            return _Resp([{"id": 10}, {"id": 20}])
        if url == "http://lidarr/api/v1/trackfile?albumId=10":
            return _Resp([{"id": 1, "path": "/music/a.flac"}])
        if url == "http://lidarr/api/v1/trackfile?albumId=20":
            return _Resp([{"id": 2, "albumId": 99, "path": "/music/b.flac"}])
        raise AssertionError(url)

    rows = library_scan_worker._fetch_lidarr_trackfiles(
        api="http://lidarr/api/v1", key="k", get=fake_get
    )

    assert rows == [
        {"id": 1, "path": "/music/a.flac", "albumId": 10},
        {"id": 2, "albumId": 99, "path": "/music/b.flac"},
    ]
    assert calls[0] == (
        "http://lidarr/api/v1/album",
        {"X-Api-Key": "k"},
        30,
    )


def _claim_scan_job(run):
    return state_db.dequeue_next_job(
        worker_id="scan-w", include_types=(state_db.LIBRARY_SCAN_JOB_TYPE,)
    )


def _patch_measure(monkeypatch, *, stat=("/lib/a.flac", 10, 1.0), status="measured"):
    monkeypatch.setattr(library_evidence, "stat_for_freshness", lambda path: stat)
    monkeypatch.setattr(
        library_evidence,
        "measure_trackfile",
        lambda path: library_evidence.TrackMeasurement(
            status=status,
            codec="flac" if status == "measured" else None,
            sample_rate=44100 if status == "measured" else None,
            bit_depth=16 if status == "measured" else None,
            lossless=True if status == "measured" else None,
            integrity_ok=True if status == "measured" else None,
            reason=None if status == "measured" else "unmapped",
        ),
    )


def test_library_scan_job_measures_trackfiles_and_completes(monkeypatch, tmp_path):
    _fresh_db(tmp_path)
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: [
            {"id": 1, "albumId": 10, "path": "/music/a.flac"},
            {"id": 2, "albumId": 10, "path": "/music/b.flac"},
        ],
    )
    _patch_measure(monkeypatch)

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    final_run = state_db.get_library_scan_run(run["id"])
    final_job = state_db.get_job(run["worker_job_id"])
    assert final_run["state"] == "completed"
    assert final_run["total_items"] == 2
    assert final_run["processed_items"] == 2
    assert final_run["measured_items"] == 2
    assert final_job["state"] == "completed"
    assert final_job["result_state"] == "library_scan_completed"
    assert state_db.get_library_evidence(1)["status"] == "measured"
    total, items = state_db.list_library_scan_items(run["id"])
    assert total == 2
    assert {item["state"] for item in items} == {"measured"}


def test_library_scan_job_skips_fresh_evidence(monkeypatch, tmp_path):
    _fresh_db(tmp_path)
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None
    path = "/lib/a.flac"
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 7,
            "album_id": 11,
            "path": path,
            "size": 99,
            "mtime": 5.0,
            "status": "measured",
            "sensor_version": library_evidence.SENSOR_VERSION,
        }
    )
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: [{"id": 7, "albumId": 11, "path": "/music/a.flac"}],
    )
    calls = {"measure": 0}
    monkeypatch.setattr(
        library_evidence,
        "stat_for_freshness",
        lambda path: ("/lib/a.flac", 99, 5.0),
    )
    monkeypatch.setattr(
        library_evidence,
        "measure_trackfile",
        lambda path: calls.__setitem__("measure", calls["measure"] + 1),
    )

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    final_run = state_db.get_library_scan_run(run["id"])
    assert final_run["fresh_items"] == 1
    assert calls["measure"] == 0
    assert state_db.list_library_scan_items(run["id"])[1][0]["state"] == "fresh"


def test_queued_cancel_becomes_terminal_at_claim(monkeypatch, tmp_path):
    _fresh_db(tmp_path)
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None
    state_db.request_library_scan_cancel(run["id"])
    calls = {"fetch": 0}
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: calls.__setitem__("fetch", calls["fetch"] + 1),
    )

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    final_run = state_db.get_library_scan_run(run["id"])
    final_job = state_db.get_job(run["worker_job_id"])
    assert final_run["state"] == "cancelled"
    assert final_job["state"] == "cancelled"
    assert calls["fetch"] == 0


def test_scan_failure_syncs_run_and_job(monkeypatch, tmp_path):
    _fresh_db(tmp_path)
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: (_ for _ in ()).throw(RuntimeError("lidarr timeout")),
    )

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    final_run = state_db.get_library_scan_run(run["id"])
    final_job = state_db.get_job(run["worker_job_id"])
    assert final_run["state"] == "failed"
    assert "lidarr timeout" in final_run["last_error"]
    assert final_job["state"] == "failed"
    assert final_job["result_state"] == "library_scan_failed"


def test_scan_yields_while_import_work_is_active(monkeypatch, tmp_path):
    _fresh_db(tmp_path)
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: [{"id": 1, "albumId": 10, "path": "/music/a.flac"}],
    )
    active = [True, False]
    monkeypatch.setattr(
        library_scan_worker,
        "_is_import_work_active",
        lambda: active.pop(0) if active else False,
    )
    monkeypatch.setattr(
        library_scan_worker._shutdown_event, "wait", lambda timeout: None
    )
    heartbeat_calls = []
    original_heartbeat = state_db.heartbeat_job

    def tracked_heartbeat(job_id, *, lease_sec=state_db.DEFAULT_LEASE_SEC):
        heartbeat_calls.append(job_id)
        original_heartbeat(job_id, lease_sec=lease_sec)

    monkeypatch.setattr(state_db, "heartbeat_job", tracked_heartbeat)
    _patch_measure(monkeypatch)

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    final_job = state_db.get_job(run["worker_job_id"])
    progress = final_job["progress_json"]
    assert final_job["state"] == "completed"
    assert "library_scan" in progress
    assert run["worker_job_id"] in heartbeat_calls
