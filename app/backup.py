"""Read-only state backup (Phase 3 slice 6 — export half).

Streams a zip of Mintarr's *queryable state* — the state_db, verification
sidecars, and decision/audit logs — but never the audio files. Read-only: it
opens files for reading and mutates nothing. Restore is intentionally a separate
later slice, since it overwrites state.
"""

from __future__ import annotations

import io
import logging
import os
import sqlite3
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path

log = logging.getLogger("tidalhires.backup")

_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None


def _snapshot_sqlite(db_path: Path) -> bytes:
    """Consistent SQLite snapshot via the online-backup API.

    state_db runs in WAL mode, so a raw byte-copy of the .db file while the
    container is live can miss WAL-pending writes. The backup API serialises a
    transactionally consistent image regardless of concurrent writers.
    """
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    try:
        dst = sqlite3.connect(tmp_name)
        try:
            src.backup(dst)
        finally:
            dst.close()
        return Path(tmp_name).read_bytes()
    finally:
        src.close()
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def build_backup_zip(
    *,
    state_db_path: Path | str | None,
    output_base: Path | str,
    archive_dirs: Mapping[str, Path | str],
    log_files: Mapping[str, Path | str],
) -> bytes:
    """Return an in-memory zip of Mintarr state (no audio files)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if state_db_path and Path(state_db_path).is_file():
            zf.writestr("state_db.sqlite", _snapshot_sqlite(Path(state_db_path)))

        base = Path(output_base)
        if base.is_dir():
            # Only the verification sidecars — never the audio under <jid>/.
            for sidecar in sorted(base.glob("*/verification.json")):
                zf.write(sidecar, f"sidecars/{sidecar.parent.name}/verification.json")

        for label, directory in archive_dirs.items():
            dpath = Path(directory)
            if dpath.is_dir():
                for sidecar in sorted(dpath.glob("*.json")):
                    zf.write(sidecar, f"archive/{label}/{sidecar.name}")

        for name, logfile in log_files.items():
            lpath = Path(logfile)
            if lpath.is_file():
                zf.write(lpath, f"logs/{name}")

    return buf.getvalue()


def write_backup_file(
    *,
    build_zip: Callable[[], bytes],
    backup_dir: Path | str,
    retention: int,
    now: float | None = None,
) -> Path:
    """Write one scheduled backup zip atomically and apply count retention.

    The builder is expected to be read-only. This function only writes inside
    ``backup_dir`` and never deletes anything except older ``mintarr-backup-*.zip``
    files in that same directory.
    """
    target_dir = Path(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now or time.time()))
    target = target_dir / f"mintarr-backup-{ts}.zip"
    suffix = 1
    while target.exists():
        target = target_dir / f"mintarr-backup-{ts}-{suffix}.zip"
        suffix += 1

    tmp = target_dir / f".{target.name}.tmp"
    try:
        tmp.write_bytes(build_zip())
        tmp.replace(target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            log.warning("failed to remove temporary backup file: %s", tmp)

    prune_scheduled_backups(target_dir, retention=retention)
    return target


def prune_scheduled_backups(backup_dir: Path | str, *, retention: int) -> list[Path]:
    """Delete older scheduled backup zips beyond ``retention``.

    ``retention <= 0`` disables pruning. Only files matching Mintarr's scheduled
    backup filename prefix are considered.
    """
    if retention <= 0:
        return []
    target_dir = Path(backup_dir)
    if not target_dir.is_dir():
        return []

    backups = sorted(
        (p for p in target_dir.glob("mintarr-backup-*.zip") if p.is_file()),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    removed: list[Path] = []
    for old in backups[retention:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            log.warning("failed to prune old backup: %s", old)
    return removed


def start_scheduled_backups(
    *,
    build_zip: Callable[[], bytes],
    backup_dir: Path | str,
    interval_seconds: float,
    retention: int,
) -> bool:
    """Start the scheduled backup thread. Idempotent; disabled by caller config."""
    global _scheduler_thread
    if interval_seconds <= 0:
        log.warning("scheduled backups not started: interval_seconds must be > 0")
        return False
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        log.debug("scheduled backup thread already running")
        return True

    target_dir = Path(backup_dir)
    _scheduler_stop.clear()

    def _loop() -> None:
        log.info(
            "scheduled backup thread started (dir=%s interval_seconds=%s retention=%s)",
            target_dir,
            interval_seconds,
            retention,
        )
        while not _scheduler_stop.wait(timeout=interval_seconds):
            try:
                written = write_backup_file(
                    build_zip=build_zip,
                    backup_dir=target_dir,
                    retention=retention,
                )
                log.info("scheduled backup written: %s", written)
            except Exception:
                log.exception("scheduled backup failed")
        log.info("scheduled backup thread exiting")

    _scheduler_thread = threading.Thread(
        target=_loop, name="mintarr-backup-scheduler", daemon=True
    )
    _scheduler_thread.start()
    return True


def stop_scheduled_backups(timeout: float = 5.0) -> None:
    """Signal scheduler shutdown and wait. Useful in tests."""
    global _scheduler_thread
    _scheduler_stop.set()
    if _scheduler_thread is not None:
        _scheduler_thread.join(timeout=timeout)
        if not _scheduler_thread.is_alive():
            _scheduler_thread = None


def is_scheduled_backup_running() -> bool:
    return _scheduler_thread is not None and _scheduler_thread.is_alive()
