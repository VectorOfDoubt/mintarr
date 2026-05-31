# HTTP API — v1

> **Type:** Spec / contract
> **Version:** 1.0.2 — runtime-validated 2026-05-31
> **Status:** Locked. Editorial fixes allowed; semantic changes require `HTTP_API_v2.md` per [ADR-0004](../architecture/adr/0004-api-versioning-semver.md).
> **Audience:** Anyone calling Mintarr's HTTP endpoints. External dashboards, alternative front-ends, monitoring scripts, automation tools. Lidarr-facing endpoints (Newznab, SAB compat) follow those external protocols and are documented here for completeness only — they are not Mintarr-versioned.
> **Goal:** Phase 3 work replaces this hand-written spec with an auto-generated OpenAPI document. Until then, this file is authoritative.

---

## 1. Authentication

All Mintarr API endpoints require authentication except `/health`. The `/dashboard` HTML shell and Flask static assets are unauthenticated so a browser can load the app and prompt for the API key; all dashboard JSON/action endpoints still require the key.

Two API-key methods are supported:

| Method | Header / parameter | Use case |
|---|---|---|
| API key in `X-Api-Key` header | `X-Api-Key: <MINTARR_API_KEY>` | Headless clients, scripts |
| API key in `apikey` query param | `?apikey=<MINTARR_API_KEY>` | Browser-driven flows where headers are hard |

Both are compared via `hmac.compare_digest`. Missing or wrong key returns `401 Unauthorized` with body `{"error": "unauthorized"}`.

The API key is set via `MINTARR_API_KEY` after cutover. Current pre-cutover builds also accept the legacy `TIDALHIRES_API_KEY` name. The key must be at least 16 characters. Mintarr refuses to boot with a shorter key.

Form-login is a future dashboard feature, not part of the current v1 API. API key authentication is the only implemented authentication mechanism today.

## 2. Endpoint catalogue

Endpoints group into five categories:

| Category | Path prefix | Purpose |
|---|---|---|
| Lidarr-facing | `/api`, `/newznab/api`, `/sabnzbd/api`, `/download/` | External protocols (Newznab, SAB) — not Mintarr-versioned |
| Health | `/health` | Liveness check |
| Source ingest | `/local/ingest`, `/soulseek/ingest` (planned) | Manual operator triggers per source |
| Dashboard | `/dashboard`, `/dashboard/v1/...` | Web UI and JSON API for it |
| Legacy verification | `/verification`, `/decisions`, `/jobs` | Pre-dashboard V2 inspection/action endpoints retained for compatibility |
| NZB pointer | `/download/<int>.nzb`, `/download/<source>/<id>.nzb` | NZB generation for SAB roundtrip |
| Infrastructure | `/`, `/<path:p>`, `/static/...` | Service index, authenticated catch-all 404, Flask static assets |

The rest of this document covers each in detail.

## 3. Lidarr-facing endpoints (external protocols)

### 3.1 `GET /api` and `GET /newznab/api`

Newznab indexer protocol. See [Newznab API docs](https://newznab.readthedocs.io/) for the protocol. Mintarr-specific behaviour:

| Query | Behaviour |
|---|---|
| `t=caps` | Returns Mintarr's capabilities XML. Static. |
| `t=search` / `t=music` | Aggregates `ReleaseCandidate`s from all `enabled_adapters().search()` calls. Sorts by candidate `priority` descending. Empty query returns a fixed placeholder so Lidarr's indexer-health-check passes. |

Responses are XML matching Lidarr's Newznab parser expectations.

### 3.2 `GET /sabnzbd/api`, `POST /sabnzbd/api`, and `POST /api`

SAB compatibility protocol. Subset of [SABnzbd API](https://sabnzbd.org/wiki/configuration/3.7/api). Mintarr-specific behaviour:

| `mode` | Behaviour |
|---|---|
| `mode=version` | Returns `{"version": "3.7.2"}` — Mintarr declares itself as SAB 3.7.2 for Lidarr compatibility |
| `mode=queue` | Returns current job queue in SAB shape |
| `mode=history` | Returns terminal jobs in SAB shape |
| `mode=addurl` / `mode=addfile` | Parses `name` (or NZB body) for `<source>:<id>` prefix; dispatches to the matching source adapter |
| `mode=delete` | Marks a job hidden from Lidarr |

`POST /api` is a compatibility fallback for clients that POST SAB-compatible `mode=...` requests to the same path used by Newznab GET requests. It has the same auth and mode handling as `/sabnzbd/api`.

The `addurl` dispatcher matches the F3.2/F3.3 routing design (held in private monorepo pending v0.2.0 migration).

### 3.3 `GET /download/<int:album_id>.nzb` (legacy)

Returns a minimal NZB file with `<meta type="name">tidal:<album_id></meta>`. Retained for Lidarr-history compatibility — old grabs reference this URL. Equivalent to `GET /download/tidal/<base64url(album_id)>.nzb`.

### 3.4 `GET /download/<source>/<encoded_source_id>.nzb`

Returns a structured NZB file. Used by Newznab search responses for non-numeric source IDs.

URL format: `<base_url>/download/<source>/<base64url_source_id>.nzb`

Response: NZB XML with `<meta type="tidalhires_source"><source></meta>` and `<meta type="tidalhires_source_id"><source_id></meta>` so the addurl dispatcher can recover both fields when Lidarr POSTs the NZB back.

Validation:

- `<source>` must match `^[a-z0-9_]+$` and reference a registered adapter; otherwise 400.
- `<encoded_source_id>` must decode as base64url; otherwise 400.
- Decoded source_id is canonicalised by `adapter.normalize_candidate_id` (for adapters that implement it); failures return 400.

## 4. Health endpoint

### 4.1 `GET /health`

Liveness check. Returns:

```http
200 OK
Content-Type: application/json

{
  "status": "ok",
  "active_jobs": 2
}
```

When unhealthy (e.g., dependent services unreachable):

```http
503 Service Unavailable

{
  "status": "degraded"
}
```

This endpoint is unauthenticated by design — load balancers and monitoring tools must be able to call it without secrets. The response does not include sensitive data.

## 5. Source ingest endpoints

These allow operators to trigger source adapters directly without going through Lidarr search. Used for manual ingest workflows (drop a folder, POST to ingest endpoint).

### 5.1 `POST /local/ingest`

Trigger a LocalFolder grab.

**Request:**

```http
POST /local/ingest
X-Api-Key: <MINTARR_API_KEY>
Content-Type: application/json

{
  "path": "Artist/Album"
}
```

**Response (success):**

```http
200 OK
Content-Type: application/json

{
  "status": true,
  "nzo_ids": ["0cd9dbf08198"],
  "job_id": 42
}
```

**Response (validation failure):**

```http
400 Bad Request

{
  "status": false,
  "error": "path traversal blocked: ../../etc"
}
```

**Response (adapter disabled):**

```http
503 Service Unavailable

{
  "status": false,
  "error": "local adapter not enabled"
}
```

The path is normalised via `LocalFolderAdapter.normalize_candidate_id` before job creation. Path traversal, symlinks, and absolute paths are rejected. Duplicate paths return the existing job (dedupe by normalized rel-path hash).

### 5.2 `POST /soulseek/ingest` (planned, F3.5a)

Same shape as `/local/ingest`. Triggers a Soulseek completed-folder grab. Adds completed-folder validation (settle window, no partial markers). See the F3.5 Soulseek adapter design (held in private monorepo pending v0.2.0 migration), §10.

## 6. Dashboard endpoints

### 6.1 `GET /dashboard`

Returns the dashboard HTML. Server-side rendered. Phase 2 work replaces this with a sidebar layout.

The HTML shell itself does not require authentication. It stores the operator-provided API key in browser localStorage and sends it to the JSON endpoints below. No JSON variant — use the `/dashboard/v1/...` endpoints below for programmatic access.

### 6.2 `GET /dashboard/v1/summary`

```http
200 OK
Content-Type: application/json

{
  "counts": {
    "total_decisions": 247,
    "imported": 211,
    "needs_review": 3,
    "pending": 0,
    "failed": 5,
    "blocked": 28,
    "discarded": 0,
    "expired": 0,
    "policy_violations": 0,
    "active_jobs": 2,
    "sab_emulated": 2,
    "lidarr_queue": 4
  },
  "queue": {
    "sab_emulated": 2,
    "lidarr_queue_total": 4,
    "lidarr_commands": {
      "status": "ok",
      "active_count": 0,
      "blocking_count": 0,
      "commands": [],
      "error": null
    }
  },
  "stack_health": {
    "tidalhires": "ok",
    "lidarr": "ok",
    "flac_detective": "ok"
  },
  "generated_at": "2026-05-31T12:00:00.000000"
}
```

### 6.3 `GET /dashboard/v1/records`

```http
GET /dashboard/v1/records?limit=50&offset=0&status=needs_review
X-Api-Key: <MINTARR_API_KEY>
```

Response:

```http
200 OK

{
  "total": 247,
  "limit": 50,
  "offset": 0,
  "records": [
    {
      "jid": "...",
      "title": "...",
      "derived_status": "needs_review",
      "verification_decision": "REVIEW_REQUIRED",
      "import_outcome": null,
      "lifecycle_state": "pending_review",
      "score": 45,
      "verdict": "SUSPICIOUS",
      "overrides": ["fake_hi_res"],
      "needs_action": true,
      "status_reason": "Review required: possible fake hi-res.",
      "album_ids": [12345],
      "ts": 1779789600.0,
      "ts_iso": "2026-05-26T14:00:00"
    }
  ]
}
```

Query parameters:

| Param | Type | Description |
|---|---|---|
| `limit` | int | Page size, default 50, max 200 |
| `offset` | int | Page offset, default 0 |
| `status` | string | Filter by `derived_status` (`imported`, `needs_review`, `blocked`, `failed`, `discarded`, `expired`) |
| `source_type` | string | Reserved for source filtering. Current implementation includes source data in detailed records and DB-backed views; UI filters may lag this parameter. |

### 6.4 `GET /dashboard/v1/record/<jid>`

```http
200 OK

{
  "jid": "0cd9dbf08198",
  "sidecar": { ... },     // Full verification.json contents
  "actions": [ ... ],     // Audit log of operator actions
  "media": {
    "audio_url": "/dashboard/v1/audio-sample/<jid>",
    "spectrum_url": "/dashboard/v1/spectrum/<jid>"
  }
}
```

Related detail endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /dashboard/v1/actions/<jid>` | Audit actions for one record |
| `GET /dashboard/v1/lidarr-context/<jid>` | Lidarr queue/history/library context for the record |
| `GET /dashboard/v1/audio-sample/<jid>` | Short review sample when files are still available |
| `GET /dashboard/v1/spectrum/<jid>` | Spectrum PNG when generated/cached |

### 6.5 `GET /dashboard/v1/jobs`

```http
200 OK

{
  "jobs": [
    {
      "id": 42,
      "jid": "...",
      "type": "tidal_grab",
      "state": "running",
      "result_state": null,
      "progress": {
        "stage": "downloading",
        "percent": 35,
        "message": "Downloading from TIDAL"
      },
      "created_at": 1779789600.0,
      "attempts": 1
    }
  ]
}
```

### 6.6 `POST /dashboard/v1/action/<jid>`

Unified operator-action endpoint. The request body selects the action.

```http
POST /dashboard/v1/action/<jid>
X-Api-Key: ...
Content-Type: application/json

{
  "action": "promote"
}
```

Response:

```http
202 Accepted

{
  "status": true,
  "queued": true,
  "job_id": 84,
  "jid": "0cd9dbf08198",
  "action": "promote"
}
```

Supported actions:

| Action | Behaviour |
|---|---|
| `promote` | Promote a REVIEW_REQUIRED record. Enqueues a `promote_import` job. |
| `discard` | Discard a REVIEW_REQUIRED or active record. Files removed, sidecar moved to `discarded/`. |
| `retry_import` | Retry import for a record stuck in FAILED outcome. Enqueues a `retry_import` job. |

Invalid actions or invalid record state return `409 Conflict` with a human-readable `error`.

### 6.7 `GET /dashboard/v1/actions` and `GET /dashboard/v1/actions/<jid>`

Returns the operator-action audit timeline. The unscoped endpoint supports recent global audit views; the scoped endpoint is used by the record drawer.

### 6.8 `POST /dashboard/v1/jobs/<job_id>/cancel`

Request cancellation of a running worker job. Cooperative cancel — the adapter's next `check_cancelled()` call raises `JobCancelled`.

### 6.9 `GET /dashboard/v1/connectors` (F4.1, planned)

Returns the static connector registry + runtime status. Shape defined in [`CONNECTOR_MANIFEST_v1.md`](CONNECTOR_MANIFEST_v1.md) §6.

This endpoint is not implemented in current runtime builds. It becomes part of v1 only when F4.1 lands and this spec is locked.

### 6.10 `GET /dashboard/v1/timings`

Aggregate timing statistics for performance analysis.

### 6.11 `GET /decisions`

```http
GET /decisions?limit=100
X-Api-Key: ...
```

Returns recent entries from `decisions.jsonl` for forensic queries.

## 7. Legacy verification endpoints

These endpoints predate the dashboard v1 JSON API and remain available for compatibility with scripts and operator workflows. New integrations should prefer `/dashboard/v1/...` where equivalent.

### 7.1 `GET /jobs`

Returns the in-memory SAB-compatible job projection. Optional query parameter `status=<state>` filters by job status.

### 7.2 `GET /verification`

Lists verification sidecars from active and archive locations.

Query parameters:

| Param | Description |
|---|---|
| `decision` | Filter by `v2_verification_decision` |
| `import_outcome` | Filter by `v2_import_outcome` |
| `since` | Unix timestamp lower bound |
| `limit` | Max rows, default 50 |
| `format=html` | Render the legacy HTML review table |

JSON response shape:

```json
{
  "count": 1,
  "verification": [
    { "jid": "...", "v2_verification_decision": "ACCEPT" }
  ]
}
```

### 7.3 `GET /verification/<jid>`

Returns one verification sidecar, after best-effort reconciliation of pending imports.

### 7.4 `POST /verification/<jid>/promote`

Legacy promote action for REVIEW_REQUIRED records. Current runtime enqueues the same `promote_import` worker job used by `/dashboard/v1/action/<jid>`.

### 7.5 `POST /verification/<jid>/retry-import`

Legacy retry action for records whose verified import failed or remained pending. Current runtime enqueues the same `retry_import` worker job used by `/dashboard/v1/action/<jid>`.

### 7.6 `POST /verification/<jid>/discard`

Legacy discard action for REVIEW_REQUIRED records and failed/pending manual-promote records.

## 8. NZB pointer endpoints

Covered in §3.3 and §3.4. The `/download/...` paths serve Lidarr's NZB-pointer-roundtrip mechanism.

## 9. Infrastructure endpoints

### 9.1 `GET /`

Returns a small JSON service index with high-level endpoint hints. Unauthenticated and does not include secrets.

### 9.2 `GET|POST /<path:p>`

Authenticated catch-all for unknown routes. Logs method/path with redacted request values and returns:

```json
{
  "error": "unknown route",
  "path": "the/requested/path"
}
```

### 9.3 `GET /static/<path:filename>`

Flask's static-file route. Current dashboard is inline HTML/CSS/JS, so this route is incidental and not a Mintarr extension surface.

## 10. Cancellation and idempotency

### 10.1 Cancellation

`POST /dashboard/v1/jobs/<job_id>/cancel` sets `jobs.cancel_requested = 1`. The worker thread polls this flag at adapter checkpoints. If the job is `queued` (not yet running), cancel removes it from the queue. If `running`, the adapter's next `ctx.check_cancelled()` raises and the worker marks the job cancelled.

Cancel acknowledgement does not wait for the worker to actually stop — the endpoint returns immediately after setting the flag. The dashboard polls `/dashboard/v1/jobs` to observe state transition.

### 10.2 Idempotency

The following endpoints are idempotent:

- `POST /local/ingest`: deduplicates by normalised path hash; same path returns the existing active job
- `POST /sabnzbd/api?mode=addurl`: deduplicates by `<source>:<source_id>`; same combination returns the existing active job
- `POST /dashboard/v1/action/<jid>` with `{"action": "promote"}`: rejects if not in REVIEW_REQUIRED state; otherwise enqueues exactly once

The following endpoints are not idempotent:

- `POST /dashboard/v1/action/<jid>` with `{"action": "retry_import"}`: enqueues a new retry attempt each call
- `POST /dashboard/v1/jobs/<job_id>/cancel`: cancel-already-cancelled returns success without action

## 11. Errors

Standard error response shape:

```json
{
  "status": false,
  "error": "human-readable description"
}
```

Some endpoints (older / external-protocol-compat) return `{"error": "..."}` without the `status: false` envelope. New endpoints follow the standard shape.

HTTP status codes:

| Code | Meaning |
|---|---|
| 200 | Success |
| 400 | Client error (bad input, malformed JSON, invalid path, validation failure) |
| 401 | Authentication failed (missing or wrong API key) |
| 403 | Authenticated but not authorised (currently unused — see [ADR-0002](../architecture/adr/0002-single-instance-arr-pattern.md)) |
| 404 | Resource not found (unknown jid, unknown job_id, unknown source) |
| 409 | State conflict (e.g., promoting a record that's not REVIEW_REQUIRED, Soulseek folder not settled) |
| 500 | Server error (bug — file a security report if exploitable) |
| 503 | Service unavailable (adapter disabled, dependent service unreachable, worker queue overloaded) |

## 12. Rate limits

Mintarr does not enforce rate limits in v1. Operators deploying behind a reverse proxy should set rate limits there for public-facing instances.

The worker queue acts as an implicit limit on grab throughput — `N=1` by default means only one source-grab job runs at a time. Other endpoints (read-only dashboard queries, health checks) are not throttled.

## 13. Versioning

This spec is v1. Breaking changes require `HTTP_API_v2.md` per [ADR-0004](../architecture/adr/0004-api-versioning-semver.md).

New endpoints can be added to v1 without bumping the major version. New optional query parameters can be added. New optional response fields can be added.

Breaking changes (changed semantics, removed endpoints, removed fields, renamed fields, changed response shape) trigger v2.

## 14. Future direction

Phase 3 (Observability and integration surface) replaces this hand-written document with an auto-generated OpenAPI specification served at `/openapi.json`, with Swagger UI at `/docs`. Until then, this file is authoritative; OpenAPI and HTTP_API_v1.md will be cross-checked when OpenAPI lands.

## 15. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-05-26 | Initial locked spec. |
| 1.0.1 | 2026-05-31 | Corrected dashboard routes and response shapes to match deployed runtime; marked spec draft until cutover validation. |
| 1.0.2 | 2026-05-31 | Locked after validation against Flask route inventory (33 routes). Added legacy verification and infrastructure endpoints; clarified dashboard HTML auth behaviour and POST /api SAB fallback. |

---

> Last updated: 2026-05-31
