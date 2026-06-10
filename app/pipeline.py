"""Common pipeline for source-grab jobs (F3.1).

Pipeline is split into four named phases per F3 design v0.3 §7:

    1. download_raw      (adapter-owned — TIDAL, future Local/Soulseek)
    2. normalize_audio   (.m4a/.alac → .flac, codec gate, flac -t)
    3. verify            (V2 verification — delegated to import_to_lidarr in F3.1)
    4. import_to_lidarr  (move to OUTPUT_BASE + trigger Lidarr ManualImport)

F3.1 is behavior-preserving. Phase boundaries are explicit so future
refactors (e.g., hoisting V2 verification out of _trigger_lidarr_import
when LocalFolderAdapter lands in F3.4) only touch one phase at a time.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from adapters.base import SourceAdapter
from adapters.context import PipelineContext

log = logging.getLogger("tidalhires.pipeline")


@dataclass
class NormalizeStats:
    codec_gate_skipped: int = 0
    conversion_failed: int = 0
    integrity_failed: int = 0


@dataclass
class PreparedOutput:
    output_dir: Path
    file_count: int
    total_bytes: int


def execute_source_grab(
    job: dict,
    adapter: SourceAdapter,
    ctx: PipelineContext,
) -> None:
    """Run the full pipeline for a source-grab job.

    Mirrors the previous `_run_download_job` flow but factored into named
    phases. State + Lidarr-call side effects remain in server.py helpers
    (called via ctx.set_progress, _trigger_lidarr_import, etc.).
    """
    import server  # lazy: pipeline runs inside the same process as server

    payload = json.loads(job.get("payload_json") or "{}")
    jid = ctx.jid
    candidate_id = str(payload.get("source_id") or payload.get("album_id") or "")
    if not candidate_id:
        raise ValueError(f"source-grab job missing source_id/album_id: {payload}")
    target_album_id = (
        payload.get("target_album_id")
        or payload.get("lidarr_album_id")
        or payload.get("album_id_lidarr")
        or payload.get("album_id_in_lidarr")
    )

    job_started = time.monotonic()
    with server._jobs_lock:
        server._jobs.setdefault(jid, {"id": jid})["status"] = "downloading"
        server._save_jobs()
    ctx.set_progress(stage="preparing", percent=2, message="Preparing source download")

    # === Phase 1: download_raw (adapter) =====================================
    download_started = time.monotonic()
    try:
        raw = adapter.download_raw(candidate_id, ctx)
    except Exception as exc:
        import worker

        if isinstance(exc, worker.JobCancelled):
            raise
        log.exception("[%s] %s download_raw failed", jid, adapter.name)
        with server._jobs_lock:
            server._jobs[jid].update(
                status="failed",
                error=str(exc),
                completed_at=time.time(),
            )
            server._save_jobs()
        raise
    server._record_job_timing(
        jid, "tidal_download_sec", time.monotonic() - download_started
    )
    ctx.check_cancelled()

    # === Phase 2: normalize_audio (common) ===================================
    normalize_started = time.monotonic()
    with server._jobs_lock:
        server._jobs[jid]["status"] = "processing"
        server._save_jobs()
    stats = normalize_audio(raw.files_dir, ctx)

    # === Phase 3: verify (common — V2 verification) ==========================
    # F3.1: V2 verification continues to run inside _trigger_lidarr_import
    # (which builds sensor results, scores, writes sidecar, then calls
    # Lidarr). Hoisting it out is F3.4+ work once LocalFolderAdapter
    # motivates a separate verify phase.
    ctx.check_cancelled()

    # === Phase 4: import_to_lidarr (common) ==================================
    prepared = prepare_output_directory(raw.files_dir, ctx)
    server._record_job_timing(
        jid, "postprocess_sec", time.monotonic() - normalize_started
    )
    server._record_job_timing(
        jid, "pre_import_total_sec", time.monotonic() - job_started
    )
    ctx.set_progress(
        stage="ready_for_import",
        percent=65,
        message="Files prepared for Lidarr import",
        file_count=prepared.file_count,
        size_bytes=prepared.total_bytes,
    )
    with server._jobs_lock:
        server._jobs[jid].update(
            status="processing",
            output_dir=str(prepared.output_dir),
            size=prepared.total_bytes,
            file_count=prepared.file_count,
            # Behavior-preserving: legacy _run_download_job set this from
            # the tidal-dl-ng exit code (always 0 on a successful download
            # since failures raised before reaching here). Keep the field
            # so audit-log readers that expect it don't see a gap.
            download_exit_code=0,
            codec_gate_skipped=stats.codec_gate_skipped,
            conversion_failed=stats.conversion_failed,
            integrity_failed=stats.integrity_failed,
            percent=100,
        )
        server._save_jobs()
    log.info(
        "[%s] Done — %d files, %d MB in %s",
        jid,
        prepared.file_count,
        prepared.total_bytes // (1024 * 1024),
        prepared.output_dir,
    )

    import_to_lidarr(
        prepared.output_dir,
        ctx,
        source_type=adapter.source_type,
        target_album_id=target_album_id,
    )


def normalize_audio(raw_dir: Path, ctx: PipelineContext) -> NormalizeStats:
    """Convert .m4a → .flac, then validate via codec gate + flac -t.

    F3.1 keeps codec gate and flac -t inside the per-file loop because
    that's how the current pipeline runs (avoids creating fake AAC-in-FLAC
    files). A cleaner split is F3.4+ work when LocalFolderAdapter lands.
    """
    m4a_files = list(raw_dir.rglob("*.m4a"))
    direct_flac_files = list(raw_dir.rglob("*.flac"))
    log.info(
        "[%s] Normalize start: %d .m4a + %d .flac",
        ctx.jid,
        len(m4a_files),
        len(direct_flac_files),
    )
    ctx.set_progress(
        stage="postprocess",
        percent=50,
        message="Converting and validating files",
        m4a_files=len(m4a_files),
        flac_files=len(direct_flac_files),
    )

    stats = NormalizeStats()
    for m in m4a_files:
        ctx.check_cancelled()
        flac_path = m.with_suffix(".flac")
        try:
            # Codec gate: ffprobe must confirm audio stream is FLAC or ALAC.
            # Otherwise ffmpeg copy/re-encode would silently produce lossy.
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "csv=p=0",
                    str(m),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            codec = (probe.stdout or "").strip().lower()
            if codec not in ("flac", "alac"):
                log.warning(
                    "[%s] CODEC GATE: skip %s — codec=%r (expected flac/alac)",
                    ctx.jid,
                    m.name,
                    codec,
                )
                stats.codec_gate_skipped += 1
                m.unlink(missing_ok=True)
                continue

            # ffmpeg copy first (bit-perfect FLAC stream from MP4 container).
            # Fallback to re-encode (still lossless since codec=flac/alac).
            r = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(m),
                    "-vn",
                    "-c:a",
                    "copy",
                    str(flac_path),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if (
                r.returncode != 0
                or not flac_path.exists()
                or flac_path.stat().st_size < 1024
            ):
                r2 = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        "error",
                        "-i",
                        str(m),
                        "-vn",
                        "-c:a",
                        "flac",
                        "-compression_level",
                        "5",
                        str(flac_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if r2.returncode != 0:
                    log.error(
                        "[%s] ffmpeg error for %s: %s", ctx.jid, m, r2.stderr[-500:]
                    )
                    stats.conversion_failed += 1
                    continue
            m.unlink()

            # Integrity gate: flac -t verifies stream decodes and MD5 matches.
            test = subprocess.run(
                ["flac", "-t", "-s", str(flac_path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if test.returncode != 0:
                log.error(
                    "[%s] INTEGRITY: flac -t failed for %s — deleting. stderr=%s",
                    ctx.jid,
                    flac_path.name,
                    (test.stderr or "")[-300:],
                )
                stats.integrity_failed += 1
                flac_path.unlink(missing_ok=True)
                continue
        except Exception:
            log.exception("[%s] Conversion/validation failed for %s", ctx.jid, m)

    # Test integrity for .flac files the adapter delivered directly
    # (e.g., 16/44 streams that didn't go through .m4a container).
    for f in list(raw_dir.rglob("*.flac")):
        ctx.check_cancelled()
        try:
            test = subprocess.run(
                ["flac", "-t", "-s", str(f)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if test.returncode != 0:
                log.error(
                    "[%s] INTEGRITY (direct-flac): flac -t failed for %s — deleting",
                    ctx.jid,
                    f.name,
                )
                stats.integrity_failed += 1
                f.unlink(missing_ok=True)
        except Exception:
            log.exception("[%s] flac -t failed for %s", ctx.jid, f)

    return stats


def prepare_output_directory(
    raw_dir: Path,
    ctx: PipelineContext,
) -> PreparedOutput:
    """Move raw_dir → ctx.output_dir and compute final stats.

    Falls back to raw_dir if the move fails (preserves existing behavior).
    """
    ctx.check_cancelled()
    output_dir = ctx.output_dir
    try:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(raw_dir), str(output_dir))
    except Exception:
        log.exception("[%s] Move to output failed", ctx.jid)
        output_dir = raw_dir

    total_bytes = sum(f.stat().st_size for f in output_dir.rglob("*") if f.is_file())
    file_count = sum(1 for f in output_dir.rglob("*") if f.is_file())
    return PreparedOutput(
        output_dir=output_dir,
        file_count=file_count,
        total_bytes=int(total_bytes),
    )


def import_to_lidarr(
    output_dir: Path,
    ctx: PipelineContext,
    *,
    source_type: str,
    target_album_id: int | str | None = None,
) -> None:
    """Trigger Lidarr direct-import (which internally runs V2 verification
    + writes sidecar + calls Lidarr ManualImport).

    source_type is threaded all the way into _trigger_lidarr_import's
    sidecar writes via closure helpers — adapters reach the persisted
    record with their actual provenance, not a 'tidal' default.
    """
    import server

    # Defer cancel cleanup so a late-stage cancel doesn't rmtree the output_dir
    # before Lidarr has imported it.
    server._raise_if_job_cancelled(
        ctx.worker_job_id, ctx.jid, output_dir, cleanup=False
    )
    try:
        server._trigger_lidarr_import(
            ctx.jid,
            output_dir,
            worker_job_id=ctx.worker_job_id,
            source_type=source_type,
            target_album_id=target_album_id,
        )
        ctx.set_progress(
            stage="finalizing",
            percent=98,
            message="Finalizing pipeline result",
        )
    except Exception as exc:
        import worker

        # A late cancel raised inside _trigger_lidarr_import (e.g. before the
        # ManualImport POST or rescue) must terminalize the job as *cancelled*, not
        # as an import failure — otherwise the worker contract is broken and the
        # operator's cancel looks like a Lidarr error. Re-raise so worker handles it.
        if isinstance(exc, worker.JobCancelled):
            raise
        log.exception(
            "[%s] Lidarr import failed (manual import via Lidarr UI possible)",
            ctx.jid,
        )
        server._mark_import_failed(ctx.jid, f"lidarr import exception: {exc}")
