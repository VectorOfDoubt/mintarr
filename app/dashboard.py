"""Dashboard endpoints (JSON API) + HTML page.

Implements TIDALHIRES_DASHBOARD_API.md v1 endpoints:
- GET /dashboard/v1/summary
- GET /dashboard/v1/records
- GET /dashboard/v1/record/<jid>
- GET /dashboard/v1/connectors
- GET /dashboard/v1/timings
- GET /dashboard/v1/library-quality
- GET /dashboard/v1/audio-sample/<jid>
- GET /dashboard/v1/spectrum/<jid>
- POST /dashboard/v1/action/<jid>
- GET /dashboard (HTML page, server-rendered shell + JS for interactivity)
"""

from __future__ import annotations

import subprocess
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from flask import Blueprint, Response, jsonify, render_template, request, send_file

from dashboard_cache import get_or_compute, invalidate_prefix

if TYPE_CHECKING:
    pass

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

_LIDARR_COMMAND_THRESHOLDS_SEC = {
    "ManualImport": 5 * 60,
    "ProcessMonitoredDownloads": 10 * 60,
    "RescanFolders": 30 * 60,
    "RescanFolder": 15 * 60,
    "RefreshArtist": 15 * 60,
}
_LIDARR_COMMAND_ACTIVE_STATES = {"queued", "started"}


# ---------- Status derivation per STRATEGY §3 ----------
def derive_status(rec: dict) -> str:
    state = rec.get("verification_state") or (rec.get("lifecycle") or {}).get(
        "state", ""
    )
    decision = rec.get("v2_verification_decision", "")
    outcome = rec.get("v2_import_outcome", "")
    if state == "discarded":
        return "discarded"
    if state == "expired":
        return "expired"
    if state == "promoted":
        return "promoted"
    if decision == "REVIEW_REQUIRED" and state == "pending_review":
        return "needs_review"
    if decision == "BLOCK" and outcome in ("MANUAL_IMPORTED", "RESCUED"):
        return "policy_violation"
    if outcome == "FAILED":
        return "failed"
    if outcome == "PENDING":
        return "pending"
    if outcome in ("MANUAL_IMPORTED", "RESCUED"):
        return "imported"
    if decision == "BLOCK":
        return "blocked"
    return "unknown"


# ---------- Available actions per STRATEGY §4.5 ----------
def available_actions(rec: dict) -> list[str]:
    state = rec.get("verification_state") or (rec.get("lifecycle") or {}).get(
        "state", ""
    )
    decision = rec.get("v2_verification_decision", "")
    outcome = rec.get("v2_import_outcome", "")
    identity_decision = rec.get("release_identity_decision", "")
    if state in ("discarded", "expired"):
        return []
    if outcome in ("MANUAL_IMPORTED", "RESCUED"):
        return []
    if decision == "REVIEW_REQUIRED" and state == "pending_review":
        actions = ["promote", "discard"]
        if identity_decision in {"AMBIGUOUS_EDITION", "INSUFFICIENT_EVIDENCE"}:
            try:
                import server

                if server._release_switch_strategy() == "review":
                    actions.append("apply_release_switch")
            except Exception:
                pass
        return actions
    if outcome == "FAILED":
        return ["retry_import", "discard"]
    if outcome == "PENDING":
        return ["retry_import"]
    if decision == "BLOCK":
        return ["discard"]
    return []


# ---------- Why-explanation for REVIEW_REQUIRED ----------
def _review_reason(rec: dict) -> str:
    overrides = rec.get("v2_overrides") or []
    verdict = rec.get("verdict") or ""
    if "fake_hi_res" in overrides:
        return "Looks like upsampled hi-res: useful high-frequency content stops at the file's technical ceiling."
    if "codec_mismatch" in overrides:
        return "Downloaded files are not FLAC"
    if "flac_t_fail" in overrides:
        return "FLAC integrity check failed"
    if "validator_error" in overrides:
        return "Validator unavailable during analysis"
    if "edition_tracklist_mismatch" in overrides:
        return (
            "Edition/tracklist mismatch: the candidate has many more tracks than the "
            "tracked release — likely a different edition (deluxe/anniversary). "
            "Confirm before it replaces the current edition."
        )
    if "track_count_undercount" in overrides:
        return (
            "Track-count mismatch: the candidate has fewer tracks than the tracked "
            "release. Confirm before it replaces a more complete library copy."
        )
    if verdict in ("FAKE_CERTAIN", "FAKE"):
        return "Detective: certain MP3-to-FLAC transcode"
    if verdict == "SUSPICIOUS":
        return "Detective: suspicious — likely fake lossless"
    return "Review required by policy"


def status_reason(rec: dict) -> str:
    """Human-readable reason for the derived dashboard status."""
    status = derive_status(rec)
    decision = rec.get("v2_verification_decision", "")
    outcome = rec.get("v2_import_outcome", "")
    reason = rec.get("reason") or ""
    job_error = rec.get("job_error") or ""
    job_warning = rec.get("job_warning") or ""
    verdict = rec.get("verdict") or ""
    lifecycle = rec.get("lifecycle") or {}
    overrides = rec.get("v2_overrides") or []
    sensors = rec.get("sensors") or []

    def _sensor_evidence(name: str) -> dict:
        for sensor in sensors:
            if sensor.get("name") == name:
                return sensor.get("evidence") or {}
        return {}

    if status == "needs_review":
        return _review_reason(rec)
    if status == "imported":
        if outcome == "RESCUED":
            return "Imported by rescue flow after Lidarr ManualImport did not complete cleanly."
        if decision == "ACCEPT_PROVISIONAL":
            if reason == "nothing pre-existing":
                return "Imported provisionally because no existing copy was present."
            if reason.startswith("upgrade from"):
                return f"Imported provisionally as an {reason}."
            if reason.startswith("score="):
                return "Imported provisionally because the score was below full-accept threshold."
            return f"Imported provisionally: {reason or 'allowed by upgrade/completeness policy'}."
        return "Imported after quality checks passed."
    if status == "failed":
        if job_error:
            if job_error == "manual promote import failed":
                return (
                    "Import failed after QC passed: Lidarr ManualImport did not confirm any imported files. "
                    "Open the record for Lidarr context, then retry import or discard."
                )
            return f"Import failed: {job_error}"
        if reason and not (
            reason == "nothing pre-existing"
            or reason.startswith("upgrade from")
            or reason.startswith("score=")
        ):
            return f"Import failed: {reason}"
        if job_warning:
            return f"Import failed after warning: {job_warning}"
        return "Import failed after QC passed; check Lidarr context/history for the exact import rejection."
    if status == "pending":
        return "Lidarr ManualImport is still pending; retry is available if it does not settle."
    if status == "blocked":
        if "codec_mismatch" in overrides:
            ffprobe = _sensor_evidence("ffprobe")
            skipped = int(ffprobe.get("codec_gate_skipped") or 0)
            flac_count = int(ffprobe.get("flac_count") or 0)
            if skipped > 0 and flac_count == 0:
                return (
                    f"Skipped before import: the release was advertised as FLAC, "
                    f"but the download contained {skipped} non-FLAC audio file(s). "
                    "All were stopped by the codec gate, so no FLAC files remained for import."
                )
            if skipped > 0:
                return f"Blocked by policy: {skipped} downloaded file(s) failed the FLAC/ALAC codec gate."
            return "Blocked by policy: downloaded files did not match the expected FLAC/ALAC codec."
        if reason:
            return f"Blocked by policy: {reason}."
        if verdict:
            return f"Blocked by policy after Detective verdict {verdict}."
        return "Blocked by quality policy before import."
    if status == "policy_violation":
        return "Policy violation: this record was imported even though V2 decided BLOCK. Keep it for audit and inspect Lidarr library/history."
    if status == "discarded":
        actor = lifecycle.get("actor")
        if actor == "user_discard":
            return "Discarded by user; files were removed and the grab was blocklisted when possible."
        return "Discarded; no further action is available."
    if status == "expired":
        return "Review window expired; item was blocklisted and hidden."
    if status == "promoted":
        return "Promoted by user after manual review."
    return "No status explanation available."


# ---------- /dashboard/v1/summary ----------
def _parse_lidarr_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except Exception:
        return None


def _lidarr_command_age_sec(command: dict, now: float) -> int | None:
    started = _parse_lidarr_ts(command.get("started"))
    queued = _parse_lidarr_ts(command.get("queued"))
    base = started or queued
    if base is None:
        return None
    return max(0, int(now - base))


def _lidarr_command_blocking_reason(
    command: dict, age_sec: int | None, has_started_rescan: bool
) -> str | None:
    status = str(command.get("status") or "").lower()
    if status not in _LIDARR_COMMAND_ACTIVE_STATES:
        return None

    name = str(command.get("name") or "")
    if (
        name in {"ManualImport", "ProcessMonitoredDownloads"}
        and status == "queued"
        and has_started_rescan
    ):
        return f"{name} is queued behind a started RescanFolders command."
    threshold = _LIDARR_COMMAND_THRESHOLDS_SEC.get(name)
    if threshold is not None and age_sec is not None and age_sec >= threshold:
        return f"{name} has been {status} for {int(age_sec / 60)}m."
    return None


def _build_lidarr_command_health(server_mod, api: str, lkey: str) -> dict:
    import requests

    response = requests.get(f"{api}/command", headers={"X-Api-Key": lkey}, timeout=5)
    if not response.ok:
        return {
            "status": "degraded",
            "active_count": 0,
            "blocking_count": 0,
            "commands": [],
            "error": f"HTTP {response.status_code}",
        }

    now = time.time()
    commands = response.json() or []
    has_started_rescan = any(
        command.get("name") == "RescanFolders"
        and str(command.get("status") or "").lower() == "started"
        for command in commands
    )
    active = []
    for command in commands:
        status = str(command.get("status") or "").lower()
        if status not in _LIDARR_COMMAND_ACTIVE_STATES:
            continue
        age_sec = _lidarr_command_age_sec(command, now)
        reason = _lidarr_command_blocking_reason(command, age_sec, has_started_rescan)
        active.append(
            {
                "id": command.get("id"),
                "name": command.get("name"),
                "status": status,
                "message": command.get("message"),
                "queued": command.get("queued"),
                "started": command.get("started"),
                "age_sec": age_sec,
                "blocking": reason is not None,
                "blocking_reason": reason,
            }
        )

    blocking_count = sum(1 for item in active if item["blocking"])
    return {
        "status": "blocked" if blocking_count else "ok",
        "active_count": len(active),
        "blocking_count": blocking_count,
        "commands": active[:10],
        "error": None,
    }


def _build_summary(server_mod) -> dict:
    # Summary still derives from sidecar-scan: sidecars are source-of-truth and certain
    # mutation paths (test fixtures, direct file-edits) bypass the DB wire-in.
    # The records endpoint uses DB for performance. Summary sidecar scanning is
    # still acceptable at current scale; switch to DB counts if it bottlenecks.
    rows = server_mod._verification_records()
    rows = [server_mod._decision_with_current_verification_state(r) for r in rows]
    decisions = Counter(
        r.get("v2_verification_decision")
        for r in rows
        if r.get("v2_verification_decision")
    )
    statuses = Counter(derive_status(r) for r in rows)
    total_records = len(rows)

    with server_mod._jobs_lock:
        active = sum(
            1
            for j in server_mod._jobs.values()
            if j.get("status") in ("queued", "downloading", "processing")
        )
        sab_queue = sum(
            1
            for j in server_mod._jobs.values()
            if j.get("status") in ("queued", "downloading", "processing")
            and not j.get("hidden_from_lidarr")
        )

    # Lidarr health (best-effort, fail-open)
    lidarr_status = "unknown"
    lidarr_queue = None
    lidarr_commands = {
        "status": "unknown",
        "active_count": 0,
        "blocking_count": 0,
        "commands": [],
        "error": None,
    }
    try:
        import os
        import requests

        api = os.environ.get(
            "LIDARR_API_URL", "http://host.docker.internal:8686/api/v1"
        )
        lkey = server_mod._get_lidarr_key()
        if lkey:
            r = requests.get(
                f"{api}/queue?pageSize=1", headers={"X-Api-Key": lkey}, timeout=3
            )
            if r.ok:
                lidarr_status = "ok"
                lidarr_queue = r.json().get("totalRecords", 0)
            else:
                lidarr_status = "degraded"
            lidarr_commands = _build_lidarr_command_health(server_mod, api, lkey)
            if lidarr_commands["blocking_count"]:
                lidarr_status = "blocked"
    except Exception:
        lidarr_status = "unreachable"
        lidarr_commands["status"] = "unreachable"

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "counts": {
            "total_decisions": total_records,
            "imported": statuses.get("imported", 0),
            "needs_review": statuses.get("needs_review", 0),
            "pending": statuses.get("pending", 0),
            "failed": statuses.get("failed", 0),
            "policy_violations": statuses.get("policy_violation", 0),
            "blocked": decisions.get("BLOCK", 0),
            "discarded": statuses.get("discarded", 0),
            "expired": statuses.get("expired", 0),
            "active_jobs": active,
        },
        "stack_health": {
            "tidalhires": "ok",
            "flac_detective": _check_flac_detective(),
            "lidarr": lidarr_status,
        },
        "queue": {
            "sab_emulated": sab_queue,
            "lidarr_queue_total": lidarr_queue,
            "lidarr_commands": lidarr_commands,
        },
    }


def _check_flac_detective() -> str:
    import os
    import requests

    url = os.environ.get("FLAC_API_URL", "http://host.docker.internal:8889/analyze")
    try:
        # /health is on root, /analyze is what we use — derive health-url
        health_url = url.rsplit("/", 1)[0] + "/health"
        r = requests.get(health_url, timeout=2)
        return "ok" if r.ok else "degraded"
    except Exception:
        return "unreachable"


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _stage_stats(values: list[float]) -> dict:
    ordered = sorted(v for v in values if isinstance(v, (int, float)) and v >= 0)
    if not ordered:
        return {"median": 0, "p95": 0, "max": 0, "fastest": 0, "count": 0}
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "median": round(median, 3),
        "p95": _percentile(ordered, 0.95),
        "max": round(max(ordered), 3),
        "fastest": round(min(ordered), 3),
        "count": len(ordered),
    }


def _timing_window_seconds(window: str) -> int:
    return {
        "1h": 3600,
        "24h": 86400,
        "7d": 7 * 86400,
        "30d": 30 * 86400,
    }.get(window, 7 * 86400)


def _record_timings_with_ts(server_mod) -> list[tuple[float, dict]]:
    rows = server_mod._verification_records()
    by_jid = {}
    for row in rows:
        jid = str(row.get("jid") or "")
        if not jid:
            continue
        timings = row.get("timings") or {}
        if timings:
            by_jid[jid] = (float(row.get("ts") or 0), dict(timings))

    with server_mod._jobs_lock:
        for jid, job in server_mod._jobs.items():
            timings = (job or {}).get("timings") or {}
            if timings:
                ts = float(
                    job.get("completed_at") or job.get("created_at") or time.time()
                )
                by_jid[str(jid)] = (ts, dict(timings))
    return list(by_jid.values())


def _build_timings(server_mod, window: str = "7d", stage: str | None = None) -> dict:
    now = time.time()
    cutoff = now - _timing_window_seconds(window)
    samples = [
        (ts, timings)
        for ts, timings in _record_timings_with_ts(server_mod)
        if ts >= cutoff
    ]
    previous_cutoff = cutoff - _timing_window_seconds(window)
    previous = [
        (ts, timings)
        for ts, timings in _record_timings_with_ts(server_mod)
        if previous_cutoff <= ts < cutoff
    ]

    stage_values: dict[str, list[float]] = {}
    for _, timings in samples:
        for name, value in timings.items():
            if stage and name != stage:
                continue
            if isinstance(value, (int, float)):
                stage_values.setdefault(name, []).append(float(value))

    previous_totals = [
        float(t["pre_import_total_sec"])
        for _, t in previous
        if isinstance(t.get("pre_import_total_sec"), (int, float))
    ]
    current_totals = stage_values.get("pre_import_total_sec", [])
    current_median = _stage_stats(current_totals)["median"] if current_totals else 0
    previous_median = _stage_stats(previous_totals)["median"] if previous_totals else 0
    regression = bool(previous_median and current_median > previous_median * 1.5)

    return {
        "window": window,
        "sample_count": len(samples),
        "stages": {
            name: _stage_stats(values) for name, values in sorted(stage_values.items())
        },
        "regression_flag": regression,
        "regression_reason": (
            f"pre_import_total_sec median {current_median}s is >1.5x previous {previous_median}s"
            if regression
            else None
        ),
    }


def _lidarr_web_base() -> str:
    import os

    return os.environ.get("LIDARR_WEB_URL", "http://127.0.0.1:8686").rstrip("/")


def _lidarr_api_request(
    server_mod, path: str, *, params: dict | None = None, timeout: int = 5
):
    import os
    import requests

    api = os.environ.get(
        "LIDARR_API_URL", "http://host.docker.internal:8686/api/v1"
    ).rstrip("/")
    key = server_mod._get_lidarr_key()
    if not key:
        raise RuntimeError("lidarr api key unavailable")
    return requests.get(
        f"{api}{path}", params=params, headers={"X-Api-Key": key}, timeout=timeout
    )


def _history_reason(item: dict) -> str | None:
    if item.get("sourceTitle"):
        return item.get("sourceTitle")
    data = item.get("data") or {}
    for key in ("reason", "message", "importDecision"):
        if data.get(key):
            return str(data[key])
    return None


def _history_success(item: dict) -> bool:
    event = str(item.get("eventType") or "").lower()
    return event in {"downloadfolderimported", "trackfileimported", "imported"}


def _build_lidarr_context(server_mod, jid: str) -> tuple[dict, int]:
    _, sidecar = server_mod._read_verification_sidecar(jid)
    if sidecar is None:
        return {"error": "record not found", "jid": jid}, 404

    album_ids = [int(a) for a in (sidecar.get("album_ids") or []) if str(a).isdigit()]
    id_set = set(album_ids)
    albums = []
    queue_entries = []
    history_items = []

    try:
        for aid in album_ids[:5]:
            r = _lidarr_api_request(server_mod, f"/album/{aid}", timeout=5)
            if not r.ok:
                continue
            album = r.json()
            stats = album.get("statistics") or {}
            releases = album.get("releases") or []
            current = album.get("currentRelease") or next(
                (rel for rel in releases if rel.get("monitored")), {}
            )
            albums.append(
                {
                    "id": album.get("id") or aid,
                    "title": album.get("title") or album.get("albumTitle"),
                    "artist_name": (album.get("artist") or {}).get("artistName")
                    or album.get("artistName"),
                    "track_count": album.get("trackCount")
                    or stats.get("trackCount")
                    or stats.get("totalTrackCount")
                    or current.get("trackCount"),
                    "track_file_count": album.get("trackFileCount")
                    or stats.get("trackFileCount"),
                    "monitored": album.get("monitored"),
                    "quality_profile_id": album.get("qualityProfileId")
                    or album.get("profileId"),
                    "release_title": current.get("title"),
                    "release_track_count": current.get("trackCount"),
                    "url": f"{_lidarr_web_base()}/album/{album.get('id') or aid}",
                }
            )

        q = _lidarr_api_request(
            server_mod, "/queue", params={"pageSize": 200}, timeout=5
        )
        if q.ok:
            for entry in q.json().get("records", []):
                entry_album_id = entry.get("albumId") or (entry.get("album") or {}).get(
                    "id"
                )
                download_id = str(entry.get("downloadId") or "")
                if entry_album_id in id_set or download_id.lower() == jid.lower():
                    queue_entries.append(
                        {
                            "id": entry.get("id"),
                            "title": entry.get("title"),
                            "download_id": download_id,
                            "status": entry.get("status"),
                            "tracked_download_status": entry.get(
                                "trackedDownloadStatus"
                            ),
                            "tracked_download_state": entry.get("trackedDownloadState"),
                            "timeleft": entry.get("timeleft"),
                            "sizeleft": entry.get("sizeleft"),
                        }
                    )

        h = _lidarr_api_request(
            server_mod,
            "/history",
            params={"pageSize": 100, "sortKey": "date", "sortDirection": "descending"},
            timeout=8,
        )
        if h.ok:
            records = h.json().get("records", [])
            for item in records:
                item_album_id = item.get("albumId") or (item.get("album") or {}).get(
                    "id"
                )
                download_id = str(item.get("downloadId") or "")
                if item_album_id in id_set or download_id.lower() == jid.lower():
                    history_items.append(
                        {
                            "ts": item.get("date"),
                            "event_type": item.get("eventType"),
                            "indexer": item.get("indexer"),
                            "download_id": download_id,
                            "successful": _history_success(item),
                            "reason": _history_reason(item),
                        }
                    )
                if len(history_items) >= 10:
                    break
    except Exception:
        return {"error": "lidarr unavailable", "retry_after_sec": 60}, 503

    return {
        "jid": jid,
        "album_ids": album_ids,
        "album": albums[0] if albums else None,
        "albums": albums,
        "queue": {
            "in_queue": bool(queue_entries),
            "queue_entries": queue_entries,
        },
        "grab_history": history_items,
    }, 200


def _dashboard_media_dir() -> Path:
    path = Path("/config/dashboard_media")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _contained_output_dir(server_mod, jid: str) -> Path | None:
    with server_mod._jobs_lock:
        output_dir = server_mod._jobs.get(jid, {}).get("output_dir")
    candidate = Path(output_dir) if output_dir else server_mod.OUTPUT_BASE / jid
    try:
        resolved = candidate.resolve()
        base = server_mod.OUTPUT_BASE.resolve()
    except OSError:
        return None
    if resolved == base or not resolved.is_relative_to(base):
        return None
    return resolved if resolved.exists() else None


def _first_audio_file(server_mod, jid: str) -> Path | None:
    output_dir = _contained_output_dir(server_mod, jid)
    if output_dir is None:
        return None
    for ext in ("*.flac", "*.m4a", "*.wav", "*.alac"):
        matches = sorted(output_dir.rglob(ext))
        if matches:
            return matches[0]
    return None


def _media_review_info(server_mod, jid: str, derived_status: str) -> dict:
    """Whether dashboard audio/spectrum review should be offered for a record."""
    audio_file = _first_audio_file(server_mod, jid)
    files_present = audio_file is not None
    terminal_without_review = {
        "imported",
        "promoted",
        "discarded",
        "expired",
        "blocked",
    }
    review_relevant = derived_status not in terminal_without_review

    if not review_relevant:
        if derived_status in {"imported", "promoted"}:
            reason = "Audio review is hidden for imported records; use Lidarr or the library player."
        elif derived_status in {"discarded", "expired"}:
            reason = "Audio review is unavailable because this review item is closed."
        else:
            reason = "Audio review is not relevant for this policy-blocked record."
    elif not files_present:
        reason = "Audio review is unavailable because files are not retained for this record."
    else:
        reason = "Audio review is available for retained, non-imported files."

    return {
        "available": bool(files_present and review_relevant),
        "files_present": files_present,
        "review_relevant": review_relevant,
        "reason": reason,
    }


def _media_artifact(server_mod, jid: str, kind: str) -> tuple[Path | None, str | None]:
    source = _first_audio_file(server_mod, jid)
    if source is None:
        return None, "no audio file available"

    media_dir = _dashboard_media_dir()
    if kind == "audio":
        target = media_dir / f"{jid}.sample.mp3"
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0",
            "-t",
            "20",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-b:a",
            "192k",
            str(target),
        ]
    elif kind == "spectrum":
        target = media_dir / f"{jid}.spectrum.v2.png"
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0",
            "-t",
            "45",
            "-i",
            str(source),
            "-lavfi",
            "showspectrumpic=s=1280x480:legend=enabled",
            "-frames:v",
            "1",
            str(target),
        ]
    else:
        return None, "unknown media kind"

    try:
        if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
            return target, None
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            return None, (result.stderr or "ffmpeg produced no output")[-300:]
        return target, None
    except Exception as exc:
        return None, str(exc)


@dashboard_bp.route("/v1/summary", methods=["GET"])
def summary():
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import server

    data = get_or_compute(("summary",), 10.0, lambda: _build_summary(server))
    return jsonify(data)


@dashboard_bp.route("/v1/connectors", methods=["GET"])
def connectors():
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import connectors as connector_registry

    return jsonify(connector_registry.registry_payload())


@dashboard_bp.route("/v1/connectors/<connector_id>/config", methods=["POST"])
def connector_config(connector_id: str):
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp

    import connectors as connector_registry
    import state_db

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run"))
    enabled = body.get("enabled") if "enabled" in body else None
    mode = body.get("mode") if "mode" in body else None
    proposed, errors = connector_registry.validate_connector_update(
        connector_id,
        enabled=enabled,
        mode=mode,
        connectors=connector_registry.all_connectors(),
    )
    if proposed is None and errors == ["unknown connector"]:
        return jsonify(
            {"connector_id": connector_id, "valid": False, "errors": errors}
        ), 404
    if errors:
        status = (
            400
            if any(error.startswith("invalid connector mode") for error in errors)
            else 409
        )
        state_db.log_action(
            f"connector:{connector_id}",
            "connector_config_dry_run" if dry_run else "connector_config",
            "user_dashboard",
            f"http_{status}",
            {"mode": mode, "enabled": enabled, "dry_run": dry_run, "errors": errors},
        )
        return jsonify(
            {
                "connector_id": connector_id,
                "dry_run": dry_run,
                "valid": False,
                "config": proposed,
                "errors": errors,
            }
        ), status

    assert proposed is not None
    if dry_run:
        state_db.log_action(
            f"connector:{connector_id}",
            "connector_config_dry_run",
            "user_dashboard",
            "ok",
            {"mode": proposed["mode"], "enabled": proposed["enabled"]},
        )
        return jsonify(
            {
                "connector_id": connector_id,
                "dry_run": True,
                "valid": True,
                "config": proposed,
                "errors": [],
            }
        )

    saved = connector_registry.persist_connector_config(proposed)
    if saved is None:
        return jsonify(
            {
                "connector_id": connector_id,
                "dry_run": False,
                "valid": False,
                "errors": ["failed to persist connector config"],
            }
        ), 500
    state_db.log_action(
        f"connector:{connector_id}",
        "connector_config",
        "user_dashboard",
        "ok",
        {"mode": saved["mode"], "enabled": saved["enabled"]},
    )
    invalidate_prefix("summary")
    invalidate_prefix("connectors")
    return jsonify(
        {
            "connector_id": connector_id,
            "dry_run": False,
            "valid": True,
            "config": saved,
            "errors": [],
        }
    )


# ---------- /dashboard/v1/records ----------
def _build_records_from_db(server_mod, filters: dict) -> dict | None:
    """F1.6: try DB-backed query first. Returns None to signal fallback to sidecar-scan."""
    import json as _json
    import time as _time

    try:
        import state_db

        decisions = filters["decision"].split(",") if filters.get("decision") else None
        outcomes = filters["outcome"].split(",") if filters.get("outcome") else None
        states = filters["state"].split(",") if filters.get("state") else None
        statuses = filters["status"].split(",") if filters.get("status") else None

        offset = int(filters.get("offset", 0))
        limit = min(int(filters.get("limit", 100)), 500)
        total, rows = state_db.list_records(
            decision=decisions,
            outcome=outcomes,
            state=states,
            status=statuses,
            limit=limit,
            offset=offset,
            sort=filters.get("sort", "ts_desc"),
        )
        # If DB is empty, return None so caller falls back to sidecar scan
        # (handles case where backfill not yet run)
        if total == 0 and not any([decisions, outcomes, states, statuses]):
            return None

        with server_mod._jobs_lock:
            jobs_by_jid = {str(jid): dict(job) for jid, job in server_mod._jobs.items()}

        out = []
        for r in rows:
            ts = r.get("created_at")
            ts_iso = (
                _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(ts)) if ts else None
            )
            album_ids = (
                _json.loads(r["album_ids_json"]) if r.get("album_ids_json") else []
            )
            # Adapter: bygg sidecar-like dict slik at status_reason() + derive_status virker
            sidecar_like: dict[str, object] = {
                "v2_verification_decision": r.get("verification_decision"),
                "v2_import_outcome": r.get("import_outcome"),
                "verdict": r.get("verdict"),
                "v2_overrides": [],
                "reason": "",
                "lifecycle": {
                    "state": r.get("lifecycle_state"),
                    "actor": r.get("actor"),
                },
            }
            # DB rows are intentionally narrow. Pull the sidecar when available so
            # table-row reasons can use overrides/sensor evidence instead of
            # falling back to vague verdict-only text such as UNKNOWN.
            try:
                _, sidecar = server_mod._read_verification_sidecar(str(r.get("jid")))
                if sidecar:
                    sidecar_like.update(
                        {
                            "v2_overrides": sidecar.get("v2_overrides") or [],
                            "reason": sidecar.get("reason") or "",
                            "sensors": sidecar.get("sensors") or [],
                            "files": sidecar.get("files") or [],
                        }
                    )
            except Exception:
                pass
            # Berik med job-runtime info hvis tilgjengelig (job_error/warning)
            job = jobs_by_jid.get(str(r.get("jid"))) or {}
            if job.get("error"):
                sidecar_like["job_error"] = job.get("error")
            if job.get("warning"):
                sidecar_like["job_warning"] = job.get("warning")
            out.append(
                {
                    "jid": r["jid"],
                    "ts": ts,
                    "ts_iso": ts_iso,
                    "title": r.get("title"),
                    "verification_decision": r.get("verification_decision"),
                    "import_outcome": r.get("import_outcome"),
                    "lifecycle_state": r.get("lifecycle_state"),
                    "score": r.get("score"),
                    "verdict": r.get("verdict"),
                    "overrides": sidecar_like.get("v2_overrides") or [],
                    "needs_action": r.get("derived_status") == "needs_review",
                    "derived_status": r.get("derived_status"),
                    "status_reason": status_reason(sidecar_like),
                    "album_ids": album_ids,
                    "_source": "db",
                }
            )
        return {"total": total, "returned": len(out), "offset": offset, "records": out}
    except Exception:
        # Defensive: DB-query failures fall back to sidecar scanning.
        return None


def _build_records(server_mod, filters: dict) -> dict:
    # F1.6: try DB first, fall back to sidecar-scan if DB empty or fails
    db_result = _build_records_from_db(server_mod, filters)
    if db_result is not None:
        return db_result

    rows = server_mod._verification_records()
    rows = [server_mod._decision_with_current_verification_state(r) for r in rows]
    with server_mod._jobs_lock:
        jobs_by_jid = {str(jid): dict(job) for jid, job in server_mod._jobs.items()}

    # Filters
    if filters.get("decision"):
        wanted = set(filters["decision"].split(","))
        rows = [r for r in rows if r.get("v2_verification_decision") in wanted]
    if filters.get("outcome"):
        wanted = set(filters["outcome"].split(","))
        rows = [r for r in rows if r.get("v2_import_outcome") in wanted]
    if filters.get("state"):
        wanted = set(filters["state"].split(","))
        rows = [
            r
            for r in rows
            if (r.get("verification_state") or (r.get("lifecycle") or {}).get("state"))
            in wanted
        ]
    if filters.get("status"):
        wanted = set(filters["status"].split(","))
        rows = [r for r in rows if derive_status(r) in wanted]

    # Sort
    sort = filters.get("sort", "ts_desc")
    if sort == "ts_asc":
        rows.sort(key=lambda r: r.get("ts", 0))
    elif sort == "score_desc":
        rows.sort(key=lambda r: r.get("v2_score", 0), reverse=True)
    else:
        rows.sort(key=lambda r: r.get("ts", 0), reverse=True)

    total = len(rows)
    offset = int(filters.get("offset", 0))
    limit = min(int(filters.get("limit", 100)), 500)
    rows = rows[offset : offset + limit]

    out = []
    for r in rows:
        job = jobs_by_jid.get(str(r.get("jid"))) or {}
        enriched = dict(r)
        if job.get("error"):
            enriched["job_error"] = job.get("error")
        if job.get("warning"):
            enriched["job_warning"] = job.get("warning")
        out.append(
            {
                "jid": r.get("jid"),
                "ts": r.get("ts"),
                "ts_iso": r.get("ts_iso"),
                "title": r.get("title"),
                "verification_decision": r.get("v2_verification_decision"),
                "import_outcome": r.get("v2_import_outcome"),
                "lifecycle_state": r.get("verification_state")
                or (r.get("lifecycle") or {}).get("state"),
                "score": r.get("v2_score"),
                "verdict": r.get("verdict"),
                "overrides": r.get("v2_overrides") or [],
                "needs_action": derive_status(r) == "needs_review",
                "derived_status": derive_status(r),
                "status_reason": status_reason(enriched),
                "album_ids": r.get("album_ids") or [],
            }
        )
    return {"total": total, "returned": len(out), "offset": offset, "records": out}


@dashboard_bp.route("/v1/records", methods=["GET"])
def records():
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import server

    filters = {
        k: request.args.get(k)
        for k in ("decision", "outcome", "state", "status", "sort", "offset", "limit")
    }
    filters = {k: v for k, v in filters.items() if v is not None}
    key = ("records", tuple(sorted(filters.items())))
    data = get_or_compute(key, 5.0, lambda: _build_records(server, filters))
    return jsonify(data)


# ---------- /dashboard/v1/actions/<jid> (NEW in F1.6) ----------
@dashboard_bp.route("/v1/actions/<jid>", methods=["GET"])
def actions_for_jid(jid: str):
    """Audit timeline for a single record — promote/discard/retry history."""
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    try:
        import state_db

        actions = state_db.list_actions(jid=jid, limit=50)
        return jsonify({"jid": jid, "actions": actions})
    except Exception:
        return jsonify({"jid": jid, "actions": []})


@dashboard_bp.route("/v1/actions/download", methods=["GET"])
def actions_download():
    """Download the audit trail as CSV (the 'download' half of the audit viewer)."""
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import csv
    import io

    try:
        import state_db

        actions = state_db.list_actions(limit=1000)
    except Exception:
        actions = []
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["created_at", "jid", "action", "actor", "result"])
    for a in actions:
        writer.writerow(
            [
                a.get("created_at", ""),
                a.get("jid", ""),
                a.get("action", ""),
                a.get("actor", ""),
                a.get("result", ""),
            ]
        )
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=mintarr-audit.csv"},
    )


@dashboard_bp.route("/v1/actions", methods=["GET"])
def actions_global():
    """Global action timeline (for dashboard activity feed)."""
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    try:
        import state_db

        limit = min(int(request.args.get("limit", 50)), 200)
        actions = state_db.list_actions(limit=limit)
        return jsonify({"actions": actions, "returned": len(actions)})
    except Exception:
        return jsonify({"actions": [], "returned": 0})


# ---------- F2.1: worker job endpoints ----------
def _job_to_payload(job: dict) -> dict:
    """Parse JSON-columns + present API-friendly representation."""
    if not job:
        return {}
    out = dict(job)
    for k in ("payload_json", "progress_json", "result_json"):
        raw = out.get(k)
        try:
            import json as _json

            out[k[:-5]] = _json.loads(raw) if raw else None
        except Exception:
            out[k[:-5]] = None
        out.pop(k, None)
    return out


@dashboard_bp.route("/v1/jobs", methods=["GET"])
def jobs_list():
    """List worker jobs with optional state/type/jid filtering."""
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    try:
        import state_db

        state = request.args.get("state")
        type_ = request.args.get("type")
        jid = request.args.get("jid")
        limit = min(int(request.args.get("limit", 50)), 500)
        offset = int(request.args.get("offset", 0))
        total, rows = state_db.list_jobs(
            state=state.split(",") if state else None,
            type=type_.split(",") if type_ else None,
            jid=jid,
            limit=limit,
            offset=offset,
        )
        return jsonify(
            {
                "total": total,
                "returned": len(rows),
                "offset": offset,
                "jobs": [_job_to_payload(r) for r in rows],
            }
        )
    except Exception:
        return jsonify({"total": 0, "returned": 0, "offset": 0, "jobs": []})


@dashboard_bp.route("/v1/queue/partial", methods=["GET"])
def queue_partial():
    """Server-rendered worker-queue partial for HTMX polling (Phase 2 slice 3).

    Returns an HTML fragment (not JSON) showing active jobs. The Queue section
    in the dashboard polls this with `hx-trigger`. Flask remains the source of
    truth per ADR-0011.
    """
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    active_states = ["queued", "running", "cancelling"]
    try:
        import state_db

        _total, rows = state_db.list_jobs(state=active_states, limit=100, offset=0)
        depth, _ = state_db.list_jobs(state=["queued"], limit=1, offset=0)
        jobs = [_queue_row(_job_to_payload(r)) for r in rows]
    except Exception:
        jobs, depth = [], 0
    return render_template("partials/queue.html", jobs=jobs, queue_depth=depth)


def _queue_row(job: dict) -> dict:
    """Flatten a job payload into the fields the queue partial renders."""
    progress = job.get("progress") or {}
    try:
        pct = int(progress.get("percent") or 0)
    except (TypeError, ValueError):
        pct = 0
    job["pct"] = max(0, min(100, pct))
    job["stage"] = progress.get("stage") or job.get("state") or ""
    job["message"] = progress.get("message") or ""
    job["jid_short"] = (job.get("jid") or "")[:12]
    return job


@dashboard_bp.route("/v1/history/partial", methods=["GET"])
def history_partial():
    """Server-rendered completed-job history partial for HTMX (Phase 2 slice 4).

    Returns an HTML fragment of recent terminal jobs. The History section polls
    this with `hx-trigger`. Flask remains the source of truth per ADR-0011.
    """
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    try:
        import state_db

        _total, rows = state_db.list_jobs(
            state=list(state_db.TERMINAL_JOB_STATES), limit=50, offset=0
        )
        jobs = [_history_row(_job_to_payload(r)) for r in rows]
    except Exception:
        jobs = []
    return render_template("partials/history.html", jobs=jobs)


def _history_row(job: dict) -> dict:
    """Flatten a terminal job payload into the fields the history partial renders."""
    job["jid_short"] = (job.get("jid") or "")[:12]
    job["result"] = job.get("result_state") or job.get("state") or ""
    ts = job.get("finished_at") or job.get("updated_at")
    try:
        job["finished_str"] = (
            datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M") if ts else "—"
        )
    except (TypeError, ValueError):
        job["finished_str"] = "—"
    return job


@dashboard_bp.route("/v1/system/partial", methods=["GET"])
def system_partial():
    """Server-rendered System overview partial for HTMX (Phase 2 slice 5).

    Reuses the cached summary (stack health + worker/queue counts) so polling
    does not re-hit Lidarr each tick. Flask stays the source of truth.
    """
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    try:
        import server
        import state_db

        data = get_or_compute(("summary",), 10.0, lambda: _build_summary(server))
        stack = data.get("stack_health") or {}
        queue = data.get("queue") or {}
        counts = data.get("counts") or {}
        workers = {
            "active_jobs": counts.get("active_jobs", 0),
            "sab_emulated": queue.get("sab_emulated", 0),
            "lidarr_queue": queue.get("lidarr_queue_total"),
        }
        events = [_audit_row(a) for a in state_db.list_actions(limit=12)]
    except Exception:
        stack = {}
        workers = {"active_jobs": 0, "sab_emulated": 0, "lidarr_queue": None}
        events = []
    return render_template(
        "partials/system.html", stack=stack, workers=workers, events=events
    )


def _restore_when(ts: object) -> str:
    """Format a restore timestamp (unix seconds) as a short UTC string."""
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(ts)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""


def _build_restore_panel(server_mod) -> dict:
    """Restore status for the System → Backup & Restore panel.

    Read-only: reflects the staged-restore marker and the last boot-apply
    outcome. The danger actions (stage/apply) stay on the API + restart; the
    dashboard only surfaces status, a backup download, and cancel.
    """
    try:
        import restore

        status = restore.restore_status(server_mod._restore_staging_dir())
        last = status.get("last_apply") or {}
        return {
            "enabled": bool(server_mod._restore_enabled()),
            "pending": bool(status.get("pending")),
            "restore_id": status.get("restore_id"),
            "state": status.get("state"),
            "created_when": _restore_when(status.get("created_at")),
            "last_apply_state": last.get("state"),
            "last_apply_detail": last.get("detail"),
            "last_apply_when": _restore_when(last.get("applied_at")),
        }
    except Exception:
        return {"enabled": False, "pending": False}


@dashboard_bp.route("/v1/restore/partial", methods=["GET"])
def restore_partial():
    """Server-rendered Backup & Restore panel for the System section."""
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import server

    return render_template(
        "partials/restore.html", restore=_build_restore_panel(server)
    )


@dashboard_bp.route("/v1/restore/cancel", methods=["POST"])
def restore_cancel():
    """Cancel a staged restore from the dashboard, then re-render the panel."""
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import server

    notice = None
    if not server._restore_enabled():
        notice = "Restore is disabled."
    else:
        try:
            import restore

            restore.cancel_staged_restore(server._restore_staging_dir())
            notice = "Staged restore cancelled."
        except Exception as exc:  # RestoreNotFoundError / RestoreStateError
            notice = str(exc)
    return render_template(
        "partials/restore.html", restore=_build_restore_panel(server), notice=notice
    )


def _audit_row(action: dict) -> dict:
    """Flatten an audit action into the fields the Events card renders."""
    out = dict(action)
    out["jid_short"] = (action.get("jid") or "")[:12]
    ts = action.get("created_at")
    try:
        out["when"] = (
            datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M") if ts else "—"
        )
    except (TypeError, ValueError):
        out["when"] = "—"
    return out


@dashboard_bp.route("/v1/jobs/<int:job_id>", methods=["GET"])
def job_detail(job_id: int):
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    try:
        import state_db

        job = state_db.get_job(job_id)
        if not job:
            return jsonify({"error": "job not found", "id": job_id}), 404
        return jsonify(_job_to_payload(job))
    except Exception:
        return jsonify({"error": "internal error"}), 500


@dashboard_bp.route("/v1/jobs/<int:job_id>/cancel", methods=["POST"])
def job_cancel(job_id: int):
    """Request cancellation — worker checks between stages."""
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    try:
        import state_db

        job = state_db.get_job(job_id)
        if not job:
            return jsonify({"error": "job not found", "id": job_id}), 404
        if job["state"] not in ("queued", "running", "cancelling"):
            return jsonify(
                {
                    "error": "cannot cancel terminal job",
                    "id": job_id,
                    "state": job["state"],
                }
            ), 409
        ok = state_db.request_job_cancel(job_id)
        current = state_db.get_job(job_id) or job
        return jsonify(
            {"id": job_id, "cancel_requested": ok, "state": current.get("state")}
        )
    except Exception:
        return jsonify({"error": "internal error"}), 500


# ---------- /dashboard/v1/record/<jid> ----------
def _basename_any(path: str | None) -> str | None:
    """Basename handling both POSIX and Windows separators.

    Unmeasured rows can carry the raw Lidarr path (e.g. a Windows
    ``H:\\Music\\...``); ``Path(...).name`` only strips ``/`` on Linux, so the
    full path would leak. Splitting on both ``/`` and ``\\`` keeps the
    basenames-only promise regardless of where Lidarr runs.
    """
    if not path:
        return None
    return path.replace("\\", "/").rsplit("/", 1)[-1] or None


def _library_evidence_detail(sidecar: dict) -> dict:
    """Measured existing-library quality for the record's album(s) (F5.4 slice 2).

    Read-only view over the library_evidence index. Exposes file basenames only
    (never full mount paths) for operator visibility in the drawer.
    """
    try:
        import state_db

        album_ids = [
            int(a) for a in (sidecar.get("album_ids") or []) if str(a).isdigit()
        ]
        rows: list[dict] = []
        for aid in album_ids:
            rows.extend(state_db.get_album_library_evidence(aid))
        if not rows:
            return {"available": False}

        tracks = []
        measured = 0
        for r in rows:
            if r.get("status") == "measured":
                measured += 1
            tracks.append(
                {
                    "filename": _basename_any(r.get("path")),
                    "status": r.get("status"),
                    "reason": r.get("reason"),
                    "codec": r.get("codec"),
                    "sample_rate": r.get("sample_rate"),
                    "bit_depth": r.get("bit_depth"),
                    "lossless": None
                    if r.get("lossless") is None
                    else bool(r.get("lossless")),
                    "integrity_ok": None
                    if r.get("integrity_ok") is None
                    else bool(r.get("integrity_ok")),
                    # F5.4 integrity split: stale FLAC MD5 (decodes, checksum failed).
                    "checksum_ok": None
                    if r.get("checksum_ok") is None
                    else bool(r.get("checksum_ok")),
                    "integrity_issue": r.get("integrity_issue"),
                    # F5.4 slice 4a: tri-state spectral authenticity (record-only).
                    "authentic": None
                    if r.get("authentic") is None
                    else bool(r.get("authentic")),
                    "spectral_status": r.get("spectral_status"),
                }
            )
        payload = {
            "available": True,
            "track_count": len(rows),
            "measured_count": measured,
            "tracks": tracks[:60],
        }
        try:
            import library_comparison

            rollup = library_comparison.album_quality(rows)
            if rollup is not None:
                payload["any_fake"] = rollup.any_fake
                payload["authenticity_known"] = rollup.authenticity_known
        except Exception:
            pass
        return payload
    except Exception:
        return {"available": False}


_LIBRARY_QUALITY_BUCKETS = [
    {
        "key": "invalid",
        "label": "Integrity failed",
        "description": "One or more fresh measured tracks have hard decode corruption.",
        "impact": "Audio did not decode cleanly during integrity verification.",
        "action": "Treat as a real replacement candidate. Inspect the listed tracks before changing the library.",
    },
    {
        "key": "nonstandard_flac_tags",
        "label": "FLAC tag cleanup",
        "description": "FLAC audio carries non-standard ID3 tags. Cleanup candidate; "
        "not a replacement trigger.",
        "impact": "The FLAC file has ID3 tags where strict FLAC tooling expects native FLAC metadata.",
        "action": "Cleanup candidate, not a re-download trigger. Do not replace the album for this alone.",
    },
    {
        "key": "measured_fake",
        "label": "Measured fake",
        "description": "Fresh spectral evidence says at least one track is fake/suspicious.",
        "impact": "Spectral verification suggests the file may be lossy audio stored as lossless.",
        "action": "Inspect before replacing. This is quality evidence, not a tag-cleanup issue.",
    },
    {
        "key": "checksum_mismatch",
        "label": "Checksum mismatch",
        "description": "Plays fine; the stored FLAC MD5 no longer matches the audio "
        "(re-tagged/re-encoded after ripping). Not corruption.",
        "impact": "Audio decoded, but the embedded FLAC MD5 no longer matches.",
        "action": "Usually advisory. Prefer cleanup or re-verification over replacement.",
    },
    {
        "key": "stale",
        "label": "Stale evidence",
        "description": "Stored evidence was produced by an older sensor version.",
        "impact": "Mintarr has evidence, but it came from an older scanner contract.",
        "action": "Run the relevant scan tier again before making decisions from this row.",
    },
    {
        "key": "unmeasured",
        "label": "Unmeasured",
        "description": "No usable measured quality evidence exists for the album.",
        "impact": "Mintarr cannot yet compare this album from real file evidence.",
        "action": "Run metadata first, then integrity if you need decode verification.",
    },
    {
        "key": "lossy",
        "label": "Lossy",
        "description": "At least one track is lossy (not lossless) — a real lossless "
        "upgrade candidate.",
        "impact": "At least one existing file is MP3, OGG, Opus, AAC, or otherwise not lossless.",
        "action": "Good upgrade target if a verified lossless candidate appears.",
    },
    {
        "key": "redbook",
        "label": "CD quality (16/44)",
        "description": "All lossless but only standard CD tier (16-bit/44.1kHz). Fine "
        "— a hi-res upgrade is optional, not a defect.",
        "impact": "Existing files are lossless CD quality.",
        "action": "Healthy baseline. Replace only for a clearly better verified edition.",
    },
    {
        "key": "mixed_tier",
        "label": "Mixed tier",
        "description": "Tracks in the album roll up to different lossless tiers.",
        "impact": "The album contains tracks with different quality tiers.",
        "action": "Inspect edition/source consistency before replacing anything.",
    },
    {
        "key": "integrity_unknown",
        "label": "Integrity unknown",
        "description": "Metadata measured (tier known) but the integrity tier "
        "(flac -t) has not verified the audio yet. Not a defect — run an integrity "
        "scan to confirm.",
        "impact": "Mintarr knows the file tier, but has not verified decode integrity.",
        "action": "Run integrity scan when you need stronger evidence.",
    },
    {
        "key": "unknown_authenticity",
        "label": "Unknown authenticity",
        "description": "Spectral mode is enabled, but authenticity is not known for every measured track.",
        "impact": "Spectral authenticity is incomplete for this album.",
        "action": "Run spectral scan only if you need fake-hi-res evidence.",
    },
    {
        "key": "ok",
        "label": "Measured OK",
        "description": "Fresh measured + integrity-verified evidence with no known quality warnings.",
        "impact": "No known quality warning from the enabled evidence tiers.",
        "action": "No operator action needed.",
    },
]
_LIBRARY_QUALITY_BUCKET_ORDER = {
    bucket["key"]: index for index, bucket in enumerate(_LIBRARY_QUALITY_BUCKETS)
}
_LIBRARY_QUALITY_BUCKET_INFO = {
    bucket["key"]: bucket for bucket in _LIBRARY_QUALITY_BUCKETS
}
_LIBRARY_QUALITY_ACTION_KIND = {
    "invalid": "Replacement candidate",
    "measured_fake": "Replacement candidate",
    "nonstandard_flac_tags": "Cleanup candidate",
    "checksum_mismatch": "Advisory",
    "stale": "Rescan needed",
    "unmeasured": "Scan needed",
    "lossy": "Upgrade target",
    "redbook": "Healthy baseline",
    "mixed_tier": "Inspect edition",
    "integrity_unknown": "Verify if needed",
    "unknown_authenticity": "Verify if needed",
    "ok": "No action",
}
_LIBRARY_QUALITY_TIERS = [
    {
        "key": "lossy",
        "label": "Lossy",
        "description": "At least one existing track is not lossless.",
    },
    {
        "key": "redbook",
        "label": "CD quality (16/44)",
        "description": "All measured tracks are lossless standard CD tier.",
    },
    {
        "key": "lossless_24",
        "label": "24-bit lossless",
        "description": "All measured tracks are lossless 24-bit up to 48 kHz.",
    },
    {
        "key": "hires",
        "label": "Hi-res FLAC",
        "description": "All measured tracks are lossless 24-bit above 48 kHz.",
    },
    {
        "key": "mixed",
        "label": "Mixed tier",
        "description": "Measured tracks span more than one audio tier.",
    },
    {
        "key": "unknown",
        "label": "Tier unknown",
        "description": "Mintarr has no usable measured tier for the album.",
    },
]


def _library_quality_scan_status(state_db_mod) -> dict:
    active = state_db_mod.get_active_library_scan_run()
    _total, recent = state_db_mod.list_library_scan_runs(limit=5)

    def _run_payload(run: dict | None) -> dict | None:
        if not run:
            return None
        total_items = int(run.get("total_items") or 0)
        processed_items = int(run.get("processed_items") or 0)
        percent = None
        if total_items > 0:
            percent = round((processed_items / total_items) * 100, 1)
        return {
            "id": run.get("id"),
            "mode": run.get("mode"),
            "state": run.get("state"),
            "total_items": total_items,
            "processed_items": processed_items,
            "measured_items": int(run.get("measured_items") or 0),
            "fresh_items": int(run.get("fresh_items") or 0),
            "unmeasured_items": int(run.get("unmeasured_items") or 0),
            "error_items": int(run.get("error_items") or 0),
            "cancel_requested": bool(run.get("cancel_requested")),
            "percent": percent,
        }

    return {"active": _run_payload(active), "recent": [_run_payload(r) for r in recent]}


def _dashboard_measured_row_current(row: dict, library_evidence_mod) -> bool:
    """DB-only freshness for dashboard ranking.

    Unlike the decision path, this view must not stat the mounted library during
    render. The background scan owns size/mtime freshness; the dashboard only
    treats sensor-version drift as stale.
    """
    return (
        row.get("status") == "measured"
        and row.get("sensor_version") == library_evidence_mod.METADATA_SENSOR_VERSION
    )


def _dashboard_spectral_row_current(row: dict, library_evidence_mod) -> bool:
    return (
        row.get("spectral_status") == "measured"
        and row.get("spectral_sensor_version")
        == library_evidence_mod.SPECTRAL_SENSOR_VERSION
    )


def _dashboard_integrity_row_current(row: dict, library_evidence_mod) -> bool:
    """True iff the integrity tier ran fresh for this row (DB-only, no disk stat)."""
    return (
        row.get("integrity_sensor_version")
        == library_evidence_mod.INTEGRITY_SENSOR_VERSION
    )


def _library_quality_album_summary(
    album_id: int | None,
    rows: list[dict],
    *,
    library_evidence_mod,
    library_comparison_mod,
) -> dict:
    """Summarize one album for the read-only quality ranking view.

    Bucket precedence is worst-first and mutually exclusive: known hard failures
    first, stale/unusable evidence next, upgrade opportunities after that, then
    measured OK. This is an operator ranking view only; it never feeds decisions.
    """

    def _track_priority(row: dict) -> tuple[int, str]:
        if row.get("integrity_ok") is False:
            return (0, str(row.get("path") or ""))
        if row.get("integrity_issue"):
            return (1, str(row.get("path") or ""))
        if row.get("checksum_ok") is False:
            return (2, str(row.get("path") or ""))
        return (3, str(row.get("path") or ""))

    display_rows = sorted(rows, key=_track_priority)
    tracks = [
        {
            "trackfile_id": r.get("trackfile_id"),
            "filename": _basename_any(r.get("path")),
            "status": r.get("status"),
            "reason": r.get("reason"),
            "integrity_issue": r.get("integrity_issue"),
            "integrity_ok": None
            if r.get("integrity_ok") is None
            else bool(r.get("integrity_ok")),
            "checksum_ok": None
            if r.get("checksum_ok") is None
            else bool(r.get("checksum_ok")),
            "codec": r.get("codec"),
            "bitrate_kbps": r.get("bitrate_kbps"),
            "lossless": None if r.get("lossless") is None else bool(r.get("lossless")),
        }
        for r in display_rows[:60]
    ]
    measured = [r for r in rows if r.get("status") == "measured"]
    fresh_rows: list[dict] = []
    stale_count = 0
    spectral_stale_count = 0
    for row in measured:
        if not _dashboard_measured_row_current(row, library_evidence_mod):
            stale_count += 1
            continue
        view = dict(row)
        if view.get("lossless") is not None:
            view["lossless"] = bool(view.get("lossless"))
        # Integrity is gated on its own tier freshness: a metadata-only or
        # stale-integrity row reads as *unknown* (None), never trusted as OK.
        integrity_fresh = _dashboard_integrity_row_current(row, library_evidence_mod)
        view["integrity_known"] = (
            integrity_fresh and view.get("integrity_issue") != "nonstandard_flac_tags"
        )
        if not integrity_fresh:
            view["integrity_ok"] = None
            view["checksum_ok"] = None
            view["integrity_issue"] = None
        else:
            if view.get("integrity_ok") is not None:
                view["integrity_ok"] = bool(view.get("integrity_ok"))
            if view.get("checksum_ok") is not None:
                view["checksum_ok"] = bool(view.get("checksum_ok"))
        if view.get("authentic") is not None:
            view["authentic"] = bool(view.get("authentic"))
        if row.get("spectral_status") == "measured":
            if _dashboard_spectral_row_current(row, library_evidence_mod):
                pass
            else:
                spectral_stale_count += 1
                view["authentic"] = None
                view["spectral_status"] = None
        fresh_rows.append(view)

    rollup = library_comparison_mod.album_quality(fresh_rows)
    invalid_count = sum(1 for r in fresh_rows if r.get("integrity_ok") is False)
    nonstandard_flac_tags_count = sum(
        1 for r in fresh_rows if r.get("integrity_issue") == "nonstandard_flac_tags"
    )
    tiers = {
        library_comparison_mod.tier_rank(
            lossless=bool(r.get("lossless")),
            bit_depth=r.get("bit_depth"),
            sample_rate=r.get("sample_rate"),
        )
        for r in fresh_rows
        if r.get("status") == "measured"
    }
    if invalid_count:
        bucket = "invalid"
    elif nonstandard_flac_tags_count:
        bucket = "nonstandard_flac_tags"
    elif rollup is not None and rollup.any_fake:
        bucket = "measured_fake"
    elif rollup is not None and rollup.any_md5_mismatch:
        # Plays fine, stale FLAC MD5 — a real finding but not corruption and never
        # a decision trigger; ranked below a fake, above plain staleness.
        bucket = "checksum_mismatch"
    elif stale_count or spectral_stale_count:
        bucket = "stale"
    elif rollup is None:
        bucket = "unmeasured"
    elif not rollup.all_lossless:
        # A genuinely lossy track (tier 0) — an actionable lossless upgrade.
        bucket = "lossy"
    elif rollup.min_tier <= 1:
        # All lossless, but the weakest track is standard CD tier (16/44). Fine,
        # not a defect — kept distinct from real lossy so CD rips don't read as a
        # problem.
        bucket = "redbook"
    elif len(tiers) > 1:
        bucket = "mixed_tier"
    elif not rollup.integrity_known:
        # Metadata measured (tier known) but integrity (flac -t) not verified for
        # every track — never present unknown as fully-verified OK (§7 guardrail).
        bucket = "integrity_unknown"
    elif library_evidence_mod.spectral_enabled() and not rollup.authenticity_known:
        bucket = "unknown_authenticity"
    else:
        bucket = "ok"

    tier_bucket = "unknown"
    if rollup is not None:
        if not rollup.all_lossless:
            tier_bucket = "lossy"
        elif len(tiers) > 1:
            tier_bucket = "mixed"
        elif rollup.min_tier <= 1:
            tier_bucket = "redbook"
        elif rollup.min_tier == 2:
            tier_bucket = "lossless_24"
        else:
            tier_bucket = "hires"
    lossy_labels = sorted(
        {
            _lossy_codec_label(r.get("codec"), r.get("bitrate_kbps"))
            for r in fresh_rows
            if r.get("status") == "measured" and r.get("lossless") is False
        }
    )

    return {
        "album_id": album_id,
        "primary_bucket": bucket,
        "bucket_label": _LIBRARY_QUALITY_BUCKET_INFO.get(bucket, {}).get(
            "label", bucket.replace("_", " ")
        ),
        "bucket_description": _LIBRARY_QUALITY_BUCKET_INFO.get(bucket, {}).get(
            "description", ""
        ),
        "bucket_action_kind": _LIBRARY_QUALITY_ACTION_KIND.get(bucket, "Inspect"),
        "tier_bucket": tier_bucket,
        "lossy_labels": lossy_labels,
        "track_count": len(rows),
        "measured_count": len(fresh_rows),
        "stale_count": stale_count + spectral_stale_count,
        "invalid_count": invalid_count,
        "nonstandard_flac_tags_count": nonstandard_flac_tags_count,
        "md5_mismatch_count": sum(
            1 for r in fresh_rows if r.get("checksum_ok") is False
        ),
        "any_fake": bool(rollup.any_fake) if rollup else False,
        "authenticity_known": bool(rollup.authenticity_known) if rollup else False,
        "integrity_known": bool(rollup.integrity_known) if rollup else False,
        "min_tier": rollup.min_tier if rollup else None,
        "all_lossless": bool(rollup.all_lossless) if rollup else False,
        "sample_files": tracks,
    }


def _library_quality_lidarr_labels(album_ids: list[int]) -> dict[int, dict]:
    if not album_ids:
        return {}
    try:
        import os
        import sqlite3

        configured = os.environ.get("MINTARR_LIDARR_DB_PATH")
        if configured:
            db_path = Path(configured)
        else:
            config_xml = os.environ.get("LIDARR_CONFIG_XML")
            if not config_xml:
                return {}
            db_path = Path(config_xml).with_name("lidarr.db")
        if not db_path.exists():
            return {}
        placeholders = ",".join("?" for _ in album_ids)
        query = f"""
            SELECT Albums.Id AS album_id, Albums.Title AS album_title,
                   ArtistMetadata.Name AS artist_name
            FROM Albums
            LEFT JOIN ArtistMetadata
              ON ArtistMetadata.Id = Albums.ArtistMetadataId
            WHERE Albums.Id IN ({placeholders})
        """
        out: dict[int, dict] = {}
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(query, album_ids):
                aid = int(row["album_id"])
                out[aid] = {
                    "artist": row["artist_name"],
                    "album": row["album_title"],
                    "lidarr_url": f"{_lidarr_web_base()}/album/{aid}",
                }
        return out
    except Exception:
        return {}


def _lossy_codec_label(codec: object, bitrate_kbps: object) -> str:
    codec_name = str(codec or "unknown").strip().lower() or "unknown"
    display = {
        "aac": "AAC",
        "mp3": "MP3",
        "opus": "Opus",
        "vorbis": "OGG/Vorbis",
        "ogg": "OGG",
    }.get(codec_name, codec_name.upper() if codec_name != "unknown" else "Unknown")
    bitrate = bitrate_kbps if isinstance(bitrate_kbps, int) else None
    if bitrate and bitrate > 0:
        return f"{display} {bitrate} kbps"
    return f"{display} bitrate unknown"


def _enrich_library_quality_albums(albums: list[dict]) -> list[dict]:
    labels = _library_quality_lidarr_labels(
        [int(a["album_id"]) for a in albums if a.get("album_id") is not None]
    )
    for album in albums:
        aid = album.get("album_id")
        if aid is None:
            continue
        label = labels.get(int(aid))
        if label:
            album.update(label)
    return albums


def _library_quality_aggregate() -> dict:
    import library_comparison
    import library_evidence
    import state_db

    total_rows, rows = state_db.list_all_library_evidence()
    grouped: dict[int | None, list[dict]] = {}
    for row in rows:
        aid = row.get("album_id")
        grouped.setdefault(int(aid) if aid is not None else None, []).append(row)

    albums = [
        _library_quality_album_summary(
            album_id,
            album_rows,
            library_evidence_mod=library_evidence,
            library_comparison_mod=library_comparison,
        )
        for album_id, album_rows in grouped.items()
    ]
    albums.sort(
        key=lambda a: (
            _LIBRARY_QUALITY_BUCKET_ORDER.get(a["primary_bucket"], 999),
            a["album_id"] is None,
            a["album_id"] or 0,
        )
    )
    bucket_counts = Counter(a["primary_bucket"] for a in albums)
    tier_counts = Counter(a["tier_bucket"] for a in albums)
    lossy_counts: Counter[str] = Counter()
    for album in albums:
        for label in album.get("lossy_labels") or []:
            lossy_counts[label] += 1

    return {
        "albums": albums,
        "buckets": [
            {**b, "count": int(bucket_counts.get(b["key"], 0))}
            for b in _LIBRARY_QUALITY_BUCKETS
        ],
        "tiers": [
            {**b, "count": int(tier_counts.get(b["key"], 0))}
            for b in _LIBRARY_QUALITY_TIERS
        ],
        "lossy_breakdown": [
            {"label": label, "count": int(count)}
            for label, count in sorted(
                lossy_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ][:12],
        "total_albums": len(grouped),
        "total_rows": total_rows,
    }


def _cached_library_quality_aggregate() -> dict:
    import library_evidence
    import state_db

    revision = state_db.library_evidence_revision()
    return get_or_compute(
        (
            "library-quality-aggregate",
            revision,
            library_evidence.METADATA_SENSOR_VERSION,
            library_evidence.INTEGRITY_SENSOR_VERSION,
            library_evidence.SPECTRAL_SENSOR_VERSION,
            bool(library_evidence.spectral_enabled()),
        ),
        120.0,
        _library_quality_aggregate,
    )


def _build_library_quality_view(
    *, bucket: str | None = None, limit: int = 50, offset: int = 0
) -> dict:
    import state_db

    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    aggregate = _cached_library_quality_aggregate()
    albums = list(aggregate["albums"])
    if bucket:
        albums = [a for a in albums if a["primary_bucket"] == bucket]
    returned = _enrich_library_quality_albums(albums[offset : offset + limit])
    prev_offset = max(0, offset - limit)
    next_offset = offset + limit if offset + limit < len(albums) else None
    return {
        "buckets": aggregate["buckets"],
        "tiers": aggregate["tiers"],
        "lossy_breakdown": aggregate["lossy_breakdown"],
        "albums": returned,
        "selected_bucket": bucket or "",
        "selected_bucket_info": next(
            (b for b in _LIBRARY_QUALITY_BUCKETS if b["key"] == bucket), None
        ),
        "total_albums": aggregate["total_albums"],
        "filtered_albums": len(albums),
        "total_rows": aggregate["total_rows"],
        "limit": limit,
        "offset": offset,
        "prev_offset": prev_offset if offset > 0 else None,
        "next_offset": next_offset,
        "scan": _library_quality_scan_status(state_db),
    }


@dashboard_bp.route("/v1/library-quality", methods=["GET"])
def library_quality_json():
    """Read-only quality ranking over measured existing-library evidence."""
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    try:
        payload = _build_library_quality_view(
            bucket=request.args.get("bucket") or None,
            limit=int(request.args.get("limit", "50")),
            offset=int(request.args.get("offset", "0")),
        )
    except Exception:
        payload = {
            "buckets": [],
            "albums": [],
            "total_albums": 0,
            "filtered_albums": 0,
            "total_rows": 0,
            "limit": 50,
            "offset": 0,
            "scan": {"active": None, "recent": []},
        }
    return jsonify(payload)


@dashboard_bp.route("/v1/library-quality/partial", methods=["GET"])
def library_quality_partial():
    """Server-rendered System panel for library quality ranking (F5.4 5d)."""
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    compact = str(request.args.get("compact") or "").lower() in {"1", "true", "yes"}
    try:
        quality = _build_library_quality_view(
            bucket=request.args.get("bucket") or None,
            limit=int(request.args.get("limit", "12" if compact else "50")),
            offset=int(request.args.get("offset", "0")),
        )
    except Exception:
        quality = {
            "buckets": [],
            "albums": [],
            "total_albums": 0,
            "filtered_albums": 0,
            "total_rows": 0,
            "selected_bucket": request.args.get("bucket") or "",
            "limit": 12 if compact else 50,
            "offset": 0,
            "prev_offset": None,
            "next_offset": None,
            "scan": {"active": None, "recent": []},
        }
    return render_template(
        "partials/library_quality.html", quality=quality, compact=compact
    )


def _build_record_detail(server_mod, jid: str) -> dict | None:
    _, sidecar = server_mod._read_verification_sidecar(jid)
    if sidecar is None:
        return None
    if (
        sidecar.get("v2_import_outcome") == "PENDING"
        and sidecar.get("v2_verification_decision") != "REVIEW_REQUIRED"
    ):
        sidecar = server_mod._reconcile_pending_import(jid, sidecar)
    with server_mod._jobs_lock:
        job = dict(server_mod._jobs.get(jid, {}))
    if job.get("error"):
        sidecar["job_error"] = job.get("error")
    if job.get("warning"):
        sidecar["job_warning"] = job.get("warning")
    timings = sidecar.get("timings") or job.get("timings") or {}

    derived = derive_status(sidecar)
    actions = available_actions(sidecar)
    review_reason = _review_reason(sidecar) if derived == "needs_review" else None
    reason = status_reason(sidecar)
    media = _media_review_info(server_mod, jid, derived)

    return {
        "jid": jid,
        "verification": {
            "decision": sidecar.get("v2_verification_decision"),
            "outcome": sidecar.get("v2_import_outcome"),
            "score": sidecar.get("v2_score"),
            "components": sidecar.get("v2_components") or {},
            "overrides": sidecar.get("v2_overrides") or [],
            "verdict": sidecar.get("verdict"),
            "review_reason": review_reason,
        },
        "release_identity": _release_identity_detail(sidecar),
        "library_evidence": _library_evidence_detail(sidecar),
        "release_switch_events": _release_switch_events(jid),
        "release_switch_strategy": server_mod._release_switch_strategy(),
        "lifecycle": sidecar.get("lifecycle") or {},
        "sensors": sidecar.get("sensors") or [],
        "files": sidecar.get("files") or [],
        "context": {
            "existing": {
                "kbps": sidecar.get("existing_kbps", 0),
                "label": sidecar.get("existing_quality", "nothing"),
            },
            "album_ids": sidecar.get("album_ids") or [],
            "title": sidecar.get("title", ""),
        },
        "derived_status": derived,
        "status_reason": reason,
        "media": media,
        "timings": timings,
        "available_actions": actions,
    }


def _release_switch_events(jid: str) -> list[dict]:
    """Read-only release-switch audit trail for a record (ADR-0013 / #70 req 10).

    Surfaces what the release-switch strategy proposed/applied/restored. Reads
    the state_db audit log only — no Lidarr call, no mutation.
    """
    try:
        import json as _json

        import state_db

        rows = state_db.list_actions(jid=jid, limit=50)
    except Exception:
        return []
    events: list[dict] = []
    for row in rows:
        if row.get("action") != "release_switch":
            continue
        raw = row.get("details_json")
        try:
            details = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            details = {}
        if not isinstance(details, dict):
            details = {}
        ts = row.get("created_at")
        try:
            when = (
                datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
                if ts
                else "—"
            )
        except (TypeError, ValueError):
            when = "—"
        events.append(
            {
                "event": details.get("event") or row.get("result") or "",
                "mode": details.get("mode") or "",
                "actor": row.get("actor") or "",
                "result": details.get("result") or row.get("result") or "",
                "old_release_id": details.get("old_release_id"),
                "new_release_id": details.get("new_release_id"),
                "confidence": details.get("confidence"),
                "reasons": details.get("reasons") or [],
                "when": when,
            }
        )
    return events


def _release_identity_detail(sidecar: dict) -> dict | None:
    raw_sensor = next(
        (
            item
            for item in sidecar.get("sensors") or []
            if item.get("name") == "release_identity"
        ),
        None,
    )
    sensor = raw_sensor if isinstance(raw_sensor, dict) else {}
    raw_evidence = sensor.get("evidence")
    evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
    decision = sidecar.get("release_identity_decision") or evidence.get(
        "identity_decision"
    )
    if not decision and not evidence:
        return None
    reasons = sidecar.get("release_identity_reasons")
    if not isinstance(reasons, list):
        reasons = evidence.get("identity_reasons") or []
    return {
        "decision": decision or "UNKNOWN",
        "confidence": sidecar.get("release_identity_confidence")
        if sidecar.get("release_identity_confidence") is not None
        else evidence.get("identity_confidence"),
        "reasons": reasons,
        "best_release_id": sidecar.get("release_identity_best_release_id")
        if sidecar.get("release_identity_best_release_id") is not None
        else evidence.get("best_release_id"),
        "current_release_id": sidecar.get("release_identity_current_release_id")
        if sidecar.get("release_identity_current_release_id") is not None
        else evidence.get("current_release_id"),
        "score": evidence.get("score"),
        "track_count_delta": evidence.get("track_count_delta"),
        "title_similarity": evidence.get("title_similarity"),
        "lidarr_rejections": evidence.get("lidarr_rejections") or [],
        "sensor_status": sensor.get("status"),
        "sensor_summary": sensor.get("summary"),
        "observed": {
            "file_count": evidence.get("file_count"),
            "track_titles": evidence.get("track_titles") or [],
            "artist_names": evidence.get("artist_names") or [],
            "album_titles": evidence.get("album_titles") or [],
            "artist_mbids": evidence.get("artist_mbids") or [],
            "release_group_mbids": evidence.get("release_group_mbids") or [],
            "release_mbids": evidence.get("release_mbids") or [],
        },
    }


@dashboard_bp.route("/v1/record/<jid>", methods=["GET"])
def record_detail(jid: str):
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import server

    data = _build_record_detail(server, jid)
    if data is None:
        return jsonify({"error": "record not found", "jid": jid}), 404
    return jsonify(data)


# ---------- /dashboard/v1/timings ----------
@dashboard_bp.route("/v1/timings", methods=["GET"])
def timings():
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import server

    window = request.args.get("window", "7d")
    stage = request.args.get("stage")
    key = ("timings", window, stage or "")
    data = get_or_compute(
        key, 60.0, lambda: _build_timings(server, window=window, stage=stage)
    )
    return jsonify(data)


# ---------- /dashboard/v1/audio-sample/<jid> + /spectrum/<jid> ----------
@dashboard_bp.route("/v1/audio-sample/<jid>", methods=["GET"])
def audio_sample(jid: str):
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import server

    detail = _build_record_detail(server, jid)
    if detail is None:
        return jsonify({"error": "record not found", "jid": jid}), 404
    if not detail.get("media", {}).get("available"):
        return jsonify(
            {
                "error": detail.get("media", {}).get("reason")
                or "audio sample unavailable",
                "jid": jid,
            }
        ), 404
    path, error = _media_artifact(server, jid, "audio")
    if path is None:
        return jsonify({"error": error or "audio sample unavailable", "jid": jid}), 404
    return send_file(path, mimetype="audio/mpeg", conditional=True)


@dashboard_bp.route("/v1/spectrum/<jid>", methods=["GET"])
def spectrum(jid: str):
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import server

    detail = _build_record_detail(server, jid)
    if detail is None:
        return jsonify({"error": "record not found", "jid": jid}), 404
    if not detail.get("media", {}).get("available"):
        return jsonify(
            {
                "error": detail.get("media", {}).get("reason")
                or "spectrum unavailable",
                "jid": jid,
            }
        ), 404
    path, error = _media_artifact(server, jid, "spectrum")
    if path is None:
        return jsonify({"error": error or "spectrum unavailable", "jid": jid}), 404
    return send_file(path, mimetype="image/png", conditional=True)


# ---------- /dashboard/v1/lidarr-context/<jid> ----------
@dashboard_bp.route("/v1/lidarr-context/<jid>", methods=["GET"])
def lidarr_context(jid: str):
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    import server

    key = ("lidarr-context", jid)
    data, status = get_or_compute(key, 30.0, lambda: _build_lidarr_context(server, jid))
    return jsonify(data), status


# ---------- POST /dashboard/v1/action/<jid> ----------
@dashboard_bp.route("/v1/action/<jid>", methods=["POST"])
def action(jid: str):
    from server import require_apikey_check

    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp

    import server

    body = request.get_json(silent=True) or {}
    requested = body.get("action", "")

    _, sidecar = server._read_verification_sidecar(jid)
    if sidecar is None:
        return jsonify({"error": "record not found", "jid": jid}), 404

    allowed = available_actions(sidecar)
    if requested not in allowed:
        return jsonify(
            {
                "error": "action not allowed for current state",
                "requested": requested,
                "allowed": allowed,
            }
        ), 409

    # Delegate to existing route functions via Flask view_functions.
    # They are already @require_apikey-decorated; the current request has apikey so
    # the decorator passes on the nested call.
    from flask import current_app

    view_map = {
        "promote": "verification_promote",
        "discard": "verification_discard",
        "retry_import": "verification_retry_import",
        "apply_release_switch": "verification_release_switch",
    }
    view_name = view_map.get(requested)
    if not view_name:
        return jsonify({"error": f"unknown action: {requested}"}), 400

    view = current_app.view_functions.get(view_name)
    if not view:
        return jsonify({"error": f"endpoint not registered: {view_name}"}), 500

    # Call original route — returns Flask Response
    response = view(jid)

    # Invalidate cache so the next GET reflects new state
    invalidate_prefix("summary")
    invalidate_prefix("records")

    # F1: Log action to state_db for audit timeline (fail-open)
    try:
        import state_db

        status_code = (
            getattr(response, "status_code", 200) if response is not None else 200
        )
        if isinstance(response, tuple) and len(response) >= 2:
            status_code = response[1]
        result_label = "ok" if 200 <= status_code < 300 else f"http_{status_code}"
        state_db.log_action(
            jid=jid,
            action=requested,
            actor="user_dashboard",
            result=result_label,
            details={"status_code": status_code},
        )
    except Exception:
        pass

    return response


# ---------- /dashboard (HTML page) ----------
@dashboard_bp.route("", methods=["GET"])
@dashboard_bp.route("/", methods=["GET"])
def dashboard_page():
    """Server-rendered HTML shell. JS handles interactivity per UI_SPEC.

    HTML/CSS/JS live in app/templates/dashboard.html + app/static/ (extracted
    from inline blobs in #45 as the framework-agnostic first Phase 2 step per
    ADR-0011). The one server-injected value is the Lidarr web base.
    """
    return render_template("dashboard.html", lidarr_web_base=_lidarr_web_base())
