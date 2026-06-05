"""Minimal OpenAPI 3 document generated from the Flask url_map (Phase 3 slice 3a).

Generated from the live routes so it stays in sync without a schema-framework
refactor of the locked HTTP_API_v1 contract (ADR-0004). Per-endpoint request/
response body schemas are intentionally out of scope for this first slice; they
can be enriched incrementally. HTTP_API_v1.md remains authoritative until the
generated spec reaches parity.
"""

from __future__ import annotations

import re
from typing import Any

# Endpoints that are unauthenticated by design (infra, like /health, /metrics).
_UNAUTHENTICATED = {"health", "metrics"}

_PATH_PARAM = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")


def _flask_path_to_openapi(rule: str) -> str:
    """`/download/<int:album_id>.nzb` -> `/download/{album_id}.nzb`."""
    return _PATH_PARAM.sub(r"{\1}", rule)


def _path_params(rule: str) -> list[str]:
    return _PATH_PARAM.findall(rule)


def _canonical_path(oas_path: str) -> str:
    """Drop a trailing slash (Flask aliases `/x` and `/x/` to one view)."""
    if len(oas_path) > 1 and oas_path.endswith("/"):
        return oas_path.rstrip("/")
    return oas_path


def _operation_id(method: str, oas_path: str) -> str:
    """Unique per (method, path) — OpenAPI requires operationId uniqueness.

    Two Flask rules can share a view (e.g. /api and /newznab/api); a path-based
    id keeps each operation distinct.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", oas_path.lower()).strip("_") or "root"
    return f"{method.lower()}_{slug}"


def build_openapi(app: Any) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static" or "<path:" in rule.rule:
            continue
        methods = sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
        if not methods:
            continue
        oas_path = _canonical_path(_flask_path_to_openapi(rule.rule))
        item = paths.setdefault(oas_path, {})
        parameters = [
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
            for name in _path_params(rule.rule)
        ]
        secured = rule.endpoint not in _UNAUTHENTICATED
        for method in methods:
            operation: dict[str, Any] = {
                "summary": rule.endpoint.replace("_", " "),
                "operationId": _operation_id(method, oas_path),
                "responses": {"200": {"description": "OK"}},
            }
            if parameters:
                operation["parameters"] = parameters
            if secured:
                operation["security"] = [{"ApiKeyHeader": []}, {"ApiKeyQuery": []}]
                operation["responses"]["401"] = {"description": "Unauthorized"}
            item[method.lower()] = operation

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Mintarr HTTP API",
            "version": "1.0.5",
            "description": (
                "Auto-generated from the live Flask routes. HTTP_API_v1.md is "
                "authoritative for request/response detail until this spec reaches "
                "parity. Most endpoints require an API key (X-Api-Key header or "
                "apikey query param); /health and /metrics are unauthenticated."
            ),
        },
        "components": {
            "securitySchemes": {
                "ApiKeyHeader": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Api-Key",
                },
                "ApiKeyQuery": {
                    "type": "apiKey",
                    "in": "query",
                    "name": "apikey",
                },
            }
        },
        "paths": dict(sorted(paths.items())),
    }
