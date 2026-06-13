"""#160 slice 3b — Lidarr-catalogue loader + Newznab album-hold suppression.

Covers the impure glue between the pure resolver (``lidarr_album_resolver``,
tested separately) and the live Newznab feed: reading Lidarr's album catalogue,
caching it, resolving a search to a held albumId, and suppressing the feed only
when exactly one trusted album has an active hold.
"""

from __future__ import annotations

import sqlite3

import pytest


# ---- Fixtures ------------------------------------------------------------


@pytest.fixture
def suppression_env(tmp_path, monkeypatch):
    """Server test client + fresh state_db + a fake read-only Lidarr DB."""
    import server
    import state_db
    import adapters
    from adapters.tidal import TidalAdapter
    from adapters.local_folder import LocalFolderAdapter

    ingest = tmp_path / "ingest"
    ingest.mkdir()
    adapters.reset_registry()
    adapters.register(TidalAdapter())
    adapters.register(LocalFolderAdapter(ingest_root=str(ingest)))
    state_db._initialized = False
    if state_db._db_path.exists():
        state_db._db_path.unlink()
    state_db.init()

    # Point the loader at a fake Lidarr config/db under tmp_path.
    config_xml = tmp_path / "config.xml"
    monkeypatch.setenv("LIDARR_CONFIG_XML", str(config_xml))
    # Reset the module-level catalogue cache between tests.
    server._ALBUM_CATALOGUE_CACHE = None

    server.app.config["TESTING"] = True
    return server.app.test_client(), tmp_path


def _make_lidarr_db(tmp_path, albums, db_path=None):
    """Create a minimal Lidarr-shaped SQLite DB.

    Defaults to ``<tmp>/lidarr.db`` (the LIDARR_CONFIG_XML sibling); pass
    ``db_path`` to write an explicitly-located DB. ``albums`` is a list of
    ``(album_id, artist, title)``.
    """
    db_path = db_path if db_path is not None else tmp_path / "lidarr.db"
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
    return db_path


def _api_key():
    import os

    return os.environ["TIDALHIRES_API_KEY"]


# ---- Catalogue loader ----------------------------------------------------


def test_read_lidarr_album_rows(suppression_env):
    import server

    _client, tmp_path = suppression_env
    _make_lidarr_db(
        tmp_path,
        [(10, "Depeche Mode", "Violator"), (11, "Björk", "Homogenic")],
    )
    rows = server._read_lidarr_album_rows()
    assert (10, "Depeche Mode", "Violator") in rows
    assert (11, "Björk", "Homogenic") in rows


def test_read_lidarr_album_rows_honors_explicit_db_path(suppression_env, monkeypatch):
    # Regression: installs that set MINTARR_LIDARR_DB_PATH explicitly must be
    # read from there, not the LIDARR_CONFIG_XML sibling — otherwise suppression
    # is silently broken on those installs (matches F5.4/inventory/dashboard).
    import server

    _client, tmp_path = suppression_env
    # Sibling DB holds a decoy album; the explicit path is the real catalogue.
    _make_lidarr_db(tmp_path, [(99, "Decoy", "Sibling")])
    explicit = tmp_path / "explicit" / "lidarr.db"
    explicit.parent.mkdir()
    _make_lidarr_db(tmp_path, [(10, "Depeche Mode", "Violator")], db_path=explicit)
    monkeypatch.setenv("MINTARR_LIDARR_DB_PATH", str(explicit))

    rows = server._read_lidarr_album_rows()
    assert (10, "Depeche Mode", "Violator") in rows
    assert (99, "Decoy", "Sibling") not in rows


def test_read_lidarr_album_rows_missing_db_returns_empty(suppression_env):
    import server

    # No lidarr.db was created → loader must fail open with [].
    assert server._read_lidarr_album_rows() == []


def test_get_album_catalogue_is_cached(suppression_env, monkeypatch):
    import server

    _client, tmp_path = suppression_env
    _make_lidarr_db(tmp_path, [(10, "Depeche Mode", "Violator")])

    calls = {"n": 0}
    real = server._read_lidarr_album_rows

    def _counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(server, "_read_lidarr_album_rows", _counting)

    first = server._get_album_catalogue()
    second = server._get_album_catalogue()
    assert [e.album_id for e in first] == [10]
    assert second is first  # served from cache, no second DB read
    assert calls["n"] == 1
    # force=True bypasses the cache.
    server._get_album_catalogue(force=True)
    assert calls["n"] == 2


# ---- Suppression decision -----------------------------------------------


def test_suppressed_album_id_returns_held_album(suppression_env):
    import server
    import state_db

    _client, tmp_path = suppression_env
    _make_lidarr_db(tmp_path, [(10, "Depeche Mode", "Violator")])
    state_db.create_album_hold(10, reason="operator_cancel", ttl_seconds=3600)

    assert (
        server._album_hold_suppressed_album_id(artist="Depeche Mode", album="Violator")
        == 10
    )


def test_suppressed_album_id_none_when_not_held(suppression_env):
    import server

    _client, tmp_path = suppression_env
    _make_lidarr_db(tmp_path, [(10, "Depeche Mode", "Violator")])
    # No hold created.
    assert (
        server._album_hold_suppressed_album_id(artist="Depeche Mode", album="Violator")
        is None
    )


def test_suppressed_album_id_abstains_on_ambiguous(suppression_env):
    import server
    import state_db

    _client, tmp_path = suppression_env
    # Two albums normalize identically → resolver abstains even though both held.
    _make_lidarr_db(
        tmp_path,
        [(20, "Various", "Now 10"), (21, "Various", "Now 10")],
    )
    state_db.create_album_hold(20, reason="operator_cancel", ttl_seconds=3600)
    state_db.create_album_hold(21, reason="operator_cancel", ttl_seconds=3600)
    assert (
        server._album_hold_suppressed_album_id(artist="Various", album="Now 10") is None
    )


def test_suppressed_album_id_q_only_off_by_default(suppression_env):
    import server
    import state_db

    _client, tmp_path = suppression_env
    _make_lidarr_db(tmp_path, [(10, "Depeche Mode", "Violator")])
    state_db.create_album_hold(10, reason="operator_cancel", ttl_seconds=3600)
    # q-only resolution is gated OFF by default → no suppression.
    assert server._album_hold_suppressed_album_id(q="Depeche Mode Violator") is None


def test_suppressed_album_id_q_only_when_enabled(suppression_env, monkeypatch):
    import server
    import state_db

    _client, tmp_path = suppression_env
    _make_lidarr_db(tmp_path, [(10, "Depeche Mode", "Violator")])
    state_db.create_album_hold(10, reason="operator_cancel", ttl_seconds=3600)
    monkeypatch.setenv("MINTARR_ALBUM_HOLD_Q_RESOLVE", "true")
    assert server._album_hold_suppressed_album_id(q="Depeche Mode Violator") == 10


def test_suppressed_album_id_empty_catalogue(suppression_env):
    import server
    import state_db

    _client, _tmp_path = suppression_env
    # No lidarr.db → empty catalogue → never suppress, even with a hold present.
    state_db.create_album_hold(10, reason="operator_cancel", ttl_seconds=3600)
    assert (
        server._album_hold_suppressed_album_id(artist="Depeche Mode", album="Violator")
        is None
    )


# ---- Newznab integration -------------------------------------------------


def _stub_tidal(monkeypatch, candidate):
    import adapters
    from adapters.base import ReleaseCandidate

    rc = ReleaseCandidate(
        source_type="tidal",
        source_id=candidate,
        title=f"{candidate} [TIDAL]",
        artist="Depeche Mode",
        album="Violator",
        year=1990,
        quality_tag="FLAC 24bit",
        size_bytes=1,
        download_url=f"tidal:{candidate}",
        priority=50,
    )
    monkeypatch.setattr(adapters.get_adapter("tidal"), "is_enabled", lambda: True)
    monkeypatch.setattr(adapters.get_adapter("tidal"), "search", lambda **kw: [rc])


def test_newznab_suppresses_held_album_search(suppression_env, monkeypatch):
    import state_db

    client, tmp_path = suppression_env
    _make_lidarr_db(tmp_path, [(10, "Depeche Mode", "Violator")])
    state_db.create_album_hold(10, reason="operator_cancel", ttl_seconds=3600)
    _stub_tidal(monkeypatch, "GRAB-1")

    r = client.get(
        f"/api?t=search&artist=Depeche Mode&album=Violator&apikey={_api_key()}"
    )
    assert r.status_code == 200
    body = r.data.decode()
    assert "GRAB-1" not in body  # candidate suppressed
    assert 'total="0"' in body


def test_newznab_returns_results_when_not_held(suppression_env, monkeypatch):
    client, tmp_path = suppression_env
    _make_lidarr_db(tmp_path, [(10, "Depeche Mode", "Violator")])
    # No hold → normal results.
    _stub_tidal(monkeypatch, "GRAB-1")

    r = client.get(
        f"/api?t=search&artist=Depeche Mode&album=Violator&apikey={_api_key()}"
    )
    assert r.status_code == 200
    assert "GRAB-1" in r.data.decode()


def test_newznab_unrelated_search_not_suppressed(suppression_env, monkeypatch):
    import state_db

    client, tmp_path = suppression_env
    _make_lidarr_db(
        tmp_path,
        [(10, "Depeche Mode", "Violator"), (11, "Björk", "Homogenic")],
    )
    # Hold only on album 10; searching album 11 must still return results.
    state_db.create_album_hold(10, reason="operator_cancel", ttl_seconds=3600)
    _stub_tidal(monkeypatch, "GRAB-1")

    r = client.get(f"/api?t=search&artist=Björk&album=Homogenic&apikey={_api_key()}")
    assert r.status_code == 200
    assert "GRAB-1" in r.data.decode()


def test_newznab_empty_indexer_test_not_suppressed(suppression_env, monkeypatch):
    import state_db

    client, tmp_path = suppression_env
    # Even if the injected test album were somehow held, the empty indexer-test
    # query must never be suppressed (health check must pass).
    _make_lidarr_db(tmp_path, [(10, "Daft Punk", "Random Access Memories")])
    state_db.create_album_hold(10, reason="operator_cancel", ttl_seconds=3600)
    _stub_tidal(monkeypatch, "GRAB-1")

    r = client.get(f"/api?t=search&apikey={_api_key()}")
    assert r.status_code == 200
    assert "GRAB-1" in r.data.decode()
