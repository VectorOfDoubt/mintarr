"""Performance baseline for the mocked full source-grab pipeline.

This module is intentionally skipped in the normal pytest suite. The standard
CI suite should stay fast and deterministic; the dedicated performance workflow
sets RUN_PERF_BENCHMARKS=1 and runs this file with pytest-benchmark.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PERF_BENCHMARKS") != "1",
    reason="performance benchmarks run only in the dedicated workflow",
)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _BenchmarkContext:
    def __init__(self, *, jid: str, raw_dir: Path, output_dir: Path):
        self.jid = jid
        self.worker_job_id = None
        self.raw_dir = raw_dir
        self.output_dir = output_dir
        self.progress_calls: list[dict] = []
        self.cancel_checks = 0

    def check_cancelled(self):
        self.cancel_checks += 1

    def run_subprocess(self, argv, *, timeout, text=True):
        return _FakeCompleted(returncode=0)

    def set_progress(self, *, stage, percent, message="", **extra):
        self.progress_calls.append(
            {"stage": stage, "percent": percent, "message": message, **extra}
        )

    def log(self, level, msg, *args, **fields):
        pass


class _BenchmarkAdapter:
    name = "benchmark"
    source_type = "benchmark"

    def is_enabled(self):
        return True

    def search(self, *args, **kwargs):
        return []

    def download_raw(self, candidate_id, ctx):
        from adapters.base import RawDownload

        ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        for index in range(8):
            payload = b"FAKEFLAC" + bytes(4096)
            path = ctx.raw_dir / f"{index + 1:02d} benchmark.flac"
            path.write_bytes(payload)
            total_bytes += len(payload)
        return RawDownload(files_dir=ctx.raw_dir, file_count=8, total_bytes=total_bytes)

    def cleanup(self, jid, ctx):
        pass


@pytest.fixture
def pipeline_benchmark_env(tmp_path, monkeypatch):
    import server
    import pipeline

    monkeypatch.setattr(server, "OUTPUT_BASE", tmp_path / "output")
    monkeypatch.setattr(server, "DOWNLOAD_BASE", tmp_path / "downloads")
    monkeypatch.setattr(server, "_trigger_lidarr_import", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_raise_if_job_cancelled", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_record_job_timing", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_save_jobs", lambda: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _FakeCompleted(returncode=0),
    )

    return server, pipeline


def test_full_pipeline_orchestration_benchmark(
    benchmark,
    pipeline_benchmark_env,
):
    """Benchmark the mocked full source-grab orchestration path.

    External subprocesses and network services are mocked, so this is a
    regression guard for Python orchestration, filesystem staging, state updates,
    and queue/pipeline glue. It is not a real-world download throughput test.
    """

    server, pipeline = pipeline_benchmark_env
    adapter = _BenchmarkAdapter()
    counter = itertools.count(1)

    def run_once():
        sequence = next(counter)
        jid = f"perf-{sequence}"
        ctx = _BenchmarkContext(
            jid=jid,
            raw_dir=server.DOWNLOAD_BASE / jid,
            output_dir=server.OUTPUT_BASE / jid,
        )
        job = {
            "id": None,
            "jid": jid,
            "payload_json": json.dumps({"source_id": f"candidate-{sequence}"}),
        }
        pipeline.execute_source_grab(job, adapter, ctx)

    benchmark.pedantic(run_once, rounds=20, iterations=1)
