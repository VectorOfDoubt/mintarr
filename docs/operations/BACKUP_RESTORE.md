# Backup and Restore

> **Type:** Operations / data protection
> **Version:** 1.1 — 2026-06-05
> **Status:** Living document. Updates as backup tooling evolves.
> **Audience:** Operators establishing a backup routine. Anyone recovering from data loss.

---

## 1. What to back up

Mintarr's stateful data lives at `/config` inside the container. Everything else is rebuildable.

| Item | Importance | Why |
|---|---|---|
| `/config/mintarr_state.db` | High | Query index over sidecars. Rebuildable from sidecars, but rebuilding is slow at scale. Pre-cutover private builds may still use `/config/tidalhires_state.db` unless `MINTARR_STATE_DB` is set. |
| `/config/decisions.jsonl` | High | Append-only audit log. Not rebuildable. |
| `/config/blocked_decisions/`, `/config/discarded/`, `/config/expired_review/` | High | Terminal-state sidecars. Hold full record evidence. |
| `/config/tidal_dl_ng/token.json` | Medium | TIDAL OAuth token. Regenerable via re-login. |
| `/config/backups/` | Low | Older backups; back up if you want history-of-backups. |
| Lidarr config and library | High | NOT Mintarr's concern but back this up separately. |
| Active `OUTPUT_BASE/<jid>/` | Medium | Audio files awaiting Lidarr import. Regenerable from source. |

The minimum-viable backup is the active state DB (`/config/mintarr_state.db` after cutover, or legacy `/config/tidalhires_state.db`) plus the three sidecar directories plus `decisions.jsonl`. Operators who can afford the disk can back up `/config` in full.

## 2. Backup procedure

### 2.1 Live backup (container running)

Mintarr writes sidecars atomically (temp + rename) so reading them while Mintarr is running is safe. state_db requires a SQLite-aware backup:

```bash
# Live backup — Mintarr keeps running
docker exec mintarr sqlite3 /config/mintarr_state.db \
    ".backup /config/backups/state.$(date +%Y%m%d-%H%M%S).db"

# Copy sidecars and audit log
docker exec mintarr tar czf /config/backups/sidecars-$(date +%Y%m%d).tar.gz \
    /config/blocked_decisions \
    /config/discarded \
    /config/expired_review \
    /config/decisions.jsonl
```

The `sqlite3 .backup` command produces a consistent snapshot even if Mintarr is mid-transaction. It is the safe way to back up SQLite while the writer is running.

### 2.2 Cold backup (container stopped)

If Mintarr is stopped, simple file copy works:

```bash
docker compose stop mintarr
cp -r /path/to/config /path/to/backups/config-$(date +%Y%m%d)
docker compose start mintarr
```

The window of unavailability is whatever it takes to copy. For typical install sizes (<1GB), this is seconds.

### 2.3 Scheduled backups

Mintarr can write scheduled backup zips itself. It is disabled by default; enable
it only after choosing a target directory and retention policy:

```bash
MINTARR_BACKUP_SCHEDULE_ENABLED=true
MINTARR_BACKUP_INTERVAL_HOURS=24
MINTARR_BACKUP_DIR=/config/backups
MINTARR_BACKUP_RETENTION=30
```

When enabled, Mintarr writes `mintarr-backup-YYYYMMDD-HHMMSS.zip` files using
the same state bundle as `GET /backup` (§2.4). Files are written atomically
(`.tmp` then rename) and retention prunes only `mintarr-backup-*.zip` files in
`MINTARR_BACKUP_DIR`; other files in that directory are left alone. Set
`MINTARR_BACKUP_RETENTION=0` to disable pruning.

The first scheduled backup runs after the interval elapses. Use `GET /backup`
for an immediate on-demand export.

You can also use a cron job, systemd timer, or existing backup tooling. Example
crontab:

```cron
# Daily Mintarr backup at 03:00
0 3 * * * docker exec mintarr sqlite3 /config/mintarr_state.db ".backup /config/backups/state.$(date +\%Y\%m\%d).db" && find /path/to/config/backups -name 'state.*.db' -mtime +30 -delete
```

The `find -mtime +30 -delete` keeps the last 30 daily backups. Adjust to fit your retention policy.

### 2.4 One-call export endpoint (`GET /backup`)

Phase 3 adds a Mintarr-managed export endpoint that bundles the minimum-viable backup (§1) into a single zip — no `docker exec` and no SQLite tooling on the host required:

```bash
curl -fsS -H "X-Api-Key: $MINTARR_API_KEY" \
    http://127.0.0.1:5025/backup -o mintarr-backup-$(date +%Y%m%d-%H%M%S).zip
```

The endpoint is authenticated and **read-only** — it never mutates state. The zip contains:

| Path in zip | Source |
|---|---|
| `state_db.sqlite` | Consistent snapshot of the active state DB via SQLite's online-backup API (WAL-safe; can run while Mintarr is live) |
| `sidecars/<jid>/verification.json` | Verification sidecars under `OUTPUT_BASE` (sidecars only — **never** audio) |
| `archive/{blocked,discarded,expired}/*.json` | Terminal-state sidecars |
| `logs/decisions.jsonl`, `logs/release_switch_audit.jsonl` | Append-only audit logs |

Audio files in `OUTPUT_BASE/<jid>/` are deliberately excluded — they are regenerable from source and would dominate the archive size. Restore is the manual procedure in §3; an automated restore endpoint is intentionally deferred because it overwrites state.

This export covers the on-demand and "scheduled via your own cron + curl" cases.
The Mintarr-managed scheduler in §2.3 writes the same zip format.

## 3. Restore procedure

### 3.1 Full restore

If `/config` is lost or corrupted:

```bash
# 1. Stop the container
docker compose down mintarr

# 2. Restore from backup
rm -rf /path/to/config
cp -r /path/to/backups/config-YYYYMMDD /path/to/config

# 3. Start the container
docker compose up -d mintarr

# 4. Verify
curl http://127.0.0.1:5025/health
docker logs mintarr --tail 50
```

Mintarr's boot runs additive schema migrations. Backups from older Mintarr versions are upgraded in place during boot.

### 3.2 Selective restore — only state_db

If sidecars are intact but state_db is corrupted:

```bash
# 1. Stop the container
docker compose down mintarr

# 2. Replace state_db with backup
cp /path/to/backups/state.YYYYMMDD.db /path/to/config/mintarr_state.db

# 3. Start the container
docker compose up -d mintarr
```

### 3.3 Selective restore — rebuild state_db from sidecars

If state_db is corrupted AND you don't have a recent backup:

```bash
# 1. Stop the container
docker compose down mintarr

# 2. Remove the broken state_db
rm /path/to/config/mintarr_state.db /path/to/config/mintarr_state.db-wal /path/to/config/mintarr_state.db-shm

# 3. Start the container — Mintarr creates an empty state_db at boot
docker compose up -d mintarr

# 4. Run backfill (rebuilds state_db rows from sidecars on disk)
docker exec mintarr python3 -m app.backfill_state

# 5. Verify
docker exec mintarr sqlite3 /config/mintarr_state.db "SELECT COUNT(*) FROM records"
```

`backfill_state` scans every sidecar in `OUTPUT_BASE/*/verification.json` and `/config/{blocked_decisions, discarded, expired_review}/*.json` and inserts/updates state_db rows. The script is idempotent — running it twice produces the same result.

For libraries with many thousands of records, backfill can take several minutes.

### 3.4 Selective restore — only sidecars

If state_db is intact but sidecars are missing or corrupted:

```bash
# 1. Stop the container
docker compose down mintarr

# 2. Restore sidecar directories
tar xzf /path/to/backups/sidecars-YYYYMMDD.tar.gz -C /

# 3. Start the container
docker compose up -d mintarr
```

state_db is the query index; it still points at the right records. Restored sidecars provide the evidence detail.

## 4. What is NOT restorable

| Item | Recovery path |
|---|---|
| In-flight grabs (jobs `state=running` when backup was taken) | Re-trigger via Lidarr |
| Audio files in `OUTPUT_BASE/<jid>/` that have not yet been imported | Re-fetch from source |
| TIDAL OAuth tokens that have since expired | Re-run `tidal-dl-ng login` |
| Lidarr's library state | Restore Lidarr's own backup |
| Custom Format configuration in Lidarr | Restore Lidarr's own backup |

Mintarr's restore covers Mintarr state only. Lidarr has its own backup mechanism for Lidarr state.

## 5. Disaster recovery — full system rebuild

Scenario: host failure, all local state lost, only off-site backup remains.

```bash
# 1. Restore Lidarr from its own backup first
# (Mintarr depends on Lidarr being functional)

# 2. Provision a new host with Docker Engine
# 3. Restore /path/to/config from off-site backup
# 4. Place docker-compose.yml on the new host with the restored config path
# 5. docker compose up -d mintarr

# 6. Verify
curl http://127.0.0.1:5025/health
curl http://127.0.0.1:5025/dashboard/v1/summary -H "X-Api-Key: $MINTARR_API_KEY"
```

If `/config/tidal_dl_ng/token.json` was not in the backup or has expired, re-run `tidal-dl-ng login`.

## 6. Backup verification

A backup that has never been restored is not a backup; it is a hopeful guess.

Quarterly verification routine:

1. Pick the most recent backup
2. Spin up Mintarr in a test environment with that backup as `/config`
3. Verify the dashboard renders with the expected records
4. Verify state_db queries return expected counts:
   ```sql
   SELECT derived_status, COUNT(*) FROM records GROUP BY derived_status;
   ```
5. Verify a few sample records can be promoted (without actually triggering Lidarr — test container should have a mock Lidarr endpoint)

If any of these fail, the backup is broken — fix the backup procedure.

## 7. Backup size estimates

| Install size | state_db | Sidecars | decisions.jsonl |
|---|---|---|---|
| 100 records | <1 MB | <10 MB | <1 MB |
| 1,000 records | 5-10 MB | 50-100 MB | 5-10 MB |
| 10,000 records | 50-100 MB | 0.5-1 GB | 50-100 MB |
| 100,000 records | 500 MB - 1 GB | 5-10 GB | 0.5-1 GB |

Sidecars are by far the largest component because they hold per-file evidence (per-file spectral analysis from FLAC Detective). state_db only indexes the headline fields.

For operators concerned about size, sidecars compress well (gzip cuts ~70%). Future work may automate this.

## 8. Encryption at rest

Mintarr does not encrypt state at rest. Sidecars and state_db are plaintext.

Operators concerned about disk-level access:

- Mount `/config` on an encrypted filesystem (LUKS, ZFS native encryption, etc.)
- Or use Docker's built-in support for encrypted volumes (where available)

Backup encryption is the operator's responsibility (e.g., `gpg --encrypt`). Mintarr's backup commands produce plaintext outputs.

## 9. Cross-host migration

To move a Mintarr install to a new host:

1. Stop Mintarr on the source host
2. Tar `/config` and the contents of `OUTPUT_BASE` (if you want in-flight records to continue)
3. Copy to the new host
4. On the new host: extract, set up `docker-compose.yml` pointing at the new paths
5. Update Lidarr's indexer / download-client URLs if the new host has a different hostname
6. Start Mintarr on the new host

Mintarr's state is portable across hosts as long as the schema version matches.

---

> Last updated: 2026-06-05
