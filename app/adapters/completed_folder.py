"""Completed-folder source adapters for operator-routed download clients.

These adapters intentionally mirror the Soulseek completed-folder safety model:
source folders are read-only, paths are relative to a configured root, partial
markers are rejected, and a size/mtime snapshot must settle before copy.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path

from .base import RawDownload, ReleaseCandidate
from .context import PipelineContext

log = logging.getLogger("tidalhires.adapter.completed_folder")

_SUPPORTED_AUDIO_SUFFIXES = (".flac", ".m4a")
_PARTIAL_SUFFIXES = (
    ".!qb",
    ".crdownload",
    ".download",
    ".incomplete",
    ".part",
    ".partial",
    ".qbt!",
    ".tmp",
)
_PARTIAL_PATH_PARTS = {"__admin__", "_unpack_", "_failed_"}


def hash_rel(rel: str) -> str:
    """Stable short id from normalized relative path."""
    return hashlib.sha256(rel.encode()).hexdigest()[:12]


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


class CompletedFolderAdapter:
    """Read-only completed-folder adapter used by SAB/qBittorrent lanes."""

    name: str
    source_type: str
    display_name: str
    source_label: str
    env_prefix: str

    def __init__(
        self,
        *,
        download_root: str | None = None,
        enabled: bool | None = None,
        max_files: int | None = None,
        max_bytes: int | None = None,
        settle_seconds: float | None = None,
    ) -> None:
        root = download_root or os.environ.get(f"{self.env_prefix}_DOWNLOAD_ROOT") or ""
        self._download_root = Path(root)
        self._enabled_override = enabled
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._settle_seconds = settle_seconds

    @property
    def max_files(self) -> int:
        if self._max_files is not None:
            return int(self._max_files)
        return _env_int(f"{self.env_prefix}_MAX_FILES", 300)

    @property
    def max_bytes(self) -> int:
        if self._max_bytes is not None:
            return int(self._max_bytes)
        return _env_int(f"{self.env_prefix}_MAX_BYTES", 0)

    @property
    def settle_seconds(self) -> float:
        if self._settle_seconds is not None:
            return max(0.0, float(self._settle_seconds))
        return float(_env_int(f"{self.env_prefix}_SETTLE_SECONDS", 10))

    def is_enabled(self) -> bool:
        enabled = self._enabled_override
        if enabled is None:
            enabled = _env_bool(f"{self.env_prefix}_ENABLED")
        return bool(enabled) and self._download_root.is_dir()

    def search(
        self,
        query: str,
        artist: str = "",
        album: str = "",
        year: int | None = None,
    ) -> list[ReleaseCandidate]:
        """Completed-folder connectors are manually enqueued for now."""
        return []

    def normalize_candidate_id(self, rel_path: str) -> str:
        src = self.resolve_source_dir(rel_path, check_completed=True)
        return src.relative_to(self._download_root.resolve()).as_posix()

    def resolve_source_dir(self, rel_path: str, *, check_completed: bool) -> Path:
        if not rel_path or Path(rel_path).is_absolute():
            raise RuntimeError(f"{self.source_label} path must be relative")
        root = self._download_root.resolve()
        raw_src = root / rel_path
        cur = root
        for part in Path(rel_path).parts:
            if part.lower() in _PARTIAL_PATH_PARTS:
                raise RuntimeError(f"{self.source_label} folder has partial markers")
            cur = cur / part
            if cur.is_symlink():
                raise RuntimeError(f"symlink blocked: {Path(rel_path)}")
        src = raw_src.resolve()
        if not src.is_relative_to(root):
            raise RuntimeError(f"path traversal blocked: {rel_path}")
        if not src.is_dir():
            raise RuntimeError(f"{self.source_label} source not a directory: {src}")
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
            lower_name = f.name.lower()
            if lower_name.endswith(_PARTIAL_SUFFIXES) or any(
                part.lower() in _PARTIAL_PATH_PARTS for part in f.relative_to(src).parts
            ):
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
            raise RuntimeError(f"{self.source_label} folder has partial markers")
        if not audio_files:
            raise RuntimeError(f"no supported audio files found in {src}")
        if len(first) > self.max_files:
            raise RuntimeError(
                f"{self.source_label} folder has too many files: "
                f"{len(first)} > {self.max_files}"
            )
        if self.max_bytes and total_bytes > self.max_bytes:
            raise RuntimeError(
                f"{self.source_label} folder exceeds max bytes: "
                f"{total_bytes} > {self.max_bytes}"
            )
        settle_seconds = self.settle_seconds
        if settle_seconds:
            time.sleep(settle_seconds)
            second, audio_files_2, total_bytes_2, partial_files_2 = self._snapshot(src)
            if partial_files_2:
                raise RuntimeError(f"{self.source_label} folder has partial markers")
            if (
                second != first
                or audio_files_2 != audio_files
                or total_bytes_2 != total_bytes
            ):
                raise RuntimeError(f"{self.source_label} folder not settled")
        return len(first), audio_files, total_bytes

    def download_raw(
        self,
        candidate_id: str,
        ctx: PipelineContext,
    ) -> RawDownload:
        src = self.resolve_source_dir(candidate_id, check_completed=True)
        root_resolved = self._download_root.resolve()

        ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        ctx.set_progress(
            stage="copying",
            percent=10,
            message=f"Copying {self.display_name} files",
        )

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
            message=f"{self.display_name} copy complete",
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


class SabUsenetCompletedAdapter(CompletedFolderAdapter):
    name = "sab_usenet"
    source_type = "sab_usenet"
    display_name = "SABnzbd"
    source_label = "sab_usenet"
    env_prefix = "SAB_USENET"


class QBittorrentCompletedAdapter(CompletedFolderAdapter):
    name = "qbittorrent_torrent"
    source_type = "qbittorrent_torrent"
    display_name = "qBittorrent"
    source_label = "qbittorrent_torrent"
    env_prefix = "QBITTORRENT_TORRENT"


class BackendCompletedAdapter(CompletedFolderAdapter):
    """Ingest for a Mintarr-managed backend download (ADR-0014 slice 5).

    Distinct source_type from the operator-routed completed-folder adapters: a
    release here was *created and tracked by Mintarr's own backend lane*, not
    dropped into a folder by the operator. The candidate_id is the completed
    path relative to the backend download root, so resolve_source_dir
    re-validates containment (root-relative, no symlink/traversal, settled, no
    partial markers) right before the copy. Copy-not-move keeps qBit seeding
    intact; backend cleanup is a later slice.
    """

    name = "backend_completed"
    source_type = "backend_completed"
    display_name = "Mintarr backend"
    source_label = "backend_completed"
    env_prefix = "MINTARR_SAB_BACKEND"
