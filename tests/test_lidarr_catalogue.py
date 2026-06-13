"""Read-only Lidarr album catalogue helper tests."""

from __future__ import annotations

import sqlite3

import lidarr_catalogue


class _Resp:
    def __init__(self, payload, *, fail: bool = False):
        self._payload = payload
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError("boom")

    def json(self):
        return self._payload


def _make_lidarr_db(path, albums):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE ArtistMetadata (Id INTEGER PRIMARY KEY, Name TEXT)")
        conn.execute(
            "CREATE TABLE Albums "
            "(Id INTEGER PRIMARY KEY, Title TEXT, ArtistMetadataId INTEGER)"
        )
        artists: dict[str, int] = {}
        for album_id, artist, title in albums:
            if artist not in artists:
                artists[artist] = len(artists) + 1
                conn.execute(
                    "INSERT INTO ArtistMetadata (Id, Name) VALUES (?, ?)",
                    (artists[artist], artist),
                )
            conn.execute(
                "INSERT INTO Albums (Id, Title, ArtistMetadataId) VALUES (?, ?, ?)",
                (album_id, title, artists[artist]),
            )


def test_lidarr_db_path_prefers_explicit_env(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit.db"
    config = tmp_path / "config.xml"
    monkeypatch.setenv("MINTARR_LIDARR_DB_PATH", str(explicit))
    monkeypatch.setenv("LIDARR_CONFIG_XML", str(config))

    assert lidarr_catalogue.lidarr_db_path() == explicit


def test_read_album_rows_uses_sqlite_first(monkeypatch, tmp_path):
    db_path = tmp_path / "lidarr.db"
    _make_lidarr_db(db_path, [(10, "Depeche Mode", "Violator")])
    monkeypatch.setenv("MINTARR_LIDARR_DB_PATH", str(db_path))
    monkeypatch.setattr(
        lidarr_catalogue,
        "read_album_rows_api",
        lambda: (_ for _ in ()).throw(AssertionError("api call")),
    )

    assert lidarr_catalogue.read_album_rows() == [(10, "Depeche Mode", "Violator")]


def test_read_album_rows_falls_back_to_api(monkeypatch, tmp_path):
    monkeypatch.setenv("MINTARR_LIDARR_DB_PATH", str(tmp_path / "missing.db"))
    monkeypatch.setattr(
        lidarr_catalogue,
        "read_album_rows_api",
        lambda: [(20, "Björk", "Homogenic")],
    )

    assert lidarr_catalogue.read_album_rows() == [(20, "Björk", "Homogenic")]


def test_read_album_rows_api_global_album(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        assert url == "http://lidarr/api/v1/album"
        return _Resp(
            [
                {
                    "id": 10,
                    "title": "Violator",
                    "artist": {"artistName": "Depeche Mode"},
                }
            ]
        )

    rows = lidarr_catalogue.read_album_rows_api(
        api="http://lidarr/api/v1", key="k", get=fake_get
    )

    assert rows == [(10, "Depeche Mode", "Violator")]
    assert calls == [
        ("http://lidarr/api/v1/album", {"X-Api-Key": "k"}, 120),
    ]


def test_read_album_rows_api_artist_fallback(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if url == "http://lidarr/api/v1/album":
            return _Resp([], fail=True)
        if url == "http://lidarr/api/v1/artist":
            return _Resp([{"id": 1, "artistName": "A-ha"}])
        if url == "http://lidarr/api/v1/album?artistId=1":
            return _Resp([{"id": 30, "title": "Memorial Beach"}])
        raise AssertionError(url)

    rows = lidarr_catalogue.read_album_rows_api(
        api="http://lidarr/api/v1", key="k", get=fake_get
    )

    assert rows == [(30, "A-ha", "Memorial Beach")]
    assert calls == [
        "http://lidarr/api/v1/album",
        "http://lidarr/api/v1/artist",
        "http://lidarr/api/v1/album?artistId=1",
    ]


def test_label_map_uses_targeted_sqlite(monkeypatch, tmp_path):
    db_path = tmp_path / "lidarr.db"
    _make_lidarr_db(
        db_path,
        [
            (10, "Depeche Mode", "Violator"),
            (20, "Björk", "Homogenic"),
        ],
    )
    monkeypatch.setenv("MINTARR_LIDARR_DB_PATH", str(db_path))
    monkeypatch.setenv("LIDARR_WEB_URL", "http://browser-lidarr")
    monkeypatch.setattr(
        lidarr_catalogue,
        "read_album_labels_api",
        lambda album_ids: (_ for _ in ()).throw(AssertionError("api call")),
    )

    assert lidarr_catalogue.label_map([20]) == {
        20: {
            "artist": "Björk",
            "album": "Homogenic",
            "lidarr_url": "http://browser-lidarr/album/20",
        }
    }


def test_label_map_does_not_derive_web_url_from_api(monkeypatch, tmp_path):
    db_path = tmp_path / "lidarr.db"
    _make_lidarr_db(db_path, [(20, "Björk", "Homogenic")])
    monkeypatch.setenv("MINTARR_LIDARR_DB_PATH", str(db_path))
    monkeypatch.delenv("LIDARR_WEB_URL", raising=False)
    monkeypatch.setenv("LIDARR_API_URL", "http://host.docker.internal:8686/api/v1")

    assert lidarr_catalogue.label_map([20])[20]["lidarr_url"] == (
        "http://127.0.0.1:8686/album/20"
    )


def test_read_album_labels_api_uses_targeted_album_endpoint():
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        assert url == "http://lidarr/api/v1/album/20"
        return _Resp(
            {
                "id": 20,
                "title": "Homogenic",
                "artist": {"artistName": "Björk"},
            }
        )

    labels = lidarr_catalogue.read_album_labels_api(
        [20], api="http://lidarr/api/v1", key="k", get=fake_get
    )

    assert labels == {
        20: {
            "artist": "Björk",
            "album": "Homogenic",
            "lidarr_url": "http://127.0.0.1:8686/album/20",
        }
    }
    assert calls == [
        ("http://lidarr/api/v1/album/20", {"X-Api-Key": "k"}, 30),
    ]
