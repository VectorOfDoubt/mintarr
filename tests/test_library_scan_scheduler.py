"""Tests for F5.4 slice 5e scheduled cheap library scans."""

from __future__ import annotations

import time

import library_scan_scheduler
import state_db


def _fresh_db(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    return db_file


def teardown_function():
    library_scan_scheduler.stop_scheduled_scans(timeout=1.0)


def test_scheduled_scan_skips_when_import_work_active():
    calls = {"enqueue": 0}

    result = library_scan_scheduler.enqueue_scheduled_scan_if_quiet(
        import_work_active=lambda: True,
        scan_already_active=lambda: False,
        enqueue_scan=lambda **kwargs: calls.__setitem__(
            "enqueue", calls["enqueue"] + 1
        ),
    )

    assert result == {"queued": False, "reason": "import_active"}
    assert calls["enqueue"] == 0


def test_scheduled_scan_skips_when_scan_already_active():
    calls = {"enqueue": 0}

    result = library_scan_scheduler.enqueue_scheduled_scan_if_quiet(
        import_work_active=lambda: False,
        scan_already_active=lambda: True,
        enqueue_scan=lambda **kwargs: calls.__setitem__(
            "enqueue", calls["enqueue"] + 1
        ),
    )

    assert result == {"queued": False, "reason": "scan_active"}
    assert calls["enqueue"] == 0


def test_scheduled_scan_enqueues_metadata_when_quiet(tmp_path):
    _fresh_db(tmp_path)

    result = library_scan_scheduler.enqueue_scheduled_scan_if_quiet(
        import_work_active=lambda: False,
        scan_already_active=lambda: False,
    )

    assert result["queued"] is True
    run = result["run"]
    # Scheduled sweeps use the fast metadata tier, not the heavy fused cheap scan.
    assert run["mode"] == "metadata"
    assert run["requested_by"] == "scheduler"
    job = state_db.get_job(run["worker_job_id"])
    assert job["type"] == state_db.LIBRARY_SCAN_JOB_TYPE


def test_scheduled_scan_handles_enqueue_failure():
    result = library_scan_scheduler.enqueue_scheduled_scan_if_quiet(
        import_work_active=lambda: False,
        scan_already_active=lambda: False,
        enqueue_scan=lambda **kwargs: None,
    )

    assert result == {"queued": False, "reason": "enqueue_failed"}


def test_start_scheduled_scans_is_idempotent_and_ticks(monkeypatch):
    calls = {"enqueue": 0}

    def fake_enqueue(*, requested_by="scheduler"):
        calls["enqueue"] += 1
        return {"queued": False, "reason": "scan_active"}

    monkeypatch.setattr(
        library_scan_scheduler, "enqueue_scheduled_scan_if_quiet", fake_enqueue
    )

    assert library_scan_scheduler.start_scheduled_scans(interval_seconds=0.01) is True
    assert library_scan_scheduler.start_scheduled_scans(interval_seconds=0.01) is True

    deadline = time.time() + 1.0
    while calls["enqueue"] == 0 and time.time() < deadline:
        time.sleep(0.01)

    assert calls["enqueue"] > 0
    assert library_scan_scheduler.is_scheduled_scan_running() is True


def test_start_scheduled_scans_rejects_non_positive_interval():
    assert library_scan_scheduler.start_scheduled_scans(interval_seconds=0) is False
    assert library_scan_scheduler.is_scheduled_scan_running() is False
