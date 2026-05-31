# Upgrade Guide

> **Type:** Operations / migration
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Each Mintarr release adds a section here.
> **Audience:** Operators upgrading an existing Mintarr installation.

---

## 1. Upgrade philosophy

Mintarr follows Semantic Versioning ([ADR-0004](../architecture/adr/0004-api-versioning-semver.md)):

- **Patch versions (`1.0.0` → `1.0.1`)** — bug fixes only. Drop-in replacements; no operator action required.
- **Minor versions (`1.0.0` → `1.1.0`)** — new features, additive schema migrations. Drop-in replacements; new features may require new config to use.
- **Major versions (`1.0.0` → `2.0.0`)** — breaking changes. Operator action required; specific steps documented per release.

For all upgrades, **back up `/config` before starting** ([BACKUP_RESTORE.md](BACKUP_RESTORE.md)).

## 2. General upgrade procedure

For minor and patch versions, the procedure is:

```bash
# 1. Back up
docker exec mintarr sqlite3 /config/mintarr_state.db ".backup /config/backups/state.pre-upgrade.db"
tar czf mintarr-config-backup-$(date +%Y%m%d).tar.gz /path/to/your/config

# 2. Pull the new image
docker compose pull mintarr

# 3. Recreate the container
docker compose up -d mintarr

# 4. Verify health
curl http://127.0.0.1:5025/health
docker logs mintarr --tail 100

# 5. Run a sanity-check grab through Lidarr
```

The state_db migration runs automatically on container boot. If it fails, the container logs the error; the database is left in its pre-migration state.

For major versions, follow the per-release section below in addition to the general procedure.

## 3. Lidarr version coordination

Mintarr's Lidarr coupling depends on Lidarr's API surface. Coordination notes:

- **Mintarr supports Lidarr 3.1.x.** Upgrades within 3.1.x are safe for Mintarr.
- **Lidarr 4.x is not yet supported.** Mintarr 1.x does not have a v4 client; running Mintarr 1.x against Lidarr 4 will fail at the import phase (other phases proceed normally).
- **When Mintarr ships v4 client support**, the release notes will name the minimum Mintarr version required.

For now: do not upgrade Lidarr to v4 until Mintarr has shipped v4 support, unless you accept that Mintarr's import phase will fail until you upgrade Mintarr.

## 4. Migration from `tidalhires` to Mintarr

This is the one-time rename ([ADR-0001](../architecture/adr/0001-rename-from-tidalhires.md)). Required for anyone who ran the project under its previous name.

### 4.1 Pre-upgrade state

- Container `tidalhires:local`
- Database file `tidalhires_state.db`
- Some env vars prefixed `TIDALHIRES_*`
- Config volume mounted to `/config` (this is unchanged)

### 4.2 Upgrade steps

1. **Stop the old container.**
   ```bash
   docker compose stop tidalhires
   ```

2. **Back up state.**
   ```bash
   cp /path/to/config/tidalhires_state.db /path/to/config/backups/tidalhires_state.pre-mintarr.db
   ```

3. **Rename the database file.** Mintarr looks for `mintarr_state.db`; the old file must be renamed.
   ```bash
   mv /path/to/config/tidalhires_state.db /path/to/config/mintarr_state.db
   ```

4. **Update `docker-compose.yml`:**
   - Service name: `tidalhires` → `mintarr`
   - `container_name: tidalhires` → `container_name: mintarr`
   - `image: tidalhires:local` → `image: ghcr.io/eivindsjursen-lab/mintarr:<version>`
   - Env vars: rename `TIDALHIRES_API_KEY` → `MINTARR_API_KEY`, `TIDALHIRES_RESCUE_RESCAN_ENABLED` → `MINTARR_RESCUE_RESCAN_ENABLED`, etc.

5. **Update Lidarr config:**
   - Settings → Indexers → edit the `tidalhires` indexer
     - Rename to `Mintarr`
     - Update URL if the container hostname changed
   - Settings → Download Clients → edit the `tidalhires` SAB client
     - Same updates

6. **Start the new container.**
   ```bash
   docker compose up -d mintarr
   docker logs mintarr --tail 50
   ```

7. **Verify:**
   - `curl http://127.0.0.1:5025/health` returns `ok`
   - Mintarr dashboard at `http://127.0.0.1:5025/dashboard` shows your existing records
   - Lidarr test connection for the renamed indexer and download client both succeed

### 4.3 What you keep

- All existing record sidecars and decisions.jsonl
- All existing state_db data (after rename)
- TIDAL OAuth token (unchanged path; rename `TIDAL_DL_NG_CONFIG` if you choose)
- LocalFolder ingest history (state_db is preserved)

### 4.4 What changes

- Service identity (name, container, image)
- API key environment variable name
- Newznab/SAB integration URL if Lidarr reaches the container at a different name

### 4.5 Rollback

If something goes wrong:

```bash
docker compose down mintarr
mv /path/to/config/mintarr_state.db /path/to/config/tidalhires_state.db
# revert docker-compose.yml changes
docker compose up -d tidalhires
```

## 5. Future per-release upgrade sections

When Mintarr releases happen, this section grows with one subsection per release. Each subsection documents what changed and what operator action is needed.

### 5.1 v1.0.0 (pending — Phase 0 cutover)

The first Mintarr release. Operators coming from `tidalhires`: see §4 above.

Operators installing fresh: see [INSTALL.md](INSTALL.md).

## 6. Schema migrations

Mintarr's state_db schema is migrated via additive `ALTER TABLE` operations at container boot ([DATA_MODEL.md §4](../architecture/DATA_MODEL.md#4-migrations)). All migrations are idempotent — running them twice does nothing.

Operators do not need to run migrations manually. They are part of the boot sequence.

If you need to rebuild state_db from sidecars (corruption, manual investigation), use:

```bash
docker exec mintarr python3 -m app.backfill_state
```

This regenerates state_db from the sidecars on disk. The pre-existing state_db is overwritten — back up first.

## 7. Connector breaking changes

The `SourceAdapter` and `ConnectorManifest` Protocols are SemVer-versioned ([ADAPTER_PROTOCOL_v1.md](../specs/ADAPTER_PROTOCOL_v1.md), [CONNECTOR_MANIFEST_v1.md](../specs/CONNECTOR_MANIFEST_v1.md)). Community-contributed adapters declare which version they target.

When Mintarr ships a new major version that breaks the Adapter Protocol:

- The old `_v1.md` spec is preserved
- The new `_v2.md` spec is added
- Mintarr's runtime supports both v1 and v2 adapters in the same container during a transition period
- Release notes document the transition timeline and a migration script if applicable

This means: community adapters do not break on Mintarr upgrades unless the operator explicitly migrates to a Mintarr version that drops v1 support.

## 8. Downgrading

Mintarr does not ship a downgrade command. Schema migrations are additive only, but downgrades may want columns the older version cannot read.

If you need to downgrade:

1. Stop the container
2. Restore `/config` from a backup taken before the upgrade
3. Pin to the older Mintarr image tag
4. Start the container

Downgrades across major versions may not work even with backup restore — major versions can change sidecar format incompatibly. Pin to the immediately-prior version if possible.

## 9. Compatibility matrix

| Mintarr | Lidarr | flac-detective | tidal-dl-ng-For-DJ |
|---|---|---|---|
| 1.0.x | 3.1.x | ≥0.6.0 | pinned to project's commit |

Mintarr declares minimum compatibility for external services via the `min_supported_version` field in connector manifests ([CONNECTOR_MANIFEST_v1.md §4](../specs/CONNECTOR_MANIFEST_v1.md)). Dashboard surfaces incompatibilities.

---

> Last updated: 2026-05-26
