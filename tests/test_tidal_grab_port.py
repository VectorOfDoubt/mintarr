"""Tests for F2.2 TIDAL grab port — addurl enqueues via worker."""

from __future__ import annotations

import json
import pytest

import server
import state_db
import worker


@pytest.fixture
def fresh_db(tmp_path):
    db_file = tmp_path / "state.db"
    state_db._initialized = False
    state_db.init(db_path=db_file)
    yield db_file
    state_db._initialized = False


@pytest.fixture
def mock_session(monkeypatch):
    """Replace _get_session to avoid TIDAL OAuth in tests.

    F3.2 also requires TidalAdapter.is_enabled()=True for addurl routing —
    in production this gates on token.json presence; in tests we patch it
    directly so the dispatcher doesn't 503.
    """

    class _FakeAlbum:
        def __init__(self, aid):
            self.id = aid
            self.name = f"Album {aid}"
            self.duration = 1800
            self.artist = type("A", (), {"name": f"Artist {aid}"})()
            self.release_date = type("D", (), {"year": 2024})()
            self.num_tracks = 10

    class _FakeSession:
        def album(self, aid):
            return _FakeAlbum(aid)

    monkeypatch.setattr(server, "_get_session", lambda: _FakeSession())
    # Also patch the get_session re-export used by adapter helpers
    from adapters import tidal as _tidal_mod

    monkeypatch.setattr(_tidal_mod, "get_session", lambda: _FakeSession())
    # F3.2 gates on adapter.is_enabled() — token.json doesn't exist in tests
    import adapters as _adapters

    tidal_adapter = _adapters.get_adapter("tidal")
    if tidal_adapter is not None:
        monkeypatch.setattr(tidal_adapter, "is_enabled", lambda: True)


def _post_addurl(client, album_id: int, key: str = "tidalhires-test-api-key"):
    """Helper: POST /sabnzbd/api?mode=addurl&apikey=...&name=tidal:<id>."""
    return client.post(
        f"/sabnzbd/api?mode=addurl&apikey={key}&name=tidal:{album_id}",
    )


def test_addurl_enqueues_via_state_db(fresh_db, mock_session, monkeypatch):
    """addurl should create a tidal_grab job in state_db (not spawn legacy thread)."""
    # Prevent worker from actually executing _run_download_job
    monkeypatch.setenv("TIDALHIRES_DISABLE_WORKER", "1")

    client = server.app.test_client()
    resp = _post_addurl(client, album_id=12345)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] is True
    assert len(body["nzo_ids"]) == 1
    jid = body["nzo_ids"][0]

    # Job should be in DB with correct fields
    total, jobs = state_db.list_jobs(type=["tidal_grab"])
    assert total == 1
    j = jobs[0]
    assert j["type"] == "tidal_grab"
    assert j["jid"] == jid
    assert j["state"] == "queued"
    assert j["dedupe_key"] == "tidal:12345"
    assert j["source_type"] == "tidal"
    assert j["source_id"] == "12345"
    # F3.2: addurl payload now uses generic source_id (album_id only kept
    # on _jobs projection for backwards-compat with audit consumers).
    payload = json.loads(j["payload_json"])
    assert payload["source_id"] == 12345


def test_addurl_dedupe_returns_existing_jid(fresh_db, mock_session, monkeypatch):
    """Two addurl for same album should return same jid (no double-enqueue)."""
    monkeypatch.setenv("TIDALHIRES_DISABLE_WORKER", "1")

    client = server.app.test_client()
    r1 = _post_addurl(client, album_id=99999)
    r2 = _post_addurl(client, album_id=99999)
    assert r1.status_code == 200 and r2.status_code == 200
    jid1 = r1.get_json()["nzo_ids"][0]
    jid2 = r2.get_json()["nzo_ids"][0]
    assert jid1 == jid2

    # Only one job in DB
    total, _ = state_db.list_jobs(type=["tidal_grab"])
    assert total == 1


def test_addurl_enqueue_dedupe_race_returns_existing_jid_without_phantom_job(
    fresh_db, mock_session, monkeypatch
):
    """If dedupe appears between precheck and enqueue, return existing jid."""
    monkeypatch.setenv("TIDALHIRES_DISABLE_WORKER", "1")
    monkeypatch.setattr(server, "_jobs", {})
    existing_id = state_db.enqueue_job(
        jid="existingjid",
        type="tidal_grab",
        dedupe_key="tidal:22222",
        source_type="tidal",
        source_id="22222",
    )
    assert existing_id is not None
    monkeypatch.setattr(state_db, "find_active_job_by_dedupe", lambda key: None)

    client = server.app.test_client()
    response = _post_addurl(client, album_id=22222)

    assert response.status_code == 200
    assert response.get_json()["nzo_ids"] == ["existingjid"]
    total, jobs = state_db.list_jobs(type=["tidal_grab"])
    assert total == 1
    assert jobs[0]["jid"] == "existingjid"
    assert server._jobs == {}


def test_addurl_dedupe_does_not_block_after_terminal(
    fresh_db, mock_session, monkeypatch
):
    """If a previous tidal_grab job completed/failed, new addurl creates new job."""
    monkeypatch.setenv("TIDALHIRES_DISABLE_WORKER", "1")

    client = server.app.test_client()
    r1 = _post_addurl(client, album_id=77777)
    jid1 = r1.get_json()["nzo_ids"][0]

    # Mark the first job completed (simulate worker finished)
    total, jobs = state_db.list_jobs(type=["tidal_grab"])
    state_db.mark_job_completed(jobs[0]["id"], result_state="imported")

    # New addurl should NOT dedupe (existing is terminal)
    r2 = _post_addurl(client, album_id=77777)
    jid2 = r2.get_json()["nzo_ids"][0]
    assert jid1 != jid2

    total, _ = state_db.list_jobs(type=["tidal_grab"])
    assert total == 2


def test_executor_invoked_with_correct_payload(fresh_db, monkeypatch):
    """The tidal_grab executor delegates to pipeline.execute_source_grab
    with the correct adapter + payload (F3.4 generic executor)."""
    import pipeline

    calls = []

    def _fake_execute(job, adapter, ctx):
        calls.append(
            {
                "jid": job["jid"],
                "adapter_name": adapter.name,
                "worker_job_id": ctx.worker_job_id,
                "source_id": json.loads(job["payload_json"]).get("source_id")
                or json.loads(job["payload_json"]).get("album_id"),
            }
        )
        with server._jobs_lock:
            server._jobs[job["jid"]] = {"id": job["jid"], "status": "completed"}

    monkeypatch.setattr(pipeline, "execute_source_grab", _fake_execute)

    job = {
        "id": 99,
        "jid": "exec1234",
        "payload_json": json.dumps({"album_id": 555, "title": "Test"}),
    }
    result_state, result = server._execute_tidal_grab_job(job)
    assert len(calls) == 1
    assert calls[0]["adapter_name"] == "tidal"
    assert calls[0]["source_id"] == 555
    assert calls[0]["jid"] == "exec1234"
    assert calls[0]["worker_job_id"] == 99
    assert result is not None
    assert result.get("jid") == "exec1234"


def test_executor_raises_on_run_download_failure(fresh_db, monkeypatch):
    """If pipeline sets _jobs status=failed (with no sidecar), executor raises
    so worker marks the queue row failed."""
    import pipeline

    def _fake_execute(job, adapter, ctx):
        with server._jobs_lock:
            server._jobs[job["jid"]] = {
                "id": job["jid"],
                "status": "failed",
                "error": "tidal-dl-ng timeout",
            }

    monkeypatch.setattr(pipeline, "execute_source_grab", _fake_execute)

    job = {
        "id": 100,
        "jid": "exec_fail",
        "payload_json": json.dumps({"album_id": 666}),
    }
    with pytest.raises(RuntimeError, match="tidal-dl-ng timeout"):
        server._execute_tidal_grab_job(job)


def test_executor_treats_v2_block_sidecar_as_completed_business_result(
    fresh_db, monkeypatch
):
    """V2 BLOCK sets _jobs failed for SAB compatibility, but worker completed."""
    import pipeline

    def _fake_execute(job, adapter, ctx):
        with server._jobs_lock:
            server._jobs[job["jid"]] = {
                "id": job["jid"],
                "status": "failed",
                "error": "v2 policy block: codec mismatch",
            }

    sidecar = {
        "jid": "exec_block",
        "v2_verification_decision": "BLOCK",
        "v2_import_outcome": "SKIPPED",
        "v2_score": 0,
        "verdict": "AUTHENTIC",
        "v2_overrides": ["codec_mismatch"],
        "lifecycle": {"state": "created"},
    }

    monkeypatch.setattr(pipeline, "execute_source_grab", _fake_execute)
    monkeypatch.setattr(
        server, "_read_verification_sidecar", lambda jid: (None, sidecar)
    )

    job = {
        "id": 101,
        "jid": "exec_block",
        "payload_json": json.dumps({"album_id": 777}),
    }
    result_state, result = server._execute_tidal_grab_job(job)

    assert result_state == "blocked"
    assert result == {
        "jid": "exec_block",
        "verification_decision": "BLOCK",
        "import_outcome": "SKIPPED",
    }


def test_executor_sets_terminal_progress_from_failed_sidecar(fresh_db, monkeypatch):
    """A failed import sidecar should not leave progress as Pipeline complete."""
    import pipeline

    jid = "exec_fail_v2"
    worker_job_id = state_db.enqueue_job(jid=jid, type="tidal_grab")

    def _fake_execute(job, adapter, ctx):
        server._set_worker_progress(
            ctx.worker_job_id,
            job["jid"],
            "finalizing",
            98,
            "Finalizing pipeline result",
        )
        with server._jobs_lock:
            server._jobs[job["jid"]] = {
                "id": job["jid"],
                "status": "failed",
                "error": "no importable files after verification",
            }

    sidecar = {
        "jid": jid,
        "v2_verification_decision": "ACCEPT_PROVISIONAL",
        "v2_import_outcome": "FAILED",
        "v2_score": 55,
        "verdict": "SUSPICIOUS",
        "v2_overrides": [],
        "lifecycle": {"state": "created"},
    }

    monkeypatch.setattr(pipeline, "execute_source_grab", _fake_execute)
    monkeypatch.setattr(
        server, "_read_verification_sidecar", lambda jid: (None, sidecar)
    )

    result_state, result = server._execute_tidal_grab_job(
        {
            "id": worker_job_id,
            "jid": jid,
            "payload_json": json.dumps({"album_id": 888}),
        }
    )

    assert result_state == "failed"
    assert result["import_outcome"] == "FAILED"
    progress = json.loads(state_db.get_job(worker_job_id)["progress_json"])
    assert progress["stage"] == "failed"
    assert progress["percent"] == 100
    assert (
        progress["message"] == "Import failed: no importable files after verification"
    )
    assert server._jobs[jid]["stage"] == "failed"
    assert (
        server._jobs[jid]["warning"]
        == "Import failed: no importable files after verification"
    )


def test_executor_missing_album_id_raises(fresh_db):
    """Generic executor (F3.4) raises ValueError when both source_id and
    album_id are absent. Legacy tidal_grab payloads with only album_id are
    still accepted; payloads with neither field are not."""
    job = {"id": 1, "jid": "x", "payload_json": "{}"}
    with pytest.raises(ValueError, match="missing source_id"):
        server._execute_tidal_grab_job(job)


def test_worker_progress_updates_db_and_jobs_projection(fresh_db, monkeypatch):
    monkeypatch.setattr(server, "_jobs", {"prog1": {"id": "prog1"}})
    monkeypatch.setattr(server, "_save_jobs", lambda: None)
    job_id = state_db.enqueue_job(jid="prog1", type="tidal_grab")

    server._set_worker_progress(
        job_id, "prog1", "downloading", 25, "Downloading from TIDAL"
    )

    job = state_db.get_job(job_id)
    progress = json.loads(job["progress_json"])
    assert progress["stage"] == "downloading"
    assert progress["percent"] == 25
    assert progress["message"] == "Downloading from TIDAL"
    assert server._jobs["prog1"]["percent"] == 25
    assert server._jobs["prog1"]["stage"] == "downloading"


def test_cancellable_subprocess_stops_and_cleans_work_dir(
    fresh_db, tmp_path, monkeypatch
):
    jid = "cancelrun"
    work_dir = tmp_path / "downloads" / jid
    work_dir.mkdir(parents=True)
    (work_dir / "partial.tmp").write_text("partial")
    monkeypatch.setattr(server, "DOWNLOAD_BASE", tmp_path / "downloads")

    job_id = state_db.enqueue_job(jid=jid, type="tidal_grab")
    state_db.dequeue_next_job(worker_id="worker-1")
    assert state_db.request_job_cancel(job_id) is True

    with pytest.raises(worker.JobCancelled):
        server._run_cancellable_subprocess(
            ["sleep", "30"],
            worker_job_id=job_id,
            jid=jid,
            work_dir=work_dir,
            timeout=60,
        )

    assert not work_dir.exists()
    assert server._jobs[jid]["error"] == "cancelled by user"


def test_addurl_falls_back_to_thread_if_enqueue_fails(
    fresh_db, mock_session, monkeypatch
):
    """If state_db.enqueue_job returns None, fallback spawns direct thread."""
    monkeypatch.setenv("TIDALHIRES_DISABLE_WORKER", "1")
    monkeypatch.setattr(state_db, "enqueue_job", lambda **kw: None)
    monkeypatch.setattr(state_db, "find_active_job_by_dedupe", lambda key: None)

    started_threads = []

    class _MockThread:
        def __init__(self, target, args, daemon=True):
            self.target = target
            self.args = args
            started_threads.append(self)

        def start(self):
            pass  # Don't actually run

    monkeypatch.setattr(server.threading, "Thread", _MockThread)

    client = server.app.test_client()
    r = _post_addurl(client, album_id=11111)
    assert r.status_code == 200
    assert len(started_threads) == 1  # Fallback thread spawned
    assert started_threads[0].args[1] == 11111  # album_id passed
