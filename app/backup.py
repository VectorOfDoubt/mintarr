"""Read-only state backup (Phase 3 slice 6 — export half).

Streams a zip of Mintarr's *queryable state* — the state_db, verification
sidecars, and decision/audit logs — but never the audio files. Read-only: it
opens files for reading and mutates nothing. Restore is intentionally a separate
later slice, since it overwrites state.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path


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
