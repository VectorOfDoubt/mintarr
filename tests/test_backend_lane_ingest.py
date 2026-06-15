"""ADR-0014 slice 5 — completion ingest into the normal QC/import pipeline.

A completed + contained backend download is copied (copy-not-move) into the
managed work dir and handed to the existing pipeline. Durable-first: importing
is persisted before the copy job is enqueued. No direct ManualImport here.
"""

from __future__ import annotations

import json
import os

import pytest


# ---- adapter -------------------------------------------------------------


class _Ctx:
    def __init__(self, raw_dir):
        self.raw_dir = raw_dir
        self.output_dir = raw_dir

    def check_cancelled(self):
        pass

    def set_progress(self, **kw):
        pass

    def log(self, *a, **k):
        pass


def test_backend_completed_adapter_identity():
    from adapters.completed_folder import BackendCompletedAdapter

    a = BackendCompletedAdapter()
    assert a.name == "backend_completed"
    assert a.source_type == "backend_completed"
    assert a.env_prefix == "MINTARR_SAB_BACKEND"
    # not an indexer
    assert a.search("anything") == []


def test_backend_completed_adapter_copies_contained_dir(tmp_path):
    from adapters.completed_folder import BackendCompletedAdapter

    root = tmp_path / "backend-complete"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 track.flac").write_bytes(b"FLAC" + bytes(1024))

    adapter = BackendCompletedAdapter(
        download_root=str(root), enabled=True, settle_seconds=0
    )
    raw = tmp_path / "work"
    out = adapter.download_raw("Artist/Album", _Ctx(raw))

    assert out.file_count == 1
    assert (raw / "01 track.flac").exists()  # copied
    assert (album / "01 track.flac").exists()  # original kept (copy-not-move)


def test_backend_completed_adapter_rejects_traversal(tmp_path):
    from adapters.completed_folder import BackendCompletedAdapter

    root = tmp_path / "root"
    root.mkdir()
    adapter = BackendCompletedAdapter(
        download_root=str(root), enabled=True, settle_seconds=0
    )
    with pytest.raises(RuntimeError):
        adapter.download_raw("../escape", _Ctx(tmp_path / "work"))


# ---- reconcile -> ingest wiring ------------------------------------------


@pytest.fixture
def ingest_env(tmp_path, monkeypatch):
    import server
    import state_db

    state_db._initialized = False
    if state_db._db_path.exists():
        state_db._db_path.unlink()
    state_db.init()

    root = tmp_path / "backend-complete"
    root.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setenv("MINTARR_SAB_BACKEND_ENABLED", "true")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_URL", "http://sab")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_API_KEY", "KEY")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_CATEGORY", "mintarr-music")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_DOWNLOAD_ROOT", str(root))
    monkeypatch.setattr(server, "OUTPUT_BASE", output)
    monkeypatch.setattr(server, "_jobs", {})
    monkeypatch.setattr(server, "_save_jobs", lambda: None)
    return server, root


def _pending_sidecar(jid="jid-1"):
    return {
        "jid": jid,
        "title": "Artist - Album",
        "album_ids": [1234],
        "ts": 1779600000.0,
        "v2_verification_decision": "ACCEPT",
        "v2_import_outcome": "PENDING",
        "v2_score": 85,
        "verdict": "AUTHENTIC",
        "lifecycle": {
            "state": "created",
            "created_at": 1779600000.0,
            "actor": None,
        },
        "source_type": "backend_completed",
    }


def _write_pending_sidecar(server, jid="jid-1"):
    import state_db
    from dashboard import derive_status

    sidecar = _pending_sidecar(jid)
    path = server.OUTPUT_BASE / jid / "verification.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(sidecar))
    state_db.upsert_from_sidecar(sidecar, derived_status=derive_status(sidecar))
    return path


def _client_with_root(root):
    return type("C", (), {"download_root": str(root)})()


def _seed_completed(server, root):
    import state_db

    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="completed",
        release_title="Artist - Album",
        target_album_id=1234,
    )


def test_start_ingest_persists_importing_then_enqueues(ingest_env, monkeypatch):
    import state_db

    server, root = ingest_env
    _seed_completed(server, root)
    enq = []
    monkeypatch.setattr(state_db, "enqueue_job", lambda **kw: enq.append(kw) or 1)

    done = root / "Artist" / "Album"
    done.mkdir(parents=True)
    server._start_backend_ingest(
        "jid-1", state_db.get_backend_job("jid-1"), str(done), _client_with_root(root)
    )

    # durable-first: importing persisted, then enqueue with the relative path
    assert state_db.get_backend_job("jid-1")["state"] == "importing"
    assert len(enq) == 1
    assert enq[0]["type"] == "backend_completed_grab"
    assert enq[0]["source_type"] == "backend_completed"
    assert enq[0]["source_id"] == "Artist/Album"  # relative to the root
    assert enq[0]["payload"]["target_album_id"] == 1234


def test_start_ingest_no_enqueue_when_importing_not_persisted(ingest_env, monkeypatch):
    import state_db

    server, root = ingest_env
    _seed_completed(server, root)
    monkeypatch.setattr(state_db, "update_backend_job", lambda *a, **k: None)
    enq = []
    monkeypatch.setattr(state_db, "enqueue_job", lambda **kw: enq.append(kw) or 1)

    done = root / "Album"
    done.mkdir()
    server._start_backend_ingest(
        "jid-1", {"category": "mintarr-music"}, str(done), _client_with_root(root)
    )

    # durable importing did not persist → never enqueued
    assert enq == []


def test_start_ingest_reverts_on_enqueue_failure(ingest_env, monkeypatch):
    import state_db

    server, root = ingest_env
    _seed_completed(server, root)

    def _boom(**kw):
        raise RuntimeError("queue down")

    monkeypatch.setattr(state_db, "enqueue_job", _boom)
    done = root / "Album"
    done.mkdir()
    server._start_backend_ingest(
        "jid-1", state_db.get_backend_job("jid-1"), str(done), _client_with_root(root)
    )

    # reverted out of importing so a later reconcile retries
    assert state_db.get_backend_job("jid-1")["state"] == "completed"


def test_start_ingest_skips_path_not_under_root(ingest_env, monkeypatch):
    import state_db

    server, root = ingest_env
    _seed_completed(server, root)
    enq = []
    monkeypatch.setattr(state_db, "enqueue_job", lambda **kw: enq.append(kw) or 1)

    server._start_backend_ingest(
        "jid-1",
        state_db.get_backend_job("jid-1"),
        "/etc/passwd",  # not under the backend root
        _client_with_root(root),
    )

    assert enq == []
    assert state_db.get_backend_job("jid-1")["state"] == "completed"


def test_start_ingest_reverts_when_enqueue_returns_none(ingest_env, monkeypatch):
    import state_db

    server, root = ingest_env
    _seed_completed(server, root)
    monkeypatch.setattr(state_db, "enqueue_job", lambda **kw: None)  # no job created
    done = root / "Album"
    done.mkdir()
    server._start_backend_ingest(
        "jid-1", state_db.get_backend_job("jid-1"), str(done), _client_with_root(root)
    )
    # never leave the row stuck in importing with no worker
    assert state_db.get_backend_job("jid-1")["state"] == "completed"


# ---- outcome mirrored back from the pipeline ------------------------------


def _seed_importing(server):
    import state_db

    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="importing",
    )


@pytest.mark.parametrize(
    ("proj", "expected"),
    [
        ({"status": "completed"}, "imported"),
        ({"status": "review_required"}, "review"),
        ({"lidarr_hold": True, "status": "downloading"}, "review"),
        ({"status": "failed"}, "failed"),
    ],
)
def test_ingest_outcome_mirrored_back(ingest_env, monkeypatch, proj, expected):
    import state_db

    server, _root = ingest_env
    _seed_importing(server)
    server._jobs["jid-1"] = {"id": "jid-1", **proj}
    monkeypatch.setattr(
        server, "_execute_source_grab_job", lambda job, name: ("completed", {})
    )

    server._execute_backend_completed_grab_job({"jid": "jid-1", "id": 1})

    assert state_db.get_backend_job("jid-1")["state"] == expected


def test_imported_backend_ingest_closes_pending_record_audit(ingest_env, monkeypatch):
    import state_db

    server, _root = ingest_env
    _seed_importing(server)
    sidecar_path = _write_pending_sidecar(server)
    server._jobs["jid-1"] = {
        "id": "jid-1",
        "status": "completed",
        "output_dir": str(server.OUTPUT_BASE / "jid-1"),
    }
    monkeypatch.setattr(
        server, "_execute_source_grab_job", lambda job, name: ("completed", {})
    )

    server._execute_backend_completed_grab_job({"jid": "jid-1", "id": 1})

    assert state_db.get_backend_job("jid-1")["state"] == "imported"
    assert (
        json.loads(sidecar_path.read_text())["v2_import_outcome"] == "MANUAL_IMPORTED"
    )
    rec = state_db.get_record("jid-1")
    assert rec["import_outcome"] == "MANUAL_IMPORTED"
    assert rec["derived_status"] == "imported"
    assert server._jobs["jid-1"]["hidden_from_lidarr"] is True


def test_queue_poll_reconciles_imported_backend_pending_record_after_restart(
    ingest_env,
):
    import state_db

    server, _root = ingest_env
    sidecar_path = _write_pending_sidecar(server)
    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="imported",
        release_title="Artist - Album",
    )
    # Simulate a redeploy/restart: durable state remains, in-memory projection is gone.
    assert server._jobs == {}

    q = (
        server.app.test_client()
        .get(f"/sabnzbd/api?mode=queue&apikey={os.environ['TIDALHIRES_API_KEY']}")
        .get_json()
    )

    assert q["queue"]["slots"] == []
    assert (
        json.loads(sidecar_path.read_text())["v2_import_outcome"] == "MANUAL_IMPORTED"
    )
    rec = state_db.get_record("jid-1")
    assert rec["import_outcome"] == "MANUAL_IMPORTED"
    assert rec["derived_status"] == "imported"


def test_imported_backend_job_is_not_left_visible_in_sab_queue(ingest_env):
    import state_db

    server, _root = ingest_env
    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="imported",
        release_title="Artist - Album",
    )
    # This stale in-memory projection is what a completed backend ingest leaves
    # behind; the durable terminal row must win so Lidarr does not see it as an
    # eternal "Verifying" queue item.
    server._jobs["jid-1"] = {
        "id": "jid-1",
        "status": "processing",
        "title": "Artist - Album",
        "category": "mintarr-music",
        "size": 1234,
        "percent": 100,
        "source_type": "sab_usenet_backend",
    }

    q = (
        server.app.test_client()
        .get(f"/sabnzbd/api?mode=queue&apikey={os.environ['TIDALHIRES_API_KEY']}")
        .get_json()
    )

    assert q["queue"]["slots"] == []


def test_ingest_outcome_failed_when_pipeline_raises(ingest_env, monkeypatch):
    import state_db

    server, _root = ingest_env
    _seed_importing(server)

    def _boom(job, name):
        raise RuntimeError("pipeline blew up")

    monkeypatch.setattr(server, "_execute_source_grab_job", _boom)

    with pytest.raises(RuntimeError):
        server._execute_backend_completed_grab_job({"jid": "jid-1", "id": 1})

    # failure mirrored before re-raise → not stuck in importing
    assert state_db.get_backend_job("jid-1")["state"] == "failed"


def test_ingest_outcome_only_applies_while_importing(ingest_env, monkeypatch):
    import state_db

    server, _root = ingest_env
    # already terminal (e.g. cancelled mid-flight) — outcome must not clobber it
    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="cancelled",
    )
    server._jobs["jid-1"] = {"id": "jid-1", "status": "completed"}
    monkeypatch.setattr(
        server, "_execute_source_grab_job", lambda job, name: ("completed", {})
    )

    server._execute_backend_completed_grab_job({"jid": "jid-1", "id": 1})

    assert state_db.get_backend_job("jid-1")["state"] == "cancelled"


def test_reconcile_skips_importing_jobs(ingest_env, monkeypatch):
    import download_backend

    server, root = ingest_env
    import state_db

    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="importing",
    )
    polled = []
    monkeypatch.setattr(
        download_backend.SabBackendClient,
        "status",
        lambda self, bid: polled.append(bid),
    )

    server._reconcile_backend_jobs()

    # importing is worker-owned: reconcile must not poll or touch it
    assert polled == []
