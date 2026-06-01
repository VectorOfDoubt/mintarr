"""Soulseek completed-folder adapter (F3.5a).

This adapter ingests folders already completed by slskd/Soulseek. It does
not talk to slskd HTTP and does not move/delete source files.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from .base import RawDownload, ReleaseCandidate
from .context import PipelineContext
from .local_folder import hash_rel

log = logging.getLogger("tidalhires.adapter.soulseek")

_SUPPORTED_AUDIO_SUFFIXES = (".flac", ".m4a")
_PARTIAL_SUFFIXES = (".part", ".partial", ".tmp", ".download", ".crdownload", ".incomplete")


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


class SoulseekCompletedAdapter:
    name = "soulseek"
    source_type = "soulseek"

    def __init__(
        self,
        *,
        download_root: str | None = None,
        enabled: bool | None = None,
        max_files: int | None = None,
        max_bytes: int | None = None,
        settle_seconds: float | None = None,
        search_enabled: bool | None = None,
    ) -> None:
        self._download_root = Path(
            download_root or os.environ.get("SOULSEEK_DOWNLOAD_ROOT", "")
        )
        self._enabled_override = enabled
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._settle_seconds = settle_seconds
        self._search_enabled = search_enabled

    @property
    def max_files(self) -> int:
        return self._max_files if self._max_files is not None else _env_int("SOULSEEK_MAX_FILES", 300)

    @property
    def max_bytes(self) -> int:
        return self._max_bytes if self._max_bytes is not None else _env_int("SOULSEEK_MAX_BYTES", 0)

    @property
    def settle_seconds(self) -> float:
        if self._settle_seconds is not None:
            return max(0.0, float(self._settle_seconds))
        return float(_env_int("SOULSEEK_SETTLE_SECONDS", 10))

    def is_enabled(self) -> bool:
        enabled = self._enabled_override
        if enabled is None:
            enabled = _env_bool("SOULSEEK_ENABLED")
        return bool(enabled) and self._download_root.is_dir()

    def search(
        self,
        query: str,
        artist: str = "",
        album: str = "",
        year: int | None = None,
    ) -> list[ReleaseCandidate]:
        """F3.5a is manual completed-folder ingest only.

        `SOULSEEK_SEARCH_ENABLED` is reserved for F3.5b slskd HTTP search; it
        intentionally does not expose completed folders through Newznab yet.
        """
        search_enabled = self._search_enabled
        if search_enabled is None:
            search_enabled = _env_bool("SOULSEEK_SEARCH_ENABLED")
        if not search_enabled:
            return []
        return []

    def normalize_candidate_id(self, rel_path: str) -> str:
        src = self.resolve_source_dir(rel_path, check_completed=True)
        return src.relative_to(self._download_root.resolve()).as_posix()

    def resolve_source_dir(self, rel_path: str, *, check_completed: bool) -> Path:
        if not rel_path or Path(rel_path).is_absolute():
            raise RuntimeError("soulseek path must be relative")
        root = self._download_root.resolve()
        raw_src = root / rel_path
        cur = root
        for part in Path(rel_path).parts:
            cur = cur / part
            if cur.is_symlink():
                raise RuntimeError(f"symlink blocked: {Path(rel_path)}")
        src = raw_src.resolve()
        if not src.is_relative_to(root):
            raise RuntimeError(f"path traversal blocked: {rel_path}")
        if not src.is_dir():
            raise RuntimeError(f"soulseek source not a directory: {src}")
        if check_completed:
            self._validate_completed_dir(src)
        return src

    def _snapshot(self, src: Path) -> tuple[dict[str, tuple[int, int]], int, int, int]:
        files: dict[str, tuple[int, int]] = {}
        audio_files = 0
        total_bytes = 0
        partial_files = 0
        root = self._download_root.resolve()
        for f in src.rglob("*"):
            if f.is_symlink():
                raise RuntimeError(f"symlink blocked: {f.relative_to(src)}")
            if not f.is_file():
                continue
            if not f.resolve().is_relative_to(root):
                raise RuntimeError(f"path escape blocked: {f}")
            rel = f.relative_to(src).as_posix()
            if f.name.lower().endswith(_PARTIAL_SUFFIXES):
                partial_files += 1
            stat = f.stat()
            files[rel] = (stat.st_size, stat.st_mtime_ns)
            total_bytes += stat.st_size
            if f.suffix.lower() in _SUPPORTED_AUDIO_SUFFIXES:
                audio_files += 1
        return files, audio_files, total_bytes, partial_files

    def _validate_completed_dir(self, src: Path) -> tuple[int, int, int]:
        first, audio_files, total_bytes, partial_files = self._snapshot(src)
        if partial_files:
            raise RuntimeError("soulseek folder has partial download markers")
        if not audio_files:
            raise RuntimeError(f"no supported audio files found in {src}")
        if len(first) > self.max_files:
            raise RuntimeError(f"soulseek folder has too many files: {len(first)} > {self.max_files}")
        if self.max_bytes and total_bytes > self.max_bytes:
            raise RuntimeError(f"soulseek folder exceeds max bytes: {total_bytes} > {self.max_bytes}")
        settle_seconds = self.settle_seconds
        if settle_seconds:
            time.sleep(settle_seconds)
            second, audio_files_2, total_bytes_2, partial_files_2 = self._snapshot(src)
            if partial_files_2:
                raise RuntimeError("soulseek folder has partial download markers")
            if second != first or audio_files_2 != audio_files or total_bytes_2 != total_bytes:
                raise RuntimeError("soulseek folder not settled")
        return len(first), audio_files, total_bytes

    def download_raw(
        self,
        candidate_id: str,
        ctx: PipelineContext,
    ) -> RawDownload:
        src = self.resolve_source_dir(candidate_id, check_completed=True)
        root_resolved = self._download_root.resolve()

        ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        ctx.set_progress(stage="copying", percent=10, message="Copying Soulseek files")

        copied = 0
        audio_files = 0
        total_bytes = 0
        for f in src.rglob("*"):
            ctx.check_cancelled()
            if f.is_symlink():
                raise RuntimeError(f"symlink blocked: {f.relative_to(src)}")
            if not f.is_file():
                continue
            if not f.resolve().is_relative_to(root_resolved):
                raise RuntimeError(f"path escape blocked: {f}")
            rel = f.relative_to(src)
            dst = ctx.raw_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(f), str(dst))
            copied += 1
            if f.suffix.lower() in _SUPPORTED_AUDIO_SUFFIXES:
                audio_files += 1
            total_bytes += f.stat().st_size

        if audio_files == 0:
            raise RuntimeError(f"no supported audio files copied from {src}")

        ctx.set_progress(
            stage="copied",
            percent=45,
            message="Soulseek copy complete",
            file_count=copied,
            audio_files=audio_files,
            size_bytes=total_bytes,
        )
        return RawDownload(
            files_dir=ctx.raw_dir,
            file_count=copied,
            total_bytes=int(total_bytes),
        )

    def cleanup(self, jid: str, ctx: PipelineContext) -> None:
        return None


__all__ = ["SoulseekCompletedAdapter", "hash_rel"]
