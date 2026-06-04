# Observability

> **Type:** Operations / observability
> **Version:** 0.1 - 2026-06-03
> **Status:** Draft skeleton. Runtime logs and sidecars exist; metrics and Grafana templates are planned.
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
| Prometheus `/metrics` | Planned | Phase 3 |
| Grafana templates | Planned | Phase 3 |
| Structured JSON logs | Planned | Phase 3 |

The current runtime is inspectable, but not yet a full Prometheus/Grafana
observability stack.

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

## 6. Planned metrics

Phase 3 introduces a Prometheus `/metrics` endpoint. The initial catalogue should
cover:

| Metric | Type | Purpose |
|---|---|---|
| `mintarr_jobs_active` | Gauge | active worker jobs |
| `mintarr_jobs_total` | Counter | jobs by source/type/result |
| `mintarr_imports_total` | Counter | import outcomes |
| `mintarr_verification_decisions_total` | Counter | ACCEPT / REVIEW_REQUIRED / BLOCK |
| `mintarr_lidarr_queue_total` | Gauge | Lidarr tracked-download queue size |
| `mintarr_lidarr_blocking_commands` | Gauge | blocking Lidarr commands |
| `mintarr_connector_health` | Gauge | connector health by connector id |
| `mintarr_pipeline_stage_seconds` | Histogram | per-stage timing |

Metric names are provisional until the Phase 3 metrics spec is written.

## 7. Performance Baseline

The F2 worker queue is intentionally single-threaded (`N=1`) and backed by
SQLite. See [F2 worker queue design](../design/F2_WORKER_QUEUE_DESIGN.md) for
the design rationale.

Mintarr tracks one dedicated benchmark for the mocked full source-grab pipeline:

| Benchmark | Baseline | Alert threshold | Workflow |
|---|---:|---:|---|
| `test_full_pipeline_orchestration_benchmark` | 2.50 ms mean | >3.75 ms mean (+50%) | Performance baseline |

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

## 8. Planned alerts

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

> Last updated: 2026-06-03
