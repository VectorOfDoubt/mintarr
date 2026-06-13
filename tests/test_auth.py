"""Auth regression tests for existing Batch D endpoints."""

from __future__ import annotations

import json
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


def _seed_active_job(mocker, jid, job_id=4242):
    """Seed a visible job in _jobs and a matching active SQLite job (mocked)."""
    import state_db

    mocker.patch.object(server, "_save_jobs")
    server._jobs[jid] = {"id": jid, "status": "downloading", "title": "X"}
    mocker.patch.object(
        state_db, "get_active_job_by_jid", return_value={"id": job_id, "jid": jid}
    )
    cancel = mocker.patch.object(state_db, "request_job_cancel", return_value=True)
    return cancel


def test_sab_history_name_delete_cancels_and_hides(mocker):
    """Lidarr blocklist-delete arrives as mode=history&name=delete — must cancel, not list."""
    client = server.app.test_client()
    cancel = _seed_active_job(mocker, "hdel123")
    try:
        resp = client.get(
            f"/sabnzbd/api?mode=history&name=delete&value=hdel123&apikey={VALID_KEY}"
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"status": True}
        cancel.assert_called_once_with(4242)
        assert server._jobs["hdel123"]["hidden_from_lidarr"] is True
    finally:
        server._jobs.pop("hdel123", None)


def test_sab_queue_name_delete_cancels(mocker):
    client = server.app.test_client()
    cancel = _seed_active_job(mocker, "qdel123")
    try:
        resp = client.get(
            f"/sabnzbd/api?mode=queue&name=delete&value=qdel123&apikey={VALID_KEY}"
        )
        assert resp.status_code == 200
        cancel.assert_called_once_with(4242)
        assert server._jobs["qdel123"]["hidden_from_lidarr"] is True
    finally:
        server._jobs.pop("qdel123", None)


def test_sab_bare_delete_cancels_active_job(mocker):
    client = server.app.test_client()
    cancel = _seed_active_job(mocker, "bdel123")
    try:
        resp = client.get(f"/sabnzbd/api?mode=delete&value=bdel123&apikey={VALID_KEY}")
        assert resp.status_code == 200
        cancel.assert_called_once_with(4242)
        assert server._jobs["bdel123"]["hidden_from_lidarr"] is True
    finally:
        server._jobs.pop("bdel123", None)


def test_sab_delete_creates_album_hold_from_target_album_id(mocker):
    import state_db

    client = server.app.test_client()
    mocker.patch.object(server, "_save_jobs")
    server._jobs["holdpayload"] = {
        "id": "holdpayload",
        "status": "downloading",
        "title": "X",
        "source_type": "tidal",
        "source_id": "tidal-release",
    }
    mocker.patch.object(
        state_db,
        "get_active_job_by_jid",
        return_value={
            "id": 4242,
            "jid": "holdpayload",
            "source_type": "tidal",
            "source_id": "tidal-release",
            "payload_json": json.dumps({"target_album_id": 777}),
        },
    )
    mocker.patch.object(state_db, "request_job_cancel", return_value=True)
    create_hold = mocker.patch.object(state_db, "create_album_hold")
    queue_lookup = mocker.patch.object(server, "_lidarr_queue_record_for_download_id")

    try:
        resp = client.get(
            f"/sabnzbd/api?mode=delete&value=holdpayload&apikey={VALID_KEY}"
        )

        assert resp.status_code == 200
        queue_lookup.assert_not_called()
        create_hold.assert_called_once()
        assert create_hold.call_args.args == (777,)
        kwargs = create_hold.call_args.kwargs
        assert kwargs["reason"] == "operator_cancelled_active_grab"
        assert kwargs["source_jid"] == "holdpayload"
        assert kwargs["source_type"] == "tidal"
        assert kwargs["source_id"] == "tidal-release"
        assert kwargs["actor"] == "lidarr_delete"
        assert kwargs["details"] == {"download_id": "holdpayload"}
        assert server._jobs["holdpayload"]["hidden_from_lidarr"] is True
    finally:
        server._jobs.pop("holdpayload", None)


def test_sab_delete_persists_album_hold_with_real_state_db(tmp_path, mocker):
    import state_db

    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    job_id = state_db.enqueue_job(
        jid="realhold",
        type="tidal_grab",
        payload={"target_album_id": 909},
        source_type="tidal",
        source_id="tidal-release",
    )
    assert job_id is not None
    mocker.patch.object(server, "_save_jobs")
    server._jobs["realhold"] = {
        "id": "realhold",
        "status": "downloading",
        "title": "X",
        "source_type": "tidal",
        "source_id": "tidal-release",
    }
    client = server.app.test_client()

    try:
        resp = client.get(f"/sabnzbd/api?mode=delete&value=realhold&apikey={VALID_KEY}")

        assert resp.status_code == 200
        job = state_db.get_job(job_id)
        assert job is not None
        assert job["cancel_requested"] == 1
        hold = state_db.get_album_hold(909)
        assert hold is not None
        assert hold["reason"] == "operator_cancelled_active_grab"
        assert hold["source_jid"] == "realhold"
        assert hold["source_type"] == "tidal"
        assert hold["source_id"] == "tidal-release"
        assert hold["actor"] == "lidarr_delete"
        assert hold["details"] == {"download_id": "realhold"}
    finally:
        server._jobs.pop("realhold", None)
        state_db._initialized = False


def test_sab_delete_creates_album_hold_from_lidarr_queue_record(mocker):
    import state_db

    client = server.app.test_client()
    mocker.patch.object(server, "_save_jobs")
    server._jobs["holdqueue"] = {
        "id": "holdqueue",
        "status": "downloading",
        "title": "X",
    }
    mocker.patch.object(
        state_db,
        "get_active_job_by_jid",
        return_value={
            "id": 4242,
            "jid": "holdqueue",
            "source_type": "tidal",
            "source_id": "tidal-release",
            "payload_json": json.dumps({"source_id": "tidal-release"}),
        },
    )
    mocker.patch.object(state_db, "request_job_cancel", return_value=True)
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(
        server,
        "_lidarr_queue_record_for_download_id",
        return_value={
            "id": 99,
            "downloadId": "holdqueue",
            "title": "Release title",
            "album": {
                "id": 888,
                "title": "Album title",
                "artist": {"artistName": "Artist name"},
            },
        },
    )
    create_hold = mocker.patch.object(state_db, "create_album_hold")

    try:
        resp = client.get(
            f"/sabnzbd/api?mode=history&name=delete&value=holdqueue&apikey={VALID_KEY}"
        )

        assert resp.status_code == 200
        create_hold.assert_called_once()
        assert create_hold.call_args.args == (888,)
        kwargs = create_hold.call_args.kwargs
        assert kwargs["details"] == {
            "album_title": "Album title",
            "artist": "Artist name",
            "download_title": "Release title",
            "lidarr_queue_id": 99,
            "download_id": "holdqueue",
        }
    finally:
        server._jobs.pop("holdqueue", None)


def test_sab_delete_without_trusted_album_id_does_not_create_album_hold(mocker):
    import state_db

    client = server.app.test_client()
    mocker.patch.object(server, "_save_jobs")
    server._jobs["noalbumhold"] = {
        "id": "noalbumhold",
        "status": "downloading",
        "title": "X",
    }
    mocker.patch.object(
        state_db,
        "get_active_job_by_jid",
        return_value={
            "id": 4242,
            "jid": "noalbumhold",
            "payload_json": json.dumps({"source_id": "tidal-release"}),
        },
    )
    mocker.patch.object(state_db, "request_job_cancel", return_value=True)
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_lidarr_queue_record_for_download_id", return_value={})
    create_hold = mocker.patch.object(state_db, "create_album_hold")

    try:
        resp = client.get(
            f"/sabnzbd/api?mode=queue&name=delete&value=noalbumhold&apikey={VALID_KEY}"
        )

        assert resp.status_code == 200
        create_hold.assert_not_called()
        assert server._jobs["noalbumhold"]["hidden_from_lidarr"] is True
    finally:
        server._jobs.pop("noalbumhold", None)


def test_sab_delete_no_active_job_just_hides(mocker):
    """History cleanup of a terminal job: no active job → hide only, no cancel error."""
    import state_db

    client = server.app.test_client()
    mocker.patch.object(server, "_save_jobs")
    server._jobs["term123"] = {"id": "term123", "status": "completed", "title": "X"}
    mocker.patch.object(state_db, "get_active_job_by_jid", return_value=None)
    cancel = mocker.patch.object(state_db, "request_job_cancel")
    try:
        resp = client.get(f"/sabnzbd/api?mode=delete&value=term123&apikey={VALID_KEY}")
        assert resp.status_code == 200
        cancel.assert_not_called()
        assert server._jobs["term123"]["hidden_from_lidarr"] is True
    finally:
        server._jobs.pop("term123", None)


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


def test_sab_queue_shows_review_hold_as_paused(mocker):
    """Review-hold invariant: a pending review stays visible to Lidarr as Paused so
    Lidarr does not re-grab the album while the operator decides."""
    client = server.app.test_client()
    mocker.patch.object(server, "_save_jobs")
    server._jobs["rev123"] = {
        "id": "rev123",
        "status": "review_required",
        "lidarr_hold": True,
        "title": "Held For Review",
        "category": "music",
        "size": 50 * 1024 * 1024,
        "percent": 100,
    }
    try:
        response = client.get(f"/sabnzbd/api?mode=queue&apikey={VALID_KEY}")
        assert response.status_code == 200
        slots = {s["nzo_id"]: s for s in response.get_json()["queue"]["slots"]}
        assert "rev123" in slots, "review-held job must stay visible to Lidarr"
        assert slots["rev123"]["status"] == "Paused"
        assert slots["rev123"]["sizeleft"] == "0"
    finally:
        server._jobs.pop("rev123", None)


def test_sab_history_excludes_review_hold(mocker):
    """A review-held job must not appear as completed in history (would trigger import)."""
    client = server.app.test_client()
    mocker.patch.object(server, "_save_jobs")
    server._jobs["rev456"] = {
        "id": "rev456",
        "status": "review_required",
        "lidarr_hold": True,
        "title": "Held",
        "completed_at": 1,
    }
    try:
        response = client.get(f"/sabnzbd/api?mode=history&apikey={VALID_KEY}")
        assert response.status_code == 200
        names = [s["nzo_id"] for s in response.get_json()["history"]["slots"]]
        assert "rev456" not in names
    finally:
        server._jobs.pop("rev456", None)


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
