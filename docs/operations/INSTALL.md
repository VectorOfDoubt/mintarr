# Install

> **Type:** Operations / getting started
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Updates as install paths evolve.
> **Audience:** Operators installing Mintarr for the first time.

---

## 1. Before you start

Mintarr is a companion application to Lidarr. You need:

- A working Lidarr instance (3.1.x supported; 4.x planned)
- Docker Engine 24+ with Docker Compose v2
- A FLAC Detective service reachable from Mintarr (Mintarr depends on this for spectral verification)
- Optional: TIDAL subscription if you intend to use the TIDAL source adapter
- Optional: an existing music library Lidarr is configured to manage

If you do not have FLAC Detective running, install it before continuing — Mintarr's import-mode source connectors require it. See the FLAC Detective project documentation for setup.

## 2. Quick start (Docker Compose)

Copy [`docker-compose.example.yml`](https://github.com/eivindsjursen-lab/mintarr/blob/main/docker-compose.example.yml) and customise the placeholders.

### 2.1 Minimum config

```yaml
services:
  mintarr:
    image: ghcr.io/eivindsjursen-lab/mintarr:latest
    container_name: mintarr
    restart: unless-stopped
    environment:
      - MINTARR_API_KEY=<generate-with-openssl-rand-base64-32>
      - LIDARR_API_URL=http://host.docker.internal:8686/api/v1
      - LIDARR_CONFIG_XML=/lidarr-config/config.xml
      - FLAC_API_URL=http://host.docker.internal:8889/analyze
      - BASE_URL=http://host.docker.internal:5025
    ports:
      - "127.0.0.1:5025:8000"
    volumes:
      - ./config:/config
      - /path/to/lidarr/config:/lidarr-config:ro
      - /path/to/downloads:/downloads
      - /path/to/complete:/output
```

### 2.2 Generate the API key

```bash
openssl rand -base64 32
```

Paste the result as the `MINTARR_API_KEY` value. Mintarr will not boot with a key shorter than 16 characters.

### 2.3 Start the container

```bash
docker compose up -d mintarr
docker logs mintarr --tail 50
```

You should see:

```
[INFO] Starting gunicorn 26.0.0
[INFO] Listening at: http://0.0.0.0:8000
[INFO] state_db initialized at /config/mintarr_state.db
[INFO] registered worker executor for type=tidal_grab
[INFO] worker thread started
```

### 2.4 Verify health

```bash
curl http://127.0.0.1:5025/health
```

Expected: `{"status":"ok","active_jobs":0}`

If you get `degraded`, check the logs for the unhealthy dependency.

## 3. Connect Mintarr to Lidarr

Two integrations: Newznab indexer and SAB-compatible download client.

### 3.1 Add Mintarr as a Newznab indexer

In Lidarr:

1. Settings → Indexers → Add
2. Choose Newznab
3. Configure:
   - **Name:** `Mintarr`
   - **URL:** `http://mintarr:8000` (or whatever Lidarr can reach Mintarr at)
   - **API Key:** the value of `MINTARR_API_KEY`
   - **Categories:** 3040 (Lossless), 3010 (MP3) — match what your sources provide
   - **Anime Categories:** (leave empty)
4. Click Test — should succeed
5. Save

### 3.2 Add Mintarr as a SAB download client

In Lidarr:

1. Settings → Download Clients → Add
2. Choose SABnzbd
3. Configure:
   - **Name:** `Mintarr`
   - **Host:** the same host Lidarr uses for the indexer
   - **Port:** `8000` (or whichever you exposed)
   - **URL Base:** leave empty
   - **API Key:** the value of `MINTARR_API_KEY`
   - **Category:** `music`
4. Click Test — should succeed
5. Save

Mintarr is now in Lidarr's pipeline. The next time Lidarr searches for an album, it will query Mintarr, and any grabs Mintarr returns will flow back through it.

## 4. Configure source adapters

### 4.1 TIDAL

The TIDAL source adapter requires an OAuth token. Mintarr does not implement the OAuth flow itself; you log in via the `tidal-dl-ng` CLI tool, and Mintarr reuses the token.

Inside the TIDAL config volume:

```bash
docker exec -it mintarr tidal-dl-ng login
```

Follow the prompts. A web browser link is shown — open it, authenticate to TIDAL, copy the code back to the terminal. Mintarr's `token.json` is written to `<TIDAL_DL_NG_CONFIG>/token.json`.

Mintarr's container patches the pinned `tidal-dl-ng` build to use PKCE OAuth for stored-token loads and new logins. Keep `TIDAL_OAUTH_PKCE=1` unless you are deliberately debugging TIDAL client behaviour; non-PKCE sessions can be downgraded to AAC/HIGH even when `HI_RES_LOSSLESS` is configured.

Verify:

```bash
docker exec mintarr python3 -c "
from adapters.tidal import TidalAdapter
print('TIDAL enabled:', TidalAdapter().is_enabled())
"
```

Expected: `TIDAL enabled: True`.

Tokens last about 30 days. When yours expires, re-run `tidal-dl-ng login`.

### 4.2 LocalFolder

The LocalFolder source adapter copies files from a mounted directory.

In `docker-compose.yml`, add:

```yaml
environment:
  - LOCAL_INGEST_PATH=/local-ingest
volumes:
  - /path/to/your/ingest:/local-ingest
```

Restart the container. Drop `Artist/Album/` directories into the ingest path, then trigger an ingest:

```bash
curl -X POST http://127.0.0.1:5025/local/ingest \
    -H "X-Api-Key: $MINTARR_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"path":"Artist/Album"}'
```

### 4.3 Soulseek

The Soulseek source adapter supports both completed-folder ingest and
slskd-backed Lidarr search/grab. In both modes, slskd writes to its normal
completed-download root and Mintarr copies files into its own work area before
verification.

In `docker-compose.yml`, add:

```yaml
environment:
  - SOULSEEK_ENABLED=true
  - SOULSEEK_DOWNLOAD_ROOT=/soulseek-ingest
  # Optional: expose Soulseek results to Lidarr through Mintarr Newznab.
  - SOULSEEK_SEARCH_ENABLED=true
  - SLSKD_API_URL=http://host.docker.internal:5030
  - SLSKD_API_KEY=<slskd-api-key>
  - SOULSEEK_SEARCH_SUFFIX=
  - SOULSEEK_CANDIDATE_CACHE=/config/soulseek_candidates.json
volumes:
  - /path/to/slskd/completed:/soulseek-ingest
```

Restart the container, then put the connector in import mode through the dashboard
or API:

```bash
curl -X POST http://127.0.0.1:5025/dashboard/v1/connectors/soulseek/config \
    -H "X-Api-Key: $MINTARR_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"mode":"import"}'
```

Trigger an ingest with a relative path under `/soulseek-ingest`:

```bash
curl -X POST http://127.0.0.1:5025/soulseek/ingest \
    -H "X-Api-Key: $MINTARR_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"path":"Artist/Album"}'
```

Mintarr rejects absolute paths, path traversal, symlinks, partial download markers,
folders without `.flac` or `.m4a` files, and folders that change during the settle
window.

When `SOULSEEK_SEARCH_ENABLED=true`, Lidarr searches Mintarr as usual. Mintarr
adds `[Soulseek]` Newznab candidates from slskd. If Lidarr grabs one, the existing
SAB-compatible Mintarr download client receives the grab, queues the selected
files in slskd, waits for them under `/soulseek-ingest`, then runs normal Mintarr
QC/import.

## 5. Configure Custom Formats in Lidarr

This is optional but recommended. Custom Formats let Lidarr prefer Mintarr-tagged releases.

In Lidarr:

1. Settings → Profiles → Custom Formats → Add
2. **Name:** `Mintarr-tidal`
3. Add a condition:
   - Type: Release Group / Release Title
   - Pattern (regex): `\[TIDAL\]`
4. Save with a score of `+50`
5. Repeat for:
   - `Mintarr-local`: regex `\[Local\]`, score `+20`
   - `Mintarr-soulseek`: regex `\[Soulseek\]`, score `-10`

Then in your Quality Profile, attach the custom formats with the same scores.

Effect: TIDAL grabs win over LocalFolder when both are available; LocalFolder beats unscored alternatives.

## 6. Verify end-to-end

Trigger a small grab via Lidarr search:

1. In Lidarr, search for an album you don't have (Settings → Profiles → Albums or via the artist page)
2. Watch Mintarr's logs: `docker logs mintarr --follow`
3. You should see Newznab search, addurl, pipeline phases, import attempt
4. Check the Mintarr dashboard at `http://127.0.0.1:5025/dashboard` — the record should appear

If the import succeeded, the album lands in Lidarr's library. If V2 BLOCKed it (e.g., the source returned AAC), Mintarr's dashboard explains why.

## 7. What's next

- [CONFIGURATION.md](CONFIGURATION.md) — every environment variable + connector setting
- [BACKUP_RESTORE.md](BACKUP_RESTORE.md) — back up `/config` periodically
- `TROUBLESHOOTING.md` — common issues (planned for v0.2.0)
- `OBSERVABILITY.md` — structured logging, Prometheus metrics (planned for v0.2.0)

If you hit something this document doesn't cover, that's a documentation bug — open an issue.

---

> Last updated: 2026-06-01
