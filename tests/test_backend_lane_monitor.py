"""ADR-0014 slice 4b — backend-lane monitor/poll/reconcile.

Reconcile updates the durable backend_jobs row and the Lidarr-facing _jobs
projection. A completed backend download is held (not importable) until slice 5.
No real HTTP — client.status is stubbed.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def monitor_env(tmp_path, monkeypatch):
    import server
    import state_db

    state_db._initialized = False
    if state_db._db_path.exists():
        state_db._db_path.unlink()
    state_db.init()

    monkeypatch.setenv("MINTARR_SAB_BACKEND_ENABLED", "true")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_URL", "http://sab")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_API_KEY", "KEY")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_CATEGORY", "mintarr-music")
    monkeypatch.setattr(server, "_jobs", {})
    monkeypatch.setattr(server, "_save_jobs", lambda: None)

    server.app.config["TESTING"] = True
    return server


def _stub_status(
    monkeypatch, state, *, progress=0.0, completed_path=None, display_name=None
):
    import download_backend
    from download_backend import BackendJobStatus

    def _status(self, backend_job_id):
        return BackendJobStatus(
            backend_job_id, state, progress, completed_path, state.value, display_name
        )

    monkeypatch.setattr(download_backend.SabBackendClient, "status", _status)


def _seed(server, *, state="downloading", with_projection=True):
    import state_db

    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state=state,
        release_title="Artist - Album",
    )
    if with_projection:
        server._jobs["jid-1"] = {
            "id": "jid-1",
            "category": "mintarr-music",
            "status": state,
            "title": "Artist - Album",
            "size": 0,
            "percent": 0,
            "source_type": "sab_usenet_backend",
            "source_id": "NZO-1",
        }


def _seed_generic_title(server):
    import state_db

    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="downloading",
        release_title="backend release NZO-1",
    )
    server._jobs["jid-1"] = {
        "id": "jid-1",
        "category": "mintarr-music",
        "status": "downloading",
        "title": "backend release NZO-1",
        "size": 0,
        "percent": 0,
        "source_type": "sab_usenet_backend",
        "source_id": "NZO-1",
    }


def _key():
    return os.environ["TIDALHIRES_API_KEY"]


# ---- reconcile transitions -----------------------------------------------


def test_reconcile_downloading_updates_progress(monitor_env, monkeypatch):
    import state_db
    from download_backend import BackendState

    server = monitor_env
    _seed(server)
    _stub_status(monkeypatch, BackendState.DOWNLOADING, progress=42.0)

    server._reconcile_backend_jobs()

    assert state_db.get_backend_job("jid-1")["state"] == "downloading"
    assert server._jobs["jid-1"]["status"] == "downloading"
    assert server._jobs["jid-1"]["percent"] == 42


def test_reconcile_captures_target_album_id_from_lidarr_context(
    monitor_env, monkeypatch
):
    import state_db
    from download_backend import BackendState

    server = monitor_env
    _seed(server)
    _stub_status(monkeypatch, BackendState.DOWNLOADING, progress=42.0)
    monkeypatch.setattr(server, "_get_lidarr_key", lambda: "lidarr-key")
    monkeypatch.setattr(
        server,
        "_infer_lidarr_target_album_id",
        lambda jid, api, key: 9829,
    )

    server._reconcile_backend_jobs()

    assert state_db.get_backend_job("jid-1")["target_album_id"] == 9829
    assert server._jobs["jid-1"]["target_album_id"] == 9829


def test_reconcile_hydrates_generic_title_from_backend_status(monitor_env, monkeypatch):
    import state_db
    from download_backend import BackendState

    server = monitor_env
    _seed_generic_title(server)
    _stub_status(
        monkeypatch,
        BackendState.DOWNLOADING,
        progress=42.0,
        display_name="Artist - Album Proper",
    )

    server._reconcile_backend_jobs()

    assert state_db.get_backend_job("jid-1")["release_title"] == "Artist - Album Proper"
    assert server._jobs["jid-1"]["title"] == "Artist - Album Proper"


def test_reconcile_completed_is_held_not_importable(monitor_env, monkeypatch, tmp_path):
    import state_db
    from download_backend import BackendState

    server = monitor_env
    _seed(server)
    done = tmp_path / "Album"
    done.mkdir()
    _stub_status(
        monkeypatch, BackendState.COMPLETED, progress=100.0, completed_path=str(done)
    )

    server._reconcile_backend_jobs()

    job = state_db.get_backend_job("jid-1")
    assert job["state"] == "completed"
    assert job["backend_path"] == str(done)
    # completed is NOT terminal — the worker still imports it (slice 5), so
    # finished_at stays unset until then.
    assert job["finished_at"] is None
    # held as processing/"Verifying", NOT presented to Lidarr as importable
    assert server._jobs["jid-1"]["status"] == "processing"


def test_reconcile_failed(monitor_env, monkeypatch):
    import state_db
    from download_backend import BackendState

    server = monitor_env
    _seed(server)
    _stub_status(monkeypatch, BackendState.FAILED)

    server._reconcile_backend_jobs()

    assert state_db.get_backend_job("jid-1")["state"] == "failed"
    assert server._jobs["jid-1"]["status"] == "failed"


def test_reconcile_recreates_lost_projection(monitor_env, monkeypatch):
    from download_backend import BackendState

    server = monitor_env
    _seed(server, with_projection=False)  # durable job, no _jobs entry
    assert "jid-1" not in server._jobs
    _stub_status(monkeypatch, BackendState.DOWNLOADING, progress=10.0)

    server._reconcile_backend_jobs()

    # recovery: projection rebuilt from durable state
    assert server._jobs["jid-1"]["status"] == "downloading"
    assert server._jobs["jid-1"]["title"] == "Artist - Album"


def test_reconcile_unknown_does_not_flip_state(monitor_env, monkeypatch):
    import state_db
    from download_backend import BackendState

    server = monitor_env
    _seed(server)
    _stub_status(monkeypatch, BackendState.UNKNOWN)

    server._reconcile_backend_jobs()

    assert state_db.get_backend_job("jid-1")["state"] == "downloading"


def test_reconcile_skips_when_lane_disabled(monitor_env, monkeypatch):
    import state_db
    from download_backend import BackendState

    server = monitor_env
    _seed(server)
    monkeypatch.setenv("MINTARR_SAB_BACKEND_ENABLED", "false")
    _stub_status(monkeypatch, BackendState.COMPLETED, progress=100.0)

    server._reconcile_backend_jobs()

    # disabled → no client → no reconcile
    assert state_db.get_backend_job("jid-1")["state"] == "downloading"


def test_reconcile_skips_projection_when_durable_update_returns_none(
    monitor_env, monkeypatch
):
    import state_db
    from download_backend import BackendState

    server = monitor_env
    _seed(server)  # downloading + matching projection
    _stub_status(monkeypatch, BackendState.COMPLETED, progress=100.0)
    monkeypatch.setattr(state_db, "update_backend_job", lambda *a, **k: None)

    server._reconcile_backend_jobs()

    # durable update failed → projection must NOT advance to processing/Verifying
    assert server._jobs["jid-1"]["status"] == "downloading"


def test_reconcile_skips_projection_when_durable_update_raises(
    monitor_env, monkeypatch
):
    import state_db
    from download_backend import BackendState

    server = monitor_env
    _seed(server)
    _stub_status(monkeypatch, BackendState.FAILED)

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(state_db, "update_backend_job", _boom)

    server._reconcile_backend_jobs()

    # durable update raised → projection must NOT advance to failed
    assert server._jobs["jid-1"]["status"] == "downloading"


def test_reconcile_restores_review_hold_projection_after_restart(monitor_env):
    import state_db

    server = monitor_env
    _seed(server, state="review", with_projection=False)
    assert "jid-1" not in server._jobs

    server._reconcile_backend_jobs()

    assert state_db.get_backend_job("jid-1")["state"] == "review"
    assert server._jobs["jid-1"]["status"] == "review_required"
    assert server._jobs["jid-1"]["lidarr_hold"] is True
    assert server._jobs["jid-1"]["percent"] == 100


def test_reconcile_restores_review_hold_with_hydrated_title(monitor_env, monkeypatch):
    import state_db
    from download_backend import BackendState

    server = monitor_env
    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="review",
        release_title="backend release NZO-1",
    )
    _stub_status(
        monkeypatch,
        BackendState.COMPLETED,
        progress=100.0,
        display_name="Artist - Album Proper",
    )

    server._reconcile_backend_jobs()

    assert state_db.get_backend_job("jid-1")["release_title"] == "Artist - Album Proper"
    assert server._jobs["jid-1"]["title"] == "Artist - Album Proper"
    assert server._jobs["jid-1"]["status"] == "review_required"


def test_queue_poll_includes_durable_review_hold_after_restart(monitor_env):
    import state_db

    server = monitor_env
    _seed(server, state="review", with_projection=False)

    client = server.app.test_client()
    q = client.get(f"/sabnzbd/api?mode=queue&apikey={_key()}").get_json()

    slots = q["queue"]["slots"]
    assert len(slots) == 1
    assert slots[0]["nzo_id"] == "jid-1"
    assert slots[0]["status"] == "Paused"
    assert state_db.get_backend_job("jid-1")["state"] == "review"


def test_queue_poll_includes_durable_review_with_minimal_record(monitor_env):
    import state_db

    server = monitor_env
    _seed(server, state="review", with_projection=False)
    state_db.upsert_record(
        {
            "jid": "jid-1",
            "title": "backend release NZO-1",
        },
        derived_status=None,
    )

    client = server.app.test_client()
    q = client.get(f"/sabnzbd/api?mode=queue&apikey={_key()}").get_json()

    slots = q["queue"]["slots"]
    assert len(slots) == 1
    assert slots[0]["nzo_id"] == "jid-1"
    assert slots[0]["status"] == "Paused"


def test_queue_poll_keeps_durable_review_for_unrecognized_status(monitor_env):
    import state_db

    server = monitor_env
    _seed(server, state="review", with_projection=False)
    state_db.upsert_record(
        {
            "jid": "jid-1",
            "title": "Artist - Album",
        },
        derived_status="future_transient_status",
    )

    client = server.app.test_client()
    q = client.get(f"/sabnzbd/api?mode=queue&apikey={_key()}").get_json()

    slots = q["queue"]["slots"]
    assert len(slots) == 1
    assert slots[0]["nzo_id"] == "jid-1"
    assert slots[0]["status"] == "Paused"


def test_queue_poll_does_not_resurrect_closed_backend_review(monitor_env):
    import state_db

    server = monitor_env
    _seed(server, state="review", with_projection=False)
    state_db.upsert_record(
        {
            "jid": "jid-1",
            "title": "Artist - Album",
            "lifecycle": {"state": "discarded"},
        },
        derived_status="discarded",
    )

    client = server.app.test_client()
    q = client.get(f"/sabnzbd/api?mode=queue&apikey={_key()}").get_json()

    assert q["queue"]["slots"] == []


# ---- integration: queue poll drives reconcile ----------------------------


def test_queue_poll_reconciles_and_holds_completed(monitor_env, monkeypatch):
    from download_backend import BackendState

    server = monitor_env
    _seed(server)
    _stub_status(monkeypatch, BackendState.COMPLETED, progress=100.0)

    client = server.app.test_client()
    q = client.get(f"/sabnzbd/api?mode=queue&apikey={_key()}").get_json()

    slots = q["queue"]["slots"]
    assert len(slots) == 1
    # completed-but-held shows as Verifying, keeping Lidarr from importing
    assert slots[0]["status"] == "Verifying"
