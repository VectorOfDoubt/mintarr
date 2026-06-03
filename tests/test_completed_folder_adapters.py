"""Phase 4 completed-folder ingest tests for SAB/qBittorrent source connectors."""

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
        raise AssertionError("Completed-folder adapters must not spawn subprocesses")

    def set_progress(self, *, stage, percent, message="", **extra):
        self.progress_calls.append(
            {"stage": stage, "percent": percent, "message": message, **extra}
        )

    def log(self, level, msg, *args, **fields):
        pass


def _seed_album(
    root: Path, artist: str = "Artist", album: str = "Album", tracks: int = 2
) -> Path:
    album_dir = root / artist / album
    album_dir.mkdir(parents=True, exist_ok=True)
    for i in range(tracks):
        (album_dir / f"{i+1:02d} track.flac").write_bytes(b"FAKE-FLAC" + bytes(2048))
    return album_dir


@pytest.mark.parametrize(
    ("cls_name", "env_prefix"),
    [
        ("SabUsenetCompletedAdapter", "SAB_USENET"),
        ("QBittorrentCompletedAdapter", "QBITTORRENT_TORRENT"),
    ],
)
def test_completed_folder_adapter_enabled_requires_toggle_and_root(
    tmp_path, cls_name, env_prefix
):
    import adapters.completed_folder as completed

    cls = getattr(completed, cls_name)

    assert cls(download_root=str(tmp_path), enabled=False).is_enabled() is False
    assert (
        cls(download_root=str(tmp_path / "missing"), enabled=True).is_enabled() is False
    )
    assert cls(download_root=str(tmp_path), enabled=True).is_enabled() is True


@pytest.mark.parametrize(
    "cls_name", ["SabUsenetCompletedAdapter", "QBittorrentCompletedAdapter"]
)
def test_completed_folder_normalize_blocks_absolute_traversal_and_symlink(
    tmp_path, cls_name
):
    import adapters.completed_folder as completed

    root = tmp_path / "completed"
    real = _seed_album(root, "Artist", "Real")
    (root / "Artist" / "Linked").symlink_to(real, target_is_directory=True)
    adapter = getattr(completed, cls_name)(
        download_root=str(root), enabled=True, settle_seconds=0
    )

    with pytest.raises(RuntimeError, match="must be relative"):
        adapter.normalize_candidate_id("/etc/passwd")
    with pytest.raises(RuntimeError, match="path traversal blocked"):
        adapter.normalize_candidate_id("../../../etc/passwd")
    with pytest.raises(RuntimeError, match="symlink blocked"):
        adapter.normalize_candidate_id("Artist/Linked")
    assert adapter.normalize_candidate_id("Artist/Real") == "Artist/Real"


@pytest.mark.parametrize(
    ("cls_name", "partial_name"),
    [
        ("SabUsenetCompletedAdapter", "03 track.flac.part"),
        ("QBittorrentCompletedAdapter", "03 track.flac.!qB"),
    ],
)
def test_completed_folder_rejects_partial_markers(tmp_path, cls_name, partial_name):
    import adapters.completed_folder as completed

    root = tmp_path / "completed"
    album = _seed_album(root)
    (album / partial_name).write_bytes(b"partial")
    adapter = getattr(completed, cls_name)(
        download_root=str(root), enabled=True, settle_seconds=0
    )

    with pytest.raises(RuntimeError, match="partial markers"):
        adapter.normalize_candidate_id("Artist/Album")


def test_completed_folder_rejects_unsettled_folder(tmp_path, monkeypatch):
    import adapters.completed_folder as completed

    root = tmp_path / "completed"
    album = _seed_album(root)
    adapter = completed.SabUsenetCompletedAdapter(
        download_root=str(root), enabled=True, settle_seconds=1
    )

    def fake_sleep(seconds):
        (album / "03 track.flac").write_bytes(b"new")

    monkeypatch.setattr(completed.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="not settled"):
        adapter.normalize_candidate_id("Artist/Album")


def test_completed_folder_download_raw_copies_without_modifying_source(tmp_path):
    from adapters.completed_folder import QBittorrentCompletedAdapter

    root = tmp_path / "completed"
    src = _seed_album(root, tracks=2)
    adapter = QBittorrentCompletedAdapter(
        download_root=str(root), enabled=True, settle_seconds=0
    )
    raw_dir = tmp_path / "raw"
    ctx = _FakeContext(jid="qbit", raw_dir=raw_dir)

    result = adapter.download_raw("Artist/Album", ctx)

    assert result.file_count == 2
    assert result.total_bytes > 0
    assert sorted(p.name for p in raw_dir.rglob("*.flac")) == [
        "01 track.flac",
        "02 track.flac",
    ]
    assert sorted(p.name for p in src.rglob("*.flac")) == [
        "01 track.flac",
        "02 track.flac",
    ]
    assert [item["stage"] for item in ctx.progress_calls] == ["copying", "copied"]


def test_completed_folder_executor_threads_source_type(tmp_path, monkeypatch):
    import pipeline
    import server
    from adapters.completed_folder import SabUsenetCompletedAdapter

    root = tmp_path / "completed"
    _seed_album(root)
    adapter = SabUsenetCompletedAdapter(
        download_root=str(root), enabled=True, settle_seconds=0
    )
    seen: dict = {}

    def _capturing_trigger(
        jid,
        output_dir,
        worker_job_id=None,
        *,
        source_type="tidal",
        target_album_id=None,
    ):
        seen["jid"] = jid
        seen["source_type"] = source_type
        seen["target_album_id"] = target_album_id

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

    jid = "sab-e2e"
    ctx = _FakeContext(
        jid=jid,
        raw_dir=server.DOWNLOAD_BASE / jid,
        output_dir=server.OUTPUT_BASE / jid,
    )
    job = {
        "id": None,
        "jid": jid,
        "payload_json": json.dumps({"source_id": "Artist/Album"}),
    }

    pipeline.execute_source_grab(job, adapter, ctx)

    assert seen == {
        "jid": jid,
        "source_type": "sab_usenet",
        "target_album_id": None,
    }


@pytest.fixture
def completed_folder_client(tmp_path, monkeypatch):
    import adapters
    import connectors
    import server
    import state_db
    from adapters.completed_folder import (
        QBittorrentCompletedAdapter,
        SabUsenetCompletedAdapter,
    )
    from adapters.local_folder import LocalFolderAdapter
    from adapters.soulseek import SoulseekCompletedAdapter
    from adapters.tidal import TidalAdapter

    sab_root = tmp_path / "sab"
    qbit_root = tmp_path / "qbit"
    sab_root.mkdir()
    qbit_root.mkdir()
    adapters.reset_registry()
    adapters.register(TidalAdapter())
    adapters.register(LocalFolderAdapter(ingest_root=str(tmp_path / "local")))
    adapters.register(
        SoulseekCompletedAdapter(download_root=str(tmp_path / "soulseek"))
    )
    adapters.register(
        SabUsenetCompletedAdapter(
            download_root=str(sab_root), enabled=True, settle_seconds=0
        )
    )
    adapters.register(
        QBittorrentCompletedAdapter(
            download_root=str(qbit_root), enabled=True, settle_seconds=0
        )
    )
    connectors.reset_registry()
    connectors.register_builtin_connectors(warn_missing_required=False)
    state_db._initialized = False
    if state_db._db_path.exists():
        state_db._db_path.unlink()
    state_db.init()
    state_db.set_connector_config(
        "sab_usenet", enabled=True, mode="import", actor="test"
    )
    state_db.set_connector_config(
        "qbittorrent_torrent", enabled=True, mode="import", actor="test"
    )
    server.app.config["TESTING"] = True
    return server.app.test_client(), sab_root, qbit_root


def _ingest(client, url: str, path: str | None):
    body = {"path": path} if path is not None else {}
    return client.post(
        url,
        data=json.dumps(body),
        content_type="application/json",
        headers={"X-Api-Key": os.environ["TIDALHIRES_API_KEY"]},
    )


def test_sab_ingest_endpoint_enqueues_job(completed_folder_client):
    client, sab_root, _ = completed_folder_client
    _seed_album(sab_root)

    response = _ingest(client, "/sab/ingest", "Artist/Album")

    assert response.status_code == 200, response.data
    payload = response.get_json()
    jid = payload["nzo_ids"][0]
    import state_db

    job = state_db.get_job(payload["job_id"])
    assert job["jid"] == jid
    assert job["type"] == "sab_usenet_grab"
    assert job["source_type"] == "sab_usenet"
    assert job["source_id"] == "Artist/Album"


def test_qbit_ingest_endpoint_rejects_partial_as_conflict(completed_folder_client):
    client, _, qbit_root = completed_folder_client
    album = _seed_album(qbit_root)
    (album / "03 track.flac.!qB").write_bytes(b"partial")

    response = _ingest(client, "/qbit/ingest", "Artist/Album")

    assert response.status_code == 409
    assert "partial markers" in response.get_json()["error"]


def test_completed_folder_ingest_endpoint_respects_connector_mode(
    completed_folder_client,
):
    client, sab_root, _ = completed_folder_client
    _seed_album(sab_root)
    import state_db

    state_db.set_connector_config(
        "sab_usenet", enabled=True, mode="dry_run", actor="test"
    )

    response = _ingest(client, "/sab/ingest", "Artist/Album")

    assert response.status_code == 503
    assert "not in import mode" in response.get_json()["error"]


def test_lidarr_complete_path_mapping_can_be_parameterized(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "OUTPUT_BASE", tmp_path / "output")
    monkeypatch.setattr(server, "LIDARR_COMPLETE_ROOT", "/downloads/mintarr")
    mapped = server._lidarr_path_to_output_path("/downloads/mintarr/abc/01.flac")

    assert server._lidarr_complete_path("abc") == "/downloads/mintarr/abc"
    assert mapped == tmp_path / "output" / "abc" / "01.flac"
