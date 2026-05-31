"""Tests for F2.1 worker queue foundation."""

from __future__ import annotations

import json
import time

import pytest

import state_db
import worker


@pytest.fixture
def fresh_db(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    yield db_file
    # Stop worker if started
    worker.stop_worker(timeout=2)
    state_db._initialized = False


# ============================================================================
# enqueue / dedupe
# ============================================================================

def test_enqueue_returns_id(fresh_db):
    job_id = state_db.enqueue_job(jid="abc123", type="noop", payload={"sleep_sec": 0})
    assert isinstance(job_id, int)
    assert job_id > 0


def test_enqueue_duplicate_with_dedupe_key_returns_existing(fresh_db):
    id1 = state_db.enqueue_job(jid="dup1", type="noop", dedupe_key="key-x")
    id2 = state_db.enqueue_job(jid="dup1-retry", type="noop", dedupe_key="key-x")
    assert id1 == id2  # existing returned, no new row


def test_enqueue_without_dedupe_key_inserts_new(fresh_db):
    id1 = state_db.enqueue_job(jid="job1", type="noop")
    id2 = state_db.enqueue_job(jid="job2", type="noop")
    assert id1 != id2


def test_enqueue_dedupe_only_blocks_active_states(fresh_db):
    """If existing dedupe-matched job is in terminal state, new enqueue creates fresh row."""
    id1 = state_db.enqueue_job(jid="completed1", type="noop", dedupe_key="key-y")
    state_db.mark_job_completed(id1, result_state="noop_ok")
    id2 = state_db.enqueue_job(jid="newattempt", type="noop", dedupe_key="key-y")
    assert id2 != id1  # not blocked since first is completed


# ============================================================================
# dequeue / state transitions
# ============================================================================

def test_dequeue_returns_none_when_empty(fresh_db):
    assert state_db.dequeue_next_job(worker_id="test-w") is None


def test_dequeue_atomic_claim(fresh_db):
    state_db.enqueue_job(jid="claim1", type="noop")
    j1 = state_db.dequeue_next_job(worker_id="w1")
    j2 = state_db.dequeue_next_job(worker_id="w2")
    assert j1 is not None
    assert j2 is None  # Already claimed by w1
    assert j1["state"] == "running"
    assert j1["worker_id"] == "w1"
    assert j1["attempts"] == 1


def test_dequeue_respects_priority_then_fifo(fresh_db):
    """Lower priority number runs first; within same priority, FIFO by created_at."""
    id_low = state_db.enqueue_job(jid="low", type="noop", priority=10)
    time.sleep(0.01)  # ensure created_at differs
    id_high = state_db.enqueue_job(jid="high", type="noop", priority=1)
    job = state_db.dequeue_next_job(worker_id="w1")
    assert job["id"] == id_high


def test_dequeue_respects_next_attempt_at(fresh_db):
    """Jobs with future next_attempt_at are not eligible yet."""
    job_id = state_db.enqueue_job(jid="future1", type="noop")
    with state_db._connect() as conn:
        conn.execute(
            "UPDATE jobs SET next_attempt_at = ? WHERE id = ?",
            (time.time() + 60, job_id),
        )
    assert state_db.dequeue_next_job(worker_id="w1") is None


def test_mark_job_completed_sets_result_state(fresh_db):
    job_id = state_db.enqueue_job(jid="comp1", type="noop")
    state_db.dequeue_next_job(worker_id="w1")
    state_db.mark_job_completed(job_id, result_state="imported", result={"x": 1})
    job = state_db.get_job(job_id)
    assert job["state"] == "completed"
    assert job["result_state"] == "imported"
    assert job["finished_at"] is not None
    assert json.loads(job["result_json"]) == {"x": 1}


def test_mark_job_failed_records_error(fresh_db):
    job_id = state_db.enqueue_job(jid="fail1", type="noop")
    state_db.dequeue_next_job(worker_id="w1")
    state_db.mark_job_failed(job_id, "validator timeout after 30s")
    job = state_db.get_job(job_id)
    assert job["state"] == "failed"
    assert "validator timeout" in (job["error_text"] or "")


def test_schedule_job_retry_requeues_running_job(fresh_db):
    job_id = state_db.enqueue_job(jid="retry1", type="noop", max_attempts=3)
    state_db.dequeue_next_job(worker_id="w1")
    assert state_db.schedule_job_retry(job_id, "HTTP 503 Service Unavailable", delay_sec=60) is True

    job = state_db.get_job(job_id)
    assert job["state"] == "queued"
    assert job["result_state"] == "retry_scheduled"
    assert job["next_attempt_at"] > time.time()
    assert "HTTP 503" in (job["error_text"] or "")
    progress = json.loads(job["progress_json"])
    assert progress["stage"] == "retry_wait"
    assert progress["retry_at"] == job["next_attempt_at"]


def test_schedule_job_retry_refuses_exhausted_attempts(fresh_db):
    job_id = state_db.enqueue_job(jid="retry_exhausted", type="noop", max_attempts=1)
    state_db.dequeue_next_job(worker_id="w1")
    assert state_db.schedule_job_retry(job_id, "timeout", delay_sec=60) is False
    assert state_db.get_job(job_id)["state"] == "running"


# ============================================================================
# heartbeat / lease / stale recovery
# ============================================================================

def test_heartbeat_extends_lease(fresh_db):
    job_id = state_db.enqueue_job(jid="hb1", type="noop")
    state_db.dequeue_next_job(worker_id="w1")
    job1 = state_db.get_job(job_id)
    time.sleep(0.05)
    state_db.heartbeat_job(job_id, lease_sec=600)
    job2 = state_db.get_job(job_id)
    assert job2["lease_expires_at"] > job1["lease_expires_at"]
    assert job2["heartbeat_at"] > (job1["heartbeat_at"] or 0)


def test_recover_stale_running_requeues_when_attempts_left(fresh_db):
    job_id = state_db.enqueue_job(jid="stale1", type="noop", max_attempts=3)
    state_db.dequeue_next_job(worker_id="dead-w")
    # Simulate expired lease
    with state_db._connect() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
            (time.time() - 60, job_id),
        )
    recovered = state_db.recover_stale_running_jobs()
    assert recovered == 1
    job = state_db.get_job(job_id)
    assert job["state"] == "queued"
    assert job["next_attempt_at"] is not None
    assert "stale lease" in (job["error_text"] or "")


def test_recover_running_jobs_force_requeues_even_before_lease_expiry(fresh_db):
    """Boot recovery should not trust a prior process lease after restart."""
    job_id = state_db.enqueue_job(jid="fresh_running", type="noop", max_attempts=3)
    state_db.dequeue_next_job(worker_id="dead-w", lease_sec=600)

    assert state_db.recover_stale_running_jobs() == 0
    assert state_db.recover_stale_running_jobs(force=True) == 1
    job = state_db.get_job(job_id)
    assert job["state"] == "queued"
    assert job["worker_id"] is None
    assert "stale lease" in (job["error_text"] or "")


def test_recover_stale_running_fails_when_attempts_exhausted(fresh_db):
    job_id = state_db.enqueue_job(jid="exhaust1", type="noop", max_attempts=1)
    state_db.dequeue_next_job(worker_id="dead-w")  # attempts → 1
    with state_db._connect() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
            (time.time() - 60, job_id),
        )
    state_db.recover_stale_running_jobs()
    job = state_db.get_job(job_id)
    assert job["state"] == "failed"
    assert job["result_state"] == "stale"


# ============================================================================
# cancel
# ============================================================================

def test_request_job_cancel_sets_flag(fresh_db):
    job_id = state_db.enqueue_job(jid="cancel1", type="noop")
    assert state_db.request_job_cancel(job_id) is True
    assert state_db.is_job_cancel_requested(job_id) is True


def test_request_job_cancel_marks_running_as_cancelling(fresh_db):
    job_id = state_db.enqueue_job(jid="cancel_running", type="noop")
    state_db.dequeue_next_job(worker_id="w1")
    assert state_db.request_job_cancel(job_id) is True
    job = state_db.get_job(job_id)
    assert job["state"] == "cancelling"
    assert job["cancel_requested"] == 1


def test_request_job_cancel_ignores_terminal_jobs(fresh_db):
    job_id = state_db.enqueue_job(jid="done1", type="noop")
    state_db.dequeue_next_job(worker_id="w1")
    state_db.mark_job_completed(job_id, result_state="ok")
    # Cannot cancel a completed job
    assert state_db.request_job_cancel(job_id) is False


# ============================================================================
# worker loop end-to-end
# ============================================================================

def test_worker_executes_enqueued_job(fresh_db):
    """Enqueue a noop job, start worker, verify it runs to completion."""
    job_id = state_db.enqueue_job(jid="e2e1", type="noop", payload={"sleep_sec": 0.05})
    worker.start_worker()
    try:
        # Poll for completion (max 5s)
        for _ in range(50):
            job = state_db.get_job(job_id)
            if job and job["state"] == "completed":
                break
            time.sleep(0.1)
        assert job["state"] == "completed"
        assert job["result_state"] == "noop_ok"
    finally:
        worker.stop_worker(timeout=2)


def test_worker_marks_failed_executor_jobs(fresh_db):
    job_id = state_db.enqueue_job(
        jid="fail2", type="noop", payload={"fail": True, "fail_msg": "boom"},
    )
    worker.start_worker()
    try:
        for _ in range(50):
            job = state_db.get_job(job_id)
            if job and job["state"] == "failed":
                break
            time.sleep(0.1)
        assert job["state"] == "failed"
        assert "boom" in (job["error_text"] or "")
    finally:
        worker.stop_worker(timeout=2)


def test_worker_handles_unknown_type_gracefully(fresh_db):
    job_id = state_db.enqueue_job(jid="unk1", type="nonexistent_type_xyz")
    worker.start_worker()
    try:
        for _ in range(50):
            job = state_db.get_job(job_id)
            if job and job["state"] == "failed":
                break
            time.sleep(0.1)
        assert job["state"] == "failed"
        assert job["result_state"] == "config_error"
    finally:
        worker.stop_worker(timeout=2)


def test_worker_requeues_transient_executor_failure(fresh_db, monkeypatch):
    def transient_executor(job):
        raise RuntimeError("HTTP 503 Service Unavailable")

    monkeypatch.setitem(worker._RETRY_BACKOFF_SEC, "transient_test", [60, 300])
    worker.register_executor("transient_test", transient_executor)
    job_id = state_db.enqueue_job(jid="transient1", type="transient_test", max_attempts=3)
    job = state_db.dequeue_next_job(worker_id="w1")
    worker._execute_job(job)

    final = state_db.get_job(job_id)
    assert final["state"] == "queued"
    assert final["result_state"] == "retry_scheduled"
    assert final["attempts"] == 1
    assert final["next_attempt_at"] > time.time()
    assert "HTTP 503" in (final["error_text"] or "")


def test_worker_does_not_retry_permanent_executor_failure(fresh_db, monkeypatch):
    def permanent_executor(job):
        raise RuntimeError("no importable files after verification")

    monkeypatch.setitem(worker._RETRY_BACKOFF_SEC, "permanent_test", [60, 300])
    worker.register_executor("permanent_test", permanent_executor)
    job_id = state_db.enqueue_job(jid="permanent1", type="permanent_test", max_attempts=3)
    job = state_db.dequeue_next_job(worker_id="w1")
    worker._execute_job(job)

    final = state_db.get_job(job_id)
    assert final["state"] == "failed"
    assert final["result_state"] == "failed"
    assert "no importable files" in (final["error_text"] or "")


def test_worker_does_not_retry_after_attempts_exhausted(fresh_db, monkeypatch):
    def timeout_executor(job):
        raise RuntimeError("tidal-dl-ng timeout")

    monkeypatch.setitem(worker._RETRY_BACKOFF_SEC, "exhausted_transient_test", [60, 300])
    worker.register_executor("exhausted_transient_test", timeout_executor)
    job_id = state_db.enqueue_job(jid="exhausted_transient", type="exhausted_transient_test", max_attempts=1)
    job = state_db.dequeue_next_job(worker_id="w1")
    worker._execute_job(job)

    final = state_db.get_job(job_id)
    assert final["state"] == "failed"
    assert final["result_state"] == "failed"
    assert "timeout" in (final["error_text"] or "")


def test_worker_cancel_before_start(fresh_db):
    """If cancel_requested set before worker dequeues, job is cancelled, not run."""
    job_id = state_db.enqueue_job(jid="cancel2", type="noop", payload={"sleep_sec": 0})
    state_db.request_job_cancel(job_id)
    worker.start_worker()
    try:
        for _ in range(50):
            job = state_db.get_job(job_id)
            if job and job["state"] in ("cancelled", "completed", "failed"):
                break
            time.sleep(0.1)
        assert job["state"] == "cancelled"
    finally:
        worker.stop_worker(timeout=2)


def test_worker_heartbeats_while_executor_runs(fresh_db, monkeypatch):
    """Long executors should keep the lease alive while the worker is busy."""
    heartbeat_calls = []
    original_heartbeat = state_db.heartbeat_job

    def tracked_heartbeat(job_id, *, lease_sec=state_db.DEFAULT_LEASE_SEC):
        heartbeat_calls.append(job_id)
        original_heartbeat(job_id, lease_sec=lease_sec)

    def slow_executor(job):
        time.sleep(0.08)
        return "slow_ok", {}

    monkeypatch.setattr(state_db, "HEARTBEAT_INTERVAL_SEC", 0.02)
    monkeypatch.setattr(state_db, "heartbeat_job", tracked_heartbeat)
    worker.register_executor("slow_test", slow_executor)

    job_id = state_db.enqueue_job(jid="slow1", type="slow_test")
    job = state_db.dequeue_next_job(worker_id="w1")
    worker._execute_job(job)

    final = state_db.get_job(job_id)
    assert final["state"] == "completed"
    assert final["result_state"] == "slow_ok"
    assert len(heartbeat_calls) >= 2


def test_count_active_jobs(fresh_db):
    state_db.enqueue_job(jid="a1", type="noop")
    state_db.enqueue_job(jid="a2", type="noop")
    j3 = state_db.enqueue_job(jid="a3", type="noop")
    state_db.dequeue_next_job(worker_id="w1")
    state_db.mark_job_completed(j3, result_state="ok")
    assert state_db.count_active_jobs() == 2  # a1 queued, a2 running


# ============================================================================
# list_jobs queries
# ============================================================================

def test_list_jobs_filters_by_state(fresh_db):
    state_db.enqueue_job(jid="lj1", type="noop")
    j2 = state_db.enqueue_job(jid="lj2", type="noop")
    state_db.dequeue_next_job(worker_id="w1")
    state_db.mark_job_completed(j2, result_state="ok")
    total, rows = state_db.list_jobs(state=["completed"])
    assert total == 1


def test_list_jobs_filters_by_jid(fresh_db):
    state_db.enqueue_job(jid="findme", type="noop")
    state_db.enqueue_job(jid="other", type="noop")
    total, rows = state_db.list_jobs(jid="findme")
    assert total == 1
    assert rows[0]["jid"] == "findme"
