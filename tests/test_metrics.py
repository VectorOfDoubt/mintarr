"""Tests for the Prometheus /metrics endpoint (Phase 3 slice 2)."""

from __future__ import annotations

import server


def test_metrics_render_contains_core_gauges():
    import metrics

    body, content_type = metrics.render_metrics()
    text = body.decode()
    assert "text/plain" in content_type
    assert "mintarr_up 1.0" in text
    assert "# TYPE mintarr_records gauge" in text
    assert "# TYPE mintarr_jobs gauge" in text
    assert "mintarr_active_jobs" in text


def test_metrics_endpoint_is_unauthenticated():
    client = server.app.test_client()
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.content_type
    # content_type set verbatim — no duplicated charset param.
    assert resp.content_type.lower().count("charset") <= 1
    body = resp.get_data(as_text=True)
    assert "mintarr_up" in body
