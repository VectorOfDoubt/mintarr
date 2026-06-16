"""ADR-0014 — dashboard backend-jobs observability endpoint.

Read-only operator view over the Mintarr-managed SAB/qBit backend lane: which
grabs are in flight and how recent ones resolved. No secrets surfaced.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def backend_api_env(tmp_path, monkeypatch):
    import server
    import state_db

    state_db._initialized = False
    if state_db._db_path.exists():
        state_db._db_path.unlink()
    state_db.init()

    server.app.config["TESTING"] = True
    return server.app.test_client()


def _key():
    return os.environ["TIDALHIRES_API_KEY"]


def test_empty_backend_jobs(backend_api_env):
    client = backend_api_env
    r = client.get(f"/dashboard/v1/backend-jobs?apikey={_key()}")
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"total": 0, "active": 0, "jobs": []}


def test_lists_jobs_newest_first_with_active_count(backend_api_env):
    import state_db

    client = backend_api_env
    state_db.create_backend_job(
        "old",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-1",
        state="imported",
        target_album_id=42,
        release_title="Artist - Old",
        now=1.0,
    )
    state_db.create_backend_job(
        "new",
        source_type="sab_usenet_backend",
        category="mintarr-music",
        backend_job_id="NZO-2",
        state="downloading",
        target_album_id=43,
        release_title="Artist - New",
        now=2.0,
    )

    r = client.get(f"/dashboard/v1/backend-jobs?apikey={_key()}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 2
    assert body["active"] == 1  # only the downloading job is active
    # newest first
    assert [j["jid"] for j in body["jobs"]] == ["new", "old"]
    top = body["jobs"][0]
    assert top["state"] == "downloading"
    assert top["release_title"] == "Artist - New"
    assert top["target_album_id"] == 43
    assert top["backend_job_id"] == "NZO-2"
    # only the curated view fields are exposed (no raw details_json blob)
    assert set(top) == {
        "jid",
        "state",
        "category",
        "source_type",
        "release_title",
        "target_album_id",
        "backend_job_id",
        "created_at",
        "updated_at",
        "finished_at",
    }


def test_requires_api_key(backend_api_env):
    client = backend_api_env
    r = client.get("/dashboard/v1/backend-jobs")
    assert r.status_code in (401, 403)
