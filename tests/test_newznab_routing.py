"""F3.2 + F3.3 tests — addurl routing, newznab aggregation, encoded download.

Covers all 18 tests from F3.2/F3.3 design v0.2 §9.
"""

from __future__ import annotations

import io
import json
import os
import re
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest


# ---- Fixtures ------------------------------------------------------------


@pytest.fixture
def routing_env(tmp_path, monkeypatch):
    """Set up server + ingest dir + re-register both adapters pointing here."""
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
    server.app.config["TESTING"] = True
    return server.app.test_client(), ingest


def _seed_album(root: Path, artist: str, album: str, tracks: int = 2) -> Path:
    album_dir = root / artist / album
    album_dir.mkdir(parents=True, exist_ok=True)
    for i in range(tracks):
        (album_dir / f"{i+1:02d} track.flac").write_bytes(b"FAKE-FLAC" + bytes(2048))
    return album_dir


def _api_key():
    return os.environ["TIDALHIRES_API_KEY"]


# ---- _parse_source_name --------------------------------------------------


def test_parse_source_name_tidal_prefix(routing_env):
    import server
    assert server._parse_source_name("tidal:12345", {}) == ("tidal", "12345")


def test_parse_source_name_local_prefix(routing_env):
    import server
    # Note: rel-path with spaces + slash + parens must survive intact
    assert server._parse_source_name(
        "local:Nightwish/Nemo (CD Single)", {},
    ) == ("local", "Nightwish/Nemo (CD Single)")


def test_parse_source_name_unknown_prefix_returns_none(routing_env):
    import server
    assert server._parse_source_name("foo:bar", {}) is None


def test_parse_source_name_nzb_content_legacy_tidal(routing_env):
    """Old-style NZBs only embedded a numeric TIDAL ID inside <meta type="name">."""
    import server
    legacy_nzb = '<head><meta type="name">tidal:789012</meta></head>'
    class _F:
        def __init__(self, data): self._data = data
        def read(self): return self._data
    assert server._parse_source_name("", {"f": _F(legacy_nzb.encode())}) == ("tidal", "789012")


def test_parse_source_name_nzb_structured_local_with_spaces(routing_env):
    """F3.3 NZBs emit structured meta so rel-paths with spaces roundtrip cleanly."""
    import server
    rel = "Nightwish/Nemo (CD Single)"
    nzb = (
        '<head>'
        '<meta type="tidalhires_source">local</meta>'
        f'<meta type="tidalhires_source_id">{rel}</meta>'
        '</head>'
    )
    class _F:
        def __init__(self, data): self._data = data
        def read(self): return self._data
    assert server._parse_source_name("", {"f": _F(nzb.encode())}) == ("local", rel)


def test_parse_source_name_nzb_structured_local_unescapes_xml_entities(routing_env):
    """NZB meta is XML-escaped on write; addurl parsing must unescape it."""
    import server
    rel = "R&B/Bookends"
    nzb = (
        '<head>'
        '<meta type="tidalhires_source">local</meta>'
        '<meta type="tidalhires_source_id">R&amp;B/Bookends</meta>'
        '</head>'
    )
    class _F:
        def __init__(self, data): self._data = data
        def read(self): return self._data
    assert server._parse_source_name("", {"f": _F(nzb.encode())}) == ("local", rel)


# ---- addurl dispatcher ---------------------------------------------------


def test_addurl_routes_local_to_local_grab_executor(routing_env):
    import state_db
    client, ingest = routing_env
    _seed_album(ingest, "Artist", "Album")

    r = client.post(
        f"/sabnzbd/api?apikey={_api_key()}",
        data={"mode": "addurl", "name": "local:Artist/Album"},
    )
    assert r.status_code == 200, r.data
    payload = r.get_json()
    assert payload["status"] is True
    jid = payload["nzo_ids"][0]

    # state_db has a local_grab job for this jid
    with state_db._connect() as conn:
        row = conn.execute(
            "SELECT type, source_type, source_id FROM jobs WHERE jid=?", (jid,),
        ).fetchone()
    assert dict(row) == {
        "type": "local_grab", "source_type": "local", "source_id": "Artist/Album",
    }


def test_addurl_publishes_jobs_projection_before_enqueue(routing_env, monkeypatch):
    """Regression: worker may start immediately after state_db enqueue.

    The SAB-compatible _jobs projection must exist before enqueue_job returns,
    otherwise a fast worker update can be overwritten back to queued by addurl.
    """
    import server
    import state_db

    client, ingest = routing_env
    _seed_album(ingest, "Artist", "Album")
    monkeypatch.setattr(server, "_jobs", {})
    real_enqueue = state_db.enqueue_job

    def _enqueue_and_simulate_fast_worker(**kwargs):
        job_id = real_enqueue(**kwargs)
        jid = kwargs["jid"]
        with server._jobs_lock:
            assert jid in server._jobs
            server._jobs[jid].update(
                status="downloading", percent=25, stage="download_raw",
            )
        return job_id

    monkeypatch.setattr(state_db, "enqueue_job", _enqueue_and_simulate_fast_worker)

    r = client.post(
        f"/sabnzbd/api?apikey={_api_key()}",
        data={"mode": "addurl", "name": "local:Artist/Album"},
    )

    assert r.status_code == 200, r.data
    jid = r.get_json()["nzo_ids"][0]
    assert server._jobs[jid]["status"] == "downloading"
    assert server._jobs[jid]["percent"] == 25


def test_addurl_rejects_unknown_source(routing_env):
    client, _ = routing_env
    r = client.post(
        f"/sabnzbd/api?apikey={_api_key()}",
        data={"mode": "addurl", "name": "foo:bar"},
    )
    assert r.status_code == 400
    assert "no recognized source prefix" in r.get_json()["error"]


def test_addurl_rejects_disabled_source(routing_env, monkeypatch):
    import adapters
    client, _ = routing_env
    monkeypatch.setattr(
        adapters.get_adapter("local"), "is_enabled", lambda: False,
    )
    r = client.post(
        f"/sabnzbd/api?apikey={_api_key()}",
        data={"mode": "addurl", "name": "local:Artist/Album"},
    )
    assert r.status_code == 503
    assert "source not enabled" in r.get_json()["error"]


# ---- newznab aggregation -------------------------------------------------


def test_newznab_aggregates_from_enabled_adapters(routing_env, monkeypatch):
    """Both adapters return one ReleaseCandidate; XML must contain both guids."""
    import adapters
    from adapters.base import ReleaseCandidate

    tidal_rc = ReleaseCandidate(
        source_type="tidal", source_id="111", title="T-Artist - T-Album [TIDAL] [FLAC 24bit]",
        artist="T-Artist", album="T-Album", year=2024,
        quality_tag="FLAC 24bit", size_bytes=1, download_url="tidal:111", priority=50,
    )
    local_rc = ReleaseCandidate(
        source_type="local", source_id="L-Artist/L-Album",
        title="L-Artist - L-Album (FLAC) [Local]",
        artist="L-Artist", album="L-Album", year=None,
        quality_tag="FLAC", size_bytes=2, download_url="local:L-Artist/L-Album", priority=30,
    )
    # TIDAL adapter has no token in tests — force enabled + stubbed search
    monkeypatch.setattr(adapters.get_adapter("tidal"), "is_enabled", lambda: True)
    monkeypatch.setattr(adapters.get_adapter("tidal"), "search", lambda **kw: [tidal_rc])
    monkeypatch.setattr(adapters.get_adapter("local"), "search", lambda **kw: [local_rc])

    client, _ = routing_env
    r = client.get(
        f"/api?t=search&q=test&apikey={_api_key()}",
    )
    assert r.status_code == 200
    body = r.data.decode()
    assert "tidal:111" in body
    assert "local:L-Artist/L-Album" in body


def test_newznab_sorts_by_priority_descending(routing_env, monkeypatch):
    import adapters
    from adapters.base import ReleaseCandidate

    low = ReleaseCandidate(
        source_type="local", source_id="low", title="LOW-MARKER",
        artist="X", album="Y", year=None, quality_tag="FLAC",
        size_bytes=1, download_url="local:low", priority=30,
    )
    high = ReleaseCandidate(
        source_type="tidal", source_id="high", title="HIGH-MARKER",
        artist="X", album="Y", year=None, quality_tag="FLAC 24bit",
        size_bytes=1, download_url="tidal:high", priority=50,
    )
    monkeypatch.setattr(adapters.get_adapter("tidal"), "is_enabled", lambda: True)
    monkeypatch.setattr(adapters.get_adapter("tidal"), "search", lambda **kw: [high])
    monkeypatch.setattr(adapters.get_adapter("local"), "search", lambda **kw: [low])

    client, _ = routing_env
    body = client.get(f"/api?t=search&q=anything&apikey={_api_key()}").data.decode()
    assert body.index("HIGH-MARKER") < body.index("LOW-MARKER")


def test_newznab_failure_isolation(routing_env, monkeypatch):
    """One adapter raising must not stop the others from returning hits."""
    import adapters
    from adapters.base import ReleaseCandidate

    def _boom(**kw):
        raise RuntimeError("simulated adapter failure")

    survivor = ReleaseCandidate(
        source_type="local", source_id="ok", title="SURVIVOR",
        artist="X", album="Y", year=None, quality_tag="FLAC",
        size_bytes=1, download_url="local:ok", priority=30,
    )
    monkeypatch.setattr(adapters.get_adapter("tidal"), "search", _boom)
    monkeypatch.setattr(adapters.get_adapter("local"), "search", lambda **kw: [survivor])

    client, _ = routing_env
    body = client.get(f"/api?t=search&q=test&apikey={_api_key()}").data.decode()
    assert "SURVIVOR" in body


def test_newznab_empty_query_skips_local_adapter(routing_env):
    """Empty Lidarr indexer-test must not expose arbitrary local folders."""
    import adapters
    client, ingest = routing_env
    _seed_album(ingest, "ShouldNotAppear", "ShouldNotAppearAlbum")
    _seed_album(ingest, "Daft Punk", "Random Access Memories")
    # Default newznab handler injects a TIDAL test-query for empty input.
    # We assert LocalFolder is skipped entirely, even when a local folder
    # would match the injected TIDAL health-check query.
    body = client.get(f"/api?t=search&apikey={_api_key()}").data.decode()
    assert "ShouldNotAppear" not in body
    assert "Daft Punk - Random Access Memories (FLAC) [Local]" not in body


def test_release_pub_date_has_matching_weekday(routing_env):
    """Regression: Lidarr rejects RSS items when pubDate weekday is wrong."""
    import server

    pub_date = server._release_pub_date(2004)

    assert pub_date == "Thu, 01 Jan 2004 00:00:00 +0000"
    assert parsedate_to_datetime(pub_date).year == 2004


# ---- LocalFolderAdapter.search ------------------------------------------


def test_local_search_filters_by_artist_album_query(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    ingest = tmp_path / "ingest"
    _seed_album(ingest, "Nightwish", "Once")
    _seed_album(ingest, "Iron Maiden", "Powerslave")
    adapter = LocalFolderAdapter(ingest_root=str(ingest))

    hits = adapter.search(query="", artist="Nightwish")
    assert len(hits) == 1
    assert hits[0].artist == "Nightwish"

    hits = adapter.search(query="Powerslave")
    assert len(hits) == 1
    assert hits[0].album == "Powerslave"


def test_local_search_empty_query_returns_empty(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    ingest = tmp_path / "ingest"
    _seed_album(ingest, "Artist", "Album")
    adapter = LocalFolderAdapter(ingest_root=str(ingest))
    assert adapter.search(query="", artist="", album="") == []


def test_local_search_caps_at_100_candidates(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    ingest = tmp_path / "ingest"
    for i in range(110):
        _seed_album(ingest, "Artist", f"Album{i:03d}", tracks=1)
    adapter = LocalFolderAdapter(ingest_root=str(ingest))
    hits = adapter.search(query="", artist="Artist")
    assert len(hits) == 100


def test_local_search_skips_symlinked_dirs(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    ingest = tmp_path / "ingest"
    _seed_album(ingest, "Real", "RealAlbum")
    # Add a symlinked artist directory that points outside ingest
    outside = tmp_path / "outside_artist"
    _seed_album(outside, "Fake", "FakeAlbum")
    (ingest / "LinkedArtist").symlink_to(outside / "Fake", target_is_directory=True)

    adapter = LocalFolderAdapter(ingest_root=str(ingest))
    # Non-empty query is required (empty query returns [] by design — see
    # test_local_search_empty_query_returns_empty). "Album" matches both
    # real and (would-be) linked albums; only real should survive.
    hits = adapter.search(query="Album")
    artist_names = {h.artist for h in hits}
    assert "Real" in artist_names
    assert "LinkedArtist" not in artist_names


# ---- /download endpoint --------------------------------------------------


def test_download_nzb_endpoint_accepts_encoded_local_token(routing_env):
    import server
    client, ingest = routing_env
    _seed_album(ingest, "Nightwish", "Nemo (CD Single)")
    rel = "Nightwish/Nemo (CD Single)"
    encoded = server._b64url_encode(rel)

    r = client.get(f"/download/local/{encoded}.nzb?apikey={_api_key()}")
    assert r.status_code == 200
    assert r.mimetype == "application/x-nzb"
    body = r.data.decode()
    assert 'type="tidalhires_source">local</meta>' in body
    assert f'type="tidalhires_source_id">{rel}</meta>' in body


def test_download_nzb_rejects_bad_encoded_source_id(routing_env):
    client, _ = routing_env
    # "!!!" is not valid base64url
    r = client.get(f"/download/local/!!!.nzb?apikey={_api_key()}")
    assert r.status_code == 400
    assert "bad encoded source_id" in r.data.decode()


def test_b64url_decode_rejects_invalid_alphabet(routing_env):
    import server
    with pytest.raises(ValueError, match="invalid base64url token"):
        server._b64url_decode("!!!")


def test_download_nzb_canonicalizes_local_path_before_nzb(routing_env):
    """Path traversal in decoded source_id must be rejected, not echoed."""
    import server
    client, _ = routing_env
    bad = server._b64url_encode("../../etc/passwd")
    r = client.get(f"/download/local/{bad}.nzb?apikey={_api_key()}")
    assert r.status_code == 400
    assert "path traversal" in r.data.decode().lower() or "invalid" in r.data.decode().lower()
