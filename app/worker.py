"""F2 worker queue — single background thread that drains jobs from state_db.

Per TIDALHIRES_F2_WORKER_QUEUE_DESIGN.md (Codex-reviewed):
- N=1 concurrency (tidal-dl-ng cfg is global, parallel grabs would race)
- Lease-based stale detection (heartbeat every 30s, 5min lease)
- jobs-table = source of truth for worker execution
- Conservative auto-retry for explicit transient executor failures
- F2.4 cancellation is cooperative: endpoints set cancel_requested and
  executors raise JobCancelled at checkpoints / cancellable subprocess loops.

The `noop` executor remains for queue tests. Real job-types
(`tidal_grab`, `promote_import`, `retry_import`) are registered by server.py.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Callable

import state_db

log = logging.getLogger("tidalhires.worker")

# ============================================================================
# Job executor registry
# ============================================================================

# type → callable(job_dict) → (result_state, result_dict)
# Executors raise exception on failure; worker catches + marks failed.
_executors: dict[str, Callable[[dict], tuple[str | None, dict | None]]] = {}


class JobCancelled(Exception):
    """Executor stopped because the user requested cancellation."""


def register_executor(
    job_type: str, fn: Callable[[dict], tuple[str | None, dict | None]]
) -> None:
    """Register a function to execute jobs of a given type.

    Function receives the full job dict (from state_db). Should return
    (result_state, result_dict) on success — e.g., ("imported", {"trackfile_count": 12}).
    Should raise on unexpected failure (worker logs + marks failed).
    """
    _executors[job_type] = fn
    log.info("registered worker executor for type=%s", job_type)


def _execute_noop_job(job: dict) -> tuple[str, dict]:
    """F2.1 testing executor — sleeps briefly + returns success.

    Honors `payload.sleep_sec` (default 0.1) and `payload.fail` flag for
    failure-path tests.
    """
    payload = json.loads(job.get("payload_json") or "{}")
    sleep_sec = float(payload.get("sleep_sec", 0.1))
    if sleep_sec > 0:
        time.sleep(sleep_sec)
    if payload.get("fail"):
        raise RuntimeError(payload.get("fail_msg", "noop job requested failure"))
    return ("noop_ok", {"slept_sec": sleep_sec})


register_executor("noop", _execute_noop_job)


# ============================================================================
# Worker thread
# ============================================================================

_shutdown_event = threading.Event()
_worker_thread: threading.Thread | None = None
_worker_id: str = ""
_POLL_INTERVAL_SEC = 2.0
_RECOVERY_INTERVAL_SEC = 30.0

_RETRY_BACKOFF_SEC: dict[str, list[int]] = {
    "tidal_grab": [60, 300, 900],
    "sab_usenet_grab": [60, 300, 900],
    "qbittorrent_torrent_grab": [60, 300, 900],
    "promote_import": [60, 300, 900],
    "retry_import": [60, 300, 900],
}

_TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "temporary failure",
    "connection reset",
    "connection aborted",
    "connection refused",
    "remote disconnected",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
)

_PERMANENT_ERROR_MARKERS = (
    "missing album_id",
    "no executor",
    "verification is not retryable",
    "promote is not allowed",
    "retry failed with http 400",
    "retry failed with http 401",
    "retry failed with http 403",
    "retry failed with http 404",
    "retry failed with http 409",
    "promote failed with http 400",
    "promote failed with http 401",
    "promote failed with http 403",
    "promote failed with http 404",
    "promote failed with http 409",
    "no importable files after verification",
    "manualimport and rescue failed",
    "manualimport returned no candidates",
    "codec mismatch",
    "flac -t failed",
    "v2 policy block",
)


def _retry_delay_sec(job: dict) -> int:
    job_type = str(job.get("type") or "")
    delays = _RETRY_BACKOFF_SEC.get(job_type, [])
    if not delays:
        return 0
    attempts = int(job.get("attempts") or 0)
    index = max(0, min(attempts - 1, len(delays) - 1))
    return delays[index]


def _is_transient_failure(job: dict, exc: Exception) -> bool:
    """Return True only for explicit transient failures safe to retry."""
    job_type = job.get("type")
    if job_type not in _RETRY_BACKOFF_SEC:
        return False
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 1)
    if attempts >= max_attempts:
        return False
    text = str(exc).lower()
    if any(marker in text for marker in _PERMANENT_ERROR_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


def _execute_job(job: dict) -> None:
    """Run a single job through the registered executor, update DB on outcome."""
    job_id = job["id"]
    job_type = job["type"]
    jid = job.get("jid", "?")

    executor = _executors.get(job_type)
    if executor is None:
        log.error(
            "[job %s/%s] no executor registered for type=%s", job_id, jid, job_type
        )
        state_db.mark_job_failed(
            job_id, f"no executor for type={job_type}", result_state="config_error"
        )
        return

    log.info("[job %s/%s] starting (type=%s)", job_id, jid, job_type)
    heartbeat_stop = threading.Event()

    def _heartbeat_loop() -> None:
        while not heartbeat_stop.wait(timeout=state_db.HEARTBEAT_INTERVAL_SEC):
            state_db.heartbeat_job(job_id)

    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        name=f"tidalhires-job-heartbeat-{job_id}",
        daemon=True,
    )
    try:
        # Early cancel check
        if state_db.is_job_cancel_requested(job_id):
            state_db.mark_job_cancelled(job_id)
            log.info("[job %s/%s] cancelled before start", job_id, jid)
            return

        state_db.heartbeat_job(job_id)
        heartbeat_thread.start()
        result_state, result = executor(job)
        state_db.mark_job_completed(
            job_id, result_state=result_state, result=result or {}
        )
        log.info("[job %s/%s] completed (result_state=%s)", job_id, jid, result_state)
    except JobCancelled as exc:
        log.info("[job %s/%s] cancelled: %s", job_id, jid, exc)
        state_db.mark_job_cancelled(job_id)
    except Exception as exc:
        if not state_db.is_job_cancel_requested(job_id) and _is_transient_failure(
            job, exc
        ):
            delay_sec = _retry_delay_sec(job)
            if state_db.schedule_job_retry(job_id, str(exc), delay_sec=delay_sec):
                log.warning(
                    "[job %s/%s] transient failure; retry scheduled in %ss: %s",
                    job_id,
                    jid,
                    delay_sec,
                    exc,
                )
                return
        log.exception("[job %s/%s] failed", job_id, jid)
        state_db.mark_job_failed(job_id, str(exc))
    finally:
        heartbeat_stop.set()
        if heartbeat_thread.is_alive():
            heartbeat_thread.join(timeout=1.0)


def _worker_loop() -> None:
    """Main poll-loop. Runs until _shutdown_event is set."""
    log.info("worker loop starting (worker_id=%s)", _worker_id)
    last_recovery = 0.0
    while not _shutdown_event.is_set():
        try:
            now = time.time()
            if now - last_recovery >= _RECOVERY_INTERVAL_SEC:
                state_db.recover_stale_running_jobs()
                last_recovery = now
            job = state_db.dequeue_next_job(worker_id=_worker_id)
            if job is None:
                # Sleep with periodic wakeup-check to respond to shutdown
                _shutdown_event.wait(timeout=_POLL_INTERVAL_SEC)
                continue
            _execute_job(job)
        except Exception:
            log.exception("worker loop encountered unexpected error")
            _shutdown_event.wait(timeout=_POLL_INTERVAL_SEC)
    log.info("worker loop exiting (worker_id=%s)", _worker_id)


def start_worker() -> None:
    """Start the background worker thread. Idempotent — safe to call multiple times."""
    global _worker_thread, _worker_id
    if _worker_thread is not None and _worker_thread.is_alive():
        log.debug("worker already running")
        return

    # Boot-time recovery — find jobs left in state=running from prior process
    try:
        recovered = state_db.recover_stale_running_jobs(force=True)
        if recovered:
            log.info("boot recovery: %d stale running jobs requeued/failed", recovered)
    except Exception:
        log.exception("boot recovery failed")

    _worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    _shutdown_event.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, name="tidalhires-worker", daemon=True
    )
    _worker_thread.start()
    log.info("worker thread started (worker_id=%s)", _worker_id)


def stop_worker(timeout: float = 5.0) -> None:
    """Signal shutdown and wait for worker to exit. Useful in tests."""
    global _worker_thread
    _shutdown_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=timeout)
        if not _worker_thread.is_alive():
            _worker_thread = None


def is_worker_alive() -> bool:
    return _worker_thread is not None and _worker_thread.is_alive()


def get_worker_id() -> str:
    return _worker_id
