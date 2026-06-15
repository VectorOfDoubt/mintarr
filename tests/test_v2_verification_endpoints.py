"""Tests for V2 verification sidecars and endpoints."""

from __future__ import annotations

import json
import time

import pytest

import server
import state_db
import worker
from verification import VerificationResult


VALID_KEY = "tidalhires-test-api-key"


def _result(
    jid="abc12345", decision="REVIEW_REQUIRED", outcome="PENDING", overrides=None
):
    return VerificationResult(
        jid=jid,
        score=0 if decision == "BLOCK" else 60,
        verification_decision=decision,
        import_outcome=outcome,
        components={"ffprobe": 25, "flac_t": 25, "detective": 0, "complete": 10},
        overrides=overrides or [],
        verdict="FAKE_CERTAIN" if decision == "REVIEW_REQUIRED" else "AUTHENTIC",
        new_kbps=192,
        existing_kbps=0,
        existing_label="nothing",
        album_ids=[20],
        title="Artist - Album",
    )


def _patch_paths(monkeypatch, tmp_path):
    output_base = tmp_path / "output"
    monkeypatch.setattr(server, "OUTPUT_BASE", output_base)
    monkeypatch.setattr(server, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(server, "BLOCKED_DECISIONS_DIR", tmp_path / "blocked")
    monkeypatch.setattr(server, "DISCARDED_DIR", tmp_path / "discarded")
    monkeypatch.setattr(server, "EXPIRED_REVIEW_DIR", tmp_path / "expired")
    return output_base


def test_verification_routes_require_apikey(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()

    assert client.get("/verification").status_code == 401
    assert client.get("/verification/abc12345").status_code == 401
    assert client.post("/verification/abc12345/promote").status_code == 401
    assert client.post("/verification/abc12345/release-switch").status_code == 401
    assert client.post("/verification/abc12345/retry-import").status_code == 401
    assert client.post("/verification/abc12345/discard").status_code == 401


def test_verification_get_and_list_filters_sidecars(tmp_path, monkeypatch):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("abc12345", _result(), output_dir)
    server._write_verification_sidecar(
        "blocked1",
        _result(jid="blocked1", decision="BLOCK", outcome="SKIPPED"),
        output_base / "blocked1",
        archive_dir=server.BLOCKED_DECISIONS_DIR,
    )

    client = server.app.test_client()
    one = client.get(f"/verification/abc12345?apikey={VALID_KEY}")
    assert one.status_code == 200
    assert one.get_json()["v2_verification_decision"] == "REVIEW_REQUIRED"

    listed = client.get(f"/verification?decision=BLOCK&apikey={VALID_KEY}")
    body = listed.get_json()
    assert listed.status_code == 200
    assert body["count"] == 1
    assert body["verification"][0]["jid"] == "blocked1"


def test_decisions_endpoint_overlays_current_sidecar_lifecycle(tmp_path, monkeypatch):
    output_base = _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "DECISIONS_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setenv("V2_VERIFICATION_ENABLED", "true")
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    result = _result()
    server._write_verification_sidecar("abc12345", result, output_dir)
    server._log_decision("abc12345", v2_result=result)

    archived = server.DISCARDED_DIR / "abc12345.json"
    sidecar = json.loads((output_dir / "verification.json").read_text())
    sidecar["lifecycle"]["state"] = "discarded"
    sidecar["lifecycle"]["actor"] = "user_discard"
    server._atomic_write_json(archived, sidecar)
    (output_dir / "verification.json").unlink()

    client = server.app.test_client()
    response = client.get(f"/decisions?apikey={VALID_KEY}")

    assert response.status_code == 200
    [record] = response.get_json()["decisions"]
    assert record["jid"] == "abc12345"
    assert record["verification_state"] == "discarded"
    assert record["lifecycle"]["actor"] == "user_discard"


def test_verification_get_reconciles_pending_import_from_lidarr_history(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    path = server._write_verification_sidecar(
        "abc12345",
        _result(
            decision="ACCEPT_PROVISIONAL",
            outcome="PENDING",
            overrides=["manual_promote"],
        ),
        output_dir,
    )
    server._jobs["abc12345"] = {"id": "abc12345", "output_dir": str(output_dir)}

    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_count_lidarr_imported_history", return_value=10)
    cleanup = mocker.patch.object(server, "_cleanup_lidarr_queue")
    mocker.patch.object(server, "_save_jobs")

    client = server.app.test_client()
    response = client.get(f"/verification/abc12345?apikey={VALID_KEY}")

    assert response.status_code == 200
    assert response.get_json()["v2_import_outcome"] == "MANUAL_IMPORTED"
    assert json.loads(path.read_text())["v2_import_outcome"] == "MANUAL_IMPORTED"
    assert server._jobs["abc12345"]["status"] == "completed"
    cleanup.assert_not_called()


def test_verification_get_marks_orphaned_pending_import_failed(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    path = server._write_verification_sidecar(
        "abc12345",
        _result(decision="ACCEPT", outcome="PENDING"),
        output_dir,
    )
    server._jobs["abc12345"] = {
        "id": "abc12345",
        "output_dir": str(output_dir),
        "status": "processing",
    }

    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_count_lidarr_imported_history", return_value=0)
    mocker.patch.object(server, "_count_lidarr_trackfiles", return_value=0)
    mocker.patch.object(
        server, "_lidarr_has_pending_import_for_jid", return_value=False
    )
    cleanup = mocker.patch.object(server, "_cleanup_lidarr_queue")
    mocker.patch.object(server, "_save_jobs")

    client = server.app.test_client()
    response = client.get(f"/verification/abc12345?apikey={VALID_KEY}")

    assert response.status_code == 200
    assert response.get_json()["v2_import_outcome"] == "FAILED"
    assert json.loads(path.read_text())["v2_import_outcome"] == "FAILED"
    assert server._jobs["abc12345"]["status"] == "failed"
    assert (
        server._jobs["abc12345"]["error"]
        == "lidarr manualimport ended without importing files"
    )
    cleanup.assert_called_once_with("abc12345", "http://lidarr/api/v1", "lidarr-key")


def test_queue_reconcile_handles_human_pending_progress_message(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    path = server._write_verification_sidecar(
        "abc12345",
        _result(decision="ACCEPT", outcome="PENDING"),
        output_dir,
    )
    server._jobs["abc12345"] = {
        "id": "abc12345",
        "output_dir": str(output_dir),
        "status": "processing",
        "stage": "pending",
        "warning": "Lidarr ManualImport is still pending; retry is available if it does not settle.",
    }

    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_count_lidarr_imported_history", return_value=0)
    mocker.patch.object(server, "_count_lidarr_trackfiles", return_value=0)
    mocker.patch.object(
        server, "_lidarr_has_pending_import_for_jid", return_value=False
    )
    cleanup = mocker.patch.object(server, "_cleanup_lidarr_queue")
    mocker.patch.object(server, "_save_jobs")

    server._reconcile_pending_import_jobs()

    assert json.loads(path.read_text())["v2_import_outcome"] == "FAILED"
    assert server._jobs["abc12345"]["status"] == "failed"
    assert (
        server._jobs["abc12345"]["error"]
        == "lidarr manualimport ended without importing files"
    )
    cleanup.assert_called_once_with("abc12345", "http://lidarr/api/v1", "lidarr-key")


def test_verification_get_reconciles_imported_trackfiles_only_when_sources_moved(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    path = server._write_verification_sidecar(
        "abc12345",
        _result(decision="ACCEPT", outcome="PENDING"),
        output_dir,
    )
    server._jobs["abc12345"] = {
        "id": "abc12345",
        "output_dir": str(output_dir),
        "status": "processing",
    }

    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_count_lidarr_imported_history", return_value=0)
    mocker.patch.object(server, "_count_lidarr_trackfiles", return_value=8)
    mocker.patch.object(
        server, "_lidarr_has_pending_import_for_jid", return_value=False
    )
    cleanup = mocker.patch.object(server, "_cleanup_lidarr_queue")
    mocker.patch.object(server, "_save_jobs")

    client = server.app.test_client()
    response = client.get(f"/verification/abc12345?apikey={VALID_KEY}")

    assert response.status_code == 200
    assert response.get_json()["v2_import_outcome"] == "MANUAL_IMPORTED"
    assert json.loads(path.read_text())["v2_import_outcome"] == "MANUAL_IMPORTED"
    assert server._jobs["abc12345"]["status"] == "completed"
    cleanup.assert_not_called()


def test_verification_get_does_not_treat_existing_trackfiles_as_import_success(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"flac")
    path = server._write_verification_sidecar(
        "abc12345",
        _result(decision="ACCEPT", outcome="PENDING"),
        output_dir,
    )
    server._jobs["abc12345"] = {
        "id": "abc12345",
        "output_dir": str(output_dir),
        "status": "processing",
    }

    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_count_lidarr_imported_history", return_value=0)
    mocker.patch.object(server, "_count_lidarr_trackfiles", return_value=8)
    mocker.patch.object(
        server, "_lidarr_has_pending_import_for_jid", return_value=False
    )
    cleanup = mocker.patch.object(server, "_cleanup_lidarr_queue")
    mocker.patch.object(server, "_save_jobs")

    client = server.app.test_client()
    response = client.get(f"/verification/abc12345?apikey={VALID_KEY}")

    assert response.status_code == 200
    assert response.get_json()["v2_import_outcome"] == "FAILED"
    assert json.loads(path.read_text())["v2_import_outcome"] == "FAILED"
    assert server._jobs["abc12345"]["status"] == "failed"
    cleanup.assert_called_once_with("abc12345", "http://lidarr/api/v1", "lidarr-key")


def test_accepted_sidecar_does_not_create_pending_blocklist_work(tmp_path, monkeypatch):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "abc12345",
        _result(decision="ACCEPT", outcome="MANUAL_IMPORTED"),
        output_dir,
    )

    record = json.loads((output_dir / "verification.json").read_text())

    assert record["lifecycle"]["state"] == "created"
    assert record["lifecycle"]["blocklist_policy"] == "skipped"
    assert record["lifecycle"]["blocklist_status"] == "skipped"


def test_verification_list_can_render_review_dashboard(tmp_path, monkeypatch):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("abc12345", _result(), output_dir)

    client = server.app.test_client()
    response = client.get(
        f"/verification?decision=REVIEW_REQUIRED&apikey={VALID_KEY}",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    text = response.get_data(as_text=True)
    assert "Mintarr Verification Dashboard" in text
    assert "Needs action" in text
    assert "Active jobs" in text
    assert "All verification records" in text
    assert "/verification/abc12345/promote" in text
    assert "/verification/abc12345/retry-import" in text
    assert "/verification/abc12345/discard" in text


def test_retry_import_reruns_verified_accept_without_recomputing_verification(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"flac")
    path = server._write_verification_sidecar(
        "abc12345",
        _result(decision="ACCEPT", outcome="FAILED"),
        output_dir,
    )
    server._jobs["abc12345"] = {
        "id": "abc12345",
        "status": "failed",
        "hidden_from_lidarr": True,
        "output_dir": str(output_dir),
    }
    import_only = mocker.patch.object(
        server, "_run_manual_import_only", return_value="MANUAL_IMPORTED"
    )
    mocker.patch.object(server, "_save_jobs")

    client = server.app.test_client()
    response = client.post(f"/verification/abc12345/retry-import?apikey={VALID_KEY}")

    assert response.status_code == 202
    body = response.get_json()
    assert body["job_type"] == "retry_import"
    assert body["message"] == "retry_import queued"
    import_only.assert_not_called()
    total, jobs = state_db.list_jobs(type=["retry_import"])
    assert total == 1
    assert jobs[0]["jid"] == "abc12345"
    record = json.loads(path.read_text())
    assert record["v2_verification_decision"] == "ACCEPT"
    assert record["v2_import_outcome"] == "FAILED"


def test_retry_import_rejects_review_required(tmp_path, monkeypatch, mocker):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("abc12345", _result(), output_dir)
    import_only = mocker.patch.object(server, "_run_manual_import_only")

    client = server.app.test_client()
    response = client.post(f"/verification/abc12345/retry-import?apikey={VALID_KEY}")

    assert response.status_code == 409
    assert response.get_json()["error"] == "verification is not retryable"
    import_only.assert_not_called()


def test_promote_updates_sidecar_without_recomputing_verification(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    path = server._write_verification_sidecar("abc12345", _result(), output_dir)
    server._jobs["abc12345"] = {"id": "abc12345", "output_dir": str(output_dir)}
    import_only = mocker.patch.object(
        server, "_run_manual_import_only", return_value="MANUAL_IMPORTED"
    )

    client = server.app.test_client()
    response = client.post(f"/verification/abc12345/promote?apikey={VALID_KEY}")

    assert response.status_code == 202
    body = response.get_json()
    assert body["job_type"] == "promote_import"
    assert body["message"] == "promote queued"
    import_only.assert_not_called()
    total, jobs = state_db.list_jobs(type=["promote_import"])
    assert total == 1
    assert jobs[0]["jid"] == "abc12345"
    result_state, payload = server._execute_promote_import_job(jobs[0])
    assert result_state == "promoted"
    assert payload["import_outcome"] == "MANUAL_IMPORTED"
    import_only.assert_called_once_with(
        "abc12345", output_dir, worker_job_id=jobs[0]["id"]
    )
    record = json.loads(path.read_text())
    assert record["v2_verification_decision"] == "ACCEPT_PROVISIONAL"
    assert record["v2_import_outcome"] == "MANUAL_IMPORTED"
    assert "manual_promote" in record["v2_overrides"]
    assert record["lifecycle"]["state"] == "promoted"


def test_promote_is_idempotent_when_already_imported(tmp_path, monkeypatch, mocker):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "abc12345",
        _result(
            decision="ACCEPT_PROVISIONAL",
            outcome="MANUAL_IMPORTED",
            overrides=["manual_promote"],
        ),
        output_dir,
    )
    import_only = mocker.patch.object(server, "_run_manual_import_only")

    client = server.app.test_client()
    response = client.post(f"/verification/abc12345/promote?apikey={VALID_KEY}")

    assert response.status_code == 200
    assert response.get_json()["message"] == "already imported"
    import_only.assert_not_called()


def test_promote_failure_keeps_sidecar_retryable(tmp_path, monkeypatch, mocker):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    path = server._write_verification_sidecar("abc12345", _result(), output_dir)
    server._jobs["abc12345"] = {"id": "abc12345", "output_dir": str(output_dir)}
    import_only = mocker.patch.object(
        server, "_run_manual_import_only", return_value="FAILED"
    )

    result_state, payload = server._execute_promote_import_job(
        {"id": 1, "jid": "abc12345", "payload_json": "{}"}
    )

    assert result_state == "failed"
    assert payload["import_outcome"] == "FAILED"
    import_only.assert_called_once_with("abc12345", output_dir, worker_job_id=1)
    record = json.loads(path.read_text())
    assert record["v2_verification_decision"] == "ACCEPT_PROVISIONAL"
    assert record["v2_import_outcome"] == "FAILED"
    assert "manual_promote" in record["v2_overrides"]
    assert record["lifecycle"]["state"] == "promoted"


def _release_switch_record(
    jid: str,
    *,
    identity_decision: str = "AMBIGUOUS_EDITION",
    decision: str = "REVIEW_REQUIRED",
    outcome: str = "PENDING",
    overrides: list[str] | None = None,
) -> VerificationResult:
    result = _result(jid=jid, decision=decision, outcome=outcome, overrides=overrides)
    result.identity_decision = identity_decision
    result.identity_confidence = 60.0
    result.identity_reasons = ["ambiguous edition"]
    result.identity_current_release_id = 30
    result.identity_best_release_id = 40
    return result


def test_release_switch_endpoint_rejects_guard_failures(tmp_path, monkeypatch, mocker):
    output_base = _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    put_mock = mocker.patch("requests.put")

    cases = [
        ("disabled", "AMBIGUOUS_EDITION", "REVIEW_REQUIRED", [], 409),
        ("review", "WRONG_ALBUM", "REVIEW_REQUIRED", [], 409),
        ("review", "AMBIGUOUS_EDITION", "BLOCK", [], 409),
        ("review", "AMBIGUOUS_EDITION", "REVIEW_REQUIRED", ["codec_mismatch"], 409),
    ]
    for idx, (strategy, identity, decision, overrides, expected) in enumerate(cases):
        jid = f"swguard{idx}"
        output_dir = output_base / jid
        output_dir.mkdir(parents=True)
        (output_dir / "01.flac").write_bytes(b"flac")
        server._write_verification_sidecar(
            jid,
            _release_switch_record(
                jid,
                identity_decision=identity,
                decision=decision,
                overrides=overrides,
            ),
            output_dir,
        )
        server._jobs[jid] = {"id": jid, "output_dir": str(output_dir)}
        monkeypatch.setenv("MINTARR_RELEASE_SWITCH_STRATEGY", strategy)

        response = client.post(f"/verification/{jid}/release-switch?apikey={VALID_KEY}")

        assert response.status_code == expected

    put_mock.assert_not_called()


def test_release_switch_endpoint_applies_and_imports(tmp_path, monkeypatch, mocker):
    output_base = _patch_paths(monkeypatch, tmp_path)
    jid = "swapply1"
    output_dir = output_base / jid
    output_dir.mkdir(parents=True)
    for idx in range(1, 5):
        (output_dir / f"{idx:02d} - Track {idx}.flac").write_bytes(b"flac")
    path = server._write_verification_sidecar(
        jid, _release_switch_record(jid), output_dir
    )
    server._jobs[jid] = {"id": jid, "output_dir": str(output_dir)}
    monkeypatch.setenv("MINTARR_RELEASE_SWITCH_STRATEGY", "review")
    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server.time, "sleep")
    get_mock = mocker.patch(
        "requests.get",
        return_value=type(
            "Resp",
            (),
            {
                "status_code": 200,
                "json": lambda self: [{"album": {"id": 20}}],
            },
        )(),
    )
    candidate = {
        "album_id": 20,
        "current_release_id": 30,
        "best_release_id": 40,
        "best_score": 88.0,
    }
    candidate_mock = mocker.patch.object(
        server, "_release_switch_candidate_for_album", return_value=candidate
    )
    audit = {"album_id": 20, "old_release_id": 30, "new_release_id": 40}
    apply_mock = mocker.patch.object(
        server, "_apply_release_switch_candidate", return_value=audit
    )
    promote_mock = mocker.patch.object(
        server,
        "_promote_verified_import",
        return_value=({"jid": jid, "import_outcome": "MANUAL_IMPORTED"}, 200),
    )
    restore_mock = mocker.patch.object(server, "_restore_release_switches")

    client = server.app.test_client()
    response = client.post(f"/verification/{jid}/release-switch?apikey={VALID_KEY}")

    assert response.status_code == 200
    body = response.get_json()
    assert body["switched"] is True
    assert body["release_switch_count"] == 1
    get_mock.assert_called_once()
    candidate_mock.assert_called_once()
    apply_mock.assert_called_once_with(
        jid,
        candidate,
        api="http://lidarr/api/v1",
        key="lidarr-key",
        mode="review",
        actor="operator_review",
        reasons=["operator-approved review-mode switch"],
    )
    promote_mock.assert_called_once_with(jid, json.loads(path.read_text()), path)
    restore_mock.assert_not_called()


def test_release_switch_endpoint_restores_on_import_failure(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    jid = "swfail1"
    output_dir = output_base / jid
    output_dir.mkdir(parents=True)
    for idx in range(1, 5):
        (output_dir / f"{idx:02d} - Track {idx}.flac").write_bytes(b"flac")
    path = server._write_verification_sidecar(
        jid, _release_switch_record(jid), output_dir
    )
    server._jobs[jid] = {"id": jid, "output_dir": str(output_dir)}
    monkeypatch.setenv("MINTARR_RELEASE_SWITCH_STRATEGY", "review")
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server.time, "sleep")
    mocker.patch(
        "requests.get",
        return_value=type(
            "Resp",
            (),
            {
                "status_code": 200,
                "json": lambda self: [{"album": {"id": 20}}],
            },
        )(),
    )
    mocker.patch.object(
        server,
        "_release_switch_candidate_for_album",
        return_value={
            "album_id": 20,
            "current_release_id": 30,
            "best_release_id": 40,
        },
    )
    audit = {"album_id": 20, "old_release_id": 30, "new_release_id": 40}
    mocker.patch.object(server, "_apply_release_switch_candidate", return_value=audit)
    mocker.patch.object(
        server,
        "_promote_verified_import",
        return_value=({"jid": jid, "import_outcome": "FAILED"}, 200),
    )
    restore_mock = mocker.patch.object(server, "_restore_release_switches")

    client = server.app.test_client()
    response = client.post(f"/verification/{jid}/release-switch?apikey={VALID_KEY}")

    assert response.status_code == 200
    assert response.get_json()["import_outcome"] == "FAILED"
    restore_mock.assert_called_once_with(
        jid,
        [audit],
        api="http://host.docker.internal:8686/api/v1",
        key="lidarr-key",
        trigger="operator_import_failed",
    )
    assert json.loads(path.read_text())["v2_verification_decision"] == "REVIEW_REQUIRED"


def test_release_switch_endpoint_restores_when_import_raises(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    jid = "swraise1"
    output_dir = output_base / jid
    output_dir.mkdir(parents=True)
    for idx in range(1, 5):
        (output_dir / f"{idx:02d} - Track {idx}.flac").write_bytes(b"flac")
    server._write_verification_sidecar(jid, _release_switch_record(jid), output_dir)
    server._jobs[jid] = {"id": jid, "output_dir": str(output_dir)}
    monkeypatch.setenv("MINTARR_RELEASE_SWITCH_STRATEGY", "review")
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server.time, "sleep")
    mocker.patch(
        "requests.get",
        return_value=type(
            "Resp",
            (),
            {"status_code": 200, "json": lambda self: [{"album": {"id": 20}}]},
        )(),
    )
    mocker.patch.object(
        server,
        "_release_switch_candidate_for_album",
        return_value={"album_id": 20, "current_release_id": 30, "best_release_id": 40},
    )
    audit = {"album_id": 20, "old_release_id": 30, "new_release_id": 40}
    mocker.patch.object(server, "_apply_release_switch_candidate", return_value=audit)
    mocker.patch.object(
        server, "_promote_verified_import", side_effect=RuntimeError("boom")
    )
    restore_mock = mocker.patch.object(server, "_restore_release_switches")

    client = server.app.test_client()
    response = client.post(f"/verification/{jid}/release-switch?apikey={VALID_KEY}")

    assert response.status_code == 500
    restore_mock.assert_called_once_with(
        jid,
        [audit],
        api="http://host.docker.internal:8686/api/v1",
        key="lidarr-key",
        trigger="operator_import_failed",
    )


def test_promote_retries_after_failed_manual_promote(tmp_path, monkeypatch, mocker):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    path = server._write_verification_sidecar(
        "abc12345",
        _result(
            decision="ACCEPT_PROVISIONAL",
            outcome="FAILED",
            overrides=["manual_promote"],
        ),
        output_dir,
    )
    server._jobs["abc12345"] = {"id": "abc12345", "output_dir": str(output_dir)}
    import_only = mocker.patch.object(
        server, "_run_manual_import_only", return_value="RESCUED"
    )

    result_state, payload = server._execute_promote_import_job(
        {"id": 1, "jid": "abc12345", "payload_json": "{}"}
    )

    assert result_state == "promoted"
    assert payload["import_outcome"] == "RESCUED"
    import_only.assert_called_once_with("abc12345", output_dir, worker_job_id=1)
    record = json.loads(path.read_text())
    assert record["v2_verification_decision"] == "ACCEPT_PROVISIONAL"
    assert record["v2_import_outcome"] == "RESCUED"
    assert record["v2_overrides"].count("manual_promote") == 1


def test_promote_retries_after_pending_manual_promote(tmp_path, monkeypatch, mocker):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    path = server._write_verification_sidecar(
        "abc12345",
        _result(
            decision="ACCEPT_PROVISIONAL",
            outcome="PENDING",
            overrides=["manual_promote"],
        ),
        output_dir,
    )
    server._jobs["abc12345"] = {"id": "abc12345", "output_dir": str(output_dir)}
    import_only = mocker.patch.object(
        server, "_run_manual_import_only", return_value="MANUAL_IMPORTED"
    )

    result_state, payload = server._execute_promote_import_job(
        {"id": 1, "jid": "abc12345", "payload_json": "{}"}
    )

    assert result_state == "promoted"
    assert payload["import_outcome"] == "MANUAL_IMPORTED"
    import_only.assert_called_once_with("abc12345", output_dir, worker_job_id=1)
    record = json.loads(path.read_text())
    assert record["v2_import_outcome"] == "MANUAL_IMPORTED"
    assert record["v2_overrides"].count("manual_promote") == 1


def test_promote_worker_honors_cancel_before_manual_import(
    tmp_path, monkeypatch, mocker
):
    state_db.init(db_path=tmp_path / "state.db")
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "cancelpr1"
    output_dir.mkdir(parents=True)
    path = server._write_verification_sidecar(
        "cancelpr1", _result(jid="cancelpr1"), output_dir
    )
    server._jobs["cancelpr1"] = {"id": "cancelpr1", "output_dir": str(output_dir)}
    import_only = mocker.patch.object(server, "_run_manual_import_only")

    job_id = state_db.enqueue_job(jid="cancelpr1", type="promote_import")
    state_db.dequeue_next_job(worker_id="worker-1")
    state_db.request_job_cancel(job_id)

    with pytest.raises(worker.JobCancelled):
        server._execute_promote_import_job(
            {
                "id": job_id,
                "jid": "cancelpr1",
                "payload_json": "{}",
            }
        )

    import_only.assert_not_called()
    assert output_dir.exists()
    record = json.loads(path.read_text())
    assert record["v2_import_outcome"] == "PENDING"


def test_worker_executes_promote_import_job(tmp_path, monkeypatch, mocker):
    state_db.init(db_path=tmp_path / "state.db")
    output_base = _patch_paths(monkeypatch, tmp_path)
    jid = "wrkprom1"
    output_dir = output_base / jid
    output_dir.mkdir(parents=True)
    path = server._write_verification_sidecar(jid, _result(jid=jid), output_dir)
    server._jobs[jid] = {"id": jid, "output_dir": str(output_dir)}
    import_only = mocker.patch.object(
        server, "_run_manual_import_only", return_value="MANUAL_IMPORTED"
    )

    worker.stop_worker(timeout=2)
    worker.register_executor("promote_import", server._execute_promote_import_job)
    job_id = state_db.enqueue_job(
        jid=jid,
        type="promote_import",
        dedupe_key=f"verification:promote:{jid}",
    )
    worker.start_worker()
    try:
        for _ in range(30):
            job = state_db.get_job(job_id)
            if job and job["state"] == "completed":
                break
            time.sleep(0.05)
        else:
            assert False, state_db.get_job(job_id)
    finally:
        worker.stop_worker(timeout=2)

    job = state_db.get_job(job_id)
    assert job["result_state"] == "promoted"
    import_only.assert_called_once_with(jid, output_dir, worker_job_id=job_id)
    record = json.loads(path.read_text())
    assert record["v2_import_outcome"] == "MANUAL_IMPORTED"


def test_discard_deletes_output_and_archives_sidecar(tmp_path, monkeypatch, mocker):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"flac")
    server._write_verification_sidecar("abc12345", _result(), output_dir)
    server._jobs["abc12345"] = {"id": "abc12345", "output_dir": str(output_dir)}
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_blocklist_grab", return_value=True)
    mocker.patch.object(server, "_save_jobs")

    client = server.app.test_client()
    response = client.post(f"/verification/abc12345/discard?apikey={VALID_KEY}")

    assert response.status_code == 200
    assert not output_dir.exists()
    archived = server.DISCARDED_DIR / "abc12345.json"
    assert archived.exists()
    record = json.loads(archived.read_text())
    assert record["lifecycle"]["state"] == "discarded"
    assert record["lifecycle"]["blocklist_status"] == "done"


def test_discard_preserves_existing_blocklist_done(tmp_path, monkeypatch, mocker):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"flac")
    path = server._write_verification_sidecar("abc12345", _result(), output_dir)
    record = json.loads(path.read_text())
    record["lifecycle"]["blocklist_status"] = "done"
    path.write_text(json.dumps(record))
    server._jobs["abc12345"] = {"id": "abc12345", "output_dir": str(output_dir)}
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    blocklist = mocker.patch.object(server, "_blocklist_grab", return_value=False)
    mocker.patch.object(server, "_save_jobs")

    client = server.app.test_client()
    response = client.post(f"/verification/abc12345/discard?apikey={VALID_KEY}")

    assert response.status_code == 200
    archived = server.DISCARDED_DIR / "abc12345.json"
    archived_record = json.loads(archived.read_text())
    assert archived_record["lifecycle"]["state"] == "discarded"
    assert archived_record["lifecycle"]["blocklist_status"] == "done"
    blocklist.assert_not_called()


def test_discard_updates_db_record_status(tmp_path, monkeypatch, mocker):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"flac")
    sidecar_path = server._write_verification_sidecar("abc12345", _result(), output_dir)
    sidecar = json.loads(sidecar_path.read_text())
    state_db.upsert_from_sidecar(sidecar, derived_status="needs_review")
    server._jobs["abc12345"] = {"id": "abc12345", "output_dir": str(output_dir)}
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_blocklist_grab", return_value=True)
    mocker.patch.object(server, "_save_jobs")

    client = server.app.test_client()
    response = client.post(f"/verification/abc12345/discard?apikey={VALID_KEY}")

    assert response.status_code == 200
    record = state_db.get_record("abc12345")
    assert record["derived_status"] == "discarded"
    assert record["lifecycle_state"] == "discarded"
    assert record["actor"] == "user_discard"


def test_discard_terminalizes_backend_review_job(tmp_path, monkeypatch, mocker):
    state_db.init(db_path=tmp_path / "state.db")
    output_base = _patch_paths(monkeypatch, tmp_path)
    jid = "backend-review"
    output_dir = output_base / jid
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"flac")
    sidecar_path = server._write_verification_sidecar(jid, _result(jid=jid), output_dir)
    sidecar = json.loads(sidecar_path.read_text())
    state_db.upsert_from_sidecar(sidecar, derived_status="needs_review")
    state_db.create_backend_job(
        jid,
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="review",
        release_title="Artist - Album",
    )
    server._jobs[jid] = {"id": jid, "output_dir": str(output_dir)}
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_blocklist_grab", return_value=True)
    mocker.patch.object(server, "_save_jobs")

    client = server.app.test_client()
    response = client.post(f"/verification/{jid}/discard?apikey={VALID_KEY}")

    assert response.status_code == 200
    backend_job = state_db.get_backend_job(jid)
    assert backend_job["state"] == "cancelled"
    assert backend_job["finished_at"] is not None
    record = state_db.get_record(jid)
    assert record["derived_status"] == "discarded"


def test_discard_accepts_failed_or_pending_manual_promote(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_blocklist_grab", return_value=True)
    mocker.patch.object(server, "_save_jobs")
    client = server.app.test_client()

    for jid, outcome in (("abc12345", "FAILED"), ("def67890", "PENDING")):
        output_dir = output_base / jid
        output_dir.mkdir(parents=True)
        (output_dir / "01.flac").write_bytes(b"flac")
        server._write_verification_sidecar(
            jid,
            _result(
                decision="ACCEPT_PROVISIONAL",
                outcome=outcome,
                overrides=["manual_promote"],
            ),
            output_dir,
        )
        server._jobs[jid] = {"id": jid, "output_dir": str(output_dir)}

        response = client.post(f"/verification/{jid}/discard?apikey={VALID_KEY}")

        assert response.status_code == 200
        assert not output_dir.exists()
        archived = server.DISCARDED_DIR / f"{jid}.json"
        record = json.loads(archived.read_text())
        assert record["v2_verification_decision"] == "ACCEPT_PROVISIONAL"
        assert record["v2_import_outcome"] == outcome
        assert record["lifecycle"]["state"] == "discarded"


def test_expire_review_required_archives_and_deletes_output(
    tmp_path, monkeypatch, mocker
):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "abc12345"
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"flac")
    path = server._write_verification_sidecar("abc12345", _result(), output_dir)
    record = json.loads(path.read_text())
    record["lifecycle"]["created_at"] = 1
    record["ts"] = 1
    path.write_text(json.dumps(record))
    server._jobs["abc12345"] = {
        "id": "abc12345",
        "status": "review_required",
        "output_dir": str(output_dir),
    }
    monkeypatch.setenv("REVIEW_RETENTION_DAYS", "0")
    mocker.patch.object(server, "_get_lidarr_key", return_value="lidarr-key")
    mocker.patch.object(server, "_blocklist_grab", return_value=True)
    cleanup = mocker.patch.object(server, "_cleanup_lidarr_queue")
    mocker.patch.object(server, "_save_jobs")

    server._expire_review_required_jobs()

    assert not output_dir.exists()
    archived = server.EXPIRED_REVIEW_DIR / "abc12345.json"
    assert archived.exists()
    expired = json.loads(archived.read_text())
    assert expired["lifecycle"]["state"] == "expired"
    assert expired["lifecycle"]["blocklist_status"] == "done"
    assert server._jobs["abc12345"]["status"] == "failed"
    # Review-hold invariant: expiry clears the hold and cleans the Lidarr queue.
    assert server._jobs["abc12345"].get("lidarr_hold") is None
    cleanup.assert_called_once()


def test_dashboard_renders_summary_cards(tmp_path, monkeypatch):
    """Dashboard should show summary cards with counts (Total, Imported, etc.)."""
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "card12345"
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("card12345", _result(), output_dir)

    client = server.app.test_client()
    response = client.get(
        f"/verification?apikey={VALID_KEY}", headers={"Accept": "text/html"}
    )

    assert response.status_code == 200
    text = response.get_data(as_text=True)
    # Summary-card labels should be present
    assert "Total decisions" in text
    assert "Imported" in text
    assert "Needs review" in text
    assert "Pending" in text
    assert "Failed" in text
    assert "SAB queue" in text
    assert "Lidarr queue" in text


def test_dashboard_highlights_needs_action_when_review_pending(tmp_path, monkeypatch):
    """REVIEW_REQUIRED records with pending_review lifecycle should appear in the Needs Action section."""
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "review123"
    output_dir.mkdir(parents=True)
    review_result = _result(decision="REVIEW_REQUIRED")
    server._write_verification_sidecar("review123", review_result, output_dir)

    client = server.app.test_client()
    response = client.get(
        f"/verification?apikey={VALID_KEY}", headers={"Accept": "text/html"}
    )

    assert response.status_code == 200
    text = response.get_data(as_text=True)
    # Needs Action heading should carry the warning flag
    assert "Needs action" in text
    # Highlight class should be set for the review row
    assert "needs-action" in text
