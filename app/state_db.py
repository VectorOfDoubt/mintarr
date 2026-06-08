"""SQLite state index for V2 records, sensors, file evidence, and actions.

F1 of Codex's roadmap (handover 2026-05-25):
- Additive layer alongside sidecars + decisions.jsonl (audit remains source-of-truth)
- Querybar state for dashboard + future filtering/timeline
- Defensive writes: DB failure never breaks sidecar/audit flow

Schema designed for queryability + future source-adapter expansion (F3).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("tidalhires.state_db")

_DEFAULT_DB_PATH = Path(
    os.environ.get("MINTARR_STATE_DB")
    or os.environ.get("TIDALHIRES_STATE_DB")
    or "/config/tidalhires_state.db"
)
_db_path: Path = _DEFAULT_DB_PATH
_lock = threading.Lock()
_initialized = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    jid TEXT PRIMARY KEY,
    title TEXT,
    album_ids_json TEXT,
    created_at REAL,
    updated_at REAL,
    verification_decision TEXT,
    import_outcome TEXT,
    derived_status TEXT,
    score INTEGER,
    verdict TEXT,
    lifecycle_state TEXT,
    actor TEXT,
    discarded_at REAL,
    promoted_at REAL,
    expired_at REAL
);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(derived_status);
CREATE INDEX IF NOT EXISTS idx_records_created ON records(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_records_decision ON records(verification_decision);

CREATE TABLE IF NOT EXISTS sensor_runs (
    jid TEXT,
    sensor_name TEXT,
    sensor_class TEXT,
    status TEXT,
    severity TEXT,
    confidence REAL,
    duration_ms INTEGER,
    evidence_json TEXT,
    PRIMARY KEY (jid, sensor_name)
);
CREATE INDEX IF NOT EXISTS idx_sensor_runs_jid ON sensor_runs(jid);
CREATE INDEX IF NOT EXISTS idx_sensor_runs_status ON sensor_runs(status);

CREATE TABLE IF NOT EXISTS file_evidence (
    jid TEXT,
    filename TEXT,
    sample_rate INTEGER,
    bit_depth INTEGER,
    cutoff_hz REAL,
    nyquist_hz REAL,
    detective_verdict TEXT,
    is_fake_high_res INTEGER,
    estimated_mp3_bitrate INTEGER,
    evidence_json TEXT,
    PRIMARY KEY (jid, filename)
);
CREATE INDEX IF NOT EXISTS idx_file_evidence_jid ON file_evidence(jid);

CREATE TABLE IF NOT EXISTS library_evidence (
    trackfile_id INTEGER PRIMARY KEY,
    album_id INTEGER,
    path TEXT,
    size INTEGER,
    mtime REAL,
    status TEXT,
    reason TEXT,
    codec TEXT,
    sample_rate INTEGER,
    bit_depth INTEGER,
    channels INTEGER,
    lossless INTEGER,
    integrity_ok INTEGER,
    checksum_ok INTEGER,
    sensor_version TEXT,
    integrity_sensor_version TEXT,
    evidence_json TEXT,
    measured_at REAL
);
CREATE INDEX IF NOT EXISTS idx_library_evidence_album ON library_evidence(album_id);

-- F5.4 slice 5: background library quality indexing
CREATE TABLE IF NOT EXISTS library_scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    worker_job_id INTEGER,
    requested_by TEXT,
    total_items INTEGER DEFAULT 0,
    processed_items INTEGER DEFAULT 0,
    measured_items INTEGER DEFAULT 0,
    fresh_items INTEGER DEFAULT 0,
    unmeasured_items INTEGER DEFAULT 0,
    error_items INTEGER DEFAULT 0,
    cancel_requested INTEGER DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_library_scan_runs_state ON library_scan_runs(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_library_scan_runs_job ON library_scan_runs(worker_job_id);

CREATE TABLE IF NOT EXISTS library_scan_items (
    run_id INTEGER NOT NULL,
    trackfile_id INTEGER NOT NULL,
    album_id INTEGER,
    state TEXT NOT NULL,
    attempts INTEGER DEFAULT 0,
    updated_at REAL NOT NULL,
    last_error TEXT,
    PRIMARY KEY (run_id, trackfile_id)
);
CREATE INDEX IF NOT EXISTS idx_library_scan_items_state ON library_scan_items(run_id, state);
CREATE INDEX IF NOT EXISTS idx_library_scan_items_album ON library_scan_items(album_id);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jid TEXT,
    action TEXT,
    actor TEXT,
    created_at REAL,
    result TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_jid ON actions(jid);
CREATE INDEX IF NOT EXISTS idx_actions_created ON actions(created_at DESC);

-- F2 worker queue
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jid TEXT NOT NULL,
    type TEXT NOT NULL,
    state TEXT NOT NULL,
    result_state TEXT,
    priority INTEGER DEFAULT 5,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    next_attempt_at REAL,
    heartbeat_at REAL,
    lease_expires_at REAL,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    dedupe_key TEXT,
    source_type TEXT,
    source_id TEXT,
    payload_json TEXT,
    progress_json TEXT,
    result_json TEXT,
    error_text TEXT,
    worker_id TEXT,
    cancel_requested INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_jobs_jid ON jobs(jid);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key, state);
CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(state, lease_expires_at);

-- F4.3 connector runtime config
CREATE TABLE IF NOT EXISTS connector_config (
    connector_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    mode TEXT NOT NULL,
    updated_at REAL NOT NULL,
    actor TEXT
);
"""

# Default lease/heartbeat per F2 design
DEFAULT_LEASE_SEC = 300.0  # 5 min — recovery-trigger
HEARTBEAT_INTERVAL_SEC = 30.0  # worker pings DB every 30s during long ops

ACTIVE_JOB_STATES = ("queued", "running", "cancelling")
TERMINAL_JOB_STATES = ("completed", "failed", "cancelled")
ACTIVE_LIBRARY_SCAN_STATES = ("queued", "running", "cancelling")
TERMINAL_LIBRARY_SCAN_STATES = ("completed", "failed", "cancelled")
LIBRARY_SCAN_JOB_TYPE = "library_scan"
LIBRARY_SCAN_DEDUPE_KEY = "library_scan"
LIBRARY_SCAN_PRIORITY = 50
LIBRARY_SCAN_MAX_ATTEMPTS = 1


def _connect() -> sqlite3.Connection:
    """Open a connection. WAL for concurrent-reader-friendliness."""
    conn = sqlite3.connect(str(_db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init(db_path: Path | None = None) -> None:
    """Idempotent schema init. Call once at startup."""
    global _db_path, _initialized
    if db_path is not None:
        _db_path = db_path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with _connect() as conn:
            conn.executescript(SCHEMA)
            _ensure_records_source_type_column(conn)
            _ensure_library_spectral_columns(conn)
            _ensure_library_checksum_column(conn)
            _ensure_library_integrity_sensor_column(conn)
        _initialized = True
        log.info("state_db initialized at %s", _db_path)


def _ensure_records_source_type_column(conn: sqlite3.Connection) -> None:
    """F3.1 migration: add records.source_type if absent. Idempotent.

    CREATE TABLE IF NOT EXISTS does not add columns to an existing table,
    so we probe schema and ALTER on demand. Existing rows are backfilled
    to 'tidal' — the only source we had pre-F3.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()}
    if "source_type" in cols:
        return
    conn.execute("ALTER TABLE records ADD COLUMN source_type TEXT")
    conn.execute("UPDATE records SET source_type = 'tidal' WHERE source_type IS NULL")
    log.info("state_db: added records.source_type column (backfilled to 'tidal')")


def _ensure_library_spectral_columns(conn: sqlite3.Connection) -> None:
    """F5.4 slice 4a migration: add the spectral (FLAC Detective) columns.

    The spectral tier is a separate sensor layered onto each library_evidence
    row (§8b). Added on demand so existing rows keep their cheap-tier evidence and
    simply read unknown authenticity until re-measured. Idempotent.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(library_evidence)").fetchall()
    }
    additions = {
        "authentic": "INTEGER",  # tri-state: 1 genuine, 0 fake, NULL unknown
        "spectral_status": "TEXT",
        "spectral_reason": "TEXT",
        "spectral_verdict": "TEXT",
        "spectral_sensor_version": "TEXT",
        "spectral_evidence_json": "TEXT",
        "spectral_measured_at": "REAL",
    }
    added = False
    for name, decl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE library_evidence ADD COLUMN {name} {decl}")
            added = True
    if added:
        log.info("state_db: added library_evidence spectral columns (F5.4 slice 4a)")


def _ensure_library_checksum_column(conn: sqlite3.Connection) -> None:
    """F5.4 integrity-split migration: add library_evidence.checksum_ok. Idempotent.

    Splits FLAC ``flac -t`` integrity into decode validity (``integrity_ok``) and
    MD5 checksum verification (``checksum_ok``). Existing rows read NULL until the
    next scan re-measures them (forced by the bumped ``sensor_version``).
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(library_evidence)").fetchall()
    }
    if "checksum_ok" not in cols:
        conn.execute("ALTER TABLE library_evidence ADD COLUMN checksum_ok INTEGER")
        log.info("state_db: added library_evidence.checksum_ok column (F5.4)")


def _ensure_library_integrity_sensor_column(conn: sqlite3.Connection) -> None:
    """F5.4 scan-tier migration: add library_evidence.integrity_sensor_version.

    The integrity tier (flac -t) becomes a separate sensor from metadata
    (ffprobe), so its freshness is keyed independently. Existing rows read NULL ⇒
    integrity is treated as *unknown* until an integrity scan re-confirms it.
    Idempotent.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(library_evidence)").fetchall()
    }
    if "integrity_sensor_version" not in cols:
        conn.execute(
            "ALTER TABLE library_evidence ADD COLUMN integrity_sensor_version TEXT"
        )
        log.info(
            "state_db: added library_evidence.integrity_sensor_version column (F5.4)"
        )


def _ensure_initialized() -> bool:
    """Return True if init has been called; auto-init on first use as fallback."""
    if _initialized:
        return True
    try:
        init()
        return True
    except Exception:
        log.exception("state_db init failed")
        return False


# ---------- Write helpers (all defensive — never raise) ----------


def upsert_record(sidecar: dict, *, derived_status: str | None = None) -> None:
    """Upsert a record row from a verification sidecar.

    Pulls v2_* fields + lifecycle. Safe to call repeatedly. derived_status
    is optional — caller can compute via dashboard.derive_status if needed.
    """
    if not _ensure_initialized():
        return
    try:
        jid = sidecar.get("jid")
        if not jid:
            return
        lifecycle = sidecar.get("lifecycle") or {}
        now = time.time()
        # F3.1: absence of source_type in sidecar means legacy TIDAL.
        params = {
            "jid": jid,
            "title": sidecar.get("title", ""),
            "album_ids_json": json.dumps(sidecar.get("album_ids") or []),
            "created_at": float(
                lifecycle.get("created_at") or sidecar.get("ts") or now
            ),
            "updated_at": now,
            "verification_decision": sidecar.get("v2_verification_decision"),
            "import_outcome": sidecar.get("v2_import_outcome"),
            "derived_status": derived_status,
            "score": sidecar.get("v2_score"),
            "verdict": sidecar.get("verdict"),
            "lifecycle_state": lifecycle.get("state"),
            "actor": lifecycle.get("actor"),
            "discarded_at": lifecycle.get("discarded_at"),
            "promoted_at": lifecycle.get("promoted_at"),
            "expired_at": lifecycle.get("expired_at"),
            "source_type": sidecar.get("source_type") or "tidal",
        }
        with _lock, _connect() as conn:
            conn.execute(
                """
                INSERT INTO records (jid, title, album_ids_json, created_at, updated_at,
                  verification_decision, import_outcome, derived_status, score, verdict,
                  lifecycle_state, actor, discarded_at, promoted_at, expired_at, source_type)
                VALUES (:jid, :title, :album_ids_json, :created_at, :updated_at,
                  :verification_decision, :import_outcome, :derived_status, :score, :verdict,
                  :lifecycle_state, :actor, :discarded_at, :promoted_at, :expired_at, :source_type)
                ON CONFLICT(jid) DO UPDATE SET
                  title=excluded.title,
                  album_ids_json=excluded.album_ids_json,
                  updated_at=excluded.updated_at,
                  verification_decision=excluded.verification_decision,
                  import_outcome=excluded.import_outcome,
                  derived_status=excluded.derived_status,
                  score=excluded.score,
                  verdict=excluded.verdict,
                  lifecycle_state=excluded.lifecycle_state,
                  actor=excluded.actor,
                  discarded_at=COALESCE(excluded.discarded_at, records.discarded_at),
                  promoted_at=COALESCE(excluded.promoted_at, records.promoted_at),
                  expired_at=COALESCE(excluded.expired_at, records.expired_at),
                  source_type=COALESCE(excluded.source_type, records.source_type)
            """,
                params,
            )
    except Exception:
        log.exception("state_db.upsert_record failed for jid=%s", sidecar.get("jid"))


def upsert_sensor_runs(jid: str, sensors: list[dict] | None) -> None:
    """Replace sensor_runs rows for a jid with the given list."""
    if not _ensure_initialized() or not jid or not sensors:
        return
    try:
        rows = []
        for s in sensors:
            if not isinstance(s, dict):
                continue
            rows.append(
                {
                    "jid": jid,
                    "sensor_name": s.get("name", ""),
                    "sensor_class": s.get("class"),
                    "status": s.get("status"),
                    "severity": s.get("severity"),
                    "confidence": s.get("confidence"),
                    "duration_ms": s.get("duration_ms"),
                    "evidence_json": json.dumps(s.get("evidence") or {}),
                }
            )
        if not rows:
            return
        with _lock, _connect() as conn:
            conn.execute("DELETE FROM sensor_runs WHERE jid = ?", (jid,))
            conn.executemany(
                """
                INSERT INTO sensor_runs (jid, sensor_name, sensor_class, status, severity,
                  confidence, duration_ms, evidence_json)
                VALUES (:jid, :sensor_name, :sensor_class, :status, :severity,
                  :confidence, :duration_ms, :evidence_json)
            """,
                rows,
            )
    except Exception:
        log.exception("state_db.upsert_sensor_runs failed for jid=%s", jid)


def upsert_file_evidence(jid: str, files: list[dict] | None) -> None:
    """Replace file_evidence rows for a jid with the given list."""
    if not _ensure_initialized() or not jid or not files:
        return
    try:
        rows = []
        for f in files:
            if not isinstance(f, dict):
                continue
            rows.append(
                {
                    "jid": jid,
                    "filename": f.get("filename") or f.get("filepath") or "",
                    "sample_rate": f.get("sample_rate"),
                    "bit_depth": f.get("bit_depth"),
                    "cutoff_hz": f.get("cutoff_hz") or f.get("cutoff_freq"),
                    "nyquist_hz": f.get("nyquist_hz"),
                    "detective_verdict": f.get("detective_verdict") or f.get("verdict"),
                    "is_fake_high_res": int(bool(f.get("is_fake_high_res")))
                    if f.get("is_fake_high_res") is not None
                    else None,
                    "estimated_mp3_bitrate": f.get("estimated_mp3_bitrate"),
                    "evidence_json": json.dumps(f),
                }
            )
        if not rows:
            return
        with _lock, _connect() as conn:
            conn.execute("DELETE FROM file_evidence WHERE jid = ?", (jid,))
            conn.executemany(
                """
                INSERT INTO file_evidence (jid, filename, sample_rate, bit_depth, cutoff_hz,
                  nyquist_hz, detective_verdict, is_fake_high_res, estimated_mp3_bitrate, evidence_json)
                VALUES (:jid, :filename, :sample_rate, :bit_depth, :cutoff_hz,
                  :nyquist_hz, :detective_verdict, :is_fake_high_res, :estimated_mp3_bitrate, :evidence_json)
            """,
                rows,
            )
    except Exception:
        log.exception("state_db.upsert_file_evidence failed for jid=%s", jid)


def upsert_library_evidence(row: dict) -> None:
    """Insert/replace measured quality evidence for one Lidarr trackfile (F5.4)."""
    if not _ensure_initialized() or row.get("trackfile_id") is None:
        return
    try:
        payload = {
            "trackfile_id": int(row["trackfile_id"]),
            "album_id": row.get("album_id"),
            "path": row.get("path"),
            "size": row.get("size"),
            "mtime": row.get("mtime"),
            "status": row.get("status"),
            "reason": row.get("reason"),
            "codec": row.get("codec"),
            "sample_rate": row.get("sample_rate"),
            "bit_depth": row.get("bit_depth"),
            "channels": row.get("channels"),
            "lossless": None
            if row.get("lossless") is None
            else int(bool(row.get("lossless"))),
            "integrity_ok": None
            if row.get("integrity_ok") is None
            else int(bool(row.get("integrity_ok"))),
            "checksum_ok": None
            if row.get("checksum_ok") is None
            else int(bool(row.get("checksum_ok"))),
            "sensor_version": row.get("sensor_version"),
            "integrity_sensor_version": row.get("integrity_sensor_version"),
            "evidence_json": json.dumps(row.get("evidence") or {}),
            "measured_at": row.get("measured_at") or time.time(),
        }
        with _lock, _connect() as conn:
            conn.execute(
                """
                INSERT INTO library_evidence (trackfile_id, album_id, path, size, mtime,
                  status, reason, codec, sample_rate, bit_depth, channels, lossless,
                  integrity_ok, checksum_ok, sensor_version, integrity_sensor_version,
                  evidence_json, measured_at)
                VALUES (:trackfile_id, :album_id, :path, :size, :mtime, :status, :reason,
                  :codec, :sample_rate, :bit_depth, :channels, :lossless, :integrity_ok,
                  :checksum_ok, :sensor_version, :integrity_sensor_version,
                  :evidence_json, :measured_at)
                ON CONFLICT(trackfile_id) DO UPDATE SET
                  album_id=excluded.album_id, path=excluded.path, size=excluded.size,
                  mtime=excluded.mtime, status=excluded.status, reason=excluded.reason,
                  codec=excluded.codec, sample_rate=excluded.sample_rate,
                  bit_depth=excluded.bit_depth, channels=excluded.channels,
                  lossless=excluded.lossless, integrity_ok=excluded.integrity_ok,
                  checksum_ok=excluded.checksum_ok,
                  sensor_version=excluded.sensor_version,
                  integrity_sensor_version=excluded.integrity_sensor_version,
                  evidence_json=excluded.evidence_json,
                  measured_at=excluded.measured_at
            """,
                payload,
            )
    except Exception:
        log.exception(
            "state_db.upsert_library_evidence failed for trackfile_id=%s",
            row.get("trackfile_id"),
        )


def upsert_library_metadata(row: dict) -> None:
    """Store only the metadata-tier (ffprobe) evidence for one trackfile (F5.4).

    Partial upsert symmetric with ``update_library_integrity``: it writes the
    codec/tier columns + the metadata ``sensor_version`` and **never touches**
    ``integrity_ok`` / ``checksum_ok`` / ``integrity_sensor_version`` (or the
    spectral columns). So a metadata scan that runs *after* an integrity scan
    cannot clobber the integrity verdict — the tiers stay independent. Used by the
    metadata scan mode; the legacy fused ``upsert_library_evidence`` still writes
    both tiers for the back-compat cheap mode.
    """
    if not _ensure_initialized() or row.get("trackfile_id") is None:
        return
    try:
        payload = {
            "trackfile_id": int(row["trackfile_id"]),
            "album_id": row.get("album_id"),
            "path": row.get("path"),
            "size": row.get("size"),
            "mtime": row.get("mtime"),
            "status": row.get("status"),
            "reason": row.get("reason"),
            "codec": row.get("codec"),
            "sample_rate": row.get("sample_rate"),
            "bit_depth": row.get("bit_depth"),
            "channels": row.get("channels"),
            "lossless": None
            if row.get("lossless") is None
            else int(bool(row.get("lossless"))),
            "sensor_version": row.get("sensor_version"),
            "evidence_json": json.dumps(row.get("evidence") or {}),
            "measured_at": row.get("measured_at") or time.time(),
        }
        with _lock, _connect() as conn:
            conn.execute(
                """
                INSERT INTO library_evidence (trackfile_id, album_id, path, size, mtime,
                  status, reason, codec, sample_rate, bit_depth, channels, lossless,
                  sensor_version, evidence_json, measured_at)
                VALUES (:trackfile_id, :album_id, :path, :size, :mtime, :status, :reason,
                  :codec, :sample_rate, :bit_depth, :channels, :lossless,
                  :sensor_version, :evidence_json, :measured_at)
                ON CONFLICT(trackfile_id) DO UPDATE SET
                  album_id=excluded.album_id, path=excluded.path, size=excluded.size,
                  mtime=excluded.mtime, status=excluded.status, reason=excluded.reason,
                  codec=excluded.codec, sample_rate=excluded.sample_rate,
                  bit_depth=excluded.bit_depth, channels=excluded.channels,
                  lossless=excluded.lossless, sensor_version=excluded.sensor_version,
                  evidence_json=excluded.evidence_json, measured_at=excluded.measured_at
            """,
                payload,
            )
    except Exception:
        log.exception(
            "state_db.upsert_library_metadata failed for trackfile_id=%s",
            row.get("trackfile_id"),
        )


def update_library_integrity(row: dict) -> None:
    """Store only the integrity-tier verdict for one trackfile (F5.4 scan tiers).

    Partial upsert: never touches the metadata (ffprobe) columns, so an integrity
    scan layers ``integrity_ok`` / ``checksum_ok`` + ``integrity_sensor_version``
    onto an existing metadata row without clobbering codec/tier evidence. Creates
    a stub row if the metadata tier has not run yet, so the tiers are independent.
    """
    if not _ensure_initialized() or row.get("trackfile_id") is None:
        return
    try:
        integrity_ok = row.get("integrity_ok")
        checksum_ok = row.get("checksum_ok")
        payload = {
            "trackfile_id": int(row["trackfile_id"]),
            "album_id": row.get("album_id"),
            "integrity_ok": None if integrity_ok is None else int(bool(integrity_ok)),
            "checksum_ok": None if checksum_ok is None else int(bool(checksum_ok)),
            "integrity_sensor_version": row.get("integrity_sensor_version"),
        }
        with _lock, _connect() as conn:
            conn.execute(
                """
                INSERT INTO library_evidence (trackfile_id, album_id, integrity_ok,
                  checksum_ok, integrity_sensor_version)
                VALUES (:trackfile_id, :album_id, :integrity_ok, :checksum_ok,
                  :integrity_sensor_version)
                ON CONFLICT(trackfile_id) DO UPDATE SET
                  album_id=excluded.album_id,
                  integrity_ok=excluded.integrity_ok,
                  checksum_ok=excluded.checksum_ok,
                  integrity_sensor_version=excluded.integrity_sensor_version
            """,
                payload,
            )
    except Exception:
        log.exception(
            "state_db.update_library_integrity failed for trackfile_id=%s",
            row.get("trackfile_id"),
        )


def update_library_spectral(row: dict) -> None:
    """Store only the spectral (Detective) verdict for one trackfile (F5.4 4a).

    Partial upsert: it never touches the cheap-tier ffprobe columns, so a spectral
    update cannot clobber the integrity/codec evidence written by
    ``upsert_library_evidence``. Creates a stub row if the cheap tier has not run
    yet (identity + spectral columns only), so the two sensors are independent.
    """
    if not _ensure_initialized() or row.get("trackfile_id") is None:
        return
    try:
        authentic = row.get("authentic")
        payload = {
            "trackfile_id": int(row["trackfile_id"]),
            "album_id": row.get("album_id"),
            "authentic": None if authentic is None else int(bool(authentic)),
            "spectral_status": row.get("spectral_status"),
            "spectral_reason": row.get("spectral_reason"),
            "spectral_verdict": row.get("spectral_verdict"),
            "spectral_sensor_version": row.get("spectral_sensor_version"),
            "spectral_evidence_json": json.dumps(row.get("spectral_evidence") or {}),
            "spectral_measured_at": row.get("spectral_measured_at") or time.time(),
        }
        with _lock, _connect() as conn:
            conn.execute(
                """
                INSERT INTO library_evidence (trackfile_id, album_id, authentic,
                  spectral_status, spectral_reason, spectral_verdict,
                  spectral_sensor_version, spectral_evidence_json, spectral_measured_at)
                VALUES (:trackfile_id, :album_id, :authentic, :spectral_status,
                  :spectral_reason, :spectral_verdict, :spectral_sensor_version,
                  :spectral_evidence_json, :spectral_measured_at)
                ON CONFLICT(trackfile_id) DO UPDATE SET
                  authentic=excluded.authentic,
                  spectral_status=excluded.spectral_status,
                  spectral_reason=excluded.spectral_reason,
                  spectral_verdict=excluded.spectral_verdict,
                  spectral_sensor_version=excluded.spectral_sensor_version,
                  spectral_evidence_json=excluded.spectral_evidence_json,
                  spectral_measured_at=excluded.spectral_measured_at
            """,
                payload,
            )
    except Exception:
        log.exception(
            "state_db.update_library_spectral failed for trackfile_id=%s",
            row.get("trackfile_id"),
        )


def get_library_evidence(trackfile_id: int) -> dict | None:
    """Return stored evidence for a trackfile, or None."""
    if not _ensure_initialized():
        return None
    try:
        with _lock, _connect() as conn:
            cur = conn.execute(
                "SELECT * FROM library_evidence WHERE trackfile_id = ?", (trackfile_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception:
        log.exception("state_db.get_library_evidence failed for %s", trackfile_id)
        return None


def get_album_library_evidence(album_id: int) -> list[dict]:
    """Return all stored trackfile evidence for an album."""
    if not _ensure_initialized():
        return []
    try:
        with _lock, _connect() as conn:
            cur = conn.execute(
                "SELECT * FROM library_evidence WHERE album_id = ? ORDER BY path",
                (album_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        log.exception("state_db.get_album_library_evidence failed for %s", album_id)
        return []


def list_library_evidence(
    *, limit: int = 5000, offset: int = 0
) -> tuple[int, list[dict]]:
    """List stored library quality evidence for read-only dashboard views."""
    if not _ensure_initialized():
        return (0, [])
    try:
        limit = max(1, min(int(limit), 10000))
        offset = max(0, int(offset))
        with _lock, _connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM library_evidence").fetchone()[0]
            rows = conn.execute(
                """
                SELECT * FROM library_evidence
                ORDER BY album_id IS NULL, album_id ASC, path ASC, trackfile_id ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            return (int(total), [dict(r) for r in rows])
    except Exception:
        log.exception("state_db.list_library_evidence failed")
        return (0, [])


def log_action(
    jid: str, action: str, actor: str, result: str, details: dict | None = None
) -> None:
    """Append-only action log for audit trail."""
    if not _ensure_initialized() or not jid or not action:
        return
    try:
        with _lock, _connect() as conn:
            conn.execute(
                """
                INSERT INTO actions (jid, action, actor, created_at, result, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (jid, action, actor, time.time(), result, json.dumps(details or {})),
            )
    except Exception:
        log.exception("state_db.log_action failed for jid=%s action=%s", jid, action)


def upsert_from_sidecar(sidecar: dict, *, derived_status: str | None = None) -> None:
    """Convenience: upsert record + sensors + files from a full sidecar dict."""
    jid = sidecar.get("jid")
    if not jid:
        return
    upsert_record(sidecar, derived_status=derived_status)
    upsert_sensor_runs(jid, sidecar.get("sensors"))
    upsert_file_evidence(jid, sidecar.get("files"))


# ---------- Read helpers (raise on init-fail — caller's responsibility) ----------


def get_record(jid: str) -> dict | None:
    if not _ensure_initialized():
        return None
    try:
        with _lock, _connect() as conn:
            row = conn.execute("SELECT * FROM records WHERE jid = ?", (jid,)).fetchone()
            return dict(row) if row else None
    except Exception:
        log.exception("state_db.get_record failed for jid=%s", jid)
        return None


def list_records(
    *,
    decision: list[str] | None = None,
    outcome: list[str] | None = None,
    state: list[str] | None = None,
    status: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "ts_desc",
) -> tuple[int, list[dict]]:
    """Return (total, rows). Lightweight query interface for dashboard."""
    if not _ensure_initialized():
        return (0, [])
    try:
        clauses, params = [], []
        if decision:
            placeholders = ",".join("?" * len(decision))
            clauses.append(f"verification_decision IN ({placeholders})")
            params.extend(decision)
        if outcome:
            placeholders = ",".join("?" * len(outcome))
            clauses.append(f"import_outcome IN ({placeholders})")
            params.extend(outcome)
        if state:
            placeholders = ",".join("?" * len(state))
            clauses.append(f"lifecycle_state IN ({placeholders})")
            params.extend(state)
        if status:
            placeholders = ",".join("?" * len(status))
            clauses.append(f"derived_status IN ({placeholders})")
            params.extend(status)
        where_clause = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        order_clause = {
            "ts_asc": "ORDER BY created_at ASC",
            "score_desc": "ORDER BY score DESC NULLS LAST",
        }.get(sort, "ORDER BY created_at DESC")

        with _lock, _connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM records {where_clause}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM records {where_clause} {order_clause} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return (total, [dict(r) for r in rows])
    except Exception:
        log.exception("state_db.list_records failed")
        return (0, [])


def list_actions(jid: str | None = None, limit: int = 100) -> list[dict]:
    if not _ensure_initialized():
        return []
    try:
        with _lock, _connect() as conn:
            if jid:
                rows = conn.execute(
                    "SELECT * FROM actions WHERE jid = ? ORDER BY created_at DESC LIMIT ?",
                    (jid, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM actions ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        log.exception("state_db.list_actions failed for jid=%s", jid)
        return []


def _connector_config_row(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    return data


def get_connector_config(connector_id: str) -> dict | None:
    if not _ensure_initialized() or not connector_id:
        return None
    try:
        with _lock, _connect() as conn:
            row = conn.execute(
                "SELECT * FROM connector_config WHERE connector_id = ?",
                (connector_id,),
            ).fetchone()
            return _connector_config_row(row) if row else None
    except Exception:
        log.exception(
            "state_db.get_connector_config failed for connector_id=%s", connector_id
        )
        return None


def list_connector_config() -> dict[str, dict]:
    if not _ensure_initialized():
        return {}
    try:
        with _lock, _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM connector_config ORDER BY connector_id"
            ).fetchall()
            return {row["connector_id"]: _connector_config_row(row) for row in rows}
    except Exception:
        log.exception("state_db.list_connector_config failed")
        return {}


def set_connector_config(
    connector_id: str,
    *,
    enabled: bool,
    mode: str,
    actor: str = "user_dashboard",
) -> dict | None:
    if not _ensure_initialized() or not connector_id:
        return None
    updated_at = time.time()
    try:
        with _lock, _connect() as conn:
            conn.execute(
                """
                INSERT INTO connector_config (connector_id, enabled, mode, updated_at, actor)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(connector_id) DO UPDATE SET
                  enabled=excluded.enabled,
                  mode=excluded.mode,
                  updated_at=excluded.updated_at,
                  actor=excluded.actor
                """,
                (connector_id, int(bool(enabled)), mode, updated_at, actor),
            )
        return {
            "connector_id": connector_id,
            "enabled": bool(enabled),
            "mode": mode,
            "updated_at": updated_at,
            "actor": actor,
        }
    except Exception:
        log.exception(
            "state_db.set_connector_config failed for connector_id=%s", connector_id
        )
        return None


def count_jobs_by_state() -> dict[str, int]:
    """Aggregate worker-job count per state (for /metrics queue depth)."""
    if not _ensure_initialized():
        return {}
    try:
        with _lock, _connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM jobs "
                "WHERE state IS NOT NULL GROUP BY state"
            ).fetchall()
            return {r["state"]: r["n"] for r in rows}
    except Exception:
        log.exception("state_db.count_jobs_by_state failed")
        return {}


def count_by_status() -> dict[str, int]:
    """Aggregate count per derived_status for summary cards."""
    if not _ensure_initialized():
        return {}
    try:
        with _lock, _connect() as conn:
            rows = conn.execute(
                "SELECT derived_status, COUNT(*) AS n FROM records "
                "WHERE derived_status IS NOT NULL GROUP BY derived_status"
            ).fetchall()
            return {r["derived_status"]: r["n"] for r in rows}
    except Exception:
        log.exception("state_db.count_by_status failed")
        return {}


# ---------- Maintenance ----------


def clear_all() -> None:
    """Test-only helper: clear all data (keep schema)."""
    if not _ensure_initialized():
        return
    with _lock, _connect() as conn:
        for table in ("records", "sensor_runs", "file_evidence", "actions"):
            conn.execute(f"DELETE FROM {table}")


def get_db_path() -> Path:
    return _db_path


# ============================================================================
# F2 worker queue helpers
# ============================================================================


def enqueue_job(
    *,
    jid: str,
    type: str,
    payload: dict | None = None,
    dedupe_key: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    priority: int = 5,
    max_attempts: int = 3,
) -> int | None:
    """Enqueue a new job. Returns job_id, or existing job_id if active dedupe-match.

    Dedupe semantics (per design §14.2): if `dedupe_key` is set and an active job
    (queued/running/cancelling) exists with same key, return that job's id instead
    of inserting a new row.
    """
    if not _ensure_initialized() or not jid or not type:
        return None
    try:
        with _lock, _connect() as conn:
            # Dedupe check (only if key supplied)
            if dedupe_key:
                placeholders = ",".join("?" * len(ACTIVE_JOB_STATES))
                row = conn.execute(
                    f"SELECT id FROM jobs WHERE dedupe_key = ? AND state IN ({placeholders}) "
                    "ORDER BY id DESC LIMIT 1",
                    (dedupe_key, *ACTIVE_JOB_STATES),
                ).fetchone()
                if row:
                    return int(row["id"])

            cur = conn.execute(
                """
                INSERT INTO jobs (jid, type, state, priority, created_at, attempts, max_attempts,
                  dedupe_key, source_type, source_id, payload_json)
                VALUES (?, ?, 'queued', ?, ?, 0, ?, ?, ?, ?, ?)
            """,
                (
                    jid,
                    type,
                    priority,
                    time.time(),
                    max_attempts,
                    dedupe_key,
                    source_type,
                    source_id,
                    json.dumps(payload or {}),
                ),
            )
            row_id = cur.lastrowid
            return int(row_id) if row_id is not None else None
    except Exception:
        log.exception("state_db.enqueue_job failed for jid=%s type=%s", jid, type)
        return None


def dequeue_next_job(
    *,
    worker_id: str,
    lease_sec: float = DEFAULT_LEASE_SEC,
    include_types: list[str] | tuple[str, ...] | None = None,
    exclude_types: list[str] | tuple[str, ...] | None = None,
) -> dict | None:
    """Atomically claim the next eligible job. Returns job dict or None.

    Eligibility: state=queued AND (next_attempt_at IS NULL OR next_attempt_at <= now).
    Ordered by priority ASC, created_at ASC (FIFO within priority).
    """
    if not _ensure_initialized():
        return None
    try:
        now = time.time()
        with _lock, _connect() as conn:
            clauses = [
                "state = 'queued'",
                "(next_attempt_at IS NULL OR next_attempt_at <= ?)",
            ]
            params: list = [now]
            if include_types:
                placeholders = ",".join("?" * len(include_types))
                clauses.append(f"type IN ({placeholders})")
                params.extend(include_types)
            if exclude_types:
                placeholders = ",".join("?" * len(exclude_types))
                clauses.append(f"type NOT IN ({placeholders})")
                params.extend(exclude_types)
            where = " AND ".join(clauses)
            row = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE {where}
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
            """,
                params,
            ).fetchone()
            if not row:
                return None

            # Atomic claim — only update if still queued (prevents double-claim if
            # another thread snuck in; defensive even with N=1 worker).
            updated = conn.execute(
                """
                UPDATE jobs
                SET state = 'running',
                    started_at = ?,
                    heartbeat_at = ?,
                    lease_expires_at = ?,
                    worker_id = ?,
                    attempts = attempts + 1
                WHERE id = ? AND state = 'queued'
            """,
                (now, now, now + lease_sec, worker_id, row["id"]),
            )
            if updated.rowcount == 0:
                return None
            # Re-fetch with updated fields
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            return dict(row)
    except Exception:
        log.exception("state_db.dequeue_next_job failed worker_id=%s", worker_id)
        return None


def heartbeat_job(job_id: int, *, lease_sec: float = DEFAULT_LEASE_SEC) -> None:
    """Update heartbeat_at + extend lease for a running job."""
    if not _ensure_initialized():
        return
    try:
        now = time.time()
        with _lock, _connect() as conn:
            conn.execute(
                "UPDATE jobs SET heartbeat_at = ?, lease_expires_at = ? "
                "WHERE id = ? AND state = 'running'",
                (now, now + lease_sec, job_id),
            )
    except Exception:
        log.exception("state_db.heartbeat_job failed for job_id=%s", job_id)


def update_job_progress(job_id: int, progress: dict) -> None:
    """Write progress_json. Worker calls this at stage transitions + throttled."""
    if not _ensure_initialized():
        return
    try:
        progress = dict(progress)
        progress.setdefault("updated_at", time.time())
        with _lock, _connect() as conn:
            conn.execute(
                "UPDATE jobs SET progress_json = ? WHERE id = ?",
                (json.dumps(progress), job_id),
            )
    except Exception:
        log.exception("state_db.update_job_progress failed for job_id=%s", job_id)


def mark_job_completed(
    job_id: int, *, result_state: str | None = None, result: dict | None = None
) -> None:
    """Worker finished job successfully (execution-state-wise). result_state holds
    the business outcome (imported / blocked / needs_review / etc)."""
    if not _ensure_initialized():
        return
    try:
        with _lock, _connect() as conn:
            conn.execute(
                """
                UPDATE jobs SET state = 'completed', finished_at = ?,
                  result_state = ?, result_json = ?, error_text = NULL
                WHERE id = ?
            """,
                (time.time(), result_state, json.dumps(result or {}), job_id),
            )
    except Exception:
        log.exception("state_db.mark_job_completed failed for job_id=%s", job_id)


def mark_job_failed(
    job_id: int, error_text: str, *, result_state: str | None = "failed"
) -> None:
    """Worker hit unexpected execution failure and no retry was scheduled."""
    if not _ensure_initialized():
        return
    try:
        with _lock, _connect() as conn:
            conn.execute(
                """
                UPDATE jobs SET state = 'failed', finished_at = ?,
                  result_state = ?, error_text = ?
                WHERE id = ?
            """,
                (time.time(), result_state, str(error_text)[:1000], job_id),
            )
    except Exception:
        log.exception("state_db.mark_job_failed failed for job_id=%s", job_id)


def schedule_job_retry(
    job_id: int,
    error_text: str,
    *,
    delay_sec: float,
    result_state: str | None = "retry_scheduled",
) -> bool:
    """Requeue a running job after a transient failure if attempts remain."""
    if not _ensure_initialized():
        return False
    try:
        now = time.time()
        retry_at = now + max(0.0, float(delay_sec))
        progress = {
            "stage": "retry_wait",
            "percent": 0,
            "message": f"Transient failure; retrying in {int(max(0.0, delay_sec))}s",
            "updated_at": now,
            "retry_at": retry_at,
        }
        with _lock, _connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET state = 'queued',
                  result_state = ?,
                  finished_at = NULL,
                  started_at = NULL,
                  heartbeat_at = NULL,
                  lease_expires_at = NULL,
                  worker_id = NULL,
                  next_attempt_at = ?,
                  progress_json = ?,
                  error_text = ?,
                  cancel_requested = 0
                WHERE id = ?
                  AND state IN ('running','cancelling')
                  AND COALESCE(attempts, 0) < COALESCE(max_attempts, 1)
                  AND COALESCE(cancel_requested, 0) = 0
            """,
                (
                    result_state,
                    retry_at,
                    json.dumps(progress),
                    str(error_text)[:1000],
                    job_id,
                ),
            )
            return cur.rowcount > 0
    except Exception:
        log.exception("state_db.schedule_job_retry failed for job_id=%s", job_id)
        return False


def mark_job_cancelled(job_id: int) -> None:
    """Worker confirmed graceful stop after cancel_requested."""
    if not _ensure_initialized():
        return
    try:
        with _lock, _connect() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'cancelled', finished_at = ?, result_state = 'cancelled' WHERE id = ?",
                (time.time(), job_id),
            )
    except Exception:
        log.exception("state_db.mark_job_cancelled failed for job_id=%s", job_id)


def request_job_cancel(job_id: int) -> bool:
    """Dashboard-trigger: set cancel_requested. Returns True if found."""
    if not _ensure_initialized():
        return False
    try:
        with _lock, _connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET cancel_requested = 1,
                    state = CASE WHEN state = 'running' THEN 'cancelling' ELSE state END
                WHERE id = ? AND state IN ('queued','running','cancelling')
                """,
                (job_id,),
            )
            return cur.rowcount > 0
    except Exception:
        log.exception("state_db.request_job_cancel failed for job_id=%s", job_id)
        return False


def is_job_cancel_requested(job_id: int) -> bool:
    """Worker checks this between stages. Returns False on error (default safe)."""
    if not _ensure_initialized():
        return False
    try:
        with _lock, _connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return bool(row and row["cancel_requested"])
    except Exception:
        return False


def get_job(job_id: int) -> dict | None:
    if not _ensure_initialized():
        return None
    try:
        with _lock, _connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None
    except Exception:
        log.exception("state_db.get_job failed for id=%s", job_id)
        return None


def list_jobs(
    *,
    state: list[str] | None = None,
    type: list[str] | None = None,
    jid: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    if not _ensure_initialized():
        return (0, [])
    try:
        clauses, params = [], []
        if state:
            placeholders = ",".join("?" * len(state))
            clauses.append(f"state IN ({placeholders})")
            params.extend(state)
        if type:
            placeholders = ",".join("?" * len(type))
            clauses.append(f"type IN ({placeholders})")
            params.extend(type)
        if jid:
            clauses.append("jid = ?")
            params.append(jid)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with _lock, _connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM jobs {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return (total, [dict(r) for r in rows])
    except Exception:
        log.exception("state_db.list_jobs failed")
        return (0, [])


def recover_stale_running_jobs(
    *, max_age_sec: float | None = None, force: bool = False
) -> int:
    """Boot-time + periodic recovery: jobs in state=running with expired lease.

    F2.1 policy: requeue once (reset state to queued, increment attempts).
    F2.5+ may add per-failure-class policy (e.g., fail on second stale-detection).

    Use force=True at worker boot. With a single gunicorn worker/thread, any
    running job found at startup belongs to a prior process and must not wait
    for its old lease to expire.

    Returns count of jobs recovered.
    """
    if not _ensure_initialized():
        return 0
    try:
        now = time.time()
        stale_before = now - (max_age_sec or 3600)
        with _lock, _connect() as conn:
            # Find stale running: lease expired OR no lease set + started_at very old
            if force:
                rows = conn.execute("""
                    SELECT id, attempts, max_attempts FROM jobs
                    WHERE state = 'running'
                """).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, attempts, max_attempts FROM jobs
                    WHERE state = 'running'
                      AND (
                        (lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                        OR (lease_expires_at IS NULL AND started_at < ?)
                      )
                """,
                    (now, stale_before),
                ).fetchall()

            recovered = 0
            for row in rows:
                attempts = row["attempts"] or 0
                max_a = row["max_attempts"] or 3
                if attempts >= max_a:
                    # Already at limit — terminal failure
                    conn.execute(
                        """
                        UPDATE jobs SET state = 'failed', finished_at = ?,
                          result_state = 'stale', error_text = 'lease expired, attempts exhausted'
                        WHERE id = ?
                    """,
                        (now, row["id"]),
                    )
                else:
                    # Requeue with delay to avoid hot-loop
                    conn.execute(
                        """
                        UPDATE jobs SET state = 'queued',
                          started_at = NULL, heartbeat_at = NULL, lease_expires_at = NULL,
                          worker_id = NULL,
                          next_attempt_at = ?,
                          error_text = 'recovered from stale lease'
                        WHERE id = ?
                    """,
                        (now + 5, row["id"]),
                    )
                recovered += 1
            if recovered:
                log.info("recovered %d stale running jobs", recovered)
            return recovered
    except Exception:
        log.exception("state_db.recover_stale_running_jobs failed")
        return 0


def count_active_jobs(
    *,
    include_types: list[str] | tuple[str, ...] | None = None,
    exclude_types: list[str] | tuple[str, ...] | None = None,
) -> int:
    """Quick count of jobs in non-terminal states (for dashboard summary)."""
    if not _ensure_initialized():
        return 0
    try:
        with _lock, _connect() as conn:
            clauses = []
            params: list = []
            placeholders = ",".join("?" * len(ACTIVE_JOB_STATES))
            clauses.append(f"state IN ({placeholders})")
            params.extend(ACTIVE_JOB_STATES)
            if include_types:
                placeholders = ",".join("?" * len(include_types))
                clauses.append(f"type IN ({placeholders})")
                params.extend(include_types)
            if exclude_types:
                placeholders = ",".join("?" * len(exclude_types))
                clauses.append(f"type NOT IN ({placeholders})")
                params.extend(exclude_types)
            where = " AND ".join(clauses)
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE {where}",
                    params,
                ).fetchone()[0]
            )
    except Exception:
        return 0


def find_active_job_by_dedupe(dedupe_key: str) -> dict | None:
    """Return active job (queued/running/cancelling) with matching dedupe_key, or None.

    Used by callers that need to check for existing job BEFORE creating new jid
    (e.g., SAB addurl-handler dedupes on tidal:<album_id>).
    """
    if not _ensure_initialized() or not dedupe_key:
        return None
    try:
        with _lock, _connect() as conn:
            placeholders = ",".join("?" * len(ACTIVE_JOB_STATES))
            row = conn.execute(
                f"SELECT * FROM jobs WHERE dedupe_key = ? AND state IN ({placeholders}) "
                "ORDER BY id DESC LIMIT 1",
                (dedupe_key, *ACTIVE_JOB_STATES),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        log.exception("state_db.find_active_job_by_dedupe failed key=%s", dedupe_key)
        return None


# ============================================================================
# F5.4 slice 5: background library scan state
# ============================================================================


def enqueue_library_scan(
    *, mode: str = "cheap", requested_by: str | None = None
) -> dict | None:
    """Create a library-scan run and matching low-priority F2 job.

    Slice 5b deliberately reuses the F2 ``jobs`` table for lease, heartbeat,
    cancellation, priority, and dedupe. There is no separate scan lease table:
    an active ``library_scan`` job is the future scanner's lease. This helper only
    creates durable state; no scanner/executor is registered in this slice.

    Returns the active run. If another scan run is already queued/running/
    cancelling, that existing run is returned and no new job is inserted.
    """
    mode = (mode or "cheap").strip() or "cheap"
    if not _ensure_initialized():
        return None
    try:
        now = time.time()
        with _lock, _connect() as conn:
            placeholders = ",".join("?" * len(ACTIVE_LIBRARY_SCAN_STATES))
            existing = conn.execute(
                f"""
                SELECT * FROM library_scan_runs
                WHERE state IN ({placeholders})
                ORDER BY id DESC
                LIMIT 1
                """,
                ACTIVE_LIBRARY_SCAN_STATES,
            ).fetchone()
            if existing:
                return dict(existing)

            cur = conn.execute(
                """
                INSERT INTO library_scan_runs
                  (mode, state, created_at, updated_at, requested_by)
                VALUES (?, 'queued', ?, ?, ?)
                """,
                (mode, now, now, requested_by),
            )
            if cur.lastrowid is None:
                return None
            run_id = int(cur.lastrowid)
            jid = f"library-scan-{run_id}"
            payload = {"run_id": run_id, "mode": mode}
            job_cur = conn.execute(
                """
                INSERT INTO jobs (jid, type, state, priority, created_at, attempts,
                  max_attempts, dedupe_key, source_type, source_id, payload_json)
                VALUES (?, ?, 'queued', ?, ?, 0, ?, ?, 'library', ?, ?)
                """,
                (
                    jid,
                    LIBRARY_SCAN_JOB_TYPE,
                    LIBRARY_SCAN_PRIORITY,
                    now,
                    LIBRARY_SCAN_MAX_ATTEMPTS,
                    LIBRARY_SCAN_DEDUPE_KEY,
                    mode,
                    json.dumps(payload),
                ),
            )
            if job_cur.lastrowid is None:
                return None
            job_id = int(job_cur.lastrowid)
            conn.execute(
                """
                UPDATE library_scan_runs
                SET worker_job_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (job_id, now, run_id),
            )
            row = conn.execute(
                "SELECT * FROM library_scan_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        log.exception("state_db.enqueue_library_scan failed mode=%s", mode)
        return None


def get_library_scan_run(run_id: int) -> dict | None:
    """Return one library scan run by id."""
    if not _ensure_initialized():
        return None
    try:
        with _lock, _connect() as conn:
            row = conn.execute(
                "SELECT * FROM library_scan_runs WHERE id = ?", (int(run_id),)
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        log.exception("state_db.get_library_scan_run failed id=%s", run_id)
        return None


def reconcile_orphaned_library_scan_runs() -> int:
    """Fail active scan runs whose backing F2 job is terminal or missing.

    On an abnormal worker death (e.g. a container restart mid-scan), boot recovery
    fails the ``library_scan`` job, but nothing updates the ``library_scan_runs``
    row — it stays ``running``/``cancelling`` forever and blocks every future scan
    via the active-run dedupe. Called at scan-worker startup: any active run whose
    job is terminal or gone is marked ``failed`` so a fresh scan can start. A run
    whose job is still ``queued``/``running`` is left alone — a live worker (or
    stale-job recovery) still owns it. Returns the number of runs reconciled.
    """
    if not _ensure_initialized():
        return 0
    try:
        now = time.time()
        with _lock, _connect() as conn:
            placeholders = ",".join("?" * len(ACTIVE_LIBRARY_SCAN_STATES))
            rows = conn.execute(
                f"""
                SELECT id, worker_job_id FROM library_scan_runs
                WHERE state IN ({placeholders})
                """,
                ACTIVE_LIBRARY_SCAN_STATES,
            ).fetchall()
            orphaned: list[int] = []
            for row in rows:
                job_id = row["worker_job_id"]
                if job_id is None:
                    orphaned.append(int(row["id"]))
                    continue
                job = conn.execute(
                    "SELECT state FROM jobs WHERE id = ?", (int(job_id),)
                ).fetchone()
                if job is None or job["state"] in TERMINAL_JOB_STATES:
                    orphaned.append(int(row["id"]))
            for run_id in orphaned:
                conn.execute(
                    """
                    UPDATE library_scan_runs
                    SET state = 'failed',
                        finished_at = COALESCE(finished_at, ?),
                        updated_at = ?,
                        last_error = COALESCE(last_error,
                          'worker restarted; backing job no longer running')
                    WHERE id = ?
                    """,
                    (now, now, run_id),
                )
            return len(orphaned)
    except Exception:
        log.exception("state_db.reconcile_orphaned_library_scan_runs failed")
        return 0


def get_active_library_scan_run() -> dict | None:
    """Return the newest active library scan run, if any."""
    if not _ensure_initialized():
        return None
    try:
        with _lock, _connect() as conn:
            placeholders = ",".join("?" * len(ACTIVE_LIBRARY_SCAN_STATES))
            row = conn.execute(
                f"""
                SELECT * FROM library_scan_runs
                WHERE state IN ({placeholders})
                ORDER BY id DESC
                LIMIT 1
                """,
                ACTIVE_LIBRARY_SCAN_STATES,
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        log.exception("state_db.get_active_library_scan_run failed")
        return None


def list_library_scan_runs(
    *, state: list[str] | None = None, limit: int = 20, offset: int = 0
) -> tuple[int, list[dict]]:
    """List scan runs for status/history views."""
    if not _ensure_initialized():
        return (0, [])
    try:
        clauses, params = [], []
        if state:
            placeholders = ",".join("?" * len(state))
            clauses.append(f"state IN ({placeholders})")
            params.extend(state)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with _lock, _connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM library_scan_runs {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT * FROM library_scan_runs {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()
            return (int(total), [dict(r) for r in rows])
    except Exception:
        log.exception("state_db.list_library_scan_runs failed")
        return (0, [])


def update_library_scan_run_state(
    run_id: int,
    state: str,
    *,
    last_error: str | None = None,
    totals: dict | None = None,
) -> None:
    """Update scan-run state/progress.

    Future scanner slices call this as the job moves through queued/running/
    terminal states. ``totals`` is optional and may contain any of the counter
    columns; unknown keys are ignored.
    """
    if not _ensure_initialized():
        return
    try:
        now = time.time()
        state = str(state)
        started_at_expr = "started_at"
        finished_at_expr = "finished_at"
        params: list = [state, now, last_error]
        if state == "running":
            started_at_expr = "COALESCE(started_at, ?)"
            params.append(now)
        if state in TERMINAL_LIBRARY_SCAN_STATES:
            finished_at_expr = "COALESCE(finished_at, ?)"
            params.append(now)

        allowed_totals = {
            "total_items",
            "processed_items",
            "measured_items",
            "fresh_items",
            "unmeasured_items",
            "error_items",
        }
        assignments = [
            "state = ?",
            "updated_at = ?",
            "last_error = COALESCE(?, last_error)",
            f"started_at = {started_at_expr}",
            f"finished_at = {finished_at_expr}",
        ]
        for key, value in (totals or {}).items():
            if key in allowed_totals:
                assignments.append(f"{key} = ?")
                params.append(int(value or 0))
        params.append(int(run_id))
        with _lock, _connect() as conn:
            conn.execute(
                f"""
                UPDATE library_scan_runs
                SET {", ".join(assignments)}
                WHERE id = ?
                """,
                params,
            )
    except Exception:
        log.exception(
            "state_db.update_library_scan_run_state failed id=%s state=%s",
            run_id,
            state,
        )


def request_library_scan_cancel(run_id: int) -> bool:
    """Request cooperative cancellation for a scan run and its backing F2 job."""
    if not _ensure_initialized():
        return False
    try:
        with _lock, _connect() as conn:
            row = conn.execute(
                "SELECT * FROM library_scan_runs WHERE id = ?", (int(run_id),)
            ).fetchone()
            if not row or row["state"] not in ACTIVE_LIBRARY_SCAN_STATES:
                return False
            now = time.time()
            conn.execute(
                """
                UPDATE library_scan_runs
                SET cancel_requested = 1,
                    state = CASE WHEN state = 'running' THEN 'cancelling' ELSE state END,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, int(run_id)),
            )
            job_id = row["worker_job_id"]
        if job_id is not None:
            request_job_cancel(int(job_id))
        return True
    except Exception:
        log.exception("state_db.request_library_scan_cancel failed id=%s", run_id)
        return False


def upsert_library_scan_item(
    run_id: int,
    trackfile_id: int,
    *,
    album_id: int | None = None,
    state: str,
    attempts: int | None = None,
    last_error: str | None = None,
) -> None:
    """Insert/update one item in a scan-run ledger.

    The item ledger is the restart/resume substrate for slice 5c+ and mandatory
    before any background spectral work. It stores IDs and outcomes only; file
    paths remain in ``library_evidence``.
    """
    if not _ensure_initialized():
        return
    try:
        now = time.time()
        with _lock, _connect() as conn:
            conn.execute(
                """
                INSERT INTO library_scan_items
                  (run_id, trackfile_id, album_id, state, attempts, updated_at,
                   last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, trackfile_id) DO UPDATE SET
                  album_id=excluded.album_id,
                  state=excluded.state,
                  attempts=COALESCE(excluded.attempts, library_scan_items.attempts),
                  updated_at=excluded.updated_at,
                  last_error=excluded.last_error
                """,
                (
                    int(run_id),
                    int(trackfile_id),
                    album_id,
                    str(state),
                    attempts,
                    now,
                    last_error,
                ),
            )
    except Exception:
        log.exception(
            "state_db.upsert_library_scan_item failed run_id=%s trackfile_id=%s",
            run_id,
            trackfile_id,
        )


def get_library_scan_item(run_id: int, trackfile_id: int) -> dict | None:
    """Return one scan-run item ledger row, if present."""
    if not _ensure_initialized():
        return None
    try:
        with _lock, _connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM library_scan_items
                WHERE run_id = ? AND trackfile_id = ?
                """,
                (int(run_id), int(trackfile_id)),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        log.exception(
            "state_db.get_library_scan_item failed run_id=%s trackfile_id=%s",
            run_id,
            trackfile_id,
        )
        return None


def list_library_scan_items(
    run_id: int,
    *,
    state: list[str] | None = None,
    limit: int = 500,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    """List scan-run items with optional state filter."""
    if not _ensure_initialized():
        return (0, [])
    try:
        clauses = ["run_id = ?"]
        params: list = [int(run_id)]
        if state:
            placeholders = ",".join("?" * len(state))
            clauses.append(f"state IN ({placeholders})")
            params.extend(state)
        where = "WHERE " + " AND ".join(clauses)
        with _lock, _connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM library_scan_items {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT * FROM library_scan_items {where}
                ORDER BY trackfile_id ASC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()
            return (int(total), [dict(r) for r in rows])
    except Exception:
        log.exception("state_db.list_library_scan_items failed run_id=%s", run_id)
        return (0, [])
