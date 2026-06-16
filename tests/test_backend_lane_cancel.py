"""ADR-0014 slice 4c — backend-lane cancel/remove propagation.

A Lidarr remove/blocklist (SAB delete) for a backend job must stop the backend
transfer, not just hide the job. Non-destructive (leave data/seeding); the
exact-release blocklist Lidarr applies is untouched. No real HTTP.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def cancel_env(tmp_path, monkeypatch):
    import server
    import state_db
    import download_backend

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
    # isolate from the hide path (existing behavior, not under test here)
    monkeypatch.setattr(server, "_hide_from_lidarr", lambda jid: None)
    # never reach Lidarr for the album-hold albumId fallback; the album-hold
    # path under test resolves the id from durable backend state, not the network
    monkeypatch.setattr(
        server, "_lidarr_queue_record_for_download_id", lambda *a, **k: None
    )

    cancelled = []

    def _fake_cancel(self, backend_job_id, *, delete_files=False):
        cancelled.append((backend_job_id, delete_files))
        return True

    monkeypatch.setattr(download_backend.SabBackendClient, "cancel", _fake_cancel)
    return server, cancelled


def _seed(server, *, state="downloading"):
    import state_db

    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state=state,
        release_title="Artist - Album",
    )
    server._jobs["jid-1"] = {
        "id": "jid-1",
        "category": "mintarr-music",
        "status": state,
        "title": "Artist - Album",
        "source_type": "sab_usenet_backend",
        "source_id": "NZO-1",
    }


def test_cancel_propagates_to_backend(cancel_env):
    import state_db

    server, cancelled = cancel_env
    _seed(server, state="downloading")

    server._cancel_and_hide_job("jid-1")

    # non-destructive backend cancel fired
    assert cancelled == [("NZO-1", False)]
    job = state_db.get_backend_job("jid-1")
    assert job["state"] == "cancelled"
    assert job["finished_at"] is not None


def test_cancel_skips_already_terminal(cancel_env):
    import state_db

    server, cancelled = cancel_env
    _seed(server, state="failed")

    server._cancel_and_hide_job("jid-1")

    # already terminal → nothing to propagate, state preserved
    assert cancelled == []
    assert state_db.get_backend_job("jid-1")["state"] == "failed"


def test_cancel_unknown_jid_is_noop(cancel_env):
    server, cancelled = cancel_env
    # neither a worker job nor a backend job → no crash, no cancel
    server._cancel_and_hide_job("does-not-exist")
    assert cancelled == []


def test_cancel_marks_cancelled_even_if_lane_disabled(cancel_env, monkeypatch):
    import state_db

    server, cancelled = cancel_env
    _seed(server, state="downloading")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_ENABLED", "false")

    server._cancel_and_hide_job("jid-1")

    # can't reach the backend, but Lidarr's intent is recorded durably
    assert cancelled == []
    assert state_db.get_backend_job("jid-1")["state"] == "cancelled"


def test_cancel_keeps_visible_when_durable_cancel_fails(cancel_env, monkeypatch):
    import state_db

    server, cancelled = cancel_env
    _seed(server, state="downloading")
    hidden = []
    monkeypatch.setattr(server, "_hide_from_lidarr", lambda jid: hidden.append(jid))
    # durable cancel does not persist
    monkeypatch.setattr(state_db, "update_backend_job", lambda *a, **k: None)

    server._cancel_and_hide_job("jid-1")

    # split-brain guard: not hidden from Lidarr, and the backend cancel is never
    # attempted (durable intent must land first)
    assert hidden == []
    assert cancelled == []


def test_cancel_holds_album_when_target_known(cancel_env):
    import state_db

    server, _ = cancel_env
    # a backend row carries a trusted Lidarr albumId captured during the grab
    state_db.create_backend_job(
        "jid-1",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="downloading",
        target_album_id=9619,
        release_title="Artist - Album",
    )
    server._jobs["jid-1"] = {"id": "jid-1", "category": "mintarr-music"}

    server._cancel_and_hide_job("jid-1")

    # without the hold, Lidarr blocklists the cancelled release and immediately
    # re-grabs an alternative of the same album (observed live)
    hold = state_db.get_album_hold(9619)
    assert hold is not None
    assert hold["reason"] == "operator_cancelled_active_grab"


def test_cancel_without_known_album_creates_no_hold(cancel_env):
    import state_db

    server, _ = cancel_env
    _seed(server, state="downloading")  # no target_album_id anywhere

    server._cancel_and_hide_job("jid-1")

    # no trusted albumId → never guess from titles, so no hold is created
    total, _rows = state_db.list_album_holds(active_only=True)
    assert total == 0


def test_cancel_via_sab_delete_endpoint(cancel_env):
    import os
    import state_db

    server, cancelled = cancel_env
    _seed(server, state="downloading")
    client = server.app.test_client()

    r = client.post(
        f"/sabnzbd/api?apikey={os.environ['TIDALHIRES_API_KEY']}",
        data={"mode": "queue", "name": "delete", "value": "jid-1"},
    )
    assert r.status_code == 200
    assert cancelled == [("NZO-1", False)]
    assert state_db.get_backend_job("jid-1")["state"] == "cancelled"
