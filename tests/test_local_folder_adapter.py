"""F3.4 tests for LocalFolderAdapter + /local/ingest endpoint.

Covers all 12 tests from F3.4 design v0.2 §7.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---- Fakes ---------------------------------------------------------------


class _FakeContext:
    """Minimal PipelineContext for adapter unit tests."""

    def __init__(self, *, jid: str, raw_dir: Path, output_dir: Path | None = None):
        self.jid = jid
        self.worker_job_id = None
        self.raw_dir = raw_dir
        self.output_dir = output_dir or raw_dir
        self.progress_calls: list[dict] = []
        self.cancel_checks = 0

    def check_cancelled(self):
        self.cancel_checks += 1

    def run_subprocess(self, argv, *, timeout, text=True):
        raise AssertionError("LocalFolderAdapter must not spawn subprocesses")

    def set_progress(self, *, stage, percent, message="", **extra):
        self.progress_calls.append(
            {"stage": stage, "percent": percent, "message": message, **extra}
        )

    def log(self, level, msg, *args, **fields):
        pass


def _seed_album(root: Path, artist: str, album: str, tracks: int = 2) -> Path:
    album_dir = root / artist / album
    album_dir.mkdir(parents=True, exist_ok=True)
    for i in range(tracks):
        (album_dir / f"{i+1:02d} track.flac").write_bytes(b"FAKE-FLAC" + bytes(2048))
    return album_dir


# ---- 1. is_enabled --------------------------------------------------------


def test_local_adapter_disabled_without_env(tmp_path, monkeypatch):
    from adapters.local_folder import LocalFolderAdapter
    monkeypatch.delenv("LOCAL_INGEST_PATH", raising=False)
    adapter = LocalFolderAdapter(ingest_root=str(tmp_path / "does-not-exist"))
    assert adapter.is_enabled() is False


def test_local_adapter_enabled_when_dir_exists(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    adapter = LocalFolderAdapter(ingest_root=str(tmp_path))
    assert adapter.is_enabled() is True


# ---- 2. search ------------------------------------------------------------


def test_local_search_returns_empty_for_empty_query(tmp_path):
    """F3.3: empty Lidarr indexer-test must not expose arbitrary local folders.

    (Renamed from test_local_search_returns_empty_until_newznab_phase after
    F3.3 wired search() to actually scan. Non-empty queries return hits
    — see test_local_search_filters_by_artist_album_query in
    test_newznab_routing.py.)
    """
    from adapters.local_folder import LocalFolderAdapter
    _seed_album(tmp_path, "Artist", "Album")
    adapter = LocalFolderAdapter(ingest_root=str(tmp_path))
    assert adapter.search(query="", artist="", album="") == []


# ---- 3. normalize_candidate_id --------------------------------------------


def test_local_normalize_candidate_blocks_absolute_and_parent_paths(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    _seed_album(tmp_path, "Artist", "Album")
    adapter = LocalFolderAdapter(ingest_root=str(tmp_path))

    with pytest.raises(RuntimeError, match="must be relative"):
        adapter.normalize_candidate_id("/etc/passwd")
    with pytest.raises(RuntimeError, match="path traversal blocked"):
        adapter.normalize_candidate_id("../../../etc/passwd")
    with pytest.raises(RuntimeError, match="not a directory"):
        adapter.normalize_candidate_id("Artist/DoesNotExist")
    # Valid path returns canonical POSIX form
    assert adapter.normalize_candidate_id("Artist/Album") == "Artist/Album"


def test_local_normalize_candidate_blocks_symlinked_source_dir(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    ingest = tmp_path / "ingest"
    real = _seed_album(ingest, "Artist", "RealAlbum")
    (ingest / "Artist" / "LinkedAlbum").symlink_to(real, target_is_directory=True)

    adapter = LocalFolderAdapter(ingest_root=str(ingest))
    with pytest.raises(RuntimeError, match="symlink blocked"):
        adapter.normalize_candidate_id("Artist/LinkedAlbum")


# ---- 4. download_raw - happy path ----------------------------------------


def test_local_download_raw_copies_files(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    ingest = tmp_path / "ingest"
    src = _seed_album(ingest, "Artist", "Album", tracks=3)
    adapter = LocalFolderAdapter(ingest_root=str(ingest))
    raw_dir = tmp_path / "raw"
    ctx = _FakeContext(jid="local-test", raw_dir=raw_dir)

    result = adapter.download_raw("Artist/Album", ctx)

    assert result.file_count == 3
    assert result.total_bytes > 0
    assert sorted(p.name for p in raw_dir.rglob("*.flac")) == [
        "01 track.flac", "02 track.flac", "03 track.flac",
    ]
    # Source files left untouched
    assert sorted(p.name for p in src.rglob("*.flac")) == [
        "01 track.flac", "02 track.flac", "03 track.flac",
    ]
    # Progress was reported for copying + copied stages
    stages = [c["stage"] for c in ctx.progress_calls]
    assert "copying" in stages
    assert "copied" in stages


# ---- 5. download_raw — path traversal ------------------------------------


def test_local_download_raw_blocks_path_traversal(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    adapter = LocalFolderAdapter(ingest_root=str(tmp_path))
    ctx = _FakeContext(jid="trav", raw_dir=tmp_path / "raw")
    with pytest.raises(RuntimeError, match="path traversal blocked"):
        adapter.download_raw("../../etc", ctx)


# ---- 6. download_raw — symlink escape -------------------------------------


def test_local_download_raw_blocks_nested_symlink_escape(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    ingest = tmp_path / "ingest"
    album = _seed_album(ingest, "Artist", "Album", tracks=1)
    # Insert a symlink inside the valid album dir
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"OUTSIDE")
    (album / "evil.flac").symlink_to(outside)

    adapter = LocalFolderAdapter(ingest_root=str(ingest))
    ctx = _FakeContext(jid="sym", raw_dir=tmp_path / "raw")
    with pytest.raises(RuntimeError, match="symlink blocked"):
        adapter.download_raw("Artist/Album", ctx)


# ---- 7. download_raw — empty dir -----------------------------------------


def test_local_download_raw_raises_on_empty_dir(tmp_path):
    from adapters.local_folder import LocalFolderAdapter
    ingest = tmp_path / "ingest"
    empty = ingest / "Artist" / "EmptyAlbum"
    empty.mkdir(parents=True)
    # Drop a non-audio file so there's *something* to iterate over
    (empty / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0")

    adapter = LocalFolderAdapter(ingest_root=str(ingest))
    ctx = _FakeContext(jid="empty", raw_dir=tmp_path / "raw")
    with pytest.raises(RuntimeError, match="no supported audio files"):
        adapter.download_raw("Artist/EmptyAlbum", ctx)


# ---- 8. end-to-end via pipeline -------------------------------------------


def test_local_executor_threads_source_type_local(tmp_path, monkeypatch):
    """Routing local_grab through pipeline.execute_source_grab must surface
    source_type='local' to import_to_lidarr (and from there into sidecar/DB)."""
    import server
    import pipeline
    from adapters.local_folder import LocalFolderAdapter

    ingest = tmp_path / "ingest"
    _seed_album(ingest, "Artist", "Album")
    adapter = LocalFolderAdapter(ingest_root=str(ingest))

    seen: dict = {}

    def _capturing_trigger(jid, output_dir, worker_job_id=None, *, source_type="tidal"):
        seen["jid"] = jid
        seen["source_type"] = source_type

    # Stub Lidarr-import + cancel/timing side effects so we exercise pipeline only
    monkeypatch.setattr(server, "_trigger_lidarr_import", _capturing_trigger)
    monkeypatch.setattr(server, "_raise_if_job_cancelled", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_record_job_timing", lambda *a, **kw: None)
    monkeypatch.setattr(server, "OUTPUT_BASE", tmp_path / "output")
    monkeypatch.setattr(server, "DOWNLOAD_BASE", tmp_path / "downloads")

    # normalize_audio probes ffprobe + flac binaries — fake all subprocess calls
    import subprocess
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="flac", stderr=""),
    )

    jid = "local-e2e"
    ctx = _FakeContext(
        jid=jid,
        raw_dir=server.DOWNLOAD_BASE / jid,
        output_dir=server.OUTPUT_BASE / jid,
    )
    job = {"id": None, "jid": jid, "payload_json": json.dumps({"source_id": "Artist/Album"})}

    pipeline.execute_source_grab(job, adapter, ctx)

    assert seen["jid"] == jid
    assert seen["source_type"] == "local"


# ---- 9-12. /local/ingest endpoint ----------------------------------------


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    """Spin up server.app with LOCAL_INGEST_PATH bound to tmp_path/ingest."""
    import server
    import adapters
    from adapters.local_folder import LocalFolderAdapter

    ingest = tmp_path / "ingest"
    ingest.mkdir()
    # Re-register local adapter pointing at tmp ingest dir
    adapters.reset_registry()
    from adapters.tidal import TidalAdapter
    adapters.register(TidalAdapter())
    adapters.register(LocalFolderAdapter(ingest_root=str(ingest)))

    server.app.config["TESTING"] = True
    return server.app.test_client(), ingest


def _ingest(client, path: str | None):
    body = {"path": path} if path is not None else {}
    return client.post(
        "/local/ingest",
        data=json.dumps(body),
        content_type="application/json",
        headers={"X-Api-Key": os.environ["TIDALHIRES_API_KEY"]},
    )


def test_local_ingest_endpoint_enqueues_job(local_client):
    client, ingest = local_client
    _seed_album(ingest, "Artist", "Album")

    r = _ingest(client, "Artist/Album")
    assert r.status_code == 200, r.data
    payload = r.get_json()
    assert payload["status"] is True
    assert len(payload["nzo_ids"]) == 1
    jid = payload["nzo_ids"][0]

    import state_db
    job = state_db.get_job(payload["job_id"])
    assert job is not None
    assert job["jid"] == jid
    assert job["type"] == "local_grab"
    assert job["source_type"] == "local"
    assert job["source_id"] == "Artist/Album"


def test_local_ingest_endpoint_rejects_invalid_path_before_job_creation(local_client):
    client, ingest = local_client

    import state_db
    before_count = 0
    try:
        with state_db._connect() as conn:
            before_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    except Exception:
        pass

    r = _ingest(client, "../../etc/passwd")
    assert r.status_code == 400
    assert "path traversal blocked" in r.get_json()["error"]

    after_count = 0
    try:
        with state_db._connect() as conn:
            after_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    except Exception:
        pass
    assert after_count == before_count, "rejected path must not create a job row"


def test_local_ingest_endpoint_rejects_non_string_path(local_client):
    client, ingest = local_client

    r = client.post(
        "/local/ingest",
        data=json.dumps({"path": 123}),
        content_type="application/json",
        headers={"X-Api-Key": os.environ["TIDALHIRES_API_KEY"]},
    )

    assert r.status_code == 400
    assert r.get_json()["error"] == "path must be a string"


def test_local_ingest_endpoint_dedupes_active_path(local_client):
    client, ingest = local_client
    _seed_album(ingest, "Artist", "Album")

    r1 = _ingest(client, "Artist/Album")
    assert r1.status_code == 200
    jid1 = r1.get_json()["nzo_ids"][0]

    r2 = _ingest(client, "Artist/Album")
    assert r2.status_code == 200
    jid2 = r2.get_json()["nzo_ids"][0]
    assert jid1 == jid2, "second ingest of same path must dedupe to existing job"


def test_local_ingest_endpoint_enqueue_dedupe_race_returns_existing_without_phantom_job(
    local_client, monkeypatch
):
    client, ingest = local_client
    _seed_album(ingest, "Artist", "Album")

    import server
    import state_db

    monkeypatch.setattr(server, "_jobs", {})
    existing_id = state_db.enqueue_job(
        jid="existinglocal",
        type="local_grab",
        dedupe_key="local:9232b530d16b",
        source_type="local",
        source_id="Artist/Album",
    )
    assert existing_id is not None
    monkeypatch.setattr(state_db, "find_active_job_by_dedupe", lambda key: None)

    r = _ingest(client, "Artist/Album")

    assert r.status_code == 200
    assert r.get_json()["nzo_ids"] == ["existinglocal"]
    total, jobs = state_db.list_jobs(type=["local_grab"])
    assert total == 1
    assert jobs[0]["jid"] == "existinglocal"
    assert server._jobs == {}


def test_local_ingest_endpoint_rejects_missing_adapter(local_client, monkeypatch):
    client, ingest = local_client
    import adapters
    # Drop the local adapter to simulate "not enabled" state
    adapters._adapters.pop("local", None)

    r = _ingest(client, "Artist/Album")
    assert r.status_code == 503
    assert "local adapter not enabled" in r.get_json()["error"]
