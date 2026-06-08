"""Optional scheduled cheap library-quality scans (F5.4 slice 5e).

This scheduler only enqueues the already-existing ``library_scan`` F2 job. The
dedicated scan worker still owns execution, leasing, cancellation and yielding.
Disabled by default so upgrades do not start scanning library mounts until an
operator opts in.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import state_db

log = logging.getLogger("tidalhires.library_scan_scheduler")

_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None


def _import_work_active() -> bool:
    return (
        state_db.count_active_jobs(exclude_types=(state_db.LIBRARY_SCAN_JOB_TYPE,)) > 0
    )


def _scan_already_active() -> bool:
    return state_db.get_active_library_scan_run() is not None


def enqueue_scheduled_scan_if_quiet(
    *,
    requested_by: str = "scheduler",
    import_work_active: Callable[[], bool] | None = None,
    scan_already_active: Callable[[], bool] | None = None,
    enqueue_scan: Callable[..., dict | None] | None = None,
) -> dict:
    """Try to enqueue one scheduled cheap scan without disturbing imports.

    Returns a small status dict for tests/logging. It never raises: scheduled
    scans are opportunistic and must not affect the main QC runtime.
    """
    try:
        import_work_active = import_work_active or _import_work_active
        scan_already_active = scan_already_active or _scan_already_active
        enqueue_scan = enqueue_scan or state_db.enqueue_library_scan
        if import_work_active():
            return {"queued": False, "reason": "import_active"}
        if scan_already_active():
            return {"queued": False, "reason": "scan_active"}
        run = enqueue_scan(mode="cheap", requested_by=requested_by)
        if not run:
            return {"queued": False, "reason": "enqueue_failed"}
        log.info("scheduled cheap library scan queued run_id=%s", run.get("id"))
        return {"queued": True, "run": run}
    except Exception:
        log.exception("scheduled cheap library scan enqueue failed")
        return {"queued": False, "reason": "error"}


def start_scheduled_scans(
    *,
    interval_seconds: float,
    requested_by: str = "scheduler",
) -> bool:
    """Start the scheduled cheap-scan thread. Idempotent."""
    global _scheduler_thread
    if interval_seconds <= 0:
        log.warning("library scan scheduler not started: interval_seconds must be > 0")
        return False
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        log.debug("library scan scheduler already running")
        return True

    _scheduler_stop.clear()

    def _loop() -> None:
        log.info(
            "library scan scheduler started (interval_seconds=%s)",
            interval_seconds,
        )
        while not _scheduler_stop.wait(timeout=interval_seconds):
            result = enqueue_scheduled_scan_if_quiet(requested_by=requested_by)
            if not result.get("queued"):
                log.debug("scheduled library scan skipped: %s", result.get("reason"))
        log.info("library scan scheduler exiting")

    _scheduler_thread = threading.Thread(
        target=_loop, name="mintarr-library-scan-scheduler", daemon=True
    )
    _scheduler_thread.start()
    return True


def stop_scheduled_scans(timeout: float = 5.0) -> None:
    """Signal scheduler shutdown and wait. Useful in tests."""
    global _scheduler_thread
    _scheduler_stop.set()
    if _scheduler_thread is not None:
        _scheduler_thread.join(timeout=timeout)
        if not _scheduler_thread.is_alive():
            _scheduler_thread = None


def is_scheduled_scan_running() -> bool:
    return _scheduler_thread is not None and _scheduler_thread.is_alive()
