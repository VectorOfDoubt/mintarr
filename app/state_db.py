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
    or os.environ.get("TIDALHIRES_STATE_DB", "/config/tidalhires_state.db")
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
"""

# Default lease/heartbeat per F2 design
DEFAULT_LEASE_SEC = 300.0    # 5 min — recovery-trigger
HEARTBEAT_INTERVAL_SEC = 30.0  # worker pings DB every 30s during long ops

ACTIVE_JOB_STATES = ("queued", "running", "cancelling")
TERMINAL_JOB_STATES = ("completed", "failed", "cancelled")


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
    conn.execute(
        "UPDATE records SET source_type = 'tidal' WHERE source_type IS NULL"
    )
    log.info("state_db: added records.source_type column (backfilled to 'tidal')")


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
            "created_at": float(lifecycle.get("created_at") or sidecar.get("ts") or now),
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
            conn.execute("""
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
            """, params)
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
            rows.append({
                "jid": jid,
                "sensor_name": s.get("name", ""),
                "sensor_class": s.get("class"),
                "status": s.get("status"),
                "severity": s.get("severity"),
                "confidence": s.get("confidence"),
                "duration_ms": s.get("duration_ms"),
                "evidence_json": json.dumps(s.get("evidence") or {}),
            })
        if not rows:
            return
        with _lock, _connect() as conn:
            conn.execute("DELETE FROM sensor_runs WHERE jid = ?", (jid,))
            conn.executemany("""
                INSERT INTO sensor_runs (jid, sensor_name, sensor_class, status, severity,
                  confidence, duration_ms, evidence_json)
                VALUES (:jid, :sensor_name, :sensor_class, :status, :severity,
                  :confidence, :duration_ms, :evidence_json)
            """, rows)
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
            rows.append({
                "jid": jid,
                "filename": f.get("filename") or f.get("filepath") or "",
                "sample_rate": f.get("sample_rate"),
                "bit_depth": f.get("bit_depth"),
                "cutoff_hz": f.get("cutoff_hz") or f.get("cutoff_freq"),
                "nyquist_hz": f.get("nyquist_hz"),
                "detective_verdict": f.get("detective_verdict") or f.get("verdict"),
                "is_fake_high_res": int(bool(f.get("is_fake_high_res"))) if f.get("is_fake_high_res") is not None else None,
                "estimated_mp3_bitrate": f.get("estimated_mp3_bitrate"),
                "evidence_json": json.dumps(f),
            })
        if not rows:
            return
        with _lock, _connect() as conn:
            conn.execute("DELETE FROM file_evidence WHERE jid = ?", (jid,))
            conn.executemany("""
                INSERT INTO file_evidence (jid, filename, sample_rate, bit_depth, cutoff_hz,
                  nyquist_hz, detective_verdict, is_fake_high_res, estimated_mp3_bitrate, evidence_json)
                VALUES (:jid, :filename, :sample_rate, :bit_depth, :cutoff_hz,
                  :nyquist_hz, :detective_verdict, :is_fake_high_res, :estimated_mp3_bitrate, :evidence_json)
            """, rows)
    except Exception:
        log.exception("state_db.upsert_file_evidence failed for jid=%s", jid)


def log_action(jid: str, action: str, actor: str, result: str, details: dict | None = None) -> None:
    """Append-only action log for audit trail."""
    if not _ensure_initialized() or not jid or not action:
        return
    try:
        with _lock, _connect() as conn:
            conn.execute("""
                INSERT INTO actions (jid, action, actor, created_at, result, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (jid, action, actor, time.time(), result, json.dumps(details or {})))
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
            total = conn.execute(f"SELECT COUNT(*) FROM records {where_clause}", params).fetchone()[0]
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

            cur = conn.execute("""
                INSERT INTO jobs (jid, type, state, priority, created_at, attempts, max_attempts,
                  dedupe_key, source_type, source_id, payload_json)
                VALUES (?, ?, 'queued', ?, ?, 0, ?, ?, ?, ?, ?)
            """, (
                jid, type, priority, time.time(), max_attempts,
                dedupe_key, source_type, source_id,
                json.dumps(payload or {}),
            ))
            return int(cur.lastrowid)
    except Exception:
        log.exception("state_db.enqueue_job failed for jid=%s type=%s", jid, type)
        return None


def dequeue_next_job(*, worker_id: str, lease_sec: float = DEFAULT_LEASE_SEC) -> dict | None:
    """Atomically claim the next eligible job. Returns job dict or None.

    Eligibility: state=queued AND (next_attempt_at IS NULL OR next_attempt_at <= now).
    Ordered by priority ASC, created_at ASC (FIFO within priority).
    """
    if not _ensure_initialized():
        return None
    try:
        now = time.time()
        with _lock, _connect() as conn:
            row = conn.execute("""
                SELECT * FROM jobs
                WHERE state = 'queued'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
            """, (now,)).fetchone()
            if not row:
                return None

            # Atomic claim — only update if still queued (prevents double-claim if
            # another thread snuck in; defensive even with N=1 worker).
            updated = conn.execute("""
                UPDATE jobs
                SET state = 'running',
                    started_at = ?,
                    heartbeat_at = ?,
                    lease_expires_at = ?,
                    worker_id = ?,
                    attempts = attempts + 1
                WHERE id = ? AND state = 'queued'
            """, (now, now, now + lease_sec, worker_id, row["id"]))
            if updated.rowcount == 0:
                return None
            # Re-fetch with updated fields
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
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


def mark_job_completed(job_id: int, *, result_state: str | None = None,
                       result: dict | None = None) -> None:
    """Worker finished job successfully (execution-state-wise). result_state holds
    the business outcome (imported / blocked / needs_review / etc)."""
    if not _ensure_initialized():
        return
    try:
        with _lock, _connect() as conn:
            conn.execute("""
                UPDATE jobs SET state = 'completed', finished_at = ?,
                  result_state = ?, result_json = ?, error_text = NULL
                WHERE id = ?
            """, (time.time(), result_state, json.dumps(result or {}), job_id))
    except Exception:
        log.exception("state_db.mark_job_completed failed for job_id=%s", job_id)


def mark_job_failed(job_id: int, error_text: str, *,
                    result_state: str | None = "failed") -> None:
    """Worker hit unexpected execution failure and no retry was scheduled."""
    if not _ensure_initialized():
        return
    try:
        with _lock, _connect() as conn:
            conn.execute("""
                UPDATE jobs SET state = 'failed', finished_at = ?,
                  result_state = ?, error_text = ?
                WHERE id = ?
            """, (time.time(), result_state, str(error_text)[:1000], job_id))
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
            cur = conn.execute("""
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
            """, (
                result_state,
                retry_at,
                json.dumps(progress),
                str(error_text)[:1000],
                job_id,
            ))
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
            total = conn.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return (total, [dict(r) for r in rows])
    except Exception:
        log.exception("state_db.list_jobs failed")
        return (0, [])


def recover_stale_running_jobs(*, max_age_sec: float | None = None, force: bool = False) -> int:
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
                rows = conn.execute("""
                    SELECT id, attempts, max_attempts FROM jobs
                    WHERE state = 'running'
                      AND (
                        (lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                        OR (lease_expires_at IS NULL AND started_at < ?)
                      )
                """, (now, stale_before)).fetchall()

            recovered = 0
            for row in rows:
                attempts = row["attempts"] or 0
                max_a = row["max_attempts"] or 3
                if attempts >= max_a:
                    # Already at limit — terminal failure
                    conn.execute("""
                        UPDATE jobs SET state = 'failed', finished_at = ?,
                          result_state = 'stale', error_text = 'lease expired, attempts exhausted'
                        WHERE id = ?
                    """, (now, row["id"]))
                else:
                    # Requeue with delay to avoid hot-loop
                    conn.execute("""
                        UPDATE jobs SET state = 'queued',
                          started_at = NULL, heartbeat_at = NULL, lease_expires_at = NULL,
                          worker_id = NULL,
                          next_attempt_at = ?,
                          error_text = 'recovered from stale lease'
                        WHERE id = ?
                    """, (now + 5, row["id"]))
                recovered += 1
            if recovered:
                log.info("recovered %d stale running jobs", recovered)
            return recovered
    except Exception:
        log.exception("state_db.recover_stale_running_jobs failed")
        return 0


def count_active_jobs() -> int:
    """Quick count of jobs in non-terminal states (for dashboard summary)."""
    if not _ensure_initialized():
        return 0
    try:
        with _lock, _connect() as conn:
            placeholders = ",".join("?" * len(ACTIVE_JOB_STATES))
            return int(conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE state IN ({placeholders})",
                ACTIVE_JOB_STATES,
            ).fetchone()[0])
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
