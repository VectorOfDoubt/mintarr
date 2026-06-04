"""Prometheus metrics for Mintarr (Phase 3 slice 2).

State-derived gauges computed on scrape from state_db only — no Lidarr call,
so /metrics stays fast and independent of external-service availability. The
endpoint exposes operational counts only; it contains no secrets.
"""

from __future__ import annotations

from collections.abc import Iterator

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector


class MintarrCollector(Collector):
    """Yields current-state gauges from state_db on each scrape."""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        import state_db

        up = GaugeMetricFamily("mintarr_up", "1 if the Mintarr app is serving")
        up.add_metric([], 1.0)
        yield up

        try:
            status_counts = state_db.count_by_status()
        except Exception:
            status_counts = {}
        records = GaugeMetricFamily(
            "mintarr_records", "Records by derived status", labels=["status"]
        )
        for status, n in sorted(status_counts.items()):
            records.add_metric([status], float(n))
        yield records

        try:
            job_counts = state_db.count_jobs_by_state()
        except Exception:
            job_counts = {}
        jobs = GaugeMetricFamily(
            "mintarr_jobs", "Worker jobs by state", labels=["state"]
        )
        for state, n in sorted(job_counts.items()):
            jobs.add_metric([state], float(n))
        yield jobs

        try:
            active = state_db.count_active_jobs()
        except Exception:
            active = 0
        active_g = GaugeMetricFamily(
            "mintarr_active_jobs", "Active worker jobs (queued/running/cancelling)"
        )
        active_g.add_metric([], float(active))
        yield active_g


def render_metrics() -> tuple[bytes, str]:
    """Render the Prometheus exposition text and its content type."""
    registry = CollectorRegistry()
    registry.register(MintarrCollector())
    return generate_latest(registry), CONTENT_TYPE_LATEST
