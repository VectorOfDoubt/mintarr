"""Dashboard endpoints (JSON API) + HTML page.

Implements TIDALHIRES_DASHBOARD_API.md v1 endpoints:
- GET /dashboard/v1/summary
- GET /dashboard/v1/records
- GET /dashboard/v1/record/<jid>
- GET /dashboard/v1/connectors
- GET /dashboard/v1/timings
- GET /dashboard/v1/audio-sample/<jid>
- GET /dashboard/v1/spectrum/<jid>
- POST /dashboard/v1/action/<jid>
- GET /dashboard (HTML page, server-rendered shell + JS for interactivity)
"""

from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from flask import Blueprint, Response, jsonify, request, send_file

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
    if state in ("discarded", "expired"):
        return []
    if outcome in ("MANUAL_IMPORTED", "RESCUED"):
        return []
    if decision == "REVIEW_REQUIRED" and state == "pending_review":
        return ["promote", "discard"]
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
    """Server-rendered HTML shell. JS handles interactivity per UI_SPEC."""
    html = _DASHBOARD_HTML.replace(
        "__LIDARR_WEB_BASE__", json.dumps(_lidarr_web_base())
    )
    return Response(html, mimetype="text/html")


# Inline HTML page — design tokens per TIDALHIRES_DASHBOARD_UI_SPEC.md
_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TidalHires V2 Dashboard</title>
<style>
:root {
  --bg-base: #0d1117;
  --bg-surface: #161b22;
  --bg-elevated: #1e252e;
  --bg-inset: #0a0e13;
  --border-subtle: #21262d;
  --border-default: #30363d;
  --text-primary: #e6edf3;
  --text-secondary: #7d8590;
  --text-muted: #545d68;
  --text-mono: #c9d1d9;
  --accent-primary: #4493f8;
  --accent-primary-hover: #58a6ff;
  --status-success: #3fb950;
  --status-success-bg: rgba(63,185,80,0.1);
  --status-warning: #d29922;
  --status-warning-bg: rgba(210,153,34,0.1);
  --status-danger: #f85149;
  --status-danger-bg: rgba(248,81,73,0.1);
  --status-neutral: #6e7681;
  --status-neutral-bg: rgba(110,118,129,0.1);
  --status-info: #a371f7;
  --status-info-bg: rgba(163,113,247,0.1);
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", system-ui, sans-serif;
  --font-mono: "SF Mono", "Cascadia Code", "Roboto Mono", monospace;
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 8px;
}
* { box-sizing: border-box; }
body {
  font-family: var(--font-sans);
  font-size: 13px;
  background: var(--bg-base);
  color: var(--text-primary);
  margin: 0;
  padding: 0;
}
header {
  position: sticky; top: 0; z-index: 10;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-default);
  padding: 12px 24px;
  display: flex; justify-content: space-between; align-items: center;
}
header h1 { margin: 0; font-size: 16px; font-weight: 600; }
header .meta { color: var(--text-secondary); font-size: 12px; }
header .refresh { display: inline-flex; gap: 8px; align-items: center; }
header .refresh-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--status-success); }
header .refresh-dot.stale { background: var(--status-warning); }
main { padding: 16px 24px; }
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.card {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 12px 16px;
}
.card:hover { border-color: var(--border-default); }
.card.warn { border-color: var(--status-danger); background: var(--status-danger-bg); }
.card.attention { border-color: var(--status-warning); background: var(--status-warning-bg); }
.card.ok { border-color: var(--status-success); background: var(--status-success-bg); }
.card .label { color: var(--text-secondary); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
.card .val { font-size: 24px; font-weight: 600; margin-top: 4px; font-family: var(--font-mono); }
.card.warn .val { color: var(--status-danger); }
.card.attention .val { color: var(--status-warning); }
.card.ok .val { color: var(--status-success); }
.filter-bar {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 8px 12px;
  margin-bottom: 16px;
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
}
.filter-bar label { font-size: 12px; color: var(--text-secondary); margin-right: 4px; }
.filter-bar select, .filter-bar input {
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  color: var(--text-primary);
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  font-size: 12px;
}
.filter-bar button {
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  color: var(--text-primary);
  padding: 4px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 12px;
}
.filter-bar button:hover { border-color: var(--accent-primary); }
.tab-bar {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border-default);
  margin: 8px 0 16px;
}
.tab-button {
  background: transparent;
  border: 1px solid transparent;
  border-bottom: none;
  color: var(--text-secondary);
  padding: 8px 12px;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  cursor: pointer;
  font-size: 13px;
}
.tab-button:hover { color: var(--text-primary); border-color: var(--border-subtle); }
.tab-button.active {
  background: var(--bg-surface);
  border-color: var(--border-default);
  color: var(--text-primary);
}
.dashboard-view[hidden] { display: none; }
.panel {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  margin-bottom: 20px;
}
.panel h2 { margin-top: 0; }
.panel.warn { border-color: var(--status-warning); background: var(--status-warning-bg); }
.integration-section { margin-bottom: 24px; }
.integration-section h2 { margin-top: 0; }
.connector-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 12px;
}
.connector-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 12px;
}
.connector-card.required.missing,
.connector-card.required.blocked {
  border-color: var(--status-danger);
  background: var(--status-danger-bg);
}
.connector-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 10px;
}
.connector-title { font-weight: 600; font-size: 14px; }
.connector-id { color: var(--text-muted); font-family: var(--font-mono); font-size: 11px; margin-top: 2px; }
.connector-meta {
  display: grid;
  grid-template-columns: minmax(92px, auto) 1fr;
  gap: 6px 10px;
  font-size: 12px;
}
.connector-meta .k { color: var(--text-secondary); }
.connector-meta .v { color: var(--text-primary); min-width: 0; overflow-wrap: anywhere; }
.connector-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.connector-tag {
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  padding: 1px 5px;
  font-size: 10px;
  font-family: var(--font-mono);
}
.connector-link { color: var(--accent-primary); text-decoration: none; }
.connector-link:hover { color: var(--accent-primary-hover); }
.connector-guidance {
  margin-top: 10px;
  border: 1px solid var(--status-warning);
  border-radius: var(--radius-sm);
  background: var(--status-warning-bg);
  padding: 8px 10px;
  font-size: 12px;
}
.connector-guidance strong { display: block; margin-bottom: 4px; color: var(--text-primary); }
.connector-guidance ul { margin: 6px 0 0 18px; padding: 0; color: var(--text-secondary); }
.connector-guidance li { margin: 3px 0; }
.connector-controls {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
}
.connector-controls select {
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  color: var(--text-primary);
  border-radius: var(--radius-sm);
  padding: 5px 8px;
  font-size: 12px;
}
.connector-controls button {
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  color: var(--text-primary);
  border-radius: var(--radius-sm);
  padding: 5px 9px;
  cursor: pointer;
  font-size: 12px;
}
.connector-controls button:hover { border-color: var(--accent-primary); }
.connector-controls button.primary {
  background: var(--accent-primary);
  border-color: var(--accent-primary);
  color: #fff;
}
.command-list { display: grid; gap: 8px; }
.command-item {
  display: grid;
  grid-template-columns: minmax(150px, 1fr) minmax(90px, auto) minmax(90px, auto) minmax(260px, 2fr);
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.command-item:last-child { border-bottom: none; }
.command-item .name { font-weight: 600; }
.command-item .age { font-family: var(--font-mono); color: var(--text-secondary); }
.command-item .reason { color: var(--status-warning); }
.timing-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 8px 16px;
}
.timing-item {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid var(--border-subtle);
  padding: 6px 0;
}
.timing-item .stage { color: var(--text-secondary); }
.timing-item .numbers { font-family: var(--font-mono); color: var(--text-primary); }
.progress-cell { min-width: 210px; }
.progress-meta { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 4px; }
.progress-stage { color: var(--text-primary); font-weight: 600; }
.progress-percent { color: var(--text-secondary); font-family: var(--font-mono); }
.progress-track {
  height: 6px;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: var(--accent-primary);
  width: 0%;
}
.progress-message { color: var(--text-muted); font-size: 11px; margin-top: 4px; }
.spectrum {
  width: 100%;
  max-height: 380px;
  object-fit: contain;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-inset);
  cursor: zoom-in;
}
.spectrum:hover {
  border-color: var(--accent-primary);
}
.media-note { color: var(--text-muted); font-size: 12px; margin-top: 6px; }
.media-unavailable {
  border: 1px dashed var(--border-default);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  padding: 10px 12px;
  font-size: 12px;
  background: var(--bg-inset);
}
.file-evidence th, .file-evidence td {
  font-size: 11px;
  padding: 5px 6px;
}
.file-evidence td:first-child {
  max-width: 210px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
table { width: 100%; border-collapse: collapse; background: var(--bg-surface); border-radius: var(--radius-md); overflow: hidden; }
thead { background: var(--bg-elevated); }
th { padding: 8px 12px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-secondary); border-bottom: 1px solid var(--border-default); }
td { padding: 8px 12px; border-bottom: 1px solid var(--border-subtle); font-size: 13px; }
tr:hover { background: var(--bg-elevated); cursor: pointer; }
tr:nth-child(even) td { background: var(--bg-inset); }
tr:nth-child(even):hover td { background: var(--bg-elevated); }
.jid { font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary); }
.title { color: var(--text-primary); }
.reason {
  color: var(--text-secondary);
  max-width: 320px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; font-size: 10px; font-weight: 600;
  border-radius: var(--radius-sm); text-transform: uppercase; letter-spacing: 0.5px;
}
.badge.success { background: var(--status-success-bg); color: var(--status-success); }
.badge.warning { background: var(--status-warning-bg); color: var(--status-warning); }
.badge.danger { background: var(--status-danger-bg); color: var(--status-danger); }
.badge.neutral { background: var(--status-neutral-bg); color: var(--status-neutral); }
.badge.info { background: var(--status-info-bg); color: var(--status-info); }
.deeplink {
  color: var(--accent-primary); text-decoration: none; font-size: 14px;
  padding: 2px 4px;
}
.deeplink:hover { color: var(--accent-primary-hover); }
h2 { margin: 32px 0 12px; font-size: 14px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border-subtle); padding-bottom: 6px; }
.muted { color: var(--text-muted); font-style: italic; padding: 12px; }

/* Drawer */
#drawer-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 100; display: none; }
#drawer-overlay.open { display: block; }
#drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(720px, 92vw); background: var(--bg-surface); border-left: 1px solid var(--border-default); z-index: 101; transform: translateX(100%); transition: transform 200ms ease-out; overflow-y: auto; }
#drawer.open { transform: translateX(0); }
#drawer header { padding: 16px 20px; border-bottom: 1px solid var(--border-default); background: var(--bg-elevated); position: sticky; top: 0; z-index: 1; }
#drawer .close { background: none; border: none; color: var(--text-secondary); font-size: 20px; cursor: pointer; float: right; }
#drawer h2 { margin: 16px 20px 8px; }
#drawer section { padding: 0 20px 16px; }
#drawer .kvrow { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px dashed var(--border-subtle); font-size: 12px; }
#drawer .kvrow .k { color: var(--text-secondary); }
#drawer .kvrow .v { font-family: var(--font-mono); color: var(--text-mono); }
#drawer .actions { position: sticky; bottom: 0; background: var(--bg-elevated); border-top: 1px solid var(--border-default); padding: 16px 20px; display: flex; gap: 8px; flex-wrap: wrap; }
#drawer .actions button { flex: 1; padding: 10px; border-radius: var(--radius-md); cursor: pointer; font-weight: 600; border: 1px solid; min-width: 100px; }
.btn-promote { background: var(--status-success-bg); color: var(--status-success); border-color: var(--status-success); }
.btn-promote:hover { background: var(--status-success); color: var(--bg-base); }
.btn-discard { background: var(--status-danger-bg); color: var(--status-danger); border-color: var(--status-danger); }
.btn-discard:hover { background: var(--status-danger); color: var(--bg-base); }
.btn-retry { background: var(--status-info-bg); color: var(--status-info); border-color: var(--status-info); }
.btn-retry:hover { background: var(--status-info); color: var(--bg-base); }

/* Modal */
#modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 200; display: none; align-items: center; justify-content: center; }
#modal-overlay.open { display: flex; }
#modal { background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-lg); padding: 24px; max-width: 480px; width: 90%; }
#modal h3 { margin: 0 0 12px; font-size: 16px; }
#modal p { color: var(--text-secondary); font-size: 13px; line-height: 1.5; }
#modal .actions { margin-top: 20px; display: flex; gap: 8px; justify-content: flex-end; }
#modal button { padding: 8px 16px; border-radius: var(--radius-md); cursor: pointer; font-weight: 600; border: 1px solid var(--border-default); }
#modal .cancel { background: var(--bg-elevated); color: var(--text-primary); }
#modal .confirm-promote { background: var(--status-success); color: var(--bg-base); border-color: var(--status-success); }
#modal .confirm-discard { background: var(--status-danger); color: var(--bg-base); border-color: var(--status-danger); }
#modal .confirm-retry { background: var(--status-info); color: var(--bg-base); border-color: var(--status-info); }

/* Image lightbox */
#image-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.88); z-index: 250; display: none; align-items: center; justify-content: center; padding: 24px; }
#image-overlay.open { display: flex; }
#image-lightbox { width: min(1400px, 96vw); max-height: 94vh; background: var(--bg-base); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 12px; box-shadow: 0 20px 60px rgba(0,0,0,0.5); }
#image-lightbox header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 8px; color: var(--text-secondary); font-size: 12px; }
#image-lightbox button { background: var(--bg-elevated); color: var(--text-primary); border: 1px solid var(--border-default); border-radius: var(--radius-sm); cursor: pointer; padding: 4px 8px; }
#image-lightbox img { width: 100%; max-height: calc(94vh - 72px); object-fit: contain; display: block; background: #000; }

/* Toast */
#toast { position: fixed; top: 80px; right: 24px; background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 12px 16px; z-index: 300; display: none; max-width: 320px; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
#toast.show { display: block; }
#toast.success { border-color: var(--status-success); }
#toast.error { border-color: var(--status-danger); }

/* Auth prompt */
#auth-prompt { position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 400; display: flex; align-items: center; justify-content: center; }
#auth-prompt .box { background: var(--bg-surface); padding: 32px; border-radius: var(--radius-lg); max-width: 400px; }
#auth-prompt input { width: 100%; padding: 10px; font-family: var(--font-mono); background: var(--bg-elevated); border: 1px solid var(--border-default); color: var(--text-primary); border-radius: var(--radius-sm); margin: 12px 0; }
#auth-prompt button { width: 100%; padding: 10px; background: var(--accent-primary); color: var(--bg-base); border: none; border-radius: var(--radius-md); cursor: pointer; font-weight: 600; }
</style>
</head>
<body>

<header>
  <div>
    <h1>TidalHires V2 Dashboard</h1>
    <div class="meta" id="meta">Loading...</div>
  </div>
  <div class="refresh">
    <span class="refresh-dot" id="refresh-dot"></span>
    <span class="meta" id="refresh-status">Updated: never</span>
  </div>
</header>

<main>
  <div class="summary-grid" id="summary-grid"></div>
  <section class="panel warn" id="lidarr-command-panel" style="display:none">
    <h2>Lidarr command queue</h2>
    <div id="lidarr-command-body" class="muted">No blocking Lidarr commands.</div>
  </section>
  <section class="panel" id="active-jobs-panel" style="display:none">
    <h2>Active jobs</h2>
    <div id="active-jobs-body" class="muted">No active worker jobs.</div>
  </section>
  <section class="panel" id="timings-panel">
    <h2>Pipeline timings</h2>
    <div id="timings-body" class="muted">No timing data yet; new jobs will populate this.</div>
  </section>

  <div class="tab-bar" role="tablist" aria-label="Dashboard views">
    <button class="tab-button active" id="tab-records" role="tab" aria-controls="records-view" onclick="showView('records')">Records</button>
    <button class="tab-button" id="tab-integrations" role="tab" aria-controls="integrations-view" onclick="showView('integrations')">Integrations</button>
  </div>

  <section class="dashboard-view" id="records-view">
    <div class="filter-bar">
      <label>Filter:</label>
      <select id="filter-status">
        <option value="">All statuses</option>
        <option value="needs_review">Needs review</option>
        <option value="imported">Imported</option>
        <option value="pending">Pending</option>
        <option value="policy_violation">Policy alert</option>
        <option value="failed">Failed</option>
        <option value="blocked">Blocked</option>
        <option value="discarded">Discarded</option>
        <option value="promoted">Promoted</option>
      </select>
      <select id="filter-decision">
        <option value="">All decisions</option>
        <option value="ACCEPT">ACCEPT</option>
        <option value="ACCEPT_PROVISIONAL">ACCEPT_PROVISIONAL</option>
        <option value="REVIEW_REQUIRED">REVIEW_REQUIRED</option>
        <option value="BLOCK">BLOCK</option>
      </select>
      <button onclick="clearFilters()">Clear</button>
      <button onclick="refresh()">Refresh now</button>
    </div>

    <h2>Records</h2>
    <table id="records-table">
      <thead><tr>
        <th>Status</th><th>Reason</th><th>Title</th><th>Decision</th><th>Outcome</th><th>State</th><th>Score</th><th>JID</th><th></th>
      </tr></thead>
      <tbody id="records-body">
        <tr><td colspan="9" class="muted">Loading...</td></tr>
      </tbody>
    </table>
  </section>

  <section class="dashboard-view" id="integrations-view" hidden>
    <div id="integrations-body" class="muted">Loading integrations...</div>
  </section>
</main>

<div id="drawer-overlay" onclick="closeDrawer()"></div>
<aside id="drawer">
  <header>
    <button class="close" onclick="closeDrawer()">×</button>
    <h2 id="drawer-title" style="margin:0;border:none">Loading…</h2>
    <div class="meta" id="drawer-jid"></div>
  </header>
  <div id="drawer-body">
    <p class="muted">Loading…</p>
  </div>
  <div class="actions" id="drawer-actions"></div>
</aside>

<div id="modal-overlay" onclick="if(event.target===this)closeModal()">
  <div id="modal">
    <h3 id="modal-title"></h3>
    <p id="modal-desc"></p>
    <div class="actions">
      <button class="cancel" onclick="closeModal()">Cancel</button>
      <button id="modal-confirm">Confirm</button>
    </div>
  </div>
</div>

<div id="image-overlay" onclick="if(event.target===this)closeImageOverlay()">
  <div id="image-lightbox">
    <header>
      <span id="image-title">Spectrum preview</span>
      <button onclick="closeImageOverlay()">Close</button>
    </header>
    <img id="image-full" alt="Expanded spectrum preview">
  </div>
</div>

<div id="toast"></div>

<div id="auth-prompt" style="display:none">
  <div class="box">
    <h2 style="margin:0">API key required</h2>
    <p class="meta">Enter the TidalHires API key. It will be stored in your browser's localStorage.</p>
    <input type="password" id="auth-key-input" placeholder="API key" autofocus>
    <button onclick="saveKey()">Save</button>
  </div>
</div>

<script>
const API = '/dashboard/v1';
const LIDARR_WEB_BASE = __LIDARR_WEB_BASE__;
const REFRESH_MS = 30000;
let apikey = localStorage.getItem('tidalhires_apikey') || '';
let refreshTimer = null;
let lastUpdate = null;
let activeDrawerJid = null;
let activeView = localStorage.getItem('tidalhires_dashboard_view') || 'records';
let lastConnectors = [];

const $ = (id) => document.getElementById(id);

if (!apikey) showAuthPrompt();
else init();

function showAuthPrompt() {
  $('auth-prompt').style.display = 'flex';
}
function saveKey() {
  const v = $('auth-key-input').value.trim();
  if (!v) return;
  apikey = v;
  localStorage.setItem('tidalhires_apikey', v);
  $('auth-prompt').style.display = 'none';
  init();
}

async function api(path, opts={}) {
  const url = path + (path.includes('?') ? '&' : '?') + 'apikey=' + encodeURIComponent(apikey);
  const resp = await fetch(API + url, opts);
  if (resp.status === 401) {
    localStorage.removeItem('tidalhires_apikey');
    apikey = '';
    showAuthPrompt();
    throw new Error('auth');
  }
  return resp;
}

function init() {
  refresh();
  refreshTimer = setInterval(refresh, REFRESH_MS);
  setInterval(updateRefreshIndicator, 1000);
  $('filter-status').addEventListener('change', refresh);
  $('filter-decision').addEventListener('change', refresh);
  // Restore filters from localStorage
  $('filter-status').value = localStorage.getItem('tidalhires_filter_status') || '';
  $('filter-decision').value = localStorage.getItem('tidalhires_filter_decision') || '';
  showView(activeView);
}

function showView(view) {
  activeView = view === 'integrations' ? 'integrations' : 'records';
  localStorage.setItem('tidalhires_dashboard_view', activeView);
  $('records-view').hidden = activeView !== 'records';
  $('integrations-view').hidden = activeView !== 'integrations';
  $('tab-records').classList.toggle('active', activeView === 'records');
  $('tab-integrations').classList.toggle('active', activeView === 'integrations');
  $('tab-records').setAttribute('aria-selected', activeView === 'records' ? 'true' : 'false');
  $('tab-integrations').setAttribute('aria-selected', activeView === 'integrations' ? 'true' : 'false');
}

function clearFilters() {
  $('filter-status').value = '';
  $('filter-decision').value = '';
  localStorage.removeItem('tidalhires_filter_status');
  localStorage.removeItem('tidalhires_filter_decision');
  refresh();
}

async function refresh() {
  try {
    const [sumResp, recResp, timingResp, jobsResp, connectorResp] = await Promise.all([
      api('/summary'),
      api('/records?' + buildFilterParams()),
      api('/timings?window=7d'),
      api('/jobs?state=queued,running,cancelling&limit=20'),
      api('/connectors')
    ]);
    const sum = await sumResp.json();
    const rec = await recResp.json();
    const timings = await timingResp.json();
    const jobs = await jobsResp.json();
    const connectors = await connectorResp.json();
    lastConnectors = connectors.connectors || [];
    renderSummary(sum, connectors.connectors || []);
    renderActiveJobs(jobs.jobs || []);
    renderTimings(timings);
    renderRecords(rec.records);
    renderIntegrations(connectors.connectors || []);
    lastUpdate = Date.now();
    updateRefreshIndicator();
    // Save filter state
    localStorage.setItem('tidalhires_filter_status', $('filter-status').value);
    localStorage.setItem('tidalhires_filter_decision', $('filter-decision').value);
  } catch (e) {
    if (e.message !== 'auth') {
      showToast('Refresh failed: ' + e.message, 'error');
    }
  }
}

function renderIntegrations(connectors) {
  if (!connectors.length) {
    $('integrations-body').innerHTML = '<span class="muted">No connectors registered.</span>';
    return;
  }
  const groups = [
    ['source', 'Sources'],
    ['verifier', 'Verifiers'],
    ['output', 'Outputs'],
  ];
  $('integrations-body').innerHTML = groups.map(([kind, label]) => {
    const items = connectors.filter(c => c.kind === kind);
    if (!items.length) return '';
    return `
      <section class="integration-section">
        <h2>${label}</h2>
        <div class="connector-grid">
          ${items.map(renderConnectorCard).join('')}
        </div>
      </section>
    `;
  }).join('');
}

function statusBadge(status) {
  const map = {
    ok: ['success', 'OK'],
    degraded: ['warning', 'Degraded'],
    blocked: ['danger', 'Blocked'],
    missing: ['danger', 'Missing'],
    disabled: ['neutral', 'Disabled'],
  };
  return map[status] || ['neutral', status || 'Unknown'];
}

function yesNo(value) {
  return value ? 'yes' : 'no';
}

function tagList(items) {
  if (!items || !items.length) return 'none';
  return `<span class="connector-tags">${items.map(item => `<span class="connector-tag">${esc(item)}</span>`).join('')}</span>`;
}

function renderInstallGuidance(connector) {
  const guidance = connector.install_guidance || {};
  if (!guidance.show) return '';
  const actions = guidance.actions || [];
  const actionList = actions.length
    ? `<ul>${actions.map(item => `<li>${esc(item)}</li>`).join('')}</ul>`
    : '';
  return `
    <div class="connector-guidance">
      <strong>Install guidance</strong>
      <div>${esc(guidance.reason || 'Connector needs attention.')}</div>
      ${actionList}
    </div>
  `;
}

function renderConnectorCard(connector) {
  const manifest = connector.manifest || {};
  const runtime = connector.runtime || {};
  const [badgeClass, badgeText] = statusBadge(runtime.health);
  const stateClass = runtime.health || 'unknown';
  const requiredClass = manifest.required ? 'required' : '';
  const version = runtime.version || 'unknown';
  const minVersion = manifest.min_supported_version || 'none';
  const service = manifest.docker_service || 'none';
  const profile = manifest.install_profile || 'none';
  const docsUrl = manifest.docs_url || '';
  const docs = docsUrl ? `<a class="connector-link" href="${esc(docsUrl)}" target="_blank" rel="noreferrer">docs</a>` : 'none';
  const err = runtime.last_error ? `<div class="k">Last error</div><div class="v">${esc(runtime.last_error)}</div>` : '';
  const mode = runtime.mode || 'disabled';
  const modeOptions = ['disabled', 'dry_run', 'import'].map(item => {
    const selected = item === mode ? ' selected' : '';
    return `<option value="${item}"${selected}>${item}</option>`;
  }).join('');
  return `
    <article class="connector-card ${requiredClass} ${esc(stateClass)}">
      <div class="connector-head">
        <div>
          <div class="connector-title">${esc(connector.display_name || manifest.display_name || connector.id)}</div>
          <div class="connector-id">${esc(connector.id || manifest.id || '')}</div>
        </div>
        <span class="badge ${badgeClass}">${esc(badgeText)}</span>
      </div>
      <div class="connector-meta">
        <div class="k">Runtime</div><div class="v">${esc(runtime.mode || 'unknown')} · installed ${yesNo(runtime.installed)} · enabled ${yesNo(runtime.enabled)}</div>
        <div class="k">Required</div><div class="v">${yesNo(manifest.required)}</div>
        <div class="k">Version</div><div class="v">${esc(version)} · min ${esc(minVersion)}</div>
        <div class="k">Service</div><div class="v">${esc(service)} · profile ${esc(profile)}</div>
        <div class="k">Required env</div><div class="v">${tagList(manifest.required_env)}</div>
        <div class="k">Optional env</div><div class="v">${tagList(manifest.optional_env)}</div>
        <div class="k">Capabilities</div><div class="v">${tagList(manifest.capabilities)}</div>
        <div class="k">Last check</div><div class="v">${esc(runtime.last_checked_at || 'unknown')} · ${docs}</div>
        ${err}
      </div>
      ${renderInstallGuidance(connector)}
      <div class="connector-controls">
        <select id="connector-mode-${esc(connector.id)}" aria-label="Mode for ${esc(connector.id)}">${modeOptions}</select>
        <button onclick="saveConnectorConfig('${esc(connector.id)}', true)">Dry run</button>
        <button class="primary" onclick="saveConnectorConfig('${esc(connector.id)}', false)">Apply</button>
      </div>
    </article>
  `;
}

async function saveConnectorConfig(connectorId, dryRun) {
  const select = $('connector-mode-' + connectorId);
  if (!select) return;
  try {
    const resp = await api('/connectors/' + encodeURIComponent(connectorId) + '/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: select.value, dry_run: dryRun})
    });
    const data = await resp.json();
    if (!resp.ok || data.valid === false) {
      showToast((data.errors || ['Connector config rejected']).join('; '), 'error');
      return;
    }
    showToast(dryRun ? 'Connector config is valid.' : 'Connector config saved.', 'success');
    if (!dryRun) refresh();
  } catch (e) {
    if (e.message !== 'auth') showToast('Connector config failed: ' + e.message, 'error');
  }
}

function renderActiveJobs(jobs) {
  $('active-jobs-panel').style.display = jobs.length ? '' : 'none';
  if (!jobs.length) {
    $('active-jobs-body').innerHTML = '<span class="muted">No active worker jobs.</span>';
    return;
  }
  $('active-jobs-body').innerHTML = `
    <table class="file-evidence">
      <thead><tr><th>Job</th><th>Type</th><th>State</th><th>Progress</th><th>Attempts</th><th>JID</th><th></th></tr></thead>
      <tbody>${jobs.map(j => {
        const p = j.progress || {};
        const pct = Math.max(0, Math.min(100, Number(p.percent ?? 0)));
        const stage = p.stage || j.state || '';
        const message = p.message || '';
        return `
        <tr>
          <td>${j.id}</td>
          <td>${esc(j.type || '')}</td>
          <td>${esc(j.state || '')}</td>
          <td class="progress-cell">
            <div class="progress-meta"><span class="progress-stage">${esc(stage)}</span><span class="progress-percent">${pct}%</span></div>
            <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
            <div class="progress-message">${esc(message)}</div>
          </td>
          <td>${j.attempts ?? 0}/${j.max_attempts ?? 0}</td>
          <td class="jid">${esc((j.jid || '').slice(0, 12))}</td>
          <td><button onclick="cancelJob(${j.id})" ${j.state === 'cancelling' ? 'disabled' : ''}>${j.state === 'cancelling' ? 'Cancelling' : 'Cancel'}</button></td>
        </tr>
      `}).join('')}</tbody>
    </table>
  `;
}

function buildFilterParams() {
  const params = [];
  const s = $('filter-status').value;
  const d = $('filter-decision').value;
  if (s) params.push('status=' + encodeURIComponent(s));
  if (d) params.push('decision=' + encodeURIComponent(d));
  return params.join('&');
}

function connectorHealth(connectors, connectorId, fallback) {
  const c = (connectors || []).find(item => item.id === connectorId);
  return (c && c.runtime && c.runtime.health) || fallback || 'unknown';
}

function renderSummary(s, connectors=[]) {
  const c = s.counts;
  const commands = s.queue.lidarr_commands || {active_count: 0, blocking_count: 0, commands: []};
  const flacHealth = connectorHealth(connectors, 'flac_detective', s.stack_health.flac_detective);
  const lidarrHealth = connectorHealth(connectors, 'lidarr_manual_import', s.stack_health.lidarr);
  const cards = [
    {label: 'Total', val: c.total_decisions, cls: ''},
    {label: 'Imported', val: c.imported, cls: c.imported > 0 ? 'ok' : ''},
    {label: 'Needs review', val: c.needs_review, cls: c.needs_review > 0 ? 'warn' : ''},
    {label: 'Pending import', val: c.pending, cls: c.pending > 0 ? 'attention' : ''},
    {label: 'Policy alerts', val: c.policy_violations || 0, cls: c.policy_violations > 0 ? 'warn' : ''},
    {label: 'Failed', val: c.failed, cls: c.failed > 0 ? 'warn' : ''},
    {label: 'Active jobs', val: c.active_jobs, cls: ''},
    {label: 'Lidarr queue', val: s.queue.lidarr_queue_total ?? '?', cls: ''},
    {label: 'Lidarr commands', val: commands.active_count ?? '?', cls: (commands.blocking_count || 0) > 0 ? 'attention' : ''},
  ];
  $('summary-grid').innerHTML = cards.map(c =>
    `<div class="card ${c.cls}"><div class="label">${c.label}</div><div class="val">${c.val}</div></div>`
  ).join('');
  $('meta').textContent = `${c.total_decisions} records · Stack: tidalhires=${s.stack_health.tidalhires}, flac-detective=${flacHealth}, lidarr=${lidarrHealth}`;
  renderLidarrCommands(commands);
}

function fmtAge(sec) {
  if (sec === null || sec === undefined) return 'unknown';
  if (sec >= 3600) return (sec / 3600).toFixed(1) + 'h';
  if (sec >= 60) return Math.floor(sec / 60) + 'm';
  return Math.max(0, Math.floor(sec)) + 's';
}

function renderLidarrCommands(commands) {
  const blocking = (commands.commands || []).filter(c => c.blocking);
  $('lidarr-command-panel').style.display = blocking.length ? '' : 'none';
  if (!blocking.length) {
    $('lidarr-command-body').innerHTML = '<span class="muted">No blocking Lidarr commands.</span>';
    return;
  }
  $('lidarr-command-body').innerHTML = `
    <div class="command-list">
      ${blocking.map(c => `
        <div class="command-item">
          <span class="name">${esc(c.name || '')} #${esc(c.id ?? '')}</span>
          <span>${esc(c.status || '')}</span>
          <span class="age">${fmtAge(c.age_sec)}</span>
          <span class="reason">${esc(c.blocking_reason || c.message || 'Blocking Lidarr queue')}</span>
        </div>
      `).join('')}
    </div>
  `;
}

function fmtSec(v) {
  if (!v && v !== 0) return '—';
  if (v >= 60) return (v / 60).toFixed(1) + 'm';
  return Number(v).toFixed(1) + 's';
}

function renderTimings(t) {
  const stages = t.stages || {};
  const entries = Object.entries(stages);
  if (!entries.length || !t.sample_count) {
    $('timings-body').innerHTML = '<span class="muted">No timing data yet; new jobs will populate this.</span>';
    return;
  }
  $('timings-body').innerHTML = `
    <div class="meta">${t.sample_count} timed jobs in ${esc(t.window)}${t.regression_flag ? ' · regression flag' : ''}</div>
    <div class="timing-grid">
      ${entries.map(([stage, stats]) => `
        <div class="timing-item">
          <span class="stage">${esc(stage)}</span>
          <span class="numbers">med ${fmtSec(stats.median)} · p95 ${fmtSec(stats.p95)}</span>
        </div>
      `).join('')}
    </div>
  `;
}

const STATUS_BADGES = {
  needs_review: ['warning', '⚠ Needs review'],
  imported: ['success', '✓ Imported'],
  promoted: ['info', '★ Promoted'],
  failed: ['danger', '✗ Failed'],
  pending: ['warning', '… Pending'],
  policy_violation: ['danger', '! Policy alert'],
  blocked: ['neutral', '⊘ Blocked'],
  discarded: ['neutral', '× Discarded'],
  expired: ['neutral', '× Expired'],
};

function renderRecords(recs) {
  if (!recs.length) {
    $('records-body').innerHTML = '<tr><td colspan="9" class="muted">No records match these filters</td></tr>';
    return;
  }
  $('records-body').innerHTML = recs.map(r => {
    const [cls, txt] = STATUS_BADGES[r.derived_status] || ['neutral', r.derived_status];
    const albumId = r.album_ids[0];
    const deeplink = albumId
      ? `<a class="deeplink" href="${LIDARR_WEB_BASE}/album/${albumId}" target="_blank" onclick="event.stopPropagation()" title="Open in Lidarr">↗</a>`
      : '';
    const when = r.ts_iso ? r.ts_iso.slice(11,19) : '';
    return `<tr onclick="openDrawer('${r.jid}')">
      <td><span class="badge ${cls}" title="${esc(r.status_reason || '')}">${txt}</span></td>
      <td class="reason" title="${esc(r.status_reason || '')}">${esc(r.status_reason || '')}</td>
      <td class="title">${esc(r.title || '')}</td>
      <td>${esc(r.verification_decision || '')}</td>
      <td>${esc(r.import_outcome || '')}</td>
      <td>${esc(r.lifecycle_state || '')}</td>
      <td>${r.score ?? ''}</td>
      <td class="jid">${(r.jid || '').slice(0,8)} · ${when}</td>
      <td>${deeplink}</td>
    </tr>`;
  }).join('');
}

async function openDrawer(jid) {
  activeDrawerJid = jid;
  $('drawer').classList.add('open');
  $('drawer-overlay').classList.add('open');
  $('drawer-body').innerHTML = '<p class="muted">Loading…</p>';
  $('drawer-actions').innerHTML = '';
  try {
    const resp = await api('/record/' + jid);
    if (!resp.ok) throw new Error('not found');
    const d = await resp.json();
    if (activeDrawerJid !== jid) return;
    renderDrawer(d);
  } catch (e) {
    if (activeDrawerJid !== jid) return;
    $('drawer-body').innerHTML = '<p class="muted">Failed to load record</p>';
  }
}
function closeDrawer() {
  activeDrawerJid = null;
  $('drawer').classList.remove('open');
  $('drawer-overlay').classList.remove('open');
}

function renderDrawer(d) {
  $('drawer-title').textContent = d.context.title || d.jid;
  $('drawer-jid').textContent = 'JID: ' + d.jid;
  const v = d.verification;
  const comps = v.components || {};
  const compsRows = Object.entries(comps).map(([k,val]) => `<div class="kvrow"><span class="k">${k}</span><span class="v">${val}</span></div>`).join('');
  const timings = d.timings || {};
  const timingRows = Object.entries(timings)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k,val]) => `<div class="kvrow"><span class="k">${esc(k)}</span><span class="v">${Number(val).toFixed(1)}s</span></div>`).join('');
  const overrides = (v.overrides || []).length ? v.overrides.join(', ') : '—';
  const reviewBox = v.review_reason ? `<section><h2 style="color:var(--status-warning)">Why this needs review</h2><p>${esc(v.review_reason)}</p></section>` : '';
  const sensorRows = (d.sensors || []).map(s => {
    const badgeClass = s.status === 'pass' ? 'success' : (s.status === 'fail' ? 'danger' : (s.status === 'warn' ? 'warning' : 'neutral'));
    const runtime = s.duration_ms || s.duration_ms === 0 ? fmtSec(Number(s.duration_ms) / 1000) : '—';
    return `<div class="kvrow"><span class="k"><span class="badge ${badgeClass}">${esc(s.status || '')}</span> ${esc(s.name || '')}</span><span class="v">${esc(runtime)} · ${esc(s.summary || '')}</span></div>`;
  }).join('');
  const fileRows = (d.files || []).slice(0, 40).map(f => {
    const sr = f.sample_rate ? `${f.sample_rate} Hz` : '—';
    const bits = f.bit_depth ? `${f.bit_depth} bit` : '—';
    const cutoff = f.cutoff_hz ? `${f.cutoff_hz} Hz` : '—';
    const nyquist = f.nyquist_hz ? `${f.nyquist_hz} Hz` : '—';
    const verdict = f.detective_verdict || (f.error ? 'ERROR' : '—');
    return `<tr><td title="${esc(f.filename || '')}">${esc(f.filename || '')}</td><td>${esc(sr)}</td><td>${esc(bits)}</td><td>${esc(cutoff)}</td><td>${esc(nyquist)}</td><td>${esc(verdict)}</td></tr>`;
  }).join('');
  const media = d.media || {available: false, reason: 'Audio review unavailable for this record.'};
  const mediaSection = media.available ? `
    <section>
      <h2>Audio review</h2>
      <audio controls preload="none" style="width:100%" src="${API}/audio-sample/${encodeURIComponent(d.jid)}?apikey=${encodeURIComponent(apikey)}"></audio>
      <div class="media-note">20s sample from the first retained audio file. Use this only as manual review evidence, not as the policy decision.</div>
      <img class="spectrum" alt="Spectrum preview with time and frequency legend" title="Click to enlarge spectrum" loading="lazy" data-title="${esc(d.context.title || d.jid)}" src="${API}/spectrum/${encodeURIComponent(d.jid)}?apikey=${encodeURIComponent(apikey)}" onclick="openImageOverlay(this.src, this.dataset.title)" onerror="this.style.display='none'; this.nextElementSibling.textContent='Spectrum unavailable for this record.'">
      <div class="media-note">Click to enlarge. X axis is time; Y axis is frequency. A hard shelf near the top can indicate upsampled hi-res.</div>
    </section>
  ` : (media.review_relevant ? `
    <section>
      <h2>Audio review</h2>
      <div class="media-unavailable">${esc(media.reason || 'Audio review unavailable for this record.')}</div>
    </section>
  ` : '');

  $('drawer-body').innerHTML = `
    ${reviewBox}
    <section>
      <h2>Status explanation</h2>
      <p>${esc(d.status_reason || 'No status explanation available.')}</p>
    </section>
    <section>
      <h2>Decision</h2>
      <div class="kvrow"><span class="k">Decision</span><span class="v">${esc(v.decision || '')}</span></div>
      <div class="kvrow"><span class="k">Outcome</span><span class="v">${esc(v.outcome || '')}</span></div>
      <div class="kvrow"><span class="k">Verdict</span><span class="v">${esc(v.verdict || '')}</span></div>
      <div class="kvrow"><span class="k">Score</span><span class="v">${v.score ?? '—'}</span></div>
      <div class="kvrow"><span class="k">Overrides</span><span class="v">${esc(overrides)}</span></div>
    </section>
    <section>
      <h2>Score components</h2>
      ${compsRows || '<p class="muted">No component data</p>'}
    </section>
    <section>
      <h2>Sensor evidence</h2>
      ${sensorRows || '<p class="muted">No structured sensor evidence yet; older records predate sensors[].</p>'}
    </section>
    <section>
      <h2>File evidence</h2>
      ${fileRows ? `<table class="file-evidence"><thead><tr><th>File</th><th>SR</th><th>Bits</th><th>Cutoff</th><th>Nyquist</th><th>Verdict</th></tr></thead><tbody>${fileRows}</tbody></table>` : '<p class="muted">No per-file evidence yet.</p>'}
    </section>
    <section>
      <h2>Timings</h2>
      ${timingRows || '<p class="muted">No timing data yet; new jobs will record stage timings.</p>'}
    </section>
    ${mediaSection}
    <section>
      <h2>Existing in library</h2>
      <div class="kvrow"><span class="k">Current quality</span><span class="v">${esc(d.context.existing.label)}</span></div>
      <div class="kvrow"><span class="k">Current kbps</span><span class="v">${d.context.existing.kbps ?? '—'}</span></div>
    </section>
    <section>
      <h2>Lidarr context</h2>
      <div id="lidarr-context" class="muted">Loading Lidarr context…</div>
    </section>
    <section>
      <h2>Lifecycle</h2>
      <div class="kvrow"><span class="k">State</span><span class="v">${esc(d.lifecycle.state || '')}</span></div>
      <div class="kvrow"><span class="k">Actor</span><span class="v">${esc(d.lifecycle.actor || '—')}</span></div>
    </section>
  `;

  const actionDefs = {
    promote: {label: 'Promote', cls: 'btn-promote', desc: 'Will import these files manually even though verification flagged them. Risk is accepted.'},
    discard: {label: 'Discard', cls: 'btn-discard', desc: 'Deletes /output/' + d.jid + '/ and blocklists this grab in Lidarr.'},
    retry_import: {label: 'Retry import', cls: 'btn-retry', desc: 'Re-trigger Lidarr ManualImport. Verification is unchanged.'},
  };
  $('drawer-actions').innerHTML = d.available_actions.length
    ? d.available_actions.map(a => `<button class="${actionDefs[a].cls}" onclick="confirmAction('${d.jid}','${a}','${actionDefs[a].label}','${esc(actionDefs[a].desc)}')">${actionDefs[a].label}</button>`).join('')
    : '<div class="muted">No actions available for this state</div>';
  setTimeout(() => loadLidarrContext(d.jid), 0);
}

async function loadLidarrContext(jid) {
  const target = $('lidarr-context');
  if (!target || activeDrawerJid !== jid) return;
  try {
    const resp = await api('/lidarr-context/' + encodeURIComponent(jid));
    if (activeDrawerJid !== jid) return;
    if (!resp.ok) {
      target.innerHTML = '<span class="muted">Lidarr context unavailable</span>';
      return;
    }
    renderLidarrContext(await resp.json());
  } catch (e) {
    if (activeDrawerJid !== jid) return;
    target.innerHTML = '<span class="muted">Lidarr context unavailable</span>';
  }
}

function renderLidarrContext(ctx) {
  const target = $('lidarr-context');
  const album = ctx.album;
  const albumHtml = album
    ? `<div class="kvrow"><span class="k">Album</span><span class="v"><a href="${esc(album.url)}" target="_blank">${esc(album.artist_name || '')} — ${esc(album.title || '')}</a></span></div>
       <div class="kvrow"><span class="k">Tracks</span><span class="v">${album.track_file_count ?? '—'} / ${album.track_count ?? '—'} files</span></div>
       <div class="kvrow"><span class="k">Release</span><span class="v">${esc(album.release_title || '—')}</span></div>`
    : '<p class="muted">No Lidarr album mapping found.</p>';
  const queue = ctx.queue || {};
  const queueHtml = (queue.queue_entries || []).length
    ? queue.queue_entries.map(q => `<div class="kvrow"><span class="k">${esc(q.status || 'queue')}</span><span class="v">${esc(q.title || q.download_id || '')}</span></div>`).join('')
    : '<p class="muted">No matching Lidarr queue entries.</p>';
  const hist = ctx.grab_history || [];
  const histHtml = hist.length
    ? hist.map(h => `<div class="kvrow"><span class="k">${esc(h.event_type || '')}</span><span class="v">${esc((h.indexer || 'unknown') + ' · ' + (h.ts || '') + (h.reason ? ' · ' + h.reason : ''))}</span></div>`).join('')
    : '<p class="muted">No matching Lidarr history entries.</p>';
  target.innerHTML = `
    ${albumHtml}
    <h3>Queue</h3>
    ${queueHtml}
    <h3>Recent history</h3>
    ${histHtml}
  `;
}

function confirmAction(jid, action, label, desc) {
  $('modal-title').textContent = label + '?';
  $('modal-desc').textContent = desc;
  const btn = $('modal-confirm');
  btn.textContent = label;
  btn.className = 'confirm-' + (action === 'retry_import' ? 'retry' : action);
  btn.onclick = () => doAction(jid, action);
  $('modal-overlay').classList.add('open');
}
function closeModal() { $('modal-overlay').classList.remove('open'); }

function openImageOverlay(src, title) {
  $('image-title').textContent = title || 'Spectrum preview';
  $('image-full').src = src;
  $('image-overlay').classList.add('open');
}
function closeImageOverlay() {
  $('image-overlay').classList.remove('open');
  $('image-full').removeAttribute('src');
}

async function doAction(jid, action) {
  closeModal();
  showToast('Processing ' + action + '…');
  try {
    const resp = await fetch(API + '/action/' + jid + '?apikey=' + encodeURIComponent(apikey), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: action})
    });
    if (resp.status === 409) {
      const body = await resp.json();
      showToast('Action not allowed: ' + (body.allowed || []).join(', '), 'error');
      return;
    }
    if (!resp.ok) {
      showToast('Action failed: HTTP ' + resp.status, 'error');
      return;
    }
    showToast(action + ' OK', 'success');
    closeDrawer();
    refresh();
  } catch (e) {
    showToast('Action failed: ' + e.message, 'error');
  }
}

async function cancelJob(jobId) {
  showToast('Cancelling job ' + jobId + '…');
  try {
    const resp = await fetch(API + '/jobs/' + jobId + '/cancel?apikey=' + encodeURIComponent(apikey), {
      method: 'POST'
    });
    if (resp.status === 409) {
      const body = await resp.json().catch(() => ({}));
      showToast(body.error || 'Job already finished', 'error');
      refresh();
      return;
    }
    if (!resp.ok) {
      showToast('Cancel failed: HTTP ' + resp.status, 'error');
      return;
    }
    const body = await resp.json();
    showToast(body.state === 'cancelling' ? 'Cancellation requested' : 'Job cancelled', 'success');
    refresh();
  } catch (e) {
    showToast('Cancel failed: ' + e.message, 'error');
  }
}

function showToast(msg, kind='') {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show ' + kind;
  setTimeout(() => t.classList.remove('show'), 3500);
}

function updateRefreshIndicator() {
  if (!lastUpdate) {
    $('refresh-status').textContent = 'Updated: never';
    return;
  }
  const sec = Math.floor((Date.now() - lastUpdate) / 1000);
  $('refresh-status').textContent = `Updated ${sec}s ago`;
  $('refresh-dot').className = 'refresh-dot' + (sec > 45 ? ' stale' : '');
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeImageOverlay();
    closeModal();
    closeDrawer();
  }
});
</script>

</body>
</html>
"""
