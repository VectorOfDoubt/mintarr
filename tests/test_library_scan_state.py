"""Tests for F5.4 slice 5b library-scan state primitives."""

from __future__ import annotations

import json

import state_db


def _fresh_db(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    return db_file


def test_library_scan_schema_uses_jobs_for_lease(tmp_path):
    _fresh_db(tmp_path)
    with state_db._connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "library_scan_runs" in tables
    assert "library_scan_items" in tables
    assert "jobs" in tables
    assert "library_scan_lease" not in tables


def test_enqueue_library_scan_creates_low_priority_f2_job(tmp_path):
    _fresh_db(tmp_path)

    run = state_db.enqueue_library_scan(mode="cheap", requested_by="operator")

    assert run is not None
    assert run["mode"] == "cheap"
    assert run["state"] == "queued"
    assert run["requested_by"] == "operator"
    assert run["worker_job_id"] is not None

    job = state_db.get_job(run["worker_job_id"])
    assert job is not None
    assert job["type"] == state_db.LIBRARY_SCAN_JOB_TYPE
    assert job["dedupe_key"] == state_db.LIBRARY_SCAN_DEDUPE_KEY
    assert job["priority"] == state_db.LIBRARY_SCAN_PRIORITY
    assert job["source_type"] == "library"
    assert job["source_id"] == "cheap"
    assert json.loads(job["payload_json"]) == {"run_id": run["id"], "mode": "cheap"}


def test_enqueue_library_scan_dedupes_active_run(tmp_path):
    _fresh_db(tmp_path)

    first = state_db.enqueue_library_scan(mode="cheap")
    second = state_db.enqueue_library_scan(mode="cheap")

    assert first is not None
    assert second is not None
    assert second["id"] == first["id"]
    total, rows = state_db.list_library_scan_runs()
    assert total == 1
    assert len(rows) == 1
    total_jobs, jobs = state_db.list_jobs(type=[state_db.LIBRARY_SCAN_JOB_TYPE])
    assert total_jobs == 1
    assert len(jobs) == 1


def test_completed_scan_allows_new_run(tmp_path):
    _fresh_db(tmp_path)

    first = state_db.enqueue_library_scan(mode="cheap")
    assert first is not None
    state_db.mark_job_completed(first["worker_job_id"], result_state="scan_completed")
    state_db.update_library_scan_run_state(first["id"], "completed")

    second = state_db.enqueue_library_scan(mode="cheap")

    assert second is not None
    assert second["id"] != first["id"]
    total, _rows = state_db.list_library_scan_runs()
    assert total == 2


def test_scan_job_priority_loses_to_import_jobs(tmp_path):
    _fresh_db(tmp_path)

    scan = state_db.enqueue_library_scan(mode="cheap")
    import_job = state_db.enqueue_job(jid="import-1", type="tidal_grab", priority=5)

    claimed = state_db.dequeue_next_job(worker_id="w1")

    assert scan is not None
    assert import_job is not None
    assert claimed is not None
    assert claimed["id"] == import_job
    scan_job = state_db.get_job(scan["worker_job_id"])
    assert scan_job["state"] == "queued"


def test_request_library_scan_cancel_marks_run_and_job(tmp_path):
    _fresh_db(tmp_path)
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None

    assert state_db.request_library_scan_cancel(run["id"]) is True

    updated = state_db.get_library_scan_run(run["id"])
    job = state_db.get_job(run["worker_job_id"])
    assert updated["cancel_requested"] == 1
    assert updated["state"] == "queued"
    assert job["cancel_requested"] == 1


def test_update_library_scan_run_state_records_timestamps_and_totals(tmp_path):
    _fresh_db(tmp_path)
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None

    state_db.update_library_scan_run_state(
        run["id"],
        "running",
        totals={"total_items": 3, "processed_items": 1, "unknown": 99},
    )
    running = state_db.get_library_scan_run(run["id"])
    assert running["state"] == "running"
    assert running["started_at"] is not None
    assert running["finished_at"] is None
    assert running["total_items"] == 3
    assert running["processed_items"] == 1

    state_db.update_library_scan_run_state(
        run["id"], "failed", last_error="lidarr timeout", totals={"error_items": 1}
    )
    failed = state_db.get_library_scan_run(run["id"])
    assert failed["state"] == "failed"
    assert failed["finished_at"] is not None
    assert failed["last_error"] == "lidarr timeout"
    assert failed["error_items"] == 1


def test_library_scan_item_ledger_upserts_and_filters(tmp_path):
    _fresh_db(tmp_path)
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None

    state_db.upsert_library_scan_item(
        run["id"], 101, album_id=20, state="queued", attempts=0
    )
    state_db.upsert_library_scan_item(
        run["id"], 102, album_id=20, state="error", attempts=1, last_error="missing"
    )
    state_db.upsert_library_scan_item(
        run["id"], 101, album_id=20, state="measured", attempts=1
    )

    total, rows = state_db.list_library_scan_items(run["id"])
    assert total == 2
    assert [row["trackfile_id"] for row in rows] == [101, 102]
    assert rows[0]["state"] == "measured"
    assert rows[0]["attempts"] == 1

    error_total, error_rows = state_db.list_library_scan_items(
        run["id"], state=["error"]
    )
    assert error_total == 1
    assert error_rows[0]["trackfile_id"] == 102
    assert error_rows[0]["last_error"] == "missing"


def test_get_library_scan_item_returns_one_ledger_row(tmp_path):
    _fresh_db(tmp_path)
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None
    state_db.upsert_library_scan_item(
        run["id"], 9, album_id=90, state="spectral_measured", attempts=1
    )

    row = state_db.get_library_scan_item(run["id"], 9)

    assert row is not None
    assert row["trackfile_id"] == 9
    assert row["album_id"] == 90
    assert row["state"] == "spectral_measured"
    assert state_db.get_library_scan_item(run["id"], 999) is None


def test_active_library_scan_run_tracks_newest_active_only(tmp_path):
    _fresh_db(tmp_path)

    assert state_db.get_active_library_scan_run() is None
    first = state_db.enqueue_library_scan(mode="cheap")
    assert first is not None
    assert state_db.get_active_library_scan_run()["id"] == first["id"]

    state_db.update_library_scan_run_state(first["id"], "completed")
    assert state_db.get_active_library_scan_run() is None
