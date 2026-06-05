# Phase 3 restore endpoint design

> **Type:** Design
> **Status:** Proposed
> **Owner:** Mintarr maintainers
> **Related:** [Backup and restore](../operations/BACKUP_RESTORE.md), [Data model](../architecture/DATA_MODEL.md)

## 1. Problem

Phase 3 already ships:

- `GET /backup` — read-only state export to a zip
- scheduled backup zips — optional, default-off

The missing half is an operator-facing restore path that can restore that zip
without requiring hand-written shell commands. Restore is materially riskier
than export: it overwrites Mintarr state (`state_db`, sidecars, audit logs) and
can corrupt the runtime if it happens while workers or request handlers are
mutating the same files.

## 2. Decision

Implement restore as a **staged restore applied on restart**, not as an in-place
live mutation.

The runtime endpoint stages and validates a restore request, writes a durable
restore marker under `/config`, and returns `202 restart_required`. The actual
file replacement happens at process boot, before:

1. `state_db.init()`
2. adapter/connector registration
3. worker start
4. scheduled backup start

This keeps the running application from replacing SQLite and sidecar files while
it is serving traffic.

## 3. API shape

### 3.1 `POST /restore`

Authenticated. Disabled by default.

Configuration:

| Env | Default | Purpose |
|---|---|---|
| `MINTARR_RESTORE_ENABLED` | `false` | Enables restore staging endpoints |
| `MINTARR_RESTORE_STAGING_DIR` | `/config/restore_staging` | Where uploaded or selected backups are staged |
| `MINTARR_RESTORE_SAFETY_BACKUP_DIR` | `/config/restore_safety` | Where pre-restore state snapshots are written |

Request forms:

1. JSON selected backup path:

   ```json
   { "backup_path": "/config/backups/mintarr-backup-20260605-030000.zip" }
   ```

2. Multipart upload:

   ```http
   POST /restore
   Content-Type: multipart/form-data

   file=@mintarr-backup-20260605-030000.zip
   ```

Response:

```json
{
  "status": true,
  "restore_id": "20260605-061500-ab12cd",
  "state": "staged",
  "restart_required": true,
  "message": "restore staged; restart Mintarr to apply"
}
```

### 3.2 `GET /restore/status`

Authenticated. Returns the current staged/apply status:

```json
{
  "enabled": true,
  "pending": true,
  "restore_id": "20260605-061500-ab12cd",
  "state": "staged",
  "created_at": 1780632900.0,
  "last_apply": null
}
```

### 3.3 `DELETE /restore`

Authenticated. Cancels a staged restore before restart. It removes the marker and
staged zip, but only if the restore has not started applying.

## 4. Restore zip contract

The restore input is exactly the `GET /backup` zip format:

| Zip path | Restore target |
|---|---|
| `state_db.sqlite` | active `state_db` path |
| `sidecars/<jid>/verification.json` | `OUTPUT_BASE/<jid>/verification.json` |
| `archive/blocked/*.json` | `BLOCKED_DECISIONS_DIR` |
| `archive/discarded/*.json` | `DISCARDED_DIR` |
| `archive/expired/*.json` | `EXPIRED_REVIEW_DIR` |
| `logs/decisions.jsonl` | `DECISIONS_LOG` |
| `logs/release_switch_audit.jsonl` | `RELEASE_SWITCH_AUDIT_LOG` |

Audio is never included and never restored.

Unknown zip entries are rejected. Absolute paths, `..`, symlinks, directories
outside the known restore prefixes, and oversized entries are rejected.

## 5. Staging validation

`POST /restore` must validate before writing the restore marker:

1. `MINTARR_RESTORE_ENABLED=true`
2. API key is valid
3. exactly one source is provided (`backup_path` or multipart `file`)
4. selected `backup_path` resolves inside `/config/backups` or
   `MINTARR_BACKUP_DIR` after realpath resolution; symlink escape is rejected
5. zip opens successfully
6. every entry matches the restore contract in §4
7. `state_db.sqlite`, if present, opens as SQLite and passes `PRAGMA integrity_check`
8. JSON sidecars and logs parse enough to detect obvious corruption
9. every `sidecars/<jid>/verification.json` path uses exactly one safe `<jid>`
   segment matching Mintarr's jid shape (`[a-f0-9]{12}`)
10. no extracted payload exceeds configured limits:
    - cap total uncompressed size
    - cap entry count
    - cap per-entry size
11. zip member attributes do not describe symlinks or special files
12. there is no existing pending restore marker

Validation failure returns `400` or `409` and leaves no marker behind.

## 6. Boot-time apply flow

At server import/start, before `state_db.init()`:

1. Read `/config/restore_request.json`.
2. If absent, continue normal boot.
3. If present, acquire a process-local restore lock.
4. Validate staged zip again. The staging-time validation is not trusted.
5. Write a safety backup of current state to
   `MINTARR_RESTORE_SAFETY_BACKUP_DIR/<restore_id>/` using the existing
   read-only backup builder.
   - Safety-backup failure aborts restore before any destructive write.
   - If the normal backup builder fails, a raw-copy fallback may be attempted for
     sidecars/logs plus a SQLite online backup for state_db. If no safety backup
     can be produced, restore does not proceed.
6. Persist `restore_status.state=applying` before the first destructive write.
7. Extract restore payload into a temp directory under the staging directory.
8. Validate extracted realpaths.
9. Atomically replace:
   - state DB file and remove stale `-wal` / `-shm`
   - archive sidecar directories
   - active verification sidecars
   - audit logs
10. Remove the restore marker.
11. Write `/config/restore_status.json` with `state=applied`.
12. Continue boot; `state_db.init()` runs additive migrations on the restored DB.

If apply fails before replacement begins, current state is left untouched and
boot continues with `restore_status.state=failed_preflight`.

If apply fails after replacement begins, boot must fail closed: log the error,
write `restore_status.state=failed_partial`, and do **not** start workers. The
operator can then recover from the safety backup or rerun manual restore.

If the process crashes after `state=applying` is persisted and before `applied`
is written, the next boot treats that as `failed_partial`: it must fail closed
and must **not** retry the staged restore automatically. This makes apply
crash-safe, not merely exception-safe.

In `failed_partial`, the web server may still start enough read-only surface to
serve `/health`, `/restore/status`, `/dashboard`, and static assets, but worker
startup, scheduled backups, ingest endpoints, and Lidarr mutations must remain
disabled until an operator explicitly resolves the restore state.

## 7. Concurrency and process model

The supported deployment is one Mintarr web process with one worker thread. A
live in-process restore endpoint would become unsafe if Gunicorn is ever run with
multiple workers or if a request handler reads a file mid-replacement. Staging
plus restart avoids those races.

The boot-time apply step must run before background worker startup and before
scheduled backup startup. A later multi-process deployment would need an
external file lock around the boot-time apply marker.

## 8. Audit and observability

Restore is an administrative state mutation. It must be auditable:

- `restore_status.json` stores `restore_id`, actor, created time, source name,
  validation result, apply result, and safety-backup path.
- `state_db.actions` cannot be the only audit target because state_db itself is
  being replaced.
- Logs must not include full local paths if they contain private names; log
  restore id and basename where possible.

Future dashboard UI should surface:

- restore enabled/disabled
- staged restore id
- restart-required state
- last apply status
- safety backup path

## 9. Test plan

Unit tests:

- accepts a valid backup zip and creates marker
- rejects disabled restore
- rejects path traversal and unknown zip members
- rejects unsafe `<jid>` path segments
- rejects symlink/special-file zip entries
- rejects total-size, entry-count, and per-entry zip bomb attempts
- rejects invalid SQLite
- rejects second pending marker
- cancel removes pending marker and staged zip
- boot apply replaces state DB and sidecars
- boot apply removes stale WAL/SHM files
- boot apply writes safety backup before replacement
- boot apply aborts if safety backup cannot be written
- failed preflight leaves current state untouched
- failed partial prevents worker startup
- boot with `state=applying` marker fails closed and does not retry

Integration smoke:

1. create a backup from fixture state
2. mutate current state
3. stage restore
4. restart test app
5. verify dashboard/state_db reflects restored state

## 10. Explicit non-goals

- No live in-process restore.
- No Lidarr restore.
- No audio restore.
- No restore from arbitrary host paths outside the configured backup directory.
- No downgrade migration support beyond current additive `state_db.init()`.
- No automatic container restart from inside Mintarr.

## 11. Implementation slices

1. Pure restore planner/validator (`app/restore.py`) + tests.
2. `POST /restore`, `GET /restore/status`, `DELETE /restore` staging endpoints.
3. Boot-time apply before `state_db.init()`; worker must not start on
   `failed_partial`.
4. Dashboard visibility/action controls.
