"""ADR-0014 slice 4a — AddUrl backend-lane routing (submit + persist + queue).

The category is the sole trigger, gated by explicit enable; no monitor/cancel/
ingest here. Submission HTTP is stubbed so tests never touch a real SAB.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def backend_env(tmp_path, monkeypatch):
    import server
    import state_db
    import download_backend
    from download_backend import BackendJob

    state_db._initialized = False
    if state_db._db_path.exists():
        state_db._db_path.unlink()
    state_db.init()

    monkeypatch.setenv("MINTARR_SAB_BACKEND_ENABLED", "true")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_URL", "http://sab")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_API_KEY", "KEY")
    monkeypatch.setenv("MINTARR_SAB_BACKEND_CATEGORY", "mintarr-music")

    # Isolate the in-memory queue projection + skip disk persistence.
    monkeypatch.setattr(server, "_jobs", {})
    monkeypatch.setattr(server, "_save_jobs", lambda: None)

    # Stub the actual SAB submit so no real HTTP happens.
    def _fake_submit(self, *, url):
        return BackendJob(backend_job_id="NZO-1", category=self.category)

    monkeypatch.setattr(download_backend.SabBackendClient, "submit", _fake_submit)

    server.app.config["TESTING"] = True
    return server.app.test_client()


def _key():
    return os.environ["TIDALHIRES_API_KEY"]


# ---- trigger logic -------------------------------------------------------


def test_backend_lane_selected_only_for_configured_category(backend_env, monkeypatch):
    import server

    assert server._backend_lane_for_category("mintarr-music") is not None
    # wrong category → not our lane (fall through)
    assert server._backend_lane_for_category("tv") is None
    assert server._backend_lane_for_category("") is None


def test_backend_lane_disabled_returns_none(backend_env, monkeypatch):
    import server

    monkeypatch.setenv("MINTARR_SAB_BACKEND_ENABLED", "false")
    assert server._backend_lane_for_category("mintarr-music") is None


def test_get_cats_advertises_backend_category_when_enabled(backend_env):
    import server

    assert server._sab_category_names() == ["*", "music", "mintarr-music"]
    # the SAB-compat endpoint Lidarr validates against
    r = backend_env.get(f"/sabnzbd/api?mode=get_cats&apikey={_key()}")
    assert "mintarr-music" in r.get_json()["categories"]


def test_get_cats_omits_backend_category_when_disabled(backend_env, monkeypatch):
    import server

    monkeypatch.setenv("MINTARR_SAB_BACKEND_ENABLED", "false")
    assert server._sab_category_names() == ["*", "music"]
    r = backend_env.get(f"/sabnzbd/api?mode=get_cats&apikey={_key()}")
    assert r.get_json()["categories"] == ["*", "music"]


# ---- addurl integration --------------------------------------------------


def test_addurl_category_match_routes_to_backend(backend_env):
    import state_db

    r = backend_env.post(
        f"/sabnzbd/api?apikey={_key()}",
        data={"mode": "addurl", "name": "http://indexer/x.nzb", "cat": "mintarr-music"},
    )
    assert r.status_code == 200, r.data
    payload = r.get_json()
    assert payload["status"] is True
    jid = payload["nzo_ids"][0]

    job = state_db.get_backend_job(jid)
    assert job is not None
    assert job["backend_job_id"] == "NZO-1"
    assert job["source_type"] == "sab_usenet_backend"
    assert job["category"] == "mintarr-music"
    assert job["state"] == "downloading"


def test_addurl_backend_job_shows_in_lidarr_queue(backend_env):
    backend_env.post(
        f"/sabnzbd/api?apikey={_key()}",
        data={"mode": "addurl", "name": "http://indexer/x.nzb", "cat": "mintarr-music"},
    )
    q = backend_env.get(f"/sabnzbd/api?mode=queue&apikey={_key()}").get_json()
    slots = q["queue"]["slots"]
    assert len(slots) == 1
    assert slots[0]["status"] == "Downloading"
    assert slots[0]["cat"] == "mintarr-music"


def test_addurl_wrong_category_falls_through_to_existing_path(backend_env):
    # Not the backend category → existing parsing handles it; an unknown prefix
    # is rejected exactly as before (proves no backend-lane guessing).
    r = backend_env.post(
        f"/sabnzbd/api?apikey={_key()}",
        data={"mode": "addurl", "name": "foo:bar", "cat": "tv"},
    )
    assert r.status_code == 400
    assert "no recognized source prefix" in r.get_json()["error"]


def test_addurl_backend_lane_rejects_addfile(backend_env):
    # addfile (uploaded NZB, no URL name) is not supported in 4a — fail closed
    # inside the lane, do not mishandle.
    import io

    r = backend_env.post(
        f"/sabnzbd/api?apikey={_key()}",
        data={
            "mode": "addfile",
            "cat": "mintarr-music",
            "nzbfile": (io.BytesIO(b"<nzb/>"), "x.nzb"),
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
    assert "addurl URL only" in r.get_json()["error"]


def test_addurl_title_never_uses_raw_url(backend_env):
    # A raw download URL can carry an indexer apikey; it must not become the
    # title (which lands in backend_jobs.release_title + the SAB queue filename).
    import state_db

    leaky = "http://indexer/x.nzb?apikey=INDEXER_SECRET"
    r = backend_env.post(
        f"/sabnzbd/api?apikey={_key()}",
        data={"mode": "addurl", "name": leaky, "cat": "mintarr-music"},
    )
    jid = r.get_json()["nzo_ids"][0]
    title = state_db.get_backend_job(jid)["release_title"]
    assert "INDEXER_SECRET" not in title
    assert title == "backend release NZO-1"


def test_addurl_title_uses_nzbname_when_present(backend_env):
    import state_db

    r = backend_env.post(
        f"/sabnzbd/api?apikey={_key()}",
        data={
            "mode": "addurl",
            "name": "http://indexer/x.nzb?apikey=SECRET",
            "cat": "mintarr-music",
            "nzbname": "Artist - Album",
        },
    )
    jid = r.get_json()["nzo_ids"][0]
    assert state_db.get_backend_job(jid)["release_title"] == "Artist - Album"


def test_addurl_rolls_back_when_persist_fails(backend_env, monkeypatch):
    # Submit succeeded but durable persist failed → roll the backend job back
    # (non-destructive), do not project to Lidarr, and fail the add.
    import download_backend
    import server
    import state_db

    monkeypatch.setattr(state_db, "create_backend_job", lambda *a, **k: None)
    cancelled = []

    def _fake_cancel(self, backend_job_id, *, delete_files=False):
        cancelled.append((backend_job_id, delete_files))
        return True

    monkeypatch.setattr(download_backend.SabBackendClient, "cancel", _fake_cancel)

    r = backend_env.post(
        f"/sabnzbd/api?apikey={_key()}",
        data={"mode": "addurl", "name": "http://indexer/x.nzb", "cat": "mintarr-music"},
    )
    assert r.status_code == 502
    assert r.get_json()["status"] is False
    assert cancelled == [("NZO-1", False)]  # rolled back, no data deletion
    assert server._jobs == {}  # no orphaned queue projection


def test_addurl_backend_submit_failure_returns_502(backend_env, monkeypatch):
    import download_backend

    def _boom(self, *, url):
        raise RuntimeError("sab down")

    monkeypatch.setattr(download_backend.SabBackendClient, "submit", _boom)
    r = backend_env.post(
        f"/sabnzbd/api?apikey={_key()}",
        data={"mode": "addurl", "name": "http://indexer/x.nzb", "cat": "mintarr-music"},
    )
    assert r.status_code == 502
    assert r.get_json()["status"] is False
