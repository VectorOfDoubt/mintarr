"""F5.4 background library scan worker.

This worker is intentionally separate from the import worker. It reuses the F2
``jobs`` row for lease/heartbeat/cancel/dedupe, but claims only
``type=library_scan`` jobs so a slow library index cannot occupy the import slot.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable

import requests

import library_evidence
import state_db

log = logging.getLogger("tidalhires.library_scan")

_shutdown_event = threading.Event()
_scan_thread: threading.Thread | None = None
_worker_id = ""
_POLL_INTERVAL_SEC = 2.0
_PAUSE_INTERVAL_SEC = 2.0


class LibraryScanCancelled(Exception):
    """Scan stopped cooperatively because cancel was requested."""


def _lidarr_api() -> str:
    return os.environ.get("LIDARR_API_URL", "http://host.docker.internal:8686/api/v1")


def _lidarr_key() -> str:
    try:
        import server

        return server._get_lidarr_key()
    except Exception:
        return os.environ.get("LIDARR_API_KEY", "")


def _fetch_lidarr_trackfiles(
    *,
    api: str | None = None,
    key: str | None = None,
    get: Callable | None = None,
) -> list[dict]:
    """Snapshot Lidarr's album trackfiles with read-only API calls."""
    api = (api or _lidarr_api()).rstrip("/")
    key = key if key is not None else _lidarr_key()
    get = get or requests.get
    headers = {"X-Api-Key": key}
    albums_resp = get(f"{api}/album", headers=headers, timeout=30)
    albums_resp.raise_for_status()
    albums = albums_resp.json()
    if not isinstance(albums, list):
        raise RuntimeError("Lidarr /album returned non-list")

    out: list[dict] = []
    for album in albums:
        if not isinstance(album, dict) or album.get("id") is None:
            continue
        album_id = album.get("id")
        tf_resp = get(
            f"{api}/trackfile?albumId={album_id}",
            headers=headers,
            timeout=30,
        )
        tf_resp.raise_for_status()
        rows = tf_resp.json()
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item.setdefault("albumId", album_id)
            out.append(item)
    return out


def _is_import_work_active() -> bool:
    """True if non-scan queue work is active; scan must yield to QC/import."""
    return (
        state_db.count_active_jobs(exclude_types=(state_db.LIBRARY_SCAN_JOB_TYPE,)) > 0
    )


def _raise_if_cancelled(job_id: int, run_id: int) -> None:
    if _shutdown_event.is_set() or state_db.is_job_cancel_requested(job_id):
        state_db.update_library_scan_run_state(run_id, "cancelling")
        raise LibraryScanCancelled("library scan cancelled")


def _wait_for_import_quiet(job_id: int, run_id: int) -> None:
    while _is_import_work_active():
        _raise_if_cancelled(job_id, run_id)
        state_db.heartbeat_job(job_id)
        state_db.update_job_progress(
            job_id,
            {
                "stage": "paused",
                "percent": 0,
                "message": "Waiting for import queue to clear",
            },
        )
        _shutdown_event.wait(timeout=_PAUSE_INTERVAL_SEC)


def _measure_trackfile_item(run_id: int, tf: dict) -> str:
    """Measure one trackfile and upsert library evidence. Returns item state."""
    trackfile_id = tf.get("id")
    path = tf.get("path")
    album_id = tf.get("albumId") or tf.get("album_id")
    if trackfile_id is None or not path:
        return "unmeasured"

    resolved_path, size, mtime = library_evidence.stat_for_freshness(path)
    prior = state_db.get_library_evidence(int(trackfile_id))
    if (
        prior
        and prior.get("status") == "measured"
        and resolved_path is not None
        and prior.get("path") == resolved_path
        and prior.get("size") == size
        and prior.get("mtime") == mtime
        and prior.get("sensor_version") == library_evidence.SENSOR_VERSION
    ):
        state_db.upsert_library_scan_item(
            run_id, int(trackfile_id), album_id=album_id, state="fresh"
        )
        return "fresh"

    measurement = library_evidence.measure_trackfile(path)
    state_db.upsert_library_evidence(
        {
            "trackfile_id": trackfile_id,
            "album_id": album_id,
            "path": resolved_path or path,
            "size": size,
            "mtime": mtime,
            "status": measurement.status,
            "reason": measurement.reason,
            "codec": measurement.codec,
            "sample_rate": measurement.sample_rate,
            "bit_depth": measurement.bit_depth,
            "channels": measurement.channels,
            "lossless": measurement.lossless,
            "integrity_ok": measurement.integrity_ok,
            "sensor_version": library_evidence.SENSOR_VERSION,
        }
    )
    state = "measured" if measurement.status == "measured" else "unmeasured"
    state_db.upsert_library_scan_item(
        run_id, int(trackfile_id), album_id=album_id, state=state
    )
    return state


def _execute_library_scan_job(job: dict) -> None:
    """Run one queued library_scan job and keep run/job states in sync."""
    job_id = int(job["id"])
    payload = json.loads(job.get("payload_json") or "{}")
    run_id = int(payload.get("run_id") or 0)
    mode = str(payload.get("mode") or "cheap")
    if not run_id:
        state_db.mark_job_failed(job_id, "library scan missing run_id")
        return

    if state_db.is_job_cancel_requested(job_id):
        state_db.update_library_scan_run_state(run_id, "cancelled")
        state_db.mark_job_cancelled(job_id)
        return

    counters = {
        "total_items": 0,
        "processed_items": 0,
        "measured_items": 0,
        "fresh_items": 0,
        "unmeasured_items": 0,
        "error_items": 0,
    }
    try:
        state_db.update_library_scan_run_state(run_id, "running")
        state_db.heartbeat_job(job_id)
        trackfiles = _fetch_lidarr_trackfiles()
        counters["total_items"] = len(trackfiles)
        state_db.update_library_scan_run_state(run_id, "running", totals=counters)

        for index, tf in enumerate(trackfiles, start=1):
            _raise_if_cancelled(job_id, run_id)
            _wait_for_import_quiet(job_id, run_id)
            trackfile_id = tf.get("id")
            album_id = tf.get("albumId") or tf.get("album_id")
            if trackfile_id is None:
                counters["unmeasured_items"] += 1
                counters["processed_items"] += 1
                continue
            try:
                state_db.upsert_library_scan_item(
                    run_id, int(trackfile_id), album_id=album_id, state="measuring"
                )
                outcome = _measure_trackfile_item(run_id, tf)
                if outcome == "fresh":
                    counters["fresh_items"] += 1
                elif outcome == "measured":
                    counters["measured_items"] += 1
                else:
                    counters["unmeasured_items"] += 1
            except Exception as exc:
                counters["error_items"] += 1
                state_db.upsert_library_scan_item(
                    run_id,
                    int(trackfile_id),
                    album_id=album_id,
                    state="error",
                    last_error=str(exc)[:1000],
                )
                log.exception("library scan item failed trackfile_id=%s", trackfile_id)
            finally:
                counters["processed_items"] += 1
                pct = int((counters["processed_items"] / max(1, len(trackfiles))) * 100)
                state_db.update_job_progress(
                    job_id,
                    {
                        "stage": "library_scan",
                        "percent": pct,
                        "message": f"Scanned {counters['processed_items']}/{len(trackfiles)} library trackfiles",
                        "mode": mode,
                    },
                )
                state_db.update_library_scan_run_state(
                    run_id, "running", totals=counters
                )
                state_db.heartbeat_job(job_id)

        state_db.update_library_scan_run_state(run_id, "completed", totals=counters)
        state_db.mark_job_completed(
            job_id,
            result_state="library_scan_completed",
            result={"run_id": run_id, **counters},
        )
    except LibraryScanCancelled as exc:
        state_db.update_library_scan_run_state(run_id, "cancelled", totals=counters)
        state_db.mark_job_cancelled(job_id)
        log.info("[library scan %s] cancelled: %s", run_id, exc)
    except Exception as exc:
        state_db.update_library_scan_run_state(
            run_id, "failed", last_error=str(exc)[:1000], totals=counters
        )
        state_db.mark_job_failed(job_id, str(exc), result_state="library_scan_failed")
        log.exception("[library scan %s] failed", run_id)


def _scan_loop() -> None:
    log.info("library scan worker starting (worker_id=%s)", _worker_id)
    while not _shutdown_event.is_set():
        try:
            job = state_db.dequeue_next_job(
                worker_id=_worker_id,
                include_types=(state_db.LIBRARY_SCAN_JOB_TYPE,),
            )
            if job is None:
                _shutdown_event.wait(timeout=_POLL_INTERVAL_SEC)
                continue
            _execute_library_scan_job(job)
        except Exception:
            log.exception("library scan worker encountered unexpected error")
            _shutdown_event.wait(timeout=_POLL_INTERVAL_SEC)
    log.info("library scan worker exiting (worker_id=%s)", _worker_id)


def start_worker() -> None:
    """Start the dedicated library-scan worker. Idempotent."""
    global _scan_thread, _worker_id
    if _scan_thread is not None and _scan_thread.is_alive():
        return
    _worker_id = f"library-scan-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    _shutdown_event.clear()
    _scan_thread = threading.Thread(
        target=_scan_loop, name="mintarr-library-scan-worker", daemon=True
    )
    _scan_thread.start()


def stop_worker(timeout: float = 5.0) -> None:
    """Stop the dedicated library-scan worker. Test helper + graceful shutdown."""
    global _scan_thread
    _shutdown_event.set()
    if _scan_thread is not None:
        _scan_thread.join(timeout=timeout)
        if not _scan_thread.is_alive():
            _scan_thread = None
