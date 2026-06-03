"""LocalFolderAdapter — ingest local FLAC/M4A folders (F3.4).

Proof-of-design that the F3.1 adapter abstraction tolerates a non-TIDAL
source without touching pipeline.py, verification.py or V2 policy. The
adapter copies (does not move) files from LOCAL_INGEST_PATH/<rel-path>
into ctx.raw_dir; the common pipeline then runs normalize → verify →
import_to_lidarr unchanged.

Locked decisions (per F3.4 design v0.2 §10):
- source_id = normalized POSIX relative path inside LOCAL_INGEST_PATH
- search() returns [] until F3.3/F3.4.x wire newznab/scan exposure
- symlinks blocked outright in copy loop
- source files left UNTOUCHED — no move, no consume marker in F3.4
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

from .base import RawDownload, ReleaseCandidate
from .context import PipelineContext

log = logging.getLogger("tidalhires.adapter.local_folder")


_SUPPORTED_AUDIO_SUFFIXES = (".flac", ".m4a")


class LocalFolderAdapter:
    name = "local"
    source_type = "local"

    def __init__(self, *, ingest_root: str | None = None) -> None:
        root = ingest_root or os.environ.get("LOCAL_INGEST_PATH") or "/local-ingest"
        self._ingest_root = Path(root)

    def is_enabled(self) -> bool:
        return self._ingest_root.is_dir()

    def search(
        self,
        query: str,
        artist: str = "",
        album: str = "",
        year: int | None = None,
    ) -> list[ReleaseCandidate]:
        """F3.3: scan LOCAL_INGEST_PATH/<artist>/<album>/ and return matching
        FLAC albums as ReleaseCandidates.

        Lidarr indexer-test pings with an empty query; we deliberately return
        [] in that case so the test does not expose arbitrary local folders.
        Cap at 100 candidates to keep response time bounded.
        """
        if not self.is_enabled():
            return []
        q_lower = (query or "").lower()
        artist_lower = (artist or "").lower()
        album_lower = (album or "").lower()
        if not (q_lower or artist_lower or album_lower):
            return []

        root = self._ingest_root.resolve()
        candidates: list[ReleaseCandidate] = []
        try:
            artist_iter = sorted(self._ingest_root.iterdir())
        except OSError:
            log.exception("LocalFolder search: failed to list ingest root")
            return []

        for artist_dir in artist_iter:
            if artist_dir.is_symlink() or not artist_dir.is_dir():
                continue
            if artist_lower and artist_lower not in artist_dir.name.lower():
                continue
            try:
                album_iter = sorted(artist_dir.iterdir())
            except OSError:
                continue
            for album_dir in album_iter:
                if album_dir.is_symlink() or not album_dir.is_dir():
                    continue
                if album_lower and album_lower not in album_dir.name.lower():
                    continue
                if q_lower:
                    haystack = f"{artist_dir.name} {album_dir.name}".lower()
                    if q_lower not in haystack:
                        continue
                try:
                    flac_files = [
                        f
                        for f in album_dir.iterdir()
                        if f.is_file()
                        and not f.is_symlink()
                        and f.suffix.lower() == ".flac"
                    ]
                except OSError:
                    continue
                if not flac_files:
                    continue
                rel = album_dir.relative_to(root).as_posix()
                total = sum(f.stat().st_size for f in flac_files)
                candidates.append(
                    ReleaseCandidate(
                        source_type=self.source_type,
                        source_id=rel,
                        title=f"{artist_dir.name} - {album_dir.name} (FLAC) [Local]",
                        artist=artist_dir.name,
                        album=album_dir.name,
                        year=None,
                        quality_tag="FLAC",
                        size_bytes=total,
                        download_url=f"local:{rel}",
                        priority=30,
                    )
                )
                if len(candidates) >= 100:
                    return candidates
        return candidates

    def normalize_candidate_id(self, rel_path: str) -> str:
        """Return canonical POSIX relative path inside ingest_root.

        Called by /local/ingest before enqueue and again by download_raw.
        Raises RuntimeError if the resolved path escapes ingest_root or is
        not a directory.
        """
        src = self.resolve_source_dir(rel_path)
        return src.relative_to(self._ingest_root.resolve()).as_posix()

    def resolve_source_dir(self, rel_path: str) -> Path:
        if not rel_path or Path(rel_path).is_absolute():
            raise RuntimeError("local path must be relative")
        root = self._ingest_root.resolve()
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
            raise RuntimeError(f"local source not a directory: {src}")
        return src

    def download_raw(
        self,
        candidate_id: str,
        ctx: PipelineContext,
    ) -> RawDownload:
        """Copy files from ingest_root/<rel-path> into ctx.raw_dir.

        Defense-in-depth: resolve_source_dir validates again at copy time
        even though /local/ingest already normalized. Symlinked files and
        symlinked subdirectories are rejected outright.
        """
        src = self.resolve_source_dir(candidate_id)
        root_resolved = self._ingest_root.resolve()

        ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        ctx.set_progress(stage="copying", percent=10, message="Copying local files")

        copied = 0
        audio_files = 0
        total_bytes = 0
        for f in src.rglob("*"):
            ctx.check_cancelled()
            if f.is_symlink():
                raise RuntimeError(f"symlink blocked: {f.relative_to(src)}")
            if not f.is_file():
                continue
            # Defense-in-depth: even non-symlinked files must resolve inside root.
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
            message="Local copy complete",
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
        # No-op: source files are UNTOUCHED (we copied, not moved).
        return None


def hash_rel(rel: str) -> str:
    """Stable short id from normalized relative path — used only for dedupe/log."""
    return hashlib.sha256(rel.encode()).hexdigest()[:12]
