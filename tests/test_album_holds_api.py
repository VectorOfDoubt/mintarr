"""#160 slice 3c — dashboard album-hold endpoints (list / clear / manual create).

Exercises the operator-facing surface: listing active holds with the snapshot
display metadata, clearing a hold, and manually creating a hold validated
against Lidarr's own catalogue (a typo must not create a phantom hold).
"""

from __future__ import annotations

import os
import sqlite3

import pytest


@pytest.fixture
def holds_api_env(tmp_path, monkeypatch):
    """Server test client + fresh state_db + a fake read-only Lidarr DB."""
    import server
    import state_db

    state_db._initialized = False
    if state_db._db_path.exists():
        state_db._db_path.unlink()
    state_db.init()

    config_xml = tmp_path / "config.xml"
    monkeypatch.setenv("LIDARR_CONFIG_XML", str(config_xml))
    monkeypatch.delenv("MINTARR_LIDARR_DB_PATH", raising=False)

    server.app.config["TESTING"] = True
    return server.app.test_client(), tmp_path


def _make_lidarr_db(tmp_path, albums):
    db_path = tmp_path / "lidarr.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE ArtistMetadata (Id INTEGER PRIMARY KEY, Name TEXT)")
    conn.execute(
        "CREATE TABLE Albums "
        "(Id INTEGER PRIMARY KEY, Title TEXT, ArtistMetadataId INTEGER)"
    )
    meta: dict[str, int] = {}
    for album_id, artist, title in albums:
        if artist not in meta:
            meta[artist] = len(meta) + 1
            conn.execute(
                "INSERT INTO ArtistMetadata (Id, Name) VALUES (?, ?)",
                (meta[artist], artist),
            )
        conn.execute(
            "INSERT INTO Albums (Id, Title, ArtistMetadataId) VALUES (?, ?, ?)",
            (album_id, title, meta[artist]),
        )
    conn.commit()
    conn.close()


def _key():
    return os.environ["TIDALHIRES_API_KEY"]


# ---- list ----------------------------------------------------------------


def test_list_album_holds_shapes_snapshot(holds_api_env):
    import state_db

    client, _tmp = holds_api_env
    state_db.create_album_hold(
        10,
        reason="operator_cancel",
        ttl_seconds=3600,
        details={"artist": "Depeche Mode", "album_title": "Violator"},
    )
    r = client.get(f"/dashboard/v1/album-holds?apikey={_key()}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 1
    hold = body["holds"][0]
    assert hold["album_id"] == 10
    assert hold["artist"] == "Depeche Mode"
    assert hold["album"] == "Violator"
    assert hold["reason"] == "operator_cancel"
    assert 0 < hold["expires_in_sec"] <= 3600


def test_list_album_holds_excludes_cleared(holds_api_env):
    import state_db

    client, _tmp = holds_api_env
    state_db.create_album_hold(10, reason="operator_cancel", ttl_seconds=3600)
    state_db.clear_album_hold(10, actor="test")
    r = client.get(f"/dashboard/v1/album-holds?apikey={_key()}")
    assert r.get_json() == {"total": 0, "holds": []}


def test_list_album_holds_requires_apikey(holds_api_env):
    client, _tmp = holds_api_env
    assert client.get("/dashboard/v1/album-holds").status_code == 401


# ---- clear ---------------------------------------------------------------


def test_clear_album_hold(holds_api_env):
    import state_db

    client, _tmp = holds_api_env
    state_db.create_album_hold(10, reason="operator_cancel", ttl_seconds=3600)
    r = client.post(f"/dashboard/v1/album-holds/10/clear?apikey={_key()}")
    assert r.status_code == 200
    assert r.get_json() == {"album_id": 10, "cleared": True}
    # Hold is no longer active.
    assert state_db.get_album_hold(10) is None


def test_clear_album_hold_404_when_absent(holds_api_env):
    client, _tmp = holds_api_env
    r = client.post(f"/dashboard/v1/album-holds/999/clear?apikey={_key()}")
    assert r.status_code == 404
    assert r.get_json()["error"] == "no active hold"


# ---- manual create -------------------------------------------------------


def test_create_album_hold_validates_against_lidarr(holds_api_env):
    import state_db

    client, tmp_path = holds_api_env
    _make_lidarr_db(tmp_path, [(10, "Depeche Mode", "Violator")])
    r = client.post(f"/dashboard/v1/album-holds/10?apikey={_key()}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["album_id"] == 10
    assert body["artist"] == "Depeche Mode"
    assert body["album"] == "Violator"
    # Snapshot persisted so the dashboard never needs a live Lidarr call.
    hold = state_db.get_album_hold(10)
    assert hold is not None
    assert hold["details"]["album_title"] == "Violator"
    assert hold["actor"] == "user_dashboard"


def test_create_album_hold_rejects_unknown_album(holds_api_env):
    import state_db

    client, tmp_path = holds_api_env
    _make_lidarr_db(tmp_path, [(10, "Depeche Mode", "Violator")])
    r = client.post(f"/dashboard/v1/album-holds/777?apikey={_key()}")
    assert r.status_code == 404
    assert r.get_json()["error"] == "unknown Lidarr album"
    # No phantom hold was written.
    assert state_db.get_album_hold(777) is None


def test_create_album_hold_requires_apikey(holds_api_env):
    client, _tmp = holds_api_env
    assert client.post("/dashboard/v1/album-holds/10").status_code == 401
