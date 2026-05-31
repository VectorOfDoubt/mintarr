# Data Model

> **Type:** Architecture / subsystem
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Schema is stable; migration policy is in place.
> **Audience:** Anyone touching state_db, sidecars, or decisions.jsonl. Backup-tool authors. Migration authors.
> **Related:** [SIDECAR_FORMAT_v2.md](../specs/SIDECAR_FORMAT_v2.md), [OVERVIEW.md](OVERVIEW.md)

---

## 1. The three persistence layers

Mintarr persists state in three places. Each has a specific role:

| Layer | Format | Source of truth? | Why it exists |
|---|---|---|---|
| Sidecars | `verification.json` per record | **Yes** | Authoritative record state; survives container restart, DB corruption, schema changes |
| state_db | SQLite | No (rebuilt from sidecars) | Query index — fast dashboard reads, joins, aggregations |
| decisions.jsonl | Append-only JSON-Lines | Audit trail | Forensic grep, survives sidecar deletion |

If sidecars and state_db disagree, sidecars win. The `backfill_state` script rebuilds state_db from sidecars.

## 2. Sidecar format

Fully specified in [`SIDECAR_FORMAT_v2.md`](../specs/SIDECAR_FORMAT_v2.md). Summary:

- One JSON file per record (per jid)
- Written atomically (temp + rename)
- Path varies by lifecycle state:
  - `<OUTPUT_BASE>/<jid>/verification.json` — active records
  - `/config/blocked_decisions/<jid>.json` — BLOCK terminal
  - `/config/discarded/<jid>.json` — operator-discarded
  - `/config/expired_review/<jid>.json` — REVIEW_REQUIRED expired

## 3. state_db schema

SQLite database at `/config/mintarr_state.db`. Five tables. WAL mode for reader-concurrency.

### 3.1 `records` table

One row per import attempt.

```sql
CREATE TABLE records (
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
    expired_at REAL,
    source_type TEXT
);

CREATE INDEX idx_records_status ON records(derived_status);
CREATE INDEX idx_records_created ON records(created_at DESC);
CREATE INDEX idx_records_decision ON records(verification_decision);
```

Field reference:

| Column | Type | Description |
|---|---|---|
| `jid` | TEXT (PK) | 12-character hex job ID |
| `title` | TEXT | Release title at grab time |
| `album_ids_json` | TEXT | JSON array of Lidarr album IDs |
| `created_at` | REAL | Unix timestamp of first sidecar write |
| `updated_at` | REAL | Unix timestamp of last sidecar revision |
| `verification_decision` | TEXT | One of `ACCEPT` / `ACCEPT_PROVISIONAL` / `REVIEW_REQUIRED` / `BLOCK` |
| `import_outcome` | TEXT | One of `MANUAL_IMPORTED` / `RESCUED` / `FAILED` / `PENDING` / `SKIPPED`, or NULL |
| `derived_status` | TEXT | Computed from decision + outcome + lifecycle; one of `imported` / `needs_review` / `blocked` / `failed` / `discarded` / `expired` |
| `score` | INTEGER | V2 score 0-100 |
| `verdict` | TEXT | FLAC Detective verdict at verify time |
| `lifecycle_state` | TEXT | One of `created` / `pending_review` / `promoted` / `discarded` / `expired` |
| `actor` | TEXT | Identifier of last action actor (operator, auto-policy) |
| `discarded_at` | REAL | Unix timestamp of discard, or NULL |
| `promoted_at` | REAL | Unix timestamp of promotion, or NULL |
| `expired_at` | REAL | Unix timestamp of auto-expiry, or NULL |
| `source_type` | TEXT | Source identifier (`tidal`, `local`, ...) |

### 3.2 `sensor_runs` table

Per-verifier sensor results. One row per (jid, sensor_name).

```sql
CREATE TABLE sensor_runs (
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

CREATE INDEX idx_sensor_runs_jid ON sensor_runs(jid);
CREATE INDEX idx_sensor_runs_status ON sensor_runs(status);
```

Mirrors the `sensors` array in the sidecar. `evidence_json` is the sensor-specific evidence payload as a serialised JSON string.

### 3.3 `file_evidence` table

Per-file evidence from FLAC Detective. One row per (jid, filename).

```sql
CREATE TABLE file_evidence (
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

CREATE INDEX idx_file_evidence_jid ON file_evidence(jid);
```

Mirrors the `files` array in the sidecar.

### 3.4 `actions` table

Operator action audit trail.

```sql
CREATE TABLE actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jid TEXT,
    action TEXT,
    actor TEXT,
    created_at REAL,
    result TEXT,
    details_json TEXT
);

CREATE INDEX idx_actions_jid ON actions(jid);
CREATE INDEX idx_actions_created ON actions(created_at DESC);
```

Field reference:

| Column | Description |
|---|---|
| `id` | Auto-increment row ID |
| `jid` | Record this action targeted |
| `action` | One of `promote` / `discard` / `retry_import` / `cancel` / `bulk_review_expire` |
| `actor` | Identifier of the actor (`operator-via-dashboard`, `api-key:xxx`, `auto-policy`, `proxy-user:<remote_user>`) |
| `created_at` | Unix timestamp |
| `result` | One of `success` / `failed` / `noop` |
| `details_json` | JSON object with action-specific context (request payload, error message, etc.) |

### 3.5 `jobs` table

Worker queue.

```sql
CREATE TABLE jobs (
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

CREATE INDEX idx_jobs_state ON jobs(state, next_attempt_at);
CREATE INDEX idx_jobs_jid ON jobs(jid);
CREATE INDEX idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX idx_jobs_dedupe ON jobs(dedupe_key, state);
CREATE INDEX idx_jobs_lease ON jobs(state, lease_expires_at);
```

Field reference:

| Column | Description |
|---|---|
| `id` | Auto-increment row ID |
| `jid` | Associated record |
| `type` | Job type (`tidal_grab`, `local_grab`, `promote_import`, `retry_import`, `soulseek_grab`, ...) |
| `state` | Worker state machine: `queued` / `running` / `cancelling` / `completed` / `failed` / `cancelled` |
| `result_state` | Business outcome distinct from `state`; see §3.5.1 |
| `priority` | Lower number = higher priority. Default 5. |
| `dedupe_key` | `f"{source}:{hash_or_id}"` — duplicate prevention |
| `source_type` | Source family |
| `source_id` | Source-specific ID for this candidate |
| `payload_json` | Job-type-specific payload as JSON |
| `progress_json` | Last progress update from `ctx.set_progress` |
| `result_json` | Worker result payload at job completion |
| `error_text` | Short error description on failure |
| `worker_id` | Identifier of the worker that claimed this job |
| `cancel_requested` | 1 if operator cancelled; 0 otherwise |
| `attempts`, `max_attempts` | Retry counter and limit |
| `lease_expires_at` | Lease deadline; expired leases get recovered by another worker |
| `heartbeat_at` | Last worker heartbeat timestamp |

#### 3.5.1 `state` vs `result_state`

`state` is the worker execution state (did the executor finish?). `result_state` is the business outcome (did the import succeed?).

A job can be `state=completed, result_state=blocked` — worker succeeded but V2 blocked the import. Or `state=completed, result_state=needs_review` — worker succeeded but the operator must decide. The distinction matters for the worker's retry policy (which only re-runs `state=failed` jobs) versus the dashboard's status display (which shows `result_state`).

## 4. Migrations

Mintarr's schema evolves over time. The migration policy:

### 4.1 Additive-only

Schema changes within a major Mintarr version are **additive only**:

- Add columns: `ALTER TABLE records ADD COLUMN ...`
- Add tables: `CREATE TABLE IF NOT EXISTS ...`
- Add indexes: `CREATE INDEX IF NOT EXISTS ...`

Drops, renames, type changes require a major Mintarr version bump and an explicit migration tool.

### 4.2 Migration mechanics

Migrations live in `state_db.init()`. They use `PRAGMA table_info(<table>)` to check whether the column exists, then `ALTER TABLE` only if missing. Example (the F3.1 `source_type` migration):

```python
def _ensure_records_source_type_column(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()}
    if "source_type" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN source_type TEXT")
        conn.execute("UPDATE records SET source_type = 'tidal' WHERE source_type IS NULL")
```

The migration runs at every container boot. It is idempotent — running it twice does nothing the second time.

### 4.3 Backup before migrations

`UPGRADE_GUIDE.md` documents that operators should back up `/config/mintarr_state.db` before any major Mintarr version upgrade. The backup mechanism is `sqlite3 backup` for a consistent snapshot:

```bash
docker exec mintarr sqlite3 /config/mintarr_state.db ".backup /config/backups/state.$(date +%Y%m%d).db"
```

This is also automated as a scheduled task in Phase 3 (Observability and integration surface) work.

### 4.4 Rollback

Additive migrations are irreversible in practice. Rolling back means:

1. Stop the container
2. Restore the previous state_db from backup
3. Downgrade Mintarr to the prior version
4. Start container

Mintarr does not ship a "downgrade" command. Restoring a backup is the documented rollback path.

## 5. decisions.jsonl

Append-only JSON-Lines log at `/config/decisions.jsonl`. One line per V2 decision; each line is a complete sidecar JSON.

### 5.1 Why both sidecars and decisions.jsonl

Sidecars are per-record files. They can be deleted (operator discards, lifecycle expiry, manual cleanup). decisions.jsonl is append-only — once written, the line is never removed.

This gives Mintarr two complementary properties:

- **Recoverability:** sidecars are the source of truth; deleting one removes the record from state. The state_db rebuilds from sidecars via `backfill_state`.
- **Forensic continuity:** decisions.jsonl preserves a record of every decision ever made, regardless of subsequent sidecar deletion. Useful for "did we ever import this album?" queries after the record itself has been discarded.

### 5.2 Format

Each line is a single JSON object matching the sidecar schema. Newlines within values are escaped (JSON-Lines convention). The file is UTF-8 encoded.

### 5.3 Reading

`grep` works. So does:

```bash
docker exec mintarr python3 -c "
import json
with open('/config/decisions.jsonl') as f:
    for line in f:
        rec = json.loads(line)
        if rec.get('verdict') == 'FAKE_CERTAIN':
            print(rec['jid'], rec['title'])
"
```

The Mintarr dashboard `GET /dashboard/v1/decisions` returns recent decisions.jsonl entries.

### 5.4 Rotation

decisions.jsonl is not rotated by Mintarr. Operators with very large grab volumes may want to rotate manually:

```bash
mv /config/decisions.jsonl /config/decisions.$(date +%Y%m).jsonl
touch /config/decisions.jsonl
```

Future work may add automatic rotation as part of Phase 3 observability.

## 6. Filesystem layout

Mintarr's persistent filesystem (mounted at `/config`):

```
/config/
├── mintarr_state.db              ← state_db (SQLite)
├── mintarr_state.db-wal          ← SQLite WAL file (transient)
├── mintarr_state.db-shm          ← SQLite shared-memory (transient)
├── decisions.jsonl               ← append-only audit log
├── blocked_decisions/
│   └── <jid>.json                ← sidecars for BLOCK terminals
├── discarded/
│   └── <jid>.json                ← sidecars after operator discard
├── expired_review/
│   └── <jid>.json                ← sidecars after REVIEW_REQUIRED expiry
├── backups/
│   └── state.YYYYMMDD.db         ← state_db backups
└── tidal_dl_ng/
    ├── settings.json             ← TIDAL adapter config
    └── token.json                ← TIDAL OAuth token (sensitive)
```

Active records live in `OUTPUT_BASE/<jid>/verification.json`, not under `/config`. Once a record reaches a terminal lifecycle state, its sidecar moves to one of the `/config/<state>/` subdirectories.

## 7. Backup and restore

### 7.1 What to back up

Operators should back up `/config` in full:

- `mintarr_state.db` (and WAL/SHM)
- `decisions.jsonl`
- All sidecar subdirectories
- TIDAL config (if applicable)

### 7.2 What to skip

- `mintarr_state.db-wal` and `-shm` can be skipped if the container is stopped during backup; they exist only when transactions are in flight.
- `OUTPUT_BASE/<jid>/` directories contain audio files that are typically much larger than the records and may be regenerable from source.

### 7.3 Restore procedure

1. Stop the container
2. Replace `/config` contents from backup
3. Start the container
4. On boot, state_db migrations run (additive-only — backups from older Mintarr versions are upgraded in place)
5. If state_db is corrupted, delete it and run `python3 -m app.backfill_state` to rebuild from sidecars

Full procedure in [`BACKUP_RESTORE.md`](../operations/BACKUP_RESTORE.md).

## 8. Invariants

These hold for Mintarr's data model and are tested:

1. **One sidecar per jid in exactly one location.** A jid does not have sidecars in two of `OUTPUT_BASE/<jid>/`, `blocked_decisions/`, `discarded/`, `expired_review/` simultaneously.
2. **state_db is rebuildable from sidecars.** `backfill_state` produces the same state_db as live operation would.
3. **decisions.jsonl is monotonic.** New entries are appended; existing entries are not modified.
4. **Schema migrations are additive within a major version.** Drops, renames, type changes are major-version-bumps.
5. **No PII in state_db.** API keys, tokens, credentials never appear in column values.
6. **jids are 12-character hex.** Generated by `uuid.uuid4().hex[:12]`. Collisions are extremely unlikely.

Mintarr's tests check most of these. The remainder are enforced by code review.

## 9. Future direction

- **Connector config table** (F4.3): `connector_config(id, enabled, mode, config_json, updated_at)` for per-connector enable/disable + dry-run state
- **Multi-instance backup format** — when operators migrate between containers, a standard backup/restore format that includes sidecars + state_db
- **Sidecar compression** — sidecars are mostly text; gzip cuts disk by 70%. May be worth automating when sidecar count exceeds 10,000.

Tracked in [ROADMAP.md](../strategy/ROADMAP.md).

---

> Last updated: 2026-05-26
