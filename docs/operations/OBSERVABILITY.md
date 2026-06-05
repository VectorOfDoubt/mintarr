# Observability

> **Type:** Operations / observability
> **Version:** 0.2 - 2026-06-05
> **Status:** Runtime-backed. Logs, metrics, OpenAPI, notifications, webhook-in, backup export, and Grafana templates exist.
> **Audience:** Operators wiring Mintarr into logs, dashboards, alerts, or backup checks.

---

## 1. Current observability surface

Mintarr currently exposes operational state through:

| Surface | Status | Purpose |
|---|---|---|
| Runtime logs | Implemented | Container-level events and warnings |
| Dashboard API | Implemented | Current jobs, records, connector health, Lidarr queue context |
| Sidecars | Implemented | Per-record verification and import evidence |
| `decisions.jsonl` | Implemented | Append-only decision audit trail |
| SQLite state DB | Implemented | Query index over sidecars and worker state |
| Prometheus `/metrics` | Implemented | State-derived operational gauges |
| Grafana templates | Implemented | Importable dashboards under `docs/grafana/` |
| Structured JSON logs | Implemented | Optional JSON log format for log stacks |
| OpenAPI / Swagger UI | Implemented | `/openapi.json` and `/docs` |
| Notifications | Implemented | Optional Apprise notifications for attention events |
| Webhook-in | Implemented | `POST /webhook/ingest` for external automation |
| Backup export | Implemented | `GET /backup` read-only state export |

Restore automation, scheduled backups, and richer event/timing metrics remain
future work.

## 2. Logs

Read logs with:

```bash
docker logs mintarr --tail 100
docker logs mintarr --follow
```

Logs should not contain API keys or OAuth tokens. Request logging redacts common
secret fields such as `apikey`, `api_key`, `x-api-key`, `password`, and `token`.

### 2.1 Log format (text or JSON)

The default format is human-readable text (`%(asctime)s %(levelname)s %(message)s`)
— unchanged from earlier releases. For ingestion into a log stack (Loki, ELK,
etc.) set `MINTARR_LOG_FORMAT=json` to emit **one JSON object per line**:

| Env | Values | Default | Effect |
|---|---|---|---|
| `MINTARR_LOG_FORMAT` | `text` / `json` | `text` | line format |
| `MINTARR_LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` | minimum level |

JSON records carry a stable envelope, plus any structured `extra=` fields a log
call attaches (e.g. `jid`, `event`, `source_type`):

| Field | Meaning |
|---|---|
| `ts` | ISO-8601 UTC timestamp |
| `level` | log level |
| `component` | logger name (e.g. `tidalhires.worker`) |
| `message` | rendered message |
| `exc` | traceback (only on exceptions) |

The formatter never adds secrets — it formats whatever was logged, so the
redaction rules above still apply to message content.

When filing issues, redact:

- API keys
- OAuth token JSON
- slskd API keys
- full Lidarr `downloadUrl` values
- local paths if they expose private names

## 3. Dashboard API

The dashboard API is the best current machine-readable status surface.

```bash
curl -H "X-Api-Key: $MINTARR_API_KEY" \
    http://127.0.0.1:5025/dashboard/v1/summary
```

Important fields:

| Field | Meaning |
|---|---|
| `counts.active_jobs` | Mintarr worker jobs currently active |
| `queue.lidarr_queue_total` | Lidarr tracked-download queue size |
| `queue.lidarr_commands` | Active or blocking Lidarr commands |
| `queue.sab_emulated` | Mintarr jobs still visible through SAB emulation |
| `stack_health` | Connector/dependency status summary |

Use `/dashboard/v1/records` for record history and `/dashboard/v1/jobs` for
worker job details.

## 4. Sidecars and audit trail

Every import decision should be explainable from a sidecar or terminal-state
archive.

| File | Meaning |
|---|---|
| `/output/<jid>/verification.json` | live sidecar for retained output |
| `/config/blocked_decisions/<jid>.json` | archived blocked decision |
| `/config/discarded/<jid>.json` | archived discarded review item |
| `/config/expired_review/<jid>.json` | archived expired review item |
| `/config/decisions.jsonl` | append-only decision log |

The state DB is a query index. Sidecars and `decisions.jsonl` are the durable
audit evidence.

## 5. Operator checks

Basic health check:

```bash
curl http://127.0.0.1:5025/health
```

Queue sanity check:

```bash
curl -H "X-Api-Key: $MINTARR_API_KEY" \
    http://127.0.0.1:5025/dashboard/v1/summary
```

Healthy idle state usually means:

- `counts.active_jobs` is `0`
- `queue.lidarr_queue_total` is `0`
- `queue.lidarr_commands.blocking_count` is `0`
- stack health entries are `ok` or intentionally disabled

During a grab, active jobs and queue rows are expected.

## 6. Metric catalogue

Mintarr exposes a Prometheus endpoint at **`GET /metrics`**. It is
**unauthenticated** by convention — like `/health` — so a Prometheus scraper on
the private network can pull without a key. It exposes operational counts only
(no secrets) and is **state-derived from the local database**, so a scrape never
calls Lidarr and stays fast and independent of external-service availability.

```bash
curl -s http://127.0.0.1:5025/metrics
```

Currently exposed at `GET /metrics`:

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `mintarr_up` | Gauge | — | `1` while the app is serving |
| `mintarr_records` | Gauge | `status` | records by derived status (imported, needs_review, blocked, …) |
| `mintarr_jobs` | Gauge | `state` | worker jobs by state (queued, running, completed, failed, …) |
| `mintarr_active_jobs` | Gauge | — | active worker jobs (queued/running/cancelling) |

All current metrics are gauges computed at scrape time from `state_db`. They are
current-state signals, not cumulative event counters. A missing label value
usually means the local state database currently has zero rows for that status
or state.

Stable label values are intentionally conservative:

| Label | Known values | Notes |
|---|---|---|
| `mintarr_records.status` | `imported`, `needs_review`, `blocked`, `failed`, `discarded`, `expired`, plus future derived statuses | Derived from record state and sidecar/import outcome |
| `mintarr_jobs.state` | `queued`, `running`, `completed`, `failed`, `cancelled`, `cancelling`, plus future worker states | Mirrors worker/state DB job lifecycle |

Planned for later slices (cumulative event counters + histograms, which require
instrumentation rather than current-state gauges): import outcomes, V2 decision
counts, release-identity decisions, release-switch events, Lidarr queue/command
health, connector health, and per-stage pipeline timing histograms.

## 7. Grafana dashboards

Grafana dashboard templates live under
[`docs/grafana/`](../grafana/README.md):

| Dashboard | Purpose |
|---|---|
| [`mintarr-overview.json`](../grafana/mintarr-overview.json) | Mintarr health, active jobs, records by status, and worker state |
| [`mintarr-worker-queue.json`](../grafana/mintarr-worker-queue.json) | Worker queue detail panels for queued/running/failed and active jobs |

Import them through Grafana's dashboard import flow and select the Prometheus
datasource that scrapes Mintarr. The templates use only the four metrics in the
catalogue above, so they work with the current `/metrics` implementation.

## 8. Performance Baseline

The F2 worker queue is intentionally single-threaded (`N=1`) and backed by
SQLite. See [F2 worker queue design](../design/F2_WORKER_QUEUE_DESIGN.md) for
the design rationale.

Mintarr tracks one dedicated benchmark for the mocked full source-grab pipeline:

| Benchmark | Baseline | Alert threshold | Workflow |
|---|---:|---:|---|
| `test_full_pipeline_orchestration_benchmark` | 1.45 ms mean | >2.18 ms mean (+50%) | Performance baseline |

This is not a real download-throughput number. External subprocesses and
services are mocked, so the benchmark measures Python orchestration, local
filesystem staging, queue/pipeline glue, and state updates. It is useful as a
regression guard for worker/pipeline refactors, not as an operator capacity
model.

The baseline is stored in
`tests/perf/baselines/pipeline_orchestration.json`. The dedicated performance
workflow compares the current pytest-benchmark JSON result against that stored
baseline. The normal pytest suite does not run this benchmark, preserving the
fast deterministic test contract.

## 9. Planned alerts

Candidate alerts:

- Mintarr active job older than source-specific timeout
- Lidarr queue non-zero while Mintarr active jobs are zero
- FLAC Detective unavailable while any source connector is in import mode
- repeated `BLOCK` decisions from one source
- repeated import failures for the same target album
- state DB unavailable or sidecar backfill errors

Alerts should be actionable. Avoid alerting on normal long-running downloads
unless they exceed configured timeouts.

---

> Last updated: 2026-06-05
