# Configuration

> **Type:** Operations / reference
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Updated as new connectors and env vars are added.
> **Audience:** Operators tuning a Mintarr installation. Anyone debugging "why does Mintarr behave this way".

---

## 1. How configuration works

Mintarr v1 is configured through:

| Mechanism | Use case | Survives container restart |
|---|---|---|
| Environment variables | Boot-time settings, secrets, mounts | Yes |
| Docker volume mounts | Filesystem paths | Yes |
| Lidarr's `config.xml` (mounted read-only) | Lidarr API key extraction | Yes |
| `connector_config` SQLite table | Per-connector enable/disable + dry-run | Yes |

Mintarr does not load `.env` files at runtime — Docker Compose handles that. Mintarr does not read YAML/TOML config files in v1.

## 2. Core environment variables

### 2.1 Required

| Variable | Purpose | Constraint |
|---|---|---|
| `MINTARR_API_KEY` | Authentication for all Mintarr endpoints | At least 16 characters; recommended 32+ |

Mintarr refuses to boot if the API key is missing or too short. Pre-cutover builds also accept the legacy `TIDALHIRES_API_KEY`; after public cutover, examples and docs should use `MINTARR_API_KEY`.

### 2.2 Lidarr integration

| Variable | Purpose | Default |
|---|---|---|
| `LIDARR_API_URL` | Base URL of Lidarr's v1 API | `http://host.docker.internal:8686/api/v1` |
| `LIDARR_API_KEY` | Lidarr API key (alternative to extracting from config.xml) | Empty (use `LIDARR_CONFIG_XML` instead) |
| `LIDARR_CONFIG_XML` | Path inside container to mounted Lidarr `config.xml` | Empty |

One of `LIDARR_API_KEY` or `LIDARR_CONFIG_XML` must be set. The XML extraction is preferred (avoids putting the key in Mintarr's env vars).

### 2.3 Storage paths

| Variable | Purpose | Default |
|---|---|---|
| `DOWNLOAD_BASE` | Work area for active downloads | `/downloads` |
| `OUTPUT_BASE` | Output area for verified files (Lidarr reads from here) | `/output` |

Both should be mounted volumes. `DOWNLOAD_BASE` is transient (safe to wipe between container restarts); `OUTPUT_BASE` holds files actively being processed.

### 2.4 External services

| Variable | Purpose | Default |
|---|---|---|
| `FLAC_API_URL` | FLAC Detective HTTP endpoint | `http://host.docker.internal:8889/analyze` |
| `BASE_URL` | Public base URL Mintarr advertises in NZB download URLs | Derived from request `Host` header |

Set `BASE_URL` if Mintarr is behind a reverse proxy or if Lidarr reaches Mintarr at a different address than the request's `Host` header reports.

### 2.5 Verification policy

| Variable | Purpose | Default |
|---|---|---|
| `V2_VERIFICATION_ENABLED` | Master toggle for V2 policy. Disabling falls back to a simpler decision logic. | `true` |
| `REVIEW_RETENTION_DAYS` | Days a REVIEW_REQUIRED record is held before auto-expiry | `30` |
| `MINTARR_RESCUE_RESCAN_ENABLED` | Allow Mintarr to trigger Lidarr `RescanFolder` as a fallback import path | `true` in current runtime; target public default `false` |

Pre-cutover builds also accept legacy `TIDALHIRES_RESCUE_RESCAN_ENABLED`. The target public default is `false` because rescue rescans can be disruptive in some Lidarr setups; the current private runtime still defaults to `true` for backward compatibility. Enable only if you understand the implications.

## 3. Source adapter configuration

Each source adapter has its own environment variables. Adapters are dormant (return `is_enabled()=False`) until their required env vars are set and validation passes.

### 3.1 TIDAL

| Variable | Purpose | Default |
|---|---|---|
| `TIDAL_DL_NG_CONFIG` | Directory containing tidal-dl-ng's `settings.json` and `token.json` | `/root/.config/tidal_dl_ng-dev` |
| `TIDAL_OAUTH_PKCE` | Load the stored TIDAL OAuth token as a PKCE session. Keep enabled for LOSSLESS/HI_RES delivery. | `1` |

Mount the configured directory as a volume; populate `token.json` via `tidal-dl-ng login` (see [INSTALL.md](INSTALL.md) §4.1). The adapter is enabled when `token.json` exists.

### 3.2 LocalFolder

| Variable | Purpose | Default |
|---|---|---|
| `LOCAL_INGEST_PATH` | Directory Mintarr scans for `Artist/Album/` candidates | `/local-ingest` |

Mount the configured path as a bind mount. The adapter is enabled when the directory exists. Source files are never modified — Mintarr copies into its work area.

### 3.3 Soulseek

| Variable | Purpose | Default |
|---|---|---|
| `SOULSEEK_ENABLED` | Master toggle for the Soulseek adapter | `false` |
| `SOULSEEK_DOWNLOAD_ROOT` | Mounted slskd completed-download root | unset |
| `SOULSEEK_MAX_FILES` | Maximum files per candidate folder | `300` |
| `SOULSEEK_MAX_BYTES` | Maximum total bytes per candidate (0 = unlimited) | `0` |
| `SOULSEEK_SETTLE_SECONDS` | File-size stable window for completed-folder check | `10` |
| `SOULSEEK_SEARCH_ENABLED` | Enable slskd-backed Newznab search candidates | `false` |
| `SLSKD_API_URL` | slskd HTTP base URL, e.g. `http://host.docker.internal:5030` | unset |
| `SLSKD_API_KEY` | slskd API key for `X-API-Key` authentication | unset |
| `SOULSEEK_SEARCH_TIMEOUT` | slskd search timeout in seconds; minimum 5 | `8` |
| `SOULSEEK_SEARCH_RESPONSE_LIMIT` | Max folder candidates returned per search | `5` |
| `SOULSEEK_SEARCH_FILE_LIMIT` | Max raw file hits accepted per slskd search | `500` |
| `SOULSEEK_MIN_TRACKS` | Minimum audio files required per folder candidate | `2` |
| `SOULSEEK_DOWNLOAD_TIMEOUT` | Max seconds to wait for queued slskd downloads | `3600` |
| `SOULSEEK_POLL_SECONDS` | Poll interval while waiting for slskd files | `5` |

The Soulseek adapter ingests already-completed folders under `SOULSEEK_DOWNLOAD_ROOT`.
It is enabled only when `SOULSEEK_ENABLED=true`, the root exists, and the `soulseek`
connector is in `import` mode. Manual ingest uses `POST /soulseek/ingest` with a
relative path under the mounted root. slskd-backed Lidarr search/grab additionally
requires `SOULSEEK_SEARCH_ENABLED=true`, `SLSKD_API_URL`, and `SLSKD_API_KEY`.

## 4. Volume mounts

Mintarr expects these volume mounts:

| Container path | Purpose | Mount type |
|---|---|---|
| `/config` | Persistent state (state_db, sidecars, decisions.jsonl, backups) | Bind |
| `/downloads` | Work area for active downloads | Bind |
| `/output` | Verified files awaiting Lidarr import | Bind |
| `/root/.config/tidal_dl_ng-dev` | TIDAL adapter config (token.json) | Bind |
| `/local-ingest` | LocalFolder source files | Bind |
| `/soulseek-ingest` | Soulseek completed-download root | Bind |
| `/lidarr-config` | Lidarr config (read-only, for API key extraction) | Bind, read-only |
| `/music` | Lidarr music library (only required if `MINTARR_RESCUE_RESCAN_ENABLED=true`) | Bind |

Tips:

- `/config` should be on a reliable filesystem with backups
- `/downloads` can be tmpfs if you have plenty of RAM (downloads are transient)
- `/output` and `/music` should be the same filesystem (Mintarr uses move-semantics across the boundary, and cross-filesystem moves degrade to copy+delete)

## 5. Networking

### 5.1 Port mapping

Mintarr listens on port 8000 inside the container. The recommended mapping:

```yaml
ports:
  - "127.0.0.1:5025:8000"
```

Binding to `127.0.0.1` means only localhost can reach Mintarr directly. Use a reverse proxy for network access.

### 5.2 Reverse proxy

Mintarr works behind any reverse proxy (Caddy, NGINX, Traefik). Recommended setup:

- Terminate TLS at the proxy
- Set `X-Forwarded-For` for client IP logging
- Forward `Remote-User` if you want SSO audit attribution (see §6.2)
- No special configuration needed for path rewriting (Mintarr does not require a path prefix)

Example Caddy config snippet:

```caddy
mintarr.example.com {
    reverse_proxy 127.0.0.1:5025
    # If using Authelia / Authentik in front
    # forward_auth ...
}
```

## 6. Authentication

### 6.1 API key

The `MINTARR_API_KEY` is shared by all clients (Lidarr, scripts, your browser). There is no per-user separation in v1.

To rotate:

1. Stop the container
2. Update `MINTARR_API_KEY` to the new value
3. Restart the container
4. Update Lidarr's indexer and download-client API key settings

### 6.2 Reverse-proxy SSO

Reverse-proxy SSO attribution is planned, not implemented in the current runtime. Target configuration:

```yaml
environment:
  - MINTARR_REMOTE_USER_HEADER=Remote-User  # default
  - MINTARR_REMOTE_USER_TRUSTED=true        # default false
```

When implemented, `MINTARR_REMOTE_USER_TRUSTED=true` will log the user identifier in the actions table. **Setting this without a real auth-proxy in front is unsafe** — anyone who can send the header can claim any identity.

## 7. Logging

Mintarr logs to stdout (Docker captures via `docker logs`). Phase 3 work introduces structured JSON logging and Prometheus metrics. v1 logs use Python's logging module with format:

```
2026-05-26 14:00:00,000 INFO message here
```

### 7.1 Log levels

| Variable | Purpose | Default |
|---|---|---|
| `MINTARR_LOG_LEVEL` | Root log level | `INFO` |
| `GUNICORN_ACCESS_LOG` | Access log destination | `-` (stdout) |

Set to `DEBUG` only when debugging — debug logs are verbose. Set to `WARNING` to quiet down a chatty container.

### 7.2 Secrets redaction

Mintarr redacts these query parameters and form fields from access logs:

- `apikey`, `api_key`, `x-api-key`, `x_api_key`
- `password`, `token`

This is automatic and tested. If you find a log line with an unredacted secret, that's a security bug — see [SECURITY.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/SECURITY.md).

## 8. Custom Format scoring (Lidarr-side, recommended)

Mintarr does not configure Lidarr's Custom Formats. The recommended configuration:

| Mintarr source | CF name | Pattern | Suggested score |
|---|---|---|---|
| TIDAL | `Mintarr-tidal` | `\[TIDAL\]` | +50 |
| LocalFolder | `Mintarr-local` | `\[Local\]` | +20 |
| Soulseek | `Mintarr-soulseek` | `\[Soulseek\]` | -10 |

Effect: TIDAL beats LocalFolder when both are available; LocalFolder beats unscored alternatives; Soulseek is penalised relative to other sources.

These are starting recommendations. Operators may adjust based on their experience.

## 9. Worker behaviour

| Variable | Purpose | Default |
|---|---|---|
| `MINTARR_DISABLE_WORKER` | Skip starting the worker thread (used by tests) | `false` |

Pre-cutover builds also accept legacy `TIDALHIRES_DISABLE_WORKER`.

In production, leave the worker enabled. Mintarr's worker is N=1 (single thread) by design (full F2 worker-queue design doc planned for v0.2.0 migration).

## 10. Timezone

| Variable | Purpose | Default |
|---|---|---|
| `TZ` | Container timezone for log timestamps | UTC |

Set to your local timezone for human-readable logs (e.g., `Europe/Oslo`).

## 11. Reference defaults

Here is the full default-values table for quick reference:

```bash
# Authentication
MINTARR_API_KEY=               # REQUIRED — no default

# Lidarr
LIDARR_API_URL=http://host.docker.internal:8686/api/v1
LIDARR_API_KEY=                # use one of LIDARR_API_KEY or LIDARR_CONFIG_XML
LIDARR_CONFIG_XML=             # use one of LIDARR_API_KEY or LIDARR_CONFIG_XML

# Storage
DOWNLOAD_BASE=/downloads
OUTPUT_BASE=/output

# External services
FLAC_API_URL=http://host.docker.internal:8889/analyze
BASE_URL=                       # derived from request Host header

# Verification
V2_VERIFICATION_ENABLED=true
REVIEW_RETENTION_DAYS=30
MINTARR_RESCUE_RESCAN_ENABLED=false

# TIDAL adapter
TIDAL_DL_NG_CONFIG=/root/.config/tidal_dl_ng-dev

# LocalFolder adapter
LOCAL_INGEST_PATH=/local-ingest

# Soulseek adapter
SOULSEEK_ENABLED=false
SOULSEEK_DOWNLOAD_ROOT=
SOULSEEK_MAX_FILES=300
SOULSEEK_MAX_BYTES=0
SOULSEEK_SETTLE_SECONDS=10
SOULSEEK_SEARCH_ENABLED=false
SLSKD_API_URL=
SLSKD_API_KEY=
SOULSEEK_SEARCH_TIMEOUT=8
SOULSEEK_SEARCH_RESPONSE_LIMIT=5
SOULSEEK_SEARCH_FILE_LIMIT=500
SOULSEEK_MIN_TRACKS=2
SOULSEEK_DOWNLOAD_TIMEOUT=3600
SOULSEEK_POLL_SECONDS=5

# Auth / SSO
MINTARR_REMOTE_USER_HEADER=Remote-User
MINTARR_REMOTE_USER_TRUSTED=false

# Logging
MINTARR_LOG_LEVEL=INFO
GUNICORN_ACCESS_LOG=-

# Worker
MINTARR_DISABLE_WORKER=false

# Timezone
TZ=UTC
```

---

> Last updated: 2026-06-01
