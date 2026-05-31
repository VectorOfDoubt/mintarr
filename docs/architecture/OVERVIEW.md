# Mintarr — Architecture Overview

> **Type:** Architecture / orientation
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document — major architectural shifts are captured in ADRs and reflected here.
> **Audience:** Contributors and operators who want to understand how Mintarr's components fit together. New contributors should start here.

---

## How to read this

This document is the map. Other architecture documents go deep on specific subsystems:

- [PIPELINE.md](PIPELINE.md) — the four pipeline phases with invariants
- [DATA_MODEL.md](DATA_MODEL.md) — state_db schema, sidecar format, decisions.jsonl
- [SECURITY_MODEL.md](SECURITY_MODEL.md) — threat model, secrets handling, attack surfaces
- `adr/` directory — locked decisions (numbered, append-only); start at [ADR-0001](adr/0001-rename-from-tidalhires.md)

Spec documents under `docs/specs/` version the contracts other contributors build against (start at [ADAPTER_PROTOCOL_v1](../specs/ADAPTER_PROTOCOL_v1.md)). Design documents under `docs/design/` record per-feature decisions (start at [F4.1 Static Connector Registry](../design/F4.1_STATIC_CONNECTOR_REGISTRY.md)).

If you read OVERVIEW.md and still cannot answer "where does X live", that is a documentation bug — please open an issue.

---

## What Mintarr does

Mintarr is a quality-control filter and orchestration layer that sits between music sources (TIDAL, LocalFolder, Soulseek, future SAB/qBit/CD-rip/YouTube) and music targets (Lidarr today; Plex / Jellyfin / filesystem-only as the OutputConnector model is exercised).

Every import passes through four pipeline phases:

```
┌──────────────┐    ┌────────────────┐    ┌──────────┐    ┌──────────────────┐
│ download_raw │───>│ normalize_audio│───>│  verify  │───>│ import_to_lidarr │
│   (adapter)  │    │    (common)    │    │ (common) │    │    (common)      │
└──────────────┘    └────────────────┘    └──────────┘    └──────────────────┘
```

Only the first phase varies by source. The other three are common code — adding a new source means writing a `download_raw()` implementation, not touching the rest of the pipeline.

See [PIPELINE.md](PIPELINE.md) for phase-by-phase invariants.

## Component diagram

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ Mintarr container (single instance)                                                 │
│                                                                                     │
│  ┌─────────────────┐  ┌──────────────────────┐  ┌──────────────────────────────┐   │
│  │  HTTP surface   │  │  Connector registry  │  │  Pipeline                    │   │
│  │                 │  │                      │  │                              │   │
│  │  /api           │──│  Sources             │──│  download_raw (per adapter)  │   │
│  │  (Newznab)      │  │   tidal              │  │  normalize_audio (ffprobe,   │   │
│  │  /sabnzbd/api   │  │   local_folder       │  │    ffmpeg, flac -t)          │   │
│  │  (SAB-emul)     │  │   soulseek (planned) │  │  verify (V2 policy)          │   │
│  │  /dashboard/v1  │  │                      │  │  import_to_lidarr            │   │
│  │  /local/ingest  │  │  Verifiers           │  │                              │   │
│  │  /download/     │  │   ffprobe (gate)     │  └──────┬───────────────────────┘   │
│  │  /openapi.json  │  │   flac_t (gate)      │         │                           │
│  │  (Phase 3)      │  │   flac_detective     │         │                           │
│  │                 │  │   ctdb (planned)     │         ▼                           │
│  └────────┬────────┘  │                      │  ┌──────────────────────────┐       │
│           │           │  Outputs             │  │ Worker queue (SQLite)    │       │
│           │           │   lidarr_manual      │  │                          │       │
│           │           │   lidarr_rescue      │  │  N=1 worker thread       │       │
│           │           │   plex_direct (Phase6│  │  Lease + heartbeat       │       │
│           │           │   jellyfin (Phase 6) │  │  Retry with allow-list   │       │
│           │           └──────────────────────┘  └──────────────────────────┘       │
│           │                                                                         │
│           ▼                                                                         │
│  ┌──────────────────────────────────────────────────────────────────────────────┐   │
│  │  state_db (SQLite) — query index over sidecars                               │   │
│  │  records / sensor_runs / file_evidence / actions / jobs                      │   │
│  └──────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                     │
│  ┌──────────────────────────────────────────────────────────────────────────────┐   │
│  │  /config (operator-mounted volume)                                           │   │
│  │   sidecars (verification.json — source of truth)                             │   │
│  │   decisions.jsonl (append-only audit log)                                    │   │
│  │   mintarr_state.db (the state_db file)                                       │   │
│  │   blocked_decisions/  discarded/  expired_review/  backups/                  │   │
│  └──────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────┘

     ▲                                                                       │
     │                                                                       ▼
┌────────┐                                          ┌─────────────────────────────┐
│ Lidarr │  <── /api Newznab search / addurl  ───►  │ External tools              │
│ (host) │       /api/v1 Lidarr ManualImport        │  ffprobe + ffmpeg (in img)  │
└────────┘                                          │  flac-detective (HTTP svc)  │
                                                    │  TIDAL HTTPS                │
                                                    │  slskd HTTP (Phase 4)       │
                                                    └─────────────────────────────┘
```

## Runtime layers

Each component box maps to a directory in the codebase. The list is structural; specific module names may evolve.

### HTTP surface (`mintarr/app/server.py`)

Flask routes for Lidarr-facing endpoints, dashboard API, source-specific ingest endpoints, and the encoded `/download` endpoint. Auth is `X-Api-Key` header or `apikey` query param using `hmac.compare_digest`.

Endpoints fall into four categories:

- **Lidarr-facing** — `/api` (Newznab), `/sabnzbd/api` (SAB emulation), `/download/<source>/<id>.nzb` (NZB pointer generation), `/health`
- **Dashboard** — `/dashboard`, `/dashboard/v1/records`, `/dashboard/v1/summary`, `/dashboard/v1/jobs`, `/dashboard/v1/actions`
- **Source-specific** — `/local/ingest` (LocalFolder), future `/soulseek/ingest`, future per-source endpoints as needed
- **Future (Phase 3)** — `/openapi.json`, `/docs`, `/metrics`

### Connector registry (`mintarr/app/adapters/` and `mintarr/app/connectors/`)

`SourceAdapter` Protocol and concrete adapters under `adapters/`. The `Connector` model wraps them with manifests, runtime health, version detection, and dashboard surface (Phase 1).

Adapters depend only on `adapters/base.py` and `adapters/context.py`. They do not import `server.py`. The dependency direction is one-way: server.py builds a `PipelineContext` and hands it to the adapter; the adapter never reaches back.

### Pipeline (`mintarr/app/pipeline.py`)

The four-phase common flow. `execute_source_grab(job, adapter, ctx)` is the entry point that the worker queue calls. Each phase is a separate function with explicit invariants; see [PIPELINE.md](PIPELINE.md).

The pipeline does not know which adapter it is running. It calls `adapter.download_raw()` and treats whatever appears in `ctx.raw_dir` uniformly from there.

### Worker queue (`mintarr/app/worker.py` and `mintarr/app/state_db.py` jobs table)

SQLite-backed work queue. One worker thread by default (`N=1`). Reasons for not using Celery/RQ (full design doc planned for v0.2.0):

- We already have SQLite for state; adding Redis doubles infrastructure
- N=1 matches our actual concurrency needs (Lidarr serialises grabs, TIDAL session is global)
- Lease + heartbeat handles worker restart cleanly
- Retry classification is policy-based (allow-list + deny-list of error substrings)

Job types today: `tidal_grab`, `local_grab`, `promote_import`, `retry_import`. Future: `soulseek_grab`, `sab_grab`, `qbit_grab`.

### State DB (`mintarr/app/state_db.py`)

SQLite database at `/config/mintarr_state.db` (legacy: `tidalhires_state.db`). Five tables:

| Table | Purpose |
|---|---|
| `records` | One row per import attempt. Indexes sidecar evidence for dashboard queries. |
| `sensor_runs` | Per-verifier evidence rows. One per (jid, sensor_name). |
| `file_evidence` | Per-file evidence rows. Used for spectral plots and audio playback in dashboard. |
| `actions` | Operator action audit. Promote, discard, retry_import, cancel. |
| `jobs` | Worker queue rows. State machine: `queued` → `running` → (`completed` / `failed` / `cancelled`). |

The DB is a **query index**, not a source of truth. Sidecars on disk are the source of truth; the DB is rebuilt from sidecars via the backfill script. This means manual DB tampering is recoverable.

See [DATA_MODEL.md](DATA_MODEL.md) for full schema.

### /config volume

Mounted from the operator's host. Contains everything stateful:

```
/config/
├── mintarr_state.db                ← state index (rebuildable)
├── decisions.jsonl                 ← append-only audit log
├── blocked_decisions/<jid>.json    ← sidecar for BLOCK decisions
├── discarded/<jid>.json            ← sidecar after operator discard
├── expired_review/<jid>.json       ← sidecar after REVIEW_REQUIRED expiry
├── tidal_dl_ng/                    ← TIDAL OAuth token directory
└── backups/                        ← timestamped state_db snapshots
```

Sidecars for live records live in `OUTPUT_BASE/<jid>/verification.json` (typically `/downloads/MintarrComplete/<jid>/verification.json` on the host). They move to `/config/{blocked_decisions, discarded, expired_review}/` once the record reaches a terminal lifecycle state.

## Request flow examples

### Lidarr-initiated TIDAL grab

```
1. Lidarr ── GET /api?t=search&q=Daft+Punk+RAM ──> Mintarr
2. Mintarr iterates enabled source adapters, calls each .search()
3. Mintarr returns aggregated Newznab XML to Lidarr
4. Lidarr selects a release, ── POST /sabnzbd/api?mode=addurl&name=tidal:12345 ──> Mintarr
5. Mintarr ── _parse_source_name ──> ("tidal", "12345")
6. Mintarr ── state_db.enqueue_job(type=tidal_grab, ...) ──> worker queue
7. Worker thread picks up the job
8. Worker ── pipeline.execute_source_grab(job, TidalAdapter, ctx) ──> runs all four phases
9. download_raw: tidal-dl-ng subprocess fetches files
10. normalize_audio: ffprobe codec gate + ffmpeg .m4a→.flac + flac -t
11. verify: flac-detective HTTP call + V2 policy decision
12. import_to_lidarr: ── POST /api/v1/command (ManualImport) ──> Lidarr
13. Worker writes terminal state to state_db.jobs
14. Operator sees the result on /dashboard
```

### Operator-initiated LocalFolder ingest

```
1. Operator drops Artist/Album/ into LOCAL_INGEST_PATH (mounted host directory)
2. Operator ── POST /local/ingest {"path":"Artist/Album"} ──> Mintarr
3. Mintarr ── LocalFolderAdapter.normalize_candidate_id ──> validated path
4. Mintarr ── state_db.enqueue_job(type=local_grab, source_id=rel_path) ──> worker queue
5. Worker thread picks up the job
6. Worker ── pipeline.execute_source_grab(job, LocalFolderAdapter, ctx) ──> runs all four phases
7. download_raw: shutil.copy2 from LOCAL_INGEST_PATH to ctx.raw_dir
8. normalize_audio + verify + import_to_lidarr: same common code as TIDAL
9. Source files left UNTOUCHED — adapter copies, does not move
```

### REVIEW_REQUIRED human-in-the-loop

```
1. Pipeline reaches verify phase with conflicting evidence
2. V2 policy returns decision = REVIEW_REQUIRED
3. Sidecar written with lifecycle.state = "pending_review"
4. Job ends with result_state = needs_review (not failed; worker succeeded)
5. Dashboard surfaces the record in the Review queue
6. Operator inspects evidence (spectra, codec, files, V2 score)
7. Operator clicks Promote → POST /dashboard/v1/action/<jid> with `{"action": "promote"}`
8. promote_import job enqueued
9. Worker runs _retry_verified_import: import_to_lidarr with the existing files
10. Sidecar updated with lifecycle.state = "promoted", import_outcome set
```

## Coupling boundaries

These boundaries are load-bearing. Crossing them creates the kind of technical debt that has to be re-architected:

| Boundary | Direction |
|---|---|
| Adapter → server.py | **One-way.** Adapters do not import server.py. server.py builds context and passes it in. |
| Adapter → pipeline.py | **One-way.** Adapters do not import pipeline. Pipeline calls adapters. |
| Pipeline → policy | **One-way.** Pipeline calls policy; policy does not know which adapter ran. |
| state_db ← sidecar | **Sidecar is source of truth.** state_db is rebuilt from sidecars via backfill. |
| Mintarr → Lidarr | **One-way.** Mintarr knows about Lidarr; Lidarr does not know Mintarr exists (it sees a Newznab indexer + SAB client). |
| Connector ↔ Adapter | **Connector wraps Adapter.** Code talks to adapters; operators talk to connectors. |

If a contribution violates one of these directions, it is by definition a structural change and must be discussed before merge.

## What is intentionally not in this overview

- **Per-feature design.** Lives in the `docs/design/` directory as F-numbered docs; start at [F4.1 Static Connector Registry](../design/F4.1_STATIC_CONNECTOR_REGISTRY.md).
- **Locked decisions and rationale.** Lives in the `docs/architecture/adr/` directory; start at [ADR-0001](adr/0001-rename-from-tidalhires.md).
- **Threat model and secrets.** Lives in [SECURITY_MODEL.md](SECURITY_MODEL.md).
- **Data schema details.** Lives in [DATA_MODEL.md](DATA_MODEL.md).
- **HTTP API reference.** Lives in [HTTP_API_v1.md](../specs/HTTP_API_v1.md) (and `/openapi.json` after Phase 3).

The principle: this document tells you where to look. It does not duplicate what is in those documents.

---

> Last updated: 2026-05-31
