"""Tests for F5.4 slice 5c library scan API endpoints."""

from __future__ import annotations

import server
import state_db

VALID_KEY = "tidalhires-test-api-key"


def _headers():
    return {"X-Api-Key": VALID_KEY}


def test_library_scan_endpoints_require_auth():
    client = server.app.test_client()

    assert client.get("/library/scan").status_code == 401
    assert client.post("/library/scan", json={"mode": "cheap"}).status_code == 401
    assert client.post("/library/scan/cancel").status_code == 401


def test_start_library_scan_enqueues_run_and_job():
    client = server.app.test_client()

    resp = client.post("/library/scan", headers=_headers(), json={"mode": "cheap"})

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["started"] is True
    run = body["run"]
    assert run["mode"] == "cheap"
    assert run["state"] == "queued"
    assert run["worker_job_id"] is not None
    job = state_db.get_job(run["worker_job_id"])
    assert job["type"] == state_db.LIBRARY_SCAN_JOB_TYPE


def test_start_library_scan_returns_existing_same_mode():
    client = server.app.test_client()
    first = client.post("/library/scan", headers=_headers(), json={"mode": "cheap"})
    run_id = first.get_json()["run"]["id"]

    resp = client.post("/library/scan", headers=_headers(), json={"mode": "cheap"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["started"] is False
    assert body["run"]["id"] == run_id


def test_start_library_scan_mode_mismatch_conflicts():
    client = server.app.test_client()
    run = state_db.enqueue_library_scan(mode="spectral_missing")
    assert run is not None

    resp = client.post("/library/scan", headers=_headers(), json={"mode": "cheap"})

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["active_mode"] == "spectral_missing"
    assert body["requested_mode"] == "cheap"


def test_start_library_scan_rejects_unsupported_mode():
    client = server.app.test_client()

    resp = client.post("/library/scan", headers=_headers(), json={"mode": "full"})

    assert resp.status_code == 400


def test_library_scan_status_lists_runs():
    client = server.app.test_client()
    state_db.enqueue_library_scan(mode="cheap")

    resp = client.get("/library/scan", headers=_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["active"]["mode"] == "cheap"
    assert body["total"] == 1
    assert len(body["runs"]) == 1


def test_library_scan_cancel_active_run():
    client = server.app.test_client()
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None

    resp = client.post("/library/scan/cancel", headers=_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["cancel_requested"] is True
    assert body["run"]["id"] == run["id"]
    assert body["run"]["cancel_requested"] == 1
    assert state_db.get_job(run["worker_job_id"])["cancel_requested"] == 1


def test_library_scan_cancel_terminal_run_conflicts():
    client = server.app.test_client()
    run = state_db.enqueue_library_scan(mode="cheap")
    assert run is not None
    state_db.update_library_scan_run_state(run["id"], "completed")

    resp = client.post(
        "/library/scan/cancel",
        headers=_headers(),
        json={"run_id": run["id"]},
    )

    assert resp.status_code == 409


def test_library_scan_cancel_invalid_run_id_is_bad_request():
    client = server.app.test_client()

    resp = client.post(
        "/library/scan/cancel",
        headers=_headers(),
        json={"run_id": "not-an-int"},
    )

    assert resp.status_code == 400
