"""Tests for V2.2 dashboard endpoints + HTML page."""

from __future__ import annotations

import json
from pathlib import Path

import server
import state_db
from verification import VerificationResult

VALID_KEY = "tidalhires-test-api-key"


def _result(jid="abc12345", decision="ACCEPT", outcome="MANUAL_IMPORTED"):
    return VerificationResult(
        jid=jid,
        score=85,
        verification_decision=decision,
        import_outcome=outcome,
        components={"ffprobe": 25, "flac_t": 25, "detective": 35, "complete": 0},
        overrides=[],
        verdict="AUTHENTIC",
        new_kbps=3000,
        existing_kbps=0,
        existing_label="nothing",
        album_ids=[100],
        title="Test Artist - Test Album",
    )


def _patch_paths(monkeypatch, tmp_path):
    output_base = tmp_path / "output"
    monkeypatch.setattr(server, "OUTPUT_BASE", output_base)
    monkeypatch.setattr(server, "BLOCKED_DECISIONS_DIR", tmp_path / "blocked")
    monkeypatch.setattr(server, "DISCARDED_DIR", tmp_path / "discarded")
    monkeypatch.setattr(server, "EXPIRED_REVIEW_DIR", tmp_path / "expired")
    return output_base


def test_dashboard_summary_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/summary").status_code == 401


def test_dashboard_records_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/records").status_code == 401


def test_dashboard_action_requires_apikey():
    client = server.app.test_client()
    assert client.post("/dashboard/v1/action/abc").status_code == 401


def test_dashboard_html_does_not_require_apikey(monkeypatch, tmp_path):
    """HTML shell loads without auth — JS handles auth via localStorage."""
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.content_type
    body = resp.get_data(as_text=True)
    assert "TidalHires V2 Dashboard" in body
    assert "summary-grid" in body
    assert "drawer" in body
    assert "integrations-view" in body
    assert "tab-integrations" in body


def test_dashboard_html_fetches_and_renders_connectors(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "api('/connectors')" in body
    assert "function renderIntegrations" in body
    assert "connector-card" in body
    assert "saveConnectorConfig" in body
    assert "/connectors/' + encodeURIComponent(connectorId) + '/config" in body
    assert "Required env" in body


def test_dashboard_summary_returns_expected_shape(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "sum12345")
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("sum12345", _result(jid="sum12345"), output_dir)

    # Clear dashboard cache to ensure fresh fetch
    from dashboard_cache import clear
    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/summary?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "counts" in data
    assert "stack_health" in data
    assert "queue" in data
    assert data["counts"]["total_decisions"] >= 1


def test_dashboard_summary_counts_pending_from_derived_status(monkeypatch, tmp_path):
    """Terminal records with a historical PENDING outcome must not inflate the card."""
    _patch_paths(monkeypatch, tmp_path)
    output_base = tmp_path / "output"

    pending_dir = output_base / "pnd11111"
    pending_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "pnd11111",
        _result(jid="pnd11111", decision="ACCEPT", outcome="PENDING"),
        pending_dir,
    )

    discarded_dir = output_base / "dis22222"
    discarded_dir.mkdir(parents=True)
    discarded_path = server._write_verification_sidecar(
        "dis22222",
        _result(jid="dis22222", decision="REVIEW_REQUIRED", outcome="PENDING"),
        discarded_dir,
    )
    discarded = json.loads(discarded_path.read_text())
    discarded["lifecycle"]["state"] = "discarded"
    discarded_path.write_text(json.dumps(discarded))

    imported_dir = output_base / "imp33333"
    imported_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "imp33333",
        _result(jid="imp33333", decision="ACCEPT", outcome="MANUAL_IMPORTED"),
        imported_dir,
    )

    from dashboard_cache import clear
    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/summary?apikey={VALID_KEY}")
    assert resp.status_code == 200
    counts = resp.get_json()["counts"]
    assert counts["pending"] == 1
    assert counts["discarded"] == 1
    assert counts["imported"] == 1


def test_dashboard_summary_flags_blocking_lidarr_commands(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "cmd12345")
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("cmd12345", _result(jid="cmd12345"), output_dir)

    import dashboard
    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    monkeypatch.setattr(server, "_get_lidarr_key", lambda: "lidarr-key")
    monkeypatch.setattr(dashboard.time, "time", lambda: 7200.0)
    monkeypatch.setattr(dashboard, "_check_flac_detective", lambda: "ok")

    class FakeResponse:
        def __init__(self, payload, ok=True, status_code=200):
            self._payload = payload
            self.ok = ok
            self.status_code = status_code

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        if url == "http://lidarr/api/v1/queue?pageSize=1":
            return FakeResponse({"totalRecords": 4})
        if url == "http://lidarr/api/v1/command":
            return FakeResponse([
                {
                    "id": 14568,
                    "name": "RescanFolders",
                    "status": "started",
                    "message": "Importing 1608 tracks",
                    "queued": "1970-01-01T00:00:00Z",
                    "started": "1970-01-01T00:10:00Z",
                },
                {
                    "id": 14647,
                    "name": "ManualImport",
                    "status": "queued",
                    "message": None,
                    "queued": "1970-01-01T01:00:00Z",
                    "started": None,
                },
            ])
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("requests.get", fake_get)

    from dashboard_cache import clear
    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/summary?apikey={VALID_KEY}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["stack_health"]["lidarr"] == "blocked"
    commands = data["queue"]["lidarr_commands"]
    assert commands["status"] == "blocked"
    assert commands["active_count"] == 2
    assert commands["blocking_count"] == 2
    reasons = [item["blocking_reason"] for item in commands["commands"]]
    assert "RescanFolders has been started for 110m." in reasons
    assert "ManualImport is queued behind a started RescanFolders command." in reasons


def test_dashboard_records_returns_records(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "rec12345")
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("rec12345", _result(jid="rec12345"), output_dir)

    from dashboard_cache import clear
    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/records?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "records" in data
    jids = [r["jid"] for r in data["records"]]
    assert "rec12345" in jids
    rec = next(r for r in data["records"] if r["jid"] == "rec12345")
    assert rec["status_reason"] == "Imported after quality checks passed."


def test_dashboard_records_filters_by_decision(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_base = tmp_path / "output"
    for jid, dec in [("acc111", "ACCEPT"), ("rev222", "REVIEW_REQUIRED")]:
        d = output_base / jid
        d.mkdir(parents=True)
        server._write_verification_sidecar(jid, _result(jid=jid, decision=dec), d)

    from dashboard_cache import clear
    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/records?decision=ACCEPT&apikey={VALID_KEY}")
    data = resp.get_json()
    jids = [r["jid"] for r in data["records"]]
    assert "acc111" in jids
    assert "rev222" not in jids


def test_dashboard_record_detail_404_for_unknown(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/record/nonexistent?apikey={VALID_KEY}")
    assert resp.status_code == 404


def test_dashboard_record_detail_includes_available_actions(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "det12345")
    output_dir.mkdir(parents=True)
    # REVIEW_REQUIRED → pending_review → should allow promote+discard
    server._write_verification_sidecar(
        "det12345",
        _result(jid="det12345", decision="REVIEW_REQUIRED", outcome="SKIPPED"),
        output_dir,
    )

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/record/det12345?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "promote" in data["available_actions"]
    assert "discard" in data["available_actions"]
    assert "retry_import" not in data["available_actions"]
    assert data["status_reason"] == "Review required by policy"
    assert data["sensors"] == []
    assert data["files"] == []


def test_dashboard_record_detail_does_not_reconcile_review_required(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "revfast1")
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "revfast1",
        _result(jid="revfast1", decision="REVIEW_REQUIRED", outcome="PENDING"),
        output_dir,
    )

    def fail_reconcile(*args, **kwargs):
        raise AssertionError("REVIEW_REQUIRED drawer should not call Lidarr reconcile")

    monkeypatch.setattr(server, "_reconcile_pending_import", fail_reconcile)

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/record/revfast1?apikey={VALID_KEY}")

    assert resp.status_code == 200
    assert resp.get_json()["derived_status"] == "needs_review"


def test_review_required_sidecar_starts_dashboard_media_prewarm(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "warm1234")
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"fake flac")
    result = _result(jid="warm1234", decision="REVIEW_REQUIRED", outcome="PENDING")
    started = []

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            started.append({"target": target, "args": args, "daemon": daemon})

        def start(self):
            pass

    with server._dashboard_media_prewarm_lock:
        server._dashboard_media_prewarm_inflight.clear()
    monkeypatch.setattr(server.threading, "Thread", FakeThread)

    server._write_verification_sidecar("warm1234", result, output_dir)

    assert started == [{
        "target": server._prewarm_dashboard_media_worker,
        "args": ("warm1234",),
        "daemon": True,
    }]
    with server._dashboard_media_prewarm_lock:
        server._dashboard_media_prewarm_inflight.discard("warm1234")


def test_dashboard_record_detail_includes_job_timings(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "tim12345")
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("tim12345", _result(jid="tim12345"), output_dir)
    monkeypatch.setattr(server, "_jobs", {
        "tim12345": {"id": "tim12345", "timings": {"flac_detective_sec": 12.345}},
    })

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/record/tim12345?apikey={VALID_KEY}")
    assert resp.status_code == 200
    assert resp.get_json()["timings"]["flac_detective_sec"] == 12.345


def test_lidarr_context_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/lidarr-context/abc12345").status_code == 401


def test_dashboard_timings_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/timings").status_code == 401


def test_dashboard_timings_aggregates_jobs(monkeypatch):
    monkeypatch.setattr(server, "_verification_records", lambda: [])
    monkeypatch.setattr(server, "_jobs", {
        "tim1": {"created_at": 1779620000, "timings": {"flac_detective_sec": 10, "pre_import_total_sec": 100}},
        "tim2": {"created_at": 1779620010, "timings": {"flac_detective_sec": 20, "pre_import_total_sec": 200}},
        "tim3": {"created_at": 1779620020, "timings": {"flac_detective_sec": 30, "pre_import_total_sec": 300}},
    })
    monkeypatch.setattr("dashboard.time.time", lambda: 1779620100)

    from dashboard_cache import clear
    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/timings?window=1h&apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sample_count"] == 3
    assert data["stages"]["flac_detective_sec"]["median"] == 20
    assert data["stages"]["flac_detective_sec"]["p95"] == 30
    assert data["stages"]["pre_import_total_sec"]["fastest"] == 100


def test_dashboard_timings_filters_stage(monkeypatch):
    monkeypatch.setattr(server, "_verification_records", lambda: [])
    monkeypatch.setattr(server, "_jobs", {
        "tim1": {"created_at": 1779620000, "timings": {"flac_detective_sec": 10, "postprocess_sec": 1}},
    })
    monkeypatch.setattr("dashboard.time.time", lambda: 1779620100)

    from dashboard_cache import clear
    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/timings?window=1h&stage=postprocess_sec&apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert list(data["stages"].keys()) == ["postprocess_sec"]


def test_dashboard_media_requires_apikey():
    client = server.app.test_client()
    assert client.get("/dashboard/v1/audio-sample/abc12345").status_code == 401
    assert client.get("/dashboard/v1/spectrum/abc12345").status_code == 401


def test_dashboard_media_rejects_missing_audio(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/audio-sample/missing1?apikey={VALID_KEY}")

    assert resp.status_code == 404


def test_dashboard_media_artifact_generates_from_contained_audio(monkeypatch, tmp_path):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "aud12345"
    output_dir.mkdir(parents=True)
    source = output_dir / "01.flac"
    source.write_bytes(b"fake flac")
    media_dir = tmp_path / "media"

    import dashboard
    monkeypatch.setattr(dashboard, "_dashboard_media_dir", lambda: media_dir)

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(cmd[-1]).write_bytes(b"mp3")
        class Result:
            returncode = 0
            stderr = ""
        return Result()

    monkeypatch.setattr(dashboard.subprocess, "run", fake_run)

    path, error = dashboard._media_artifact(server, "aud12345", "audio")

    assert error is None
    assert path == media_dir / "aud12345.sample.mp3"
    assert path.read_bytes() == b"mp3"


def test_record_detail_marks_media_review_available_only_for_retained_non_imported(monkeypatch, tmp_path):
    output_base = _patch_paths(monkeypatch, tmp_path)

    review_dir = output_base / "rev12345"
    review_dir.mkdir(parents=True)
    (review_dir / "01.flac").write_bytes(b"fake flac")
    review = _result(jid="rev12345", decision="REVIEW_REQUIRED", outcome="PENDING")
    review.overrides = ["fake_hi_res"]
    review.verdict = "SUSPICIOUS"
    server._write_verification_sidecar("rev12345", review, review_dir)

    imported_dir = output_base / "imp12345"
    imported_dir.mkdir(parents=True)
    (imported_dir / "01.flac").write_bytes(b"fake flac")
    server._write_verification_sidecar("imp12345", _result(jid="imp12345"), imported_dir)

    import dashboard
    assert dashboard._build_record_detail(server, "rev12345")["media"] == {
        "available": True,
        "files_present": True,
        "review_relevant": True,
        "reason": "Audio review is available for retained, non-imported files.",
    }

    imported_media = dashboard._build_record_detail(server, "imp12345")["media"]
    assert imported_media["available"] is False
    assert imported_media["files_present"] is True
    assert imported_media["review_relevant"] is False


def test_dashboard_media_endpoint_hides_imported_records(monkeypatch, tmp_path):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_dir = output_base / "impmedia"
    output_dir.mkdir(parents=True)
    (output_dir / "01.flac").write_bytes(b"fake flac")
    server._write_verification_sidecar("impmedia", _result(jid="impmedia"), output_dir)

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/audio-sample/impmedia?apikey={VALID_KEY}")

    assert resp.status_code == 404
    assert "hidden for imported records" in resp.get_json()["error"]


def test_dashboard_media_artifact_rejects_uncontained_job_path(monkeypatch, tmp_path):
    output_base = _patch_paths(monkeypatch, tmp_path)
    output_base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "01.flac").write_bytes(b"fake flac")
    monkeypatch.setattr(server, "_jobs", {
        "escape1": {"id": "escape1", "output_dir": str(outside)},
    })

    import dashboard
    path, error = dashboard._media_artifact(server, "escape1", "audio")

    assert path is None
    assert error == "no audio file available"
    assert output_base.exists()


def test_lidarr_context_returns_album_queue_and_history(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "ctx12345")
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("ctx12345", _result(jid="ctx12345"), output_dir)
    monkeypatch.setattr(server, "_get_lidarr_key", lambda: "lidarr-key")
    monkeypatch.setenv("LIDARR_API_URL", "http://lidarr/api/v1")
    monkeypatch.setenv("LIDARR_WEB_URL", "http://lidarr")

    class FakeResponse:
        ok = True

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        if url.endswith("/album/100"):
            return FakeResponse({
                "id": 100,
                "title": "Test Album",
                "artist": {"artistName": "Test Artist"},
                "statistics": {"trackCount": 12, "trackFileCount": 10},
                "monitored": True,
                "profileId": 1,
                "currentRelease": {"title": "Deluxe", "trackCount": 12},
            })
        if url.endswith("/queue"):
            return FakeResponse({"records": [
                {"id": 1, "albumId": 100, "title": "Test Artist - Test Album",
                 "downloadId": "ctx12345", "status": "completed", "sizeleft": 0},
                {"id": 2, "albumId": 999, "title": "Other"},
            ]})
        if url.endswith("/history"):
            return FakeResponse({"records": [
                {"albumId": 100, "date": "2026-05-24T12:00:00Z",
                 "eventType": "downloadFolderImported", "indexer": "TidalHires",
                 "downloadId": "ctx12345", "sourceTitle": "Test Artist - Test Album"},
                {"albumId": 999, "eventType": "grabbed"},
            ]})
        raise AssertionError(url)

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    from dashboard_cache import clear
    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/lidarr-context/ctx12345?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["album"]["title"] == "Test Album"
    assert data["album"]["track_file_count"] == 10
    assert data["queue"]["in_queue"] is True
    assert len(data["queue"]["queue_entries"]) == 1
    assert data["grab_history"][0]["successful"] is True


def test_dashboard_action_rejects_unallowed(monkeypatch, tmp_path):
    """ACCEPT/MANUAL_IMPORTED record allows no actions — discard should 409."""
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "act12345")
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("act12345", _result(jid="act12345"), output_dir)

    client = server.app.test_client()
    resp = client.post(
        f"/dashboard/v1/action/act12345?apikey={VALID_KEY}",
        json={"action": "discard"},
    )
    assert resp.status_code == 409
    assert "allowed" in resp.get_json()


def test_dashboard_action_404_for_unknown(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.post(
        f"/dashboard/v1/action/nonexistent?apikey={VALID_KEY}",
        json={"action": "discard"},
    )
    assert resp.status_code == 404


def test_derive_status_for_common_combinations():
    from dashboard import derive_status
    assert derive_status({"v2_verification_decision": "ACCEPT", "v2_import_outcome": "MANUAL_IMPORTED",
                          "lifecycle": {"state": "created"}}) == "imported"
    assert derive_status({"v2_verification_decision": "REVIEW_REQUIRED",
                          "lifecycle": {"state": "pending_review"}}) == "needs_review"
    assert derive_status({"v2_import_outcome": "FAILED", "lifecycle": {"state": "created"}}) == "failed"
    assert derive_status({"lifecycle": {"state": "discarded"}}) == "discarded"
    assert derive_status({"v2_verification_decision": "BLOCK", "lifecycle": {"state": "created"}}) == "blocked"
    assert derive_status({"v2_verification_decision": "BLOCK", "v2_import_outcome": "MANUAL_IMPORTED",
                          "lifecycle": {"state": "created"}}) == "policy_violation"


def test_status_reason_for_operator_states():
    from dashboard import status_reason

    assert status_reason({
        "v2_verification_decision": "REVIEW_REQUIRED",
        "v2_import_outcome": "PENDING",
        "v2_overrides": ["fake_hi_res"],
        "lifecycle": {"state": "pending_review"},
    }) == "Looks like upsampled hi-res: useful high-frequency content stops at the file's technical ceiling."
    assert status_reason({
        "v2_verification_decision": "ACCEPT_PROVISIONAL",
        "v2_import_outcome": "FAILED",
        "reason": "manualimport and rescue failed",
        "lifecycle": {"state": "created"},
    }) == "Import failed: manualimport and rescue failed"
    assert status_reason({
        "v2_verification_decision": "ACCEPT_PROVISIONAL",
        "v2_import_outcome": "FAILED",
        "reason": "nothing pre-existing",
        "job_error": "manual promote import failed",
        "lifecycle": {"state": "created"},
    }) == (
        "Import failed after QC passed: Lidarr ManualImport did not confirm any imported files. "
        "Open the record for Lidarr context, then retry import or discard."
    )
    assert status_reason({
        "v2_verification_decision": "ACCEPT_PROVISIONAL",
        "v2_import_outcome": "MANUAL_IMPORTED",
        "reason": "score=68",
        "lifecycle": {"state": "created"},
    }) == "Imported provisionally because the score was below full-accept threshold."
    assert status_reason({
        "v2_verification_decision": "BLOCK",
        "v2_import_outcome": "SKIPPED",
        "reason": "validator unavailable",
        "lifecycle": {"state": "created"},
    }) == "Blocked by policy: validator unavailable."
    assert status_reason({
        "v2_verification_decision": "BLOCK",
        "v2_import_outcome": "SKIPPED",
        "reason": "codec mismatch",
        "v2_overrides": ["codec_mismatch", "no_audio_files"],
        "sensors": [{
            "name": "ffprobe",
            "evidence": {"codec_gate_skipped": 10, "flac_count": 0},
        }],
        "lifecycle": {"state": "created"},
    }) == (
        "Skipped before import: the release was advertised as FLAC, "
        "but the download contained 10 non-FLAC audio file(s). "
        "All were stopped by the codec gate, so no FLAC files remained for import."
    )
    assert status_reason({
        "v2_verification_decision": "BLOCK",
        "v2_import_outcome": "MANUAL_IMPORTED",
        "reason": "codec mismatch",
        "lifecycle": {"state": "created"},
    }) == "Policy violation: this record was imported even though V2 decided BLOCK. Keep it for audit and inspect Lidarr library/history."
    assert status_reason({
        "v2_verification_decision": "REVIEW_REQUIRED",
        "v2_import_outcome": "PENDING",
        "lifecycle": {"state": "discarded", "actor": "user_discard"},
    }) == "Discarded by user; files were removed and the grab was blocklisted when possible."


def test_record_job_timing_rounds_and_persists(monkeypatch):
    saved = []
    monkeypatch.setattr(server, "_jobs", {})
    monkeypatch.setattr(server, "_save_jobs", lambda: saved.append(True))

    server._record_job_timing("timing1", "flac_detective_sec", 1.23456)

    assert server._jobs["timing1"]["timings"]["flac_detective_sec"] == 1.235
    assert saved


def test_job_cancel_accepts_running_tidal_grab_in_f24(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    job_id = state_db.enqueue_job(jid="tidalrun", type="tidal_grab")
    state_db.dequeue_next_job(worker_id="worker-1")

    client = server.app.test_client()
    response = client.post(f"/dashboard/v1/jobs/{job_id}/cancel?apikey={VALID_KEY}")

    assert response.status_code == 200
    body = response.get_json()
    assert body["cancel_requested"] is True
    assert body["state"] == "cancelling"
    job = state_db.get_job(job_id)
    assert job["cancel_requested"] == 1
    assert job["state"] == "cancelling"


def test_job_cancel_409_has_operator_friendly_error(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    job_id = state_db.enqueue_job(jid="donecancel", type="noop")
    state_db.dequeue_next_job(worker_id="worker-1")
    state_db.mark_job_completed(job_id, result_state="ok")

    client = server.app.test_client()
    response = client.post(f"/dashboard/v1/jobs/{job_id}/cancel?apikey={VALID_KEY}")

    assert response.status_code == 409
    body = response.get_json()
    assert body["error"] == "cannot cancel terminal job"
    assert body["state"] == "completed"


def test_available_actions_per_state():
    from dashboard import available_actions
    # REVIEW_REQUIRED + pending_review → promote/discard
    assert set(available_actions({
        "v2_verification_decision": "REVIEW_REQUIRED",
        "lifecycle": {"state": "pending_review"},
    })) == {"promote", "discard"}
    # FAILED + ACCEPT → retry/discard
    assert set(available_actions({
        "v2_verification_decision": "ACCEPT",
        "v2_import_outcome": "FAILED",
        "lifecycle": {"state": "created"},
    })) == {"retry_import", "discard"}
    # IMPORTED → no actions
    assert available_actions({
        "v2_verification_decision": "ACCEPT",
        "v2_import_outcome": "MANUAL_IMPORTED",
        "lifecycle": {"state": "created"},
    }) == []
    # discarded → no actions
    assert available_actions({"lifecycle": {"state": "discarded"}}) == []


def test_dashboard_cache_invalidate_on_action(monkeypatch, tmp_path):
    """POST action should invalidate summary + records cache."""
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "inv12345")
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "inv12345",
        _result(jid="inv12345", decision="BLOCK", outcome="SKIPPED"),
        output_dir,
    )

    from dashboard_cache import clear, _cache
    clear()

    client = server.app.test_client()
    # Prime cache with summary
    client.get(f"/dashboard/v1/summary?apikey={VALID_KEY}")
    assert any(k[0] == "summary" for k in _cache)

    # Trigger action — should invalidate cache
    client.post(
        f"/dashboard/v1/action/inv12345?apikey={VALID_KEY}",
        json={"action": "discard"},
    )
    assert not any(k[0] == "summary" for k in _cache)


# ---- F1.6: DB-backed records + actions endpoint ----

def test_dashboard_records_uses_db_when_available(monkeypatch, tmp_path):
    """When DB has records, /records returns _source=db marker."""
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "dbq12345")
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar("dbq12345", _result(jid="dbq12345"), output_dir)

    from dashboard_cache import clear
    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/records?apikey={VALID_KEY}")
    data = resp.get_json()
    rec = next(r for r in data["records"] if r["jid"] == "dbq12345")
    assert rec.get("_source") == "db"


def test_dashboard_records_db_reason_uses_sidecar_evidence(monkeypatch, tmp_path):
    """DB-backed rows should still expose non-cryptic status reasons."""
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "dbblock1")
    output_dir.mkdir(parents=True)
    result = _result(jid="dbblock1", decision="BLOCK", outcome="SKIPPED")
    result.score = 0
    result.verdict = "UNKNOWN"
    result.overrides = ["codec_mismatch", "no_audio_files"]
    result.sensors = [{
        "name": "ffprobe",
        "evidence": {"codec_gate_skipped": 10, "flac_count": 0},
    }]
    server._write_verification_sidecar("dbblock1", result, output_dir)

    from dashboard_cache import clear
    clear()

    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/records?apikey={VALID_KEY}")
    data = resp.get_json()
    rec = next(r for r in data["records"] if r["jid"] == "dbblock1")
    assert rec.get("_source") == "db"
    assert rec["overrides"] == ["codec_mismatch", "no_audio_files"]
    assert rec["status_reason"] == (
        "Skipped before import: the release was advertised as FLAC, "
        "but the download contained 10 non-FLAC audio file(s). "
        "All were stopped by the codec gate, so no FLAC files remained for import."
    )


def test_dashboard_actions_endpoint_empty_when_no_actions(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    resp = client.get(f"/dashboard/v1/actions?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["actions"] == []
    assert data["returned"] == 0


def test_dashboard_actions_per_jid(monkeypatch, tmp_path):
    """Action POST should be logged + readable via /actions/<jid>."""
    _patch_paths(monkeypatch, tmp_path)
    output_dir = (tmp_path / "output" / "logj1234")
    output_dir.mkdir(parents=True)
    server._write_verification_sidecar(
        "logj1234",
        _result(jid="logj1234", decision="BLOCK", outcome="SKIPPED"),
        output_dir,
    )

    client = server.app.test_client()
    # Discard a BLOCK record (allowed action)
    client.post(
        f"/dashboard/v1/action/logj1234?apikey={VALID_KEY}",
        json={"action": "discard"},
    )

    # Verify action logged
    resp = client.get(f"/dashboard/v1/actions/logj1234?apikey={VALID_KEY}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["actions"]) == 1
    assert data["actions"][0]["action"] == "discard"
    assert data["actions"][0]["jid"] == "logj1234"


def test_dashboard_actions_requires_apikey(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    client = server.app.test_client()
    assert client.get("/dashboard/v1/actions").status_code == 401
    assert client.get("/dashboard/v1/actions/abc123").status_code == 401
