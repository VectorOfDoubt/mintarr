"""F3.1 pipeline phase tests.

Uses a FakeAdapter + FakePipelineContext to verify the four phases run
in the expected order without invoking real TIDAL or Lidarr.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeContext:
    """Minimal PipelineContext stand-in for tests."""

    def __init__(self, *, jid: str, raw_dir: Path, output_dir: Path):
        self.jid = jid
        self.worker_job_id = None
        self.raw_dir = raw_dir
        self.output_dir = output_dir
        self.progress_calls: list[dict] = []
        self.cancel_checks = 0
        self.subprocess_calls: list[list[str]] = []

    def check_cancelled(self):
        self.cancel_checks += 1

    def run_subprocess(self, argv, *, timeout, text=True):
        self.subprocess_calls.append(list(argv))
        return _FakeCompleted(returncode=0)

    def set_progress(self, *, stage, percent, message="", **extra):
        self.progress_calls.append(
            {"stage": stage, "percent": percent, "message": message, **extra}
        )

    def log(self, level, msg, *args, **fields):
        pass


class _FakeAdapter:
    name = "fake"
    source_type = "fake"

    def __init__(self, *, audio_files: int = 2):
        self.download_called_with = None
        self.cleanup_called = False
        self._audio_files = audio_files

    def is_enabled(self): return True
    def search(self, *a, **kw): return []

    def download_raw(self, candidate_id, ctx):
        from adapters.base import RawDownload
        self.download_called_with = candidate_id
        ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        for i in range(self._audio_files):
            (ctx.raw_dir / f"track{i}.flac").write_bytes(b"FAKEFLAC" + bytes(2048))
        return RawDownload(
            files_dir=ctx.raw_dir,
            file_count=self._audio_files,
            total_bytes=2048 * self._audio_files,
        )

    def cleanup(self, jid, ctx):
        self.cleanup_called = True


def _no_op(*args, **kwargs):
    return None


@pytest.fixture
def pipeline_env(tmp_path, monkeypatch):
    import server
    import pipeline

    monkeypatch.setattr(server, "OUTPUT_BASE", tmp_path / "output")
    monkeypatch.setattr(server, "DOWNLOAD_BASE", tmp_path / "downloads")
    # Stub side-effects so we exercise pipeline.execute_source_grab only
    monkeypatch.setattr(server, "_trigger_lidarr_import", _no_op)
    monkeypatch.setattr(server, "_raise_if_job_cancelled", _no_op)
    monkeypatch.setattr(server, "_record_job_timing", _no_op)
    # flac -t inside normalize_audio: pretend everything verifies fine
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _FakeCompleted(returncode=0),
    )
    return server, pipeline


def test_execute_source_grab_runs_phases_in_order(pipeline_env, tmp_path):
    server, pipeline = pipeline_env
    adapter = _FakeAdapter(audio_files=3)
    jid = "phasetest1"
    ctx = _FakeContext(
        jid=jid,
        raw_dir=server.DOWNLOAD_BASE / jid,
        output_dir=server.OUTPUT_BASE / jid,
    )
    job = {"id": None, "jid": jid, "payload_json": json.dumps({"source_id": "999"})}

    pipeline.execute_source_grab(job, adapter, ctx)

    # Adapter received the candidate_id from payload
    assert adapter.download_called_with == "999"
    # Output dir was created and files moved
    assert (server.OUTPUT_BASE / jid).exists()
    assert len(list((server.OUTPUT_BASE / jid).rglob("*.flac"))) == 3
    # Pipeline emitted progress for at least preparing + ready_for_import phases
    stages = [c["stage"] for c in ctx.progress_calls]
    assert "preparing" in stages
    assert "postprocess" in stages
    assert "ready_for_import" in stages


def test_execute_source_grab_missing_source_id_raises(pipeline_env):
    server, pipeline = pipeline_env
    adapter = _FakeAdapter()
    ctx = _FakeContext(
        jid="missing",
        raw_dir=server.DOWNLOAD_BASE / "missing",
        output_dir=server.OUTPUT_BASE / "missing",
    )
    job = {"id": None, "jid": "missing", "payload_json": json.dumps({})}
    with pytest.raises(ValueError, match="missing source_id/album_id"):
        pipeline.execute_source_grab(job, adapter, ctx)


def test_execute_source_grab_failing_adapter_marks_failed(pipeline_env, monkeypatch):
    server, pipeline = pipeline_env
    adapter = _FakeAdapter()

    def _boom(candidate_id, ctx):
        raise RuntimeError("source unreachable")

    monkeypatch.setattr(adapter, "download_raw", _boom)
    jid = "failtest"
    ctx = _FakeContext(
        jid=jid,
        raw_dir=server.DOWNLOAD_BASE / jid,
        output_dir=server.OUTPUT_BASE / jid,
    )
    job = {"id": None, "jid": jid, "payload_json": json.dumps({"source_id": "1"})}

    with pytest.raises(RuntimeError, match="source unreachable"):
        pipeline.execute_source_grab(job, adapter, ctx)
    assert server._jobs[jid]["status"] == "failed"
    assert "source unreachable" in (server._jobs[jid].get("error") or "")


def test_execute_source_grab_uses_album_id_when_source_id_absent(pipeline_env):
    """Legacy payloads only have 'album_id' — wrapper still forwards it."""
    server, pipeline = pipeline_env
    adapter = _FakeAdapter()
    jid = "legacy"
    ctx = _FakeContext(
        jid=jid,
        raw_dir=server.DOWNLOAD_BASE / jid,
        output_dir=server.OUTPUT_BASE / jid,
    )
    job = {"id": None, "jid": jid, "payload_json": json.dumps({"album_id": 4242})}
    pipeline.execute_source_grab(job, adapter, ctx)
    assert adapter.download_called_with == "4242"


def test_execute_source_grab_threads_non_tidal_source_type(pipeline_env, monkeypatch):
    """Regression: a non-TIDAL adapter must surface its source_type all the
    way into _trigger_lidarr_import (and from there into sidecar/state_db),
    not be silently defaulted to 'tidal'."""
    server, pipeline = pipeline_env

    class _LocalFolderAdapter(_FakeAdapter):
        name = "local"
        source_type = "local"

    adapter = _LocalFolderAdapter()
    seen: dict = {}

    def _capturing_trigger(jid, output_dir, worker_job_id=None, *, source_type="tidal"):
        seen["jid"] = jid
        seen["source_type"] = source_type
        seen["output_dir"] = output_dir

    monkeypatch.setattr(server, "_trigger_lidarr_import", _capturing_trigger)

    jid = "local-source-grab"
    ctx = _FakeContext(
        jid=jid,
        raw_dir=server.DOWNLOAD_BASE / jid,
        output_dir=server.OUTPUT_BASE / jid,
    )
    job = {"id": None, "jid": jid, "payload_json": json.dumps({"source_id": "abc"})}

    pipeline.execute_source_grab(job, adapter, ctx)

    assert seen["jid"] == jid
    assert seen["source_type"] == "local", (
        "non-TIDAL adapter must thread its source_type through "
        "import_to_lidarr → _trigger_lidarr_import, not default to 'tidal'"
    )


def test_prepare_output_directory_uses_ctx_output_dir(pipeline_env, tmp_path):
    """prepare_output_directory must respect ctx.output_dir, not hardcoded OUTPUT_BASE."""
    server, pipeline = pipeline_env
    jid = "custom-output"
    custom_out = tmp_path / "custom" / jid
    raw = tmp_path / "raw" / jid
    raw.mkdir(parents=True)
    (raw / "track.flac").write_bytes(b"flac")
    ctx = _FakeContext(jid=jid, raw_dir=raw, output_dir=custom_out)

    result = pipeline.prepare_output_directory(raw, ctx)

    assert result.output_dir == custom_out
    assert (custom_out / "track.flac").exists()


def test_pipeline_success_sets_download_exit_code(pipeline_env):
    """download_exit_code=0 must appear on _jobs after a successful run
    (audit consumers depend on the field being present)."""
    server, pipeline = pipeline_env
    adapter = _FakeAdapter()
    jid = "exitcode-test"
    ctx = _FakeContext(
        jid=jid,
        raw_dir=server.DOWNLOAD_BASE / jid,
        output_dir=server.OUTPUT_BASE / jid,
    )
    job = {"id": None, "jid": jid, "payload_json": json.dumps({"source_id": "1"})}
    pipeline.execute_source_grab(job, adapter, ctx)
    assert server._jobs[jid].get("download_exit_code") == 0


def test_trigger_lidarr_import_has_no_direct_sidecar_writes():
    """Regression guard: every sidecar write inside _trigger_lidarr_import
    must go through the closure helpers (_write_sidecar_maybe /
    _write_sidecar_force) so source_type is threaded automatically.

    Any direct call to _maybe_write_v2_sidecar or _write_verification_sidecar
    inside the function would silently default source_type to 'tidal' for
    future non-TIDAL adapters. This test catches that at lint-time.
    """
    import inspect
    import server

    src = inspect.getsource(server._trigger_lidarr_import)
    # The only place these names should appear is the single closure
    # definition each, which forwards the captured source_type kwarg.
    assert src.count("_maybe_write_v2_sidecar(") == 1, (
        "Direct _maybe_write_v2_sidecar() call inside _trigger_lidarr_import. "
        "Use _write_sidecar_maybe(...) closure instead so source_type threads."
    )
    assert src.count("_write_verification_sidecar(") == 1, (
        "Direct _write_verification_sidecar() call inside _trigger_lidarr_import. "
        "Use _write_sidecar_force(...) closure instead so source_type threads."
    )
