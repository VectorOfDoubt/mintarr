"""F5.4 background library scan worker.

This worker is intentionally separate from the import worker. It reuses the F2
``jobs`` row for lease/heartbeat/cancel/dedupe, but claims only
``type=library_scan`` jobs so a slow library index cannot occupy the import slot.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from collections.abc import Callable
from pathlib import Path

import requests

import library_evidence
import state_db

log = logging.getLogger("tidalhires.library_scan")

_shutdown_event = threading.Event()
_scan_thread: threading.Thread | None = None
_worker_id = ""
_POLL_INTERVAL_SEC = 2.0
_PAUSE_INTERVAL_SEC = 2.0
# FLAC Detective calls can legally take up to 900s. Extend the F2 lease before
# each background spectral item so stale-job recovery cannot fail a live scan.
_SPECTRAL_LEASE_SEC = 15 * 60 + 120
_SPECTRAL_TERMINAL_ITEM_STATES = {
    "spectral_fresh",
    "spectral_measured",
    "spectral_unmeasured",
    "spectral_skipped",
}


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


def _lidarr_inventory_mode() -> str:
    mode = os.environ.get("MINTARR_LIDARR_INVENTORY_MODE", "sqlite")
    return mode.strip().lower() or "artist"


def _lidarr_inventory_timeout() -> int:
    raw = os.environ.get("MINTARR_LIDARR_INVENTORY_TIMEOUT", "120")
    try:
        return max(1, int(raw))
    except ValueError:
        return 120


def _lidarr_request_timeout() -> int:
    raw = os.environ.get("MINTARR_LIDARR_REQUEST_TIMEOUT", "30")
    try:
        return max(1, int(raw))
    except ValueError:
        return 30


def _cheap_scan_workers() -> int:
    """Cheap scan subprocess parallelism.

    One trackfile measurement runs ffprobe and, for FLAC, ``flac -t``. That is
    mostly external CPU + disk I/O, so a small amount of parallelism improves
    full-library scans without letting the background scanner saturate the host.
    Operators can raise/lower it explicitly for their storage.
    """
    raw = os.environ.get("MINTARR_LIBRARY_SCAN_WORKERS")
    if raw:
        try:
            return max(1, min(int(raw), 32))
        except ValueError:
            return 1
    cpus = os.cpu_count() or 1
    return max(1, min(4, cpus // 4))


def _lidarr_db_path() -> Path:
    raw = os.environ.get("MINTARR_LIDARR_DB_PATH")
    if raw:
        return Path(raw)
    config_xml = Path(os.environ.get("LIDARR_CONFIG_XML", "/lidarr-config/config.xml"))
    return config_xml.with_name("lidarr.db")


def _fetch_lidarr_trackfiles_sqlite(db_path: Path | None = None) -> list[dict]:
    """Snapshot Lidarr trackfiles from its SQLite DB, read-only.

    The API's global ``/album`` payload can be very large on real libraries.
    For full-library scans, TrackFiles has the exact stable identity/path data
    Mintarr needs and avoids thousands of read-only API calls.
    """
    path = db_path or _lidarr_db_path()
    uri = f"file:{path}?mode=ro"
    rows: list[dict] = []
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            """
            SELECT Id, AlbumId, Path
            FROM TrackFiles
            WHERE Path IS NOT NULL AND Path != ''
            ORDER BY Id
            """
        ):
            rows.append(
                {
                    "id": int(row["Id"]),
                    "albumId": int(row["AlbumId"]),
                    "path": str(row["Path"]),
                }
            )
    finally:
        conn.close()
    return rows


def _fetch_albums_global(api: str, headers: dict, get: Callable) -> list[dict]:
    albums_resp = get(
        f"{api}/album", headers=headers, timeout=_lidarr_inventory_timeout()
    )
    albums_resp.raise_for_status()
    albums = albums_resp.json()
    if not isinstance(albums, list):
        raise RuntimeError("Lidarr /album returned non-list")
    return [album for album in albums if isinstance(album, dict)]


def _fetch_albums_by_artist(api: str, headers: dict, get: Callable) -> list[dict]:
    artists_resp = get(
        f"{api}/artist", headers=headers, timeout=_lidarr_inventory_timeout()
    )
    artists_resp.raise_for_status()
    artists = artists_resp.json()
    if not isinstance(artists, list):
        raise RuntimeError("Lidarr /artist returned non-list")

    albums: list[dict] = []
    seen_album_ids: set[int] = set()
    for artist in artists:
        if not isinstance(artist, dict) or artist.get("id") is None:
            continue
        artist_id = artist.get("id")
        albums_resp = get(
            f"{api}/album?artistId={artist_id}",
            headers=headers,
            timeout=_lidarr_request_timeout(),
        )
        albums_resp.raise_for_status()
        rows = albums_resp.json()
        if not isinstance(rows, list):
            continue
        for album in rows:
            if not isinstance(album, dict) or album.get("id") is None:
                continue
            album_id = int(album["id"])
            if album_id in seen_album_ids:
                continue
            seen_album_ids.add(album_id)
            albums.append(album)
    return albums


def _fetch_lidarr_albums(api: str, headers: dict, get: Callable) -> list[dict]:
    mode = _lidarr_inventory_mode()
    if mode == "album":
        return _fetch_albums_global(api, headers, get)
    if mode == "artist":
        return _fetch_albums_by_artist(api, headers, get)
    if mode != "auto":
        log.warning("unknown MINTARR_LIDARR_INVENTORY_MODE=%r; using artist", mode)
        return _fetch_albums_by_artist(api, headers, get)
    try:
        return _fetch_albums_global(api, headers, get)
    except Exception as exc:
        log.warning("Lidarr /album inventory failed; falling back to /artist: %s", exc)
        return _fetch_albums_by_artist(api, headers, get)


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
    mode = _lidarr_inventory_mode()
    if mode == "sqlite":
        try:
            return _fetch_lidarr_trackfiles_sqlite()
        except Exception as exc:
            log.warning("Lidarr SQLite inventory failed; falling back to API: %s", exc)
    albums = _fetch_lidarr_albums(api, headers, get)

    out: list[dict] = []
    for album in albums:
        if not isinstance(album, dict) or album.get("id") is None:
            continue
        album_id = album.get("id")
        tf_resp = get(
            f"{api}/trackfile?albumId={album_id}",
            headers=headers,
            timeout=_lidarr_request_timeout(),
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


def _background_spectral_enabled() -> bool:
    return library_evidence.spectral_enabled() and os.environ.get(
        "MINTARR_LIBRARY_BACKGROUND_SPECTRAL", ""
    ).strip().lower() in ("1", "true", "yes", "on")


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


def _measure_spectral_item(run_id: int, tf: dict) -> str:
    """Measure missing/stale spectral authenticity for one trackfile.

    Background spectral is the expensive tier. It only runs when cheap evidence
    for this trackfile is fresh, and it relies on the scan item ledger so a
    resumed/retried run can skip items already processed by this run.
    """
    trackfile_id = tf.get("id")
    path = tf.get("path")
    album_id = tf.get("albumId") or tf.get("album_id")
    if trackfile_id is None or not path:
        return "unmeasured"
    trackfile_id = int(trackfile_id)

    prior_item = state_db.get_library_scan_item(run_id, trackfile_id)
    if prior_item and prior_item.get("state") in _SPECTRAL_TERMINAL_ITEM_STATES:
        return "fresh"

    prior = state_db.get_library_evidence(trackfile_id)
    if not prior or not library_evidence.is_measured_row_fresh(prior):
        state_db.upsert_library_scan_item(
            run_id, trackfile_id, album_id=album_id, state="spectral_skipped"
        )
        return "unmeasured"
    if library_evidence.is_spectral_row_fresh(prior):
        state_db.upsert_library_scan_item(
            run_id, trackfile_id, album_id=album_id, state="spectral_fresh"
        )
        return "fresh"

    spectral = library_evidence.measure_trackfile_spectral(path)
    state_db.update_library_spectral(
        {
            "trackfile_id": trackfile_id,
            "album_id": album_id,
            "authentic": spectral.authentic,
            "spectral_status": spectral.status,
            "spectral_reason": spectral.reason,
            "spectral_verdict": spectral.verdict,
            "spectral_sensor_version": library_evidence.SPECTRAL_SENSOR_VERSION,
        }
    )
    state = (
        "spectral_measured" if spectral.status == "measured" else "spectral_unmeasured"
    )
    state_db.upsert_library_scan_item(
        run_id, trackfile_id, album_id=album_id, state=state
    )
    return "measured" if spectral.status == "measured" else "unmeasured"


def _update_scan_progress(
    job_id: int, run_id: int, mode: str, counters: dict, total_items: int
) -> None:
    pct = int((counters["processed_items"] / max(1, total_items)) * 100)
    state_db.update_job_progress(
        job_id,
        {
            "stage": "library_scan",
            "percent": pct,
            "message": (
                f"Scanned {counters['processed_items']}/{total_items} "
                "library trackfiles"
            ),
            "mode": mode,
        },
    )
    state_db.update_library_scan_run_state(run_id, "running", totals=counters)
    state_db.heartbeat_job(job_id)


def _count_scan_outcome(counters: dict, outcome: str) -> None:
    if outcome == "fresh":
        counters["fresh_items"] += 1
    elif outcome == "measured":
        counters["measured_items"] += 1
    else:
        counters["unmeasured_items"] += 1


def _execute_cheap_scan_items(
    job_id: int, run_id: int, trackfiles: list[dict], counters: dict
) -> None:
    workers = _cheap_scan_workers()
    total_items = len(trackfiles)
    pending: dict[Future[str], tuple[int, int | None]] = {}
    next_index = 0
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="library-cheap-scan"
    ) as pool:
        while pending or next_index < total_items:
            while len(pending) < workers and next_index < total_items:
                _raise_if_cancelled(job_id, run_id)
                _wait_for_import_quiet(job_id, run_id)
                tf = trackfiles[next_index]
                next_index += 1
                trackfile_id = tf.get("id")
                album_id_raw = tf.get("albumId") or tf.get("album_id")
                album_id = int(album_id_raw) if album_id_raw is not None else None
                if trackfile_id is None:
                    counters["unmeasured_items"] += 1
                    counters["processed_items"] += 1
                    _update_scan_progress(
                        job_id, run_id, "cheap", counters, total_items
                    )
                    continue
                trackfile_id_int = int(trackfile_id)
                state_db.upsert_library_scan_item(
                    run_id, trackfile_id_int, album_id=album_id, state="measuring"
                )
                future = pool.submit(_measure_trackfile_item, run_id, tf)
                pending[future] = (trackfile_id_int, album_id)

            if not pending:
                continue

            done, _not_done = wait(
                pending.keys(), timeout=1.0, return_when=FIRST_COMPLETED
            )
            if not done:
                state_db.heartbeat_job(job_id)
                _raise_if_cancelled(job_id, run_id)
                continue

            for future in done:
                trackfile_id, album_id = pending.pop(future)
                try:
                    _count_scan_outcome(counters, future.result())
                except Exception as exc:
                    counters["error_items"] += 1
                    state_db.upsert_library_scan_item(
                        run_id,
                        trackfile_id,
                        album_id=album_id,
                        state="error",
                        last_error=str(exc)[:1000],
                    )
                    log.exception(
                        "library scan item failed trackfile_id=%s", trackfile_id
                    )
                finally:
                    counters["processed_items"] += 1
                    _update_scan_progress(
                        job_id, run_id, "cheap", counters, total_items
                    )


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
        if mode == "spectral_missing" and not _background_spectral_enabled():
            raise RuntimeError("background spectral scan disabled")
        state_db.update_library_scan_run_state(run_id, "running")
        state_db.heartbeat_job(job_id)
        trackfiles = _fetch_lidarr_trackfiles()
        counters["total_items"] = len(trackfiles)
        state_db.update_library_scan_run_state(run_id, "running", totals=counters)

        if mode != "spectral_missing":
            _execute_cheap_scan_items(job_id, run_id, trackfiles, counters)
            state_db.update_library_scan_run_state(run_id, "completed", totals=counters)
            state_db.mark_job_completed(
                job_id,
                result_state="library_scan_completed",
                result={"run_id": run_id, **counters},
            )
            return

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
                if mode == "spectral_missing":
                    prior_item = state_db.get_library_scan_item(
                        run_id, int(trackfile_id)
                    )
                    if (
                        prior_item
                        and prior_item.get("state") in _SPECTRAL_TERMINAL_ITEM_STATES
                    ):
                        outcome = "fresh"
                    else:
                        state_db.upsert_library_scan_item(
                            run_id,
                            int(trackfile_id),
                            album_id=album_id,
                            state="spectral_measuring",
                        )
                        # Explicit contention rule: never start a background
                        # Detective request while import work is already waiting.
                        # If import work appears after the HTTP request begins, v1
                        # allows one in-flight file to finish; the next item yields.
                        _wait_for_import_quiet(job_id, run_id)
                        state_db.heartbeat_job(job_id, lease_sec=_SPECTRAL_LEASE_SEC)
                        outcome = _measure_spectral_item(run_id, tf)
                else:
                    state_db.upsert_library_scan_item(
                        run_id, int(trackfile_id), album_id=album_id, state="measuring"
                    )
                    outcome = _measure_trackfile_item(run_id, tf)
                _count_scan_outcome(counters, outcome)
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
                _update_scan_progress(job_id, run_id, mode, counters, len(trackfiles))

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
