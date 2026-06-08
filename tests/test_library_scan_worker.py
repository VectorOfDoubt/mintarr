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


def test_spectral_scan_measures_stale_spectral_only(monkeypatch, tmp_path):
    _fresh_db(tmp_path)
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    monkeypatch.setenv("MINTARR_LIBRARY_BACKGROUND_SPECTRAL", "true")
    run = state_db.enqueue_library_scan(mode="spectral_missing")
    assert run is not None
    path = "/lib/Artist/01.flac"
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 80,
            "album_id": 800,
            "path": path,
            "size": 10,
            "mtime": 1.0,
            "status": "measured",
            "lossless": True,
            "integrity_ok": True,
            "sensor_version": library_evidence.SENSOR_VERSION,
        }
    )
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: [{"id": 80, "albumId": 800, "path": "/music/Artist/01.flac"}],
    )
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda row: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda row: False)
    calls = {"spectral": 0}

    def _spectral(path):
        calls["spectral"] += 1
        return library_evidence.SpectralMeasurement(
            status="measured", authentic=False, verdict="FAKE"
        )

    monkeypatch.setattr(library_evidence, "measure_trackfile_spectral", _spectral)

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    row = state_db.get_library_evidence(80)
    final_run = state_db.get_library_scan_run(run["id"])
    assert calls["spectral"] == 1
    assert row["spectral_status"] == "measured"
    assert row["authentic"] == 0
    assert row["spectral_verdict"] == "FAKE"
    assert final_run["measured_items"] == 1
    assert state_db.get_library_scan_item(run["id"], 80)["state"] == "spectral_measured"


def test_spectral_scan_skips_when_cheap_evidence_not_fresh(monkeypatch, tmp_path):
    _fresh_db(tmp_path)
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    monkeypatch.setenv("MINTARR_LIBRARY_BACKGROUND_SPECTRAL", "true")
    run = state_db.enqueue_library_scan(mode="spectral_missing")
    assert run is not None
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 81,
            "album_id": 801,
            "path": "/lib/Artist/02.flac",
            "status": "measured",
            "sensor_version": "old",
        }
    )
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: [{"id": 81, "albumId": 801, "path": "/music/Artist/02.flac"}],
    )
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda row: False)
    calls = {"spectral": 0}
    monkeypatch.setattr(
        library_evidence,
        "measure_trackfile_spectral",
        lambda path: calls.__setitem__("spectral", calls["spectral"] + 1),
    )

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    assert calls["spectral"] == 0
    assert state_db.get_library_scan_item(run["id"], 81)["state"] == "spectral_skipped"


def test_spectral_scan_uses_ledger_to_skip_completed_item(monkeypatch, tmp_path):
    _fresh_db(tmp_path)
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    monkeypatch.setenv("MINTARR_LIBRARY_BACKGROUND_SPECTRAL", "true")
    run = state_db.enqueue_library_scan(mode="spectral_missing")
    assert run is not None
    state_db.upsert_library_scan_item(
        run["id"], 82, album_id=802, state="spectral_measured"
    )
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 82,
            "album_id": 802,
            "path": "/lib/Artist/03.flac",
            "status": "measured",
            "sensor_version": library_evidence.SENSOR_VERSION,
        }
    )
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: [{"id": 82, "albumId": 802, "path": "/music/Artist/03.flac"}],
    )
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda row: True)
    calls = {"spectral": 0}
    monkeypatch.setattr(
        library_evidence,
        "measure_trackfile_spectral",
        lambda path: calls.__setitem__("spectral", calls["spectral"] + 1),
    )

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    assert calls["spectral"] == 0
    assert state_db.get_library_scan_run(run["id"])["fresh_items"] == 1


def test_spectral_scan_waits_before_detective_when_import_active(monkeypatch, tmp_path):
    _fresh_db(tmp_path)
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    monkeypatch.setenv("MINTARR_LIBRARY_BACKGROUND_SPECTRAL", "true")
    run = state_db.enqueue_library_scan(mode="spectral_missing")
    assert run is not None
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 83,
            "album_id": 803,
            "path": "/lib/Artist/04.flac",
            "status": "measured",
            "sensor_version": library_evidence.SENSOR_VERSION,
        }
    )
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: [{"id": 83, "albumId": 803, "path": "/music/Artist/04.flac"}],
    )
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda row: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda row: False)
    # Two wait sites in spectral mode: before the item and immediately before
    # Detective. The second one sees import work and must drain before measuring.
    active = [False, True, False]
    monkeypatch.setattr(
        library_scan_worker,
        "_is_import_work_active",
        lambda: active.pop(0) if active else False,
    )
    monkeypatch.setattr(
        library_scan_worker._shutdown_event, "wait", lambda timeout: None
    )
    order = []

    def _spectral(path):
        order.append("spectral")
        return library_evidence.SpectralMeasurement(status="unmeasured", reason="x")

    def _heartbeat(job_id, *, lease_sec=state_db.DEFAULT_LEASE_SEC):
        order.append("heartbeat")
        return True

    monkeypatch.setattr(state_db, "heartbeat_job", _heartbeat)
    monkeypatch.setattr(library_evidence, "measure_trackfile_spectral", _spectral)

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    assert "heartbeat" in order
    assert order.index("heartbeat") < order.index("spectral")


def test_spectral_scan_extends_lease_before_detective(monkeypatch, tmp_path):
    _fresh_db(tmp_path)
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    monkeypatch.setenv("MINTARR_LIBRARY_BACKGROUND_SPECTRAL", "true")
    run = state_db.enqueue_library_scan(mode="spectral_missing")
    assert run is not None
    state_db.upsert_library_evidence(
        {
            "trackfile_id": 84,
            "album_id": 804,
            "path": "/lib/Artist/05.flac",
            "status": "measured",
            "sensor_version": library_evidence.SENSOR_VERSION,
        }
    )
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: [{"id": 84, "albumId": 804, "path": "/music/Artist/05.flac"}],
    )
    monkeypatch.setattr(library_evidence, "is_measured_row_fresh", lambda row: True)
    monkeypatch.setattr(library_evidence, "is_spectral_row_fresh", lambda row: False)
    calls = []

    def _heartbeat(job_id, *, lease_sec=state_db.DEFAULT_LEASE_SEC):
        calls.append(("heartbeat", lease_sec))
        return True

    def _spectral(path):
        calls.append(("spectral", None))
        return library_evidence.SpectralMeasurement(status="unmeasured", reason="x")

    monkeypatch.setattr(state_db, "heartbeat_job", _heartbeat)
    monkeypatch.setattr(library_evidence, "measure_trackfile_spectral", _spectral)

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    extended = [
        index
        for index, call in enumerate(calls)
        if call[0] == "heartbeat" and call[1] > state_db.DEFAULT_LEASE_SEC
    ]
    spectral_index = next(
        index for index, call in enumerate(calls) if call[0] == "spectral"
    )
    assert extended
    assert max(extended) < spectral_index
    assert calls[extended[-1]][1] > 900


def test_spectral_scan_worker_rejects_when_background_flag_disabled(
    monkeypatch, tmp_path
):
    _fresh_db(tmp_path)
    monkeypatch.setenv("MINTARR_LIBRARY_SPECTRAL", "true")
    monkeypatch.delenv("MINTARR_LIBRARY_BACKGROUND_SPECTRAL", raising=False)
    run = state_db.enqueue_library_scan(mode="spectral_missing")
    assert run is not None
    monkeypatch.setattr(
        library_scan_worker,
        "_fetch_lidarr_trackfiles",
        lambda: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )

    library_scan_worker._execute_library_scan_job(_claim_scan_job(run))

    final_run = state_db.get_library_scan_run(run["id"])
    final_job = state_db.get_job(run["worker_job_id"])
    assert final_run["state"] == "failed"
    assert "background spectral scan disabled" in final_run["last_error"]
    assert final_job["state"] == "failed"
