"""Auth regression tests for existing Batch D endpoints."""

from __future__ import annotations

import logging

import server


VALID_KEY = "tidalhires-test-api-key"


def test_newznab_routes_require_apikey():
    client = server.app.test_client()

    assert client.get("/api?t=caps").status_code == 401
    assert client.get("/newznab/api?t=caps").status_code == 401


def test_status_and_download_routes_require_apikey():
    client = server.app.test_client()

    assert client.get("/jobs").status_code == 401
    assert client.get("/decisions").status_code == 401
    assert client.get("/download/123.nzb").status_code == 401


def test_get_routes_accept_query_apikey():
    client = server.app.test_client()

    assert client.get(f"/api?t=caps&apikey={VALID_KEY}").status_code == 200
    assert client.get(f"/newznab/api?t=caps&apikey={VALID_KEY}").status_code == 200
    assert client.get(f"/jobs?apikey={VALID_KEY}").status_code == 200
    assert client.get(f"/decisions?apikey={VALID_KEY}").status_code == 200
    assert client.get(f"/download/123.nzb?apikey={VALID_KEY}").status_code == 200


def test_get_routes_accept_header_apikey():
    client = server.app.test_client()
    headers = {"X-Api-Key": VALID_KEY}

    assert client.get("/api?t=caps", headers=headers).status_code == 200
    assert client.get("/newznab/api?t=caps", headers=headers).status_code == 200
    assert client.get("/jobs", headers=headers).status_code == 200
    assert client.get("/decisions", headers=headers).status_code == 200
    assert client.get("/download/123.nzb", headers=headers).status_code == 200


def test_sab_routes_require_and_accept_apikey():
    client = server.app.test_client()

    assert client.get("/sabnzbd/api?mode=version").status_code == 401
    assert client.post("/sabnzbd/api", data={"mode": "version"}).status_code == 401
    assert client.post("/api", data={"mode": "version"}).status_code == 401

    assert (
        client.get(f"/sabnzbd/api?mode=version&apikey={VALID_KEY}").status_code == 200
    )
    assert (
        client.post(
            f"/sabnzbd/api?apikey={VALID_KEY}", data={"mode": "version"}
        ).status_code
        == 200
    )
    assert (
        client.post(f"/api?apikey={VALID_KEY}", data={"mode": "version"}).status_code
        == 200
    )


def test_sab_history_hides_cleaned_jobs(mocker):
    client = server.app.test_client()
    mocker.patch.object(server, "_save_jobs")
    server._jobs["visible123"] = {
        "id": "visible123",
        "status": "completed",
        "title": "Visible",
        "completed_at": 1,
    }
    server._jobs["hidden123"] = {
        "id": "hidden123",
        "status": "completed",
        "title": "Hidden",
        "completed_at": 1,
        "hidden_from_lidarr": True,
    }

    response = client.get(f"/sabnzbd/api?mode=history&apikey={VALID_KEY}")

    assert response.status_code == 200
    names = [slot["nzo_id"] for slot in response.get_json()["history"]["slots"]]
    assert "visible123" in names
    assert "hidden123" not in names
    server._jobs.pop("visible123", None)
    server._jobs.pop("hidden123", None)


def test_sab_queue_reports_progress_and_mbleft(mocker):
    client = server.app.test_client()
    mocker.patch.object(server, "_save_jobs")
    server._jobs["down123"] = {
        "id": "down123",
        "status": "downloading",
        "title": "Downloading Album",
        "category": "music",
        "size": 100 * 1024 * 1024,
        "percent": 40,
    }
    server._jobs["proc123"] = {
        "id": "proc123",
        "status": "processing",
        "title": "Processing Album",
        "category": "music",
        "size": 50 * 1024 * 1024,
        "percent": 100,
    }
    server._jobs["hiddenq"] = {
        "id": "hiddenq",
        "status": "downloading",
        "title": "Hidden",
        "hidden_from_lidarr": True,
    }

    response = client.get(f"/sabnzbd/api?mode=queue&apikey={VALID_KEY}")

    assert response.status_code == 200
    queue = response.get_json()["queue"]
    slots = {slot["nzo_id"]: slot for slot in queue["slots"]}
    assert set(slots) == {"down123", "proc123"}
    assert slots["down123"]["status"] == "Downloading"
    assert slots["down123"]["percentage"] == "40"
    assert slots["down123"]["mb"] == "100.00"
    assert slots["down123"]["mbleft"] == "60.00"
    assert slots["down123"]["sizeleft"] == str(60 * 1024 * 1024)
    assert slots["proc123"]["status"] == "Verifying"
    assert slots["proc123"]["timeleft"] == "0:00:00"
    assert queue["mb"] == "150.00"
    assert queue["mbleft"] == "60.00"
    server._jobs.pop("down123", None)
    server._jobs.pop("proc123", None)
    server._jobs.pop("hiddenq", None)


def test_apikey_is_redacted_from_success_logs(caplog):
    client = server.app.test_client()

    with caplog.at_level(logging.INFO, logger="tidalhires"):
        assert client.get(f"/api?t=caps&apikey={VALID_KEY}").status_code == 200

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert VALID_KEY not in log_text
    assert "'apikey': '<redacted>'" in log_text


def test_health_does_not_expose_tidal_user(mocker):
    client = server.app.test_client()
    mock_session = mocker.Mock()
    mock_session.user.username = "tidal-user@example.com"
    mocker.patch.object(server, "_get_session", return_value=mock_session)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.get_json()
    assert body == {"active_jobs": 0, "status": "ok"}
    assert "tidal_user" not in body


def test_health_does_not_expose_error_details(mocker):
    client = server.app.test_client()
    mocker.patch.object(
        server, "_get_session", side_effect=RuntimeError("secret token path")
    )

    response = client.get("/health")

    assert response.status_code == 503
    body = response.get_json()
    assert body == {"status": "degraded"}
    assert "error" not in body


def test_unknown_routes_require_apikey_and_redact_query(caplog):
    client = server.app.test_client()

    assert client.get("/does-not-exist?apikey=wrong").status_code == 401

    with caplog.at_level(logging.WARNING, logger="tidalhires"):
        response = client.get(f"/does-not-exist?apikey={VALID_KEY}&token=secret-token")

    assert response.status_code == 404
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert VALID_KEY not in log_text
    assert "secret-token" not in log_text
    assert "'apikey': '<redacted>'" in log_text
    assert "'token': '<redacted>'" in log_text
