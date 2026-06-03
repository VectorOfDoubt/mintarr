"""
TidalHires — Flask wrapper that exposes TIDAL HiRes (24-bit/88+ kHz)
to Lidarr as a Newznab indexer + SAB download client.

Newznab endpoint: /api?t=caps|search|music  (Lidarr searches albums)
SAB endpoint:     /sabnzbd/api?mode=...      (Lidarr requests download)

Internal:
  - tidalapi for search (re-uses the token from the tidal-dl-ng login container)
  - `tidal-dl-ng dl <url>` for download (subprocess)
  - ffmpeg .m4a → .flac post-process (bit-perfect copy, no re-encoding)
  - Output: /downloads/<job_id>/<artist>/<album>/*.flac  (Lidarr's SAB complete folder)
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from functools import wraps
from html import unescape as html_unescape
from pathlib import Path
from typing import Literal
from urllib.parse import quote, quote_plus, unquote_plus  # noqa: F401
from xml.sax.saxutils import escape as xml_escape

import requests
from flask import Flask, Response, jsonify, request

from adapters.tidal import (  # noqa: F401
    classify_quality as _classify_quality,
    get_session as _get_session,
    release_title as _release_title,
    search_albums as _search_albums,
)
from dashboard import dashboard_bp
from sensor_registry import default_registry
from verification import (
    ImportOutcome,
    VerificationResult,
    apply_overrides,
    compute_components,
    decide,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tidalhires")

app = Flask(__name__)

# ---- Config ----
API_KEY: str = (
    os.environ.get("MINTARR_API_KEY") or os.environ.get("TIDALHIRES_API_KEY") or ""
)
MIN_API_KEY_LEN = 16
if len(API_KEY) < MIN_API_KEY_LEN:
    raise RuntimeError(
        f"MINTARR_API_KEY or TIDALHIRES_API_KEY must be set and at least {MIN_API_KEY_LEN} characters"
    )
DOWNLOAD_BASE = Path(os.environ.get("DOWNLOAD_BASE", "/downloads"))
OUTPUT_BASE = Path(os.environ.get("OUTPUT_BASE", "/output"))  # SAB-style complete-mappe
TIDAL_DL_NG_CONFIG = os.environ.get(
    "TIDAL_DL_NG_CONFIG", "/root/.config/tidal_dl_ng-dev"
)
JOBS_FILE = Path("/config/jobs.json")
DECISIONS_LOG = Path("/config/decisions.jsonl")  # append-only audit-trail
BLOCKED_DECISIONS_DIR = Path("/config/blocked_decisions")
DISCARDED_DIR = Path("/config/discarded")
EXPIRED_REVIEW_DIR = Path("/config/expired_review")

SENSITIVE_REQUEST_KEYS = {
    "apikey",
    "api_key",
    "x-api-key",
    "x_api_key",
    "password",
    "token",
}


def _redact_request_values(values) -> dict:
    """Return request values safely for logging without API keys/secrets."""
    redacted = {}
    for key in values:
        redacted[key] = (
            "<redacted>" if key.lower() in SENSITIVE_REQUEST_KEYS else values.get(key)
        )
    return redacted


def _sab_queue_slot(jid: str, job: dict) -> dict:
    total_bytes = int(job.get("size") or 0)
    percent = max(0, min(100, int(job.get("percent") or 0)))
    left_bytes = (
        max(0, total_bytes - int(total_bytes * percent / 100)) if total_bytes else 0
    )
    status = job.get("status")
    if status == "downloading":
        sab_status = "Downloading"
        timeleft = "0:00:30" if left_bytes else "0:00:00"
    elif status == "processing":
        sab_status = "Verifying"
        timeleft = "0:00:00"
        percent = 100
        left_bytes = 0
    else:
        sab_status = "Queued"
        timeleft = "0:00:00"
    return {
        "nzo_id": jid,
        "filename": job.get("title", "?"),
        "cat": job.get("category", "music"),
        "priority": "Normal",
        "size": str(total_bytes),
        "sizeleft": str(left_bytes),
        "mb": f"{total_bytes / (1024 * 1024):.2f}",
        "mbleft": f"{left_bytes / (1024 * 1024):.2f}",
        "percentage": str(percent),
        "status": sab_status,
        "timeleft": timeleft,
    }


def _v2_verification_enabled() -> bool:
    return os.environ.get("V2_VERIFICATION_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _rescue_rescan_enabled() -> bool:
    try:
        import state_db

        row = state_db.get_connector_config("lidarr_rescue_rescan")
        if row is not None and row.get("mode") != "import":
            return False
    except Exception:
        pass
    value = os.environ.get("MINTARR_RESCUE_RESCAN_ENABLED")
    if value is None:
        value = os.environ.get("TIDALHIRES_RESCUE_RESCAN_ENABLED", "true")
    return value.lower() in ("1", "true", "yes", "on")


def _connector_import_mode_enabled(connector_id: str) -> bool:
    try:
        import connectors

        connector = connectors.get_connector(connector_id)
        if connector is None:
            return True
        return (
            connectors.configured_mode(connector.manifest)
            == connectors.ConnectorMode.IMPORT.value
        )
    except Exception:
        log.debug(
            "connector mode lookup failed for %s; allowing runtime path",
            connector_id,
            exc_info=True,
        )
        return True


def _adapter_import_mode_enabled(adapter_name: str) -> bool:
    try:
        import connectors

        connector = connectors.connector_for_adapter(adapter_name)
        if connector is None:
            return True
        return (
            connectors.configured_mode(connector.manifest)
            == connectors.ConnectorMode.IMPORT.value
        )
    except Exception:
        log.debug(
            "adapter connector mode lookup failed for %s; allowing runtime path",
            adapter_name,
            exc_info=True,
        )
        return True


def _record_job_timing(jid: str, stage: str, seconds: float) -> None:
    if seconds < 0:
        return
    with _jobs_lock:
        job = _jobs.setdefault(jid, {"id": jid})
        job.setdefault("timings", {})[stage] = round(seconds, 3)
        _save_jobs()


def _job_timings(jid: str) -> dict:
    with _jobs_lock:
        return dict((_jobs.get(jid, {}) or {}).get("timings") or {})


def _set_worker_progress(
    worker_job_id: int | None,
    jid: str,
    stage: str,
    percent: int,
    message: str,
    **extra,
) -> None:
    """Best-effort progress projection to F2 jobs table + SAB-compatible _jobs."""
    percent = max(0, min(100, int(percent)))
    progress = {
        "stage": stage,
        "percent": percent,
        "message": message,
        **extra,
    }
    if worker_job_id is not None:
        try:
            import state_db

            state_db.update_job_progress(worker_job_id, progress)
        except Exception:
            log.exception(
                "[%s] Failed to write worker-progress for job %s", jid, worker_job_id
            )
    with _jobs_lock:
        job = _jobs.setdefault(jid, {"id": jid})
        job["percent"] = percent
        job["stage"] = stage
        if message:
            job["warning"] = message
        _save_jobs()


def _worker_terminal_progress(
    sidecar: dict, job_state: dict | None = None
) -> tuple[str, str, str]:
    """Translate a V2 sidecar into final worker progress.

    Worker completion means the executor finished; operator progress should show
    the business result from V2/Lidarr, especially for failed imports that still
    have a valid sidecar.
    """
    from dashboard import derive_status, status_reason

    enriched = dict(sidecar)
    job_state = job_state or {}
    if job_state.get("error"):
        enriched["job_error"] = job_state.get("error")
    if job_state.get("warning"):
        enriched["job_warning"] = job_state.get("warning")

    result_state = derive_status(enriched)
    message = status_reason(enriched)
    stage = {
        "imported": "done",
        "promoted": "done",
        "failed": "failed",
        "needs_review": "needs_review",
        "blocked": "blocked",
        "pending": "pending",
        "policy_violation": "failed",
        "discarded": "discarded",
        "expired": "expired",
    }.get(result_state, "done")
    return result_state, stage, message


def _with_log_timestamps(rec: dict) -> dict:
    if not rec.get("ts"):
        rec["ts"] = time.time()
    if not rec.get("ts_iso"):
        rec["ts_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return rec


def _legacy_record_from_verification(result: VerificationResult) -> dict:
    return {
        key: value
        for key, value in result.to_decisions_log().items()
        if not key.startswith("v2_") and key not in {"sensors", "files"}
    }


def _decision_record(
    jid: str, v2_result: VerificationResult | None, fields: dict
) -> dict:
    if v2_result is None:
        return _with_log_timestamps({"jid": jid, **fields})

    if v2_result.jid != jid:
        raise ValueError(
            f"VerificationResult jid mismatch: {v2_result.jid!r} != {jid!r}"
        )

    rec = (
        v2_result.to_decisions_log()
        if _v2_verification_enabled()
        else _legacy_record_from_verification(v2_result)
    )
    for key, value in fields.items():
        rec.setdefault(key, value)
    return _with_log_timestamps(rec)


def _log_decision(jid: str, v2_result: VerificationResult | None = None, **fields):
    """Append-only log for every import decision, used for audit and tuning."""
    rec = _decision_record(jid, v2_result, fields)
    try:
        DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DECISIONS_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        log.exception("Decision-log write failed")


DOWNLOAD_BASE.mkdir(parents=True, exist_ok=True)
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

# ---- Job state (persistent on disk) ----
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_promote_locks: dict[str, threading.Lock] = {}
_promote_locks_lock = threading.Lock()
_dashboard_media_prewarm_lock = threading.Lock()
_dashboard_media_prewarm_inflight: set[str] = set()


def _load_jobs():
    """Last jobs fra disk + zombie-cleanup. Markerer downloading/processing-jobber
    older than 30 min as failed (likely killed by container restart)."""
    global _jobs
    if JOBS_FILE.exists():
        try:
            _jobs = json.loads(JOBS_FILE.read_text())
        except Exception:
            _jobs = {}
    # Zombie-cleanup ved boot
    now = time.time()
    zombie_count = 0
    for jid, j in _jobs.items():
        if j.get("status") in ("downloading", "queued", "processing"):
            age = now - j.get("created_at", now)
            if age > 1800:  # 30 min
                j["status"] = "failed"
                j["error"] = "container restart / stale download (auto-cleanup)"
                j["completed_at"] = now
                zombie_count += 1
    if zombie_count:
        log.warning(
            f"Zombie-cleanup ved boot: markerte {zombie_count} stale jobber som failed"
        )
        _save_jobs()


def _save_jobs():
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(json.dumps(_jobs, indent=2, default=str))


# ---- F3.2/F3.3 source routing + multi-adapter newznab ----

_SOURCE_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_NZB_META_RE = re.compile(r'<meta\s+type="([^"]+)">([^<]*)</meta>')


def _b64url_encode(s: str) -> str:
    """URL-safe base64 without padding. Roundtrip-stable for arbitrary strings."""
    return base64.urlsafe_b64encode(s.encode("utf-8")).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> str:
    """Inverse of _b64url_encode. Raises ValueError on malformed input."""
    if not s:
        raise ValueError("empty base64url token")
    if not _B64URL_RE.fullmatch(s):
        raise ValueError("invalid base64url token")
    pad = "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s + pad, altchars=b"-_", validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("invalid base64url token") from exc


def _validate_source_name(source: str | None) -> str:
    """Enforce the source-name shape used by adapter.name. Raises ValueError on bad input."""
    if not source or not _SOURCE_NAME_RE.match(source):
        raise ValueError(f"invalid source name: {source!r}")
    return source


def _parse_nzb_meta(content: str) -> dict[str, str]:
    """Extract <meta type="X">val</meta> pairs from NZB body."""
    return {
        m.group(1): html_unescape(m.group(2)) for m in _NZB_META_RE.finditer(content)
    }


def _parse_source_name(name: str, files) -> tuple[str, str] | None:
    """Resolve (source, raw_id) from SAB addurl name or uploaded NZB body.

    Direct addurl uses exact prefix-match so local rel-paths with spaces or
    slashes survive intact. NZB uploads prefer structured meta emitted by
    download_nzb(); legacy whitespace regex is only allowed for numeric TIDAL.
    """
    import adapters as _adapters

    for adapter in _adapters.all_adapters():
        prefix = f"{adapter.name}:"
        if name.startswith(prefix):
            return (adapter.name, name[len(prefix) :])

    for f in files.values():
        try:
            content = f.read().decode("utf-8", errors="replace")
            meta = _parse_nzb_meta(content)
            source = meta.get("tidalhires_source")
            source_id = meta.get("tidalhires_source_id")
            if source and source_id:
                return (source, source_id)
            m2 = re.search(r"tidal:?[\s_-]?(\d{6,12})", content)
            if m2:
                return ("tidal", m2.group(1))
        except Exception:
            continue

    m = re.search(r"tidal:?[\s_-]?(\d{6,12})", name)
    if m:
        return ("tidal", m.group(1))
    return None


def _addurl_canonicalize(adapter, raw_id: str, name: str):
    """Per-source canonicalization of raw addurl input.

    Returns (source_id, dedupe_key, title, size_est). Each adapter knows
    how to validate its raw_id and produce a clean title for SAB display.
    """
    if adapter.name == "tidal":
        try:
            album_id = int(raw_id)
        except (TypeError, ValueError):
            raise RuntimeError(f"tidal raw_id must be int: {raw_id!r}")
        title = f"TIDAL album {album_id}"
        size_est = 0
        try:
            from adapters.tidal import (
                get_session as _gs,
                classify_quality as _cq,
                release_title as _rt,
            )

            s = _gs()
            album = s.album(album_id)
            qtag, size_est = _cq(album)
            title = _rt(album, qtag)
        except Exception:
            log.warning("addurl tidal lookup failed for %s", album_id)
        return (album_id, f"tidal:{album_id}", title, size_est)

    if adapter.name == "local":
        from adapters.local_folder import hash_rel as _hr

        rel = adapter.normalize_candidate_id(raw_id)  # raises RuntimeError if invalid
        return (rel, f"local:{_hr(rel)}", f"[Local] {rel}", 0)

    if adapter.name == "soulseek":
        from adapters.soulseek import hash_rel as _hr

        source_id = adapter.normalize_candidate_id(raw_id)
        title = adapter.title_for_candidate_id(source_id)
        return (source_id, f"soulseek:{_hr(source_id)}", title, 0)

    # Generic fallback for future adapters
    return (raw_id, f"{adapter.name}:{raw_id}", f"[{adapter.name.upper()}] {raw_id}", 0)


def _canonicalize_source_id(adapter, source_id: str) -> str:
    """Run per-adapter validation on a decoded source_id before NZB generation."""
    if adapter.name == "local":
        return adapter.normalize_candidate_id(source_id)
    if adapter.name == "soulseek":
        return adapter.normalize_candidate_id(source_id)
    if adapter.name == "tidal":
        return str(int(source_id))  # validates int-shape
    return source_id


def _addurl_callback_url(base_url: str, candidate) -> str:
    """Build /download/<source>/<b64url(source_id)>.nzb?apikey=... callback URL."""
    encoded_id = _b64url_encode(candidate.source_id)
    return (
        f"{base_url}/download/{quote_plus(candidate.source_type)}/{encoded_id}.nzb"
        f"?apikey={quote_plus(API_KEY)}"
    )


def _release_pub_date(year: int | None) -> str:
    """RFC 2822 pub date for newznab item.

    Lidarr validates the weekday against the date, so this must be generated
    rather than built from a fixed weekday string.
    """
    if year and 1900 <= year <= 2100:
        return format_datetime(datetime(year, 1, 1, tzinfo=timezone.utc))
    return format_datetime(datetime(2024, 1, 1, tzinfo=timezone.utc))


def _nzb_pointer(source: str, source_id: str) -> str:
    """Build the .nzb body Lidarr POSTs back to /sabnzbd/api?addurl.

    Strukturert meta lar _parse_source_name plukke source + source_id ut
    again without attempting regex on free-form strings. All fields XML-escaped.
    """
    safe_source = xml_escape(source)
    safe_id = xml_escape(source_id)
    safe_name = xml_escape(f"{source}:{source_id}")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
  <head>
    <meta type="name">{safe_name}</meta>
    <meta type="tidalhires_source">{safe_source}</meta>
    <meta type="tidalhires_source_id">{safe_id}</meta>
  </head>
  <file poster="tidalhires@local" date="{int(time.time())}" subject="{safe_name} yEnc (1/1)">
    <groups><group>tidalhires.fake</group></groups>
    <segments><segment bytes="1" number="1">tidalhires-{safe_source}@local</segment></segments>
  </file>
</nzb>"""


# ---- Newznab XML-bygging ----
def _newznab_caps() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="TidalHires" url="" image=""/>
  <limits max="100" default="50"/>
  <retention days="365"/>
  <registration available="no" open="no"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <music-search available="yes" supportedParams="q,artist,album,year"/>
  </searching>
  <categories>
    <category id="3000" name="Audio">
      <subcat id="3010" name="MP3"/>
      <subcat id="3040" name="Lossless"/>
    </category>
  </categories>
</caps>"""


def _newznab_item(
    title: str,
    guid: str,
    dl_url: str,
    size: int,
    pub_date: str = "",
    attrs: dict | None = None,
) -> str:
    attrs = attrs or {}
    attr_xml = "\n      ".join(
        f'<newznab:attr name="{k}" value="{xml_escape(str(v))}"/>'
        for k, v in attrs.items()
    )
    return f"""    <item>
      <title>{xml_escape(title)}</title>
      <guid isPermaLink="false">{xml_escape(guid)}</guid>
      <link>{xml_escape(dl_url)}</link>
      <comments></comments>
      <pubDate>{xml_escape(pub_date) or "Mon, 01 Jan 2024 00:00:00 +0000"}</pubDate>
      <category>3040</category>
      <enclosure url="{xml_escape(dl_url)}" length="{size}" type="application/x-nzb"/>
      {attr_xml}
    </item>"""


def _newznab_feed(items_xml: str, total: int = 0) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel>
  <atom:link href="" rel="self" type="application/rss+xml"/>
  <title>TidalHires</title>
  <description>TIDAL HiRes 24-bit indexer</description>
  <link></link>
  <language>en-US</language>
  <newznab:response offset="0" total="{total}"/>
{items_xml}
</channel>
</rss>"""


# TIDAL helpers (_classify_quality / _release_title / _search_albums)
# are re-exported above from adapters.tidal — see F3.1 hardening notes.


# ---- Auth ----
def require_apikey_check():
    """Return 401-response if apikey is missing/wrong, None if OK.
    Used by blueprint endpoints that need auth without decorator-pattern."""
    provided = request.args.get("apikey") or request.headers.get("X-Api-Key") or ""
    if not hmac.compare_digest(provided, API_KEY):
        log.warning(
            "AUTH FAIL %s %s from %s key_present=%s key_len=%d",
            request.method,
            request.path,
            request.remote_addr,
            bool(provided),
            len(provided),
        )
        return jsonify({"error": "unauthorized"}), 401
    return None


def require_apikey(fn):
    """Require a matching ?apikey=... query param or X-Api-Key header.
    Returns 401 on mismatch. Uses hmac.compare_digest for constant-time comparison."""

    @wraps(fn)
    def _wrapped(*args, **kwargs):
        auth_resp = require_apikey_check()
        if auth_resp:
            return auth_resp
        return fn(*args, **kwargs)

    return _wrapped


# ---- Newznab endpoints ----
@app.route("/api", methods=["GET"])
@app.route("/newznab/api", methods=["GET"])
@require_apikey
def newznab():
    """F3.3: aggregate ReleaseCandidates from all enabled adapters."""
    import adapters as _adapters

    t = request.args.get("t", "")
    log.info("Newznab GET t=%s args=%s", t, _redact_request_values(request.args))
    if t == "caps":
        return Response(_newznab_caps(), mimetype="application/xml")
    if t in ("search", "music", "tvsearch", "movie"):
        q = request.args.get("q", "")
        artist = request.args.get("artist", "")
        album = request.args.get("album", "")
        year_str = request.args.get("year", "")
        year = int(year_str) if year_str.isdigit() else None
        # Lidarr indexer-test sends an empty query — return a TIDAL test album
        # so the indexer-health check passes. LocalFolder skips empty queries
        # internally (see adapter.search) to avoid exposing arbitrary folders.
        empty_query = not (q or artist or album)
        if empty_query:
            q = "Daft Punk Random Access Memories"

        base = os.environ.get("BASE_URL") or request.host_url.rstrip("/")
        all_candidates = []
        for adapter in _adapters.enabled_adapters():
            if not _adapter_import_mode_enabled(adapter.name):
                continue
            if empty_query and adapter.name != "tidal":
                continue
            try:
                hits = adapter.search(query=q, artist=artist, album=album, year=year)
                if hits:
                    log.info(
                        "[%s] search returned %d candidates", adapter.name, len(hits)
                    )
                    all_candidates.extend(hits)
            except Exception:
                log.exception(
                    "[%s] search failed — continuing with other adapters", adapter.name
                )
        # Adapter-declared priority decides ordering (TIDAL=50 > Local=30).
        all_candidates.sort(key=lambda c: c.priority, reverse=True)

        items = []
        for c in all_candidates:
            attrs = {
                "category": "3040",
                "size": c.size_bytes,
                "guid": c.guid,
            }
            items.append(
                _newznab_item(
                    c.title,
                    c.guid,
                    _addurl_callback_url(base, c),
                    c.size_bytes,
                    pub_date=_release_pub_date(c.year),
                    attrs=attrs,
                )
            )
        return Response(
            _newznab_feed("\n".join(items), len(items)), mimetype="application/xml"
        )
    return Response(_newznab_caps(), mimetype="application/xml")


# ---- SAB endpoint (Lidarr ser oss som SAB-DLclient) ----
@app.route("/sabnzbd/api", methods=["GET", "POST"])
@app.route("/api", methods=["POST"])  # fallback hvis Lidarr POSTer hit
@require_apikey
def sab():
    mode = request.values.get("mode", "")
    log.info(
        "SAB %s mode=%s args=%s",
        request.method,
        mode,
        _redact_request_values(request.values),
    )

    if mode == "version":
        return jsonify({"version": "3.7.2"})
    if mode == "auth":
        return jsonify({"auth": "apikey"})
    if mode == "get_config":
        return jsonify(
            {
                "config": {
                    "misc": {
                        "complete_dir": str(OUTPUT_BASE),
                        "download_dir": str(DOWNLOAD_BASE),
                        "pre_check": False,
                    },
                    "categories": [
                        {
                            "name": "*",
                            "dir": "",
                            "order": 0,
                            "priority": -100,
                            "pp": 3,
                            "script": "None",
                        },
                        {
                            "name": "music",
                            "dir": "",
                            "order": 1,
                            "priority": -100,
                            "pp": 3,
                            "script": "None",
                        },
                    ],
                }
            }
        )
    if mode == "get_cats":
        return jsonify({"categories": ["*", "music"]})
    if mode == "fullstatus":
        return jsonify(
            {
                "status": {
                    "version": "3.7.2",
                    "uptime": "1d",
                    "paused": False,
                    "speedlimit": 0,
                    "speedlimit_abs": "",
                    "downloaders": [],
                }
            }
        )
    if mode == "queue":
        _reconcile_pending_import_jobs()
        with _jobs_lock:
            slots = []
            for jid, j in _jobs.items():
                if j.get("hidden_from_lidarr"):
                    continue
                if j.get("status") in ("queued", "downloading", "processing"):
                    slots.append(_sab_queue_slot(jid, j))
            mb_total = sum(float(slot["mb"]) for slot in slots)
            mb_left = sum(float(slot["mbleft"]) for slot in slots)
            return jsonify(
                {
                    "queue": {
                        "version": "3.7.2",
                        "paused": False,
                        "slots": slots,
                        "noofslots": len(slots),
                        "noofslots_total": len(slots),
                        "speedlimit": "0",
                        "speedlimit_abs": "",
                        "kbpersec": "0",
                        "mbleft": f"{mb_left:.2f}",
                        "mb": f"{mb_total:.2f}",
                        "diskspace1": "999",
                        "diskspace2": "999",
                        "diskspacetotal1": "9999",
                        "diskspacetotal2": "9999",
                    }
                }
            )
    if mode == "history":
        with _jobs_lock:
            slots = []
            for jid, j in list(_jobs.items()):
                if j.get("hidden_from_lidarr"):
                    continue
                if j.get("status") in ("completed", "failed"):
                    slots.append(
                        {
                            "nzo_id": jid,
                            "id": jid,
                            "name": j.get("title", "?"),
                            "category": j.get("category", "music"),
                            "status": "Completed"
                            if j.get("status") == "completed"
                            else "Failed",
                            "storage": j.get("output_dir", ""),
                            "path": j.get("output_dir", ""),
                            "completed": j.get("completed_at", 0),
                            "bytes": j.get("size", 0),
                            "size": str(j.get("size", 0) // (1024 * 1024)) + " MB",
                            "fail_message": j.get("error", "")
                            if j.get("status") == "failed"
                            else "",
                        }
                    )
            return jsonify({"history": {"slots": slots, "noofslots": len(slots)}})
    if mode in ("addurl", "addfile"):
        import adapters as _adapters
        import state_db

        name = request.values.get("name") or request.values.get("nzbname") or ""
        cat = request.values.get("cat", "music")
        # F3.2: dispatch to whichever adapter matches the name prefix or NZB meta.
        parsed = _parse_source_name(name, request.files)
        if not parsed:
            return jsonify(
                {"status": False, "error": "no recognized source prefix"}
            ), 400
        source, raw_id = parsed

        adapter = _adapters.get_adapter(source)
        if adapter is None:
            return jsonify({"status": False, "error": f"unknown source: {source}"}), 400
        if not adapter.is_enabled():
            return jsonify(
                {"status": False, "error": f"source not enabled: {source}"}
            ), 503
        if not _adapter_import_mode_enabled(adapter.name):
            return jsonify(
                {"status": False, "error": f"source not in import mode: {source}"}
            ), 503

        try:
            source_id, dedupe_key, title, size_est = _addurl_canonicalize(
                adapter, raw_id, name
            )
        except RuntimeError as exc:
            return jsonify({"status": False, "error": str(exc)}), 400

        try:
            existing = state_db.find_active_job_by_dedupe(dedupe_key)
            if existing and existing.get("jid"):
                log.info(
                    "[addurl] dedupe hit — returning existing jid=%s for %s",
                    existing["jid"],
                    dedupe_key,
                )
                return jsonify({"status": True, "nzo_ids": [existing["jid"]]})
        except Exception:
            log.debug("[addurl] dedupe check failed (continuing)", exc_info=True)

        jid = uuid.uuid4().hex[:12]
        with _jobs_lock:
            _jobs[jid] = {
                "id": jid,
                "category": cat,
                "status": "queued",
                "title": title,
                "size": size_est,
                "percent": 0,
                "source_type": source,
                "source_id": str(source_id),
                "created_at": time.time(),
            }
            # Backwards-compat: keep album_id on TIDAL jobs for old readers
            if source == "tidal":
                _jobs[jid]["album_id"] = source_id
            _save_jobs()

        job_id = None
        try:
            job_id = state_db.enqueue_job(
                jid=jid,
                type=f"{source}_grab",
                payload={"source_id": source_id, "title": title, "category": cat},
                dedupe_key=dedupe_key,
                source_type=source,
                source_id=str(source_id),
            )
        except Exception:
            log.exception(
                "[addurl] state_db.enqueue_job failed — fallback to direct thread"
            )

        # Race-safe dedupe check after enqueue. _jobs is already published so
        # a fast worker cannot lose progress to an endpoint-side queued write.
        if job_id is not None:
            try:
                queued_job = state_db.get_job(job_id)
            except Exception:
                queued_job = None
            if queued_job and queued_job.get("jid") and queued_job.get("jid") != jid:
                existing_jid = queued_job["jid"]
                with _jobs_lock:
                    _jobs.pop(jid, None)
                    _save_jobs()
                log.info(
                    "[addurl] enqueue dedupe hit — returning existing jid=%s for %s",
                    existing_jid,
                    dedupe_key,
                )
                return jsonify({"status": True, "nzo_ids": [existing_jid]})

        if job_id is None:
            # Sync fallback path only supports TIDAL today (legacy _run_download_job).
            if source != "tidal":
                with _jobs_lock:
                    _jobs.pop(jid, None)
                    _save_jobs()
                return jsonify({"status": False, "error": "queue unavailable"}), 503
            log.warning(
                "[%s] enqueue_job returned None — spawning direct thread (legacy path)",
                jid,
            )
            threading.Thread(
                target=_run_download_job, args=(jid, int(source_id)), daemon=True
            ).start()
        else:
            log.info(
                "[%s] enqueued %s_grab as job_id=%s for %s",
                jid,
                source,
                job_id,
                dedupe_key,
            )

        return jsonify({"status": True, "nzo_ids": [jid]})
    if mode == "delete":
        # Lidarr may call delete to clean up history
        name = (
            request.values.get("name")
            or request.values.get("value")
            or request.values.get("nzo_id")
        )
        if name:
            _hide_from_lidarr(name)
        return jsonify({"status": True})
    return jsonify({"status": False, "error": f"unsupported mode: {mode}"}), 400


# ---- F3.4: LocalFolderAdapter ingest endpoint ----
@app.route("/local/ingest", methods=["POST"])
@require_apikey
def local_ingest():
    """Enqueue a local_grab job for files under LOCAL_INGEST_PATH/<rel-path>.

    POST body: {"path": "Artist/Album"}  (relative to LOCAL_INGEST_PATH)
    Returns:   {"status": True, "nzo_ids": ["<jid>"], "job_id": <int>}

    Endpoint normalizes and validates the path before any job is created;
    download_raw validates again as defense-in-depth.
    """
    import adapters
    from adapters.local_folder import hash_rel
    import state_db

    body = request.get_json(silent=True) or {}
    raw_path = body.get("path")
    if not isinstance(raw_path, str):
        return jsonify({"status": False, "error": "path must be a string"}), 400
    rel = raw_path.strip()
    if not rel:
        return jsonify({"status": False, "error": "path required"}), 400

    adapter = adapters.get_adapter("local")
    if adapter is None or not adapter.is_enabled():
        return jsonify({"status": False, "error": "local adapter not enabled"}), 503
    if not _adapter_import_mode_enabled(adapter.name):
        return jsonify(
            {"status": False, "error": "local adapter not in import mode"}
        ), 503

    try:
        rel = adapter.normalize_candidate_id(rel)
    except RuntimeError as exc:
        return jsonify({"status": False, "error": str(exc)}), 400

    dedupe_key = f"local:{hash_rel(rel)}"
    try:
        existing = state_db.find_active_job_by_dedupe(dedupe_key)
    except Exception:
        log.exception("[local_ingest] dedupe check failed (continuing)")
        existing = None
    if existing and existing.get("jid"):
        return jsonify(
            {
                "status": True,
                "nzo_ids": [existing["jid"]],
                "job_id": existing.get("id"),
            }
        )

    jid = uuid.uuid4().hex[:12]
    title = f"[Local] {rel}"
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid,
            "category": "music",
            "status": "queued",
            "title": title,
            "size": 0,
            "percent": 0,
            "source_type": "local",
            "source_id": rel,
            "created_at": time.time(),
        }
        _save_jobs()

    try:
        job_id = state_db.enqueue_job(
            jid=jid,
            type="local_grab",
            payload={"source_id": rel, "title": title},
            dedupe_key=dedupe_key,
            source_type="local",
            source_id=rel,
        )
    except Exception:
        log.exception("[local_ingest] state_db.enqueue_job failed")
        job_id = None
    if job_id is None:
        with _jobs_lock:
            _jobs.pop(jid, None)
            _save_jobs()
        return jsonify({"status": False, "error": "queue unavailable"}), 503

    try:
        queued_job = state_db.get_job(job_id)
    except Exception:
        queued_job = None
    if queued_job and queued_job.get("jid") and queued_job.get("jid") != jid:
        existing_jid = queued_job["jid"]
        with _jobs_lock:
            _jobs.pop(jid, None)
            _save_jobs()
        log.info(
            "[local_ingest] enqueue dedupe hit — returning existing jid=%s for path=%r",
            existing_jid,
            rel,
        )
        return jsonify(
            {
                "status": True,
                "nzo_ids": [existing_jid],
                "job_id": queued_job.get("id"),
            }
        )

    log.info(
        "[%s] /local/ingest enqueued local_grab job_id=%s path=%r", jid, job_id, rel
    )
    return jsonify({"status": True, "nzo_ids": [jid], "job_id": job_id})


@app.route("/soulseek/ingest", methods=["POST"])
@require_apikey
def soulseek_ingest():
    """Enqueue a soulseek_grab job for files under SOULSEEK_DOWNLOAD_ROOT/<rel-path>."""
    import adapters
    from adapters.soulseek import hash_rel
    import state_db

    body = request.get_json(silent=True) or {}
    raw_path = body.get("path")
    if not isinstance(raw_path, str):
        return jsonify({"status": False, "error": "path must be a string"}), 400
    rel = raw_path.strip()
    if not rel:
        return jsonify({"status": False, "error": "path required"}), 400

    adapter = adapters.get_adapter("soulseek")
    if adapter is None or not adapter.is_enabled():
        return jsonify({"status": False, "error": "soulseek adapter not enabled"}), 503
    if not _adapter_import_mode_enabled(adapter.name):
        return jsonify(
            {"status": False, "error": "soulseek adapter not in import mode"}
        ), 503

    try:
        rel = adapter.normalize_candidate_id(rel)
    except RuntimeError as exc:
        message = str(exc)
        status = (
            409
            if "not settled" in message or "partial download markers" in message
            else 400
        )
        return jsonify({"status": False, "error": message}), status

    dedupe_key = f"soulseek:{hash_rel(rel)}"
    try:
        existing = state_db.find_active_job_by_dedupe(dedupe_key)
    except Exception:
        log.exception("[soulseek_ingest] dedupe check failed (continuing)")
        existing = None
    if existing and existing.get("jid"):
        return jsonify(
            {
                "status": True,
                "nzo_ids": [existing["jid"]],
                "job_id": existing.get("id"),
            }
        )

    jid = uuid.uuid4().hex[:12]
    title = f"[Soulseek] {rel}"
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid,
            "category": "music",
            "status": "queued",
            "title": title,
            "size": 0,
            "percent": 0,
            "source_type": "soulseek",
            "source_id": rel,
            "created_at": time.time(),
        }
        _save_jobs()

    try:
        job_id = state_db.enqueue_job(
            jid=jid,
            type="soulseek_grab",
            payload={"source_id": rel, "title": title},
            dedupe_key=dedupe_key,
            source_type="soulseek",
            source_id=rel,
        )
    except Exception:
        log.exception("[soulseek_ingest] state_db.enqueue_job failed")
        job_id = None
    if job_id is None:
        with _jobs_lock:
            _jobs.pop(jid, None)
            _save_jobs()
        return jsonify({"status": False, "error": "queue unavailable"}), 503

    try:
        queued_job = state_db.get_job(job_id)
    except Exception:
        queued_job = None
    if queued_job and queued_job.get("jid") and queued_job.get("jid") != jid:
        existing_jid = queued_job["jid"]
        with _jobs_lock:
            _jobs.pop(jid, None)
            _save_jobs()
        log.info(
            "[soulseek_ingest] enqueue dedupe hit — returning existing jid=%s for path=%r",
            existing_jid,
            rel,
        )
        return jsonify(
            {
                "status": True,
                "nzo_ids": [existing_jid],
                "job_id": queued_job.get("id"),
            }
        )

    log.info(
        "[%s] /soulseek/ingest enqueued soulseek_grab job_id=%s path=%r",
        jid,
        job_id,
        rel,
    )
    return jsonify({"status": True, "nzo_ids": [jid], "job_id": job_id})


# ---- Download worker ----
def _mark_download_cancelled(jid: str, work_dir: Path | None = None) -> None:
    with _jobs_lock:
        job = _jobs.setdefault(jid, {"id": jid})
        job.update(
            status="failed",
            error="cancelled by user",
            completed_at=time.time(),
            percent=100,
            hidden_from_lidarr=True,
        )
        _save_jobs()
    if work_dir is not None and work_dir.exists():
        try:
            resolved = work_dir.resolve()
            if resolved.is_relative_to(DOWNLOAD_BASE.resolve()):
                _safe_rmtree_under(DOWNLOAD_BASE, work_dir)
            elif resolved.is_relative_to(OUTPUT_BASE.resolve()):
                _safe_rmtree_under(OUTPUT_BASE, work_dir)
        except OSError:
            log.exception(
                "[%s] Failed to clean up cancelled job path %s", jid, work_dir
            )


def _raise_if_job_cancelled(
    worker_job_id: int | None,
    jid: str,
    work_dir: Path | None = None,
    *,
    cleanup: bool = True,
) -> None:
    if worker_job_id is None:
        return
    try:
        import state_db

        if not state_db.is_job_cancel_requested(worker_job_id):
            return
    except Exception:
        log.exception(
            "[%s] Failed to check cancel-flag for worker job %s", jid, worker_job_id
        )
        return
    _mark_download_cancelled(jid, work_dir if cleanup else None)
    import worker

    raise worker.JobCancelled(f"cancel requested for jid={jid}")


def _run_cancellable_subprocess(
    args: list[str],
    *,
    worker_job_id: int | None,
    jid: str,
    work_dir: Path | None,
    timeout: int,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run a long command and terminate it when the worker job is cancelled."""
    if worker_job_id is None:
        return subprocess.run(args, capture_output=True, text=text, timeout=timeout)

    started = time.monotonic()
    with (
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as out,
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as err,
    ):
        proc = subprocess.Popen(args, stdout=out, stderr=err, text=text)
        import worker

        while proc.poll() is None:
            try:
                _raise_if_job_cancelled(worker_job_id, jid, work_dir)
            except worker.JobCancelled:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
                raise
            if time.monotonic() - started > timeout:
                proc.kill()
                proc.wait(timeout=10)
                raise subprocess.TimeoutExpired(args, timeout)
            time.sleep(1)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(
            args,
            proc.returncode,
            stdout=out.read(),
            stderr=err.read(),
        )


def _source_grab_terminal_result(
    jid: str,
    job: dict,
) -> tuple[str | None, dict | None]:
    """Post-pipeline: lookup sidecar and derive worker terminal result_state.

    Shared by tidal_grab and local_grab executors so both surface identical
    sidecar/result_state semantics. V2 business-terminal outcomes (BLOCK/SKIPPED)
    intentionally set _jobs[jid].status='failed' for SAB/Lidarr compat, but
    worker execution itself completed successfully — we read the sidecar to
    distinguish.
    """
    worker_job_id = int(job["id"]) if job.get("id") is not None else None
    try:
        _, sidecar = _read_verification_sidecar(jid)
        if sidecar:
            with _jobs_lock:
                job_state = dict(_jobs.get(jid, {}))
            result_state, stage, message = _worker_terminal_progress(sidecar, job_state)
            _set_worker_progress(worker_job_id, jid, stage, 100, message)
            return (
                result_state,
                {
                    "jid": jid,
                    "verification_decision": sidecar.get("v2_verification_decision"),
                    "import_outcome": sidecar.get("v2_import_outcome"),
                },
            )
    except Exception:
        log.exception("[%s] source-grab post-process sidecar lookup failed", jid)

    with _jobs_lock:
        job_state = dict(_jobs.get(jid, {}))
    if job_state.get("status") == "failed":
        raise RuntimeError(job_state.get("error", "source-grab failed (status=failed)"))
    _set_worker_progress(worker_job_id, jid, "done", 100, "Pipeline complete")
    return ("completed", {"jid": jid, "status": job_state.get("status")})


def _execute_source_grab_job(
    job: dict,
    adapter_name: str,
) -> tuple[str | None, dict | None]:
    """Generic source-grab worker executor (F3.4).

    Replaces per-source executor duplication. Builds a RuntimePipelineContext,
    delegates to pipeline.execute_source_grab, then derives terminal result via
    _source_grab_terminal_result. Adapter must be registered before this runs.
    """
    import adapters
    from adapters.runtime import RuntimePipelineContext
    import pipeline

    payload = json.loads(job.get("payload_json") or "{}")
    jid = job["jid"]
    # Backwards-compatible: pre-F3.4 tidal_grab jobs only have album_id.
    source_id = payload.get("source_id") or payload.get("album_id")
    if not source_id:
        raise ValueError(
            f"{adapter_name}_grab job missing source_id in payload: {payload}"
        )

    adapter = adapters.get_adapter(adapter_name)
    if adapter is None:
        raise RuntimeError(f"{adapter_name} adapter not registered")

    raw_dir = DOWNLOAD_BASE / jid
    raw_dir.mkdir(parents=True, exist_ok=True)
    ctx = RuntimePipelineContext(
        jid=jid,
        worker_job_id=int(job["id"]) if job.get("id") is not None else None,
        raw_dir=raw_dir,
        output_dir=OUTPUT_BASE / jid,
        adapter_name=adapter.name,
    )
    pipeline.execute_source_grab(job, adapter, ctx)
    return _source_grab_terminal_result(jid, job)


def _execute_tidal_grab_job(job: dict) -> tuple[str | None, dict | None]:
    """Thin wrapper around generic source-grab executor for TIDAL."""
    return _execute_source_grab_job(job, "tidal")


def _execute_local_grab_job(job: dict) -> tuple[str | None, dict | None]:
    """Thin wrapper around generic source-grab executor for LocalFolder (F3.4)."""
    return _execute_source_grab_job(job, "local")


def _execute_soulseek_grab_job(job: dict) -> tuple[str | None, dict | None]:
    """Thin wrapper around generic source-grab executor for Soulseek completed folders."""
    return _execute_source_grab_job(job, "soulseek")


def _run_download_job(jid: str, album_id: int, worker_job_id: int | None = None):
    """F3.1: thin wrapper that dispatches to the source adapter pipeline.

    Kept for backwards-compatibility with the sync addurl fallback. New
    callers should go through `pipeline.execute_source_grab` directly.
    """
    import adapters
    from adapters.runtime import RuntimePipelineContext
    import pipeline

    adapter = adapters.get_adapter("tidal")
    if adapter is None:
        log.error("[%s] tidal adapter not registered — cannot start download", jid)
        with _jobs_lock:
            _jobs.setdefault(jid, {"id": jid}).update(
                status="failed",
                error="tidal adapter not registered",
                completed_at=time.time(),
            )
            _save_jobs()
        return

    raw_dir = DOWNLOAD_BASE / jid
    raw_dir.mkdir(parents=True, exist_ok=True)
    ctx = RuntimePipelineContext(
        jid=jid,
        worker_job_id=worker_job_id,
        raw_dir=raw_dir,
        output_dir=OUTPUT_BASE / jid,
        adapter_name=adapter.name,
    )
    job_stub = {
        "id": worker_job_id,
        "jid": jid,
        "payload_json": json.dumps({"source_id": str(album_id), "album_id": album_id}),
    }
    pipeline.execute_source_grab(job_stub, adapter, ctx)


def _get_lidarr_key():
    """Read the Lidarr API key from the mounted config.xml."""
    config_path = os.environ.get("LIDARR_CONFIG_XML", "/lidarr-config/config.xml")
    try:
        with open(config_path) as f:
            content = f.read()
        m = re.search(r"<ApiKey>([a-f0-9]+)</ApiKey>", content)
        if m:
            return m.group(1)
    except Exception:
        pass
    return os.environ.get("LIDARR_API_KEY", "")


QUALITY_KBPS = {
    # Effective content quality (kbps-equivalent) for comparison.
    # FLAC = bit-perfect, better than all MP3 variants. We give it 1411 (CD-rate).
    # FLAC 24bit gets a higher score (we use 3000 = baseline 24/96).
    "Unknown": 0,
    "MP3-8": 8,
    "MP3-16": 16,
    "MP3-24": 24,
    "MP3-32": 32,
    "MP3-40": 40,
    "MP3-48": 48,
    "MP3-56": 56,
    "MP3-64": 64,
    "MP3-80": 80,
    "MP3-96": 96,
    "MP3-112": 112,
    "MP3-128": 128,
    "MP3-160": 160,
    "MP3-192": 192,
    "MP3-224": 224,
    "MP3-256": 256,
    "MP3-320": 320,
    "MP3-VBR-V2": 256,
    "MP3-VBR-V0": 320,
    "OGG Vorbis Q5": 160,
    "OGG Vorbis Q6": 192,
    "OGG Vorbis Q7": 224,
    "OGG Vorbis Q8": 256,
    "OGG Vorbis Q9": 288,
    "OGG Vorbis Q10": 320,
    "AAC-192": 192,
    "AAC-256": 256,
    "AAC-320": 320,
    "AAC-VBR": 256,
    "WMA": 192,
    "FLAC": 1411,
    "ALAC": 1411,
    "FLAC 24bit": 3000,
    "ALAC 24bit": 3000,
    "WAV": 1411,
    "APE": 1411,
    "WavPack": 1411,
}

AUDIO_SUFFIXES = (".flac", ".m4a", ".mp3", ".ogg", ".aac")
RELEASE_FAMILY_REJECTION_MARKERS = (
    "match is not close enough",
    "missing tracks",
    "unmatched tracks",
)


def _count_audio_files(output_dir: Path) -> int:
    return sum(
        1
        for f in output_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in AUDIO_SUFFIXES
    )


def _has_flac_files(output_dir: Path) -> bool:
    return any(
        f.is_file() and f.suffix.lower() == ".flac" for f in output_dir.rglob("*")
    )


def _normalize_track_title_for_match(title: str) -> str:
    """Normalize remaster/edition noise while preserving alternate-version identity."""
    path = Path(title)
    normalized = path.stem if path.suffix.lower() in AUDIO_SUFFIXES else title
    normalized = re.sub(
        r"^\s*(?:disc\s*)?\d{1,2}[\s._-]+(?:\d{1,2}[\s._-]+)?",
        "",
        normalized,
        flags=re.I,
    )
    if " - " in normalized:
        normalized = normalized.rsplit(" - ", 1)[-1]
    normalized = re.sub(
        r"[\[(]\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|remix(?:ed)?|anniversary remaster)\s*[\])]",
        "",
        normalized,
        flags=re.I,
    )
    normalized = re.sub(r"\b(?:\d{4}\s+)?remaster(?:ed)?\b", "", normalized, flags=re.I)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _downloaded_track_names(output_dir: Path) -> set[str]:
    names = set()
    for f in output_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in AUDIO_SUFFIXES:
            normalized = _normalize_track_title_for_match(f.name)
            if normalized:
                names.add(normalized)
    return names


def _track_title_names(tracks: list[dict]) -> set[str]:
    names = set()
    for track in tracks:
        normalized = _normalize_track_title_for_match(track.get("title") or "")
        if normalized:
            names.add(normalized)
    return names


def _score_release_match(
    file_count: int, downloaded_names: set[str], rel: dict, track_titles: set[str]
) -> float:
    rel_tracks = int(rel.get("trackCount") or len(track_titles) or 0)
    count_score = max(0, 100 - abs(rel_tracks - file_count) * 10)
    if downloaded_names and track_titles:
        matches = sum(1 for name in downloaded_names if name in track_titles)
        name_score = (matches / len(downloaded_names)) * 100
    else:
        name_score = 50
    return count_score * 0.4 + name_score * 0.6


def _rejection_reasons(item: dict) -> list[str]:
    return [str(r.get("reason", "")).lower() for r in item.get("rejections") or []]


def _is_release_family_rejection(item: dict) -> bool:
    reasons = _rejection_reasons(item)
    return bool(reasons) and all(
        any(marker in reason for marker in RELEASE_FAMILY_REJECTION_MARKERS)
        for reason in reasons
    )


def _album_ids_from_manualimport(items: list[dict]) -> list[int]:
    return sorted(
        {i["album"]["id"] for i in items if i.get("album") and i["album"].get("id")}
    )


_SOULSEEK_TITLE_STOP_TOKENS = {
    "a",
    "an",
    "and",
    "bit",
    "by",
    "cd",
    "disc",
    "feat",
    "featuring",
    "flac",
    "for",
    "in",
    "khz",
    "lossless",
    "m4a",
    "mp3",
    "of",
    "on",
    "ost",
    "remix",
    "scene",
    "soulseek",
    "the",
    "tidal",
    "to",
    "tracks",
    "vol",
    "volume",
    "web",
}

_SOULSEEK_QUALITY_TOKENS = {
    "16bit",
    "24bit",
    "44khz",
    "48khz",
    "88khz",
    "96khz",
    "192khz",
    "aac",
    "alac",
    "cd",
    "deezer",
    "flac",
    "hires",
    "lossless",
    "mp3",
    "qobuz",
    "remastered",
    "tidal",
    "vinyl",
    "web",
}

_SOULSEEK_RELEASE_FAMILY_TOKENS = {
    "anniversary",
    "bonus",
    "deluxe",
    "edition",
    "expanded",
    "extended",
    "legacy",
    "remaster",
    "remastered",
    "special",
    "version",
}


def _manualimport_album_titles(items: list[dict]) -> list[str]:
    titles: set[str] = set()
    for item in items:
        album = item.get("album") or {}
        title = album.get("title") or album.get("albumTitle")
        if title:
            titles.add(str(title))
    return sorted(titles)


def _soulseek_title_tokens(value: str) -> set[str]:
    text = html_unescape(str(value or "")).lower()
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return {
        token
        for token in text.split()
        if len(token) > 1 and token not in _SOULSEEK_TITLE_STOP_TOKENS
    }


def _soulseek_release_subject(title: str) -> str:
    """Best-effort album-title fragment from a Soulseek/scene release name."""
    value = re.sub(r"\[[^\]]+\]", " ", str(title or "")).strip()
    if " - " in value:
        subject = value.split(" - ", 1)[1]
        return re.sub(r"\((?:19|20)\d{2}\)", " ", subject).strip()

    parts = [part.strip() for part in value.split("-") if part.strip()]
    if len(parts) < 2:
        return value

    subject_parts: list[str] = []
    for part in parts[1:]:
        tokens = _soulseek_title_tokens(part)
        if not tokens:
            continue
        if tokens <= _SOULSEEK_QUALITY_TOKENS or re.fullmatch(
            r"(19|20)\d{2}", part.strip()
        ):
            break
        subject_parts.append(part)
    return " ".join(subject_parts) if subject_parts else value


def _soulseek_album_title_compatible(candidate_title: str, album_title: str) -> bool:
    subject_tokens = _soulseek_title_tokens(_soulseek_release_subject(candidate_title))
    album_tokens = _soulseek_title_tokens(album_title)
    if not subject_tokens or not album_tokens:
        return True
    if subject_tokens == album_tokens:
        return True

    overlap = subject_tokens & album_tokens
    if not overlap:
        return False

    extra_subject_tokens = subject_tokens - album_tokens
    if album_tokens <= subject_tokens and (
        len(album_tokens) >= 2
        or extra_subject_tokens <= _SOULSEEK_RELEASE_FAMILY_TOKENS
    ):
        return True

    jaccard = len(overlap) / len(subject_tokens | album_tokens)
    return jaccard >= 0.55


def _manualimport_target_guard_failure(
    jid: str,
    items: list[dict],
    *,
    source_type: str,
    target_album_id: int | str | None = None,
) -> str | None:
    album_ids = _album_ids_from_manualimport(items)
    if target_album_id is not None and target_album_id != "":
        try:
            expected_album_id = int(target_album_id)
        except (TypeError, ValueError):
            expected_album_id = None
        if expected_album_id is not None:
            mismatched_ids = [aid for aid in album_ids if aid != expected_album_id]
            if mismatched_ids:
                return (
                    "manualimport target album mismatch: "
                    f"expected Lidarr albumId {expected_album_id}, got {mismatched_ids}"
                )

    if source_type != "soulseek":
        return None

    with _jobs_lock:
        candidate_title = str((_jobs.get(jid) or {}).get("title") or "")
    if not candidate_title:
        return None

    album_titles = _manualimport_album_titles(items)
    if not album_titles:
        return None

    mismatches = [
        title
        for title in album_titles
        if not _soulseek_album_title_compatible(candidate_title, title)
    ]
    if not mismatches:
        return None

    return (
        "Soulseek manualimport target album mismatch: "
        f"candidate {candidate_title!r} resolved to Lidarr album(s) {mismatches}"
    )


def _lidarr_album_id_from_record(record: dict) -> int | None:
    album_id = record.get("albumId")
    if album_id is None and isinstance(record.get("album"), dict):
        album_id = record["album"].get("id")
    if album_id is None or album_id == "":
        return None
    try:
        return int(album_id)
    except (TypeError, ValueError):
        return None


def _infer_lidarr_target_album_id(jid: str, api: str, key: str) -> int | None:
    """Infer the Lidarr album targeted by the grab that produced this Mintarr jid."""
    import requests

    try:
        q = requests.get(
            f"{api}/queue?pageSize=200", headers={"X-Api-Key": key}, timeout=10
        ).json()
        for record in q.get("records", []) or []:
            if record.get("downloadId") == jid:
                album_id = _lidarr_album_id_from_record(record)
                if album_id is not None:
                    return album_id
    except Exception:
        log.debug(
            "[%s] could not infer target album from Lidarr queue", jid, exc_info=True
        )

    try:
        h = requests.get(
            f"{api}/history?pageSize=50&sortKey=date&sortDirection=descending",
            headers={"X-Api-Key": key},
            timeout=15,
        ).json()
        for record in h.get("records", []) or []:
            if record.get("downloadId") != jid or record.get("eventType") != "grabbed":
                continue
            album_id = _lidarr_album_id_from_record(record)
            if album_id is not None:
                return album_id
    except Exception:
        log.debug(
            "[%s] could not infer target album from Lidarr history", jid, exc_info=True
        )

    return None


def _manualimport_album_complete(output_dir: Path, items: list[dict]) -> bool:
    audio_count = _count_audio_files(output_dir)
    if audio_count == 0:
        return False
    importable_count = sum(
        1
        for item in items
        if not item.get("rejections")
        and item.get("artist")
        and item.get("album")
        and item.get("tracks")
    )
    return importable_count >= audio_count


def _has_fake_hi_res_signal(detective_result: dict | None) -> bool:
    if not detective_result:
        return False
    return any(
        bool(f.get("is_fake_high_res")) for f in detective_result.get("files", [])
    )


SENSOR_POLICY_VERSION = "pipeline-v2-spec-0.4.4"


def _sensor_result(
    name: str,
    sensor_class: str,
    status: str,
    severity: str,
    confidence: float,
    summary: str,
    evidence: dict,
    *,
    sensor_version: str,
    binary_version: str | None = None,
    duration_ms: int | None = None,
) -> dict:
    return {
        "name": name,
        "sensor_version": sensor_version,
        "binary_version": binary_version,
        "policy_version": SENSOR_POLICY_VERSION,
        "evidence_schema_version": default_registry.get(name).evidence_schema_version,
        "class": sensor_class,
        "status": status,
        "severity": severity,
        "confidence": confidence,
        "duration_ms": duration_ms,
        "summary": summary,
        "evidence": evidence,
    }


def _as_int_or_none(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _detective_file_evidence(
    detective_result: dict | None, output_dir: Path
) -> list[dict]:
    if detective_result and isinstance(detective_result.get("files"), list):
        out = []
        for item in detective_result.get("files") or []:
            raw_path = (
                item.get("filepath") or item.get("path") or item.get("filename") or ""
            )
            try:
                filename = Path(str(raw_path)).name if raw_path else ""
            except Exception:
                filename = str(raw_path)
            sample_rate = _as_int_or_none(item.get("sample_rate"))
            cutoff_hz = _as_int_or_none(item.get("cutoff_freq"))
            nyquist_hz = sample_rate // 2 if sample_rate else None
            overrides = item.get("wrapper_overrides") or []
            out.append(
                {
                    "filename": filename,
                    "size_bytes": item.get("size_bytes"),
                    "sample_rate": sample_rate,
                    "bit_depth": _as_int_or_none(item.get("bit_depth")),
                    "duration_sec": item.get("duration_sec") or item.get("duration"),
                    "estimated_kbps": item.get("estimated_kbps"),
                    "detective_verdict": item.get("verdict"),
                    "cutoff_hz": cutoff_hz,
                    "nyquist_hz": nyquist_hz,
                    "is_fake_high_res": bool(item.get("is_fake_high_res")),
                    "estimated_mp3_bitrate": item.get("estimated_mp3_bitrate"),
                    "wrapper_overrides": list(overrides)
                    if isinstance(overrides, list)
                    else [str(overrides)],
                    "error": item.get("error"),
                }
            )
        return out

    return [
        {"filename": f.name, "size_bytes": f.stat().st_size}
        for f in sorted(output_dir.rglob("*"))
        if f.is_file() and f.suffix.lower() in AUDIO_SUFFIXES
    ]


def _build_sensor_results(
    *,
    output_dir: Path,
    audio_count: int,
    has_flac: bool,
    no_audio_files: bool,
    codec_mismatch: bool,
    validator_error: bool,
    detective_error: str | None,
    detective_result: dict | None,
    normalized_verdict: str,
    job_quality: dict,
) -> list[dict]:
    codec_gate_skipped = int(job_quality.get("codec_gate_skipped") or 0)
    integrity_failed = int(job_quality.get("integrity_failed") or 0)
    flac_count = sum(1 for f in output_dir.rglob("*.flac") if f.is_file())
    fake_hi_res = _has_fake_hi_res_signal(detective_result)

    if no_audio_files and codec_gate_skipped > 0:
        ffprobe_status, ffprobe_severity = "fail", "blocker"
        ffprobe_summary = (
            f"Downloaded {codec_gate_skipped} audio file(s), but all were skipped "
            "because they were not FLAC/ALAC."
        )
    elif no_audio_files:
        ffprobe_status, ffprobe_severity = "fail", "blocker"
        ffprobe_summary = "No audio files were downloaded."
    elif codec_mismatch:
        ffprobe_status, ffprobe_severity = "fail", "blocker"
        ffprobe_summary = "Downloaded files failed the expected FLAC/ALAC codec gate."
    else:
        ffprobe_status, ffprobe_severity = "pass", "none"
        ffprobe_summary = "Audio files passed the expected codec/container gate."

    if no_audio_files and codec_gate_skipped > 0:
        flac_status, flac_severity = "fail", "blocker"
        flac_summary = (
            "No FLAC files remained after the codec gate removed non-FLAC downloads."
        )
    elif no_audio_files or not has_flac:
        flac_status, flac_severity = "fail", "blocker"
        flac_summary = "No FLAC files were available for integrity testing."
    elif integrity_failed:
        flac_status, flac_severity = "warn", "warning"
        flac_summary = (
            f"{integrity_failed} corrupt FLAC file(s) were removed before verification."
        )
    else:
        flac_status, flac_severity = "pass", "none"
        flac_summary = "FLAC files decoded without integrity errors."

    if no_audio_files and codec_gate_skipped > 0:
        detective_status, detective_severity, detective_confidence = (
            "skipped",
            "info",
            0.0,
        )
        detective_summary = (
            "Spectral analysis skipped because the codec gate left no FLAC files."
        )
    elif no_audio_files:
        detective_status, detective_severity, detective_confidence = (
            "skipped",
            "info",
            0.0,
        )
        detective_summary = (
            "Spectral analysis skipped because no audio files were available."
        )
    elif validator_error:
        detective_status, detective_severity, detective_confidence = (
            "fail",
            "blocker",
            0.0,
        )
        detective_summary = f"FLAC Detective unavailable or failed: {detective_error or normalized_verdict}."
    elif normalized_verdict == "AUTHENTIC" and not fake_hi_res:
        detective_status, detective_severity, detective_confidence = "pass", "none", 0.8
        detective_summary = "Overall verdict AUTHENTIC."
    elif normalized_verdict in ("WARNING", "SUSPICIOUS") or fake_hi_res:
        detective_status, detective_severity, detective_confidence = (
            "warn",
            "warning",
            0.8,
        )
        detective_summary = f"Overall verdict {normalized_verdict}; review evidence before trusting this release."
    else:
        detective_status, detective_severity, detective_confidence = (
            "fail",
            "blocker",
            0.8,
        )
        detective_summary = f"Overall verdict {normalized_verdict}."

    wrapper_overrides: list[object] = []
    for item in (detective_result or {}).get("files") or []:
        wrapper_overrides.extend(item.get("wrapper_overrides") or [])

    return [
        _sensor_result(
            "ffprobe",
            "hard_gate",
            ffprobe_status,
            ffprobe_severity,
            1.0,
            ffprobe_summary,
            {
                "audio_count": audio_count,
                "flac_count": flac_count,
                "codec_gate_skipped": codec_gate_skipped,
            },
            sensor_version="tidalhires-ffprobe-wrapper 2026-05-25",
        ),
        _sensor_result(
            "flac_t",
            "hard_gate",
            flac_status,
            flac_severity,
            1.0,
            flac_summary,
            {
                "flac_count": flac_count,
                "integrity_failed": integrity_failed,
            },
            sensor_version="tidalhires-flac-t-wrapper 2026-05-25",
        ),
        _sensor_result(
            "flac_detective",
            "spectral_heuristic",
            detective_status,
            detective_severity,
            detective_confidence,
            detective_summary,
            {
                "overall_verdict": normalized_verdict,
                "file_count": (detective_result or {}).get("file_count"),
                "fake_hi_res": fake_hi_res,
                "wrapper_overrides": wrapper_overrides,
                "error": detective_error,
            },
            sensor_version="tidalhires-flac-detective-api-wrapper 2026-05-25",
        ),
    ]


def _compute_verification(
    jid: str,
    output_dir: Path,
    manualimport_items: list[dict],
    *,
    verdict: str | None,
    detective_error: str | None,
    detective_result: dict | None,
    existing_kbps: int,
    existing_label: str,
    new_effective_kbps: int,
    album_ids: list[int],
    title: str,
    existing_track_count: int = 0,
    new_track_count: int = 0,
    expected_track_count: int = 0,
) -> VerificationResult:
    """Build the V2 verification result from already-collected import context."""
    normalized_verdict = verdict or "UNKNOWN"
    audio_count = _count_audio_files(output_dir)
    has_flac = _has_flac_files(output_dir)
    no_audio_files = audio_count == 0
    no_flac_files = audio_count > 0 and not has_flac
    with _jobs_lock:
        job_quality = dict(_jobs.get(jid, {}))
    codec_gate_skipped = int(job_quality.get("codec_gate_skipped") or 0)
    partial_codec_mismatch = codec_gate_skipped > 0
    validator_error = not no_audio_files and (
        bool(detective_error) or normalized_verdict in ("UNKNOWN", "ERROR")
    )
    codec_mismatch = no_flac_files or partial_codec_mismatch
    components = compute_components(
        ffprobe_ok=audio_count > 0,
        flac_t_ok=not validator_error and has_flac,
        detective_verdict=normalized_verdict,
        complete_album=_manualimport_album_complete(output_dir, manualimport_items),
    )
    score, overrides = apply_overrides(
        components,
        codec_mismatch=codec_mismatch,
        flac_t_failed=False,
        validator_error=validator_error,
        fake_hi_res=_has_fake_hi_res_signal(detective_result),
        detective_verdict=normalized_verdict,
    )
    if no_audio_files and "no_audio_files" not in overrides:
        overrides.append("no_audio_files")
        score = 0
    sensors = _build_sensor_results(
        output_dir=output_dir,
        audio_count=audio_count,
        has_flac=has_flac,
        no_audio_files=no_audio_files,
        codec_mismatch=codec_mismatch,
        validator_error=validator_error,
        detective_error=detective_error,
        detective_result=detective_result,
        normalized_verdict=normalized_verdict,
        job_quality=job_quality,
    )
    files = _detective_file_evidence(detective_result, output_dir)
    decision = decide(
        score,
        existing_kbps,
        new_effective_kbps,
        normalized_verdict,
        overrides,
        existing_track_count=existing_track_count,
        new_track_count=new_track_count,
        expected_track_count=expected_track_count,
    )
    return VerificationResult(
        jid=jid,
        score=score,
        verification_decision=decision,
        components=components,
        overrides=overrides,
        verdict=normalized_verdict,
        new_kbps=0
        if (validator_error or no_audio_files or codec_mismatch)
        else new_effective_kbps,
        existing_kbps=existing_kbps,
        existing_label=existing_label,
        album_ids=album_ids,
        title=title,
        sensors=sensors,
        files=files,
    )


def _set_v2_import_outcome(
    result: VerificationResult | None, outcome: ImportOutcome
) -> None:
    if result is not None:
        result.import_outcome = outcome


BlocklistPolicy = Literal["blocklist_now", "hold_for_review"]


def decide_blocklist_policy(verdict: str, overrides: list[str]) -> BlocklistPolicy:
    if "fake_hi_res" in overrides:
        return "hold_for_review"
    if verdict == "FAKE_CERTAIN":
        return "blocklist_now"
    return "blocklist_now"


def _sidecar_lifecycle(result: VerificationResult) -> dict:
    now = time.time()
    state = (
        "pending_review"
        if result.verification_decision == "REVIEW_REQUIRED"
        else "created"
    )
    policy = (
        decide_blocklist_policy(result.verdict, result.overrides)
        if state == "pending_review"
        else "skipped"
    )
    return {
        "state": state,
        "created_at": now,
        "promoted_at": None,
        "discarded_at": None,
        "expired_at": None,
        "actor": None,
        "blocklist_policy": policy,
        "blocklist_status": "pending" if policy == "blocklist_now" else "skipped",
    }


def _verification_record(
    result: VerificationResult,
    existing: dict | None = None,
    *,
    source_type: str | None = None,
) -> dict:
    record = result.to_decisions_log()
    if existing and existing.get("lifecycle"):
        record["lifecycle"] = existing["lifecycle"]
    else:
        record["lifecycle"] = _sidecar_lifecycle(result)
    timings = _job_timings(result.jid)
    if timings:
        record["timings"] = timings
    # F3.1: persist source_type so future readers can filter per adapter.
    # Absence in a sidecar means legacy TIDAL — readers must default to 'tidal'.
    if source_type is not None:
        record["source_type"] = source_type
    elif existing and existing.get("source_type"):
        record["source_type"] = existing["source_type"]
    else:
        record["source_type"] = "tidal"
    return _with_log_timestamps(record)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, default=str)
        f.write("\n")
        try:
            f.flush()
            os.fsync(f.fileno())
        except OSError:
            pass
    tmp.replace(path)


def _sidecar_output_path(jid: str, output_dir: Path | None = None) -> Path:
    if output_dir is None:
        output_dir = OUTPUT_BASE / jid
    return output_dir / "verification.json"


def _archive_sidecar_path(jid: str, directory: Path) -> Path:
    return directory / f"{jid}.json"


def _find_verification_sidecar(jid: str) -> Path | None:
    candidates = []
    with _jobs_lock:
        output_dir = _jobs.get(jid, {}).get("output_dir")
    if output_dir:
        candidates.append(Path(output_dir) / "verification.json")
    candidates.extend(
        [
            _sidecar_output_path(jid),
            _archive_sidecar_path(jid, BLOCKED_DECISIONS_DIR),
            _archive_sidecar_path(jid, DISCARDED_DIR),
            _archive_sidecar_path(jid, EXPIRED_REVIEW_DIR),
        ]
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def _read_verification_sidecar(jid: str) -> tuple[Path, dict] | tuple[None, None]:
    path = _find_verification_sidecar(jid)
    if path is None:
        return None, None
    try:
        return path, json.loads(path.read_text())
    except Exception:
        log.exception("[%s] Failed to read verification sidecar", jid)
        return None, None


def _prewarm_dashboard_media_worker(jid: str) -> None:
    """Best-effort dashboard media cache generation for retained review items."""
    try:
        import dashboard

        server_mod = sys.modules[__name__]
        for kind in ("audio", "spectrum"):
            path, error = dashboard._media_artifact(server_mod, jid, kind)
            if error:
                log.info("[%s] Dashboard %s prewarm skipped: %s", jid, kind, error)
            elif path is not None:
                log.info("[%s] Dashboard %s prewarmed: %s", jid, kind, path.name)
    except Exception:
        log.exception("[%s] Dashboard media prewarm failed", jid)
    finally:
        with _dashboard_media_prewarm_lock:
            _dashboard_media_prewarm_inflight.discard(jid)


def _maybe_prewarm_dashboard_media(
    jid: str,
    result: VerificationResult,
    output_dir: Path | None,
    archive_dir: Path | None,
) -> None:
    if os.environ.get("TIDALHIRES_DASHBOARD_MEDIA_PREWARM", "1") == "0":
        return
    if archive_dir is not None or output_dir is None:
        return
    if result.verification_decision != "REVIEW_REQUIRED":
        return
    with _dashboard_media_prewarm_lock:
        if jid in _dashboard_media_prewarm_inflight:
            return
        _dashboard_media_prewarm_inflight.add(jid)
    threading.Thread(
        target=_prewarm_dashboard_media_worker, args=(jid,), daemon=True
    ).start()


def _write_verification_sidecar(
    jid: str,
    result: VerificationResult,
    output_dir: Path | None = None,
    *,
    archive_dir: Path | None = None,
    source_type: str | None = None,
) -> Path:
    existing_path, existing = _read_verification_sidecar(jid)
    record = _verification_record(result, existing, source_type=source_type)
    target = (
        _archive_sidecar_path(jid, archive_dir)
        if archive_dir is not None
        else _sidecar_output_path(jid, output_dir)
    )
    _atomic_write_json(target, record)
    if existing_path is not None and existing_path != target and existing_path.exists():
        try:
            existing_path.unlink()
        except OSError:
            log.exception("[%s] Failed to remove old verification sidecar", jid)
    _maybe_prewarm_dashboard_media(jid, result, output_dir, archive_dir)
    # F1: Mirror to SQLite state-index (additive, fail-open — sidecar is source-of-truth)
    try:
        import state_db
        from dashboard import derive_status

        state_db.upsert_from_sidecar(record, derived_status=derive_status(record))
    except Exception:
        log.exception("[%s] state_db mirror failed (sidecar write succeeded)", jid)
    return target


def _maybe_write_v2_sidecar(
    jid: str,
    result: VerificationResult | None,
    output_dir: Path,
    *,
    archive_dir: Path | None = None,
    source_type: str | None = None,
) -> None:
    if _v2_verification_enabled() and result is not None:
        _write_verification_sidecar(
            jid,
            result,
            output_dir,
            archive_dir=archive_dir,
            source_type=source_type,
        )


def _safe_rmtree_under(base: Path, target: Path) -> bool:
    target_resolved = target.resolve()
    base_resolved = base.resolve()
    if target_resolved == base_resolved or not target_resolved.is_relative_to(
        base_resolved
    ):
        log.error(
            "rmtree containment ERROR: %s is not under %s",
            target_resolved,
            base_resolved,
        )
        return False
    shutil.rmtree(target_resolved, ignore_errors=True)
    return True


def _blocklist_grab(jid: str, api: str, key: str) -> bool:
    import requests

    try:
        h = requests.get(
            f"{api}/history?pageSize=50&sortKey=date&sortDirection=descending",
            headers={"X-Api-Key": key},
            timeout=15,
        ).json()
        for rec in h.get("records", []):
            if rec.get("downloadId") == jid and rec.get("eventType") == "grabbed":
                resp = requests.post(
                    f"{api}/history/failed/{rec['id']}",
                    headers={"X-Api-Key": key},
                    json={},
                    timeout=15,
                )
                return resp.status_code in (200, 201, 202)
    except Exception:
        log.exception("[%s] Blocklist API failed", jid)
    return False


def _count_lidarr_imported_history(jid: str, api: str, key: str) -> int:
    """Count recent Lidarr import events for the same downloadId.

    With replaceExistingFiles the trackfile count can stay equal before/after ManualImport.
    History events are therefore necessary to tell an actual replace-import from a failure.
    """
    import requests

    try:
        h = requests.get(
            f"{api}/history?pageSize=100&sortKey=date&sortDirection=descending",
            headers={"X-Api-Key": key},
            timeout=15,
        ).json()
        return sum(
            1
            for rec in h.get("records", [])
            if rec.get("downloadId") == jid
            and rec.get("eventType") == "trackFileImported"
        )
    except Exception:
        log.exception("[%s] Failed to read Lidarr import history", jid)
    return 0


def _count_lidarr_trackfiles(album_ids, api: str, key: str) -> int:
    """Count current Lidarr trackfiles for album ids."""
    import requests

    total = 0
    for aid in set(album_ids or []):
        try:
            tfs = requests.get(
                f"{api}/trackfile?albumId={aid}", headers={"X-Api-Key": key}, timeout=10
            ).json()
            total += len(tfs)
        except Exception:
            log.exception("Failed to read Lidarr trackfiles for album %s", aid)
    return total


def _count_missing_manualimport_sources(files: list[dict]) -> int:
    """Count ManualImport source files that Lidarr has already moved away from output."""
    missing = 0
    for item in files:
        path = str(item.get("path") or "")
        src_path = Path(path.replace("/downloads/TidalHiRes/complete/", "/output/"))
        if not src_path.exists():
            missing += 1
    return missing


def _count_manualimport_progress(
    jid: str,
    api: str,
    key: str,
    files: list[dict],
    pre_counts: dict,
    import_threshold: int,
    *,
    context: str = "ManualImport",
) -> int:
    """Return the best evidence count for a completed Lidarr ManualImport."""
    import requests

    imported_count = 0
    for aid in pre_counts:
        try:
            tfs_after = requests.get(
                f"{api}/trackfile?albumId={aid}", headers={"X-Api-Key": key}, timeout=10
            ).json()
            imported_count += max(0, len(tfs_after) - pre_counts.get(aid, 0))
        except Exception:
            pass
    if imported_count < import_threshold:
        imported_count = max(
            imported_count, _count_lidarr_imported_history(jid, api, key)
        )

    if imported_count < import_threshold:
        current_trackfiles = _count_lidarr_trackfiles(pre_counts.keys(), api, key)
        missing_sources = _count_missing_manualimport_sources(files)
        if (
            current_trackfiles >= import_threshold
            and missing_sources >= import_threshold
        ):
            log.info(
                "[%s] %s registered files even though command/history did not confirm "
                "(trackfiles=%d, sources missing=%d/%d)",
                jid,
                context,
                current_trackfiles,
                missing_sources,
                len(files),
            )
            imported_count = current_trackfiles

    return imported_count


def _wait_for_manualimport_progress(
    jid: str,
    api: str,
    key: str,
    files: list[dict],
    pre_counts: dict,
    *,
    context: str = "ManualImport",
    timeout_s: int = 20,
    interval_s: int = 2,
) -> tuple[int, int]:
    """Poll Lidarr import evidence and return early when enough files are imported."""
    import_threshold = max(1, (len(files) + 1) // 2)
    attempts = max(1, (timeout_s // interval_s) + 1)
    imported_count = 0
    for attempt in range(attempts):
        imported_count = _count_manualimport_progress(
            jid, api, key, files, pre_counts, import_threshold, context=context
        )
        if imported_count >= import_threshold:
            return imported_count, import_threshold
        if attempt < attempts - 1:
            time.sleep(interval_s)
    return imported_count, import_threshold


def _lidarr_command_still_pending(cmd_response, api: str, key: str) -> bool:
    """Return True when Lidarr accepted a command but has not run it yet."""
    import requests

    try:
        body = cmd_response.json()
    except Exception:
        return False
    status = (body or {}).get("status")
    if status not in ("queued", "started"):
        return False
    cmd_id = (body or {}).get("id")
    if not cmd_id:
        return True
    for _ in range(6):
        try:
            resp = requests.get(
                f"{api}/command/{cmd_id}", headers={"X-Api-Key": key}, timeout=10
            )
            if resp.status_code == 404:
                return False
            current = resp.json()
            if current.get("status") not in ("queued", "started"):
                return False
        except requests.RequestException:
            log.exception("Failed to check Lidarr command status for %s", cmd_id)
            return True
        except Exception:
            log.exception("Uventet Lidarr command-status-respons for %s", cmd_id)
            return False
        time.sleep(5)
    return True


def _lidarr_has_pending_import_for_jid(jid: str, api: str, key: str) -> bool:
    """Return True when Lidarr still has a queued/started ManualImport for this jid."""
    import requests

    try:
        commands = requests.get(
            f"{api}/command", headers={"X-Api-Key": key}, timeout=10
        ).json()
    except Exception:
        log.exception(
            "[%s] Failed to read Lidarr command queue for pending-reconcile", jid
        )
        return True
    for command in commands or []:
        if command.get("name") != "ManualImport":
            continue
        if command.get("status") not in ("queued", "started"):
            continue
        files = (command.get("body") or {}).get("files") or []
        for item in files:
            if item.get("downloadId") == jid or f"/{jid}/" in str(
                item.get("path") or ""
            ):
                return True
    return False


def _reconcile_pending_import(jid: str, record: dict, path: Path | None = None) -> dict:
    """Refresh a PENDING V2 import from Lidarr history without starting new work."""
    if record.get("v2_import_outcome") != "PENDING":
        return record
    api = os.environ.get("LIDARR_API_URL", "http://host.docker.internal:8686/api/v1")
    key = _get_lidarr_key()
    if not key:
        return record

    if _count_lidarr_imported_history(jid, api, key) > 0:
        record["v2_import_outcome"] = "MANUAL_IMPORTED"
        target = path or _find_verification_sidecar(jid)
        if target is not None:
            _atomic_write_json(target, record)
        output_dir = Path(_jobs.get(jid, {}).get("output_dir") or OUTPUT_BASE / jid)
        _complete_lidarr_import_without_queue_delete(jid, output_dir)
        return record

    if record.get("v2_verification_decision") in ("ACCEPT", "ACCEPT_PROVISIONAL"):
        output_dir = Path(_jobs.get(jid, {}).get("output_dir") or OUTPUT_BASE / jid)
        if (
            _count_lidarr_trackfiles(record.get("album_ids") or [], api, key) > 0
            and _count_audio_files(output_dir) == 0
        ):
            record["v2_import_outcome"] = "MANUAL_IMPORTED"
            target = path or _find_verification_sidecar(jid)
            if target is not None:
                _atomic_write_json(target, record)
            _complete_lidarr_import_without_queue_delete(jid, output_dir)
            return record

    if _lidarr_has_pending_import_for_jid(jid, api, key):
        return record

    if record.get("v2_verification_decision") in ("ACCEPT", "ACCEPT_PROVISIONAL"):
        reason = "lidarr manualimport ended without importing files"
        record["v2_import_outcome"] = "FAILED"
        target = path or _find_verification_sidecar(jid)
        if target is not None:
            _atomic_write_json(target, record)
        _mark_import_failed(jid, reason)
        _cleanup_lidarr_queue(jid, api, key)

    return record


def _reconcile_pending_import_jobs() -> None:
    def _needs_pending_import_reconcile(job: dict) -> bool:
        if job.get("status") != "processing":
            return False
        if job.get("stage") == "pending":
            return True
        warning = str(job.get("warning") or "").lower()
        return "manualimport" in warning and "pending" in warning

    with _jobs_lock:
        pending_jids = [
            jid for jid, job in _jobs.items() if _needs_pending_import_reconcile(job)
        ]
    for jid in pending_jids:
        path, record = _read_verification_sidecar(jid)
        if record is not None and record.get("v2_import_outcome") == "PENDING":
            _reconcile_pending_import(jid, record, path)


def _get_promote_lock(jid: str) -> threading.Lock:
    with _promote_locks_lock:
        lock = _promote_locks.get(jid)
        if lock is None:
            lock = threading.Lock()
            _promote_locks[jid] = lock
        return lock


def _mark_import_failed(jid: str, reason: str):
    """Mark a positively verified download as import-fail without blocklisting the release."""
    with _jobs_lock:
        job = _jobs.setdefault(jid, {"id": jid})
        job.update(
            status="failed",
            error=reason,
            completed_at=time.time(),
            percent=100,
            hidden_from_lidarr=True,
        )
        _save_jobs()


def _mark_import_completed(jid: str, output_dir: Path | None = None):
    """Mark finished after V2 verification and our Lidarr import have completed."""
    with _jobs_lock:
        job = _jobs.setdefault(jid, {"id": jid})
        update = {
            "status": "completed",
            "completed_at": time.time(),
            "percent": 100,
            "hidden_from_lidarr": True,
        }
        if output_dir is not None:
            update["output_dir"] = str(output_dir)
        job.update(update)
        _save_jobs()


def _mark_review_required(jid: str, reason: str):
    """Hold files for manual V2 review without marking the download as imported."""
    with _jobs_lock:
        job = _jobs.setdefault(jid, {"id": jid})
        job.update(
            status="review_required",
            warning=reason,
            review_required_at=time.time(),
            completed_at=time.time(),
            output_dir=str(OUTPUT_BASE / jid),
            percent=100,
            hidden_from_lidarr=True,
        )
        _save_jobs()


def _hide_from_lidarr(jid: str) -> None:
    with _jobs_lock:
        job = _jobs.get(jid)
        if job is not None:
            job["hidden_from_lidarr"] = True
            _save_jobs()


def _create_review_required(
    jid: str,
    result: VerificationResult,
    output_dir: Path,
    api: str,
    key: str,
    *,
    source_type: str = "tidal",
) -> None:
    """Atomisk/idempotent creation av REVIEW_REQUIRED sidecar + job state + Lidarr cleanup."""
    lock = _get_promote_lock(jid)
    with lock:
        path = _write_verification_sidecar(
            jid, result, output_dir, source_type=source_type
        )
        record = json.loads(path.read_text())
        lifecycle = record.get("lifecycle", {})
        if (
            lifecycle.get("blocklist_policy") == "blocklist_now"
            and lifecycle.get("blocklist_status") != "done"
        ):
            lifecycle["blocklist_status"] = (
                "done" if _blocklist_grab(jid, api, key) else "failed"
            )
            record["lifecycle"] = lifecycle
            _atomic_write_json(path, record)
        _mark_review_required(jid, f"V2 review required: {result.verdict}")
        _cleanup_lidarr_queue(jid, api, key)


def _trigger_lidarr_import(
    jid: str,
    output_dir: Path,
    worker_job_id: int | None = None,
    *,
    source_type: str = "tidal",
    target_album_id: int | str | None = None,
):
    """After download: smart pre-import evaluation.

    Logic:
    1. ManualImport lookup → gives album_id + existing quality
    2. Call flac-detective
    3. Decision:
       - AUTHENTIC/WARNING → import
       - SUSPICIOUS/FAKE_CERTAIN → compare content with existing:
         - upgrade (>20% better) → import with warning
         - downgrade/sideways → delete + blocklist + return

    F3.1 hardening: source_type kwarg lets adapters tag sidecars with their
    actual provenance. Default 'tidal' preserves legacy behavior for the
    sync addurl-fallback path that still calls _run_download_job → here
    without an explicit adapter context.
    """
    import requests

    import_started = time.monotonic()

    # Bind source_type into closures so we don't have to repeat the kwarg
    # at every sidecar-write call site below (14 of them). Both helpers
    # forward source_type so the adapter's provenance reaches the sidecar
    # writer (which mirrors into state_db).
    def _write_sidecar_maybe(result, target, *, archive_dir=None):
        _maybe_write_v2_sidecar(
            jid,
            result,
            target,
            archive_dir=archive_dir,
            source_type=source_type,
        )

    def _write_sidecar_force(result, target):
        _write_verification_sidecar(jid, result, target, source_type=source_type)

    api = os.environ.get("LIDARR_API_URL", "http://host.docker.internal:8686/api/v1")
    key = _get_lidarr_key()
    if not key:
        log.warning("[%s] No Lidarr API key — skipping auto-import", jid)
        return
    if source_type == "soulseek" and target_album_id in (None, ""):
        target_album_id = _infer_lidarr_target_album_id(jid, api, key)
        if target_album_id is not None:
            log.info(
                "[%s] inferred Lidarr target albumId=%s from grab context",
                jid,
                target_album_id,
            )
            with _jobs_lock:
                _jobs.setdefault(jid, {"id": jid})["target_album_id"] = target_album_id
                _save_jobs()

    # 1. Manual-import-lookup (gir oss artist+album-mapping for filene)
    _set_worker_progress(
        worker_job_id,
        jid,
        "lidarr_precheck",
        70,
        "Checking Lidarr manual import candidates",
    )
    lidarr_path = f"/downloads/TidalHiRes/complete/{jid}"

    def _lookup():
        try:
            r = requests.get(
                f"{api}/manualimport",
                params={"folder": lidarr_path},
                headers={"X-Api-Key": key},
                timeout=60,
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            log.exception("[%s] manualimport lookup failed", jid)
            return []

    items = _lookup()

    # 1.5. Lidarr 80%-match-bug workaround: auto-pick albumRelease that matches track-count
    # Lidarr often has many MBz releases (standard, deluxe, expanded). If the downloaded album
    # has a track-count matching a DIFFERENT release than the active one, switch the active release first.
    file_count = _count_audio_files(output_dir)
    initial_album_ids = _album_ids_from_manualimport(items)
    if items and file_count >= 4:  # skip for small/single-track items
        rejected_due_to_match = any(
            any(
                "match is not close enough" in (r.get("reason", "") or "")
                or "missing tracks" in (r.get("reason", "") or "").lower()
                or "unmatched tracks" in (r.get("reason", "") or "").lower()
                for r in (i.get("rejections") or [])
            )
            for i in items
        )
        if rejected_due_to_match and initial_album_ids:
            log.warning(
                "[%s] Lidarr 80%%-match-bug detected — trying auto-release-switch", jid
            )
            # Build set of track names from downloaded files, normalised for remaster/edition noise.
            downloaded_names = _downloaded_track_names(output_dir)
            log.info(
                "[%s] downloaded track-names sample: %s",
                jid,
                list(downloaded_names)[:3],
            )

            for aid in initial_album_ids:
                try:
                    album = requests.get(
                        f"{api}/album/{aid}", headers={"X-Api-Key": key}, timeout=15
                    ).json()
                    current_rel = (album.get("currentRelease") or {}).get("id")
                    releases = album.get("releases", [])
                    # Score each release: track-count proximity + name overlap
                    scored = []
                    for rel in releases:
                        rel_tracks = rel.get("trackCount", 0)
                        # Fetch track titles for this release
                        rel_id = rel.get("id")
                        track_titles = set()
                        try:
                            tr = requests.get(
                                f"{api}/track?albumReleaseId={rel_id}",
                                headers={"X-Api-Key": key},
                                timeout=15,
                            ).json()
                            track_titles = _track_title_names(tr)
                        except Exception:
                            pass
                        # Score: track-count-match (40%) + normalised name overlap (60%).
                        total = _score_release_match(
                            file_count, downloaded_names, rel, track_titles
                        )
                        scored.append(
                            (total, rel_tracks, len(track_titles), rel_id, rel)
                        )
                    if not scored:
                        log.warning(
                            "[%s] No releases to switch to for album %s", jid, aid
                        )
                        continue
                    scored.sort(reverse=True)  # highest score first
                    best_score, best_tracks, _, best_id, best = scored[0]
                    log.info(
                        "[%s] Release-scoring for album %s: best=%s (score=%.1f, tracks=%s) of %d candidates",
                        jid,
                        aid,
                        best_id,
                        best_score,
                        best_tracks,
                        len(scored),
                    )

                    if best_score < 50:
                        log.warning(
                            "[%s] Best release score is too low (%.1f) — probably wrong Tidal album. Skipping switch.",
                            jid,
                            best_score,
                        )
                        continue

                    if best_id and best_id != current_rel:
                        album["albumReleaseId"] = best_id
                        for rel in releases:
                            rel["monitored"] = rel["id"] == best_id
                        put_r = requests.put(
                            f"{api}/album/{aid}",
                            json=album,
                            headers={"X-Api-Key": key},
                            timeout=30,
                        )
                        log.info(
                            "[%s] Switched album %s to release %s (tracks=%s, score=%.1f, was %s) HTTP=%s",
                            jid,
                            aid,
                            best_id,
                            best_tracks,
                            best_score,
                            current_rel,
                            put_r.status_code,
                        )
                except Exception:
                    log.exception("[%s] release-switch failed for album %s", jid, aid)
            # Re-fetch manualimport candidates with the new release active.
            time.sleep(2)
            items = _lookup()
            log.info(
                "[%s] After release-switch: %d candidates (%d accepted)",
                jid,
                len(items),
                sum(1 for i in items if not i.get("rejections")),
            )

    # 2. Fetch existing quality for matching album(s)
    album_ids = _album_ids_from_manualimport(items)
    existing_kbps = 0
    existing_label = "nothing"
    for aid in album_ids:
        try:
            tfs = requests.get(
                f"{api}/trackfile?albumId={aid}", headers={"X-Api-Key": key}, timeout=15
            ).json()
            for tf in tfs:
                qname = tf.get("quality", {}).get("quality", {}).get("name", "Unknown")
                kbps = QUALITY_KBPS.get(qname, 0)
                if kbps > existing_kbps:
                    existing_kbps = kbps
                    existing_label = qname
        except Exception:
            pass
    log.info(
        "[%s] Existing library quality: %s (~%d kbps)",
        jid,
        existing_label,
        existing_kbps,
    )

    # Fetch album statistics for the completeness rule (V2.1). Use max track-count
    # across matched albums if they differ (deluxe vs standard).
    existing_track_count = 0
    expected_track_count = 0
    for aid in album_ids:
        try:
            album_data = requests.get(
                f"{api}/album/{aid}", headers={"X-Api-Key": key}, timeout=15
            ).json()
            stats = album_data.get("statistics", {}) or {}
            tc = int(stats.get("trackCount") or 0)
            tfc = int(stats.get("trackFileCount") or 0)
            if tc > expected_track_count:
                expected_track_count = tc
            if tfc > existing_track_count:
                existing_track_count = tfc
        except Exception:
            pass
    new_track_count = _count_audio_files(output_dir)
    log.info(
        "[%s] Tracks: existing=%d / expected=%d / new=%d",
        jid,
        existing_track_count,
        expected_track_count,
        new_track_count,
    )
    _record_job_timing(jid, "lidarr_precheck_sec", time.monotonic() - import_started)

    # 3. PRE-IMPORT VALIDATION via flac-detective (FAIL-CLOSED)
    # If the validator does not respond or returns ERROR, we must NOT import.
    # The pipeline exists to protect against fake FLAC — fail-open undermines its purpose.
    detective_url = os.environ.get(
        "FLAC_API_URL", "http://host.docker.internal:8889/analyze"
    )
    verdict = None
    new_effective_kbps = (
        3000  # default: assume real FLAC 24bit (overridden on SUSPICIOUS)
    )
    detective_error = None
    d_result = None
    detective_started = time.monotonic()
    _set_worker_progress(
        worker_job_id, jid, "flac_detective", 78, "Running FLAC Detective validation"
    )
    try:
        r = requests.post(detective_url, json={"path": f"/output/{jid}"}, timeout=900)
        if r.status_code == 200:
            d_result = r.json()
            verdict = d_result.get("overall_verdict", "UNKNOWN")
            n_files = d_result.get("file_count", 0)
            log.info("[%s] flac-detective: %s (%d files)", jid, verdict, n_files)
            if verdict in ("SUSPICIOUS", "FAKE_CERTAIN", "FAKE"):
                # Detective's estimated_mp3_bitrate approximates the actual content quality.
                worst_est = 0
                for f in d_result.get("files", []):
                    est = f.get("estimated_mp3_bitrate") or 0
                    if est > worst_est:
                        worst_est = est
                # Fallback if not estimated: 320 for SUSPICIOUS (could have been MP3 320 transcode), 192 for FAKE_CERTAIN
                new_effective_kbps = (
                    worst_est
                    if worst_est > 0
                    else (320 if verdict == "SUSPICIOUS" else 192)
                )
        else:
            detective_error = f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        detective_error = f"connection/timeout: {e}"
    _record_job_timing(jid, "flac_detective_sec", time.monotonic() - detective_started)
    _set_worker_progress(
        worker_job_id, jid, "policy", 86, "Applying V2 verification policy"
    )

    v2_result = _compute_verification(
        jid,
        output_dir,
        items,
        verdict=verdict,
        detective_error=detective_error,
        detective_result=d_result,
        existing_kbps=existing_kbps,
        existing_label=existing_label,
        new_effective_kbps=new_effective_kbps,
        album_ids=album_ids,
        title=_jobs.get(jid, {}).get("title", ""),
        existing_track_count=existing_track_count,
        new_track_count=new_track_count,
        expected_track_count=expected_track_count,
    )

    # Fail-closed: if the validator did not respond, or returned ERROR — abort import
    if detective_error or verdict in (None, "UNKNOWN", "ERROR"):
        err_msg = detective_error or f"verdict={verdict}"
        log.error(
            "[%s] 🚨 FAIL-CLOSED: flac-detective unavailable/failed — aborting import. %s",
            jid,
            err_msg,
        )
        _set_v2_import_outcome(v2_result, "SKIPPED")
        _log_decision(
            jid,
            v2_result=v2_result,
            decision="BLOCKED",
            reason=f"validator unavailable: {err_msg}",
            verdict=verdict or "UNKNOWN",
            new_kbps=0,
            existing_quality=existing_label,
            existing_kbps=existing_kbps,
            album_ids=album_ids,
            title=_jobs.get(jid, {}).get("title", ""),
        )
        _write_sidecar_maybe(v2_result, output_dir, archive_dir=BLOCKED_DECISIONS_DIR)
        try:
            import shutil

            out_resolved = Path(output_dir).resolve()
            base_resolved = OUTPUT_BASE.resolve()
            if out_resolved != base_resolved and out_resolved.is_relative_to(
                base_resolved
            ):
                shutil.rmtree(out_resolved, ignore_errors=True)
        except Exception:
            log.exception("[%s] Cleanup failed during fail-closed handling", jid)
        with _jobs_lock:
            _jobs[jid].update(
                status="failed",
                error=f"validator unavailable: {err_msg}",
                completed_at=time.time(),
                hidden_from_lidarr=True,
            )
            _save_jobs()
        # Blocklist so Lidarr does not retry the same grab immediately
        try:
            h = requests.get(
                f"{api}/history?pageSize=50&sortKey=date&sortDirection=descending",
                headers={"X-Api-Key": key},
                timeout=15,
            ).json()
            for r2 in h.get("records", []):
                if r2.get("downloadId") == jid and r2.get("eventType") == "grabbed":
                    requests.post(
                        f"{api}/history/failed/{r2['id']}",
                        headers={"X-Api-Key": key},
                        json={},
                        timeout=15,
                    )
                    break
        except Exception:
            log.exception("[%s] Blocklist API failed during fail-closed handling", jid)
        _cleanup_lidarr_queue(jid, api, key)
        return

    if (
        _v2_verification_enabled()
        and v2_result.verification_decision == "REVIEW_REQUIRED"
    ):
        reason = f"V2 review required: {v2_result.verdict}"
        log.warning(
            "[%s] V2 REVIEW_REQUIRED — retaining files for manual review (%s)",
            jid,
            v2_result.verdict,
        )
        _log_decision(jid, v2_result=v2_result, reason=reason)
        _create_review_required(
            jid, v2_result, output_dir, api, key, source_type=source_type
        )
        return

    if _v2_verification_enabled() and v2_result.verification_decision == "BLOCK":
        reason = v2_result._legacy_reason()
        log.warning(
            "[%s] V2 BLOCK — aborting import before Lidarr ManualImport (%s)",
            jid,
            reason,
        )
        _set_v2_import_outcome(v2_result, "SKIPPED")
        _log_decision(
            jid,
            v2_result=v2_result,
            decision="BLOCKED",
            reason=reason,
            verdict=verdict,
            new_kbps=v2_result.new_kbps,
            existing_quality=existing_label,
            existing_kbps=existing_kbps,
            album_ids=album_ids,
            title=_jobs.get(jid, {}).get("title", ""),
        )
        _write_sidecar_maybe(v2_result, output_dir, archive_dir=BLOCKED_DECISIONS_DIR)
        try:
            import shutil

            out_resolved = Path(output_dir).resolve()
            base_resolved = OUTPUT_BASE.resolve()
            if out_resolved == base_resolved or not out_resolved.is_relative_to(
                base_resolved
            ):
                log.error(
                    "[%s] rmtree containment ERROR: %s is not under %s. Skipping deletion.",
                    jid,
                    out_resolved,
                    base_resolved,
                )
            else:
                shutil.rmtree(out_resolved, ignore_errors=True)
        except Exception:
            log.exception("[%s] Cleanup failed during V2 BLOCK handling", jid)
        with _jobs_lock:
            _jobs[jid].update(
                status="failed",
                error=f"v2 policy block: {reason}",
                completed_at=time.time(),
                hidden_from_lidarr=True,
            )
            _save_jobs()
        try:
            h = requests.get(
                f"{api}/history?pageSize=50&sortKey=date&sortDirection=descending",
                headers={"X-Api-Key": key},
                timeout=15,
            ).json()
            for r2 in h.get("records", []):
                if r2.get("downloadId") == jid and r2.get("eventType") == "grabbed":
                    requests.post(
                        f"{api}/history/failed/{r2['id']}",
                        headers={"X-Api-Key": key},
                        json={},
                        timeout=15,
                    )
                    log.info("[%s] Blocklist triggered (history %s)", jid, r2["id"])
                    break
        except Exception:
            log.exception("[%s] Blocklist API failed during V2 BLOCK handling", jid)
        _cleanup_lidarr_queue(jid, api, key)
        return

    # 4. Beslutning
    if verdict in ("SUSPICIOUS", "FAKE_CERTAIN", "FAKE"):
        # Require at least 20% upgrade (or nothing existing) to import a FAKE file
        is_upgrade = (existing_kbps == 0) or (new_effective_kbps > existing_kbps * 1.2)
        v2_forces_block = (
            _v2_verification_enabled() and v2_result.verification_decision == "BLOCK"
        )
        if not is_upgrade or v2_forces_block:
            block_reason = "no upgrade" if not is_upgrade else "v2 policy block"
            log.warning(
                "[%s] 🚨 PRE-IMPORT BLOCK: %s — no upgrade (~%d kbps vs existing %s ~%d kbps)",
                jid,
                verdict,
                new_effective_kbps,
                existing_label,
                existing_kbps,
            )
            _set_v2_import_outcome(v2_result, "SKIPPED")
            _log_decision(
                jid,
                v2_result=v2_result,
                decision="BLOCKED",
                reason=block_reason,
                verdict=verdict,
                new_kbps=new_effective_kbps,
                existing_quality=existing_label,
                existing_kbps=existing_kbps,
                album_ids=album_ids,
                title=_jobs.get(jid, {}).get("title", ""),
            )
            _write_sidecar_maybe(
                v2_result, output_dir, archive_dir=BLOCKED_DECISIONS_DIR
            )
            try:
                import shutil

                # Containment guard: never delete outside OUTPUT_BASE.
                out_resolved = Path(output_dir).resolve()
                base_resolved = OUTPUT_BASE.resolve()
                if out_resolved == base_resolved or not out_resolved.is_relative_to(
                    base_resolved
                ):
                    log.error(
                        "[%s] rmtree containment ERROR: %s is not under %s. Skipping deletion.",
                        jid,
                        out_resolved,
                        base_resolved,
                    )
                else:
                    shutil.rmtree(out_resolved, ignore_errors=True)
            except Exception:
                log.exception("Failed to delete work-dir")
            with _jobs_lock:
                _jobs[jid].update(
                    status="failed",
                    error=f"flac-detective: {verdict} ({block_reason} vs {existing_label})",
                    completed_at=time.time(),
                    hidden_from_lidarr=True,
                )
                _save_jobs()
            # Blocklist so Lidarr does not retry the same grab
            try:
                h = requests.get(
                    f"{api}/history?pageSize=50&sortKey=date&sortDirection=descending",
                    headers={"X-Api-Key": key},
                    timeout=15,
                ).json()
                for r2 in h.get("records", []):
                    if r2.get("downloadId") == jid and r2.get("eventType") == "grabbed":
                        requests.post(
                            f"{api}/history/failed/{r2['id']}",
                            headers={"X-Api-Key": key},
                            json={},
                            timeout=15,
                        )
                        log.info("[%s] Blocklist triggered (history %s)", jid, r2["id"])
                        break
            except Exception:
                log.exception("[%s] Blocklist API failed", jid)
            _cleanup_lidarr_queue(jid, api, key)
            return
        else:
            reason = (
                "nothing pre-existing"
                if existing_kbps == 0
                else f"upgrade from {existing_label}"
            )
            log.warning(
                "[%s] ⚠️ %s but allowing import — %s (~%d kbps > %d kbps)",
                jid,
                verdict,
                reason,
                new_effective_kbps,
                existing_kbps,
            )
            if not _v2_verification_enabled():
                _log_decision(
                    jid,
                    v2_result=v2_result,
                    decision="IMPORTED_DESPITE_FAKE",
                    reason=reason,
                    verdict=verdict,
                    new_kbps=new_effective_kbps,
                    existing_quality=existing_label,
                    existing_kbps=existing_kbps,
                    album_ids=album_ids,
                    title=_jobs.get(jid, {}).get("title", ""),
                )
            with _jobs_lock:
                _jobs[jid]["warning"] = f"{verdict} accepted: {reason}"
                _save_jobs()
    elif verdict == "WARNING":
        log.warning(
            "[%s] flac-detective WARNING — importerer likevel (review anbefalt)", jid
        )
        if not _v2_verification_enabled():
            _log_decision(
                jid,
                v2_result=v2_result,
                decision="IMPORTED_WITH_WARNING",
                reason="WARNING - manual review recommended",
                verdict=verdict,
                new_kbps=new_effective_kbps,
                existing_quality=existing_label,
                existing_kbps=existing_kbps,
                album_ids=album_ids,
                title=_jobs.get(jid, {}).get("title", ""),
            )
    else:
        if not _v2_verification_enabled():
            _log_decision(
                jid,
                v2_result=v2_result,
                decision="IMPORTED_AUTHENTIC",
                reason="passed validation",
                verdict=verdict,
                new_kbps=new_effective_kbps,
                existing_quality=existing_label,
                existing_kbps=existing_kbps,
                album_ids=album_ids,
                title=_jobs.get(jid, {}).get("title", ""),
            )

    # Lidarr's path to our output in /downloads/TidalHiRes/complete/<jid>/
    # (same bind mount; Lidarr sees it as /downloads via remote path mapping)
    lidarr_path = f"/downloads/TidalHiRes/complete/{jid}"

    # Fetch manual-import candidates.
    manualimport_started = time.monotonic()
    _set_worker_progress(
        worker_job_id, jid, "manualimport_lookup", 88, "Preparing Lidarr ManualImport"
    )
    r = requests.get(
        f"{api}/manualimport",
        params={"folder": lidarr_path},
        headers={"X-Api-Key": key},
        timeout=60,
    )
    if r.status_code != 200:
        _record_job_timing(
            jid, "lidarr_manualimport_sec", time.monotonic() - manualimport_started
        )
        log.warning(
            "[%s] manualimport lookup failed: HTTP %s — %s",
            jid,
            r.status_code,
            r.text[:200],
        )
        reason = f"manualimport lookup failed: HTTP {r.status_code}"
        _set_v2_import_outcome(v2_result, "FAILED")
        _log_decision(
            jid,
            v2_result=v2_result,
            decision="IMPORT_FAILED",
            reason=reason,
            verdict=verdict,
            new_kbps=new_effective_kbps,
            existing_quality=existing_label,
            existing_kbps=existing_kbps,
            album_ids=album_ids,
            title=_jobs.get(jid, {}).get("title", ""),
        )
        _write_sidecar_maybe(v2_result, output_dir)
        _mark_import_failed(jid, reason)
        _cleanup_lidarr_queue(jid, api, key)
        return
    items = r.json()
    if not items:
        log.warning(
            "[%s] manualimport returned 0 candidates for path %s — trying rescue-flow",
            jid,
            lidarr_path,
        )
        # We have files but Lidarr cannot match them to an album. Trigger rescue anyway
        # by building a minimal "files" list from album info in jobs.
        try:
            with _jobs_lock:
                job = _jobs.get(jid, {})
            # Tidal album-id is in the job; needs mapping to Lidarr album-id
            # Quick lookup: use artist+title from the job title to find the Lidarr album
            title = job.get("title", "")
            # Format: "<Artist> - <Album> (<Year>) [TIDAL] [FLAC 24bit]"
            m = re.match(r"^(.+?) - (.+?) \((\d{4})\)", title)
            if m:
                artist_name, album_title = m.group(1), m.group(2)
                # Search Lidarr albums by name
                search_r = requests.get(
                    f"{api}/search?term={quote(artist_name)}",
                    headers={"X-Api-Key": key},
                    timeout=20,
                )
                if search_r.status_code == 200:
                    for hit in search_r.json():
                        if (
                            hit.get("artist")
                            and artist_name.lower()
                            in hit["artist"].get("artistName", "").lower()
                        ):
                            artist_id = hit["artist"]["id"]
                            # Fetch the artist's album list.
                            albs = requests.get(
                                f"{api}/album?artistId={artist_id}",
                                headers={"X-Api-Key": key},
                                timeout=15,
                            ).json()
                            for ab in albs:
                                if album_title.lower() in ab.get("title", "").lower():
                                    # Build a synthetic file list for rescue.
                                    src_dir = output_dir
                                    files = []
                                    for fp in src_dir.rglob("*"):
                                        if fp.is_file() and fp.suffix.lower() in (
                                            ".flac",
                                            ".m4a",
                                        ):
                                            files.append(
                                                {
                                                    "path": str(fp).replace(
                                                        "/output/",
                                                        "/downloads/TidalHiRes/complete/",
                                                    ),
                                                    "artistId": artist_id,
                                                    "albumId": ab["id"],
                                                }
                                            )
                                    if files:
                                        log.info(
                                            "[%s] Bygde %d fake-files fra job-title — trigge rescue",
                                            jid,
                                            len(files),
                                        )
                                        rescued = _rescue_place_and_rescan(
                                            jid, files, api, key
                                        )
                                        _record_job_timing(
                                            jid,
                                            "lidarr_manualimport_sec",
                                            time.monotonic() - manualimport_started,
                                        )
                                        if not rescued:
                                            reason = "manualimport returned no candidates and rescue failed"
                                            _set_v2_import_outcome(v2_result, "FAILED")
                                            _log_decision(
                                                jid,
                                                v2_result=v2_result,
                                                decision="IMPORT_FAILED",
                                                reason=reason,
                                                verdict=verdict,
                                                new_kbps=new_effective_kbps,
                                                existing_quality=existing_label,
                                                existing_kbps=existing_kbps,
                                                album_ids=album_ids,
                                                title=_jobs.get(jid, {}).get(
                                                    "title", ""
                                                ),
                                            )
                                            _write_sidecar_maybe(v2_result, output_dir)
                                            _mark_import_failed(jid, reason)
                                            _cleanup_lidarr_queue(jid, api, key)
                                        return
                                    break
                            break
        except Exception:
            log.exception("[%s] Could not build rescue fallback", jid)
        reason = "manualimport returned no candidates"
        _record_job_timing(
            jid, "lidarr_manualimport_sec", time.monotonic() - manualimport_started
        )
        _set_v2_import_outcome(v2_result, "FAILED")
        _log_decision(
            jid,
            v2_result=v2_result,
            decision="IMPORT_FAILED",
            reason=reason,
            verdict=verdict,
            new_kbps=new_effective_kbps,
            existing_quality=existing_label,
            existing_kbps=existing_kbps,
            album_ids=album_ids,
            title=_jobs.get(jid, {}).get("title", ""),
        )
        _write_sidecar_maybe(v2_result, output_dir)
        _mark_import_failed(jid, reason)
        _cleanup_lidarr_queue(jid, api, key)
        return

    guard_reason = _manualimport_target_guard_failure(
        jid,
        items,
        source_type=source_type,
        target_album_id=target_album_id,
    )
    if guard_reason:
        log.warning("[%s] %s — aborting before Lidarr ManualImport", jid, guard_reason)
        _record_job_timing(
            jid, "lidarr_manualimport_sec", time.monotonic() - manualimport_started
        )
        _set_v2_import_outcome(v2_result, "FAILED")
        _log_decision(
            jid,
            v2_result=v2_result,
            decision="IMPORT_FAILED",
            reason=guard_reason,
            verdict=verdict,
            new_kbps=new_effective_kbps,
            existing_quality=existing_label,
            existing_kbps=existing_kbps,
            album_ids=album_ids,
            title=_jobs.get(jid, {}).get("title", ""),
        )
        _write_sidecar_maybe(v2_result, output_dir)
        _mark_import_failed(jid, guard_reason)
        _cleanup_lidarr_queue(jid, api, key)
        return

    log.info("[%s] %d manualimport candidates found", jid, len(items))

    # Build the ManualImport command payload. After V2 has approved the audio, we can force-import
    # release-family rejects (remaster/deluxe/anniversary) when Lidarr actually has matched tracks.
    files = _manual_import_files_from_items(
        jid, items, allow_release_family_rejections=True
    )
    for i in items:
        if i.get("rejections") and not _is_release_family_rejection(i):
            log.info(
                "[%s]   skip rejected: %s — %s",
                jid,
                i.get("path", ""),
                i["rejections"][:1],
            )
        elif i.get("rejections"):
            log.info(
                "[%s]   force release-family import: %s — %s",
                jid,
                i.get("path", ""),
                i["rejections"][:1],
            )
        elif not i.get("artist") or not i.get("album"):
            log.info("[%s]   skip (no artist/album): %s", jid, i.get("path", ""))
        elif not i.get("tracks"):
            log.info("[%s]   skip (no tracks matched): %s", jid, i.get("path", ""))
    if not files:
        log.warning(
            "[%s] No importable files (all rejected or missing artist/album)", jid
        )
        reason = "no importable files after verification"
        _record_job_timing(
            jid, "lidarr_manualimport_sec", time.monotonic() - manualimport_started
        )
        _set_v2_import_outcome(v2_result, "FAILED")
        _log_decision(
            jid,
            v2_result=v2_result,
            decision="IMPORT_FAILED",
            reason=reason,
            verdict=verdict,
            new_kbps=new_effective_kbps,
            existing_quality=existing_label,
            existing_kbps=existing_kbps,
            album_ids=album_ids,
            title=_jobs.get(jid, {}).get("title", ""),
        )
        _write_sidecar_maybe(v2_result, output_dir)
        _mark_import_failed(jid, reason)
        _cleanup_lidarr_queue(jid, api, key)
        return

    # Save track-count BEFORE ManualImport so we can detect whether import succeeded
    pre_counts = {}
    for aid in {f["albumId"] for f in files}:
        try:
            tfs_before = requests.get(
                f"{api}/trackfile?albumId={aid}", headers={"X-Api-Key": key}, timeout=10
            ).json()
            pre_counts[aid] = len(tfs_before)
        except Exception:
            pre_counts[aid] = 0

    cmd = requests.post(
        f"{api}/command",
        json={"name": "ManualImport", "files": files, "importMode": "auto"},
        headers={"X-Api-Key": key},
        timeout=30,
    )
    if cmd.status_code in (200, 201):
        log.info("[%s] ✓ Lidarr ManualImport triggered — %d files", jid, len(files))
    else:
        log.warning(
            "[%s] ManualImport command failed: HTTP %s — %s",
            jid,
            cmd.status_code,
            cmd.text[:200],
        )

    # Wait for Lidarr import, but return early when we see enough import evidence.
    _set_worker_progress(
        worker_job_id,
        jid,
        "manualimport",
        93,
        "Waiting for Lidarr import",
        file_count=len(files),
    )
    imported_count, import_threshold = _wait_for_manualimport_progress(
        jid, api, key, files, pre_counts, context="ManualImport"
    )

    if imported_count >= import_threshold:
        _record_job_timing(
            jid, "lidarr_manualimport_sec", time.monotonic() - manualimport_started
        )
        log.info(
            "[%s] ✓ ManualImport succeeded (%d/%d files imported)",
            jid,
            imported_count,
            len(files),
        )
        _set_v2_import_outcome(v2_result, "MANUAL_IMPORTED")
        if _v2_verification_enabled():
            _write_sidecar_force(v2_result, output_dir)
            _log_decision(jid, v2_result=v2_result)
        _complete_lidarr_import_without_queue_delete(jid, output_dir)
        return

    if _lidarr_command_still_pending(cmd, api, key):
        _record_job_timing(
            jid, "lidarr_manualimport_sec", time.monotonic() - manualimport_started
        )
        log.warning(
            "[%s] ManualImport is still queued/started in Lidarr — leaving import_outcome=PENDING",
            jid,
        )
        _set_v2_import_outcome(v2_result, "PENDING")
        _write_sidecar_maybe(v2_result, output_dir)
        with _jobs_lock:
            _jobs[jid]["warning"] = "Lidarr ManualImport pending"
            _save_jobs()
        return

    # RESCUE: ManualImport failed (probably 'Has missing tracks'). Use place-files-and-rescan.
    log.warning(
        "[%s] ManualImport did NOT succeed (imported: %d/%d) — trying place-files-and-rescan rescue",
        jid,
        imported_count,
        len(files),
    )
    try:
        if _rescue_place_and_rescan(jid, files, api, key):
            _record_job_timing(
                jid, "lidarr_manualimport_sec", time.monotonic() - manualimport_started
            )
            _set_v2_import_outcome(v2_result, "RESCUED")
            if _v2_verification_enabled():
                _write_sidecar_force(v2_result, output_dir)
                _log_decision(jid, v2_result=v2_result)
            _complete_lidarr_import_without_queue_delete(jid, output_dir)
            return
        else:
            reason = "manualimport and rescue failed"
            _record_job_timing(
                jid, "lidarr_manualimport_sec", time.monotonic() - manualimport_started
            )
            _set_v2_import_outcome(v2_result, "FAILED")
            _log_decision(
                jid,
                v2_result=v2_result,
                decision="IMPORT_FAILED",
                reason=reason,
                verdict=verdict,
                new_kbps=new_effective_kbps,
                existing_quality=existing_label,
                existing_kbps=existing_kbps,
                album_ids=album_ids,
                title=_jobs.get(jid, {}).get("title", ""),
            )
            _write_sidecar_maybe(v2_result, output_dir)
            _mark_import_failed(jid, reason)
            _cleanup_lidarr_queue(jid, api, key)
    except Exception:
        log.exception("[%s] Rescue failed", jid)
        reason = "manualimport and rescue failed"
        _record_job_timing(
            jid, "lidarr_manualimport_sec", time.monotonic() - manualimport_started
        )
        _set_v2_import_outcome(v2_result, "FAILED")
        _log_decision(
            jid,
            v2_result=v2_result,
            decision="IMPORT_FAILED",
            reason=reason,
            verdict=verdict,
            new_kbps=new_effective_kbps,
            existing_quality=existing_label,
            existing_kbps=existing_kbps,
            album_ids=album_ids,
            title=_jobs.get(jid, {}).get("title", ""),
        )
        _write_sidecar_maybe(v2_result, output_dir)
        _mark_import_failed(jid, reason)
        _cleanup_lidarr_queue(jid, api, key)


def _sanitize_path_segment(s: str) -> str:
    """Strip separators, NUL, parent refs and leading dots from a path segment.
    Protects against path traversal from Lidarr API responses to mkdir/copy."""
    if not s:
        return "Unknown"
    # Remove path separators and NUL bytes.
    s = s.replace("/", "_").replace("\\", "_").replace("\x00", "")
    # Remove leading dots and surrounding whitespace.
    s = s.strip().lstrip(".")
    # Explicitly reject plain parent/current directory refs.
    if s in ("", "..", "."):
        return "Unknown"
    return s


def _rescue_place_and_rescan(jid, files, api, key):
    """Last resort: place files directly in Lidarr's library tree and rescan.
    Lidarr scans them as library files, excluding missing_tracks from the
    score and bypassing the 80% import-match issue.
    """
    import requests
    import shutil
    from pathlib import Path as _P

    if not _rescue_rescan_enabled():
        log.warning(
            "[%s] RESCUE: disabled by TIDALHIRES_RESCUE_RESCAN_ENABLED=false", jid
        )
        return False
    if not files:
        return False
    # Fetch artist + album info from the first file
    first = files[0]
    aid = first["albumId"]
    artist_id = first["artistId"]
    album = requests.get(
        f"{api}/album/{aid}", headers={"X-Api-Key": key}, timeout=15
    ).json()
    artist = requests.get(
        f"{api}/artist/{artist_id}", headers={"X-Api-Key": key}, timeout=15
    ).json()
    # Sanitize for path traversal protection; a malicious or buggy API response
    # may contain "/" or "..".
    artist_name = _sanitize_path_segment(artist.get("artistName", "Unknown"))
    album_title = _sanitize_path_segment(album.get("title", "Unknown"))
    album_year = (album.get("releaseDate") or "")[:4] or "0000"
    if not album_year.isdigit():
        album_year = "0000"

    # Build Lidarr folder structure matching standardTrackFormat:
    # "{Album Title} ({Release Year})".
    # Library mount is /music inside the Mintarr container; operator chooses the host mount.
    library_root = _P("/music/Album").resolve()
    album_folder_p = (
        library_root / artist_name / f"{album_title} ({album_year})"
    ).resolve()
    # Containment guard: must be inside /music/Album/
    if not album_folder_p.is_relative_to(library_root):
        log.error(
            "[%s] RESCUE: containment check FAILED — %s is not under %s. Aborting.",
            jid,
            album_folder_p,
            library_root,
        )
        return False
    album_folder = str(album_folder_p)

    album_folder_p.mkdir(parents=True, exist_ok=True)
    log.info("[%s] RESCUE: target path = %s", jid, album_folder)

    # Copy all files; all paths are container paths.
    moved = 0
    for f in files:
        # f["path"] is the Lidarr container's path to our output: /downloads/TidalHiRes/complete/<jid>/Albums/...
        # Inside the Mintarr container this is the same mount: /output/<jid>/Albums/...
        src_container_lidarr = f["path"]
        src_container_tidalhires = src_container_lidarr.replace(
            "/downloads/TidalHiRes/complete/", "/output/"
        )
        src_p = _P(src_container_tidalhires)
        if not src_p.exists():
            log.warning("[%s] RESCUE: src not found: %s", jid, src_container_tidalhires)
            continue
        # Keep the original filename; Lidarr renames during scan if renameTracks=True.
        dst = f"{album_folder}/{src_p.name}"
        try:
            shutil.copy2(str(src_p), dst)
            moved += 1
        except Exception as e:
            log.warning("[%s] RESCUE: copy failed for %s: %s", jid, src_p, e)
    log.info(
        "[%s] RESCUE: copied %d/%d files to %s", jid, moved, len(files), album_folder
    )

    if moved == 0:
        log.error("[%s] RESCUE: no files copied — aborting", jid)
        return False

    # Trigger RescanFolder/RefreshArtist so Lidarr discovers the files
    cmd = requests.post(
        f"{api}/command",
        json={"name": "RefreshArtist", "artistIds": [artist_id]},
        headers={"X-Api-Key": key},
        timeout=15,
    )
    log.info(
        "[%s] RESCUE: RefreshArtist triggered for artist %s HTTP=%s",
        jid,
        artist_id,
        cmd.status_code,
    )

    # Wait and verify.
    time.sleep(30)
    tfs_after = requests.get(
        f"{api}/trackfile?albumId={aid}", headers={"X-Api-Key": key}, timeout=10
    ).json()
    rescue_count = len(tfs_after)
    log.info(
        "[%s] RESCUE: after RefreshArtist Lidarr has %d trackfiles for album %s",
        jid,
        rescue_count,
        aid,
    )

    # Require at least ceil(moved/2) registered trackfiles. moved=1 must require 1, not 0 (off-by-one).
    if rescue_count >= max(1, (moved + 1) // 2):
        log.info(
            "[%s] ✓ RESCUE succeeded — %d trackfiles registered", jid, rescue_count
        )
        _log_decision(
            jid,
            decision="RESCUED_BY_RESCAN",
            reason="place-files-and-rescan after manualimport-fail",
            verdict="N/A",
            new_kbps=0,
            existing_quality="N/A",
            existing_kbps=0,
            album_ids=[aid],
            title=_jobs.get(jid, {}).get("title", ""),
        )
        _hide_from_lidarr(jid)
        return True
    else:
        log.warning(
            "[%s] RESCUE: rescan did not register files in the Lidarr DB — fundamental mismatch",
            jid,
        )
        _log_decision(
            jid,
            decision="RESCUE_FAILED",
            reason="Lidarr rescan did not register files",
            verdict="N/A",
            new_kbps=0,
            existing_quality="N/A",
            existing_kbps=0,
            album_ids=[aid],
            title=_jobs.get(jid, {}).get("title", ""),
        )
        return False


def _cleanup_lidarr_queue(jid, api, key):
    """Remove the Lidarr queue entry for our jid after terminal handling.
    removeFromClient=false avoids Lidarr calling SAB DELETE (we have already
    cleaned up our side)."""
    import requests

    started = time.monotonic()
    try:
        q = requests.get(
            f"{api}/queue?pageSize=200", headers={"X-Api-Key": key}, timeout=10
        ).json()
        removed = 0
        for r in q.get("records", []):
            if r.get("downloadId") == jid:
                qid = r.get("id")
                resp = requests.delete(
                    f"{api}/queue/{qid}",
                    params={"removeFromClient": "false", "blocklist": "false"},
                    headers={"X-Api-Key": key},
                    timeout=10,
                )
                if resp.status_code in (200, 204):
                    removed += 1
        if removed:
            log.info(
                "[%s] Lidarr queue cleaned: %d entries removed after terminal handling",
                jid,
                removed,
            )
        _hide_from_lidarr(jid)
    except Exception:
        log.exception(
            "[%s] Failed to clean up Lidarr queue after terminal handling", jid
        )
    finally:
        _record_job_timing(jid, "queue_cleanup_sec", time.monotonic() - started)


def _complete_lidarr_import_without_queue_delete(
    jid: str, output_dir: Path | None = None
) -> None:
    """Mark a successful import without forcing Lidarr's queue item to ignored.

    After ManualImport succeeds, Lidarr records trackFileImported events and usually
    settles the tracked download itself. Deleting the queue row at that point creates
    a misleading downloadIgnored history row in Lidarr, even though the import worked.
    Failed/blocked terminal states should still use _cleanup_lidarr_queue.
    """
    _mark_import_completed(jid, output_dir)


def _manual_import_files_from_items(
    jid: str,
    items: list[dict],
    *,
    allow_release_family_rejections: bool = False,
) -> list[dict]:
    files = []
    for item in items:
        if item.get("rejections") and not (
            allow_release_family_rejections and _is_release_family_rejection(item)
        ):
            continue
        if not item.get("artist") or not item.get("album"):
            continue
        tracks = item.get("tracks") or []
        if not tracks:
            continue
        album = item.get("album") or {}
        album_release_id = (
            item.get("albumReleaseId")
            or (album.get("currentRelease") or {}).get("id")
            or ((album.get("releases") or [{}])[0].get("id"))
        )
        if not album_release_id:
            continue
        files.append(
            {
                "path": item["path"],
                "artistId": item["artist"]["id"],
                "albumId": album["id"],
                "albumReleaseId": album_release_id,
                "trackIds": [t["id"] for t in tracks],
                "quality": item["quality"],
                "releaseGroup": item.get("releaseGroup") or "",
                "downloadId": jid,
                "additionalFile": False,
                "replaceExistingFiles": True,
                "disableReleaseSwitching": False,
            }
        )
    return files


def _run_manual_import_only(
    jid: str,
    output_dir: Path,
    *,
    worker_job_id: int | None = None,
) -> ImportOutcome:
    """Promote path: run only Lidarr ManualImport + rescue, without re-running verification."""
    import requests

    _set_worker_progress(
        worker_job_id,
        jid,
        "manualimport_lookup",
        20,
        "Looking up Lidarr ManualImport candidates",
    )
    _raise_if_job_cancelled(worker_job_id, jid, output_dir, cleanup=False)
    api = os.environ.get("LIDARR_API_URL", "http://host.docker.internal:8686/api/v1")
    key = _get_lidarr_key()
    if not key:
        return "FAILED"

    lidarr_path = f"/downloads/TidalHiRes/complete/{jid}"
    r = requests.get(
        f"{api}/manualimport",
        params={"folder": lidarr_path},
        headers={"X-Api-Key": key},
        timeout=60,
    )
    if r.status_code != 200:
        return "FAILED"
    _raise_if_job_cancelled(worker_job_id, jid, output_dir, cleanup=False)

    items = r.json()
    files = _manual_import_files_from_items(
        jid, items, allow_release_family_rejections=True
    )
    if not files:
        return "FAILED"
    _raise_if_job_cancelled(worker_job_id, jid, output_dir, cleanup=False)
    _set_worker_progress(
        worker_job_id,
        jid,
        "manualimport",
        45,
        "Triggering Lidarr ManualImport",
        file_count=len(files),
    )

    pre_counts = {}
    for aid in {f["albumId"] for f in files}:
        try:
            tfs_before = requests.get(
                f"{api}/trackfile?albumId={aid}", headers={"X-Api-Key": key}, timeout=10
            ).json()
            pre_counts[aid] = len(tfs_before)
        except Exception:
            pre_counts[aid] = 0

    cmd = requests.post(
        f"{api}/command",
        json={"name": "ManualImport", "files": files, "importMode": "auto"},
        headers={"X-Api-Key": key},
        timeout=30,
    )
    if cmd.status_code not in (200, 201):
        log.warning(
            "[%s] Promote ManualImport command failed: HTTP %s — %s",
            jid,
            cmd.status_code,
            cmd.text[:200],
        )
    _raise_if_job_cancelled(worker_job_id, jid, output_dir, cleanup=False)

    _set_worker_progress(
        worker_job_id,
        jid,
        "manualimport_wait",
        65,
        "Waiting for Lidarr import",
        file_count=len(files),
    )
    imported_count, import_threshold = _wait_for_manualimport_progress(
        jid, api, key, files, pre_counts, context="Promote ManualImport"
    )

    if imported_count >= import_threshold:
        _set_worker_progress(
            worker_job_id, jid, "cleanup", 90, "Finalizing Lidarr import"
        )
        _complete_lidarr_import_without_queue_delete(jid, output_dir)
        _set_worker_progress(worker_job_id, jid, "done", 100, "ManualImport complete")
        return "MANUAL_IMPORTED"

    if _lidarr_command_still_pending(cmd, api, key):
        log.warning(
            "[%s] Promote ManualImport is still queued/started in Lidarr — keeping PENDING",
            jid,
        )
        with _jobs_lock:
            _jobs.setdefault(jid, {"id": jid}).update(
                warning="Lidarr ManualImport pending", percent=100
            )
            _save_jobs()
        return "PENDING"
    _raise_if_job_cancelled(worker_job_id, jid, output_dir, cleanup=False)

    _set_worker_progress(worker_job_id, jid, "rescue", 80, "Trying rescue import")
    if _rescue_place_and_rescan(jid, files, api, key):
        _complete_lidarr_import_without_queue_delete(jid, output_dir)
        _set_worker_progress(worker_job_id, jid, "done", 100, "Rescue import complete")
        return "RESCUED"

    _mark_import_failed(jid, "manual promote import failed")
    _set_worker_progress(worker_job_id, jid, "failed", 100, "ManualImport failed")
    _cleanup_lidarr_queue(jid, api, key)
    return "FAILED"


def _is_promotable(record: dict) -> bool:
    decision = record.get("v2_verification_decision")
    outcome = record.get("v2_import_outcome")
    lifecycle = record.get("lifecycle") or {}
    overrides = record.get("v2_overrides") or []
    return (
        decision == "REVIEW_REQUIRED" and lifecycle.get("state") == "pending_review"
    ) or (
        decision == "ACCEPT_PROVISIONAL"
        and outcome in ("FAILED", "PENDING")
        and "manual_promote" in overrides
    )


def _is_retryable_verified_import(record: dict) -> bool:
    decision = record.get("v2_verification_decision")
    outcome = record.get("v2_import_outcome")
    lifecycle = record.get("lifecycle") or {}
    return (
        decision in ("ACCEPT", "ACCEPT_PROVISIONAL")
        and outcome in ("FAILED", "PENDING")
        and lifecycle.get("state") not in ("discarded", "expired")
    )


def _promote_verified_import(
    jid: str,
    record: dict,
    path: Path,
    *,
    worker_job_id: int | None = None,
) -> tuple[dict, int]:
    outcome = record.get("v2_import_outcome")
    if outcome in ("MANUAL_IMPORTED", "RESCUED"):
        return {
            "jid": jid,
            "import_outcome": outcome,
            "message": "already imported",
        }, 200

    lifecycle = record.setdefault("lifecycle", {})
    overrides = record.setdefault("v2_overrides", [])
    if not _is_promotable(record):
        return {"error": "verification is not promotable", "jid": jid}, 409

    _raise_if_job_cancelled(
        worker_job_id,
        jid,
        Path(_jobs.get(jid, {}).get("output_dir") or OUTPUT_BASE / jid),
        cleanup=False,
    )
    now = time.time()
    record["v2_verification_decision"] = "ACCEPT_PROVISIONAL"
    if "manual_promote" not in overrides:
        overrides.append("manual_promote")
    lifecycle["state"] = "promoted"
    lifecycle["actor"] = "user_promote"
    lifecycle["promoted_at"] = lifecycle.get("promoted_at") or now
    _atomic_write_json(path, record)

    output_dir = Path(_jobs.get(jid, {}).get("output_dir") or OUTPUT_BASE / jid)
    import_outcome = _run_manual_import_only(
        jid, output_dir, worker_job_id=worker_job_id
    )
    record["v2_import_outcome"] = import_outcome
    _atomic_write_json(path, record)
    return {
        "jid": jid,
        "import_outcome": import_outcome,
        "message": "promote complete",
    }, 200


def _retry_verified_import(
    jid: str,
    record: dict,
    path: Path,
    *,
    worker_job_id: int | None = None,
) -> tuple[dict, int]:
    outcome = record.get("v2_import_outcome")
    if outcome in ("MANUAL_IMPORTED", "RESCUED"):
        return {
            "jid": jid,
            "import_outcome": outcome,
            "message": "already imported",
        }, 200

    lifecycle = record.get("lifecycle") or {}
    if not _is_retryable_verified_import(record):
        return {"error": "verification is not retryable", "jid": jid}, 409

    output_dir = Path(_jobs.get(jid, {}).get("output_dir") or OUTPUT_BASE / jid)
    if not output_dir.exists():
        return {"error": "output directory missing", "jid": jid}, 409
    _raise_if_job_cancelled(worker_job_id, jid, output_dir, cleanup=False)

    with _jobs_lock:
        job = _jobs.setdefault(jid, {"id": jid})
        job.update(
            status="processing",
            warning="ManualImport retry",
            hidden_from_lidarr=False,
            percent=100,
        )
        job["output_dir"] = str(output_dir)
        _save_jobs()

    import_outcome = _run_manual_import_only(
        jid, output_dir, worker_job_id=worker_job_id
    )
    record["v2_import_outcome"] = import_outcome
    record.setdefault("lifecycle", lifecycle)
    _atomic_write_json(path, record)
    return {
        "jid": jid,
        "import_outcome": import_outcome,
        "message": "retry complete",
    }, 200


def _verification_action_result_state(jid: str) -> str:
    """Return dashboard-derived business state for a completed action job."""
    try:
        from dashboard import derive_status

        _, record = _read_verification_sidecar(jid)
        if record:
            outcome = record.get("v2_import_outcome")
            if outcome == "FAILED":
                return "failed"
            if outcome == "PENDING":
                return "pending"
            return derive_status(record)
    except Exception:
        log.exception("[%s] Failed to derive action-job result_state", jid)
    return "completed"


def _execute_promote_import_job(job: dict) -> tuple[str | None, dict | None]:
    """F2.3 worker executor for user-approved REVIEW_REQUIRED imports."""
    jid = str(job.get("jid") or "")
    if not jid:
        raise ValueError("promote_import job missing jid")
    worker_job_id = int(job["id"])
    _set_worker_progress(worker_job_id, jid, "starting", 5, "Starting promote import")
    lock = _get_promote_lock(jid)
    with lock:
        path, record = _read_verification_sidecar(jid)
        if path is None or record is None:
            raise RuntimeError(f"verification not found for jid={jid}")
        _set_worker_progress(
            worker_job_id, jid, "validating", 10, "Validating promote state"
        )
        payload, status = _promote_verified_import(
            jid, record, path, worker_job_id=worker_job_id
        )
        if status >= 400:
            raise RuntimeError(
                payload.get("error") or f"promote failed with HTTP {status}"
            )
        return _verification_action_result_state(jid), payload


def _execute_retry_import_job(job: dict) -> tuple[str | None, dict | None]:
    """F2.3 worker executor for retrying a previously verified import."""
    jid = str(job.get("jid") or "")
    if not jid:
        raise ValueError("retry_import job missing jid")
    worker_job_id = int(job["id"])
    _set_worker_progress(worker_job_id, jid, "starting", 5, "Starting retry import")
    lock = _get_promote_lock(jid)
    with lock:
        path, record = _read_verification_sidecar(jid)
        if path is None or record is None:
            raise RuntimeError(f"verification not found for jid={jid}")
        _set_worker_progress(
            worker_job_id, jid, "validating", 10, "Validating retry state"
        )
        payload, status = _retry_verified_import(
            jid, record, path, worker_job_id=worker_job_id
        )
        if status >= 400:
            raise RuntimeError(
                payload.get("error") or f"retry failed with HTTP {status}"
            )
        return _verification_action_result_state(jid), payload


def _enqueue_verification_action(jid: str, action: str) -> tuple[dict, int] | None:
    """Persist a promote/retry action as a worker job. Returns None if DB is unavailable."""
    job_type = {
        "promote": "promote_import",
        "retry_import": "retry_import",
    }.get(action)
    if not job_type:
        return {"error": f"unknown action: {action}", "jid": jid}, 400

    try:
        import state_db

        dedupe_key = f"verification:{action}:{jid}"
        existing = state_db.find_active_job_by_dedupe(dedupe_key)
        if existing:
            return {
                "jid": jid,
                "job_id": existing["id"],
                "job_state": existing.get("state"),
                "message": f"{action} already queued",
            }, 202

        job_id = state_db.enqueue_job(
            jid=jid,
            type=job_type,
            payload={"action": action, "jid": jid},
            dedupe_key=dedupe_key,
            source_type="verification",
            source_id=jid,
            priority=2,
            max_attempts=3,
        )
        if job_id is None:
            return None
        return {
            "jid": jid,
            "job_id": job_id,
            "job_type": job_type,
            "message": f"{action} queued",
        }, 202
    except Exception:
        log.exception("[%s] Failed to enqueue verification action=%s", jid, action)
        return None


def _verification_records() -> list[dict]:
    paths = list(OUTPUT_BASE.glob("*/verification.json"))
    for directory in (BLOCKED_DECISIONS_DIR, DISCARDED_DIR, EXPIRED_REVIEW_DIR):
        if directory.exists():
            paths.extend(directory.glob("*.json"))

    seen: set[str] = set()
    rows = []
    for path in paths:
        try:
            rec = json.loads(path.read_text())
        except Exception:
            continue
        jid = rec.get("jid")
        if jid in seen:
            continue
        seen.add(jid)
        rows.append(rec)
    return rows


def _decision_with_current_verification_state(rec: dict) -> dict:
    """Overlay current sidecar lifecycle on immutable decision-log rows."""
    jid = rec.get("jid")
    if not jid:
        return rec
    _, sidecar = _read_verification_sidecar(str(jid))
    if sidecar is None:
        return rec

    merged = dict(rec)
    for key in (
        "lifecycle",
        "v2_verification_decision",
        "v2_import_outcome",
        "v2_overrides",
        "v2_score",
        "v2_components",
        "verdict",
    ):
        if key in sidecar:
            merged[key] = sidecar[key]
    lifecycle = merged.get("lifecycle") or {}
    if lifecycle.get("state"):
        merged["verification_state"] = lifecycle["state"]
    return merged


_DASHBOARD_CSS = """
body{font-family:system-ui,sans-serif;margin:24px;background:#fafafa;color:#222}
h1{margin:0 0 8px}
h2{margin-top:32px;border-bottom:1px solid #ddd;padding-bottom:4px}
.summary{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.card{background:#fff;border:1px solid #ddd;border-radius:6px;padding:12px 16px;min-width:120px}
.card .label{color:#666;font-size:0.85em}
.card .val{font-size:1.6em;font-weight:600}
.card.warn{border-color:#e88;background:#fff5f5}
.card.warn .val{color:#c33}
.card.ok{border-color:#8c8;background:#f5fff5}
.card.ok .val{color:#383}
table{border-collapse:collapse;width:100%;background:#fff}
td,th{border:1px solid #ddd;padding:6px 8px;text-align:left;font-size:0.92em}
th{background:#f0f0f0}
tr.needs-action{background:#fff5f5}
tr.active{background:#fffce6}
form{display:inline-flex;gap:4px;margin-right:6px}
input{width:120px;padding:2px 4px}
button{padding:2px 8px;cursor:pointer}
.state-pending_review{color:#c80;font-weight:600}
.state-promoted{color:#383}
.state-discarded{color:#888}
.state-expired{color:#888;font-style:italic}
.decision-ACCEPT{color:#383}
.decision-ACCEPT_PROVISIONAL{color:#c80}
.decision-REVIEW_REQUIRED{color:#c33;font-weight:600}
.decision-BLOCK{color:#888}
.muted{color:#888;font-style:italic}
.health-ok{color:#383}.health-warn{color:#c80}.health-err{color:#c33}
"""


def _render_dashboard_actions(jid: str) -> str:
    return (
        f"<form method='post' action='/verification/{jid}/promote'>"
        "<input name='apikey' type='password' placeholder='API key'>"
        "<button type='submit'>Promote</button></form>"
        f"<form method='post' action='/verification/{jid}/retry-import'>"
        "<input name='apikey' type='password' placeholder='API key'>"
        "<button type='submit'>Retry</button></form>"
        f"<form method='post' action='/verification/{jid}/discard'>"
        "<input name='apikey' type='password' placeholder='API key'>"
        "<button type='submit'>Discard</button></form>"
    )


def _render_dashboard_row(rec: dict, *, highlight_class: str = "") -> str:
    jid = xml_escape(str(rec.get("jid", "")))
    title = xml_escape(str(rec.get("title", "")))
    decision = str(rec.get("v2_verification_decision", ""))
    outcome = xml_escape(str(rec.get("v2_import_outcome", "")))
    score = xml_escape(str(rec.get("v2_score", "")))
    verdict = xml_escape(str(rec.get("verdict", "")))
    state = str(
        rec.get("verification_state") or rec.get("lifecycle", {}).get("state", "")
    )
    tr_class = f" class='{highlight_class}'" if highlight_class else ""
    return (
        f"<tr{tr_class}><td><code>{jid[:12]}</code></td>"
        f"<td>{title}</td>"
        f"<td class='decision-{xml_escape(decision)}'>{xml_escape(decision)}</td>"
        f"<td>{outcome}</td>"
        f"<td class='state-{xml_escape(state)}'>{xml_escape(state)}</td>"
        f"<td>{score}</td><td>{verdict}</td>"
        f"<td>{_render_dashboard_actions(jid)}</td></tr>"
    )


def _verification_html(rows: list[dict]) -> str:
    # --- Summary counts ---
    from collections import Counter

    outcome_counts = Counter(
        r.get("v2_import_outcome") for r in rows if r.get("v2_import_outcome")
    )
    total = len(rows)

    # --- Needs action: REVIEW_REQUIRED + pending_review lifecycle ---
    needs_action = [
        r
        for r in rows
        if r.get("v2_verification_decision") == "REVIEW_REQUIRED"
        and (r.get("verification_state") or r.get("lifecycle", {}).get("state"))
        == "pending_review"
    ]

    # --- Active jobs (downloading/processing) ---
    with _jobs_lock:
        active_jobs = [
            {"jid": jid, **j}
            for jid, j in _jobs.items()
            if j.get("status") in ("queued", "downloading", "processing")
        ]

    # --- Stack health: SAB queue + Lidarr queue (best-effort) ---
    sab_queue_count = sum(
        1
        for j in _jobs.values()
        if j.get("status") in ("queued", "downloading", "processing")
        and not j.get("hidden_from_lidarr")
    )
    lidarr_queue_count = "?"
    try:
        api = os.environ.get(
            "LIDARR_API_URL", "http://host.docker.internal:8686/api/v1"
        )
        lkey = _get_lidarr_key()
        if lkey:
            r = requests.get(
                f"{api}/queue?pageSize=1", headers={"X-Api-Key": lkey}, timeout=3
            )
            if r.ok:
                lidarr_queue_count = str(r.json().get("totalRecords", 0))
    except Exception:
        pass

    # --- Build summary cards ---
    fail_count = outcome_counts.get("FAILED", 0)
    pending_count = outcome_counts.get("PENDING", 0)
    cards = []
    cards.append(
        f"<div class='card'><div class='label'>Total decisions</div><div class='val'>{total}</div></div>"
    )
    cards.append(
        f"<div class='card ok'><div class='label'>Imported</div><div class='val'>{outcome_counts.get('MANUAL_IMPORTED',0) + outcome_counts.get('RESCUED',0)}</div></div>"
    )
    rr_class = "card warn" if len(needs_action) > 0 else "card"
    cards.append(
        f"<div class='{rr_class}'><div class='label'>Needs review</div><div class='val'>{len(needs_action)}</div></div>"
    )
    cards.append(
        f"<div class='card{ ' warn' if pending_count else ''}'><div class='label'>Pending</div><div class='val'>{pending_count}</div></div>"
    )
    cards.append(
        f"<div class='card{ ' warn' if fail_count else ''}'><div class='label'>Failed</div><div class='val'>{fail_count}</div></div>"
    )
    cards.append(
        f"<div class='card'><div class='label'>Active jobs</div><div class='val'>{len(active_jobs)}</div></div>"
    )
    cards.append(
        f"<div class='card'><div class='label'>SAB queue</div><div class='val'>{sab_queue_count}</div></div>"
    )
    cards.append(
        f"<div class='card'><div class='label'>Lidarr queue</div><div class='val'>{lidarr_queue_count}</div></div>"
    )
    summary_html = "".join(cards)

    # --- Needs Action section ---
    if needs_action:
        na_rows = "\n".join(
            _render_dashboard_row(r, highlight_class="needs-action")
            for r in needs_action
        )
        needs_section = (
            "<h2>⚠ Needs action — REVIEW_REQUIRED awaiting user decision</h2>"
            "<table><thead><tr><th>JID</th><th>Title</th><th>Decision</th><th>Outcome</th>"
            "<th>State</th><th>Score</th><th>Verdict</th><th>Actions</th></tr></thead>"
            f"<tbody>{na_rows}</tbody></table>"
        )
    else:
        needs_section = "<h2>Needs action</h2><p class='muted'>Nothing pending — all REVIEW_REQUIRED items have been promoted, discarded, or expired.</p>"

    # --- Active jobs section ---
    if active_jobs:
        aj_body = "\n".join(
            f"<tr class='active'><td><code>{xml_escape(j['jid'][:12])}</code></td>"
            f"<td>{xml_escape(j.get('title','?'))}</td>"
            f"<td>{xml_escape(j.get('status',''))}</td>"
            f"<td>{j.get('percent', 0)}%</td>"
            f"<td>{int(j.get('size', 0) / (1024*1024))} MB</td></tr>"
            for j in active_jobs
        )
        active_section = (
            "<h2>Active jobs</h2>"
            "<table><thead><tr><th>JID</th><th>Title</th><th>Status</th>"
            "<th>%</th><th>Size</th></tr></thead>"
            f"<tbody>{aj_body}</tbody></table>"
        )
    else:
        active_section = "<h2>Active jobs</h2><p class='muted'>Idle.</p>"

    # --- All decisions table ---
    all_rows = "\n".join(_render_dashboard_row(r) for r in rows)
    rows_html = all_rows or "<tr><td colspan='8'>No verification records yet.</td></tr>"

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>TidalHires V2 Dashboard</title>"
        f"<style>{_DASHBOARD_CSS}</style>"
        "</head><body>"
        "<h1>TidalHires V2 Dashboard</h1>"
        f"<div class='summary'>{summary_html}</div>"
        f"{needs_section}"
        f"{active_section}"
        "<h2>All verification records</h2>"
        "<table><thead><tr><th>JID</th><th>Title</th><th>Decision</th><th>Outcome</th>"
        "<th>State</th><th>Score</th><th>Verdict</th><th>Actions</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
        "</body></html>"
    )


def _expire_review_required_jobs() -> None:
    retention_days = float(os.environ.get("REVIEW_RETENTION_DAYS", "30"))
    max_age = retention_days * 24 * 60 * 60
    now = time.time()
    api = os.environ.get("LIDARR_API_URL", "http://host.docker.internal:8686/api/v1")
    key = _get_lidarr_key()

    for rec in _verification_records():
        if rec.get("v2_verification_decision") != "REVIEW_REQUIRED":
            continue
        lifecycle = rec.get("lifecycle") or {}
        if lifecycle.get("state") != "pending_review":
            continue
        created_at = float(lifecycle.get("created_at") or rec.get("ts") or now)
        if now - created_at <= max_age:
            continue

        jid = rec.get("jid")
        if not jid:
            continue
        path, current = _read_verification_sidecar(jid)
        if path is None or current is None:
            continue

        lifecycle = current.setdefault("lifecycle", {})
        if lifecycle.get("blocklist_status") != "done":
            lifecycle["blocklist_status"] = (
                "done" if key and _blocklist_grab(jid, api, key) else "failed"
            )
        lifecycle["state"] = "expired"
        lifecycle["expired_at"] = now
        lifecycle["actor"] = "auto_expire"

        target = _archive_sidecar_path(jid, EXPIRED_REVIEW_DIR)
        _atomic_write_json(target, current)
        if path.exists() and path != target:
            path.unlink()

        output_dir = Path(_jobs.get(jid, {}).get("output_dir") or OUTPUT_BASE / jid)
        if output_dir.exists():
            _safe_rmtree_under(OUTPUT_BASE, output_dir)
        with _jobs_lock:
            if jid in _jobs:
                _jobs[jid].update(
                    status="failed", error="review_required expired", completed_at=now
                )
                _save_jobs()


_load_jobs()
_expire_review_required_jobs()


@app.route("/health")
def health():
    try:
        _get_session()
        return jsonify(
            {
                "status": "ok",
                "active_jobs": sum(
                    1
                    for j in _jobs.values()
                    if j.get("status") in ("queued", "downloading", "processing")
                ),
            }
        )
    except Exception:
        log.exception("Healthcheck degraded")
        return jsonify({"status": "degraded"}), 503


@app.route("/jobs")
@require_apikey
def jobs():
    status = request.args.get("status")
    if status:
        return jsonify(
            {jid: job for jid, job in _jobs.items() if job.get("status") == status}
        )
    return jsonify(_jobs)


@app.route("/verification/<jid>", methods=["GET"])
@require_apikey
def verification_get(jid: str):
    path, record = _read_verification_sidecar(jid)
    if record is None:
        return jsonify({"error": "verification not found", "jid": jid}), 404
    record = _reconcile_pending_import(jid, record, path)
    return jsonify(record)


@app.route("/verification", methods=["GET"])
@require_apikey
def verification_list():
    decision_filter = request.args.get("decision")
    outcome_filter = request.args.get("import_outcome")
    since = float(request.args.get("since", "0"))
    limit = int(request.args.get("limit", "50"))

    rows = []
    for rec in _verification_records():
        jid = rec.get("jid")
        if jid:
            rec = _reconcile_pending_import(jid, rec)
        if decision_filter and rec.get("v2_verification_decision") != decision_filter:
            continue
        if outcome_filter and rec.get("v2_import_outcome") != outcome_filter:
            continue
        if since and float(rec.get("ts") or 0) < since:
            continue
        rows.append(rec)

    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    rows = rows[:limit]
    wants_html = request.args.get("format") == "html" or (
        request.accept_mimetypes.accept_html
        and request.accept_mimetypes["text/html"]
        >= request.accept_mimetypes["application/json"]
    )
    if wants_html:
        return Response(_verification_html(rows), mimetype="text/html")
    return jsonify({"count": len(rows), "verification": rows})


@app.route("/verification/<jid>/promote", methods=["POST"])
@require_apikey
def verification_promote(jid: str):
    path, record = _read_verification_sidecar(jid)
    if path is None or record is None:
        return jsonify({"error": "verification not found", "jid": jid}), 404

    outcome = record.get("v2_import_outcome")
    if outcome in ("MANUAL_IMPORTED", "RESCUED"):
        return jsonify(
            {"jid": jid, "import_outcome": outcome, "message": "already imported"}
        )

    if not _is_promotable(record):
        return jsonify({"error": "verification is not promotable", "jid": jid}), 409

    queued = _enqueue_verification_action(jid, "promote")
    if queued is not None:
        payload, status = queued
        return jsonify(payload), status

    # Availability fallback: if SQLite is unavailable, preserve old synchronous behavior.
    lock = _get_promote_lock(jid)
    if not lock.acquire(blocking=False):
        return jsonify({"error": "promote already running", "jid": jid}), 409
    try:
        payload, status = _promote_verified_import(jid, record, path)
        return jsonify(payload), status
    finally:
        lock.release()


@app.route("/verification/<jid>/retry-import", methods=["POST"])
@require_apikey
def verification_retry_import(jid: str):
    path, record = _read_verification_sidecar(jid)
    if path is None or record is None:
        return jsonify({"error": "verification not found", "jid": jid}), 404

    outcome = record.get("v2_import_outcome")
    if outcome in ("MANUAL_IMPORTED", "RESCUED"):
        return jsonify(
            {"jid": jid, "import_outcome": outcome, "message": "already imported"}
        )

    if not _is_retryable_verified_import(record):
        return jsonify({"error": "verification is not retryable", "jid": jid}), 409

    output_dir = Path(_jobs.get(jid, {}).get("output_dir") or OUTPUT_BASE / jid)
    if not output_dir.exists():
        return jsonify({"error": "output directory missing", "jid": jid}), 409

    queued = _enqueue_verification_action(jid, "retry_import")
    if queued is not None:
        payload, status = queued
        return jsonify(payload), status

    # Availability fallback: if SQLite is unavailable, preserve old synchronous behavior.
    lock = _get_promote_lock(jid)
    if not lock.acquire(blocking=False):
        return jsonify({"error": "retry already running", "jid": jid}), 409
    try:
        payload, status = _retry_verified_import(jid, record, path)
        return jsonify(payload), status
    finally:
        lock.release()


@app.route("/verification/<jid>/discard", methods=["POST"])
@require_apikey
def verification_discard(jid: str):
    path, record = _read_verification_sidecar(jid)
    if path is None or record is None:
        return jsonify({"error": "verification not found", "jid": jid}), 404

    decision = record.get("v2_verification_decision")
    outcome = record.get("v2_import_outcome")
    overrides = record.get("v2_overrides") or []
    discardable = decision == "REVIEW_REQUIRED" or (
        decision == "ACCEPT_PROVISIONAL"
        and outcome in ("FAILED", "PENDING")
        and "manual_promote" in overrides
    )
    if not discardable:
        return jsonify({"error": "verification is not discardable", "jid": jid}), 409

    output_dir = Path(_jobs.get(jid, {}).get("output_dir") or OUTPUT_BASE / jid)
    if output_dir.exists() and not _safe_rmtree_under(OUTPUT_BASE, output_dir):
        return jsonify({"error": "unsafe output path", "jid": jid}), 500

    api = os.environ.get("LIDARR_API_URL", "http://host.docker.internal:8686/api/v1")
    key = _get_lidarr_key()
    blocklisted = _blocklist_grab(jid, api, key) if key else False

    lifecycle = record.setdefault("lifecycle", {})
    lifecycle["state"] = "discarded"
    lifecycle["discarded_at"] = time.time()
    lifecycle["actor"] = "user_discard"
    lifecycle["blocklist_status"] = "done" if blocklisted else "failed"

    target = _archive_sidecar_path(jid, DISCARDED_DIR)
    _atomic_write_json(target, record)
    if path.exists() and path != target:
        path.unlink()
    _mark_import_failed(jid, "discarded by user")
    return jsonify({"jid": jid, "message": "discarded"})


@app.route("/decisions")
@require_apikey
def decisions():
    """Audit trail of all pre-import decisions.
    Query: ?limit=50&filter=blocked|imported|all
    """
    limit = int(request.args.get("limit", 50))
    filt = request.args.get("filter", "all").lower()
    rows = []
    if DECISIONS_LOG.exists():
        try:
            with open(DECISIONS_LOG) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    dec = rec.get("decision", "").upper()
                    if filt == "blocked" and "BLOCK" not in dec:
                        continue
                    if filt == "imported" and "IMPORT" not in dec:
                        continue
                    rows.append(rec)
        except Exception:
            log.exception("Could not read decisions log")
    rows = [
        _decision_with_current_verification_state(rec)
        for rec in list(reversed(rows))[:limit]
    ]  # newest first
    return jsonify({"count": len(rows), "decisions": rows})


@app.route("/download/<int:album_id>.nzb", methods=["GET"])
@require_apikey
def download_nzb_legacy(album_id: int):
    """Legacy TIDAL-only NZB endpoint. F3.3 supersedes via /download/<source>/<b64>.nzb;
    kept for Lidarr-history compat (old grabs reference this URL)."""
    return _emit_nzb_response("tidal", str(album_id))


@app.route("/download/<source>/<encoded_source_id>.nzb", methods=["GET"])
@require_apikey
def download_nzb(source: str, encoded_source_id: str):
    """F3.3 multi-source NZB endpoint. source_id is base64url-encoded so it
    can contain spaces, slashes, parentheses without breaking Flask routing.
    """
    import adapters as _adapters

    try:
        source = _validate_source_name(source)
    except ValueError as exc:
        return Response(str(exc), status=400)
    try:
        source_id = _b64url_decode(encoded_source_id)
    except (ValueError, Exception) as exc:
        return Response(f"bad encoded source_id: {exc}", status=400)

    adapter = _adapters.get_adapter(source)
    if adapter is None:
        return Response("unknown source", status=404)
    if not adapter.is_enabled():
        return Response("source disabled", status=503)
    if not _adapter_import_mode_enabled(adapter.name):
        return Response("source not in import mode", status=503)
    try:
        source_id = _canonicalize_source_id(adapter, source_id)
    except (RuntimeError, ValueError) as exc:
        return Response(f"invalid source_id: {exc}", status=400)

    return _emit_nzb_response(source, source_id)


def _emit_nzb_response(source: str, source_id: str) -> Response:
    """Shared NZB writer for legacy + F3.3 routes. Safe filename derived from hash
    so source_id with slashes/spaces never reaches Content-Disposition."""
    from adapters.local_folder import hash_rel as _hr

    nzb = _nzb_pointer(source, source_id)
    safe_name = f"tidalhires-{source}-{_hr(source_id)}.nzb"
    return Response(
        nzb,
        mimetype="application/x-nzb",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@app.route("/<path:p>", methods=["GET", "POST"])
def catch_all(p):
    auth_resp = require_apikey_check()
    if auth_resp:
        return auth_resp
    log.warning(
        "CATCH-ALL %s /%s args=%s",
        request.method,
        p,
        _redact_request_values(request.values),
    )
    return jsonify({"error": "unknown route", "path": p}), 404


@app.route("/")
def index():
    return jsonify(
        {
            "service": "tidalhires",
            "endpoints": [
                "/api?t=caps",
                "/api?t=music&q=...",
                "/sabnzbd/api?mode=...",
                "/health",
                "/jobs",
                "/dashboard",
            ],
        }
    )


app.register_blueprint(dashboard_bp)

# F1: Initialize SQLite state index (idempotent — schema applied if missing)
try:
    import state_db

    state_db.init()
except Exception:
    log.exception("state_db.init() failed — continuing without state index")

# F3.1/F3.4: Register source adapters. Common pipeline dispatches by adapter.name.
try:
    import adapters
    from adapters.tidal import TidalAdapter
    from adapters.local_folder import LocalFolderAdapter
    from adapters.soulseek import SoulseekCompletedAdapter

    if adapters.get_adapter("tidal") is None:
        adapters.register(TidalAdapter())
    if adapters.get_adapter("local") is None:
        adapters.register(LocalFolderAdapter())
    if adapters.get_adapter("soulseek") is None:
        adapters.register(SoulseekCompletedAdapter())
except Exception:
    log.exception("adapter registration failed — source grabs may not work")

# F4.1: Register operator-facing connectors after adapters are available.
try:
    import connectors

    connectors.register_builtin_connectors()
except Exception:
    log.exception(
        "connector registration failed — dashboard connector status may be unavailable"
    )

# F2.1: Start background worker thread (single worker per design).
# Skip in tests where pytest manages lifecycle, or via env-toggle for ops.
if not (
    os.environ.get("MINTARR_DISABLE_WORKER")
    or os.environ.get("TIDALHIRES_DISABLE_WORKER")
):
    try:
        import worker

        # F2.2/F2.3/F3.4: register executors before starting worker
        worker.register_executor("tidal_grab", _execute_tidal_grab_job)
        worker.register_executor("local_grab", _execute_local_grab_job)
        worker.register_executor("soulseek_grab", _execute_soulseek_grab_job)
        worker.register_executor("promote_import", _execute_promote_import_job)
        worker.register_executor("retry_import", _execute_retry_import_job)
        worker.start_worker()
    except Exception:
        log.exception(
            "worker.start_worker() failed — continuing without background worker"
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
