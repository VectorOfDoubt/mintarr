"""Tests for Swagger UI at /docs (Phase 3 slice 3b)."""

from __future__ import annotations

import server


def test_docs_serves_swagger_ui():
    client = server.app.test_client()
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.content_type
    body = resp.get_data(as_text=True)
    assert 'id="swagger-ui"' in body
    assert "/openapi.json" in body
    assert "vendor/swagger-ui-bundle-5.17.14.js" in body
    assert 'integrity="sha384-' in body


def test_swagger_vendor_assets_served():
    client = server.app.test_client()
    css = client.get("/static/vendor/swagger-ui-5.17.14.css")
    assert css.status_code == 200
    assert "text/css" in css.content_type
    js = client.get("/static/vendor/swagger-ui-bundle-5.17.14.js")
    assert js.status_code == 200
    assert "javascript" in js.content_type
