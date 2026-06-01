"""F3.5a Soulseek completed-folder ingest tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


class _FakeContext:
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
        raise AssertionError("SoulseekCompletedAdapter must not spawn subprocesses in F3.5a")

    def set_progress(self, *, stage, percent, message="", **extra):
        self.progress_calls.append(
            {"stage": stage, "percent": percent, "message": message, **extra}
        )

    def log(self, level, msg, *args, **fields):
        pass


def _seed_album(root: Path, artist: str = "Artist", album: str = "Album", tracks: int = 2) -> Path:
    album_dir = root / artist / album
    album_dir.mkdir(parents=True, exist_ok=True)
    for i in range(tracks):
        (album_dir / f"{i+1:02d} track.flac").write_bytes(b"FAKE-FLAC" + bytes(2048))
    return album_dir


def test_soulseek_adapter_enabled_requires_toggle_and_root(tmp_path):
    from adapters.soulseek import SoulseekCompletedAdapter

    assert SoulseekCompletedAdapter(download_root=str(tmp_path), enabled=False).is_enabled() is False
    assert SoulseekCompletedAdapter(download_root=str(tmp_path / "missing"), enabled=True).is_enabled() is False
    assert SoulseekCompletedAdapter(download_root=str(tmp_path), enabled=True).is_enabled() is True


def test_soulseek_normalize_blocks_absolute_traversal_and_symlink(tmp_path):
    from adapters.soulseek import SoulseekCompletedAdapter

    root = tmp_path / "soulseek"
    real = _seed_album(root, "Artist", "Real")
    (root / "Artist" / "Linked").symlink_to(real, target_is_directory=True)
    adapter = SoulseekCompletedAdapter(download_root=str(root), enabled=True, settle_seconds=0)

    with pytest.raises(RuntimeError, match="must be relative"):
        adapter.normalize_candidate_id("/etc/passwd")
    with pytest.raises(RuntimeError, match="path traversal blocked"):
        adapter.normalize_candidate_id("../../../etc/passwd")
    with pytest.raises(RuntimeError, match="symlink blocked"):
        adapter.normalize_candidate_id("Artist/Linked")
    assert adapter.normalize_candidate_id("Artist/Real") == "Artist/Real"


def test_soulseek_rejects_partial_markers(tmp_path):
    from adapters.soulseek import SoulseekCompletedAdapter

    root = tmp_path / "soulseek"
    album = _seed_album(root)
    (album / "03 track.flac.part").write_bytes(b"partial")
    adapter = SoulseekCompletedAdapter(download_root=str(root), enabled=True, settle_seconds=0)

    with pytest.raises(RuntimeError, match="partial download markers"):
        adapter.normalize_candidate_id("Artist/Album")


def test_soulseek_rejects_max_files_and_bytes(tmp_path):
    from adapters.soulseek import SoulseekCompletedAdapter

    root = tmp_path / "soulseek"
    _seed_album(root, tracks=3)
    with pytest.raises(RuntimeError, match="too many files"):
        SoulseekCompletedAdapter(
            download_root=str(root),
            enabled=True,
            max_files=2,
            settle_seconds=0,
        ).normalize_candidate_id("Artist/Album")
    with pytest.raises(RuntimeError, match="exceeds max bytes"):
        SoulseekCompletedAdapter(
            download_root=str(root),
            enabled=True,
            max_bytes=1,
            settle_seconds=0,
        ).normalize_candidate_id("Artist/Album")


def test_soulseek_rejects_unsettled_folder(tmp_path, monkeypatch):
    import adapters.soulseek as soulseek_mod
    from adapters.soulseek import SoulseekCompletedAdapter

    root = tmp_path / "soulseek"
    album = _seed_album(root)
    adapter = SoulseekCompletedAdapter(download_root=str(root), enabled=True, settle_seconds=1)

    def fake_sleep(seconds):
        (album / "03 track.flac").write_bytes(b"new")

    monkeypatch.setattr(soulseek_mod.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="not settled"):
        adapter.normalize_candidate_id("Artist/Album")


def test_soulseek_download_raw_copies_without_modifying_source(tmp_path):
    from adapters.soulseek import SoulseekCompletedAdapter

    root = tmp_path / "soulseek"
    src = _seed_album(root, tracks=2)
    adapter = SoulseekCompletedAdapter(download_root=str(root), enabled=True, settle_seconds=0)
    raw_dir = tmp_path / "raw"
    ctx = _FakeContext(jid="slsk", raw_dir=raw_dir)

    result = adapter.download_raw("Artist/Album", ctx)

    assert result.file_count == 2
    assert result.total_bytes > 0
    assert sorted(p.name for p in raw_dir.rglob("*.flac")) == ["01 track.flac", "02 track.flac"]
    assert sorted(p.name for p in src.rglob("*.flac")) == ["01 track.flac", "02 track.flac"]
    assert [item["stage"] for item in ctx.progress_calls] == ["copying", "copied"]


def test_soulseek_executor_threads_source_type(tmp_path, monkeypatch):
    import pipeline
    import server
    from adapters.soulseek import SoulseekCompletedAdapter

    root = tmp_path / "soulseek"
    _seed_album(root)
    adapter = SoulseekCompletedAdapter(download_root=str(root), enabled=True, settle_seconds=0)
    seen: dict = {}

    def _capturing_trigger(jid, output_dir, worker_job_id=None, *, source_type="tidal"):
        seen["jid"] = jid
        seen["source_type"] = source_type

    monkeypatch.setattr(server, "_trigger_lidarr_import", _capturing_trigger)
    monkeypatch.setattr(server, "_raise_if_job_cancelled", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_record_job_timing", lambda *a, **kw: None)
    monkeypatch.setattr(server, "OUTPUT_BASE", tmp_path / "output")
    monkeypatch.setattr(server, "DOWNLOAD_BASE", tmp_path / "downloads")
    import subprocess
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="flac", stderr=""),
    )

    jid = "soul-e2e"
    ctx = _FakeContext(
        jid=jid,
        raw_dir=server.DOWNLOAD_BASE / jid,
        output_dir=server.OUTPUT_BASE / jid,
    )
    job = {"id": None, "jid": jid, "payload_json": json.dumps({"source_id": "Artist/Album"})}

    pipeline.execute_source_grab(job, adapter, ctx)

    assert seen == {"jid": jid, "source_type": "soulseek"}


@pytest.fixture
def soulseek_client(tmp_path, monkeypatch):
    import adapters
    import connectors
    import server
    import state_db
    from adapters.local_folder import LocalFolderAdapter
    from adapters.soulseek import SoulseekCompletedAdapter
    from adapters.tidal import TidalAdapter

    root = tmp_path / "soulseek"
    root.mkdir()
    adapters.reset_registry()
    adapters.register(TidalAdapter())
    adapters.register(LocalFolderAdapter(ingest_root=str(tmp_path / "local")))
    adapters.register(SoulseekCompletedAdapter(download_root=str(root), enabled=True, settle_seconds=0))
    connectors.reset_registry()
    connectors.register_builtin_connectors(warn_missing_required=False)
    state_db._initialized = False
    if state_db._db_path.exists():
        state_db._db_path.unlink()
    state_db.init()
    state_db.set_connector_config("soulseek", enabled=True, mode="import", actor="test")
    server.app.config["TESTING"] = True
    return server.app.test_client(), root


def _ingest(client, path: str | None):
    body = {"path": path} if path is not None else {}
    return client.post(
        "/soulseek/ingest",
        data=json.dumps(body),
        content_type="application/json",
        headers={"X-Api-Key": os.environ["TIDALHIRES_API_KEY"]},
    )


def test_soulseek_ingest_endpoint_enqueues_job(soulseek_client):
    client, root = soulseek_client
    _seed_album(root)

    response = _ingest(client, "Artist/Album")

    assert response.status_code == 200, response.data
    payload = response.get_json()
    assert payload["status"] is True
    jid = payload["nzo_ids"][0]
    import state_db
    job = state_db.get_job(payload["job_id"])
    assert job["jid"] == jid
    assert job["type"] == "soulseek_grab"
    assert job["source_type"] == "soulseek"
    assert job["source_id"] == "Artist/Album"


def test_soulseek_ingest_endpoint_rejects_partial_as_conflict(soulseek_client):
    client, root = soulseek_client
    album = _seed_album(root)
    (album / "03 track.flac.part").write_bytes(b"partial")

    response = _ingest(client, "Artist/Album")

    assert response.status_code == 409
    assert "partial download markers" in response.get_json()["error"]


def test_soulseek_ingest_endpoint_dedupes_active_path(soulseek_client):
    client, root = soulseek_client
    _seed_album(root)

    first = _ingest(client, "Artist/Album")
    second = _ingest(client, "Artist/Album")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["nzo_ids"] == second.get_json()["nzo_ids"]


def test_soulseek_ingest_endpoint_respects_connector_mode(soulseek_client):
    client, root = soulseek_client
    _seed_album(root)
    import state_db
    state_db.set_connector_config("soulseek", enabled=True, mode="dry_run", actor="test")

    response = _ingest(client, "Artist/Album")

    assert response.status_code == 503
    assert "not in import mode" in response.get_json()["error"]
