"""Tests for the generated OpenAPI spec (Phase 3 slice 3a)."""

from __future__ import annotations

import server

VALID_KEY = "tidalhires-test-api-key"


def test_openapi_is_public():
    """The spec is a descriptor (no secrets); Swagger UI must fetch it unauthed."""
    client = server.app.test_client()
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.get_json()["openapi"].startswith("3.")


def test_openapi_spec_shape():
    client = server.app.test_client()
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.get_json()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "Mintarr HTTP API"
    assert "ApiKeyHeader" in spec["components"]["securitySchemes"]
    assert "ApiKeyQuery" in spec["components"]["securitySchemes"]

    # Real routes present; Flask converters normalised to {name}.
    assert "/health" in spec["paths"]
    assert "/dashboard/v1/record/{jid}" in spec["paths"]

    # static + catch-all excluded.
    assert not any("<path:" in p or p.startswith("/static") for p in spec["paths"])

    # /health is unauthenticated -> no security on its operation.
    assert "security" not in spec["paths"]["/health"]["get"]
    # An authenticated endpoint carries the security requirement.
    assert spec["paths"]["/dashboard/v1/record/{jid}"]["get"].get("security")


def test_openapi_operation_ids_are_unique():
    import openapi

    spec = openapi.build_openapi(server.app)
    op_ids = [
        op["operationId"]
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    ]
    assert op_ids, "spec produced no operations"
    assert len(op_ids) == len(set(op_ids)), "duplicate operationId in generated spec"

    # Trailing-slash alias collapsed to a single path.
    assert "/dashboard" in spec["paths"]
    assert "/dashboard/" not in spec["paths"]
    # Distinct routes sharing a view keep distinct path-based operationIds.
    assert "/api" in spec["paths"]
    assert "/newznab/api" in spec["paths"]
    assert (
        spec["paths"]["/api"]["get"]["operationId"]
        != spec["paths"]["/newznab/api"]["get"]["operationId"]
    )


def test_build_openapi_path_conversion():
    import openapi

    assert (
        openapi._flask_path_to_openapi("/download/<int:album_id>.nzb")
        == "/download/{album_id}.nzb"
    )
    assert (
        openapi._flask_path_to_openapi("/verification/<jid>/promote")
        == "/verification/{jid}/promote"
    )
