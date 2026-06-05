# Grafana dashboards

This directory contains importable Grafana dashboard templates for Mintarr's
Prometheus metrics.

## Dashboards

| File | Purpose |
|---|---|
| `mintarr-overview.json` | Single-screen health, active jobs, record status, and worker state |
| `mintarr-worker-queue.json` | Worker queue detail with active jobs and queued/running/failed state panels |

## Import

1. Configure Prometheus to scrape Mintarr:

   ```yaml
   scrape_configs:
     - job_name: mintarr
       static_configs:
         - targets: ["mintarr:8000"]
   ```

2. In Grafana, open **Dashboards -> New -> Import**.
3. Upload one of the JSON files from this directory.
4. Select the Prometheus datasource when prompted.

The dashboards use only the metrics documented in
[Observability](../operations/OBSERVABILITY.md#6-metric-catalogue), so they do
not require any future counter or histogram instrumentation.
